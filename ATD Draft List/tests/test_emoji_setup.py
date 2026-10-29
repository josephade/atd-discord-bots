import ast
import asyncio
import logging
from pathlib import Path
import re
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock


class Emoji:
    def __init__(self, name, emoji_id):
        self.name, self.id = name, emoji_id

    def __str__(self):
        return f'<:{self.name}:{self.id}>'


def namespace():
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'bot.py').read_text(encoding='utf-8'))
    wanted = {'_ensure_user', '_is_valid_emoji', '_resolve_emoji_input', '_extract_lotto_lines',
              '_scan_lotto_teams', '_load_lotto_teams', '_lookup_emoji_from_lotto',
              '_ensure_team_emoji', '_confirm_team_setup', '_build_pick_message'}
    constants = {'_CUSTOM_EMOJI_RE2', '_UNICODE_EMOJI_RE'}
    nodes = [n for n in tree.body if
             isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in wanted
             or isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in n.targets)]
    local = Emoji('OKC', 123)
    channel = NS(id=1, guild=NS(emojis=[local]))
    ns = dict(re=re, asyncio=asyncio, time=time, log=logging.getLogger('test'),
              _data={}, _save_data=Mock(), _with_default_year=lambda pick: pick, _lotto_teams_cache=None,
              _lotto_teams_cache_ts=0, _lotto_lookup_lock=asyncio.Lock(),
              LOTTO_CHANNEL_ID=2, DRAFT_CHANNEL_ID=1,
              bot=NS(emojis=[local], get_channel=lambda n: channel if n == 1 else None))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'bot.py', 'exec'), ns)
    return ns


class EmojiSetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ns = namespace()

    def test_shortcodes_are_not_valid_pick_markers(self):
        for text in (':OKC:', ':Nuggets:', '', 'OKC', 'hello', '<:DeletedTeam:999>'):
            self.assertFalse(self.ns['_is_valid_emoji'](text))
        for text in ('<:OKC:123>', '<a:OKC:123>', '🦌', '🇮🇪', '☀️'):
            self.assertTrue(self.ns['_is_valid_emoji'](text))

    def test_shortcode_and_old_id_resolve_to_live_server_emoji(self):
        for text in (':OKC:', ':okc:', '<:OKC:999>'):
            self.assertEqual(self.ns['_resolve_emoji_input'](text), '<:OKC:123>')

    def test_lotto_parses_slashes_commas_and_multiple_co_gms(self):
        text = '1. <:OKC:123> - <@10> / <@!11>\n2. 🦌 — <@12>, <@13> / <@14>'
        self.assertEqual(list(self.ns['_extract_lotto_lines'](text)),
                         [('<:OKC:123>', [10, 11]), ('🦌', [12, 13, 14])])

    async def test_missing_emoji_is_saved_from_current_lotto(self):
        self.ns['_lookup_emoji_from_lotto'] = AsyncMock(return_value='<:OKC:999>')
        self.assertEqual(await self.ns['_ensure_team_emoji']('10'), '<:OKC:123>')
        self.assertEqual(self.ns['_data']['10']['emoji'], '<:OKC:123>')
        self.ns['_save_data'].assert_called_once()

    async def test_existing_shortcode_is_repaired_without_lotto_request(self):
        self.ns['_data']['10'] = {'emoji': ':OKC:', 'picks': []}
        lookup = self.ns['_lookup_emoji_from_lotto'] = AsyncMock()
        self.assertEqual(await self.ns['_ensure_team_emoji']('10'), '<:OKC:123>')
        lookup.assert_not_awaited()

    async def test_unresolved_team_gets_warning_and_keeps_list(self):
        picks = [{'player': 'Marques Johnson'}]
        self.ns['_data']['10'] = {'emoji': '', 'picks': picks}
        self.ns['_lookup_emoji_from_lotto'] = AsyncMock(return_value=None)
        ctx = NS(send=AsyncMock())
        await self.ns['_confirm_team_setup'](ctx, '10')
        self.assertIn("couldn't identify the team", ctx.send.await_args.args[0])
        self.assertIs(self.ns['_data']['10']['picks'], picks)

    def test_post_builder_refuses_unresolved_shortcode(self):
        with self.assertRaises(ValueError):
            self.ns['_build_pick_message'](69, ':OKC:', {'player': 'Jason Kidd'})

    async def test_newest_lotto_does_not_assign_old_draft_team(self):
        latest = NS(content='1. <:OKC:123> - <@10> / <@11>\n2. 🦌 - <@12>')
        older = NS(content='1. <:OKC:123> - <@99>\n2. 🦌 - <@98>')
        async def history(**kwargs):
            yield latest
            yield older
        channel = NS(id=2, history=history)
        self.ns['bot'].get_channel = lambda n: channel
        self.assertEqual(await self.ns['_lookup_emoji_from_lotto'](11), '<:OKC:123>')
        self.assertIsNone(await self.ns['_lookup_emoji_from_lotto'](99))


if __name__ == '__main__':
    unittest.main()
