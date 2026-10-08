# themes/auto_draft.py
#
# Auto Draft: every round, every team DMs this bot a ranked list of up to
# `list_size` (default 10) players instead of making one live pick at a
# time — standing in for a human mod who'd otherwise collect those lists
# and adjudicate them by hand. Once every team has submitted (or the
# round's grace timer expires), the whole round resolves at once, using
# the standard "immediate acceptance" (Boston) mechanism:
#   - Each team's current bid is the first player still left on its own
#     list, in its own preference order.
#   - A player bid by exactly one team goes straight to them.
#   - A player bid by two or more teams at DIFFERENT ranks just goes to
#     whoever ranked it higher (lower rank number) outright — no lottery.
#   - A player bid by two or more teams at the SAME rank is a true tie,
#     settled by the tiebreaker waiver order (shown via `!status`) —
#     whoever is higher in that order wins outright, no randomness. Every
#     tie within the same round is decided against the order as it stood
#     at the START of that round — it's frozen for the whole round, not
#     updated pick-by-pick (see ADP tax below for why).
#   - Every team that didn't win its current bid (lost a tie, or got
#     outranked) drops to the next still-available name on its own list
#     and re-bids — repeating until every team has either won a player or
#     run out of listed names for the round.
# A team that runs out of names without winning anything just gets no
# pick that round — rounds don't guarantee one pick per team.
#
# Waiver order movement ("ADP tax"): every team carries a persistent
# `last_tax_adp` — the ADP of the most recent pick that actually taxed
# them (a round win, a Round 1 pool fallback from being skipped/struck
# out, or a Round 2+ makeup claim — see claim_makeup). Any time ONE of
# those happens, the WHOLE order is immediately recomputed (see
# _recompute_tax_order): every team that's never been taxed keeps its
# original relative order up front; every taxed team sits behind them,
# sorted by its last_tax_adp — the better the player (lower ADP), the
# further back. A skip/strikeout is NOT a free pass to keep front-of-line
# position just because the pick was a random fallback rather than a real
# win — it still counts and gets taxed the same as anyone else's.
#
# This is deliberately a running "ADP of your LAST pick" signal, not a
# cumulative total — land a great player this round and a replacement
# player next round, and your position reflects the replacement player,
# not the great one. Making a makeup claim mid-round (e.g. for an owed
# Round 2 pick, claimed during Round 3) moves you immediately too — it's
# compared against every other team's own last_tax_adp, not against
# whatever else resolves later in Round 3, since there's no shared batch
# to wait for.
#
# Ties within a single round's RESOLUTION (who wins a contested player)
# are a separate thing from the above — those are still decided by the
# order as it stood at the START of that round, frozen for the whole
# round. Ties in last_tax_adp itself (for the ADP tax ordering) are
# broken by original !setteams position (earlier position = taxed more).
#
# Each round's recap lists results sorted by ADP (adp_data.py), not pick
# number — see _ADP_KEYS. Each line still shows its own real pick number
# regardless of where it lands in that ADP-sorted display order.
#
# Legend penalty: this is about HOLDING Michael Jordan or LeBron James
# during Round 6-10 (_LEGEND_PENALTY_ROUNDS), not about when the team
# actually drafted one — a team that grabbed one in Round 1 is penalized
# exactly the same as a team that gets one fresh in Round 7. Checked
# fresh every round inside that window (every team's full pick history,
# not just this round's winners), so it's caught no matter which round
# it was actually acquired in. Once caught, a team is marked PERMANENTLY
# penalized (team['legend_penalized'] = True, never cleared) — not just
# moved to the back once. _recompute_tax_order forces any penalized team
# into its own tier at the very back of the order, below every other
# team, for the rest of the draft — no later pick of theirs, however bad
# its ADP, can ever rank them ahead of a non-penalized team again. (An
# earlier version just did a one-time order.remove/append, which doesn't
# survive: the very next tax event from ANYONE — a different round
# resolving, another team's makeup claim — triggers a full
# _recompute_tax_order from last_tax_adp alone, silently erasing an order
# mutation with no memory behind it. The persistent flag is what actually
# makes "last at all times" true.) A GM listing either name on any
# submission, in any round, gets warned at submission time regardless of
# whether that team would actually hold it once Round 6 arrives (see
# _LEGEND_PENALTY_KEYS) — the penalty itself only ever applies in Round
# 6-10.
#
# Round 1 is special-cased: every list that round is restricted to the 30
# names in _ROUND1_POOL — a submission naming anything outside that pool is
# rejected outright (not filtered down), same as a non-Round-1 submission
# naming an already-drafted player; either way the whole list has to be
# fixed and resubmitted, nothing gets silently dropped. Two situations get
# a team a random still-available pool name instead of no pick at all when
# Round 1 resolves: getting skipped outright (nobody submitted in time),
# or submitting a list that struck out entirely (every name on it got
# taken or lost a tiebreak) — both treated identically for this fallback
# (see struck_out_teams).
#
# From Round 2 onward there's no pool restriction, and that same pair of
# situations (skip, or a struck-out list) means the team owes a makeup
# pick instead of an auto-assign — claimable anytime via `!makeup <player>`
# (claim_makeup), a free/uncontested claim against whatever's still
# available (NOT run back through the competitive waterfall). Both cases
# trigger the same owed-makeup mechanics; only the DM explaining why
# differs (nobody submitted, vs. everyone on the list is already gone).
# Makeup claims never expire — they still work even after the
# draft's last round has resolved and its status is 'complete'. Claiming
# one can remove a name out from under another team's currently pending
# (not-yet-resolved) list for the live round; that's always safe for
# resolution itself (it re-checks who's actually taken fresh every time),
# but the affected team gets a DM heads-up and their stored list is
# trimmed immediately so `!status` doesn't show a name that can never
# actually be won.
#
# Picks are never written to the Team Sheet by this bot directly — each
# finalized pick is instead announced as a bare "N. <logo> <Player>"
# message (global pick number, see _pick_number — no markdown, no extra
# text) in the shared results channel
# (_RESULTS_CHANNEL_ID — same fixed channel Anonymous/Nation Flux drafts
# already post to), which is what lets ATD Team Sheet Bot auto-parse and
# write it the moment it sees a recognized team logo + player name (year
# optional, same as any other chat-driven pick there). That means
# _RESULTS_CHANNEL_ID must be the channel ATD Team Sheet Bot has mapped to
# the sheet (`!setsheet`), and `!setteams`' logo column must use the exact
# emoji ATD Team Sheet Bot's emoji_map.py (or its built-in unicode map)
# already recognizes for that team. Submission-progress pings and round
# recaps/tiebreaker/skip notices also post to that same channel — the
# recap is posted as an EMBED rather than plain content specifically so
# it can show every team's real logo safely: Team Sheet Bot only ever
# reads message.content, never embed fields, so a plain-text message
# naming more than one real team logo (which it WOULD try to parse, and
# misfire by writing the rest of that message as a garbage "player" onto
# whichever team's emoji appears first) is never produced here.

import asyncio
import difflib
import logging
import random
import re
import time
import unicodedata

import discord

from adp_data import ADP
import db
import lotto
from timer import PickTimer
from .base import DraftContext, Theme, draft_lock, highlight_taken
from .player_years import get_default_year

log = logging.getLogger(__name__)

_timer = PickTimer()  # keyed by draft_id — one shared grace clock per round, not per team

# Round 1's locked pool — see the module docstring. Matching is
# case-insensitive and apostrophe-normalized (a curly apostrophe from one
# GM's phone vs. a straight one from another's would otherwise compare as
# different strings — the same gotcha other themes in this bot already
# guard against for duplicate-pick checks).
_ROUND1_POOL = [
    "Michael Jordan", "LeBron James", "Kevin Garnett", "Stephen Curry",
    "Shaquille O'Neal", "Magic Johnson", "Larry Bird", "Kareem Abdul-Jabbar",
    "Hakeem Olajuwon", "Kevin Durant", "Jerry West", "Tim Duncan",
    "Kobe Bryant", "Dwyane Wade", "Kawhi Leonard", "Shai Gilgeous-Alexander",
    "Nikola Jokic", "Chris Paul", "David Robinson", "Victor Wembanyama",
    "Steve Nash", "Tracy McGrady", "Oscar Robertson", "Bill Walton",
    "Giannis Antetokounmpo", "Wilt Chamberlain", "Anthony Davis",
    "Julius Erving", "Joel Embiid", "Bill Russell",
]

_APOSTROPHE_VARIANTS = str.maketrans({
    '‘': "'", '’': "'", 'ʼ': "'", '´': "'", '`': "'",
})


def _strip_diacritics(text: str) -> str:
    """NFKD-decomposes and drops combining marks, so e.g. Stojaković's
    'ć' normalizes the same as a plain 'c' — same motivation as the
    apostrophe-variant handling below: a name typed/copied with an accent
    by one GM and without it by another must never compare as two
    different players (confirmed live: this exact mismatch let two teams
    both "win" Peja Stojaković in the same round)."""
    decomposed = unicodedata.normalize('NFKD', text)
    return ''.join(c for c in decomposed if not unicodedata.combining(c))


def _normalize_name(text: str) -> str:
    text = text.strip().lower().translate(_APOSTROPHE_VARIANTS)
    return _strip_diacritics(text)


# Same year-token shapes ATD Team Sheet Bot's own parser recognizes
# ('92, 2012-13, bare 2012, 92-93) — used only to check whether a player
# string already carries one before auto-filling a default (never to parse
# a value out of it; Auto Draft doesn't split player/year apart like Pack
# Draft does, it just passes whatever the GM typed straight through to the
# results channel and lets Team Sheet Bot find the year in it).
_YEAR_TOKEN_IN_TEXT_RE = re.compile(r"'\d{2}\b|\b\d{4}-\d{2,4}\b|\b\d{4}\b|\b\d{2}-\d{2}\b")


def _with_default_year(player: str) -> str:
    """Append this player's on-record default season if their text doesn't
    already carry a year of its own — e.g. a GM just typed "LeBron James"
    with no year anywhere in their list entry."""
    if _YEAR_TOKEN_IN_TEXT_RE.search(player):
        return player
    year = get_default_year(player)
    return f"{player} {year}" if year else player


# Only strips a TRAILING year (not one anywhere in the text, to avoid ever
# mangling a real name) — e.g. "Paul Pierce 2008" from a `!makeup` claim,
# since other themes' `!pick <player> <year>` syntax trains GMs to add one
# out of habit. A bare ADP-table lookup is keyed on the player's name
# alone, so a trailing year silently misses it entirely (confirmed live:
# this is exactly what sent a team's waiver tax to "unranked" instead of
# the real ADP) — strip it before any such lookup.
_TRAILING_YEAR_RE = re.compile(r"\s*(?:'\d{2}|\d{4}-\d{2,4}|\d{4}|\d{2}-\d{2})\s*$")


def _strip_year(text: str) -> str:
    stripped = _TRAILING_YEAR_RE.sub('', text).strip()
    return stripped or text


def _taken_players(ctx: DraftContext) -> set[str]:
    """Apostrophe/case-normalized version of db.get_taken_players. Compare
    against this with _normalize_name() on the other side ALWAYS — never a
    plain .strip().lower(). db.get_taken_players() only lowercases, so a
    curly apostrophe from one GM's phone vs. a straight one from another's
    previously compared as different strings, letting two different teams
    both "win" the same player in the same round (confirmed live: two
    picks of Shaquille O'Neal in one Round 1)."""
    return {_normalize_name(p) for p in db.get_taken_players(ctx.draft_id)}


_ROUND1_POOL_KEYS = {_normalize_name(n) for n in _ROUND1_POOL}

# Round-recap ordering: lines are sorted by ADP (lower = drafted earlier/
# more valuable), not by pick number — a player missing from ADP_KEYS
# (not in adp_data.py) sorts after every ranked player, and a team with no
# pick at all sorts last of all. Each line still shows its own real pick
# number regardless of where it lands in this display order.
_ADP_KEYS = {_normalize_name(name): value for name, value in ADP.items()}
_UNRANKED_ADP = float('inf')

# GMs type picks with wildly inconsistent capitalization ("kareem
# abdul-jabbar", "SHAI GILGEOUS-ALEXANDER", ...). Everything downstream
# (recap posts, `!theme lists`, sheet writes via highlight_taken) should
# show one consistent, properly-capitalized form regardless of how it was
# typed, so every known player name (Round 1 pool or ADP sheet) gets
# canonicalized the moment it's stored — see _canonical_name().
_CANONICAL_NAMES: dict[str, str] = {}
for _n in _ROUND1_POOL:
    _CANONICAL_NAMES[_normalize_name(_n)] = _n
for _n in ADP:
    _CANONICAL_NAMES.setdefault(_normalize_name(_n), _n)


def _fuzzy_canonical(key: str) -> str | None:
    """A unique, high-confidence near-match for `key` (already
    normalized) among every known player — e.g. "sidney moncreif" (a
    real typo submitted live) catches "Sidney Moncrief". 0.88 is tight
    enough that genuinely different players (e.g. "marc gasol"/"pau
    gasol", "chris paul"/"chris mullin") never match, but loose enough to
    catch real-world typos/transpositions (confirmed against every typo
    actually seen in this draft). Returns None — never guesses — if
    nothing clears the cutoff, or if two-plus candidates both do, since
    picking between two real players by typo-distance alone isn't safe."""
    matches = difflib.get_close_matches(key, _CANONICAL_NAMES.keys(), n=2, cutoff=0.88)
    return _CANONICAL_NAMES[matches[0]] if len(matches) == 1 else None


def _canonical_name(name: str) -> str:
    """The properly-capitalized form of `name` if it matches a known
    player exactly or by close typo (Round 1 pool or ADP sheet);
    otherwise the original text, title-cased as a best-effort fallback
    for an unranked/off-sheet name."""
    name = name.strip()
    key = _normalize_name(name)
    known = _CANONICAL_NAMES.get(key)
    if known is not None:
        return known
    fuzzy = _fuzzy_canonical(key)
    if fuzzy is not None:
        return fuzzy
    return name.title()


def _merged_names_split(name: str) -> tuple[str, str] | None:
    """If `name` doesn't match any known player on its own but reads as
    TWO known players typed back-to-back with no separator (confirmed
    live: "Karl Malone Clyde Drexler" submitted as a single list entry),
    returns (first, second) in their canonical forms. Tries every
    word-boundary split and only returns a result when BOTH halves
    independently resolve to a real player (exact or fuzzy) — this never
    fires on a single legitimate name, however obscure/off-sheet, since
    that can't split into two positive matches on its own."""
    words = name.split()
    if len(words) < 4:
        return None
    for i in range(2, len(words) - 1):
        left_key = _normalize_name(' '.join(words[:i]))
        right_key = _normalize_name(' '.join(words[i:]))
        left = _CANONICAL_NAMES.get(left_key) or _fuzzy_canonical(left_key)
        right = _CANONICAL_NAMES.get(right_key) or _fuzzy_canonical(right_key)
        if left and right:
            return left, right
    return None


def _split_trailing_year(text: str) -> tuple[str, str]:
    """(base_name_without_year, year_suffix_or_empty) — e.g. "Peja
    Stojakovic 2003-04" -> ("Peja Stojakovic", "2003-04"). Used only to
    match the bare name against known players; the year itself is never
    discarded (see _resolve_submitted_name), since Auto Draft passes
    whatever a GM typed straight through for Team Sheet Bot to read a
    season/price from — only the MATCHING should ignore it, same
    reasoning as _strip_year but this one re-attaches it afterward."""
    match = _TRAILING_YEAR_RE.search(text)
    if not match:
        return text, ''
    return text[:match.start()].rstrip(), text[match.start():].strip()


def _resolve_submitted_name(name: str) -> tuple[str, tuple[str, str] | None]:
    """The single entry point list/makeup submissions should use instead
    of calling _canonical_name directly — adds the merged-names check on
    top. Returns (canonical_name, merge_error): merge_error is None for
    a normal single player (exact or fuzzy match, or an unrecognized
    name left as-is); when it's not None, `name` matched nothing on its
    own but splits into two known players glued together, and the
    caller should reject the whole submission using merge_error
    (first, second) instead of trusting canonical_name. A trailing year
    (e.g. "Peja Stojakovic 2003-04") is ignored for matching but kept in
    the returned canonical_name — a GM naming a season still needs that
    season to survive for the sheet write, only the player-name part
    needs to be corrected/matched."""
    base, year_suffix = _split_trailing_year(name)
    key = _normalize_name(base)
    known = _CANONICAL_NAMES.get(key) or _fuzzy_canonical(key)
    if known is not None:
        return (f'{known} {year_suffix}' if year_suffix else known), None
    merge = _merged_names_split(base)
    if merge is not None:
        return name, merge
    return name.title(), None

# "Legend penalty": whoever HOLDS Michael Jordan or LeBron James once
# Round 6-10 is underway is locked at the very back of the tiebreaker
# waiver order, permanently — it doesn't matter which round they actually
# drafted it in (Round 1 counts the same as Round 7). The WARNING is
# broader than the penalty on purpose: a GM gets warned the instant
# either name shows up on ANY list (any round, as long as it's still
# undrafted — on_list/queue_future_list already refuse an already-taken
# name outright, so reaching the warning check means it's still live) —
# the idea being standing awareness of who the penalty players are, not
# just a Round 6-10-only notice. The penalty check itself only ever runs
# during Round 6-10 (_resolve_round scans every team's full history each
# of those rounds, not just that round's own winners).
_LEGEND_PENALTY_ROUNDS = range(6, 11)
_LEGEND_PENALTY_KEYS = {_normalize_name(n) for n in ("Michael Jordan", "LeBron James")}

# ATD Team Sheet Bot treats ANY message starting with "N." or containing a
# raw custom-emoji token as a pick attempt (its own looks_like_pick check),
# regardless of whether it's actually one — so any PLAIN-TEXT message that
# mixes a real logo with other prose, or leads with a bare pick number,
# risks misfiring there. The genuine "N. <logo> <Player>" trigger message
# is deliberately exactly that shape (on purpose, so it DOES get parsed);
# every other announcement that needs to show a real logo (round recaps,
# makeup-needed notices) is posted as an embed instead — Team Sheet Bot
# only ever reads message.content, never embed fields, so the real emoji
# can render there with no risk of being parsed as a pick.

# Live submission-progress pings ("X has submitted, waiting on N more,
# Y mins left") post here — the same shared results channel Anonymous
# Draft/Nation Flux already post to for this league — rather than the
# draft's own admin channel, so the whole league can watch a round fill
# in without needing access to wherever the draft was actually created.
_RESULTS_CHANNEL_ID = 934052208624930848


async def _results_channel(ctx: DraftContext):
    channel = ctx.bot.get_channel(_RESULTS_CHANNEL_ID)
    if channel is not None:
        return channel
    try:
        return await ctx.bot.fetch_channel(_RESULTS_CHANNEL_ID)
    except Exception:
        log.warning("AutoDraft | results channel %s not found/reachable", _RESULTS_CHANNEL_ID)
        return None


class AutoDraftTheme(Theme):
    key = 'auto_draft'
    display_name = 'Auto Draft'

    def default_settings(self) -> dict:
        return {
            'rounds': 10,
            'list_size': 10,
            'response_deadline_minutes': 120,  # 2h grace window per round
        }

    def _settings(self, ctx: DraftContext) -> dict:
        return {**self.default_settings(), **ctx.settings}

    # ── Setup (DM-only, commish) ─────────────────────────────────────────────
    async def setup_teams(self, ctx: DraftContext, text: str) -> str:
        parsed = lotto.parse_team_lines(text)
        if not parsed:
            return (
                "❌ No valid team lines found. Format: `1. 🦅 - @GM1 @GM2`, one team per line, in draft order — "
                "the logo must be the exact emoji ATD Team Sheet Bot already recognizes for that team, since "
                "that's what lets picks auto-write to the sheet."
            )

        seen_users: set[int] = set()
        for entry in parsed:
            if not (1 <= len(entry['user_ids']) <= 3):
                return f"❌ Team on line {entry['order_index'] + 1} has {len(entry['user_ids'])} member(s) — must be 1-3."
            for uid in entry['user_ids']:
                if uid in seen_users:
                    return f"❌ <@{uid}> is listed on more than one team."
                seen_users.add(uid)

        teams = []
        for entry in parsed:
            member_ids = []
            for uid in entry['user_ids']:
                drafter = db.get_drafter_by_user(ctx.draft_id, uid)
                if drafter:
                    member_ids.append(drafter['id'])
                    continue
                display_name = str(uid)
                try:
                    user = ctx.bot.get_user(uid) or await ctx.bot.fetch_user(uid)
                    if user:
                        display_name = getattr(user, 'display_name', None) or user.name
                except Exception:
                    pass
                member_ids.append(db.add_drafter(ctx.draft_id, uid, display_name))

            teams.append({
                'index': len(teams),
                'logo': entry['logo'],
                'member_ids': member_ids,
                'skip_count': 0,
                'owed_makeups': [],  # round numbers (Round 2+) this team was skipped in and hasn't claimed yet
                'future_lists': {},  # str(round_number) -> queued player list, applied when that round opens
            })

        for team in teams:
            for mid in team['member_ids']:
                db.update_drafter_state(mid, {'team_index': team['index']})

        state = ctx.state
        state['teams'] = teams
        state['tiebreak_order'] = [t['index'] for t in teams]
        ctx.save_state(state)

        for team in teams:
            for mid in team['member_ids']:
                drafter = db.get_drafter(mid)
                await ctx.dm(
                    drafter['user_id'],
                    f"🏀 You're registered for Auto Draft — your team's logo is {team['logo']}. "
                    f"That's what you'll see on every pick and announcement for your team.",
                )

        total_gms = sum(len(t['member_ids']) for t in teams)
        return (
            f"✅ Registered {len(teams)} teams ({total_gms} GMs total) — line order is also the starting "
            f"tiebreaker waiver order. Run `!theme start {ctx.draft_id}` to open round 1."
        )

    async def start(self, ctx: DraftContext) -> str:
        teams = ctx.state.get('teams', [])
        if not teams:
            raise ValueError("No teams registered — DM me `!setteams` first.")
        settings = self._settings(ctx)
        state = ctx.state
        state['round_index'] = 0
        state['lists'] = {}
        ctx.save_state(state)
        await self._open_round(ctx)
        return (
            f"Auto Draft started — {len(teams)} teams, {settings['rounds']} rounds. "
            f"Round 1 is open: DM me `!list P1, P2, ..., P{settings['list_size']}`."
        )

    # ── Round lifecycle ──────────────────────────────────────────────────────
    async def _open_round(self, ctx: DraftContext, results: dict[int, str] | None = None) -> None:
        """Opens the next round. `results` (team_idx -> short result text),
        when given, folds the just-finished round's outcome into the SAME
        embed as the "round N is open" notice — one DM per GM per
        transition instead of two separate back-to-back messages."""
        state = ctx.state
        settings = self._settings(ctx)
        round_number = state['round_index'] + 1
        deadline_ts = int(time.time() + settings['response_deadline_minutes'] * 60)

        # Apply any list a team queued ahead of time for THIS round via
        # `!futurelist` (see queue_future_list) — for GMs who won't be
        # online when it opens. Unlike a live `!list`, an invalid name here
        # is silently dropped rather than rejecting the whole thing —
        # nobody's present to fix and resend it.
        already_taken = _taken_players(ctx)
        lists = state.setdefault('lists', {})
        applied_future: dict[int, tuple[list[str], int]] = {}
        for team in state['teams']:
            queued = team.get('future_lists', {}).pop(str(round_number), None)
            if queued is None:
                continue
            if round_number == 1:
                available = [p for p in queued if _normalize_name(p) in _ROUND1_POOL_KEYS]
            else:
                available = [p for p in queued if _normalize_name(p) not in already_taken]
            dropped = len(queued) - len(available)
            if available:
                lists[str(team['index'])] = available
                applied_future[team['index']] = (available, dropped)
        if applied_future:
            ctx.save_state(state)

        if len(lists) >= len(state['teams']):
            # Every team's list for this round was already queued ahead of
            # time — resolve immediately instead of opening a round nobody
            # still needs to act on. Safe re: draft_lock's "callers must
            # already hold it" rule for _resolve_round: this can only ever
            # be reached for round 2+ (round 1 can never be pre-queued,
            # since it's always "current," never "future," before
            # !theme start), and round 2+ only ever opens from inside
            # _resolve_round's own already-locked flow.
            await self._resolve_round(ctx)
            return

        for team in state['teams']:
            embed = discord.Embed(title=f"🤖 Auto Draft — Round {round_number}", color=discord.Color.blue())
            if results is not None:
                embed.add_field(name=f"Round {round_number - 1} result", value=results.get(team['index'], '—'), inline=False)
            if team['index'] in applied_future:
                available, dropped = applied_future[team['index']]
                note = f" ({dropped} name(s) dropped — already taken.)" if dropped else ''
                embed.add_field(
                    name=f"Round {round_number} list",
                    value=f"✅ Auto-submitted from your queued `!futurelist`: {', '.join(available)}{note}",
                    inline=False,
                )
            else:
                embed.add_field(
                    name="This round",
                    value=(
                        f"DM me a ranked list of up to {settings['list_size']} players, most wanted first:\n"
                        f"`!list Player One, Player Two, Player Three, ...`\n"
                        f"Won't be around? Queue it now for a later round: `!futurelist <round#> P1, P2, ...`"
                    ),
                    inline=False,
                )
            embed.add_field(
                name="Deadline",
                value=f"<t:{deadline_ts}:R> — anyone still missing by then gets resolved with whatever they've submitted (or nothing, if they never did).",
                inline=False,
            )
            for mid in team['member_ids']:
                drafter = db.get_drafter(mid)
                await ctx.dm(drafter['user_id'], embed=embed)

        self._start_round_timer(ctx, state, settings['response_deadline_minutes'])

    def _start_round_timer(self, ctx: DraftContext, state: dict, minutes: float) -> None:
        state['round_deadline'] = time.time() + minutes * 60
        ctx.save_state(state)

        async def _on_timeout():
            async with draft_lock(ctx.draft_id):
                await self._resolve_round(ctx, forced=True)

        _timer.start(ctx.draft_id, minutes, _on_timeout)

    # ── List submission ──────────────────────────────────────────────────────
    async def on_list(self, ctx: DraftContext, drafter: dict, text: str) -> str:
        async with draft_lock(ctx.draft_id):
            settings = self._settings(ctx)
            state = ctx.state
            if state.get('round_index', 0) >= settings['rounds']:
                return "Auto Draft has finished all its rounds."

            team_idx = drafter['state'].get('team_index')
            if team_idx is None:
                return "You're not assigned to a team yet."

            players = self._parse_list(text, settings['list_size'])
            if players is None:
                return f"Submit 1-{settings['list_size']} distinct players, comma- or line-separated."

            is_round1 = state.get('round_index', 0) == 0
            if is_round1:
                invalid = [p for p in players if _normalize_name(p) not in _ROUND1_POOL_KEYS]
                if invalid:
                    return (
                        f"❌ Not in the Round 1 pool: {', '.join(invalid)}. Round 1 is locked to a set list "
                        f"of 30 players — fix your list and resubmit."
                    )
            else:
                already_taken = _taken_players(ctx)
                invalid = [p for p in players if _normalize_name(p) in already_taken]
                if invalid:
                    return f"❌ Already drafted: {', '.join(invalid)}. Fix your list and resubmit."

            resolved_players = []
            for p in players:
                canonical, merge = _resolve_submitted_name(p)
                if merge:
                    left, right = merge
                    return (
                        f"❌ \"{p}\" looks like two players combined — did you mean **{left}** and "
                        f"**{right}**? Separate them with a comma and resubmit."
                    )
                resolved_players.append(canonical)
            players = resolved_players
            lists = state.setdefault('lists', {})
            lists[str(team_idx)] = players
            ctx.save_state(state)

            teams = state['teams']
            team = teams[team_idx]
            remaining = len(teams) - len(lists)
            round_number = state['round_index'] + 1

            legend_warning = ''
            if any(_normalize_name(p) in _LEGEND_PENALTY_KEYS for p in players):
                legend_warning = (
                    " ⚠️ Heads up: Michael Jordan/LeBron James is on this list — if your team HOLDS either "
                    "one once Round 6 arrives (whichever round you actually got it), you'll be locked at the "
                    "very back of the waiver order for the rest of the draft, no matter what you draft "
                    "afterward."
                )

            # Heads-up to any teammate(s) on a duo/trio — there's no
            # consensus step gating a submission (last one in before the
            # round resolves just wins), so a partner should always know
            # the instant the other half of their team sets the list.
            for mid in team['member_ids']:
                if mid == drafter['id']:
                    continue
                mate = db.get_drafter(mid)
                await ctx.dm(
                    mate['user_id'],
                    f"📝 Your partner **{drafter['display_name']}** just submitted your team's Round "
                    f"{round_number} list: {', '.join(players)}",
                )

            # Auto Draft's own overnight auto-pause (night_paused) freezes
            # the clock and auto-resolve, but submissions stay open — a GM
            # who happens to be online shouldn't be blocked from getting
            # their list in early. Don't let a submission break that
            # freeze though: no restarting the timer, and no resolving
            # early even if everyone's now in — that waits for the actual
            # resume job (see _apply_auto_draft_resume in bot.py).
            draft_row = ctx.draft
            is_night_paused = draft_row['status'] == 'paused' and draft_row['state'].get('night_paused', False)

            channel = await _results_channel(ctx)
            if channel:
                if remaining <= 0 and is_night_paused:
                    await channel.send(f"📝 <@{drafter['user_id']}> has submitted their list for Round {round_number} — everyone's in, but the draft is paused for the night. It'll resolve once it resumes.")
                elif remaining <= 0:
                    await channel.send(f"📝 <@{drafter['user_id']}> has submitted their list for Round {round_number} — everyone's in, resolving now!")
                else:
                    deadline = state.get('round_deadline')
                    time_left = f" Time left: <t:{int(deadline)}:R>" if deadline else ''
                    await channel.send(
                        f"📝 <@{drafter['user_id']}> has submitted their list for Round {round_number} — "
                        f"waiting on {remaining} more list(s).{time_left}"
                    )

            if remaining <= 0 and is_night_paused:
                return (
                    f"✅ List received — everyone's in, but the draft is paused for the night. It'll resolve "
                    f"automatically once it resumes.{legend_warning}"
                )

            if remaining <= 0:
                _timer.cancel(ctx.draft_id)
                await self._resolve_round(ctx)
                return f"✅ List received — everyone's in, round {round_number} is resolving now.{legend_warning}"

            if not is_night_paused and not _timer.is_running(ctx.draft_id):
                self._start_round_timer(ctx, state, settings['response_deadline_minutes'])

            return f"✅ List received for round {round_number}. Waiting on {remaining} more team(s).{legend_warning}"

    # ── Queue a future round's list (for GMs who won't be online then) ──────
    async def queue_future_list(self, ctx: DraftContext, drafter: dict, text: str) -> str:
        async with draft_lock(ctx.draft_id):
            settings = self._settings(ctx)
            team_idx = drafter['state'].get('team_index')
            if team_idx is None:
                return "You're not assigned to a team yet."

            parts = text.strip().split(maxsplit=1)
            if len(parts) < 2 or not parts[0].isdigit():
                return "Usage: `!futurelist <round number> Player One, Player Two, ...`"

            round_number = int(parts[0])
            state = ctx.state
            current_round = state.get('round_index', 0) + 1
            if round_number < current_round:
                return f"Round {round_number} has already passed."
            if round_number == current_round:
                return f"Round {round_number} is already open — just use `!list` instead."
            if round_number > settings['rounds']:
                return f"This draft only has {settings['rounds']} rounds."

            players = self._parse_list(parts[1], settings['list_size'])
            if players is None:
                return f"Submit 1-{settings['list_size']} distinct players, comma- or line-separated."

            resolved_players = []
            for p in players:
                canonical, merge = _resolve_submitted_name(p)
                if merge:
                    left, right = merge
                    return (
                        f"❌ \"{p}\" looks like two players combined — did you mean **{left}** and "
                        f"**{right}**? Separate them with a comma and resubmit."
                    )
                resolved_players.append(canonical)
            players = resolved_players

            team = state['teams'][team_idx]
            team.setdefault('future_lists', {})[str(round_number)] = players
            ctx.save_state(state)

            for mid in team['member_ids']:
                if mid == drafter['id']:
                    continue
                mate = db.get_drafter(mid)
                await ctx.dm(
                    mate['user_id'],
                    f"📝 Your partner **{drafter['display_name']}** just queued your team's Round "
                    f"{round_number} list: {', '.join(players)}",
                )

            legend_warning = ''
            if any(_normalize_name(p) in _LEGEND_PENALTY_KEYS for p in players):
                legend_warning = (
                    f" ⚠️ Heads up: Michael Jordan/LeBron James is on this list — if your team HOLDS either "
                    f"one once Round 6 arrives (whichever round you actually got it), you'll be locked at "
                    f"the very back of the waiver order for the rest of the draft, no matter what you draft "
                    f"afterward."
                )

            return (
                f"✅ Queued for Round {round_number}: {', '.join(players)}. It'll auto-submit the moment that "
                f"round opens — any name already taken by then gets dropped automatically, no need to be "
                f"online.{legend_warning}"
            )

    # ── Round resolution ─────────────────────────────────────────────────────
    # Callers must already hold draft_lock(ctx.draft_id) — this is never an
    # independent entry point itself (on_list and the round timer's own
    # timeout callback each acquire the lock once, then call this).
    async def _resolve_round(self, ctx: DraftContext, forced: bool = False) -> None:
        state = ctx.state
        settings = self._settings(ctx)
        if state.get('round_index', 0) >= settings['rounds']:
            return  # already finished — a stale timer fired after completion

        teams = state['teams']
        lists_raw = state.get('lists', {})
        skipped_teams = []
        lists: dict[int, list[str]] = {}
        for team in teams:
            entry = lists_raw.get(str(team['index']))
            if entry is None:
                team['skip_count'] = team.get('skip_count', 0) + 1
                skipped_teams.append(team['index'])
            lists[team['index']] = entry or []

        already_taken = _taken_players(ctx)
        tiebreak_order_before = list(state.get('tiebreak_order', [t['index'] for t in teams]))
        assigned, tie_events = self._resolve_assignments(
            lists, already_taken, tiebreak_order_before,
        )
        round_number = state['round_index'] + 1

        # A team that DID submit a list but struck out entirely (every
        # name on it got taken or lost a tiebreak) gets the exact same
        # fallback as a team that was genuinely skipped — Round 1 random
        # pool assignment, or a Round 2+ owed makeup — just with different
        # wording in their own DM explaining why (see `results` below).
        struck_out_teams = [t['index'] for t in teams if t['index'] not in assigned and t['index'] not in skipped_teams]

        auto_assigned_teams: list[int] = []
        if round_number == 1 and (skipped_teams or struck_out_teams):
            claimed_keys = {_normalize_name(p) for p in assigned.values()} | already_taken
            remaining_pool = [name for name in _ROUND1_POOL if _normalize_name(name) not in claimed_keys]
            random.shuffle(remaining_pool)
            for team_idx in skipped_teams + struck_out_teams:
                if not remaining_pool:
                    log.warning("AutoDraft | Round 1 pool exhausted — draft_id=%s team_idx=%s got no auto-assign", ctx.draft_id, team_idx)
                    break
                assigned[team_idx] = remaining_pool.pop()
                auto_assigned_teams.append(team_idx)
        elif skipped_teams or struck_out_teams:
            # Round 2+: no auto-assign — instead the team just owes a free,
            # uncontested makeup pick they can claim whenever via
            # `!makeup <player>` (see claim_makeup). Never expires.
            for team_idx in skipped_teams + struck_out_teams:
                teams[team_idx].setdefault('owed_makeups', []).append(round_number)

        # ADP tax: every team with a player this round — including Round
        # 1's random pool fallback for a skip/strikeout — gets its
        # last_tax_adp updated and the whole order recomputed around it.
        # A skip or strikeout isn't a free pass to keep front-of-line
        # position: getting bailed out by the pool fallback still counts
        # as "having a player," so it's taxed exactly like everyone else's
        # real pick. (Round 2+ skip/strikeout is different — those teams
        # get NO player here, just an owed makeup claimed later via
        # `!makeup`, which taxes them at claim time instead — see
        # claim_makeup.) See the module docstring for the full mechanic.
        for team_idx, player in assigned.items():
            self._tax_team(state, team_idx, player)

        # Legend penalty (Round 6-10 only) — this is about HOLDING Michael
        # Jordan or LeBron James during this window, not just drafting one
        # THIS round — a team that grabbed one back in Round 1 is just as
        # penalized as one that gets it fresh in Round 7. Checked fresh
        # every round in the window (not just the first one entered) so a
        # team is caught the moment it's found to hold one, however it
        # got there; already-flagged teams are skipped since the flag is
        # permanent anyway. See _recompute_tax_order, which always forces
        # a penalized team to the very back for the rest of the draft,
        # regardless of ADP. The WARNING at submission time fires in
        # every round regardless of when the penalty itself can apply —
        # see _LEGEND_PENALTY_KEYS.
        legend_penalties: list[tuple[int, str]] = []
        if round_number in _LEGEND_PENALTY_ROUNDS:
            for team in teams:
                if team.get('legend_penalized'):
                    continue
                this_round_player = assigned.get(team['index'])
                if this_round_player and _normalize_name(this_round_player) in _LEGEND_PENALTY_KEYS:
                    hit = this_round_player
                else:
                    hit = None
                    for mid in team['member_ids']:
                        owned = db.get_picks(ctx.draft_id, mid)
                        hit = next((p for p in owned if _normalize_name(p) in _LEGEND_PENALTY_KEYS), None)
                        if hit:
                            break
                if hit:
                    team['legend_penalized'] = True
                    legend_penalties.append((team['index'], hit))

        state['tiebreak_order'] = self._recompute_tax_order(state)

        channel = await _results_channel(ctx)
        if channel is None:
            log.warning("AutoDraft | no results channel available — round %s picks/recap not posted", ctx.draft_id)

        num_teams = len(teams)
        # Picks are recorded/announced in ADP order (best player first),
        # not pick-number order — same sort as the recap embed below. The
        # pick number shown on each line is still that team's real slot.
        pick_entries: list[tuple[float, int, dict, str]] = []
        for team in teams:
            player = assigned.get(team['index'])
            if player is not None:
                pick_num = self._pick_number(round_number, team['index'], num_teams)
                adp = _ADP_KEYS.get(_normalize_name(player), _UNRANKED_ADP)
                pick_entries.append((adp, pick_num, team, player))

        for _adp, pick_num, team, player in sorted(pick_entries, key=lambda e: e[0]):
            for mid in team['member_ids']:
                db.record_pick(ctx.draft_id, mid, player, round_number=round_number, pick_number=pick_num, auto_pick=False)
            # Fire-and-forget, not awaited: highlight_taken's own Sheets
            # call can block for 100+ seconds across its internal 429
            # retry/backoff (sheets.py's _batch_update_with_retry) — a
            # Sheets quota exhaustion has previously delayed the round
            # recap by minutes because this was awaited inline per pick.
            # It already swallows its own exceptions, so nothing here
            # needs its result.
            asyncio.create_task(highlight_taken(player))
            # "N. <logo> <Player> [year]" — ATD Team Sheet Bot's own parser
            # already strips a leading "N. " pick-number prefix before
            # reading the logo + player, so this is safe and actually
            # the format it was originally built around. See the module
            # docstring. _with_default_year only adds a year when the GM's
            # own text didn't already carry one.
            if channel:
                await channel.send(f"{pick_num}. {team['logo']} {_with_default_year(player)}")

        for team in teams:
            if assigned.get(team['index']) is not None:
                continue
            if (team['index'] in skipped_teams or team['index'] in struck_out_teams) and round_number > 1 and channel:
                pick_num = self._pick_number(round_number, team['index'], num_teams)
                # Plain text on purpose, with a real emoji token and a
                # leading pick number — the exact shape ATD Team Sheet Bot
                # and ATD Sheet Bot would otherwise treat as a pick/player
                # mention. Both are taught to skip any message containing
                # the literal phrase "needs to make up the pick in DMs."
                # (see their own on_message handlers) — don't reword this
                # without updating both of those checks to match.
                mentions = ', '.join(f"<@{db.get_drafter(mid)['user_id']}>" for mid in team['member_ids'])
                await channel.send(f"{pick_num}. {team['logo']} — {mentions} needs to make up the pick in DMs.")

        # Kept forever (never pruned) so a round's actual submitted lists
        # and resolution outcome stay inspectable after it resolves —
        # state['lists'] below gets wiped every round, and without this
        # there'd be no way to answer "what did everyone submit in round
        # N" or redo a waiver-order calculation after the fact (this is
        # exactly the data that was missing when the Shaq duplicate-pick
        # bug needed a Round 1 recount). See current_round_lists().
        state.setdefault('round_history', {})[str(round_number)] = {
            'lists': {str(k): v for k, v in lists.items()},
            'assigned': {str(k): v for k, v in assigned.items()},
            'skipped_teams': list(skipped_teams),
            'struck_out_teams': list(struck_out_teams),
            'auto_assigned_teams': list(auto_assigned_teams),
            'tie_events': [[player, list(tied), winner] for player, tied, winner in tie_events],
            'legend_penalties': [[t, p] for t, p in legend_penalties],
            'tiebreak_order_before': tiebreak_order_before,
            'tiebreak_order_after': list(state['tiebreak_order']),
        }

        state['lists'] = {}
        state['round_index'] = round_number
        ctx.save_state(state)
        _timer.cancel(ctx.draft_id)

        by_team = {t['index']: t for t in teams}
        result_entries: list[tuple[float, str]] = []
        for team in teams:
            pick_num = self._pick_number(round_number, team['index'], num_teams)
            player = assigned.get(team['index'])
            if player:
                adp = _ADP_KEYS.get(_normalize_name(player), _UNRANKED_ADP)
                result_entries.append((adp, f"{pick_num}. {team['logo']} **{player}**"))
            elif (team['index'] in skipped_teams or team['index'] in struck_out_teams) and round_number > 1:
                mentions = ', '.join(f"<@{db.get_drafter(mid)['user_id']}>" for mid in team['member_ids'])
                result_entries.append((_UNRANKED_ADP, f"{pick_num}. {team['logo']} — {mentions} needs to make up the pick in DMs."))
            else:
                result_entries.append((_UNRANKED_ADP, f"{pick_num}. {team['logo']} *(no pick)*"))
        # Sorted by ADP (lower = drafted earlier/more valuable) rather than
        # by pick number — see _ADP_KEYS. Python's sort is stable, so the
        # unranked/no-pick group at the back keeps its original pick-number
        # order among themselves.
        result_lines = [line for _adp, line in sorted(result_entries, key=lambda e: e[0])]
        tie_lines = [
            f"⚖️ **{player}** — {', '.join(by_team[t]['logo'] for t in tied)} tied, "
            f"{by_team[winner]['logo']} had higher waiver priority."
            for player, tied, winner in tie_events
        ]
        skip_lines = [f"{by_team[i]['logo']} ({assigned[i]})" for i in auto_assigned_teams]
        penalty_lines = [
            f"{by_team[t]['logo']} drafted **{p}** — locked at the very back of the waiver order for the "
            "rest of the draft."
            for t, p in legend_penalties
        ]
        if channel:
            # Posted as an embed, never plain content: Team Sheet Bot only
            # ever reads message.content, never embed fields, so this is
            # the one place real multi-team logo tokens are safe to render
            # — see the module docstring for why a plain-text message with
            # more than one raw logo in it is dangerous in this channel.
            embed = discord.Embed(
                title=f"🤖 Auto Draft — Round {round_number} recap",
                description='\n'.join(result_lines),
                color=discord.Color.blurple(),
            )
            # Chunked at 1000 chars/field (not Discord's real 1024 cap —
            # a safety margin) and split across "Name (cont.)" fields as
            # needed: a single field CAN exceed 1024 for real — Round 1
            # alone had 7 ties that added up to 1179 chars in one field,
            # which Discord outright rejects the whole embed for. Letting
            # that 400 happen here silently skips everything after it in
            # this function, including every team's own recap DM and
            # opening the next round, so this can never be allowed to
            # fail no matter how many ties/skips/penalties stack up.
            self._add_chunked_field(embed, 'Tiebreakers', tie_lines)
            self._add_chunked_field(embed, 'Round 1 skips', skip_lines)
            self._add_chunked_field(embed, 'Legend penalty', penalty_lines)
            await channel.send(embed=embed)

        penalized_teams = {t for t, _ in legend_penalties}
        results: dict[int, str] = {}
        for team in teams:
            player = assigned.get(team['index'])
            if player and team['index'] in penalized_teams:
                results[team['index']] = (
                    f"🏀 You drafted **{player}**! ⚠️ You're now locked at the very back of the waiver order "
                    "for the rest of the draft, no matter what you draft afterward."
                )
            elif player:
                results[team['index']] = f"🏀 You drafted **{player}**!"
            elif team['index'] in skipped_teams and round_number > 1:
                results[team['index']] = (
                    "😬 Nobody on your team submitted a list in time — you now owe a makeup pick. "
                    "DM me `!makeup <player>` whenever you're ready to claim one (it never expires)."
                )
            elif team['index'] in struck_out_teams and round_number > 1:
                results[team['index']] = (
                    "😬 Everyone on your list is gone — every name was already taken or lost a tiebreak. You "
                    "now owe a makeup pick: DM me `!makeup <player>` whenever you're ready to claim one (it "
                    "never expires)."
                )
            else:
                results[team['index']] = "😬 Every name on your list was taken or lost a tiebreak — no pick this round."

        if round_number >= settings['rounds']:
            db.set_draft_status(ctx.draft_id, 'complete')
            for team in teams:
                embed = discord.Embed(
                    title=f"🏁 Auto Draft — Round {round_number} (Final)",
                    description=results[team['index']],
                    color=discord.Color.gold(),
                )
                for mid in team['member_ids']:
                    drafter = db.get_drafter(mid)
                    await ctx.dm(drafter['user_id'], embed=embed)

            outstanding = [(t['logo'], t['owed_makeups']) for t in teams if t.get('owed_makeups')]
            note = ''
            if outstanding:
                note = "\n⚠️ Outstanding makeup picks still owed: " + ', '.join(
                    f"{logo} (Round(s) {', '.join(map(str, rounds))})" for logo, rounds in outstanding
                )
            await ctx.announce(f"🏁 **Auto Draft complete!** All {settings['rounds']} rounds resolved.{note}")
            return

        await self._open_round(ctx, results)

    @staticmethod
    def _resolve_assignments(
        lists: dict[int, list[str]], already_taken: set[str], tiebreak_order: list[int],
    ) -> tuple[dict[int, str], list[tuple[str, list[int], int]]]:
        """Runs the immediate-acceptance waterfall for one round. Returns
        (team_idx -> winning player, [(player, [tied team_idxs], winner_idx)]
        for every tie actually broken).

        `tiebreak_order` is a frozen snapshot of the order as it stood at
        the START of this round — every tie in the round is decided
        against this same snapshot, not a live-updating one. Waiver order
        MOVEMENT (the "ADP tax") happens once, after the whole round is
        resolved — see _resolve_round — not pick-by-pick in here, so this
        function never mutates or returns a new order.

        Every "is this the same player" comparison here MUST go through
        _normalize_name(), never a plain .strip().lower() — a curly
        apostrophe from one GM's phone vs. a straight one from another's
        otherwise compare as different strings, which let two different
        teams both "win" the literal same player in the same round
        (confirmed live: two Round 1 picks of Shaquille O'Neal) since each
        team's bid hashed to a different key and neither ever saw the
        other as a conflict. `already_taken` must already be normalized
        by the caller (see _taken_players) — re-normalized here too in
        case it isn't, since this is exactly the class of bug a silent
        mismatch here causes."""
        already_taken = {_normalize_name(p) for p in already_taken}
        pointers = {team_idx: 0 for team_idx in lists}
        assigned: dict[int, str] = {}
        taken_this_round: set[str] = set()
        tie_events: list[tuple[str, list[int], int]] = []
        order = tiebreak_order
        pending = {team_idx for team_idx, names in lists.items() if names}

        while pending:
            candidates: dict[int, tuple[str, int]] = {}
            exhausted_now = set()
            for team_idx in pending:
                team_list = lists[team_idx]
                p = pointers[team_idx]
                while p < len(team_list) and (
                    _normalize_name(team_list[p]) in already_taken
                    or _normalize_name(team_list[p]) in taken_this_round
                ):
                    p += 1
                pointers[team_idx] = p
                if p >= len(team_list):
                    exhausted_now.add(team_idx)
                    continue
                candidates[team_idx] = (team_list[p], p)

            pending -= exhausted_now
            if not candidates:
                break

            by_key: dict[str, list[tuple[int, int, str]]] = {}
            for team_idx, (player, rank) in candidates.items():
                key = _normalize_name(player)
                by_key.setdefault(key, []).append((team_idx, rank, player))

            progressed = False
            for key, bids in by_key.items():
                min_rank = min(r for _, r, _ in bids)
                top = [(t, p) for t, r, p in bids if r == min_rank]
                display_player = top[0][1]
                if len(top) == 1:
                    winner = top[0][0]
                else:
                    # Deterministic, not random: whoever is higher in the
                    # START-of-round waiver order wins outright. A team not
                    # in `order` (shouldn't normally happen) sorts last, so
                    # it never wins a tie against a team that's actually
                    # ranked.
                    top_team_idxs = [t for t, _ in top]
                    winner = min(top_team_idxs, key=lambda t: order.index(t) if t in order else len(order))
                    tie_events.append((display_player, top_team_idxs, winner))
                assigned[winner] = display_player
                taken_this_round.add(key)
                pending.discard(winner)
                pointers[winner] += 1
                progressed = True

            if not progressed:
                break

        return assigned, tie_events

    @staticmethod
    def _tax_team(state: dict, team_idx: int, player: str) -> None:
        """Records `player` as this team's new last_tax_adp — the one
        thing _recompute_tax_order reads. Doesn't touch the order itself;
        call _recompute_tax_order (and save the result into
        state['tiebreak_order']) after tagging everyone who needs it for
        this event. Strips a trailing year first (see _strip_year) — a
        `!makeup` claim is free text and can carry one out of habit, which
        would otherwise miss the ADP table entirely and silently tax as
        "unranked" instead of the player's real value (confirmed live)."""
        by_idx = {t['index']: t for t in state['teams']}
        by_idx[team_idx]['last_tax_adp'] = _ADP_KEYS.get(_normalize_name(_strip_year(player)), _UNRANKED_ADP)

    @staticmethod
    def _recompute_tax_order(state: dict) -> list[int]:
        """Rebuilds the full waiver order from scratch using each team's
        persistent last_tax_adp (see _tax_team and the module docstring).
        Teams with no last_tax_adp yet (never taxed) keep their original
        !setteams relative order, up front. Every taxed team sits behind
        them, sorted by last_tax_adp — lower ADP (better player) further
        back — ties broken by original position (earlier = taxed more).

        A team ever marked `legend_penalized` (drafted MJ/LeBron in Round
        6-10 — see _resolve_round/claim_makeup) is forced into its own
        tier at the very back, below every other team, UNCONDITIONALLY —
        no later pick of theirs, however bad its ADP, can rank them ahead
        of a non-penalized team again. This flag is never cleared once
        set, which is the whole point: a one-time order mutation (the
        original implementation) gets silently erased the next time
        ANYONE's tax event triggers a full recompute, since recomputing
        from last_tax_adp alone has no memory of it — this tier is what
        actually makes the penalty stick for the rest of the draft.
        Pure function of current state; safe to call after every single
        tax event (a round's worth of winners, or one makeup claim)."""
        teams = state['teams']
        original_order = [t['index'] for t in teams]
        by_idx = {t['index']: t for t in teams}
        untaxed = [idx for idx in original_order if 'last_tax_adp' not in by_idx[idx] and not by_idx[idx].get('legend_penalized')]
        taxed = [idx for idx in original_order if 'last_tax_adp' in by_idx[idx] and not by_idx[idx].get('legend_penalized')]
        penalized = [idx for idx in original_order if by_idx[idx].get('legend_penalized')]
        taxed.sort(key=lambda idx: (-by_idx[idx]['last_tax_adp'], -idx))
        penalized.sort(key=lambda idx: (-by_idx[idx].get('last_tax_adp', _UNRANKED_ADP), -idx))
        return untaxed + taxed + penalized

    @staticmethod
    def _add_chunked_field(embed: discord.Embed, name: str, lines: list[str], limit: int = 1000) -> None:
        """Adds `lines` to `embed` as one or more fields, each kept under
        `limit` chars — well under Discord's real 1024-char-per-field cap
        — by starting a new "`name` (cont.)" field whenever the next line
        would push the current one over. A field at or past 1024 makes
        Discord reject the WHOLE embed outright (confirmed live: Round 1's
        7 ties alone added up to 1179 chars in one field), so any list
        whose length scales with how many ties/skips/penalties happen —
        which has no fixed upper bound — has to be able to split itself
        indefinitely rather than assume it'll always fit in one field.
        No-ops if `lines` is empty."""
        if not lines:
            return
        chunk: list[str] = []
        first = True
        for line in lines:
            candidate = chunk + [line]
            if chunk and len('\n'.join(candidate)) > limit:
                embed.add_field(name=name if first else f"{name} (cont.)", value='\n'.join(chunk), inline=False)
                first = False
                chunk = [line]
            else:
                chunk = candidate
        if chunk:
            embed.add_field(name=name if first else f"{name} (cont.)", value='\n'.join(chunk), inline=False)

    @staticmethod
    def _pick_number(round_number: int, team_idx: int, num_teams: int) -> int:
        """Global 1-based pick number across the whole draft (e.g. 30 teams
        -> Round 1 is picks 1-30, Round 2 is 31-60, ...), by each team's
        stable setup-order index — not anything that varies at resolution
        time, so a makeup claimed rounds later still carries the number of
        the round it was actually owed for."""
        return (round_number - 1) * num_teams + team_idx + 1

    # ── Makeup picks (Round 2+ skips — free, uncontested, never expire) ───────
    async def claim_makeup(self, ctx: DraftContext, drafter: dict, text: str) -> str:
        async with draft_lock(ctx.draft_id):
            team_idx = drafter['state'].get('team_index')
            if team_idx is None:
                return "You're not assigned to a team yet."

            state = ctx.state
            team = state['teams'][team_idx]
            owed = team.get('owed_makeups', [])
            if not owed:
                return "You don't owe any makeup picks right now."

            player = text.strip()
            if not player:
                return "Usage: `!makeup <player name>`"
            player, merge = _resolve_submitted_name(player)
            if merge:
                left, right = merge
                return (
                    f"❌ \"{text.strip()}\" looks like two players combined — did you mean **{left}** and "
                    f"**{right}**? Claim one at a time with `!makeup <player name>`."
                )

            already_taken = _taken_players(ctx)
            if _normalize_name(player) in already_taken:
                return f"❌ **{player}** has already been drafted — try someone else."

            makeup_round = owed[0]
            team['owed_makeups'] = owed[1:]
            pick_num = self._pick_number(makeup_round, team_idx, len(state['teams']))

            for mid in team['member_ids']:
                db.record_pick(ctx.draft_id, mid, player, round_number=makeup_round, pick_number=pick_num, auto_pick=False)
            # Fire-and-forget — see the note in _resolve_round; a Sheets
            # quota retry shouldn't make a GM's own !makeup command hang.
            asyncio.create_task(highlight_taken(player))

            # ADP tax, applied immediately (not deferred to the next round
            # resolution) — a makeup claim has no round-wide batch to be
            # compared against, so it's taxed against every other team's
            # own last_tax_adp right away. See the module docstring.
            self._tax_team(state, team_idx, player)

            # Legend penalty (Round 6-10 only) — PERMANENT once set, same
            # as the real-round-resolution version — see
            # _recompute_tax_order, which always forces a penalized team
            # to the very back regardless of ADP, for the rest of the
            # draft.
            legend_penalty = makeup_round in _LEGEND_PENALTY_ROUNDS and _normalize_name(player) in _LEGEND_PENALTY_KEYS
            if legend_penalty:
                team['legend_penalized'] = True

            state['tiebreak_order'] = self._recompute_tax_order(state)

            # Heads-up to anyone else whose CURRENT pending list named this
            # player — resolution already re-checks who's actually taken at
            # the time it runs, so this can never cause a double-draft; it's
            # purely a courtesy notice, and we trim it from their stored
            # list now so a `!status` check in the meantime isn't stale.
            key = _normalize_name(player)
            lists = state.get('lists', {})
            affected: list[tuple[dict, int]] = []
            for other in state['teams']:
                if other['index'] == team_idx:
                    continue
                lst = lists.get(str(other['index']))
                if not lst or not any(_normalize_name(p) == key for p in lst):
                    continue
                new_list = [p for p in lst if _normalize_name(p) != key]
                lists[str(other['index'])] = new_list
                affected.append((other, len(new_list)))

            ctx.save_state(state)

            channel = await _results_channel(ctx)
            if channel:
                await channel.send(f"{pick_num}. {team['logo']} {_with_default_year(player)}")
                penalty_note = " ⚠️ Locked at the very back of the waiver order for the rest of the draft." if legend_penalty else ''
                await channel.send(f"📋 <@{drafter['user_id']}> claimed pick {pick_num} (Round {makeup_round} makeup): **{player}**.{penalty_note}")

            for other, remaining_count in affected:
                tail = (
                    "your list for this round is now empty — send a fresh `!list`."
                    if remaining_count == 0 else
                    f"{remaining_count} player(s) left on it. Send a new `!list` to replace it, or leave it as-is."
                )
                for mid in other['member_ids']:
                    mate = db.get_drafter(mid)
                    await ctx.dm(
                        mate['user_id'],
                        f"⚠️ **{player}** was just claimed as a makeup pick by another team — removed from your "
                        f"current list; {tail}",
                    )

            waiver_pos = state['tiebreak_order'].index(team_idx) + 1
            penalty_suffix = " ⚠️ You're now locked at the very back of the waiver order for the rest of the draft." if legend_penalty else ''
            return (
                f"✅ Claimed **{player}** as pick {pick_num} (your Round {makeup_round} makeup pick). "
                f"Waiver order updated immediately — you're now #{waiver_pos}/{len(state['teams'])}.{penalty_suffix}"
            )

    # ── Status / misc ────────────────────────────────────────────────────────
    async def on_status(self, ctx: DraftContext, drafter: dict) -> str:
        settings = self._settings(ctx)
        team_idx = drafter['state'].get('team_index')
        if team_idx is None:
            return "You're not assigned to a team yet."
        state = ctx.state
        team = state['teams'][team_idx]
        owed = team.get('owed_makeups', [])
        owed_line = (
            f" You owe {len(owed)} makeup pick(s) (Round(s) {', '.join(map(str, owed))}) — DM `!makeup <player>` to claim one."
            if owed else ''
        )
        queued = team.get('future_lists', {})
        queued_line = (
            f" Queued future list(s) for Round(s): {', '.join(sorted(queued, key=int))}."
            if queued else ''
        )
        round_number = state.get('round_index', 0) + 1
        if state.get('round_index', 0) >= settings['rounds']:
            return f"Auto Draft has finished all its rounds.{owed_line}{queued_line}"
        submitted = str(team_idx) in state.get('lists', {})
        picks = len(db.get_picks(ctx.draft_id, drafter['id']))
        waiver_pos = state.get('tiebreak_order', []).index(team_idx) + 1 if team_idx in state.get('tiebreak_order', []) else '?'
        return (
            f"Round {round_number}/{settings['rounds']}. Your list this round: "
            f"{'submitted' if submitted else 'not submitted yet'}. "
            f"Picks so far: {picks}. Tiebreaker waiver position: {waiver_pos}/{len(state.get('teams', []))}."
            f"{owed_line}{queued_line}\n"
            "-# Why: your position reflects the ADP of your most recent pick (round win, Round 1 pool "
            "fallback, or makeup claim) — better player = further back, updated the instant it happens. "
            "Never having a pick yet keeps you at the front."
        )

    async def check_chances(self, ctx: DraftContext, team_ref: str, player: str) -> str:
        player = player.strip()
        if not player:
            return "Usage: `!theme chances <id> <team#/logo/@GM> <player name>`"

        state = ctx.state
        teams = state.get('teams', [])
        team_idx = self._resolve_team_ref(teams, team_ref)
        if team_idx is None:
            return f"❌ Couldn't find a team matching '{team_ref}' (use its number from `!theme teams`, its exact logo, or a GM mention)."

        current_lists = state.get('lists', {})
        my_list = current_lists.get(str(team_idx))
        if not my_list:
            return "That team hasn't submitted a list for this round yet — no chances to check until they do."

        by_team = {t['index']: t for t in teams}
        team_logo = by_team[team_idx]['logo']

        key = _normalize_name(player)
        my_rank = next((i + 1 for i, p in enumerate(my_list) if _normalize_name(p) == key), None)
        if my_rank is None:
            return f"❌ **{player}** isn't on {team_logo}'s current list."

        contenders: list[tuple[int, int]] = []
        for t_idx_str, lst in current_lists.items():
            t_idx = int(t_idx_str)
            if t_idx == team_idx:
                continue
            for i, p in enumerate(lst):
                if _normalize_name(p) == key:
                    contenders.append((t_idx, i + 1))
                    break

        caveat = " (based on current submissions only — not everyone's in yet, and lists can still change)"

        if not contenders:
            return f"✅ {team_logo} has **{player}** at #{my_rank} and nobody else currently has them listed — uncontested.{caveat}"

        best_other_rank = min(r for _, r in contenders)
        if my_rank < best_other_rank:
            others = ', '.join(f"{by_team[t]['logo']} (#{r})" for t, r in contenders)
            return f"✅ {team_logo} would win **{player}** outright — their #{my_rank} beats everyone else currently on it: {others}.{caveat}"

        if my_rank > best_other_rank:
            beaters = ', '.join(f"{by_team[t]['logo']} (#{r})" for t, r in contenders if r == best_other_rank)
            return f"❌ {team_logo} would lose **{player}** — {beaters} rank them higher (#{best_other_rank} vs {team_logo}'s #{my_rank}).{caveat}"

        # Tied at the same rank — resolved by the current waiver order.
        tied_with = [t for t, r in contenders if r == my_rank]
        order = state.get('tiebreak_order', [t['index'] for t in teams])
        winner = min([team_idx] + tied_with, key=lambda t: order.index(t) if t in order else len(order))
        tied_logos = ', '.join(by_team[t]['logo'] for t in tied_with)
        return (
            f"⚖️ {team_logo} is tied at #{my_rank} with {tied_logos} — current waiver order has "
            f"{by_team[winner]['logo']} winning the tiebreak.{caveat}"
        )

    # ── Admin ops (team listing / round nudge / early close) ────────────────
    async def add_teammate(self, ctx: DraftContext, existing_user_id: int, new_user_id: int) -> str:
        async with draft_lock(ctx.draft_id):
            state = ctx.state
            teams = state.get('teams', [])
            existing = db.get_drafter_by_user(ctx.draft_id, existing_user_id)
            team_idx = existing['state'].get('team_index') if existing else None
            if team_idx is None:
                return f"❌ <@{existing_user_id}> isn't on a team in this draft."

            team = teams[team_idx]
            if len(team['member_ids']) >= 3:
                return f"❌ {team['logo']} already has 3 GMs — that's the max for a duo/trio."
            if any(db.get_drafter(mid)['user_id'] == new_user_id for mid in team['member_ids']):
                return f"❌ <@{new_user_id}> is already on {team['logo']}."
            for other in teams:
                if other['index'] != team_idx and any(db.get_drafter(mid)['user_id'] == new_user_id for mid in other['member_ids']):
                    return f"❌ <@{new_user_id}> is already on {other['logo']}."

            new_drafter = db.get_drafter_by_user(ctx.draft_id, new_user_id)
            if new_drafter:
                new_id = new_drafter['id']
            else:
                display_name = str(new_user_id)
                try:
                    user = ctx.bot.get_user(new_user_id) or await ctx.bot.fetch_user(new_user_id)
                    if user:
                        display_name = getattr(user, 'display_name', None) or user.name
                except Exception:
                    pass
                new_id = db.add_drafter(ctx.draft_id, new_user_id, display_name)

            team['member_ids'].append(new_id)
            db.update_drafter_state(new_id, {'team_index': team_idx})
            ctx.save_state(state)

            # Back-fill everything the team has already drafted onto the
            # new member's OWN picks row — !mypicks/!status key off each
            # drafter's individual id, not the team, so without this a
            # mid-draft add would show them a blank history for every
            # round before they joined even though their team drafted
            # plenty. Round/pick numbers aren't preserved on the copy
            # (only the player names) since nothing downstream reads
            # those off an individual's own row anyway.
            for player in db.get_picks(ctx.draft_id, team['member_ids'][0]):
                db.record_pick(ctx.draft_id, new_id, player, round_number=None, pick_number=None, auto_pick=False)

            other_mentions = ', '.join(
                f"<@{db.get_drafter(mid)['user_id']}>" for mid in team['member_ids'] if mid != new_id
            )
            await ctx.dm(
                new_user_id,
                f"🏀 You've been added to {team['logo']} in Auto Draft, alongside "
                f"{other_mentions}. `!mypicks` shows everything your team has drafted so far.",
            )
            return f"✅ <@{new_user_id}> added to {team['logo']} (now {len(team['member_ids'])} GM(s))."

    async def list_teams(self, ctx: DraftContext) -> str:
        teams = ctx.state.get('teams', [])
        if not teams:
            return "No teams registered yet — DM me `!setteams` first."

        lines = []
        for team in teams:
            mentions = ', '.join(f"<@{db.get_drafter(mid)['user_id']}>" for mid in team['member_ids'])
            extras = []
            if team.get('owed_makeups'):
                extras.append(f"owes Round(s) {', '.join(map(str, team['owed_makeups']))}")
            if team.get('future_lists'):
                extras.append(f"queued Round(s) {', '.join(sorted(team['future_lists'], key=int))}")
            extra_text = f" ({'; '.join(extras)})" if extras else ''
            lines.append(f"{team['index'] + 1}. {team['logo']} — {mentions}{extra_text}")

        return f"**{len(teams)} team(s):**\n" + '\n'.join(lines)

    async def current_round_lists(self, ctx: DraftContext, round_number: int | None = None) -> str:
        """With no round_number, shows the currently-open round's lists
        as they're submitted. With one, shows that round specifically —
        live if it's the open round, or from round_history (see
        _resolve_round) if it already resolved. History only exists for
        rounds resolved after that tracking was added, so an older round
        (e.g. this draft's Round 1) may come back unrecoverable."""
        settings = self._settings(ctx)
        state = ctx.state
        teams = state.get('teams', [])
        by_team = {t['index']: t for t in teams}
        history_all = state.get('round_history', {})
        current_round = state.get('round_index', 0) + 1

        if round_number is None:
            if state.get('round_index', 0) >= settings['rounds']:
                return "Auto Draft has finished all its rounds."
            round_number = current_round

        if round_number < 1:
            return "❌ Round numbers start at 1."

        is_live_open_round = (
            round_number == current_round
            and state.get('round_index', 0) < settings['rounds']
            and str(round_number) not in history_all
        )
        if is_live_open_round:
            lists = state.get('lists', {})
            lines = [f"**Round {round_number} lists so far (still open):**"]
            for team in teams:
                submitted = lists.get(str(team['index']))
                if submitted:
                    lines.append(f"{team['logo']} — {', '.join(submitted)}")
                else:
                    lines.append(f"{team['logo']} — *(not submitted yet)*")
            return '\n'.join(lines)

        history = history_all.get(str(round_number))
        if history is None:
            if round_number > current_round:
                return f"❌ Round {round_number} hasn't happened yet — the draft is currently on Round {current_round}."
            return (
                f"⚠️ No saved history for Round {round_number} — it resolved before round-history tracking "
                "was added, so the original lists aren't recoverable."
            )

        return self._format_resolved_round(round_number, history, teams, by_team)

    @staticmethod
    def _format_resolved_round(round_number: int, history: dict, teams: list[dict], by_team: dict[int, dict]) -> str:
        """Shared by current_round_lists (one round) and full_history (every
        round concatenated) — every team's submitted list and outcome for
        one already-resolved round, from its round_history entry."""
        lines = [f"**Round {round_number} lists (resolved):**"]
        auto_assigned = set(history.get('auto_assigned_teams', []))
        struck_out = set(history.get('struck_out_teams', []))
        skipped = set(history.get('skipped_teams', []))
        assigned = history.get('assigned', {})
        for team in teams:
            idx = team['index']
            submitted = history.get('lists', {}).get(str(idx), [])
            submitted_text = ', '.join(submitted) if submitted else '*(no list submitted)*'
            if idx in auto_assigned:
                outcome = f"→ auto-assigned **{assigned.get(str(idx), '?')}** (Round 1 pool fallback)"
            elif idx in struck_out:
                outcome = "→ struck out (every name taken/lost a tiebreak) — owed a makeup pick"
            elif idx in skipped:
                outcome = "→ skipped (no list submitted) — owed a makeup pick"
            elif str(idx) in assigned:
                outcome = f"→ got **{assigned[str(idx)]}**"
            else:
                outcome = "→ *(no pick)*"
            lines.append(f"{team['logo']} — {submitted_text} {outcome}")

        if history.get('tie_events'):
            lines.append('')
            lines.append('**Ties:**')
            for player, tied, winner in history['tie_events']:
                tied_logos = ', '.join(by_team[t]['logo'] for t in tied if t in by_team)
                winner_logo = by_team[winner]['logo'] if winner in by_team else '?'
                lines.append(f"⚖️ {player} — {tied_logos} tied, {winner_logo} had higher waiver priority.")

        if history.get('legend_penalties'):
            lines.append('')
            lines.append('**Legend penalty applied:**')
            for idx, player in history['legend_penalties']:
                logo = by_team[idx]['logo'] if idx in by_team else '?'
                lines.append(f"🏀 {logo} drafted {player} — locked at the very back of the waiver order for the rest of the draft.")

        return '\n'.join(lines)

    async def full_history(self, ctx: DraftContext) -> str:
        """`!theme history <id>` — every team's submitted list and outcome
        for EVERY round resolved so far, all in one place, round by round.
        Reads straight from round_history (see _resolve_round), so it
        always reflects whatever has actually resolved up to the moment
        it's called — nothing to "refresh," running it again after the
        next round resolves just shows one more round. Rounds with no
        saved history (resolved before that tracking existed) are skipped
        with a note rather than silently omitted."""
        state = ctx.state
        teams = state.get('teams', [])
        if not teams:
            return "No teams registered yet — DM me `!setteams` first."
        by_team = {t['index']: t for t in teams}
        history_all = state.get('round_history', {})
        if not history_all:
            return "No rounds have resolved yet — nothing to compile."

        sections = []
        missing_rounds = []
        current_round = state.get('round_index', 0) + 1
        for round_number in range(1, current_round):
            history = history_all.get(str(round_number))
            if history is None:
                missing_rounds.append(round_number)
                continue
            sections.append(self._format_resolved_round(round_number, history, teams, by_team))

        if missing_rounds:
            sections.insert(0, (
                f"⚠️ No saved history for Round(s) {', '.join(map(str, missing_rounds))} — resolved before "
                "round-history tracking existed, so those lists aren't recoverable."
            ))

        if not sections:
            return "No rounds have resolved yet — nothing to compile."

        return '\n\n'.join(sections)

    async def show_waiver_order(self, ctx: DraftContext) -> str:
        state = ctx.state
        teams = state.get('teams', [])
        if not teams:
            return "No teams registered yet — DM me `!setteams` first."
        by_team = {t['index']: t for t in teams}
        order = state.get('tiebreak_order', [t['index'] for t in teams])
        lines = []
        for i, idx in enumerate(order):
            team = by_team[idx]
            mentions = ', '.join(f"<@{db.get_drafter(mid)['user_id']}>" for mid in team['member_ids'])
            lines.append(f"{i + 1}. {team['logo']} — {mentions}")
        return "**Tiebreaker waiver order (1st to last):**\n" + '\n'.join(lines)

    @staticmethod
    def _demand_info(name: str, current_lists: dict) -> tuple[int, int | None]:
        """(how many teams currently have `name` anywhere on their
        not-yet-resolved list, the best/lowest 1-indexed rank among them —
        None if nobody has it)."""
        key = _normalize_name(name)
        count = 0
        best_rank = None
        for lst in current_lists.values():
            for idx, p in enumerate(lst):
                if _normalize_name(p) == key:
                    count += 1
                    rank = idx + 1
                    if best_rank is None or rank < best_rank:
                        best_rank = rank
                    break  # a team's own list never repeats a name
        return count, best_rank

    async def available_players(self, ctx: DraftContext) -> tuple[str, list[str]]:
        """Returns (header, all_lines) — unpaginated; bot.py chunks these
        into 20-per-page embeds with a Previous/Next button pager."""
        settings = self._settings(ctx)
        state = ctx.state
        round_index = state.get('round_index', 0)
        if round_index >= settings['rounds']:
            return "Auto Draft has finished all its rounds.", []

        round_number = round_index + 1
        already_taken = _taken_players(ctx)
        current_lists = state.get('lists', {})

        def demand_suffix(name: str) -> str:
            count, best_rank = self._demand_info(name, current_lists)
            if count == 0:
                return "on nobody's list"
            return f"{count} GM(s) have them, best at #{best_rank}"

        if round_number == 1:
            # Round 1's closed 30-player pool makes "how many teams want
            # this one" a meaningful, readable signal — least-wanted (most
            # overlooked) first, so a 0-team entry is literally "on
            # nobody's list."
            pool_available = [n for n in _ROUND1_POOL if _normalize_name(n) not in already_taken]
            demand: dict[str, int] = {_normalize_name(n): 0 for n in pool_available}
            for lst in current_lists.values():
                for p in lst:
                    key = _normalize_name(p)
                    if key in demand:
                        demand[key] += 1
            ordered = sorted(pool_available, key=lambda n: (demand[_normalize_name(n)], n))
            all_lines = [f"{i + 1}. {name} — {demand_suffix(name)}" for i, name in enumerate(ordered)]
            header = "Round 1 — pool players still available (fewest teams listing them first)"
        else:
            # Round 2+: the pool is the whole ADP sheet, not a fixed 30 —
            # with that many names, "how many teams want it" is rarely
            # meaningful (lists only hold 10 slots each), so this ranks by
            # ADP instead: best remaining talent first.
            available = [n for n in ADP if _normalize_name(n) not in already_taken]
            ordered = sorted(available, key=lambda n: _ADP_KEYS.get(_normalize_name(n), _UNRANKED_ADP))
            all_lines = [f"{i + 1}. {name} (ADP {ADP.get(name, '—')}) — {demand_suffix(name)}" for i, name in enumerate(ordered)]
            header = f"Round {round_number} — available players (by ADP)"

        return header, all_lines

    async def set_waiver_order(self, ctx: DraftContext, text: str) -> str:
        state = ctx.state
        teams = state.get('teams', [])
        if not teams:
            return "No teams registered yet — DM me `!setteams` first."

        parsed = lotto.parse_team_lines(text)
        if not parsed:
            return (
                "❌ No valid lines found. Format: `1. 🦅 - @GM1`, one team per line, in the new waiver order "
                "(logo or GM mention, either resolves to the right team)."
            )

        user_to_team: dict[int, int] = {}
        for team in teams:
            for mid in team['member_ids']:
                d = db.get_drafter(mid)
                if d:
                    user_to_team[d['user_id']] = team['index']

        new_order: list[int] = []
        seen: set[int] = set()
        errors: list[str] = []
        for entry in parsed:
            matched_idx = next((user_to_team[uid] for uid in entry['user_ids'] if uid in user_to_team), None)
            if matched_idx is None:
                matched_idx = next((t['index'] for t in teams if t['logo'] == entry['logo']), None)
            if matched_idx is None:
                errors.append(f"line {entry['order_index'] + 1}: couldn't match a registered team")
            elif matched_idx in seen:
                errors.append(f"line {entry['order_index'] + 1}: {teams[matched_idx]['logo']} listed more than once")
            else:
                seen.add(matched_idx)
                new_order.append(matched_idx)

        if errors:
            return "❌ " + '; '.join(errors)
        if len(new_order) != len(teams):
            missing = [t['logo'] for t in teams if t['index'] not in seen]
            return f"❌ Missing from the list: {', '.join(missing)}. Every team must appear exactly once."

        state['tiebreak_order'] = new_order
        ctx.save_state(state)
        order_text = '\n'.join(f"{i + 1}. {teams[idx]['logo']}" for i, idx in enumerate(new_order))
        return f"✅ Waiver order updated:\n{order_text}"

    @staticmethod
    def _resolve_team_ref(teams: list[dict], ref: str) -> int | None:
        ref = ref.strip()
        if ref.isdigit():
            idx = int(ref) - 1
            return idx if 0 <= idx < len(teams) else None
        m = re.match(r'^<@!?(\d+)>$', ref)
        if m:
            uid = int(m.group(1))
            for team in teams:
                for mid in team['member_ids']:
                    d = db.get_drafter(mid)
                    if d and d['user_id'] == uid:
                        return team['index']
            return None
        for team in teams:
            if team['logo'] == ref:
                return team['index']
        return None

    async def admin_submit_list(self, ctx: DraftContext, team_ref: str, text: str) -> str:
        teams = ctx.state.get('teams', [])
        team_idx = self._resolve_team_ref(teams, team_ref)
        if team_idx is None:
            return f"❌ Couldn't find a team matching '{team_ref}' (use its number from `!theme teams`, its exact logo, or a GM mention)."
        team = teams[team_idx]
        if not team['member_ids']:
            return "❌ That team has no registered members."
        # Routed through the real on_list — identical validation (Round 1
        # pool, reject-on-any-invalid-name, etc.) and the usual submission-
        # progress ping, just attributed to one of the team's own GMs
        # instead of whoever typed this admin command.
        drafter = db.get_drafter(team['member_ids'][0])
        return await self.on_list(ctx, drafter, text)

    async def admin_claim_makeup(self, ctx: DraftContext, team_ref: str, text: str) -> str:
        teams = ctx.state.get('teams', [])
        team_idx = self._resolve_team_ref(teams, team_ref)
        if team_idx is None:
            return f"❌ Couldn't find a team matching '{team_ref}' (use its number from `!theme teams`, its exact logo, or a GM mention)."
        team = teams[team_idx]
        if not team['member_ids']:
            return "❌ That team has no registered members."
        # Routed through the real claim_makeup — identical validation
        # (owed check, already-taken check, immediate ADP tax, etc.) and
        # the usual channel/DM notifications, just attributed to one of
        # the team's own GMs instead of whoever typed this admin command.
        drafter = db.get_drafter(team['member_ids'][0])
        return await self.claim_makeup(ctx, drafter, text)

    async def nudge_missing(self, ctx: DraftContext) -> str:
        settings = self._settings(ctx)
        state = ctx.state
        if state.get('round_index', 0) >= settings['rounds']:
            return "Auto Draft has already finished all its rounds."

        round_number = state['round_index'] + 1
        lists = state.get('lists', {})
        deadline = state.get('round_deadline')
        time_left = f" Time left: <t:{int(deadline)}:R>" if deadline else ''
        missing = [team for team in state['teams'] if str(team['index']) not in lists]
        if not missing:
            return "Everyone's already submitted for this round — nothing to nudge."

        for team in missing:
            for mid in team['member_ids']:
                drafter = db.get_drafter(mid)
                await ctx.dm(
                    drafter['user_id'],
                    f"⏰ Reminder — Round {round_number} is still waiting on your team's list. "
                    f"DM me `!list P1, P2, ...` when ready.{time_left}",
                )
        return f"✅ Nudged {len(missing)} team(s) still missing a Round {round_number} list."

    async def close_window(self, ctx: DraftContext) -> str:
        async with draft_lock(ctx.draft_id):
            settings = self._settings(ctx)
            state = ctx.state
            if state.get('round_index', 0) >= settings['rounds']:
                return "Auto Draft has already finished all its rounds."
            round_number = state['round_index'] + 1
            _timer.cancel(ctx.draft_id)
            await self._resolve_round(ctx, forced=True)
            return f"✅ Round {round_number}'s window closed early — resolved with whatever had been submitted."

    async def extend_round(self, ctx: DraftContext, extra_minutes: float) -> str:
        async with draft_lock(ctx.draft_id):
            settings = self._settings(ctx)
            state = ctx.state
            if state.get('round_index', 0) >= settings['rounds']:
                return "Auto Draft has already finished all its rounds."
            deadline = state.get('round_deadline')
            if deadline is None:
                return "❌ No round is currently open with a running timer."
            # Adds to whatever time is ACTUALLY left, not a fresh full
            # clock — restarting from `response_deadline_minutes` would
            # silently discard however much of the current window had
            # already elapsed.
            remaining_minutes = max(0, deadline - time.time()) / 60
            new_minutes = remaining_minutes + extra_minutes
            self._start_round_timer(ctx, state, new_minutes)
            round_number = state['round_index'] + 1
            new_deadline = int(state['round_deadline'])
            channel = await _results_channel(ctx)
            if channel:
                await channel.send(
                    f"⏰ Round {round_number}'s deadline was extended by {extra_minutes:.0f} minutes — "
                    f"now ends <t:{new_deadline}:R>."
                )
            return f"✅ Round {round_number}'s timer extended by {extra_minutes:.0f} minutes — now ends <t:{new_deadline}:R>."

    async def on_timeout(self, ctx: DraftContext, drafter: dict) -> None:
        # Resolution is round-wide (see _resolve_round's own shared timer),
        # not per-drafter — nothing to do via this generic hook.
        return

    def is_pick_phase_complete(self, ctx: DraftContext, drafter: dict | None = None) -> bool:
        settings = self._settings(ctx)
        return ctx.state.get('round_index', 0) >= settings['rounds']

    # ── Team grouping (1-3 drafters share 1 team) ───────────────────────────
    def team_key(self, ctx: DraftContext, drafter: dict) -> str:
        idx = drafter['state'].get('team_index')
        return f'auto-team-{idx}' if idx is not None else str(drafter['id'])

    def team_label(self, ctx: DraftContext, drafter: dict) -> str:
        idx = drafter['state'].get('team_index')
        if idx is None:
            return drafter['display_name'] or str(drafter['user_id'])
        return ctx.state['teams'][idx]['logo']

    def linked_drafter_ids(self, ctx: DraftContext, drafter: dict) -> list[int]:
        idx = drafter['state'].get('team_index')
        if idx is None:
            return [drafter['id']]
        return list(ctx.state['teams'][idx]['member_ids'])

    # ── Helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _parse_list(text: str, max_size: int) -> list[str] | None:
        parts = [p.strip() for p in text.replace('\n', ',').split(',') if p.strip()]
        if not (1 <= len(parts) <= max_size):
            return None
        if len({p.lower() for p in parts}) != len(parts):
            return None
        return parts

    # ── Timer lifecycle (restart-safety) ────────────────────────────────────
    async def cancel(self, ctx: DraftContext) -> None:
        state = ctx.state
        state['round_deadline'] = None
        ctx.save_state(state)
        _timer.cancel(ctx.draft_id)

    async def resume_timers(self, ctx: DraftContext) -> int:
        settings = self._settings(ctx)
        state = ctx.state
        if state.get('round_index', 0) >= settings['rounds']:
            return 0
        if _timer.is_running(ctx.draft_id):
            return 0

        deadline = state.get('round_deadline')
        teams = state.get('teams', [])
        if deadline is None:
            self._start_round_timer(ctx, ctx.state, settings['response_deadline_minutes'])
            return 1

        # No "reconnected" DM to every GM here on purpose — this fires on
        # every gateway reconnect AND every process restart (a routine
        # deploy included), not just a genuine extended outage, so it was
        # spamming everyone far more often than it was actually useful.
        # The timer itself still re-arms (or resolves immediately if the
        # deadline already passed) exactly as before — only the
        # notification is gone.
        remaining = deadline - time.time()
        if remaining <= 0:
            async with draft_lock(ctx.draft_id):
                await self._resolve_round(ctx, forced=True)
        else:
            minutes = remaining / 60
            self._start_round_timer(ctx, ctx.state, minutes)
        return 1
