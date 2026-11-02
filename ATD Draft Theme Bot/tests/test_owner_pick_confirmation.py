import ast
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch
import test_readiness
import discord
import owner_picks


class ConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_confirmation_is_an_embed_not_sheet_pick_text(self):
        tree = ast.parse((Path(__file__).resolve().parents[1]/'bot.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='cmd_anonpick')
        node.decorator_list=[]
        ns=dict(discord=discord,OWNER_ID=1,OWNER_REPORT_CHANNEL_ID=2,bot=Mock(),_resolve_draft=lambda ref:{'id':30})
        exec(compile(ast.Module(body=[node],type_ignores=[]),'bot.py','exec'),ns)
        ctx=NS(author=NS(id=1),guild=NS(id=3),channel=NS(id=2),message=NS(id=4),send=AsyncMock())
        confirmation='Recorded pick #31 for <:Wizards:123>: **Luka Doncic**.'
        with patch.object(owner_picks,'submit',new_callable=AsyncMock,return_value=confirmation):
            await ns['cmd_anonpick'](ctx,'30','<:Wizards:123>',text='Luka Doncic $24')
        call=ctx.send.call_args
        self.assertFalse(call.args)
        self.assertEqual(call.kwargs['embed'].description,confirmation)
        self.assertFalse(call.kwargs['allowed_mentions'].users)
