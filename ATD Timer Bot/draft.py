"""
draft.py — Draft state machine for ATD Timer Bot.
Handles snake order, lotto, pick recording, and LeBron/MJ end-of-round penalty.
Also supports roundless (money-based dynamic pick order) mode.
"""
import json
import os
from datetime import datetime, timezone

from config import ROUNDS

_state_dir = os.environ.get("STATE_DIR", os.path.dirname(__file__))
STATE_FILE   = os.path.join(_state_dir, "draft_state.json")
HISTORY_FILE = os.path.join(_state_dir, "skip_history.json")

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
        self.teams:              list[dict] = []   # {user_ids, name, picks, skip_count, money_spent, last_pick_number}
        self.pick_order:         list[list[int]] = []
        self.current_round:      int = 0           # 0-indexed round (snake) or overall pick counter (roundless)
        self.current_in_round:   int = 0           # 0-indexed within round (snake only; always 0 in roundless)
        self.penalty_teams:      list[int] = []    # team indices (LeBron / MJ owners)
        self.timer_start:        str | None = None # ISO-8601 UTC
        self.paused_remaining:   int | None = None # seconds left when paused
        self.state:              str = "idle"      # idle | setup | lotto | active | complete | paused | window_paused
        self.draft_label:        str | None = None  # e.g. "ATD 101"
        self.draft_started:      str | None = None  # ISO-8601 UTC when !timerstart ran
        self.last_skip:          dict | None = None # undo state
        self.mode:               str = "snake"      # "snake" | "roundless"
        self.timer_override:     int | None = None  # override all round timers (seconds); None = use config
        self.next_team_override: int | None = None  # force a specific team idx to be current for one pick

    # ── Convenience properties ────────────────────────────────────────────────

    @property
    def num_teams(self) -> int:
        return len(self.teams)

    PICKS_TO_COMPLETE = 10  # teams with this many picks are done and excluded from the queue

    def _roundless_sorted_order(self) -> list[int]:
        """Return team indices sorted by roundless pick order.

        Teams with 10+ picks are complete and excluded entirely.
        Teams with pending_makeup=True sort last regardless of stats.
        For everyone else, tiebreaker priority:
          1. money_spent ASC   (less spent → picks sooner)
          2. picks_made ASC    (fewer picks → picks sooner)
          3. last_pick_number ASC  (earlier last pick → more time has passed → picks sooner)
          4. lotto slot ASC    (lotto position as final tiebreaker)
        """
        def key(idx):
            t = self.teams[idx]
            pending = 1 if t.get("pending_makeup") else 0
            return (
                pending,
                t.get("money_spent", 0),
                len(t.get("picks", [])),
                t.get("last_pick_number", 0),
                idx,
            )
        active = [i for i in range(self.num_teams)
                  if len(self.teams[i].get("picks", [])) < self.PICKS_TO_COMPLETE]
        return sorted(active, key=key)

    @property
    def current_team_idx(self) -> int | None:
        if self.next_team_override is not None:
            return self.next_team_override
        if self.mode == "roundless":
            order = self._roundless_sorted_order()
            return order[0] if order else None
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
        if self.mode == "roundless":
            return self.current_round + 1   # current_round doubles as pick counter
        return self.current_round * self.num_teams + self.current_in_round + 1

    @property
    def round_number(self) -> int:
        return self.current_round + 1

    @property
    def pick_in_round(self) -> int:
        return self.current_in_round + 1

    # ── Mutation ──────────────────────────────────────────────────────────────

    def advance(self):
        """Move to the next pick.  In roundless mode, just increments the counter."""
        self.next_team_override = None  # always clear after a pick is made
        if self.mode == "roundless":
            self.current_round += 1
            return
        # Snake mode
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
        """Base timer for this pick minus accumulated skip penalties (min 0)."""
        from config import SKIP_PENALTY
        if self.timer_override is not None:
            base = self.timer_override
        elif self.mode == "roundless":
            from config import ROUNDLESS_TIMER
            base = ROUNDLESS_TIMER
        else:
            from config import ROUND_TIMERS
            base = ROUND_TIMERS.get(round_num, 1800)
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
                "teams":            self.teams,
                "pick_order":       self.pick_order,
                "current_round":    self.current_round,
                "current_in_round": self.current_in_round,
                "penalty_teams":    self.penalty_teams,
                "timer_start":      self.timer_start,
                "paused_remaining": self.paused_remaining,
                "state":            self.state,
                "draft_label":      self.draft_label,
                "draft_started":    self.draft_started,
                "last_skip":        self.last_skip,
                "mode":             self.mode,
                "timer_override":     self.timer_override,
                "next_team_override": self.next_team_override,
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
        ds.timer_start      = d.get("timer_start")
        ds.paused_remaining = d.get("paused_remaining")
        ds.state            = d.get("state", "idle")
        ds.draft_label      = d.get("draft_label")
        ds.draft_started    = d.get("draft_started")
        ds.last_skip        = d.get("last_skip")
        ds.mode             = d.get("mode", "snake")
        ds.timer_override     = d.get("timer_override")
        ds.next_team_override = d.get("next_team_override")
        return ds
