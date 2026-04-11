"""
draft.py — Draft state machine for ATD Timer Bot.
Handles snake order, lotto, pick recording, and LeBron/MJ end-of-round penalty.
"""
import json
import os
import random
from datetime import datetime, timezone

from config import ROUNDS

_state_dir = os.environ.get("STATE_DIR", os.path.dirname(__file__))
STATE_FILE = os.path.join(_state_dir, "draft_state.json")

# ATD snake direction per round (0-indexed).
# True = reversed (picks N→1), False = forward (picks 1→N).
# Rounds 3 and 6 are "flips" — same direction as the previous round.
_REVERSED = [False, True, True, False, True, True, False, True, False, True]


def build_snake_order(num_teams: int, penalty_teams: list[int] = None) -> list[list[int]]:
    """
    Build the full pick order for all rounds.
    Returns a list of ROUNDS lists, each containing team indices in pick order.

    penalty_teams: indices of teams that drafted LeBron or MJ.
    From round 6 onward, those teams are moved to the END of each round.
    If multiple penalty teams exist, their relative snake order is preserved.
    """
    penalty_teams = penalty_teams or []
    order = []

    for r in range(ROUNDS):
        rev = _REVERSED[r] if r < len(_REVERSED) else (r % 2 == 1)
        seq = list(range(num_teams))
        if rev:
            seq.reverse()

        if r >= 5 and penalty_teams:          # rounds 6-10 (0-indexed r = 5..9)
            normal  = [t for t in seq if t not in penalty_teams]
            penalty = [t for t in seq if t in penalty_teams]
            seq = normal + penalty            # penalty teams pick last, preserving snake order

        order.append(seq)

    return order


class DraftState:
    def __init__(self):
        self.teams:              list[dict] = []   # {user_ids: list[int], name, picks, skip_count}
        self.pick_order:         list[list[int]] = []
        self.current_round:      int = 0           # 0-indexed
        self.current_in_round:   int = 0           # 0-indexed within round
        self.penalty_teams:      list[int] = []    # team indices (LeBron / MJ owners)
        self.timer_start:        str | None = None # ISO-8601 UTC
        self.paused_remaining:   int | None = None # seconds left when paused
        self.state:              str = "idle"      # idle | setup | lotto | active | complete | paused

    # ── Convenience properties ────────────────────────────────────────────────

    @property
    def num_teams(self) -> int:
        return len(self.teams)

    @property
    def current_team_idx(self) -> int | None:
        if (not self.pick_order
                or self.current_round >= ROUNDS
                or self.current_round >= len(self.pick_order)):
            return None
        return self.pick_order[self.current_round][self.current_in_round]

    @property
    def current_team(self) -> dict | None:
        idx = self.current_team_idx
        return self.teams[idx] if idx is not None else None

    @property
    def overall_pick(self) -> int:
        return self.current_round * self.num_teams + self.current_in_round + 1

    @property
    def round_number(self) -> int:
        return self.current_round + 1

    @property
    def pick_in_round(self) -> int:
        return self.current_in_round + 1

    # ── Mutation ──────────────────────────────────────────────────────────────

    def advance(self):
        """Move to the next pick; set state = 'complete' when done."""
        self.current_in_round += 1
        if self.current_in_round >= self.num_teams:
            self.current_in_round = 0
            self.current_round += 1
        if self.current_round >= ROUNDS:
            self.state = "complete"

    def apply_penalty(self, team_idx: int):
        """Register a LeBron/MJ team and rebuild pick order from round 6 onward."""
        if team_idx not in self.penalty_teams:
            self.penalty_teams.append(team_idx)
        self.pick_order = build_snake_order(self.num_teams, self.penalty_teams)

    def effective_timer(self, round_num: int, team_idx: int) -> int:
        """Base timer for this round minus accumulated skip penalties. Can reach 0."""
        from config import ROUND_TIMERS, SKIP_PENALTY
        base       = ROUND_TIMERS.get(round_num, 1800)
        deductions = self.teams[team_idx].get("skip_count", 0) * SKIP_PENALTY
        return max(base - deductions, 0)

    def is_active_skip(self, team_idx: int) -> bool:
        """Returns True if this team has hit the AS threshold and should be skipped instantly."""
        from config import AS_THRESHOLD
        return self.teams[team_idx].get("skip_count", 0) >= AS_THRESHOLD

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self):
        with open(STATE_FILE, "w") as f:
            json.dump({
                "teams":           self.teams,
                "pick_order":      self.pick_order,
                "current_round":   self.current_round,
                "current_in_round": self.current_in_round,
                "penalty_teams":   self.penalty_teams,
                "timer_start":       self.timer_start,
                "paused_remaining":  self.paused_remaining,
                "state":             self.state,
            }, f, indent=2)

    @classmethod
    def load(cls) -> "DraftState":
        if not os.path.exists(STATE_FILE):
            return cls()
        with open(STATE_FILE) as f:
            d = json.load(f)
        ds = cls()
        ds.teams            = d.get("teams", [])
        ds.pick_order       = d.get("pick_order", [])
        ds.current_round    = d.get("current_round", 0)
        ds.current_in_round = d.get("current_in_round", 0)
        ds.penalty_teams    = d.get("penalty_teams", [])
        ds.timer_start       = d.get("timer_start")
        ds.paused_remaining  = d.get("paused_remaining")
        ds.state             = d.get("state", "idle")
        return ds
