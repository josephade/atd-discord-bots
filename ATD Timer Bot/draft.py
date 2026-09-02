"""
draft.py — Draft state machine for ATD Timer Bot.
Handles snake order, lotto, pick recording, and LeBron/MJ end-of-round penalty.
Also supports roundless (money-based dynamic pick order) mode.
"""
import json
import os
import random
from datetime import datetime, timezone

from config import ROUNDS

_state_dir = os.environ.get("STATE_DIR", os.path.dirname(__file__))

# Per-channel state files
def state_file(channel_id: int) -> str:
    return os.path.join(_state_dir, f"draft_state_{channel_id}.json")

# Skip history is shared across all drafts (entries include channel_id)
HISTORY_FILE = os.path.join(_state_dir, "skip_history.json")

# ATD snake direction per round (0-indexed).
# True = reversed (picks N→1), False = forward (picks 1→N).
# Normal ATD (with flips on rounds 3 and 6):
_REVERSED_FLIPS    = [False, True, True, False, True, True, False, True, False, True]
# "snake+budget" mode's rule is "no 3rd/6th round flip" — standard alternating snake:
_REVERSED_NO_FLIPS = [False, True, False, True, False, True, False, True, False, True]


def build_snake_order(num_teams: int, penalty_teams: list[int] = None,
                       base_order: list[int] = None, start_round: int = 0,
                       existing_order: list[list[int]] = None, flips: bool = True) -> list[list[int]]:
    """
    Build the full pick order for all rounds.
    Returns a list of ROUNDS lists, each containing team indices in pick order.

    penalty_teams: indices of teams that drafted LeBron or MJ.
    From round 6 onward, those teams are moved to the END of each round.
    If multiple penalty teams exist, their relative snake order is preserved.

    base_order: the team-index sequence used as a forward round's order,
    before any snake reversal. Defaults to range(num_teams); pass a shuffled
    permutation to draw a fresh lottery (see reroll_from_round below).

    start_round / existing_order: build only rounds start_round..ROUNDS-1,
    prefixed with existing_order's rounds before start_round left untouched.
    Lets a mid-draft re-lottery leave already-drafted rounds exactly as-is.

    flips: True for the real ATD pattern (extra reversal on rounds 3 and 6);
    False for plain alternating snake — pass (mode != "snake+budget").
    """
    penalty_teams = penalty_teams or []
    base_order = base_order if base_order is not None else list(range(num_teams))
    order = list(existing_order[:start_round]) if existing_order else []
    reversed_pattern = _REVERSED_FLIPS if flips else _REVERSED_NO_FLIPS

    for r in range(start_round, ROUNDS):
        rev = reversed_pattern[r] if r < len(reversed_pattern) else (r % 2 == 1)
        seq = list(base_order)
        if rev:
            seq.reverse()

        if r >= 5 and penalty_teams:          # rounds 6-10 (0-indexed r = 5..9)
            normal  = [t for t in seq if t not in penalty_teams]
            # Penalty teams pick last, reverse draft order (later pick goes first)
            penalty = sorted(penalty_teams, reverse=True)
            seq = normal + penalty

        order.append(seq)

    return order


def reroll_from_round(pick_order: list[list[int]], num_teams: int, start_round: int,
                       penalty_teams: list[int] = None, flips: bool = True) -> list[list[int]]:
    """Draw a brand new lottery for rounds start_round..ROUNDS-1 (0-indexed),
    leaving every round before start_round exactly as already drafted. Used
    by !timerlottoreroll for themes like Rising Budget Flux, where pick
    order is re-drawn every two rounds.

    A plain uniform shuffle can land a team right back near where they just
    were, which defeats the point of a reroll — so the new draw is
    constrained to keep every team out of the third of the order (early /
    middle / late) they were picking in the round right before the reroll.
    That round is compared in its un-reversed, "1 to num_teams" form — a
    snake draft's even rounds are just the odd round mirrored, so comparing
    against the raw (possibly-reversed) round directly can send a team
    right back toward the *other* round's tier instead of away from both."""
    def _tier(pos: int) -> int:
        return min(pos * 3 // num_teams, 2)

    reversed_pattern = _REVERSED_FLIPS if flips else _REVERSED_NO_FLIPS

    if start_round > 0:
        prev_idx = start_round - 1
        prev_round = pick_order[prev_idx]
        was_reversed = reversed_pattern[prev_idx] if prev_idx < len(reversed_pattern) else (prev_idx % 2 == 1)
        if was_reversed:
            prev_round = list(reversed(prev_round))
    else:
        prev_round = list(range(num_teams))
    old_tier = {team: _tier(pos) for pos, team in enumerate(prev_round)}

    shuffled = list(range(num_teams))
    random.shuffle(shuffled)

    # Checking the constraint against `shuffled` itself wouldn't be enough —
    # odd rounds (and rounds 6+ with a penalty team) reorder it before it's
    # actually used, so the repair below checks/fixes against what the
    # first affected round will really look like. Every fix is then
    # translated back into a swap on `shuffled` itself (by team identity,
    # not position — works regardless of the reversal/penalty transform)
    # rather than editing that round's list directly: build_snake_order
    # derives every later round from this same `shuffled`, so patching a
    # derived round in isolation would silently desync it from the rounds
    # that come after — exactly the bug this used to have (Round 4 stayed
    # on the pre-repair draw while Round 3 alone got fixed, breaking the
    # snake mirror between them). Patching `shuffled` first and building
    # once at the end keeps every round consistent with every other.
    is_reversed_first = reversed_pattern[start_round] if start_round < len(reversed_pattern) else (start_round % 2 == 1)
    has_penalty_first = start_round >= 5 and bool(penalty_teams)

    def _first_round_view(base: list[int]) -> list[int]:
        seq = list(base)
        if is_reversed_first:
            seq.reverse()
        if has_penalty_first:
            normal  = [t for t in seq if t not in penalty_teams]
            penalty = sorted(penalty_teams, reverse=True)
            seq = normal + penalty
        return seq

    swappable = range(num_teams - len(penalty_teams)) if has_penalty_first else range(num_teams)
    for _ in range(num_teams * num_teams):
        first_view = _first_round_view(shuffled)
        violations = [i for i in swappable if _tier(i) == old_tier.get(first_view[i], -1)]
        if not violations:
            break
        i = violations[0]
        team_i = first_view[i]
        for j in swappable:
            if j == i:
                continue
            team_j = first_view[j]
            if _tier(j) != old_tier.get(team_i, -1) and _tier(i) != old_tier.get(team_j, -1):
                pi, pj = shuffled.index(team_i), shuffled.index(team_j)
                shuffled[pi], shuffled[pj] = shuffled[pj], shuffled[pi]
                break

    return build_snake_order(num_teams, penalty_teams, base_order=shuffled,
                              start_round=start_round, existing_order=pick_order, flips=flips)


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
        self.mode:               str = "snake"      # "snake" | "roundless" | "snake+sbl" | "roundless+sbl"
        self.timer_override:     int | None = None  # override all round timers (seconds); None = use config
        self.next_team_override: int | None = None  # force a specific team idx to be current for one pick
        self.budget_max:         int | None = None  # per-team total spending cap; None = no cap enforced

        # ── Steal / Block / Lock (SBL) state ────────────────────────────────
        self.total_picks_made:   int = 0            # monotonic counter; drives overall_pick when sbl_enabled
        self.pick_records:       dict = {}          # str(pick_number) -> record, see draft.py docstring below
        self.repick_queue:       list[tuple[int, int]] = []  # (team_idx, reopened_pick_num) owed an emergency repick, FIFO — front is current when sbl_enabled
        self.next_timer_override_secs: int | None = None  # force the next _start_timer duration (e.g. restoring a blocked GM's remaining time instead of a fresh clock); consumed once

    # ── Convenience properties ────────────────────────────────────────────────

    @property
    def num_teams(self) -> int:
        return len(self.teams)

    @property
    def order_mode(self) -> str:
        """Underlying pick-order algorithm, independent of the SBL ruleset: 'snake' | 'roundless'."""
        return self.mode.split("+")[0]

    @property
    def sbl_enabled(self) -> bool:
        """Whether the Steal/Block/Lock ruleset is layered on top of the order mode."""
        return self.mode.endswith("+sbl")

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
        if self.sbl_enabled and self.repick_queue:
            return self.repick_queue[0][0]
        if self.order_mode == "roundless":
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
        if self.sbl_enabled:
            if self.repick_queue:
                return self.repick_queue[0][1]  # reopened original pick number
            return self.total_picks_made + 1
        if self.order_mode == "roundless":
            return self.current_round + 1   # current_round doubles as pick counter
        return self.current_round * self.num_teams + self.current_in_round + 1

    @property
    def round_number(self) -> int:
        return self.current_round + 1

    @property
    def pick_in_round(self) -> int:
        return self.current_in_round + 1

    # ── Mutation ──────────────────────────────────────────────────────────────

    def advance(self, served_from_queue: bool = False):
        """Move to the next pick.  In roundless mode, just increments the counter.

        served_from_queue: True if the pick that just happened was served from
        repick_queue (an SBL emergency repick reusing the original voided
        pick's number) rather than the normal order. Callers must determine
        this themselves, by checking whether repick_queue[0] matched the
        picking team *before* they made any other repick_queue changes of
        their own (e.g. queuing a fresh victim) — by the time advance() runs,
        repick_queue may already contain unrelated entries added as part of
        this same action, so it can't be inferred here. Repicks reuse a pick
        number rather than consuming a new one, so total_picks_made and
        current_round/current_in_round are left untouched — nobody who was
        already waiting their turn loses or gains a slot, and the next fresh
        pick continues exactly where the numbering left off.
        """
        self.next_team_override = None  # always clear after a pick is made

        if served_from_queue:
            if self.repick_queue:
                self.repick_queue.pop(0)
        else:
            self.total_picks_made += 1
            if self.order_mode == "roundless":
                self.current_round += 1
            else:
                self.current_in_round += 1
                if self.current_in_round >= self.num_teams:
                    self.current_in_round = 0
                    self.current_round += 1
                if self.current_round >= ROUNDS:
                    self.state = "complete"

    def apply_penalty(self, team_idx: int):
        """Register a LeBron/MJ team and rebuild pick order from round 6 onward.
        No-op in "snake+budget" mode — that theme has no pick-last penalty."""
        if self.mode == "snake+budget":
            return
        if team_idx not in self.penalty_teams:
            self.penalty_teams.append(team_idx)
        self.pick_order = build_snake_order(self.num_teams, self.penalty_teams, flips=(self.mode != "snake+budget"))

    def _uses_stepped_skip_schedule(self, round_num: int) -> bool:
        """R3-8 and roundless mode (both 45 min base) use a fixed step-down
        schedule per skip instead of a flat -5 min/skip deduction. A
        timer_override (fixed custom timer) always uses the flat formula."""
        if self.timer_override is not None:
            return False
        return self.order_mode == "roundless" or 3 <= round_num <= 8

    def active_skip_threshold(self, round_num: int | None = None) -> int:
        """Skip count at which a team is skipped instantly with no timer.
        Higher for the stepped schedule (35/20/10 min steps before AS)."""
        from config import AS_THRESHOLD, STEPPED_AS_THRESHOLD
        if self._uses_stepped_skip_schedule(round_num if round_num is not None else self.round_number):
            return STEPPED_AS_THRESHOLD
        return AS_THRESHOLD

    def effective_timer(self, round_num: int, team_idx: int) -> int:
        """Base timer for this pick minus accumulated skip penalties (min 0).
        Applies even when timer_override (fixed timer mode) is set — a fixed
        base duration doesn't mean skips go unpenalized, just that the base
        itself is a flat value instead of round/mode-dependent."""
        from config import SKIP_PENALTY, STEPPED_SKIP_SCHEDULE
        skip_count = self.teams[team_idx].get("skip_count", 0)

        if self.timer_override is not None:
            base = self.timer_override
        elif self.order_mode == "roundless":
            from config import ROUNDLESS_TIMER
            base = ROUNDLESS_TIMER
        else:
            from config import ROUND_TIMERS
            base = ROUND_TIMERS.get(round_num, 1800)

        if skip_count > 0 and self._uses_stepped_skip_schedule(round_num):
            return STEPPED_SKIP_SCHEDULE.get(skip_count, 0)
        return max(base - skip_count * SKIP_PENALTY, 0)

    def is_active_skip(self, team_idx: int) -> bool:
        """Returns True if this team has hit the AS threshold and should be skipped instantly."""
        return self.teams[team_idx].get("skip_count", 0) >= self.active_skip_threshold()

    # ── Steal / Block / Lock ──────────────────────────────────────────────────
    #
    # pick_records maps str(pick_number) -> {
    #     "player_name": str,        # display name
    #     "name_key":     str,       # normalized key for matching
    #     "team_idx":     int,       # CURRENT owner
    #     "locked":       bool,      # GM spent a lock charge on this pick — permanently immune
    #     "protected":    bool,      # this is a repick made after being hit once — permanently immune
    #     "is_steal_result": bool,   # current owner obtained this via a steal (for refund-on-block)
    #     "stolen_from_team_idx": int | None,  # who it was stolen from (lets them block-reclaim it)
    #     "price":        int | None,
    # }
    #
    # Note: a pick that gets stolen (not blocked) is NOT itself immune afterward —
    # it can be re-stolen or blocked later (see rule: "if your steal is stolen you
    # do not get it back"). Only a *repick made after being hit* is permanently safe.

    def sbl_window(self, pick_number: int) -> int:
        """A 'round' for steal/block eligibility = one pick per team, same as a
        real draft round — everyone has picked once before the window closes.
        This applies even in roundless mode, independent of the dynamic order."""
        teams = self.num_teams or 1
        return (pick_number - 1) // teams

    def sbl_eligible(self, pick_number: int) -> bool:
        """Whether a pick is still within the active steal/block window."""
        return self.sbl_window(pick_number) == self.sbl_window(self.overall_pick)

    def register_pick_record(self, pick_number: int, player_name: str, name_key: str,
                              team_idx: int, price: int | None = None, protected: bool = False,
                              remaining_at_pick: int | None = None):
        self.pick_records[str(pick_number)] = {
            "player_name": player_name,
            "name_key": name_key,
            "team_idx": team_idx,
            "locked": False,
            "protected": protected,
            "is_steal_result": False,
            "stolen_from_team_idx": None,  # set on the record if it's later stolen
            "price": price,
            "remaining_at_pick": remaining_at_pick,  # seconds left on their clock when they made this pick — restored (not a fresh timer) if this pick later gets blocked
        }

    def queue_repick(self, team_idx: int, reopened_pick_num: int):
        """Queue team_idx for an emergency repick that reuses reopened_pick_num
        (the number of the pick that just got blocked/stolen). Inserted at the
        FRONT, not appended — the most recently-created obligation takes
        priority and gets pinged immediately, ahead of whatever was already
        queued (e.g. a block that reverses a steal shouldn't sit behind the
        stealer's own still-pending original pick)."""
        self.repick_queue.insert(0, (team_idx, reopened_pick_num))

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self, channel_id: int):
        with open(state_file(channel_id), "w") as f:
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
                "budget_max":         self.budget_max,
                "total_picks_made":  self.total_picks_made,
                "pick_records":      self.pick_records,
                "repick_queue":      self.repick_queue,
                "next_timer_override_secs": self.next_timer_override_secs,
            }, f, indent=2)

    @classmethod
    def load(cls, channel_id: int) -> "DraftState":
        path = state_file(channel_id)
        if not os.path.exists(path):
            return cls()
        with open(path) as f:
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
        ds.budget_max         = d.get("budget_max")
        ds.total_picks_made   = d.get("total_picks_made", 0)
        ds.pick_records       = d.get("pick_records", {})
        ds.repick_queue       = [tuple(x) for x in d.get("repick_queue", [])]
        ds.next_timer_override_secs = d.get("next_timer_override_secs")
        return ds
