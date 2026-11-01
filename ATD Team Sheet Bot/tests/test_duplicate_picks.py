"""Exercise the real sheet write path against an in-memory worksheet."""
import ast
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from functools import wraps
import io
from contextlib import redirect_stdout
from pathlib import Path
import re
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
import unittest

from draft_history import player_key


def a1(row, col):
    return f"{chr(64 + col)}{row}"


def manager_class():
    tree = ast.parse((Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SheetManager"
             or isinstance(n, ast.FunctionDef) and n.name == "_serialized_sheet_write"]
    ns = dict(threading=threading, wraps=wraps, re=re, time=time, player_key=player_key,
              _sheet_write_locks={}, _sheet_write_locks_guard=threading.Lock(),
              SPREADSHEET_ID="draft", _load_undo=lambda: {},
              _persist_undo=lambda *args: None,
              EMOJI_TEAM_MAP={"OSU": "Oklahoma State Cowboys", "Other": "Original Team"},
              PLAYER_POSITIONS={"Paul Westphal": "SG", "Michael Porter Jr.": "SF", "New Player": "PG"},
              POSITION_OFFSETS={"PG": (1, 6), "SG": (2, 7), "SF": (3, 8), "PF": (4, 9), "C": (5, 10)},
              gspread=SimpleNamespace(utils=SimpleNamespace(rowcol_to_a1=a1)))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "bot.py", "exec"), ns)
    return ns["SheetManager"]


class Worksheet:
    id = 123
    title = "Current Draft"

    def __init__(self, player="Paul Westphal"):
        self.rows = [["Original Team", "Year", "Oklahoma State Cowboys", "Year"]]
        self.rows += [["", "", "", ""] for _ in range(10)]
        if player:
            self.rows[2][:2] = [player, "1978-79"]
        self.writes = []

    def get_all_values(self):
        snapshot = deepcopy(self.rows)
        # Expose overlapping reads if the lock is removed.
        time.sleep(0.03)
        return snapshot

    def batch_update(self, updates):
        self.writes.append(deepcopy(updates))
        for update in updates:
            col, row = re.fullmatch(r"([A-Z])(\d+)", update["range"]).groups()
            self.rows[int(row) - 1][ord(col) - 65] = update["values"][0][0]


class SkipNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_mapped_tab_reports_trusted_pick_instead_of_silent_drop(self):
        tree = ast.parse((Path(__file__).resolve().parents[1]/'bot.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'on_message')
        node.decorator_list = []
        ns = dict(re=re, TIMER_BOT_ID=0, FLUX_BOT_ID=0, TRUSTED_BOT_IDS={99},
                  _get_manager_async=AsyncMock(return_value=None),
                  _channel_sheet_map={123: dict(tab='Old renamed tab')}, _SILENT_PICK_ERROR_CHANNELS=set())
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'bot.py', 'exec'), ns)
        message = SimpleNamespace(author=SimpleNamespace(id=99, bot=True),
                                  content="1. <:Cavs:123> LeBron James '13 $43",
                                  channel=SimpleNamespace(id=123, send=AsyncMock()), add_reaction=AsyncMock())
        await ns['on_message'](message)
        self.assertIn('not written', message.channel.send.call_args.args[0])
        self.assertIn('Old renamed tab', message.channel.send.call_args.args[0])
        self.assertIn('!setsheet', message.channel.send.call_args.args[0])
        message.add_reaction.assert_awaited_once_with('⚠️')
    async def test_numbered_skip_from_trusted_theme_bot_never_reaches_sheet_writer(self):
        tree = ast.parse((Path(__file__).resolve().parents[1]/'bot.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'on_message')
        node.decorator_list = []
        manager = AsyncMock()
        ns = dict(re=re, TIMER_BOT_ID=0, FLUX_BOT_ID=0, TRUSTED_BOT_IDS={99}, _get_manager_async=manager)
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'bot.py', 'exec'), ns)
        message = SimpleNamespace(author=SimpleNamespace(id=99, bot=True), content='1. <:Cavs:123> skipped')
        await ns['on_message'](message)
        manager.assert_not_awaited()


class DuplicatePicks(unittest.TestCase):
    def test_changed_budget_layout_overrides_old_plain_sheet_cache_and_writes_price(self):
        self.manager._price_col_cache = False
        self.ws.rows = [['Oklahoma State Cowboys', 'Year', '$0', 'Original Team', 'Year', '$0']]+[['']*6 for _ in range(10)]
        ok, _ = self.manager.add_player('Oklahoma State Cowboys', 'New Player', year='2022-23', price='$44')
        self.assertTrue(ok)
        self.assertEqual(self.ws.rows[1][2], 44)

    def test_plain_layout_does_not_reuse_old_budget_cache(self):
        self.manager._price_col_cache = True
        self.assertFalse(self.manager._tab_has_price_column([['Oklahoma State Cowboys', 'Year', 'Original Team', 'Year']]))

    def test_fractional_price_is_written_without_truncation(self):
        self.ws.rows = [['Oklahoma State Cowboys', 'Year', 'Price', 'Original Team', 'Year', 'Price']]+[['']*6 for _ in range(10)]
        ok, _ = self.manager.add_player('Oklahoma State Cowboys', 'New Player', price='$27.50')
        self.assertTrue(ok)
        self.assertEqual(self.ws.rows[1][2], 27.5)

    def setUp(self):
        self.output = redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        self.Manager = manager_class()
        self.ws = Worksheet()
        self.manager = self.Manager(self.ws)

    def test_paul_westphal_without_year_is_rejected_without_any_writes(self):
        ok, message = self.manager.add_player("Oklahoma State Cowboys", "Paul Westphal")
        self.assertFalse(ok)
        self.assertIn("already on **Original Team**", message)
        self.assertEqual(self.ws.writes, [])
        self.assertEqual(self.manager._undo_stack, [])
        self.assertEqual(self.ws.rows[2][0], "Paul Westphal")

    def test_year_price_and_position_do_not_bypass_duplicate_check(self):
        ok, _ = self.manager.add_player("Oklahoma State Cowboys", "Paul Westphal",
            year="1980-81", price="$5", position_override="PG", bench_only=True)
        self.assertFalse(ok)
        self.assertEqual(self.ws.writes, [])

    def test_repeated_pick_on_same_team_is_rejected(self):
        ok, _ = self.manager.add_player("Original Team", "Paul Westphal")
        self.assertFalse(ok)
        self.assertEqual(self.ws.writes, [])

    def test_case_spacing_and_punctuation_are_normalized(self):
        self.ws.rows[2][0] = "Michael Porter Jr."
        ok, _ = self.manager.add_player("Oklahoma State Cowboys", " michael   porter jr ", position_override="SF")
        self.assertFalse(ok)
        self.assertEqual(self.ws.writes, [])

    def test_valid_pick_writes_and_repeated_delivery_does_not_write_again(self):
        ok, _ = self.manager.add_player("Oklahoma State Cowboys", "New Player", year="2022-23")
        self.assertTrue(ok)
        self.assertEqual(self.ws.rows[1][2:], ["New Player", "2022-23"])
        ok, _ = self.manager.add_player("Oklahoma State Cowboys", "New Player", year="2022-23")
        self.assertFalse(ok)
        self.assertEqual(len(self.ws.writes), 1)

    def test_simultaneous_picks_across_channel_managers_only_write_once(self):
        other = self.Manager(self.ws, channel_id=2)
        self.assertIs(self.manager._write_lock, other._write_lock)
        ready = threading.Barrier(2)

        def pick(manager, team):
            ready.wait(timeout=2)
            return manager.add_player(team, "New Player")[0]

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(pick, self.manager, "Original Team"),
                       pool.submit(pick, other, "Oklahoma State Cowboys")]
            results = [future.result(timeout=5) for future in futures]
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(self.ws.writes), 1)

    def test_separate_drafts_can_pick_same_player(self):
        other = self.Manager(Worksheet(player=None), spreadsheet_id="different draft")
        self.assertIsNot(other._write_lock, self.manager._write_lock)
        self.assertTrue(other.add_player("Oklahoma State Cowboys", "Paul Westphal")[0])

    def test_confirmed_steal_can_move_player_without_duplicate_copy(self):
        self.assertTrue(self.manager.steal_player("Paul Westphal", "Oklahoma State Cowboys")[0])
        self.assertEqual(self.ws.rows[2][:2], ["", ""])
        self.assertEqual(self.ws.rows[2][2:], ["Paul Westphal", "1978-79"])


if __name__ == "__main__":
    unittest.main()
