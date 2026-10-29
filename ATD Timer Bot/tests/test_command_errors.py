"""Check both bots' real error formatters/handlers without starting Discord."""
import ast
import inspect
from pathlib import Path
import types
import unittest
from unittest.mock import AsyncMock

WORKSPACE = Path(__file__).resolve().parents[2]


class CommandError(Exception):
    pass


class UserInputError(CommandError):
    pass


class MissingRequiredArgument(UserInputError):
    def __init__(self, param):
        self.param = param


class CheckFailure(CommandError):
    pass


class MissingPermissions(CheckFailure):
    pass


class CommandNotFound(CommandError):
    pass


class CommandInvokeError(CommandError):
    pass


class Member:
    pass


def load_bot(folder):
    tree = ast.parse((WORKSPACE / folder / "bot.py").read_text(encoding="utf-8"))
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in (
                "_command_usage_error", "on_command_error"):
            node.decorator_list = []
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name)
                and t.id == "_COMMAND_INPUT_EXAMPLES" for t in node.targets):
            selected.append(node)
    namespace = {"commands": types.SimpleNamespace(
        CommandNotFound=CommandNotFound, MissingRequiredArgument=MissingRequiredArgument,
        UserInputError=UserInputError, CheckFailure=CheckFailure,
        MissingPermissions=MissingPermissions, CommandInvokeError=CommandInvokeError),
        "COMMISSIONER_ROLE": "Commissioner", "_err": lambda text: text}
    exec(compile(ast.Module(body=selected, type_ignores=[]), folder, "exec"), namespace)
    return namespace


def context(name, params, invoked=None):
    return types.SimpleNamespace(
        command=types.SimpleNamespace(qualified_name=name, clean_params={p.name: p for p in params}),
        clean_prefix="!", invoked_with=invoked or name, current_parameter=None, send=AsyncMock())


def parameter(name, annotation=str, default=inspect.Parameter.empty, kind=inspect.Parameter.POSITIONAL_OR_KEYWORD):
    return inspect.Parameter(name, kind, annotation=annotation, default=default)


class CommandErrors(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bots = {folder: load_bot(folder) for folder in ("ATD Timer Bot", "ATD Win Bot")}

    def message(self, ctx, folder):
        args, kwargs = ctx.send.call_args
        return kwargs["embed"] if folder == "ATD Win Bot" else args[0]

    async def test_missing_gm_has_usage_example_and_no_internal_member_name(self):
        param = parameter("member", Member)
        ctx = context("nextpick", [param])
        await self.bots["ATD Timer Bot"]["on_command_error"](ctx, MissingRequiredArgument(param))
        message = self.message(ctx, "ATD Timer Bot")
        self.assertIn("Mention the GM", message)
        self.assertIn("Usage: `!nextpick @GM`", message)
        self.assertIn("Example: `!nextpick @theflightmike`", message)
        self.assertNotIn("member", message)

    async def test_missing_second_argument_includes_entire_command(self):
        param = parameter("pick_number", int)
        ctx = context("timeraddpick", [parameter("member", Member), param,
            parameter("pick_text", kind=inspect.Parameter.KEYWORD_ONLY)])
        await self.bots["ATD Timer Bot"]["on_command_error"](ctx, MissingRequiredArgument(param))
        message = self.message(ctx, "ATD Timer Bot")
        self.assertIn("Please provide a pick number", message)
        self.assertIn("!timeraddpick @GM <pick number> <player and year>", message)
        self.assertIn("!timeraddpick @GM 74 Devin Booker 2022-23", message)

    async def test_bad_gm_explains_mention_and_bad_number_explains_whole_number(self):
        for folder, ns in self.bots.items():
            for param, reason in ((parameter("member", Member), "Mention them using @"),
                                  (parameter("pick_number", int), "whole number for pick number")):
                with self.subTest(bot=folder, param=param.name):
                    ctx = context("test", [param])
                    ctx.current_parameter = param
                    await ns["on_command_error"](ctx, UserInputError())
                    self.assertIn(reason, self.message(ctx, folder))
                    self.assertIn("Usage:", self.message(ctx, folder))

    async def test_parsing_errors_without_parameter_still_show_usage(self):
        for folder, ns in self.bots.items():
            ctx = context("compare", [parameter("args", default="")])
            await ns["on_command_error"](ctx, UserInputError())
            message = self.message(ctx, folder)
            self.assertIn("Invalid command input", message)
            self.assertIn("Example:", message)
            if folder == "ATD Win Bot":
                self.assertIn("!compare <drafter name> vs <drafter name>", message)
                self.assertIn("!compare Mondon vs Easy", message)

    def test_global_and_individual_timer_syntax_explained(self):
        ns = self.bots["ATD Timer Bot"]
        ctx = context("timersettimer", [parameter("target"), parameter("minutes", int, None)])
        message = ns["_command_usage_error"](ctx, parameter=ctx.command.clean_params["target"])
        self.assertIn("!timersettimer [@GM] <minutes>", message)
        self.assertIn("!timersettimer @GM 60", message)
        self.assertNotIn("target", message)

    def test_optional_arguments_and_alias_prefix(self):
        for folder, ns in self.bots.items():
            ctx = context("timeraddskip", [parameter("member", Member), parameter("count", int, 1)], "skipalias")
            ctx.clean_prefix = "?"
            message = ns["_command_usage_error"](ctx, parameter=ctx.command.clean_params["member"])
            self.assertIn("?skipalias @GM [count]", message)

    async def test_unknown_commands_stay_silent_and_unexpected_errors_propagate(self):
        for folder, ns in self.bots.items():
            ctx = context("test", [])
            await ns["on_command_error"](ctx, CommandNotFound())
            ctx.send.assert_not_awaited()
            with self.assertRaises(RuntimeError):
                await ns["on_command_error"](ctx, RuntimeError("unexpected"))

    async def test_timer_permissions_are_not_treated_as_input_errors(self):
        ctx = context("timersettimer", [])
        await self.bots["ATD Timer Bot"]["on_command_error"](ctx, MissingPermissions())
        self.assertIn("Commissioner", self.message(ctx, "ATD Timer Bot"))
        self.assertNotIn("Usage:", self.message(ctx, "ATD Timer Bot"))

    def test_usage_overrides_only_reference_registered_commands(self):
        for folder, ns in self.bots.items():
            tree = ast.parse((WORKSPACE / folder / "bot.py").read_text(encoding="utf-8"))
            registered = set()
            for node in tree.body:
                if not isinstance(node, ast.AsyncFunctionDef):
                    continue
                for decorator in node.decorator_list:
                    if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and decorator.func.attr == "command":
                        registered.add(next((k.value.value for k in decorator.keywords if k.arg == "name"), node.name))
            self.assertFalse(set(ns["_COMMAND_INPUT_EXAMPLES"]) - registered, folder)


if __name__ == "__main__":
    unittest.main()
