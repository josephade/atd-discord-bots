from types import SimpleNamespace as NS
from datetime import timedelta
import unittest
from unittest.mock import Mock, AsyncMock
import discord
import restart_recovery


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setup_case(self, present=None):
        data=dict(team='Portland Trail Blazers',player='Jayson Tatum',year='2024-25',price='$24',position_override=None,bench_only=False)
        self.present=present
        manager=NS(_call=Mock(return_value=[]),_find_existing_player=Mock(side_effect=lambda *args:self.present),add_player=Mock())
        def add(*args,**kwargs):
            self.present=data['team'];return True,'PF Starter'
        manager.add_player.side_effect=add
        msg=NS(id=34,author=NS(id=restart_recovery.THEME_BOT_ID),content="34. <:trailblazers:123> Jayson Tatum '25 $24",reactions=[],add_reaction=AsyncMock())
        async def history(**kwargs):
            yield msg
        channel=NS(history=Mock(side_effect=history))
        return data,manager,msg,channel

    async def test_missed_accepted_pick_added_and_acknowledged_once(self):
        data,manager,message,channel=self.setup_case()
        get=AsyncMock(return_value=manager);audit=Mock();bot=NS(get_channel=Mock(return_value=channel))
        for n in range(2):
            await restart_recovery.recover_recent(bot,{99:dict(tab='Current',sheet_id='sheet')},get,lambda text:(data,None),audit)
        manager.add_player.assert_called_once()
        self.assertEqual(message.add_reaction.await_count,2)
        self.assertEqual(audit.call_args.args[0]['source_message_id'],34)

    async def test_existing_player_on_correct_team_only_adds_missing_ack(self):
        data,manager,message,channel=self.setup_case('Portland Trail Blazers')
        await restart_recovery.recover_recent(NS(get_channel=lambda cid:channel),{99:{}},AsyncMock(return_value=manager),lambda text:(data,None),Mock())
        manager.add_player.assert_not_called()
        message.add_reaction.assert_awaited_once_with('\u2705')

    async def test_other_team_conflict_never_overwrites_or_acknowledges(self):
        data,manager,message,channel=self.setup_case('Other team')
        await restart_recovery.recover_recent(NS(get_channel=lambda cid:channel),{99:{}},AsyncMock(return_value=manager),lambda text:(data,None),Mock())
        manager.add_player.assert_not_called();message.add_reaction.assert_not_awaited()

    async def test_admin_confirmations_skips_and_own_confirmed_picks_are_ignored(self):
        data,manager,message,channel=self.setup_case()
        get=AsyncMock(return_value=manager)
        for text,reactions in [('Recorded pick #34 for <:team:123>: **Jayson Tatum**.',[]),
                               ('34. <:team:123> skipped',[]),
                               (message.content,[NS(emoji='\u2705',me=True)])]:
            message.content=text;message.reactions=reactions
            await restart_recovery.recover_recent(NS(get_channel=lambda cid:channel),{99:{}},get,lambda text:(data,None),Mock())
        get.assert_not_awaited();message.add_reaction.assert_not_awaited()

    async def test_changed_sheet_mapping_stops_replay_and_link_time_limits_history(self):
        data,manager,message,channel=self.setup_case()
        linked=discord.utils.utcnow()-timedelta(seconds=30)
        mappings={99:dict(tab='Current',linked_at=linked.isoformat())}
        async def history(**kwargs):
            self.assertGreaterEqual(kwargs['after'],linked)
            mappings[99]=dict(tab='New draft')
            yield message
        channel.history=Mock(side_effect=history)
        get=AsyncMock(return_value=manager)
        await restart_recovery.recover_recent(NS(get_channel=lambda cid:channel),mappings,get,lambda text:(data,None),Mock())
        get.assert_not_awaited()
