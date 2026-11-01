import ast
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import test_readiness
import db
import upcoming_picks
import discord


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = str(Path(self.tmp.name)/'upcoming.db'); db.init_db()
        self.draft = db.create_draft(1, 2, 'anon_budget_draft', 'Anonymous Budget', {'rounds': 4})
        self.a = db.add_drafter(self.draft, 100, 'Secret A')
        self.b = db.add_drafter(self.draft, 101, 'Secret B')
        self.others = [db.add_drafter(self.draft, 200+i, 'Secret other') for i in range(3)]
        db.set_draft_state(self.draft, dict(live_pick_number=1, teams=[dict(logo='TEAM', member_ids=[self.a, self.b],
            open_picks=[dict(pick_number=1, skipped=False)])]+[dict(logo=f'OTHER{i}',member_ids=[mid],open_picks=[]) for i,mid in enumerate(self.others)]))
        db.set_draft_status(self.draft, 'active')

    def tearDown(self): self.tmp.cleanup()

    def test_actual_snake_numbers_and_both_cogms_share_private_schedule(self):
        before = db.get_draft(self.draft)
        rows = upcoming_picks.schedule(100)[2]
        self.assertEqual([r['pick'] for r in rows], [1, 8, 9, 16])
        self.assertEqual(rows[0]['note'], 'On the clock')
        self.assertEqual(upcoming_picks.schedule(101)[2], rows)
        self.assertNotIn('Secret', str(upcoming_picks.embed(100).to_dict()))
        self.assertEqual(db.get_draft(self.draft), before)

    def test_completed_picks_omitted_and_skipped_makeups_stay_visible(self):
        db.record_pick(self.draft, self.a, 'Player', pick_number=1)
        db.update_draft_state(self.draft, {'live_pick_number': 10})
        state = db.get_draft(self.draft)['state']
        state['teams'][0]['open_picks'] = [dict(pick_number=8, skipped=True)]
        db.set_draft_state(self.draft, state)
        rows = upcoming_picks.schedule(100)[2]
        self.assertEqual([r['pick'] for r in rows], [8, 16])
        self.assertEqual(rows[0]['note'], 'Makeup owed')

    def test_round_count_changes_and_pause_are_respected(self):
        db.update_draft_settings(self.draft, {'rounds': 6})
        db.set_draft_status(self.draft, 'paused')
        rows = upcoming_picks.schedule(100)[2]
        self.assertEqual([r['pick'] for r in rows], [1, 8, 9, 16, 17, 24])
        self.assertEqual(rows[0]['note'], 'Paused')

    def test_flux_hold_does_not_claim_team_is_on_the_clock(self):
        db.update_draft_state(self.draft, {'flux_wait': {'round': 1}})
        rows = upcoming_picks.schedule(100)[2]
        self.assertEqual(rows[0]['note'], 'Waiting for Flux')
        self.assertNotIn('On the clock', upcoming_picks.embed(100).description)

    def test_finished_and_unregistered_and_cancelled_do_not_select_another_team(self):
        db.set_draft_status(self.draft, 'complete')
        self.assertEqual(upcoming_picks.schedule(100)[2], [])
        self.assertIn('no remaining picks', upcoming_picks.embed(100).description)
        with self.assertRaises(ValueError): upcoming_picks.schedule(999)
        db.set_draft_status(self.draft, 'cancelled')
        with self.assertRaisesRegex(ValueError, 'cancelled'): upcoming_picks.schedule(100)

    def test_partner_uses_saved_shuffled_order_and_owed_rounds(self):
        draft = db.create_draft(1, 2, 'partner_draft', 'Partner', {'roster_size': 4})
        a, b = [db.add_drafter(draft, uid, 'Secret') for uid in (100, 101)]
        turns = [dict(pair=p,round=r) for p,r in [(1,0),(0,0),(0,1),(1,1),(0,2),(1,2),(1,3),(0,3)]]
        db.set_draft_state(draft, dict(turn_index=2, turns=turns, pairs=[dict(a=a,b=b,logo='PAIR',completed_rounds=[],owed_rounds=[0])]))
        db.set_draft_status(draft, 'active')
        rows = upcoming_picks.schedule(100)[2]
        self.assertEqual([r['pick'] for r in rows], [2,3,5,8])
        self.assertEqual([r['note'] for r in rows[:2]], ['Makeup owed','On the clock'])
        self.assertEqual(upcoming_picks.schedule(101)[2], rows)


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_command_is_ignored_and_dm_queries_caller_without_target(self):
        from unittest.mock import patch
        tree = ast.parse((Path(__file__).resolve().parents[1]/'bot.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name=='cmd_nextpick')
        node.decorator_list=[]
        ns={'discord':discord, '_dm_only':lambda ctx: ctx.guild is None}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'bot.py','exec'),ns)
        ctx=NS(guild=object(),author=NS(id=100),reply=AsyncMock(),send=AsyncMock())
        with patch.object(upcoming_picks,'embed',return_value=discord.Embed(title='Private')) as render:
            await ns['cmd_nextpick'](ctx)
            render.assert_not_called()
            ctx.send.assert_not_awaited()
            ctx.reply.assert_not_awaited()
            ctx.guild=None
            await ns['cmd_nextpick'](ctx)
            render.assert_called_once_with(100)
            ctx.send.assert_awaited_once()
