import asyncio
import copy
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,patch

import controls
import workflow
from store import Store
from test_workflow import SOURCE,teams


class ChannelAndCancelTests(unittest.IsolatedAsyncioTestCase):
    async def test_backup_channel_allows_private_admin_commands_and_buttons_only_for_commissioners(self):
        b=self.bot
        channel=NS(id=939947875805302784,parent_id=None,permissions_for=lambda role:NS(view_channel=False))
        user=NS(id=b.OWNER_ID)
        ctx=NS(channel=channel,guild=NS(id=1,default_role=None),author=user)
        with patch.object(b,'GUILD_ID',1):
            self.assertTrue(await b.channel_check(ctx))
            for name in ('simcreate','simcancel','simsetup','simexport'):
                self.assertTrue(await b.bot.get_command(name).checks[0](ctx))
            interaction=NS(channel=channel,channel_id=channel.id,guild=ctx.guild,user=user)
            self.assertTrue(controls.authorized(b,interaction,private=True))
            user.id=123;user.roles=[];user.guild_permissions=NS(administrator=False,manage_guild=False)
            self.assertFalse(controls.authorized(b,interaction,private=True))
            with self.assertRaises(b.commands.CheckFailure):await b.bot.get_command('simcancel').checks[0](ctx)

    async def asyncSetUp(self):
        import bot
        self.bot=bot
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Store(Path(self.tmp.name)/'state.sqlite3')
        self.db.install_setup(1,113,teams(),copy.deepcopy(SOURCE),99)
        self.sid=self.db.all_series(1)[0]['id']

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_parent_thread_private_allowed_other_channel_and_thread_rejected(self):
        b=self.bot
        with patch.object(b,'PUBLIC_CHANNEL_IDS',{934054991965077564}),patch.object(b,'GUILD_ID',1):
            for channel in (NS(id=934054991965077564,parent_id=None),NS(id=555,parent_id=934054991965077564),NS(id=b.PRIVATE_CHANNEL_ID,parent_id=None)):
                self.assertTrue(await b.channel_check(NS(guild=NS(id=1),channel=channel)))
            for channel in (NS(id=777,parent_id=None),NS(id=888,parent_id=777)):
                with self.assertRaises(b.commands.CheckFailure):await b.channel_check(NS(guild=NS(id=1),channel=channel))

    async def test_buttons_apply_same_thread_permissions_as_commands(self):
        b=self.bot
        interaction=NS(user=NS(id=b.OWNER_ID),guild=NS(id=1),channel=NS(id=555,parent_id=934054991965077564),channel_id=555)
        with patch.object(b,'PUBLIC_CHANNEL_IDS',{934054991965077564}),patch.object(b,'GUILD_ID',1):
            self.assertTrue(controls.authorized(b,interaction))
            self.assertFalse(controls.authorized(b,interaction,private=True))
            interaction.channel.parent_id=777
            self.assertFalse(controls.authorized(b,interaction))

    async def test_joins_only_threads_under_sim_parent(self):
        b=self.bot
        thread=NS(id=555,parent_id=934054991965077564,guild=NS(id=1),join=AsyncMock())
        with patch.object(b,'PUBLIC_CHANNEL_IDS',{934054991965077564}),patch.object(b,'GUILD_ID',1):
            await b.join_sim_thread(thread)
            thread.join.assert_awaited_once()
            thread.parent_id=777
            await b.join_sim_thread(thread)
            thread.join.assert_awaited_once()

    async def test_cancel_wrong_atd_does_not_change_tournament(self):
        before=self.db.export(1)
        with self.assertRaises(ValueError):self.db.cancel(1,114,99)
        self.assertEqual(self.db.export(1),before)

    async def test_cancel_preserves_records_blocks_new_games_and_allows_new_atd(self):
        self.db.configure(1,self.sid,99,base=5000)
        _,game,_=self.db.game(1,self.sid,1,99)
        self.db.focus(1,555,self.sid)
        before=self.db.export(1)
        season=self.db.cancel(1,113,99)
        self.assertTrue(season['cancelled'])
        self.assertEqual(self.db.game(1,self.sid,1)[1],game)
        self.assertEqual(self.db.export(1)['games'],before['games'])
        with self.assertRaises(ValueError):self.db.game(1,self.sid,2,99)
        with self.assertRaises(ValueError):self.db.configure(1,self.sid,99,base=6000)
        with self.assertRaises(ValueError):self.db.focus(1,555)
        self.db.create(1,'ATD 114',99)
        self.assertEqual(self.db.season(1)['name'],'ATD 114')

    async def test_old_panel_rejects_actions_after_cancel_even_when_new_season_exists(self):
        b=self.bot
        interaction=NS(user=NS(id=b.OWNER_ID),guild=NS(id=1),channel_id=b.PRIVATE_CHANNEL_ID,
                       channel=NS(id=b.PRIVATE_CHANNEL_ID,parent_id=None),response=NS(send_message=AsyncMock()))
        with patch.object(b,'store',self.db),patch.object(b,'GUILD_ID',1):
            panel=controls.SeriesPanel(b,1,self.sid)
            self.db.cancel(1,113,99)
            self.db.install_setup(1,114,teams(),copy.deepcopy(SOURCE),99)
            self.assertFalse(await panel.interaction_check(interaction))
            self.assertIn('closed',interaction.response.send_message.call_args.args[0])

    async def test_cancel_pending_import_does_not_resurrect_it(self):
        db=Store(Path(self.tmp.name)/'other.sqlite3')
        started=asyncio.Event();release=asyncio.Event()
        async def progress(message):
            if not started.is_set():
                started.set();await release.wait()
        with patch.object(workflow,'read_setup',return_value=(teams(),copy.deepcopy(SOURCE))),\
             patch.object(workflow.historical,'sync',side_effect=lambda players,path:(players,[])):
            task=asyncio.create_task(workflow.setup(db,1,99,'ATD 113',[],'unused',Path(self.tmp.name)/'cache',progress))
            await started.wait()
            self.assertTrue(workflow.cancel_pending(1,113))
            release.set()
            with self.assertRaises(ValueError):await task
        with self.assertRaises(ValueError):db.season(1)

    async def test_thread_has_its_own_series_selection(self):
        ids=[s['id'] for s in self.db.all_series(1)]
        self.db.focus(1,934054991965077564,ids[0])
        self.db.focus(1,555,ids[1])
        self.assertEqual(self.db.focus(1,555),ids[1])
        self.assertEqual(self.db.focus(1,934054991965077564),ids[0])

    async def test_gms_can_select_only_own_series_and_thread_binding_is_enforced(self):
        b=self.bot
        uid=123456789012345678;partner=223456789012345678
        self.db.set_series_gms(1,self.sid,[uid,partner],[323456789012345678],99)
        self.assertTrue(self.db.can_play(1,self.sid,uid))
        self.assertTrue(self.db.can_play(1,self.sid,partner))
        self.assertFalse(self.db.can_play(1,self.sid,423456789012345678))
        user=NS(id=uid,roles=[],guild_permissions=NS(manage_guild=False,administrator=False))
        ctx=NS(guild=NS(id=1),author=user,channel=NS(id=934054991965077564,parent_id=None))
        with patch.object(b,'store',self.db),patch.object(b,'PUBLIC_CHANNEL_IDS',{934054991965077564}):
            self.assertEqual(b.choose_play(ctx),self.sid)
            other=self.db.all_series(1)[1]['id']
            with self.assertRaises(b.commands.CheckFailure):b.choose_play(ctx,other)
            ctx.channel=NS(id=555,parent_id=934054991965077564)
            with self.assertRaises(b.commands.CheckFailure):b.choose_play(ctx)
            self.db.focus(1,555,self.sid)
            self.assertEqual(b.choose_play(ctx),self.sid)
            self.db.focus(1,555,other)
            with self.assertRaises(b.commands.CheckFailure):b.choose_play(ctx,self.sid)

    async def test_gm_assignment_is_private_and_clear_revokes_permissions(self):
        import memberships
        uid=123456789012345678
        self.assertEqual(memberships.parse(f'<@{uid}>, {uid}'),[uid])
        with self.assertRaises(ValueError):memberships.parse('@unverified_username')
        self.db.set_series_gms(1,self.sid,[uid],[],99)
        self.assertTrue(self.db.can_play(1,self.sid,uid))
        self.db.set_series_gms(1,self.sid,[],[],99)
        self.assertFalse(self.db.can_play(1,self.sid,uid))
        with self.assertRaises(ValueError):self.db.set_series_gms(1,self.sid,[uid],[uid],99)

    async def test_verified_registry_fallback_and_explicit_revoke(self):
        import json,base64
        uid=123456789012345678
        secret=base64.b64encode(json.dumps({'113':{'Team 1':[uid]}}).encode()).decode()
        with patch.dict('os.environ',{'PLAYOFF_GM_REGISTRY_B64':secret}):
            self.assertTrue(self.db.can_play(1,self.sid,uid))
            self.assertFalse(self.db.can_play(1,self.sid,uid+1))
            self.db.set_series_gms(1,self.sid,[],[],99)
            self.assertFalse(self.db.can_play(1,self.sid,uid))


if __name__=='__main__':
    unittest.main()
