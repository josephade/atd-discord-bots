"""User-facing command errors without connecting Discord or Sheets."""
import ast
from contextlib import asynccontextmanager
from pathlib import Path
import re
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock

SOURCE = Path(__file__).resolve().parents[1] / "bot.py"


class CheckFailure(Exception):
    pass


class UserInputError(Exception):
    pass


class MissingRequiredArgument(UserInputError):
    pass


class CommandNotFound(Exception):
    pass


class CommandInvokeError(Exception):
    pass


@asynccontextmanager
async def typing():
    yield


def namespace():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    nodes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in (
                "_command_input_error", "on_command_error", "_normalize_year", "cmd_addyear", "_addyear_roster"):
            node.decorator_list = []
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name)
                and t.id == "_COMMAND_INPUT_HELP" for t in node.targets):
            nodes.append(node)
    ns = dict(re=re, commands=NS(CheckFailure=CheckFailure, UserInputError=UserInputError,
              MissingRequiredArgument=MissingRequiredArgument, CommandNotFound=CommandNotFound,
              CommandInvokeError=CommandInvokeError))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), ns)
    return ns


def context(name):
    return NS(command=NS(name=name, qualified_name=name), clean_prefix="!", invoked_with=name,
              send=AsyncMock(), author=NS(id=1), channel=NS(id=123, typing=typing))


class CommandErrorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ns = namespace()

    async def test_missing_addyear_explains_player_and_season(self):
        ctx = context("addyear")
        await self.ns["on_command_error"](ctx, MissingRequiredArgument())
        message = ctx.send.call_args.args[0]
        self.assertIn("Please provide a player name and season", message)
        self.assertIn("Usage: `!addyear <player name> <year>`", message)
        self.assertIn("Example: `!addyear Jimmy Butler 2016-17`", message)
        self.assertNotIn("args", message)

    async def test_bad_numeric_and_channel_input_show_expected_inputs(self):
        for name, expected in (("setbudget", "whole number of dollars"),
                               ("addchannel", "channel mention and worksheet tab name")):
            with self.subTest(command=name):
                ctx = context(name)
                await self.ns["on_command_error"](ctx, UserInputError())
                message = ctx.send.call_args.args[0]
                self.assertIn(expected, message)
                self.assertIn("Usage:", message)
                self.assertIn("Example:", message)

    async def test_unknown_commands_and_permission_errors_preserved(self):
        ctx = context("unknown")
        await self.ns["on_command_error"](ctx, CommandNotFound())
        ctx.send.assert_not_awaited()
        await self.ns["on_command_error"](ctx, CheckFailure())
        self.assertIn("permission", ctx.send.call_args.args[0])

    def test_every_command_with_arguments_has_specific_help(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.AsyncFunctionDef) or ast.unparse(node.args) == "ctx":
                continue
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and decorator.func.attr == "command":
                    name = next((k.value.value for k in decorator.keywords if k.arg == "name"), node.name)
                    self.assertIn(name, self.ns["_COMMAND_INPUT_HELP"])

    def test_alias_and_prefix_are_used(self):
        ctx = context("addyear")
        ctx.clean_prefix, ctx.invoked_with = "?", "alias"
        self.assertIn("Example: `?alias Jimmy Butler 2016-17`", self.ns["_command_input_error"](ctx))

    def prepare_addyear(self):
        manager = NS(_call=Mock(return_value=[["OSU", "Year"], ["Jimmy Butler", ""]]),
                     _find_team_cell=Mock(return_value=(1, 1)))
        self.ns.update(_team_owners={"1": "OSU"}, _get_manager=Mock(return_value=manager),
            _channel_sheet_map={123: {}}, _find_roster_slot=lambda *args: ({"name": "Jimmy Butler", "offset": 1}, None),
            gspread=NS(utils=NS(rowcol_to_a1=lambda *args: "B2")), _friendly_sheet_error=str)
        return manager

    async def test_missing_or_invalid_year_never_touches_sheet(self):
        self.prepare_addyear()
        for text in ("Jimmy", "Jimmy Butler", "Jimmy Butler nonsense"):
            with self.subTest(input=text):
                ctx = context("addyear")
                await self.ns["cmd_addyear"](ctx, args=text)
                self.assertIn("Example: `!addyear Jimmy Butler 2016-17`", ctx.send.call_args.args[0])
        self.ns["_get_manager"].assert_not_called()

    async def test_supported_year_formats_still_write_normalized_season(self):
        manager = self.prepare_addyear()
        for year in ("2017", "'17", "2016-17"):
            with self.subTest(year=year):
                await self.ns["cmd_addyear"](context("addyear"), args="Jimmy Butler " + year)
                manager._call.assert_called_with("batch_update", [{"range": "B2", "values": [["2016-17"]]}])


if __name__ == "__main__":
    unittest.main()
