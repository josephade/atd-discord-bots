import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import test_readiness
import db
import market
import suspects
from themes.base import _draft_locks
import test_suspects


class MarketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = str(Path(self.tmp.name)/'market.db')
        db.init_db()
        _draft_locks.clear()
        self.draft, self.members = test_suspects.SuspectTests.make_draft(self, mode='partner_draft')
        self.ctx = NS(guild=NS(id=1), channel=NS(id=1566394824288894976), author=NS(id=100),
                      message=NS(id=800), reply=AsyncMock())

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def guess(self, nonce, user=100, target=103, emoji=':Nets:'):
        return await suspects.guess(None, self.draft, nonce, user, target, emoji)

    async def test_quote_costs_ten_for_two_but_no_confirmation_means_no_charge_or_credit(self):
        purchase = await market.quote(self.ctx, 'suspect 2')
        self.assertEqual((purchase['quantity'], purchase['cost'], purchase['status']), (2, 10, 'pending'))
        self.assertEqual((await self.guess(901))['remaining'], 0)
        self.assertEqual((await self.guess(902))['status'], 'limit')
        self.assertFalse(any(p['status'] == 'confirmed' for p in db.suspect_purchases(self.draft)))
        await market.settle(purchase, 100, True)
        self.assertEqual((await self.guess(903, user=101))['remaining'], 1)

    async def test_confirmed_credits_are_shared_and_repeated_confirmation_charges_once(self):
        purchase = await market.quote(self.ctx, 'suspect 2')
        await asyncio.gather(market.settle(purchase, 100, True), market.settle(purchase, 100, True))
        self.assertEqual(len(db.suspect_purchases(self.draft)), 1)
        for nonce, user, remaining in ((901, 100, 2), (902, 101, 1), (903, 100, 0)):
            result = await self.guess(nonce, user=user)
            self.assertEqual((result['status'], result['remaining']), ('wrong', remaining))
        self.assertEqual((await self.guess(904, user=101))['status'], 'limit')
        self.assertEqual(sum(p['cost'] for p in db.suspect_purchases(self.draft) if p['status'] == 'confirmed'), 10)

    async def test_cancel_and_expiry_never_add_credits_or_charge_wins(self):
        purchase = await market.quote(self.ctx, 'suspect 2')
        self.assertEqual((await market.settle(purchase, 100, False))['status'], 'cancelled')
        self.assertEqual((await market.settle(purchase, 100, True))['status'], 'cancelled')
        self.ctx.message.id += 1
        purchase = await market.quote(self.ctx, 'suspect 1')
        with db._conn() as con:
            con.execute('UPDATE suspect_purchases SET expires_at=0 WHERE id=?', (purchase['id'],))
        self.assertEqual((await market.settle(purchase, 100, True))['status'], 'expired')
        self.assertEqual((await self.guess(901))['remaining'], 0)

    async def test_only_buyer_can_confirm_and_team_change_or_reveal_blocks_charge(self):
        purchase = await market.quote(self.ctx, 'suspect 1')
        with self.assertRaises(ValueError): await market.settle(purchase, 101, True)
        db.update_draft_state(self.draft, {'identities_revealed': True})
        with self.assertRaises(ValueError): await market.settle(purchase, 100, True)
        self.assertEqual(db.suspect_purchases(self.draft)[0]['status'], 'pending')
        with self.assertRaises(ValueError): await market.quote(self.ctx, 'suspect 1')

    async def test_invalid_quantity_or_product_never_creates_purchase(self):
        for text in ('', 'suspect', 'suspect 0', 'suspect -1', 'suspect 1.5', 'wins 2', 'suspect 2 extra'):
            with self.subTest(text=text):
                with self.assertRaises(ValueError): await market.quote(self.ctx, text)
        self.assertEqual(db.suspect_purchases(self.draft), [])

    async def test_purchases_do_not_allow_repeat_catches_or_increase_catch_penalty(self):
        purchase = await market.quote(self.ctx, 'suspect 2')
        await market.settle(purchase, 100, True)
        first = await self.guess(901, target=102, emoji=':Lakers:')
        self.assertEqual((first['status'], first['penalty_amount']), ('correct', 5))
        second = await self.guess(902, target=103, emoji=':Lakers:')
        self.assertEqual((second['status'], second['remaining']), ('caught', 2))
        self.assertEqual(len(db.suspect_attempts(self.draft)), 1)

    async def test_prompt_displays_full_cost_and_restricts_buttons_to_buyer(self):
        await market.buy(self.ctx, 'suspect 2')
        call = self.ctx.reply.call_args.kwargs
        self.assertIn('**10 wins**', call['embed'].description)
        self.assertNotIn('Nets', call['embed'].description)
        view = call['view']
        interaction = NS(user=NS(id=101), response=NS(send_message=AsyncMock()))
        self.assertFalse(await view.interaction_check(interaction))
        interaction.response.send_message.assert_awaited_once()
        interaction.user.id = 100
        interaction.channel_id = 934052158532378634
        self.assertFalse(await view.interaction_check(interaction))
        interaction.channel_id = 1533242104304631969
        self.assertTrue(await view.interaction_check(interaction))
        await view.on_timeout()
        self.assertEqual(db.suspect_purchases(self.draft)[0]['status'], 'cancelled')
        view.stop()

    async def test_confirmation_button_commits_cost_and_removes_buttons(self):
        purchase = await market.quote(self.ctx, 'suspect 2')
        view = market.PurchaseConfirm(purchase)
        interaction = NS(user=NS(id=100), response=NS(defer=AsyncMock()),
                         edit_original_response=AsyncMock(), followup=NS(send=AsyncMock()))
        await view.finish(interaction, True)
        row = db.suspect_purchases(self.draft)[0]
        self.assertEqual((row['cost'], row['status']), (10, 'confirmed'))
        call = interaction.edit_original_response.call_args.kwargs
        self.assertIsNone(call['view'])
        self.assertIn('**10 wins**', call['embed'].description)
        interaction.followup.send.assert_not_awaited()

    async def test_five_guesses_are_shared_across_target_partners_and_sixth_is_free(self):
        purchase = await market.quote(self.ctx, 'suspect 10')
        await market.settle(purchase, 100, True)
        for n in range(5):
            result = await self.guess(901+n, target=102+n%2)
            self.assertEqual(result['status'], 'wrong')
        self.assertTrue(result['target_limit_reached'])
        before = result['remaining']
        db.init_db()  # Existing history remains counted after restart/schema initialization.
        for target in (102, 103):
            blocked = await self.guess(920+target, target=target, emoji=':Lakers:')
            self.assertEqual((blocked['status'], blocked['remaining']), ('target_limit', before))
        self.assertEqual(len(db.suspect_attempts(self.draft)), 5)
        other = await self.guess(999, target=104)
        self.assertEqual((other['status'], other['remaining']), ('wrong', before-1))

    async def test_simultaneous_fifth_and_sixth_target_guesses_only_charge_one(self):
        purchase = await market.quote(self.ctx, 'suspect 10')
        await market.settle(purchase, 100, True)
        for n in range(4): await self.guess(901+n)
        results = await asyncio.gather(self.guess(910, target=102), self.guess(911, target=103))
        self.assertEqual(sorted(r['status'] for r in results), ['target_limit', 'wrong'])
        self.assertEqual(len(db.suspect_attempts(self.draft)), 5)

    async def test_invalid_guesses_do_not_count_towards_team_cap(self):
        purchase = await market.quote(self.ctx, 'suspect 10')
        await market.settle(purchase, 100, True)
        for n in range(6):
            self.assertEqual((await self.guess(901+n, emoji=':not-a-team:'))['status'], 'invalid')
        for n in range(5):
            result = await self.guess(930+n)
            self.assertEqual(result['status'], 'wrong')
        self.assertEqual(len(db.suspect_attempts(self.draft)), 5)

    async def test_team_can_buy_ten_total_and_spending_them_does_not_reset_purchase_cap(self):
        purchase = await market.quote(self.ctx, 'suspect 10')
        await market.settle(purchase, 100, True)
        await self.guess(901)
        self.ctx.message.id += 1
        for user in (100, 101):
            self.ctx.author.id = user
            with self.assertRaisesRegex(ValueError, 'can buy 0 more'):
                await market.quote(self.ctx, 'suspect 1')
        self.assertEqual(len(db.suspect_purchases(self.draft)), 1)

    async def test_duo_purchases_combine_and_pending_confirmations_cannot_exceed_ten(self):
        first = await market.quote(self.ctx, 'suspect 6')
        self.ctx.message.id += 1
        self.ctx.author.id = 101
        second = await market.quote(self.ctx, 'suspect 6')
        await market.settle(first, 100, True)
        with self.assertRaisesRegex(ValueError, 'can buy 4 more'):
            await market.settle(second, 101, True)
        self.ctx.message.id += 1
        final = await market.quote(self.ctx, 'suspect 4')
        await market.settle(final, 101, True)
        confirmed = [p for p in db.suspect_purchases(self.draft) if p['status'] == 'confirmed']
        self.assertEqual(sum(p['quantity'] for p in confirmed), 10)
        self.assertEqual(sum(p['cost'] for p in confirmed), 50)

    async def test_eleven_at_once_rejected_and_cancelled_purchases_do_not_use_cap(self):
        with self.assertRaisesRegex(ValueError, 'between 1 and 10'):
            await market.quote(self.ctx, 'suspect 11')
        first = await market.quote(self.ctx, 'suspect 10')
        await market.settle(first, 100, False)
        self.ctx.message.id += 1
        second = await market.quote(self.ctx, 'suspect 10')
        await market.settle(second, 100, True)
        self.assertEqual(db.suspect_purchases(self.draft)[1]['status'], 'confirmed')

    async def test_market_and_buys_only_run_in_suspect_and_private_admin_channels(self):
        import ast, discord
        from unittest.mock import patch
        tree = ast.parse((Path(__file__).resolve().parents[1]/'bot.py').read_text(encoding='utf-8'))
        nodes = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name in ('cmd_market', 'cmd_buys')]
        for node in nodes: node.decorator_list = []
        ns = {'discord': discord}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'bot.py', 'exec'), ns)
        with patch.object(market, 'buy', new_callable=AsyncMock) as buy:
            for channel in (934052158532378634, 934052208624930848, 123):
                self.ctx.channel.id = channel
                for name in ('cmd_market', 'cmd_buys'):
                    await ns[name](self.ctx)
                    self.assertIn('<#1566394824288894976>', self.ctx.reply.call_args.args[0])
                with self.assertRaises(ValueError): await market.quote(self.ctx, 'suspect 1')
            self.ctx.guild = None
            await ns['cmd_market'](self.ctx)
            await ns['cmd_buys'](self.ctx)
            buy.assert_not_awaited()
            self.ctx.guild = NS(id=1)
            for channel in (1566394824288894976, 1533242104304631969):
                self.ctx.channel.id = channel
                await ns['cmd_market'](self.ctx)
                self.assertEqual(self.ctx.reply.call_args.kwargs['embed'].title, 'Suspect Market')
                await ns['cmd_buys'](self.ctx, text='suspect 2')
            self.assertEqual(buy.await_count, 2)
        self.assertEqual(db.suspect_purchases(self.draft), [])
