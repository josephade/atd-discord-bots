import copy
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace as NS

from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import engine
import render
from store import Store
import sheet_source
import historical


def roster(prefix='Player'):
    return engine.validate_roster([dict(name=f'{prefix} {i}',year='1995-96',position=engine.POSITIONS[i]) for i in range(10)])


def series():
    return dict(id=1,stage=1,base=4700,hca=1000,penalty_high=0,penalty_low=0,
                high=dict(seed=1,name='Baltimore Bullets',logo='B',roster=roster('Baltimore')),
                low=dict(seed=16,name='Phoenix Suns',logo='P',roster=roster('Phoenix')))


class EngineTests(unittest.TestCase):
    def test_historical_zero_three_share_players_never_attempt_threes(self):
        s=series()
        for t in (s['high'],s['low']):
            for p in t['roster']:
                p['profile']['three_share']=0
        g=engine.simulate(s,1,'old-school')
        self.assertEqual(sum(p['tpa'] for b in g['boxes'] for p in b),0)
        for side in (0,1):
            self.assertEqual(sum(p['pts'] for p in g['boxes'][side]),g['scores'][side])

    def test_hca_schedule_penalties_and_clamping(self):
        self.assertEqual([engine.probability(4700,n) for n in range(1,8)],[5700,5700,3700,3700,5700,3700,5700])
        self.assertEqual(engine.probability(4700,1,1000),4700)
        self.assertEqual(engine.probability(4700,1,0,1000),6700)
        self.assertEqual(engine.probability(9900,1),10000)
        self.assertEqual(engine.probability(0,3),0)

    def test_percent_validation(self):
        self.assertEqual(engine.percentage('47.123'),4712)
        for value in ('nan','Infinity','-1','101','abc'):
            with self.assertRaises(ValueError):engine.percentage(value)

    def test_ballot_formula_is_expected_games_not_series_win_percentage(self):
        self.assertEqual(engine.vote_odds([0,7]),5000)
        self.assertEqual(engine.vote_odds([1]),8000)
        self.assertEqual(engine.vote_odds([3]),5714)
        with self.assertRaises(ValueError):engine.vote_odds([])

    def test_reproducibility_and_profiles_never_change_winner(self):
        s=series()
        first=engine.simulate(s,1,'fixed-seed')
        self.assertEqual(first,engine.simulate(s,1,'fixed-seed'))
        for p in s['high']['roster']:
            p['profile']['pts']=80
        second=engine.simulate(s,1,'fixed-seed')
        self.assertEqual(first['winner'],second['winner'])
        self.assertEqual(first['roll'],second['roll'])
        self.assertEqual(first['scores'],second['scores'])

    def test_many_games_have_consistent_scores_shooting_minutes_and_defense(self):
        s=series();overtime=False
        for seed in range(250):
            g=engine.simulate(s,seed%7+1,str(seed))
            overtime |= g['ot']
            self.assertEqual(g['winner'],0 if g['roll']<=g['odds_high'] else 1)
            self.assertGreater(g['scores'][g['winner']],g['scores'][1-g['winner']])
            for side,box in enumerate(g['boxes']):
                self.assertEqual(sum(g['quarters'][side]),g['scores'][side])
                self.assertEqual(len(box),10)
                self.assertEqual(sum(p['pts'] for p in box),g['scores'][side])
                self.assertEqual(sum(p['min'] for p in box),265 if g['ot'] else 240)
                for p in box:
                    self.assertLessEqual(p['min'],53 if g['ot'] else 48)
                    self.assertEqual(p['pts'],2*p['fgm']+p['tpm']+p['ftm'])
                    self.assertEqual(p['reb'],p['oreb']+p['dreb'])
                    self.assertLessEqual(p['tpm'],p['fgm'])
                    self.assertLessEqual(p['fgm'],p['fga'])
                    self.assertLessEqual(p['tpm'],p['tpa'])
                    self.assertLessEqual(p['ftm'],p['fta'])
                    self.assertLessEqual(p['pf'],5)
                self.assertLessEqual(sum(p['stl'] for p in box),sum(p['tov'] for p in g['boxes'][1-side]))
            if g['ot']:
                self.assertEqual(sum(g['quarters'][0][:4]),sum(g['quarters'][1][:4]))
        self.assertTrue(overtime)

    def test_roster_rejects_duplicates_and_nonfinite_baselines(self):
        rows=roster()
        rows[1]['name']=rows[0]['name']
        with self.assertRaises(ValueError):engine.validate_roster(rows)
        rows=roster();rows[0]['profile']['pts']=float('nan')
        with self.assertRaises(ValueError):engine.validate_roster(rows)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'playoffs.sqlite3'
        self.db=Store(self.path)
        self.season=self.db.create(1,'ATD Test',99)
        for seed in range(1,17):
            self.db.save_team(1,seed,f':T{seed}:',f'Team {seed}',99,roster(f'Team {seed} player'))
        self.matches=self.db.bracket(1,99)
        self.sid=self.matches[0]['id']

    def tearDown(self):
        self.tmp.cleanup()

    def odds(self,sid=None):
        return self.db.configure(1,sid or self.sid,99,base=4700,votes_open=False,odds_source='official')

    def test_saved_game_replayed_after_restart_and_no_reroll(self):
        self.odds()
        s,g,new=self.db.game(1,self.sid,1,99)
        self.assertTrue(new)
        again=Store(self.path).game(1,self.sid,1,99)
        self.assertFalse(again[2]);self.assertEqual(again[1],g)
        self.assertEqual(sum(again[0]['wins']),1)
        with self.assertRaises(ValueError):self.db.configure(1,self.sid,99,base=8000)
        with self.assertRaises(ValueError):self.db.reset_bracket(1,99)

    def test_concurrent_simulations_commit_exactly_once(self):
        self.odds()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.db.game(1,self.sid,1,99),range(4)))
        self.assertEqual(sum(r[2] for r in results),1)
        self.assertEqual(len({r[1]['seed'] for r in results}),1)
        self.assertEqual(len(self.db.games(1,self.sid)),1)

    def test_wrong_guild_missing_odds_and_out_of_order_rejected(self):
        with self.assertRaises(ValueError):self.db.series(2,self.sid)
        with self.assertRaises(ValueError):self.db.game(1,self.sid,1,99)
        self.odds()
        with self.assertRaises(ValueError):self.db.game(1,self.sid,2,99)
        with self.assertRaises(ValueError):self.db.game(1,self.sid,1)

    def test_latest_ballot_counts_once_and_closed_ballots_lock(self):
        self.db.vote(1,self.sid,123,0)
        self.db.vote(1,self.sid,123,7)
        s=self.db.close_votes(1,self.sid,99)
        self.assertEqual(s['base'],0)
        with self.assertRaises(ValueError):self.db.vote(1,self.sid,123,0)
        with self.assertRaises(ValueError):self.db.vote(1,self.sid,123,8)

    def test_full_16_team_bracket_champion_and_archival_without_deleting_games(self):
        for stage in range(1,5):
            current=[s for s in self.db.all_series(1) if s['stage']==stage]
            for s in current:
                self.odds(s['id'])
                for number in range(1,8):
                    result,g,_=self.db.game(1,s['id'],number,99)
                    if result['complete']:
                        break
                self.assertEqual(max(result['wins']),4)
                with self.assertRaises(ValueError):self.db.game(1,s['id'],sum(result['wins'])+1,99)
            if stage<4:self.db.advance(1,99)
        rows=self.db.all_series(1)
        self.assertEqual(len(rows),15)
        final=next(s for s in rows if s['stage']==4)
        for s in rows:
            self.assertLess(s['high']['seed'],s['low']['seed'])
        saved=self.db.games(1,final['id'])
        self.db.archive(1,99)
        self.assertEqual(self.db.games(1,final['id']),saved)
        self.db.create(1,'Next ATD',99)
        self.assertEqual(self.db.teams(self.db.season(1)['id']),[])

    def test_early_advance_and_roster_mutation_are_blocked(self):
        with self.assertRaises(ValueError):self.db.advance(1,99)
        with self.assertRaises(ValueError):self.db.archive(1,99)
        with self.assertRaises(ValueError):self.db.save_team(1,1,':T1:','Changed',99,roster())

    def test_unplayed_reset_clears_votes_and_focus_but_not_rosters(self):
        self.db.vote(1,self.sid,123,0)
        self.db.focus(1,20,self.sid)
        self.db.reset_bracket(1,99)
        self.assertEqual(self.db.all_series(1),[])
        with self.assertRaises(ValueError):self.db.focus(1,20)
        self.assertEqual(len(self.db.teams(self.season['id'])),16)
        self.db.bracket(1,99)

    def test_exports_do_not_leak_other_servers(self):
        other=self.db.create(2,'Other Server',100)
        out=self.db.export(1)
        self.assertEqual([s['guild'] for s in out['seasons']],[1])
        self.assertNotIn(other['name'],json.dumps(out))

    def test_test_season_can_end_midseries_without_deleting_or_rerolling_games(self):
        self.db.create(2,'TEST - ATD 113',99,is_test=True)
        for t in self.db.teams(self.season['id']):
            self.db.save_team(2,t['seed'],t['logo'],t['name'],99,t['roster'])
        s=self.db.bracket(2,99)[0]
        self.assertTrue(s['is_test'])
        self.db.configure(2,s['id'],99,base=5000)
        _,game,_=self.db.game(2,s['id'],1,99)
        self.db.archive(2,99)
        self.assertEqual(self.db.game(2,s['id'],1)[1],game)
        with self.assertRaises(ValueError):self.db.game(2,s['id'],2,99)
        self.db.create(2,'Real tournament',99)
        self.assertFalse(self.db.season(2)['is_test'])


class RenderingTests(unittest.TestCase):
    def test_sheet_credentials_decode_without_exposing_or_writing_them(self):
        import base64
        fake={'type':'service_account','private_key':'example\nnot-a-real-key'}
        encoded=base64.b64encode(json.dumps(fake).encode()).decode()
        ws=Mock(get_all_values=Mock(return_value=[['Team A','Year']]+[[f'Player {i}','1995-96'] for i in range(10)]))
        client=Mock()
        client.open_by_key.return_value.worksheet.return_value=ws
        with patch.dict(os.environ,{'GOOGLE_SERVICE_ACCOUNT_B64':encoded}),\
             patch('gspread.service_account_from_dict',return_value=client) as auth:
            result=sheet_source.load(dict(sheet='example',tab='Test'),'Team A','unused.json')
        self.assertEqual(len(result),10)
        self.assertEqual(auth.call_args.args[0],fake)
        self.assertEqual(auth.call_args.kwargs['scopes'],['https://www.googleapis.com/auth/spreadsheets.readonly'])

    def test_all_pages_are_full_resolution_pngs(self):
        s=series();g=engine.simulate(s,1,'example')
        g.update(series_wins=[1,0],series_complete=False)
        for raw,height in ((render.game_card(s,g),1050),(render.box_card(s,g,0),1170),
                           (render.box_card(s,g,1),1170),(render.stats_card(s,[g]),1450)):
            image=Image.open(BytesIO(raw))
            self.assertEqual(image.format,'PNG')
            self.assertEqual(image.size,(1500,height))
            self.assertFalse(getattr(image,'is_animated',False))

    def test_series_averages_use_saved_games_only(self):
        s=series();games=[engine.simulate(s,i+1,str(i)) for i in range(3)]
        data=render.averages(s,games)
        self.assertEqual(data[0][0]['pts'],sum(g['boxes'][0][0]['pts'] for g in games)/3)
        with self.assertRaises(ValueError):render.stats_card(s,[])

    def test_sheet_reader_exact_header_ten_slots_and_no_price_as_year(self):
        rows=[['Team A','Year','$100','Other'],*[['Name '+str(i),'1995-96','$5'] for i in range(10)]]
        players=sheet_source.parse_roster(rows,'Team A')
        self.assertEqual(len(players),10)
        self.assertEqual(players[0]['year'],'1995-96')
        with self.assertRaises(ValueError):sheet_source.parse_roster(rows,'Team')
        with self.assertRaises(ValueError):sheet_source.parse_roster(rows[:5],'Team A')
        with self.assertRaises(ValueError):sheet_source.source('not-a-sheet | Tab')


class HistoricalTests(unittest.TestCase):
    def test_exact_season_ending_year_and_multiteam_totals(self):
        self.assertEqual(historical.season("'09"),'2008-09')
        self.assertEqual(historical.season('1996'),'1995-96')
        with self.assertRaises(ValueError):historical.season('1995-01')
        rows=[dict(SEASON_ID='1995-96',LEAGUE_ID='00',TEAM_ID=1,PTS=1,REB=2,AST=3,FGM=1,FG3M=0),
              dict(SEASON_ID='1995-96',LEAGUE_ID='00',TEAM_ID=0,PTS=30,REB=10,AST=5,FGM=10,FG3M=2)]
        p=historical.profile(rows,'1996')
        self.assertEqual((p['pts'],p['reb'],p['ast'],p['three_share']),(30,10,5,.2))
        with self.assertRaises(ValueError):historical.profile(rows,'1997')
        with self.assertRaises(ValueError):historical.profile([dict(rows[0],LEAGUE_ID='01')],'1996')

    def test_import_uses_cache_without_any_network_request(self):
        import sqlite3
        try:
            from nba_api.stats.endpoints.playercareerstats import PlayerCareerStats
        except ImportError:
            self.skipTest('nba_api is tested in the dedicated bot environment')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'cache.sqlite3'
            rows=[dict(SEASON_ID='1995-96',LEAGUE_ID='00',TEAM_ID=0,PTS=30.4,REB=6.6,AST=4.3,FGM=11.2,FG3M=1.4)]
            with sqlite3.connect(path) as con:
                con.execute('CREATE TABLE careers(player INTEGER PRIMARY KEY,data TEXT NOT NULL)')
                con.execute('INSERT INTO careers VALUES(?,?)',(893,json.dumps(rows)))
            con.close()
            with patch('nba_api.stats.static.players.get_players',return_value=[dict(id=893,full_name='Michael Jordan')]),\
                 patch('nba_api.stats.endpoints.playercareerstats.PlayerCareerStats') as request:
                players,report=historical.sync([dict(name='Michael Jordan',year='1995-96',position='SG')],path)
                request.assert_not_called()
            self.assertEqual(players[0]['profile']['pts'],30.4)
            self.assertIn('loaded',report[0])


class CommandTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        with patch.dict(os.environ,{'STATE_DIR':cls.tmp.name}):
            import bot
            cls.module=bot

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def ctx(self,owner=True,channel=None):
        m=self.module
        author=NS(id=m.OWNER_ID if owner else 123,roles=[],guild_permissions=NS(manage_guild=False,administrator=False))
        return NS(author=author,guild=NS(id=1,default_role=None),channel=NS(id=channel or m.PRIVATE_CHANNEL_ID,
            permissions_for=lambda role:NS(view_channel=False)),send=AsyncMock())

    async def test_admin_commands_cannot_be_run_by_gms_or_publicly(self):
        m=self.module
        for name in ('simcreate','simcancel','simsetup','simtest','simteam','simsetroster','simbracket','simodds','simpenalty','simclosevotes','simadvance','simaudit','simexport','simarchive','simsheet','simimport','simprofile','simloadstats','simresetbracket'):
            cmd=m.bot.get_command(name)
            self.assertIsNotNone(cmd)
            for ctx in (self.ctx(False),self.ctx(True,channel=1)):
                with self.assertRaises(m.commands.CheckFailure):await cmd.checks[0](ctx)
            self.assertTrue(await cmd.checks[0](self.ctx()))

    async def test_commissioner_only_focus_works_in_public_channel(self):
        m=self.module
        for name in ('simfocus',):
            cmd=m.bot.get_command(name)
            self.assertTrue(await cmd.checks[0](self.ctx(True,channel=1)))
            with self.assertRaises(m.commands.CheckFailure):await cmd.checks[0](self.ctx(False,channel=1))

    async def test_help_lists_every_registered_command(self):
        m=self.module;ctx=self.ctx()
        await m.simhelp.callback(ctx,mode='advanced')
        payload=str(ctx.send.call_args.kwargs['embed'].to_dict())
        for command in m.bot.commands:
            self.assertIn('!'+command.name,payload)

    async def test_sim_command_requires_game_number_and_never_calls_store_on_bad_input(self):
        m=self.module
        with self.assertRaises(ValueError):await m.sim.callback(self.ctx(),text='Game 8')

    async def test_familiar_create_command_dispatches_to_single_automatic_setup(self):
        m=self.module
        setup=AsyncMock()
        with patch.object(m,'simsetup',NS(callback=setup)):
            await m.simcreate.callback(self.ctx(),name='ATD 113')
            self.assertEqual(setup.await_count,1)
            self.assertEqual(setup.call_args.kwargs['text'],'ATD 113')
            await m.simcreate.callback(self.ctx(),name='ATD 114 | https://docs.google.com/spreadsheets/d/example')
            self.assertEqual(setup.await_count,2)


if __name__=='__main__':
    unittest.main()
