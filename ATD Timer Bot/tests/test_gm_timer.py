"""Exercise timer rules and the real command without Discord or credentials."""
import ast
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
# Load actual timer constants, omitting environment-dependent configuration.
config = types.ModuleType("config")
tree = ast.parse((ROOT / "config.py").read_text(encoding="utf-8"))
names = {"ROUNDS", "ROUND_TIMERS", "SKIP_PENALTY", "ROUNDLESS_TIMER",
         "AS_THRESHOLD", "STEPPED_SKIP_SCHEDULE", "STEPPED_AS_THRESHOLD"}
exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n, ast.Assign)
     and any(isinstance(t, ast.Name) and t.id in names for t in n.targets)], type_ignores=[]),
     "config.py", "exec"), config.__dict__)
spec = importlib.util.spec_from_file_location("timer_draft_under_test", ROOT / "draft.py")
draft = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"config": config}):
    spec.loader.exec_module(draft)


def state():
    ds = draft.DraftState()
    ds.teams = [{"name": "A", "user_ids": [11, 12], "skip_count": 0},
                {"name": "B", "user_ids": [22], "skip_count": 0}]
    ds.pick_order = [[0, 1]] * 10
    ds.current_round = 2
    ds.state = "active"
    return ds


class TimerRules(unittest.TestCase):
    def setUp(self):
        self.config_patch = patch.dict(sys.modules, {"config": config})
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.ds = state()

    def test_individual_precedes_global_without_affecting_other_team(self):
        self.ds.timer_override = 1800
        self.ds.teams[0]["timer_override"] = 3600
        self.assertEqual(self.ds.effective_timer(3, 0), 3600)
        self.assertEqual(self.ds.effective_timer(3, 1), 1800)
        del self.ds.teams[0]["timer_override"]
        self.assertEqual(self.ds.effective_timer(3, 0), 1800)

    def test_skip_penalties_and_as_are_team_specific(self):
        self.ds.teams[0]["timer_override"] = 3600
        for team in self.ds.teams:
            team["skip_count"] = 3
        self.assertEqual(self.ds.effective_timer(3, 0), 2700)
        self.assertEqual(self.ds.effective_timer(3, 1), 600)
        self.assertTrue(self.ds.is_active_skip(0))
        self.assertFalse(self.ds.is_active_skip(1))

    def test_existing_defaults_in_all_modes_and_rounds(self):
        for mode in ("snake", "snake+sbl", "roundless", "roundless+sbl"):
            self.ds.mode = mode
            for r in range(1, 11):
                for skips in range(5):
                    self.ds.teams[1]["skip_count"] = skips
                    stepped = mode.startswith("roundless") or 3 <= r <= 8
                    base = 2700 if mode.startswith("roundless") else config.ROUND_TIMERS[r]
                    expected = (config.STEPPED_SKIP_SCHEDULE.get(skips, 0)
                                if skips and stepped else max(0, base - skips * 300))
                    self.assertEqual(self.ds.effective_timer(r, 1), expected)

    def test_persists_and_old_state_loads(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(draft, "_state_dir", folder):
            self.ds.save(1)
            self.assertEqual(draft.DraftState.load(1).effective_timer(3, 0), 2700)
            self.ds.teams[0]["timer_override"] = 4200
            self.ds.save(1)
            self.assertEqual(draft.DraftState.load(1).effective_timer(3, 0), 4200)


class TimerCommand(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config_patch = patch.dict(sys.modules, {"config": config})
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.ds = state()
        self.ds.save = Mock()
        self.ctx = types.SimpleNamespace(send=AsyncMock())
        self.session = types.SimpleNamespace(draft=self.ds, channel_id=1,
                                            window_task=None, timer_task=None)
        self.member = types.SimpleNamespace(id=11, display_name="GM")
        self.convert = AsyncMock(return_value=self.member)
        self.start = AsyncMock()
        self.delete = AsyncMock()
        self.ns = {"_resolve_command_session": AsyncMock(return_value=self.session),
                   "commands": types.SimpleNamespace(MemberConverter=lambda: types.SimpleNamespace(convert=self.convert),
                                                     BadArgument=ValueError),
                   "_start_timer": self.start, "_delete_active_ping": self.delete}
        tree = ast.parse((ROOT / "bot.py").read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "timersettimer")
        node.decorator_list = []
        exec(compile(ast.Module(body=[node], type_ignores=[]), "bot.py", "exec"), self.ns)
        self.command = self.ns["timersettimer"]

    async def test_current_team_restarts_and_co_owner_targets_same_team(self):
        self.member.id = 12
        await self.command(self.ctx, "@GM", 60)
        self.assertEqual(self.ds.teams[0]["timer_override"], 3600)
        self.assertNotIn("timer_override", self.ds.teams[1])
        self.start.assert_awaited_once_with(self.session)
        self.ds.save.assert_called()

    async def test_other_team_clock_untouched(self):
        self.member.id = 22
        self.ds.timer_start = "existing clock"
        await self.command(self.ctx, "@GM", 60)
        self.assertEqual(self.ds.teams[1]["timer_override"], 3600)
        self.assertEqual(self.ds.timer_start, "existing clock")
        self.start.assert_not_awaited()
        self.delete.assert_not_awaited()

    async def test_manual_pause_kept_and_remaining_updated(self):
        self.ds.state = "paused"
        self.ds.teams[0]["skip_count"] = 1
        await self.command(self.ctx, "@GM", 60)
        self.assertEqual(self.ds.state, "paused")
        self.assertEqual(self.ds.paused_remaining, 3300)
        self.start.assert_not_awaited()

    async def test_closed_window_replaces_resume_task(self):
        self.ds.state = "window_paused"
        self.session.window_task = Mock()
        self.session.window_task.done.return_value = False
        await self.command(self.ctx, "@GM", 60)
        self.session.window_task.cancel.assert_called_once()
        self.start.assert_awaited_once()

    async def test_clear_falls_back_to_global(self):
        self.ds.state = "paused"
        self.ds.timer_override = 1200
        self.ds.teams[0]["timer_override"] = 3600
        await self.command(self.ctx, "@GM", 0)
        self.assertNotIn("timer_override", self.ds.teams[0])
        self.assertEqual(self.ds.paused_remaining, 1200)

    async def test_legacy_global_set_and_clear_preserve_individual(self):
        self.ds.teams[0]["timer_override"] = 3600
        await self.command(self.ctx, "30")
        self.assertEqual(self.ds.timer_override, 1800)
        await self.command(self.ctx, "0")
        self.assertIsNone(self.ds.timer_override)
        self.assertEqual(self.ds.teams[0]["timer_override"], 3600)
        self.start.assert_not_awaited()

    async def test_invalid_targets_and_negative_do_not_mutate(self):
        await self.command(self.ctx, "-1")
        await self.command(self.ctx, "@GM")
        self.member.id = 99
        await self.command(self.ctx, "@GM", 60)
        self.ds.save.assert_not_called()
        self.start.assert_not_awaited()

    async def test_complete_draft_unchanged(self):
        self.ds.state = "complete"
        await self.command(self.ctx, "@GM", 60)
        self.ds.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
