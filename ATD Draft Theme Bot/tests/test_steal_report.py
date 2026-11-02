import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, AsyncMock, patch
from decimal import Decimal
import test_readiness
import db
import steal_report

ROWS=[['Player','Value','Round 2','Round 3'],['A Player',30,27,40],['B Player',5,2,0],['C Player',40,38,0],
      ['Same',10,10,1],['Rise',10,12,1],['Missing',10,'',1],['Conflict',50,47,1],['Conflict',50,48,1]]


class RankingTests(unittest.TestCase):
    def test_drops_rank_biggest_first_then_cheaper_price_and_ignore_future_round(self):
        rows,excluded=steal_report.rank_drops(ROWS,2,['A Player'])
        self.assertEqual([r['player'] for r in rows],['B Player','C Player'])
        self.assertEqual([r['drop'] for r in rows],[Decimal(3),Decimal(2)])
        self.assertNotIn('A Player',[r['player'] for r in rows])
        self.assertEqual(excluded,2)

    def test_blue_and_black_taken_fills_excluded_but_white_unfilled_and_heatmap_safe(self):
        def cell(name,bg=None):
            return dict(formattedValue=name,effectiveFormat={} if bg is None else {'backgroundColor':bg})
        cells=[cell('A Player',{'red':72/255,'green':132/255,'blue':232/255}),
               cell('B Player',{}),cell('C Player',{'red':1,'green':1,'blue':1}),
               cell('Other'),cell('Heatmap',{'red':1,'green':.5})]
        meta={'sheets':[{'data':[{'rowData':[{'values':[c]} for c in cells]}]}]}
        self.assertEqual(steal_report.highlighted_names(meta),['A Player','B Player'])
        rows,_=steal_report.rank_drops(ROWS,2,steal_report.highlighted_names(meta))
        self.assertEqual([r['player'] for r in rows],['C Player'])

    def test_round_one_and_missing_previous_round_never_use_unrelated_history(self):
        with self.assertRaises(ValueError): steal_report.rank_drops(ROWS,1)
        with self.assertRaisesRegex(ValueError,'Round 2'): steal_report.rank_drops([['Player','Value','Round 3'],['A',30,20]],3)

    def test_waiting_flux_uses_last_published_round_and_in_progress_is_blocked(self):
        draft=dict(status='active',settings={'rounds':10},state=dict(teams=[{},{}],live_pick_number=5,flux_wait={'round':3}))
        self.assertEqual(steal_report.current_round(draft),2)
        draft['state']['flux_in_progress']=True
        with self.assertRaisesRegex(ValueError,'in progress'):steal_report.current_round(draft)

    def test_embed_has_ranked_price_changes_and_pagination_metadata(self):
        rows,excluded=steal_report.rank_drops(ROWS,2)
        from datetime import datetime,timezone
        report=dict(round=2,ranked=rows,excluded=excluded,updated=datetime.now(timezone.utc))
        embed=steal_report.report_embed(report)
        self.assertIn('Round 1 to Round 2',embed.description)
        self.assertIn('$5',embed.description);self.assertIn('**-$3**',embed.description)
        self.assertIn('2 missing/conflicting',embed.footer.text)
        self.assertEqual(steal_report.money(Decimal('30.00')),'30')


class PrivateReportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        db.DB_PATH=str(Path(self.tmp.name)/'steal.db');db.init_db()
        self.draft=db.create_draft(1,77,'anon_budget_draft','Test',{'rounds':10})
        db.set_draft_status(self.draft,'active')
        db.set_draft_state(self.draft,dict(teams=[{},{}],live_pick_number=3,flux_price_source=dict(sheet='test-sheet-id',tab='ADP')))
        self.ctx=NS(author=NS(id=steal_report.OWNER_ID),guild=NS(id=1,default_role=object()),
                    channel=NS(id=77,permissions_for=lambda role:NS(view_channel=False)),send=AsyncMock(return_value=NS(edit=AsyncMock())))
        self.channel_patch=patch.object(steal_report,'OWNER_REPORT_CHANNEL_ID',77);self.channel_patch.start()

    async def asyncTearDown(self):self.channel_patch.stop();self.tmp.cleanup()

    async def test_private_owner_report_loads_sheet_once_without_changing_draft(self):
        before=db.get_draft(self.draft)
        with patch.object(steal_report.sheets,'load_flux_report_data',create=True,return_value=(ROWS,{})) as read:
            await steal_report.show(self.ctx)
        read.assert_called_once()
        edit=self.ctx.send.return_value.edit.call_args.kwargs
        self.assertIn('B Player',edit['embed'].description)
        self.assertIsNone(edit['content'])
        self.assertEqual(db.get_draft(self.draft),before)

    async def test_nonowner_public_channel_and_dm_are_silent(self):
        with patch.object(steal_report,'load',new_callable=AsyncMock) as load:
            self.ctx.author.id=0
            await steal_report.show(self.ctx)
            self.ctx.author.id=steal_report.OWNER_ID;self.ctx.channel.id=934052208624930848
            await steal_report.show(self.ctx)
            self.ctx.channel.id=77;self.ctx.guild=None
            await steal_report.show(self.ctx)
            load.assert_not_awaited();self.ctx.send.assert_not_awaited()

    async def test_report_page_buttons_are_owner_and_private_channel_only(self):
        view=steal_report.ReportPages({'ranked':[{}]*16})
        self.assertTrue(view.previous.disabled);self.assertFalse(view.next.disabled)
        interaction=NS(user=NS(id=0),channel_id=77,response=NS(send_message=AsyncMock()))
        self.assertFalse(await view.interaction_check(interaction))
        interaction.user.id=steal_report.OWNER_ID;interaction.channel_id=0
        self.assertFalse(await view.interaction_check(interaction))
        interaction.channel_id=77
        self.assertTrue(await view.interaction_check(interaction))
        view.stop()
