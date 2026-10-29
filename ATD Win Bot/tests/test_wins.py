"""Exercise command logic without connecting to Discord or Google Sheets."""
import ast
from contextlib import asynccontextmanager
from pathlib import Path
import re
import difflib
import unicodedata
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock
from player_aliases import PLAYER_ALIASES


class Embed:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def set_footer(self, **kwargs):
        self.footer = kwargs

    def add_field(self, **kwargs):
        if not hasattr(self, 'fields'):
            self.fields = []
        self.fields.append(kwargs)


class View:
    def __init__(self, **kwargs):
        self.children = [NS(disabled=False), NS(disabled=False)]


def command_namespace():
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'bot.py').read_text(encoding='utf-8'))
    wanted = {'_safe_int', '_safe_pct', '_parse', '_find', '_name_variants', '_all_name_variants', '_drafter_match', '_shared_draft_wins', '_resolve_drafter_name', '_team_gms', '_team_wins_line', 'cmd_team', 'cmd_gmteam', 'cmd_wins', 'cmd_profile', 'PlayerListView'}
    wanted.update({'_add_chunked_field', '_player_drafter_win_results', '_canonical_player_name', '_team_wins_value', '_build_player_win_totals', 'cmd_playerwins', '_fold', '_player_key', '_roster_player_names', '_get_drafter_players', '_make_drafter_resolver',
                   '_build_player_drafter_index', '_get_adp', '_lookup_adp_player', '_title_player', 'cmd_merchant'})
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in wanted]
    discord = NS(
        Embed=Embed, Interaction=object, ButtonStyle=NS(secondary=2),
        ui=NS(View=View, Button=object, button=lambda **kw: lambda fn: fn),
    )
    ns = {
        're': re, 'difflib': difflib, 'unicodedata': unicodedata,
        'discord': discord, 'RECENT_COUNT': 5, 'C_PROFILE': 1, 'GAMES_PER_DRAFT': 82,
        'DRAFTER_ALIASES': {'old name': 'New Name'}, 'PLAYER_ALIASES': PLAYER_ALIASES,
        '_CASE_SENSITIVE_NAME_OVERRIDES': {'CEO': 'CEOOfKobeFans'},
        'bot': NS(command=lambda **kw: lambda fn: fn),
        '_in_channel': lambda ctx: True,
        '_err': lambda msg: Embed(description=msg),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'bot.py', 'exec'), ns)
    return ns


@asynccontextmanager
async def typing():
    yield


class WinsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ns = command_namespace()
        self.raw = [['Name'] + [f'Draft {n}' for n in range(90, 113)]]
        # The user skipped several league drafts; a recorded zero still counts.
        self.raw.append(['New Name'] + [str(n - 90) if n % 2 == 0 else '' for n in range(90, 113)])
        self.ns['_get_raw'] = AsyncMock(return_value=self.raw)
        self.ns['_load_profiles'] = lambda: {'123': {'sheet_name': 'old name'}}
        self.ctx = NS(typing=typing, send=AsyncMock())

    async def run_command(self, args):
        await self.ns['cmd_wins'](self.ctx, args=args)
        return self.ctx.send.call_args.kwargs

    async def test_default_ten_own_drafts_newest_first_and_alias_resolution(self):
        sent = await self.run_command('<@123>')
        self.assertNotIn('view', sent)
        desc = sent['embed'].description
        self.assertIn('Latest **10** of **12**', desc)
        rows = [line for line in desc.splitlines() if line.startswith('ATD ')]
        self.assertEqual(len(rows), 10)
        self.assertTrue(rows[0].startswith('ATD 112'))
        self.assertTrue(rows[-1].startswith('ATD 94'))
        self.assertNotIn('ATD 111', desc)

    async def test_requested_count_and_legacy_mention(self):
        sent = await self.run_command('<@!123> 2')
        self.assertIn('Latest **2** of **12**', sent['embed'].description)
        self.assertIn('**42** wins', sent['embed'].description)

    async def test_more_than_available_paginates_and_includes_zero(self):
        sent = await self.run_command('<@123> 100')
        view = sent['view']
        self.assertEqual(len(view.lines), 12)
        self.assertEqual(view.total_pages, 2)
        self.assertIn('12 drafts total', sent['embed'].footer['text'])
        self.assertTrue(view.children[0].disabled)
        response = NS(edit_message=AsyncMock())
        await view.next_button(NS(response=response), None)
        last_page = response.edit_message.call_args.kwargs['embed']
        self.assertIn('ATD 90', last_page.description)
        self.assertIn('  0 wins', last_page.description)
        self.assertTrue(view.children[1].disabled)
        await view.prev_button(NS(response=response), None)
        self.assertEqual(view.page, 0)

    async def test_invalid_arguments_do_not_read_sheet(self):
        for args in ('', '<@123> 0', '<@123> -1', '<@123> 1.5', '<@123> abc', '<@123> 3 extra', 'someone 2'):
            self.ctx.send.reset_mock()
            sent = await self.run_command(args)
            self.assertIn('Usage:', sent['embed'].description)
        self.ns['_get_raw'].assert_not_awaited()

    async def test_unlinked_user(self):
        sent = await self.run_command('<@456>')
        self.assertIn('!linkprofile', sent['embed'].description)
        self.ns['_get_raw'].assert_not_awaited()

    async def test_missing_sheet_row(self):
        self.ns['_get_raw'].return_value = [['Name', 'Draft 112'], ['Someone else', '40']]
        sent = await self.run_command('<@123>')
        self.assertIn('No Win Sheet drafter', sent['embed'].description)

    async def test_no_recorded_results(self):
        self.ns['_get_raw'].return_value = [['Name', 'Draft 112'], ['New Name', '']]
        sent = await self.run_command('<@123>')
        self.assertIn('no recorded draft results', sent['embed'].description)

    async def test_empty_sheet(self):
        self.ns['_get_raw'].return_value = []
        sent = await self.run_command('<@123>')
        self.assertIn('Could not load', sent['embed'].description)

    def set_partner(self):
        self.ns['_load_profiles'] = lambda: {
            '123': {'sheet_name': 'old name'}, '456': {'sheet_name': 'Partner'},
        }
        self.raw.append(['Partner'] + self.raw[1][1:])
        self.ns['_load_lottos'] = lambda: {
            f'ATD {n}': {'Team': {'drafters': ['old name', 'Partner']}}
            for n in range(90, 113, 2)
        }

    async def test_pair_defaults_to_ten_shared_drafts(self):
        self.set_partner()
        sent = await self.run_command('<@123> <@456>')
        self.assertIn('New Name + Partner', sent['embed'].title)
        self.assertIn('Latest **10** of **12** shared drafts', sent['embed'].description)
        self.assertIn('ATD 112', sent['embed'].description)
        self.assertNotIn('ATD 90 ', sent['embed'].description)

    async def test_pair_custom_count_and_pagination(self):
        self.set_partner()
        sent = await self.run_command('<@!123> <@456> 11')
        self.assertEqual(sent['view'].total_pages, 2)
        self.assertEqual(len(sent['view'].lines), 11)

    async def test_same_wins_on_different_teams_does_not_count(self):
        self.set_partner()
        self.ns['_load_lottos'] = lambda: {
            'ATD 112': {'Team A': {'drafters': ['New Name']}, 'Team B': {'drafters': ['Partner']}},
            'ATD 108': {'Team C': {'drafters': ['old name', 'Partner']}},
        }
        sent = await self.run_command('<@123> <@456> 1')
        self.assertIn('ATD 108', sent['embed'].description)
        self.assertNotIn('ATD 112', sent['embed'].description)
        self.assertIn('Latest **1** of **1** shared drafts', sent['embed'].description)

    async def test_pair_missing_ownership_data(self):
        self.set_partner()
        self.ns['_load_lottos'] = lambda: {}
        sent = await self.run_command('<@123> <@456>')
        self.assertIn('No shared teams found', sent['embed'].description)

    async def test_pair_unlinked_second_user(self):
        sent = await self.run_command('<@123> <@456> 5')
        self.assertIn('second user', sent['embed'].description)
        self.ns['_get_raw'].assert_not_awaited()

    async def test_duplicate_mentions(self):
        sent = await self.run_command('<@123> <@123> 5')
        self.assertIn('two different users', sent['embed'].description)

    async def test_same_drafter_on_two_accounts(self):
        self.set_partner()
        self.ns['_load_profiles'] = lambda: {'123': {'sheet_name': 'old name'}, '456': {'sheet_name': 'New Name'}}
        sent = await self.run_command('<@123> <@456>')
        self.assertIn('same Win Sheet drafter', sent['embed'].description)

    def test_shared_draft_result_labels_and_split_drafts(self):
        first = {'name': 'New Name', 'draft_wins': {110: 0, 109: 42, 108: 33}}
        second = {'name': 'Partner', 'draft_wins': {110: 0, 109: 43}}
        teams = {'Team': {'drafters': ['old name', 'Partner']}}
        lottos = {f'ATD {n}': teams for n in (107, 108, 109, 110)}
        lottos['ATD 110-D2'] = teams
        results = self.ns['_shared_draft_wins'](first, second, lottos)
        self.assertEqual([r['draft'] for r in results], ['ATD 110-D2', 'ATD 110', 'ATD 109', 'ATD 108', 'ATD 107'])
        self.assertEqual([r['label'] for r in results], ['0 wins', '0 wins', '42 / 43 wins*', '33 wins', 'Wins not recorded'])
        self.assertTrue(results[2]['conflict'])


if __name__ == '__main__':
    unittest.main()
