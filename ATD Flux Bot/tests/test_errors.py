import ast
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock
import discord
from discord.ext import commands


class FluxErrors(unittest.IsolatedAsyncioTestCase):
    def handler(self):
        tree = ast.parse((Path(__file__).resolve().parents[1]/'flux_bot.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name=='on_command_error')
        node.decorator_list=[]
        ns=dict(discord=discord, commands=commands, log=Mock())
        exec(compile(ast.Module(body=[node],type_ignores=[]),'flux_bot.py','exec'),ns)
        return ns['on_command_error']

    async def test_failure_before_write_updates_progress_instead_of_leaving_it_running(self):
        progress=NS(edit=AsyncMock())
        ctx=NS(command=NS(name='flux'),channel=NS(id=99),send=AsyncMock(),_flux_progress_message=progress,_flux_price_write_started=False)
        await self.handler()(ctx,commands.CommandInvokeError(ValueError('Grid error')))
        self.assertIn('before writing any new round prices',progress.edit.call_args.kwargs['content'])
        ctx.send.assert_not_awaited()

    async def test_partial_write_is_not_reported_as_safe_to_reroll(self):
        progress=NS(edit=AsyncMock())
        ctx=NS(command=NS(name='fluxplayers'),channel=NS(id=99),send=AsyncMock(),_flux_progress_message=progress,_flux_price_write_started=True)
        await self.handler()(ctx,commands.CommandInvokeError(ValueError('Write failed')))
        self.assertIn('partial round',progress.edit.call_args.kwargs['content'])
        self.assertIn('check it before running Flux again',progress.edit.call_args.kwargs['content'])

    async def test_other_bots_commands_are_silently_ignored(self):
        ctx=NS(command=None,channel=NS(id=99),send=AsyncMock())
        await self.handler()(ctx,commands.CommandNotFound('nextpick'))
        ctx.send.assert_not_awaited()
