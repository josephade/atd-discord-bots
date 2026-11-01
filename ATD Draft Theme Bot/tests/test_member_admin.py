import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, AsyncMock

import test_readiness
import test_suspects
import db
import market
import member_admin
import suspects
from config import OWNER_ID, OWNER_REPORT_CHANNEL_ID
from themes.base import _draft_locks


class MemberAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = str(Path(self.tmp.name)/'members.db')
        db.init_db()
        _draft_locks.clear()
        self.draft, self.members = test_suspects.SuspectTests.make_draft(self)
        self.bot = Mock(get_user=Mock(return_value=NS(send=AsyncMock())), get_channel=Mock())
        self.ctx = NS(author=NS(id=OWNER_ID), guild=NS(id=1, default_role=object(),
            get_member=Mock(return_value=NS(display_name='New GM'))), channel=NS(id=OWNER_REPORT_CHANNEL_ID,
            permissions_for=lambda role: NS(view_channel=False)), send=AsyncMock())
        self.state = db.get_draft(self.draft)['state']
        self.state['teams'][0].update(budget_spent=44, timer_deadline=123456, timer_pick_number=1,
            skip_count=2, open_picks=[dict(pick_number=1, skipped=False, proposal=None, pending_from=[])])
        db.set_draft_state(self.draft, self.state)
        db.record_pick(self.draft, self.members[0], 'Michael Jordan', pick_number=1)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def edit(self, new_user, replace=None):
        return db.edit_anonymous_member(self.draft, db.get_draft(self.draft)['state'], 0,
                                        new_user, 'New GM', OWNER_ID, replace)

    async def test_add_cogm_backfills_roster_and_preserves_team_clock_budget_and_order(self):
        result = self.edit(200)
        team = db.get_draft(self.draft)['state']['teams'][0]
        self.assertEqual(team['member_ids'], [self.members[0], result['member_id']])
        self.assertEqual(db.get_picks(self.draft, result['member_id']), ['Michael Jordan'])
        self.assertEqual((team['budget_spent'], team['timer_deadline'], team['skip_count']), (44, 123456, 2))
        self.assertEqual(db.get_draft(self.draft)['state']['live_pick_number'], 1)

    async def test_replace_keeps_drafter_slot_picks_but_revokes_old_access_and_private_list(self):
        db.update_drafter_state(self.members[0], {'draftlist_queue': {'picks': [{'player': 'Secret'}]}, 'queued_pick': {'player': 'Secret'}})
        result = self.edit(200, 100)
        self.assertEqual(result['member_id'], self.members[0])
        self.assertIsNone(db.get_drafter_by_user(self.draft, 100))
        new = db.get_drafter_by_user(self.draft, 200)
        self.assertEqual(db.get_picks(self.draft, new['id']), ['Michael Jordan'])
        self.assertEqual(new['state']['former_user_ids'], [100])
        self.assertNotIn('draftlist_queue', new['state'])
        self.assertNotIn('queued_pick', new['state'])

    async def test_replacement_does_not_reset_free_guesses_market_cap_or_target_cap(self):
        order = db.create_suspect_purchase(self.draft, 800, 100, [100], 10)
        db.finish_suspect_purchase(order['id'], 100, [100])
        first = await suspects.guess(None, self.draft, 901, 100, 101, ':Nets:')
        self.assertEqual(first['remaining'], 10)
        self.edit(200, 100)
        second = await suspects.guess(None, self.draft, 902, 200, 101, ':Nets:')
        self.assertEqual(second['remaining'], 9)
        with self.assertRaisesRegex(ValueError, 'can buy 0 more'):
            db.create_suspect_purchase(self.draft, 801, 200, [200], 1)
        order = db.create_suspect_purchase(self.draft, 802, 102, [102], 10)
        db.finish_suspect_purchase(order['id'], 102, [102])
        for n in range(5):
            self.assertEqual((await suspects.guess(None, self.draft, 930+n, 102, 200, ':Lakers:'))['status'], 'wrong')
        self.edit(201, 200)
        self.assertEqual((await suspects.guess(None, self.draft, 940, 102, 201, ':Nets:'))['status'], 'target_limit')

    async def test_existing_catch_remains_protected_after_replacement(self):
        self.assertEqual((await suspects.guess(None, self.draft, 901, 101, 100, ':Nets:'))['status'], 'correct')
        self.edit(200, 100)
        self.assertEqual((await suspects.guess(None, self.draft, 902, 102, 200, ':Nets:'))['status'], 'caught')

    async def test_adding_cogm_does_not_give_team_another_free_guess(self):
        await suspects.guess(None, self.draft, 901, 100, 101, ':Nets:')
        self.edit(200)
        self.assertEqual((await suspects.guess(None, self.draft, 902, 200, 101, ':Nets:'))['status'], 'limit')

    async def test_edit_clears_pending_approval_without_extending_original_clock(self):
        state = db.get_draft(self.draft)['state']
        state['teams'][0]['open_picks'][0].update(proposal={'player': 'Old proposal'}, pending_from=[self.members[0]], proposal_deadline=123400)
        db.set_draft_state(self.draft, state)
        result = self.edit(200, 100)
        self.assertTrue(result['cleared_proposal'])
        team = db.get_draft(self.draft)['state']['teams'][0]
        self.assertIsNone(team['open_picks'][0]['proposal'])
        self.assertEqual(team['timer_deadline'], 123456)

    async def test_duplicate_or_full_team_rejected_without_changes(self):
        with self.assertRaises(ValueError): self.edit(101)
        self.edit(200)
        self.edit(201)
        before = db.get_draft(self.draft)['state']
        with self.assertRaisesRegex(ValueError, 'maximum of 3'): self.edit(202)
        self.assertEqual(db.get_draft(self.draft)['state'], before)
        db.set_draft_status(self.draft, 'complete')
        with self.assertRaises(ValueError): self.edit(203, 100)

    async def test_commands_only_owner_in_private_hidden_channel_and_notify_new_gm(self):
        self.ctx.author.id = 100
        await member_admin.execute(self.ctx, self.bot, f'{self.draft} 1 <@200>')
        self.ctx.send.assert_not_awaited()
        self.ctx.author.id = OWNER_ID
        self.ctx.channel.id = 934052158532378634
        await member_admin.execute(self.ctx, self.bot, f'{self.draft} 1 <@200>')
        self.assertIsNone(db.get_drafter_by_user(self.draft, 200))
        self.ctx.channel.id = OWNER_REPORT_CHANNEL_ID
        await member_admin.execute(self.ctx, self.bot, f'{self.draft} 1 <@200>')
        self.assertIsNotNone(db.get_drafter_by_user(self.draft, 200))
        self.assertIn('Added co-GM', self.ctx.send.call_args.args[0])
        self.assertEqual(self.bot.get_user.return_value.send.await_count, 2)
        self.assertIn('Lotto number', self.bot.get_user.return_value.send.call_args_list[0].kwargs['content'])

    async def test_original_gm_can_return_to_same_slot_without_resetting_history(self):
        self.edit(200, 100)
        self.edit(100, 200)
        restored = db.get_drafter_by_user(self.draft, 100)
        self.assertEqual(restored['id'], self.members[0])
        self.assertEqual(db.anonymous_ledger_users(self.draft, [100]), [100, 200])
        self.assertEqual(db.get_picks(self.draft, restored['id']), ['Michael Jordan'])
