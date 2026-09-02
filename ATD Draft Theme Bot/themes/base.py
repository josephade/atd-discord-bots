# themes/base.py
# The plugin interface every draft format implements. bot.py never contains
# theme-specific rules — it only routes DM/admin commands to whichever
# Theme is attached to the drafter's active draft. Adding a new format means
# writing a new themes/<name>.py and registering it in themes/__init__.py.

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

import discord

import db
import sheets
from config import POOL_SPREADSHEET_ID, POOL_TAB_NAME, ROSTER_SPREADSHEET_ID

log = logging.getLogger(__name__)

# One lock per draft_id, held for the duration of any state-mutating theme
# call (on_pick/on_list/on_message/on_timeout). Discord DMs from different
# drafters can arrive and be handled "simultaneously" by the event loop;
# without this, two concurrent mutations of the same draft's state could
# race and silently drop one of them (lost-update). Cheap to hold since
# these calls are per-drafter, not per-draft-wide-fanout.
_draft_locks: dict[int, asyncio.Lock] = {}


def draft_lock(draft_id: int) -> asyncio.Lock:
    if draft_id not in _draft_locks:
        _draft_locks[draft_id] = asyncio.Lock()
    return _draft_locks[draft_id]


@dataclass
class DraftContext:
    """Bundles everything a theme needs to act on one draft. Cheap to
    construct — draft/settings/state are re-read from the DB each time so
    themes never operate on stale data."""
    bot: discord.Client
    draft_id: int
    # The Discord message that triggered this call (e.g. a DM'd pick), if
    # any — bot.py fills this in for DM commands. Lets a theme reply
    # directly to that message (e.g. a makeup-pick confirmation) instead of
    # just sending a fresh DM with no visual link back to what prompted it.
    message: discord.Message | None = None

    @property
    def draft(self) -> dict:
        return db.get_draft(self.draft_id)

    @property
    def settings(self) -> dict:
        return self.draft['settings']

    @property
    def state(self) -> dict:
        return self.draft['state']

    def save_state(self, state: dict):
        db.set_draft_state(self.draft_id, state)

    def drafters(self) -> list[dict]:
        return db.list_drafters(self.draft_id)

    async def dm(self, user_id: int, content: str | None = None, embed: discord.Embed | None = None) -> bool:
        """Returns whether the DM actually sent — callers that gate other
        state on delivery (e.g. starting a pick timer) need to know it
        failed instead of silently assuming success."""
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await user.send(content=content, embed=embed)
            return True
        except discord.Forbidden:
            log.warning("DraftContext.dm | user %s has DMs closed", user_id)
            return False
        except Exception:
            log.exception("DraftContext.dm | failed to DM user %s", user_id)
            return False

    async def announce(self, content: str | None = None, embed: discord.Embed | None = None) -> discord.Message | None:
        channel_id = self.draft.get('admin_channel_id')
        if not channel_id:
            return None
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return None
        return await channel.send(content=content, embed=embed)

    async def announce_reply_to(self, message_id: int | None, content: str | None = None,
                                 embed: discord.Embed | None = None) -> discord.Message | None:
        """Like announce(), but posted as a genuine Discord reply to an
        earlier admin-channel message (e.g. a makeup pick replying to the
        skip notice it resolves) — falls back to a plain announce() if
        message_id is missing or that message can no longer be fetched."""
        channel_id = self.draft.get('admin_channel_id')
        if not channel_id:
            return None
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return None
        if message_id:
            try:
                target = await channel.fetch_message(message_id)
                return await target.reply(content=content, embed=embed)
            except Exception:
                log.warning("DraftContext.announce_reply_to | couldn't fetch/reply to %s, falling back", message_id)
        return await channel.send(content=content, embed=embed)

    async def reply_to_trigger(self, content: str | None = None, embed: discord.Embed | None = None) -> bool:
        """Reply directly to self.message, if one was provided. Returns True
        if a reply was actually sent, so callers know whether they still
        need to send something themselves (e.g. via .dm())."""
        if self.message is None:
            return False
        try:
            await self.message.reply(content=content, embed=embed)
            return True
        except Exception:
            log.exception("DraftContext.reply_to_trigger | failed to reply")
            return False


class Theme(ABC):
    key: str = ''
    display_name: str = ''

    @abstractmethod
    def default_settings(self) -> dict:
        """Baseline settings merged under whatever the commissioner overrides
        with `!theme settings`."""

    @abstractmethod
    async def start(self, ctx: DraftContext) -> str:
        """Build initial state and DM opening prompts to every drafter.
        Returns a short admin-facing summary."""

    async def on_pick(self, ctx: DraftContext, drafter: dict, text: str) -> str:
        return "This draft format doesn't use `!pick` — check `!help`."

    async def on_list(self, ctx: DraftContext, drafter: dict, text: str) -> str:
        return "This draft format doesn't use `!list` — check `!help`."

    async def on_message(self, ctx: DraftContext, drafter: dict, text: str) -> str:
        return "This draft format doesn't support partner messages."

    @abstractmethod
    async def on_timeout(self, ctx: DraftContext, drafter: dict) -> None:
        """Invoked by the timer when a drafter's clock expires uncancelled —
        theme decides what an auto-pick/auto-resolve looks like."""

    @abstractmethod
    async def on_status(self, ctx: DraftContext, drafter: dict) -> str:
        """DM-facing text for `!status` — what this drafter is waiting on."""

    @abstractmethod
    def is_pick_phase_complete(self, ctx: DraftContext) -> bool:
        """True once every drafter has made all their picks (roster
        submission can still be pending)."""

    async def format_picks(self, ctx: DraftContext, drafter: dict, picks: list[str]) -> str:
        """DM-facing text for `!mypicks`. Default: a flat numbered list in
        draft order — override for formats where a structured view (e.g.
        sorted by price) is more useful."""
        if not picks:
            return "You haven't drafted anyone yet."
        return '\n'.join(f'{i + 1}. {p}' for i, p in enumerate(picks))

    async def format_team(self, ctx: DraftContext, drafter: dict, picks: list[str]) -> str:
        """DM-facing text for `!myteam` — the drafter's current roster/lineup
        (as opposed to `!mypicks`, everything they've drafted). Default:
        same as format_picks, for formats with no separate roster-slot
        concept to show."""
        return await self.format_picks(ctx, drafter, picks)

    async def add_player(self, ctx: DraftContext, drafter: dict, player: str) -> str:
        """`!add <player>` — place a drafted player into an open roster slot
        automatically. Default: not supported (formats with no roster-slot
        concept have nothing for this to do)."""
        return "This draft format doesn't support adding a player to a slot — check `!help`."

    # ── Team/roster grouping — defaults suit "1 drafter = 1 team" formats;
    # override for formats like Anonymous Partner Draft where 2 drafters
    # share one team and one roster. ─────────────────────────────────────────
    def team_key(self, ctx: DraftContext, drafter: dict) -> str:
        return str(drafter['id'])

    def team_label(self, ctx: DraftContext, drafter: dict) -> str:
        return drafter['display_name'] or str(drafter['user_id'])

    def linked_drafter_ids(self, ctx: DraftContext, drafter: dict) -> list[int]:
        """Drafter IDs that share drafter's team — a roster submission from
        any one of them applies to all of them."""
        return [drafter['id']]

    def roster_requirements(self, ctx: DraftContext) -> tuple[dict | None, float | None]:
        """(price_lookup, budget) used by `!submitroster`'s budget check.
        Return (None, None) to skip the budget check entirely."""
        return None, None

    def best_affordable_roster(self, ctx: DraftContext, drafter: dict) -> list[str] | None:
        """Best-effort fallback used by `!theme rostertimer`'s auto-submit
        when a GM's own arrangement doesn't validate (e.g. over budget) —
        build the highest-value legal roster from their picks that still
        fits. Default: not supported (no fallback beyond what's already
        arranged)."""
        return None

    def validate_final_roster(self, ctx: DraftContext, drafter: dict, submitted: list[str]) -> tuple[bool, list[str]]:
        """Extra format-specific validation on top of the generic
        drafted-by/budget checks `!submitroster` always runs — e.g.
        positional slot legality. Default: no extra rules."""
        return True, []

    def final_roster_rows(self, ctx: DraftContext, drafter: dict) -> list:
        """Rows written under this drafter's team column at `!theme reveal`.
        Default: a flat list of player names (one sheet column). Override
        to return a list of (player, year, price) tuples instead for a
        Player/Year/Price 3-column block per team — see write_draft_results."""
        return list(drafter['state'].get('final_roster', []))

    async def on_roster_submitted(self, ctx: DraftContext, drafter: dict) -> None:
        """Called by `!submitroster` right after a roster is accepted, in
        addition to bot.py's own generic admin-channel notice — themes can
        use this to post a progress update somewhere more specific (e.g.
        Pack Draft's per-division spectator thread). Default: nothing extra."""
        return

    # ── Roster editing — DM commands `!swap`/`!addyear`, and `!submitroster`
    # run with no arguments. Default: not supported (the drafter must type
    # their final list out explicitly every time). ─────────────────────────
    async def swap_slots(self, ctx: DraftContext, drafter: dict, arg_a: str, arg_b: str) -> str:
        return "This draft format doesn't support roster editing."

    async def set_player_year(self, ctx: DraftContext, drafter: dict, player: str, year: str) -> str:
        return "This draft format doesn't support setting a player's year."

    def current_roster_list(self, ctx: DraftContext, drafter: dict) -> list[str] | None:
        """The drafter's current fully-arranged roster, in slot order, if
        one exists and every slot is filled — used by `!submitroster` with
        no arguments. Return None to fall back to requiring an explicit list."""
        return None

    # ── Optional admin ops some formats need (divisions, manual pack
    # upload, moderator overrides, etc.) — bot.py exposes these as
    # top-level commands; formats that don't need them just inherit the
    # "not supported" default rather than every format having to implement
    # every hook. ─────────────────────────────────────────────────────────
    async def import_draft_order(self, ctx: DraftContext, division: str, team_lines: list[dict]) -> str:
        """team_lines: lotto.parse_team_lines()'s output — one entry per
        seat, in draft-order, each {'order_index', 'logo', 'user_ids'}."""
        return "This draft format doesn't use imported draft order."

    async def generate_packs(self, ctx: DraftContext, division: str) -> str:
        return "This draft format doesn't generate packs."

    async def load_packs(self, ctx: DraftContext, division: str, text: str) -> str:
        return "This draft format doesn't support manually loaded packs."

    async def force_skip(self, ctx: DraftContext, drafter: dict) -> str:
        return "This draft format doesn't support forcing a skip."

    async def resend_current(self, ctx: DraftContext, drafter: dict) -> str:
        return "Nothing to resend for this draft format — check `!status`."

    async def admin_status(self, ctx: DraftContext) -> str:
        """Richer, moderator-facing status than the generic `!theme status`
        (e.g. per-division pack/queue/timer detail, without leaking contents)."""
        return "This draft format doesn't have a richer status view — use `!theme status`."

    async def resume_timers(self, ctx: DraftContext) -> int:
        """Called on bot startup/reconnect for every active draft, so any
        in-memory timer lost to a process restart gets restarted. Returns
        how many were resumed. Default: no in-memory timers to resume."""
        return 0

    async def cancel(self, ctx: DraftContext) -> None:
        """Called by `!theme cancel` — stop any in-memory timers so they
        don't fire after the draft is dead. The draft row itself is marked
        'cancelled' by the caller; formats only need to clean up their own
        runtime state here. Default: nothing to clean up."""
        return

    # ── Anonymous Draft-specific ops (team/logo setup, Timer Bot relay) —
    # only meaningful for formats that anonymize GMs behind a team logo and
    # ride on an external bot's turn order. Default: not supported. ────────
    async def setup_teams(self, ctx: DraftContext, text: str) -> str:
        return "This draft format doesn't use team/logo setup."

    async def generate_fake_lotto(self, ctx: DraftContext) -> str:
        return "This draft format doesn't need a Timer Bot registration message."

    async def handle_timer_ping(self, ctx: DraftContext, pick_number: int, deadline_ts: int | None,
                                 window_closed: bool = False) -> None:
        """Called by bot.py when it detects the external turn-order bot's
        'your turn' ping in the selection channel. deadline_ts is None when
        window_closed is True (the external bot's own closed-window ping
        carries no real deadline yet). Default: not supported."""
        return

    async def override_pick(self, ctx: DraftContext, pick_number: int, team_ref: str) -> str:
        """Commish safety valve for `!anonoverride` — manually force-open a
        pick for a specific team, if this bot's own pick-number-to-team
        tracking ever drifts from the external turn-order bot's actual
        state. Default: not supported."""
        return "This draft format doesn't support pick overrides."


# ── Shared Sheets side-effects ─────────────────────────────────────────────────
# Both are best-effort/cosmetic: the sqlite DB is always the source of truth,
# so a Sheets hiccup here should never block or undo a pick that's already
# been recorded. Callers just await these and move on.

async def highlight_taken(player: str) -> None:
    """Marks a player taken on the ADP/pool sheet — same convention every
    theme can call, since it never reveals who took the player."""
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, sheets.mark_player_taken, POOL_SPREADSHEET_ID, POOL_TAB_NAME, player)
    except Exception:
        log.warning("highlight_taken | failed to mark '%s' taken on the ADP sheet", player, exc_info=True)


async def sync_team_sheet(ctx: DraftContext, theme: Theme) -> None:
    """Live-writes everyone's in-progress picks (not a final submitted
    roster) to the Team Sheet. Only for formats with no GM identity to
    protect — Anonymous Partner Draft must NOT call this; it writes once at
    reveal instead (see bot.py's `!theme reveal`)."""
    all_picks = db.get_all_picks(ctx.draft_id)
    teams: dict[str, list[str]] = {}
    seen_keys: set[str] = set()
    for d in ctx.drafters():
        key = theme.team_key(ctx, d)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        teams[theme.team_label(ctx, d)] = all_picks.get(d['id'], [])

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, sheets.write_draft_results, ROSTER_SPREADSHEET_ID, ctx.draft['name'], teams)
    except Exception:
        log.warning("sync_team_sheet | live write failed for draft_id=%s", ctx.draft_id, exc_info=True)
