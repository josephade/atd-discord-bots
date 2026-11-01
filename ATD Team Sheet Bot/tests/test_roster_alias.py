"""Public roster aliases share screenshot output and never read owner mappings."""
import ast
import asyncio
from contextlib import asynccontextmanager
import io
from pathlib import Path
import re
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock

import discord
from discord.ext import commands
import budget_rules


@asynccontextmanager
async def typing():
    yield


class RosterAliasTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_commands_send_same_png_without_owner_mentions(self):
        tree = ast.parse((Path(__file__).resolve().parents[1]/'bot.py').read_text(encoding='utf-8'))
        function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'cmd_teamshot')
        caption = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_teamshot_caption')
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        manager = Mock()
        manager.find_team_position.return_value = (1, 1)
        manager.get_matchup_range.return_value = ('sheet', 1, 'A1:C11')
        image = Mock()
        image.save.side_effect = lambda buffer, format: buffer.write(b'PNG')
        ns = dict(bot=bot, discord=discord, re=re, asyncio=asyncio, io=io,
                  _channel_sheet_map={99: dict(tab='ATD 113')},
                  budget_rules=budget_rules, _load_config=lambda: {},
                  _resolve_team_name_from_emoji=lambda raw: ('Cleveland Cavaliers', None),
                  _get_manager_async=AsyncMock(return_value=manager),
                  _get_export_token=Mock(return_value='test'), _export_team_range_png=Mock(return_value=image))
        # No ownership mapping exists in this namespace. Any accidental owner
        # lookup would fail instead of quietly revealing someone in the output.
        exec(compile(ast.Module(body=[caption, function], type_ignores=[]), 'bot.py', 'exec'), ns)
        self.assertIs(bot.get_command('roster'), bot.get_command('teamshot'))
        for command in ('roster', 'teamshot'):
            for args in ('<:Cavs:1234>', 'ATD 113 <:Cavs:1234>'):
                ctx = NS(channel=NS(id=99, typing=typing), send=AsyncMock())
                await bot.get_command(command).callback(ctx, args=args)
                call = ctx.send.call_args.kwargs
                self.assertEqual(call['content'], '**Cleveland Cavaliers**')
                self.assertEqual(call['file'].filename, 'teamshot.png')
                self.assertEqual(call['file'].fp.getvalue(), b'PNG')
                self.assertNotIn('embed', call)
                self.assertNotIn('<@', call['content'])
                call['file'].close()
        await bot.close()
