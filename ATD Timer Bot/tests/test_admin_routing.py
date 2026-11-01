import ast
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import admin_routing


class AdminRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = patch.object(admin_routing, 'CONFIG_FILE', Path(self.tmp.name)/'route.json')
        self.config.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.config.stop)
        tree = ast.parse((Path(__file__).resolve().parents[1]/'bot.py').read_text(encoding='utf-8'))
        wanted = {'_get_session', '_resolve_command_session', '_resolve_viewable_session'}
        nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted]
        self.ns = dict(admin_routing=admin_routing, _draft_channel_id=admin_routing.target,
                       _sessions={}, DraftSession=lambda channel_id: NS(channel_id=channel_id),
                       _REMOTE_VIEW_CHANNELS={admin_routing.PRIVATE_CHANNEL_ID},
                       _IN_PROGRESS_STATES=('active',), _ACTIVE_DRAFT_STATES=('active',))
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'bot.py', 'exec'), self.ns)

    async def test_private_setup_and_admin_actions_share_real_selection_session(self):
        private = admin_routing.PRIVATE_CHANNEL_ID
        actual = admin_routing.DEFAULT_DRAFT_CHANNEL_ID
        session = self.ns['_get_session'](private)
        self.assertEqual(session.channel_id, actual)
        self.assertIs(session, self.ns['_get_session'](actual))
        self.assertNotIn(private, self.ns['_sessions'])
        # Other live sessions cannot make the explicit private target ambiguous.
        self.ns['_sessions'][123] = NS(channel_id=123, draft=NS(state='active'))
        ctx = NS(channel=NS(id=private), send=AsyncMock())
        self.assertIs(await self.ns['_resolve_command_session'](ctx), session)
        self.assertIs(await self.ns['_resolve_viewable_session'](ctx), session)
        ctx.send.assert_not_awaited()

    def test_target_can_be_changed_persistently_without_reusing_private_channel(self):
        admin_routing.configure(777)
        self.assertEqual(admin_routing.target(admin_routing.PRIVATE_CHANNEL_ID), 777)
        self.assertEqual(admin_routing.target(123), 123)
        with self.assertRaises(ValueError):
            admin_routing.configure(admin_routing.PRIVATE_CHANNEL_ID)
