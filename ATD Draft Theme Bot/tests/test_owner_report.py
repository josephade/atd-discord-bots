"""Exercise the real command without connecting a Discord client."""
import ast
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock


class OwnerReportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        source = Path(__file__).resolve().parents[1] / 'bot.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        selected = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name in ('cmd_allteams', 'cmd_setteams')]
        for command in selected:
            command.decorator_list = []
        self.theme = NS(team_key=lambda *args: 'pair-0', team_label=lambda *args: 'Alice & Bob',
                        setup_teams=AsyncMock(return_value='Registered two teams.'))
        self.database = NS(list_drafters=Mock(return_value=[{'id': 1}, {'id': 2}]),
                           get_picks=Mock(return_value=['Larry Bird']))
        self.resolve = Mock(return_value={'id': 1, 'name': 'Partner draft', 'status': 'active', 'theme': 'partner_draft'})
        self.ns = {'OWNER_ID': 123, 'OWNER_REPORT_CHANNEL_ID': 99,
                   '_resolve_draft': self.resolve, 'db': self.database, 'bot': Mock(),
                   'DraftContext': lambda *args: object(), 'themes': NS(get_theme=lambda key: self.theme)}
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), 'exec'), self.ns)
        self.ctx = NS(author=NS(id=123), guild=NS(default_role=object()), send=AsyncMock(),
                      channel=NS(id=99, permissions_for=Mock(return_value=NS(view_channel=False))))

    async def run_report(self):
        await self.ns['cmd_allteams'](self.ctx, arg='1')

    def assert_no_report_read(self):
        self.resolve.assert_not_called()
        self.database.list_drafters.assert_not_called()

    async def test_owner_gets_pairings_in_configured_private_channel(self):
        await self.run_report()
        output = self.ctx.send.call_args.args[0]
        self.assertIn('Alice & Bob', output)
        self.assertIn('Larry Bird', output)
        self.assertEqual(output.count('Alice & Bob'), 1)

    async def test_non_owner_cannot_view_report(self):
        self.ctx.author.id = 456
        await self.run_report()
        self.ctx.send.assert_not_awaited()
        self.assert_no_report_read()

    async def test_owner_cannot_use_another_channel(self):
        self.ctx.channel.id = 100
        await self.run_report()
        self.assertIn('private owner channel', self.ctx.send.call_args.args[0])
        self.assert_no_report_read()

    async def test_owner_cannot_use_dms(self):
        self.ctx.guild = None
        await self.run_report()
        self.assert_no_report_read()

    async def test_report_blocked_if_channel_becomes_public(self):
        self.ctx.channel.permissions_for.return_value.view_channel = True
        await self.run_report()
        self.assertIn('Hide this channel', self.ctx.send.call_args.args[0])
        self.assert_no_report_read()

    async def test_no_channel_configuration_fails_closed(self):
        self.ns['OWNER_REPORT_CHANNEL_ID'] = None
        await self.run_report()
        self.assert_no_report_read()

    async def run_setup(self):
        await self.ns['cmd_setteams'](self.ctx, '24', text='1. 🦅 - <@111>\n2. 🐻 - <@222>')

    async def test_setteams_allowed_in_private_owner_channel(self):
        await self.run_setup()
        self.theme.setup_teams.assert_awaited_once()
        self.assertEqual(self.ctx.send.call_args.args[0], 'Registered two teams.')

    async def test_setteams_still_allowed_in_owner_dm(self):
        self.ctx.guild = None
        await self.run_setup()
        self.theme.setup_teams.assert_awaited_once()

    async def test_setteams_denied_in_other_server_channels(self):
        self.ctx.channel.id = 100
        await self.run_setup()
        self.theme.setup_teams.assert_not_awaited()
        self.resolve.assert_not_called()

    async def test_setteams_owner_restriction_remains(self):
        self.ctx.author.id = 456
        await self.run_setup()
        self.theme.setup_teams.assert_not_awaited()
        self.ctx.send.assert_not_awaited()

    async def test_setteams_denied_if_owner_channel_is_public(self):
        self.ctx.channel.permissions_for.return_value.view_channel = True
        await self.run_setup()
        self.theme.setup_teams.assert_not_awaited()
        self.resolve.assert_not_called()
