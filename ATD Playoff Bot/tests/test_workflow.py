import asyncio
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,Mock,patch

import controls
import engine
import render
import workflow
from store import Store
from test_playoffs import roster


SOURCE=dict(sheet='roster-sheet',tab='ATD 113',standings=dict(sheet='voting-sheet',gid=5))


def teams():
    return [dict(seed=i,name=f'Team {i}',logo=f'<:Team{i}:{100+i}>',roster=roster(f'Team {i} player')) for i in range(1,17)]


def sheet_rows(data):
    rows=[]
    for team in data:
        rows.append([team['name'],'Year'])
        rows.extend([[p['name'],p['year']] for p in team['roster']])
        rows.append([])
    return rows


class SetupParsingTests(unittest.TestCase):
    def test_exact_standings_decimal_seeds_and_logo_identity(self):
        rows=[['Team','Wins','Losses','GM']]+[[f'Team {i}',str(60-i/100),'22','GM'] for i in range(1,18)]
        result=workflow.standings(rows)
        self.assertEqual([r['seed'] for r in result],list(range(1,17)))
        self.assertEqual(result[0]['name'],'Team 1')
        self.assertEqual(workflow.parse_request('ATD 114 | https://docs.google.com/spreadsheets/d/abcdefghijklmnopqrst/edit#gid=3 | New Tab')[0],114)
        self.assertEqual(workflow.voting_source('https://docs.google.com/spreadsheets/d/abcdefghijklmnopqrst/edit#gid=3')['gid'],3)
        with self.assertRaises(ValueError):workflow.standings(rows[:10])
        with self.assertRaises(ValueError):workflow.standings(rows+[rows[1]])
        with self.assertRaises(ValueError):workflow.team_logo('Denver Rockets',[dict(name='Rockets',id='1')])
        self.assertIn('2',workflow.team_logo('Denver Rockets',[dict(name='DVR',id='2')]))

    def test_bulk_reader_reads_roster_once_for_all_16_teams(self):
        data=teams();rows=sheet_rows(data)
        tab=NS(id=5,get_all_values=Mock(return_value=[['Team','Wins','Losses','GM']]+[[t['name'],str(70-t['seed']),'22','GM'] for t in data]))
        roster_tab=NS(get_all_values=Mock(return_value=rows))
        voting=NS(get_worksheet_by_id=Mock(return_value=tab))
        book=NS(worksheet=Mock(return_value=roster_tab))
        client=NS(open_by_key=Mock(side_effect=lambda key:voting if key=='voting-sheet' else book))
        emojis=[dict(name=f'Team{i}',id=str(100+i)) for i in range(1,17)]
        with patch.object(workflow.sheet_source,'client',return_value=client):
            result,source=workflow.read_setup(113,copy.deepcopy(SOURCE),emojis,'unused')
        self.assertEqual(len(result),16)
        roster_tab.get_all_values.assert_called_once()
        self.assertEqual(sum(len(t['roster']) for t in result),160)


class LiveWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Store(Path(self.tmp.name)/'state.sqlite3')
        self.data=teams()
        self.cache=Path(self.tmp.name)/'no-cache.sqlite3'

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def installed(self):
        season,_=self.db.install_setup(1,113,copy.deepcopy(self.data),copy.deepcopy(SOURCE),99)
        s=self.db.all_series(1)[0]
        self.db.configure(1,s['id'],99,base=5000)
        return season,s['id']

    async def test_single_setup_imports_everything_and_is_idempotent(self):
        progress=AsyncMock()
        with patch.object(workflow,'read_setup',return_value=(copy.deepcopy(self.data),copy.deepcopy(SOURCE))) as read,\
             patch.object(workflow.historical,'sync',side_effect=lambda players,path:(players,[])) as stats:
            season,created,warnings=await workflow.setup(self.db,1,99,'ATD 113',[],'unused',self.cache,progress)
            self.assertTrue(created)
            self.assertEqual(len(self.db.teams(season['id'])),16)
            self.assertEqual(len(self.db.all_series(1)),8)
            self.assertEqual(stats.call_count,16)
            _,again,_=await workflow.setup(self.db,1,99,'ATD 113',[],'unused',self.cache,progress)
            self.assertFalse(again)
            read.assert_called_once()

    async def test_invalid_bulk_setup_leaves_no_partial_season(self):
        invalid=copy.deepcopy(self.data);invalid[3]['roster'][0]['name']=invalid[0]['roster'][0]['name']
        with self.assertRaises(ValueError):self.db.install_setup(1,113,invalid,SOURCE,99)
        with self.assertRaises(ValueError):self.db.season(1)

    async def test_changed_lineup_used_for_next_game_and_old_result_unchanged(self):
        _,sid=self.installed()
        with patch.object(workflow.sheet_source,'load_rows',return_value=sheet_rows(self.data)):
            s,g1,_=await workflow.play(self.db,1,sid,1,99,'unused',self.cache)
        original=copy.deepcopy(g1)
        changed=copy.deepcopy(self.data)
        changed[0]['roster'][0],changed[0]['roster'][5]=changed[0]['roster'][5],changed[0]['roster'][0]
        with patch.object(workflow.sheet_source,'load_rows',return_value=sheet_rows(changed)):
            s,g2,_=await workflow.play(self.db,1,sid,2,99,'unused',self.cache)
        self.assertEqual(g2['boxes'][0][0]['name'],changed[0]['roster'][0]['name'])
        self.assertEqual(g2['rosters'][0][0]['position'],'PG')
        self.assertEqual(self.db.game(1,sid,1)[1],original)
        averages=render.averages(s,[g1,g2])
        for player in averages[0]:
            values=[p['pts'] for g in (g1,g2) for p in g['boxes'][0] if p['name']==player['name']]
            self.assertEqual(player['pts'],sum(values)/2)
        replay=copy.deepcopy(s);replay['high']['roster'],replay['low']['roster']=g1['rosters']
        self.assertEqual(engine.simulate(replay,1,g1['seed'])['boxes'],g1['boxes'])

    async def test_sheet_failure_blocks_new_roll_and_saved_games_need_no_sheet(self):
        _,sid=self.installed()
        with patch.object(workflow.sheet_source,'load_rows',side_effect=OSError('offline')):
            with self.assertRaises(OSError):await workflow.play(self.db,1,sid,1,99,'unused',self.cache)
        self.assertEqual(self.db.games(1,sid),[])
        with patch.object(workflow.sheet_source,'load_rows',return_value=sheet_rows(self.data)):
            _,g,_=await workflow.play(self.db,1,sid,1,99,'unused',self.cache)
        with patch.object(workflow.sheet_source,'load_rows',side_effect=OSError('offline')) as read:
            _,saved,created=await workflow.play(self.db,1,sid,1,99,'unused',self.cache)
            read.assert_not_called()
        self.assertFalse(created);self.assertEqual(saved,g)

    async def test_duplicate_click_uses_one_roll_and_one_sheet_read(self):
        _,sid=self.installed()
        with patch.object(workflow.sheet_source,'load_rows',return_value=sheet_rows(self.data)) as read:
            results=await asyncio.gather(*(workflow.play(self.db,1,sid,1,99,'unused',self.cache) for _ in range(3)))
        self.assertEqual(sum(r[2] for r in results),1)
        read.assert_called_once()
        self.assertEqual(len(self.db.games(1,sid)),1)

    async def test_changed_player_set_or_stale_source_blocks_roll(self):
        _,sid=self.installed()
        changed=copy.deepcopy(self.data);changed[0]['roster'][0]['name']='Undrafted player'
        with patch.object(workflow.sheet_source,'load_rows',return_value=sheet_rows(changed)):
            with self.assertRaises(ValueError):await workflow.play(self.db,1,sid,1,99,'unused',self.cache)
        self.assertEqual(self.db.games(1,sid),[])
        s=self.db.series(1,sid)
        with self.assertRaises(ValueError):self.db.refresh_lineups(1,sid,[s['high']['roster'],s['low']['roster']],0,{'tab':'other'},99)

    async def test_panel_buttons_private_settings_and_permission_guards(self):
        import bot
        _,sid=self.installed()
        role=NS()
        user=NS(id=bot.OWNER_ID,roles=[],guild_permissions=NS(manage_guild=False,administrator=False))
        interaction=NS(user=user,guild=NS(id=1,default_role=role),channel_id=bot.PRIVATE_CHANNEL_ID,
            channel=NS(permissions_for=lambda r:NS(view_channel=False)),
            response=NS(is_done=lambda:False,send_message=AsyncMock(),edit_message=AsyncMock(),defer=AsyncMock()),followup=NS(send=AsyncMock()))
        with patch.object(bot,'store',self.db),patch.object(bot,'GUILD_ID',1),patch.object(bot,'PUBLIC_CHANNEL_IDS',set()):
            view=controls.SeriesPanel(bot,1,sid)
            self.assertEqual(len(view.selector.options),8)
            self.assertFalse(view.play.disabled)
            self.assertTrue(await view.interaction_check(interaction))
            modal=controls.Settings(view)
            modal.odds._value='55';modal.high._value='10';modal.low._value='0'
            await modal.on_submit(interaction)
            self.assertEqual(self.db.series(1,sid)['base'],5500)
            self.assertEqual(self.db.series(1,sid)['penalty_high'],1000)
            interaction.user.id=123
            self.assertFalse(await view.interaction_check(interaction))
            self.assertFalse(controls.authorized(bot,interaction))
            interaction.user.id=bot.OWNER_ID;interaction.channel_id=1
            self.assertFalse(controls.authorized(bot,interaction,private=True))

    async def test_roster_view_reads_live_order_without_changing_saved_rosters(self):
        import bot
        self.installed()
        old=self.db.team(1,1)
        fresh=copy.deepcopy(old['roster']);fresh[0],fresh[5]=fresh[5],fresh[0]
        ctx=NS(guild=NS(id=1),send=AsyncMock())
        with patch.object(bot,'store',self.db),patch.object(bot.sheet_source,'load',return_value=fresh) as load:
            await bot.simroster.callback(ctx,seed=1)
        load.assert_called_once()
        description=ctx.send.call_args.kwargs['embed'].description
        self.assertTrue(description.startswith('**PG**'))
        self.assertIn(fresh[0]['name'],description.splitlines()[0])
        self.assertEqual(self.db.team(1,1),old)


if __name__=='__main__':
    unittest.main()
