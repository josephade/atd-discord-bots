# bot.py — ATD Draft Theme Bot
#
# Generic DM-drafting engine. All theme-specific rules (Pack Draft,
# Anonymous Partner Draft, ...) live in themes/ — this file only routes
# admin commands (run in a server channel) and drafter commands (run in DM)
# to whichever Theme is attached to a given draft. See themes/base.py for
# the plugin interface, and themes/__init__.py to register a new format.

import asyncio
import logging
import re
import time
from datetime import datetime
from datetime import time as dt_time
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

import db
import lotto
import roster
import sheets
import themes
from config import DISCORD_TOKEN, OWNER_ID, ROSTER_SPREADSHEET_ID, ROSTER_TAB_NAME
from emoji_map import EMOJI_TEAM_MAP
from themes.base import DraftContext

# !theme reveal <draft> <emoji> (skip-count / position-breakdown lookup) is
# only meaningful in the main ATD server — restricted here rather than via
# has_permissions, since that's already satisfied by any admin in any guild
# the bot happens to be in.
_STATS_LOOKUP_GUILD_ID = 426899646082711563

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

db.init_db()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)


# ── Roster submission deadline (!theme rostertimer) ─────────────────────────────
# In-memory only, like the pick timers themes/*.py keep — resumed from the
# persisted draft['state']['roster_deadline'] on reconnect, same pattern as
# Theme.resume_timers().
_roster_timers: dict[int, asyncio.Task] = {}


# ── Overnight draft window (Eastern Time) ────────────────────────────────────
# Mirrors ATD Timer Bot's own midnight-close / 10 AM-open window, but applied
# here as a full draft pause/resume rather than a per-timer delay — GMs
# shouldn't be expected to pick between midnight and 10 AM ET. Only drafts
# this job itself paused (state['night_paused']) get auto-resumed; a draft
# the commish paused manually (!pausepackdraft / !theme pause) is left alone
# until they resume it themselves.
_ET = ZoneInfo('America/New_York')


async def _apply_night_pause(draft: dict):
    theme = themes.get_theme(draft['theme'])
    draft_ctx = DraftContext(bot, draft['id'])
    try:
        db.set_draft_status(draft['id'], 'paused')
        db.update_draft_state(draft['id'], {'night_paused': True})
        await theme.cancel(draft_ctx)
        await draft_ctx.announce(
            '🌙 **Draft window closed** (midnight ET) — auto-paused. Resumes automatically at **10:00 AM ET**.'
        )
    except Exception:
        log.exception('_apply_night_pause | failed for draft_id=%s', draft['id'])


async def _apply_morning_resume(draft: dict):
    theme = themes.get_theme(draft['theme'])
    draft_ctx = DraftContext(bot, draft['id'])
    try:
        db.set_draft_status(draft['id'], 'active')
        db.update_draft_state(draft['id'], {'night_paused': False})
        resumed = await theme.resume_timers(draft_ctx)
        await draft_ctx.announce(
            f'☀️ **Draft window open** (10:00 AM ET) — auto-resumed, {resumed} timer(s) restarted.'
        )
    except Exception:
        log.exception('_apply_morning_resume | failed for draft_id=%s', draft['id'])


@tasks.loop(time=dt_time(hour=0, minute=0, tzinfo=_ET))
async def _nightly_pause():
    for draft in db.list_drafts(status='active'):
        await _apply_night_pause(draft)


@tasks.loop(time=dt_time(hour=10, minute=0, tzinfo=_ET))
async def _morning_resume():
    for draft in db.list_drafts(status='paused'):
        if draft['state'].get('night_paused'):
            await _apply_morning_resume(draft)


def _validate_submission(theme, draft_ctx, drafter: dict, picks: list[str], submitted: list[str] | None,
                          price_lookup, budget) -> tuple[bool, list[str]]:
    if not submitted:
        return False, ["nothing to submit"]
    extra_allowed = theme.filler_player_names(draft_ctx)
    ok, errors = roster.validate_roster(picks, submitted, price_lookup, budget, extra_allowed)
    if ok:
        ok, errors = theme.validate_final_roster(draft_ctx, drafter, submitted)
    return ok, errors


async def _attempt_auto_submit(draft: dict, drafter: dict) -> tuple[bool, str]:
    """Tries to submit whatever this drafter currently has arranged (same
    as !submitroster run with no arguments). If that doesn't validate (most
    commonly: over budget), falls back to the theme's best-effort optimizer
    (e.g. Pack Draft builds the highest-value legal roster from their picks
    that still fits) before giving up. Returns (submitted, detail) — detail
    is either a description of what got submitted, or why nothing could be."""
    theme = themes.get_theme(draft['theme'])
    draft_ctx = DraftContext(bot, draft['id'])
    picks = db.get_picks(draft['id'], drafter['id'])
    price_lookup, budget = theme.roster_requirements(draft_ctx)

    as_arranged = theme.current_roster_list(draft_ctx, drafter)
    ok, errors = _validate_submission(theme, draft_ctx, drafter, picks, as_arranged, price_lookup, budget)
    label = "as arranged"

    if not ok:
        arranged_errors = errors
        optimized = theme.best_affordable_roster(draft_ctx, drafter)
        ok, errors = _validate_submission(theme, draft_ctx, drafter, picks, optimized, price_lookup, budget)
        if ok:
            as_arranged = optimized
            label = "auto-optimized for budget"
        else:
            return False, '; '.join(arranged_errors)

    for linked_id in theme.linked_drafter_ids(draft_ctx, drafter):
        db.set_roster_submitted(linked_id, True)
        db.update_drafter_state(linked_id, {'final_roster': as_arranged})
    await theme.on_roster_submitted(draft_ctx, drafter)
    return True, f"{len(as_arranged)} players ({label})"


async def _resolve_roster_deadline(draft_id: int):
    """Fires once the roster deadline passes — auto-submits whatever's
    arranged (if valid) for every team that hasn't submitted yet, notifies
    each affected GM, and posts a summary to the admin channel."""
    draft = db.get_draft(draft_id)
    if not draft or draft['status'] != 'active':
        return
    theme = themes.get_theme(draft['theme'])
    draft_ctx = DraftContext(bot, draft_id)

    seen_keys: set[str] = set()
    auto_submitted: list[str] = []
    still_missing: list[str] = []
    for d in db.list_drafters(draft_id):
        key = theme.team_key(draft_ctx, d)
        if key in seen_keys or d['roster_submitted']:
            continue
        seen_keys.add(key)
        label = theme.team_label(draft_ctx, d)
        ok, detail = await _attempt_auto_submit(draft, d)
        if ok:
            auto_submitted.append(f"{label} ({detail})")
            dm_text = f"⏰ Roster deadline hit — your team's roster was auto-submitted as-is ({detail})."
        else:
            still_missing.append(f"{label} — {detail}")
            dm_text = f"⏰ Roster deadline hit and your team still isn't submitted ({detail}). Submit ASAP: `!submitroster`."
        for linked_id in theme.linked_drafter_ids(draft_ctx, d):
            linked = db.get_drafter(linked_id)
            if linked:
                await draft_ctx.dm(linked['user_id'], dm_text)

    state = db.get_draft(draft_id)['state']
    state.pop('roster_deadline', None)
    db.set_draft_state(draft_id, state)
    _roster_timers.pop(draft_id, None)

    admin_channel_id = draft.get('admin_channel_id')
    if admin_channel_id:
        channel = bot.get_channel(admin_channel_id)
        if channel:
            lines = [f"⏰ **Roster deadline reached for {draft['name']}.**"]
            if auto_submitted:
                lines.append("✅ Auto-submitted: " + ', '.join(auto_submitted))
            if still_missing:
                lines.append("❌ Still missing: " + ', '.join(still_missing))
            if not auto_submitted and not still_missing:
                lines.append("Everyone had already submitted.")
            await channel.send('\n'.join(lines))


async def _start_roster_timer(draft_id: int, minutes: float):
    existing = _roster_timers.pop(draft_id, None)
    if existing and not existing.done():
        existing.cancel()

    async def _run():
        try:
            await asyncio.sleep(minutes * 60)
        except asyncio.CancelledError:
            return
        try:
            await _resolve_roster_deadline(draft_id)
        except Exception:
            log.exception('Roster deadline resolution failed for draft_id=%s', draft_id)

    _roster_timers[draft_id] = asyncio.create_task(_run())


# ── Shared lookups ────────────────────────────────────────────────────────────
def _dm_only(ctx) -> bool:
    return isinstance(ctx.channel, discord.DMChannel)


def _not_active_message(draft: dict) -> str:
    """Reply for a picking command (!pick / !list / !msg) blocked because
    the draft isn't 'active' — called out by name (the overnight window)
    when that's why, instead of a bare status string a GM has to guess at.
    Non-picking commands (!swap, !myteam, !mypicks, !add, !pack, ...) have
    no such gate and keep working through a pause either way."""
    if draft['status'] == 'paused' and draft['state'].get('night_paused'):
        return (
            "🌙 The draft window is closed for the night — picking resumes automatically at **10:00 AM ET**. "
            "You can still use `!swap`, `!myteam`, `!mypicks`, etc. in the meantime."
        )
    return f"This draft is currently `{draft['status']}`."


def _resolve_draft(arg: str) -> dict | None:
    arg = arg.strip()
    if arg.isdigit():
        draft = db.get_draft(int(arg))
        if draft:
            return draft
    return db.get_draft_by_name(arg)


def _resolve_active_drafter(user_id: int) -> tuple[dict, dict] | None:
    """Returns (draft, drafter) for the user's current draft. If they're
    somehow in more than one active draft at once, picks the most recent —
    proper multi-draft session switching (`!use <id>`) isn't implemented yet."""
    candidates = db.get_active_drafters_for_user(user_id)
    if not candidates:
        return None
    drafter = max(candidates, key=lambda d: d['draft_id'])
    return db.get_draft(drafter['draft_id']), drafter


def _resolve_draft_for_channel(channel_id: int) -> dict | None:
    """Most recent draft whose admin channel is this one — lets the
    zero-argument moderator commands (!startpackdraft, !generatepacks, ...)
    work without needing a draft id every time. list_drafts() is newest-first."""
    for d in db.list_drafts():
        if d.get('admin_channel_id') == channel_id:
            return d
    return None


def _parse_settings_kv(text: str) -> dict:
    out = {}
    for token in text.split():
        if '=' not in token:
            continue
        key, _, val = token.partition('=')
        val = val.strip()
        try:
            out[key.strip()] = int(val)
        except ValueError:
            try:
                out[key.strip()] = float(val)
            except ValueError:
                out[key.strip()] = val
    return out


# ── Admin commands (server channel, administrator only) ────────────────────────
@bot.command(name='theme')
@commands.has_permissions(administrator=True)
async def cmd_theme(ctx, action: str = '', *, rest: str = ''):
    """!theme <list|drafts|create|adddrafter|settings|start|status|pause|resume|reveal> ..."""
    action = action.lower()

    if action == 'list':
        lines = [f"• `{key}` — {t.display_name}" for key, t in themes.REGISTRY.items()]
        await ctx.send('**Available themes:**\n' + '\n'.join(lines))
        return

    if action == 'drafts':
        guild_id = ctx.guild.id if ctx.guild else None
        found = db.list_drafts(guild_id=guild_id)
        if not found:
            await ctx.send('No drafts created in this server yet. Try `!theme create <theme_key> <name>`.')
            return
        lines = [f"• id `{d['id']}` — **{d['name']}** — theme `{d['theme']}` — status `{d['status']}`" for d in found]
        await ctx.send('**Drafts in this server:**\n' + '\n'.join(lines))
        return

    if action == 'create':
        parts = rest.split(maxsplit=1)
        if len(parts) < 2:
            await ctx.send('Usage: `!theme create <theme_key> <draft name>`')
            return
        theme_key, name = parts[0], parts[1]
        if theme_key not in themes.REGISTRY:
            await ctx.send(f"❌ Unknown theme `{theme_key}`. Try `!theme list`.")
            return
        draft_id = db.create_draft(
            guild_id=ctx.guild.id if ctx.guild else None,
            admin_channel_id=ctx.channel.id,
            theme=theme_key,
            name=name,
            settings=themes.REGISTRY[theme_key].default_settings(),
        )
        await ctx.send(f"✅ Created draft **{name}** (id `{draft_id}`, theme `{theme_key}`). Add drafters with `!theme adddrafter {draft_id} @user`.")
        return

    if action == 'adddrafter':
        parts = rest.split(maxsplit=1)
        if not parts or not ctx.message.mentions:
            await ctx.send('Usage: `!theme adddrafter <draft_id_or_name> @user`')
            return
        draft = _resolve_draft(parts[0])
        if not draft:
            await ctx.send('❌ Draft not found.')
            return
        member = ctx.message.mentions[0]
        existing = db.get_drafter_by_user(draft['id'], member.id)
        if existing:
            await ctx.send(f"⚠️ {member.display_name} is already in this draft.")
            return
        seat = len(db.list_drafters(draft['id']))
        db.add_drafter(draft['id'], member.id, member.display_name, seat_index=seat)
        await ctx.send(f"✅ Added **{member.display_name}** to draft **{draft['name']}** (seat {seat}).")
        return

    if action == 'settings':
        parts = rest.split(maxsplit=1)
        if len(parts) < 2:
            await ctx.send('Usage: `!theme settings <draft_id_or_name> key=value key2=value2 ...`')
            return
        draft = _resolve_draft(parts[0])
        if not draft:
            await ctx.send('❌ Draft not found.')
            return
        patch = _parse_settings_kv(parts[1])
        if not patch:
            await ctx.send('No `key=value` pairs found.')
            return
        db.update_draft_settings(draft['id'], patch)
        await ctx.send(f"✅ Updated settings: `{patch}`")
        return

    if action == 'start':
        draft = _resolve_draft(rest)
        if not draft:
            await ctx.send('Usage: `!theme start <draft_id_or_name>`')
            return
        theme = themes.get_theme(draft['theme'])
        draft_ctx = DraftContext(bot, draft['id'])
        try:
            summary = await theme.start(draft_ctx)
        except ValueError as e:
            await ctx.send(f"❌ Can't start: {e}")
            return
        db.set_draft_status(draft['id'], 'active')
        await ctx.send(f"🚀 {summary}")
        return

    if action == 'status':
        draft = _resolve_draft(rest)
        if not draft:
            await ctx.send('Usage: `!theme status <draft_id_or_name>`')
            return
        theme = themes.get_theme(draft['theme'])
        drafters = db.list_drafters(draft['id'])
        lines = [
            f"**{draft['name']}** (id `{draft['id']}`) — theme `{draft['theme']}` — status `{draft['status']}`",
        ]
        for d in drafters:
            picks = len(db.get_picks(draft['id'], d['id']))
            submitted = '✅' if d['roster_submitted'] else '—'
            lines.append(f"  • {d['display_name']} — {picks} picks — roster submitted: {submitted}")
        await ctx.send('\n'.join(lines))
        return

    if action == 'team':
        parts = rest.split(maxsplit=1)
        if len(parts) < 2 or not ctx.message.mentions:
            await ctx.send('Usage: `!theme team <draft_id_or_name> @user` — looks up what team/division a GM is on, and re-DMs it to them (e.g. to confirm they\'ve fixed their DM settings after a failed send).')
            return
        draft = _resolve_draft(parts[0])
        if not draft:
            await ctx.send('❌ Draft not found.')
            return
        member = ctx.message.mentions[0]
        drafter = db.get_drafter_by_user(draft['id'], member.id)
        if not drafter:
            await ctx.send(f"❌ {member.display_name} isn't in this draft.")
            return
        theme = themes.get_theme(draft['theme'])
        draft_ctx = DraftContext(bot, draft['id'])
        label = theme.team_label(draft_ctx, drafter)
        div_idx = drafter.get('division')
        div_part = f" in {theme.division_label(draft_ctx, div_idx)}" if div_idx is not None else ''
        delivered = await draft_ctx.dm(
            member.id,
            f"🏀 You're drafting{div_part} — your team is **{label}**.\n"
            f"⚠️ Keep this to yourself until seeding is public — disclosing your team early costs you 5 wins.",
        )
        status = '✅ Re-sent via DM — their DMs are open again.' if delivered else "❌ Still couldn't DM them — DMs are still closed/blocked."
        await ctx.send(f"**{member.display_name}** is drafting **{label}**{div_part}.\n{status}")
        return

    if action in ('pause', 'resume'):
        draft = _resolve_draft(rest)
        if not draft:
            await ctx.send(f'Usage: `!theme {action} <draft_id_or_name>`')
            return
        theme = themes.get_theme(draft['theme'])
        draft_ctx = DraftContext(bot, draft['id'])
        if action == 'pause':
            db.set_draft_status(draft['id'], 'paused')
            db.update_draft_state(draft['id'], {'night_paused': False})  # manual pause, not the overnight window job
            await theme.cancel(draft_ctx)  # stop in-memory timers too — pausing shouldn't leave one ticking toward an auto-pick
            await ctx.send("⏸️ Paused — timers stopped.")
        else:
            db.set_draft_status(draft['id'], 'active')
            db.update_draft_state(draft['id'], {'night_paused': False})
            resumed = await theme.resume_timers(draft_ctx)
            await ctx.send(f"▶️ Resumed — {resumed} timer(s) restarted.")
        return

    if action == 'reveal':
        parts = rest.split(maxsplit=1)
        # Only branch into the stats lookup when the first token is a bare
        # draft ID — a multi-word draft NAME (e.g. "ATD 110 - Pack Draft")
        # would otherwise get its first space mistaken for the id/emoji
        # boundary and silently truncated into the wrong draft reference.
        if len(parts) == 2 and parts[0].isdigit():
            await _reveal_team_stats(ctx, parts[0], parts[1])
        else:
            await _reveal_draft(ctx, rest)
        return

    if action == 'cancel':
        draft = _resolve_draft(rest)
        if not draft:
            await ctx.send('Usage: `!theme cancel <draft_id_or_name>`')
            return
        if draft['status'] == 'cancelled':
            await ctx.send(f"**{draft['name']}** is already cancelled.")
            return
        theme = themes.get_theme(draft['theme'])
        await theme.cancel(DraftContext(bot, draft['id']))
        db.set_draft_status(draft['id'], 'cancelled')
        await ctx.send(f"🛑 Draft **{draft['name']}** cancelled — no further picks or timers will fire. It stays on record; `!theme create` a new one to start over.")
        return

    if action == 'rostertimer':
        parts = rest.split()
        if len(parts) < 2:
            await ctx.send('Usage: `!theme rostertimer <draft_id_or_name> <minutes>` (or `... cancel` to clear it).')
            return
        draft = _resolve_draft(parts[0])
        if not draft:
            await ctx.send('❌ Draft not found.')
            return

        if parts[1].lower() == 'cancel':
            existing = _roster_timers.pop(draft['id'], None)
            if existing and not existing.done():
                existing.cancel()
            state = draft['state']
            had_one = state.pop('roster_deadline', None) is not None
            db.set_draft_state(draft['id'], state)
            await ctx.send(f"🗑️ Roster deadline cleared for **{draft['name']}**." if had_one else f"**{draft['name']}** didn't have a roster deadline set.")
            return

        try:
            minutes = float(parts[1])
        except ValueError:
            await ctx.send('Minutes must be a number (or `cancel`).')
            return
        if minutes <= 0:
            await ctx.send('Minutes must be positive.')
            return

        theme = themes.get_theme(draft['theme'])
        draft_ctx = DraftContext(bot, draft['id'])
        if not theme.is_pick_phase_complete(draft_ctx):
            await ctx.send("⚠️ Picking isn't finished yet — you can still set a deadline, but nobody can `!submitroster` until everyone's done picking.")

        deadline_ts = int(time.time() + minutes * 60)
        state = draft['state']
        state['roster_deadline'] = deadline_ts
        db.set_draft_state(draft['id'], state)
        await _start_roster_timer(draft['id'], minutes)

        seen_keys: set[str] = set()
        notified = 0
        for d in db.list_drafters(draft['id']):
            key = theme.team_key(draft_ctx, d)
            if key in seen_keys or d['roster_submitted']:
                continue
            seen_keys.add(key)
            for linked_id in theme.linked_drafter_ids(draft_ctx, d):
                linked = db.get_drafter(linked_id)
                if linked:
                    await draft_ctx.dm(
                        linked['user_id'],
                        f"⏰ You have until <t:{deadline_ts}:R> to submit your final roster (`!submitroster`) — "
                        f"whatever you have arranged when time's up gets auto-submitted for you if it's valid.",
                    )
                    notified += 1
        await ctx.send(f"⏰ Roster deadline set for **{draft['name']}**: <t:{deadline_ts}:R> ({notified} GM(s) notified).")
        return

    if action == 'divisionname':
        parts = rest.split(maxsplit=2)
        if len(parts) < 3:
            await ctx.send('Usage: `!theme divisionname <draft_id_or_name> <division_letter_or_number> <new name>`')
            return
        draft = _resolve_draft(parts[0])
        if not draft:
            await ctx.send('❌ Draft not found.')
            return
        theme = themes.get_theme(draft['theme'])
        result = await theme.set_division_name(DraftContext(bot, draft['id']), parts[1], parts[2])
        await ctx.send(result)
        return

    await ctx.send(
        "Usage: `!theme <list|drafts|create|adddrafter|settings|start|status|pause|resume|reveal|cancel|rostertimer|divisionname> ...`\n"
        "Type `!help` for the full reference."
    )


# ── Anonymous Draft / Anonymous Budget Draft admin commands ─────────────────────
@bot.command(name='setteams')
async def cmd_setteams(ctx, draft_ref: str = '', *, text: str = ''):
    """!setteams <draft_id_or_name> — DM only. Paste one team per line after
    the draft id: `1. 🦅 - @GM1 @GM2`. Carries the secret logo->GM mapping,
    so this must be sent as a DM, never in a server channel."""
    if not _dm_only(ctx):
        await ctx.send('This carries secret GM mappings — DM it to me directly, not in a server channel.')
        return
    if ctx.author.id != OWNER_ID:
        return
    if not draft_ref or not text.strip():
        await ctx.send(
            'Usage (DM only):\n```\n!setteams <draft_id_or_name>\n1. 🦅 - @GM1\n2. 🐻 - @GM2 @GM3\n```'
        )
        return
    draft = _resolve_draft(draft_ref)
    if not draft:
        await ctx.send('❌ Draft not found.')
        return
    theme = themes.get_theme(draft['theme'])
    result = await theme.setup_teams(DraftContext(bot, draft['id']), text)
    await ctx.send(result)


@bot.command(name='anonoverride')
async def cmd_anonoverride(ctx, draft_ref: str = '', pick_number: str = '', *, team_ref: str = ''):
    """!anonoverride <draft_id_or_name> <pick_number> <team_number_or_logo> —
    safety valve if this bot's pick-number-to-team tracking ever drifts from
    Timer Bot's actual state."""
    if ctx.author.id != OWNER_ID:
        return
    if not draft_ref or not pick_number.isdigit() or not team_ref.strip():
        await ctx.send('Usage: `!anonoverride <draft_id_or_name> <pick_number> <team_number_or_logo>`')
        return
    draft = _resolve_draft(draft_ref)
    if not draft:
        await ctx.send('❌ Draft not found.')
        return
    theme = themes.get_theme(draft['theme'])
    result = await theme.override_pick(DraftContext(bot, draft['id']), int(pick_number), team_ref.strip())
    await ctx.send(result)


async def _reveal_draft(ctx, arg: str):
    draft = _resolve_draft(arg)
    if not draft:
        await ctx.send('Usage: `!theme reveal <draft_id_or_name>`')
        return
    theme = themes.get_theme(draft['theme'])
    draft_ctx = DraftContext(bot, draft['id'])

    if not theme.is_pick_phase_complete(draft_ctx):
        await ctx.send("❌ Not everyone has finished picking yet — check `!theme status`.")
        return

    teams: dict[str, list] = {}
    team_divisions: dict[str, int | None] = {}
    missing: list[str] = []
    seen_keys: set[str] = set()
    for d in db.list_drafters(draft['id']):
        key = theme.team_key(draft_ctx, d)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        label = theme.team_label(draft_ctx, d)
        if not d['roster_submitted']:
            missing.append(label)
            continue

        rows = theme.final_roster_rows(draft_ctx, d)
        div_idx = d.get('division')
        final_label = label
        if label in teams:
            # Two DIFFERENT teams (different division/seat) can land on the
            # identical team_label/team_name — e.g. two divisions both
            # independently assigned a GM "Indiana Pacers". `teams` is
            # keyed by label for the sheet header, so an unqualified second
            # occurrence silently overwrote the first team's whole column.
            # Confirmed live: reveal wrote 39 columns instead of 40 for
            # exactly this reason. Tag BOTH occurrences with their division
            # the moment a collision shows up, so neither looks like "the"
            # team and both actually make it onto the sheet.
            prior_div = team_divisions.pop(label, None)
            if prior_div is not None:
                teams[f"{label} ({theme.division_label(draft_ctx, prior_div)})"] = teams.pop(label)
            final_label = f"{label} ({theme.division_label(draft_ctx, div_idx)})" if div_idx is not None else f"{label} (2)"
        teams[final_label] = rows
        team_divisions[final_label] = div_idx

    if missing:
        await ctx.send('❌ Waiting on roster submissions from: ' + ', '.join(missing))
        return
    if not teams:
        await ctx.send('❌ No submitted rosters to reveal yet.')
        return

    loop = asyncio.get_running_loop()
    try:
        tab_name = await loop.run_in_executor(
            None, sheets.write_draft_results, ROSTER_SPREADSHEET_ID, draft['name'] or ROSTER_TAB_NAME, teams
        )
    except Exception as e:
        log.exception('Reveal | sheet write failed')
        await ctx.send(f"❌ Failed writing to the team sheet: `{e}`")
        return

    db.set_draft_status(draft['id'], 'complete')
    await ctx.send(f"🎉 **Draft revealed!** Results written to tab **{tab_name}**.")
    for d in db.list_drafters(draft['id']):
        roster_list = d['state'].get('final_roster')
        if roster_list:
            await draft_ctx.dm(d['user_id'], f"🏁 The draft has been revealed! Your final roster:\n" + '\n'.join(f'• {p}' for p in roster_list))


def _format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _average_pick_gap_seconds(timestamps: list) -> float | None:
    """Elapsed time between a team's own consecutive picks. The draft
    auto-pauses overnight, which would otherwise inflate this with a
    multi-hour gap that has nothing to do with how long the GM actually
    took — a gap more than 3x the (outlier-resistant) median is treated as
    a pause/idle stretch and dropped rather than averaged in."""
    if len(timestamps) < 2:
        return None
    gaps = [(timestamps[i] - timestamps[i - 1]).total_seconds() for i in range(1, len(timestamps))]
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return None
    sorted_gaps = sorted(gaps)
    n = len(sorted_gaps)
    median = sorted_gaps[n // 2] if n % 2 else (sorted_gaps[n // 2 - 1] + sorted_gaps[n // 2]) / 2
    filtered = [g for g in gaps if median <= 0 or g <= median * 3]
    if not filtered:
        filtered = gaps
    return sum(filtered) / len(filtered)


async def _dm_excerpts(user_id: int, limit: int = 3) -> list[str]:
    """A few of this GM's own recent DM messages to the bot — real
    conversation, not command attempts, for the reveal-stats embed's flavor
    section. Best-effort: empty on any failure (DMs closed, no shared
    history, etc.), never raises."""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        channel = user.dm_channel or await user.create_dm()
        found = []
        async for msg in channel.history(limit=200):
            if msg.author.id != user_id:
                continue
            text = msg.content.strip()
            # "!" is this bot's real prefix; "/" isn't a real command here,
            # but GMs sometimes type it out of slash-command habit (e.g.
            # "/swap Cowens/ Pippen") -- filter both so a mistyped command
            # doesn't slip through as if it were a genuine remark.
            if not text or text.startswith('!') or text.startswith('/'):
                continue
            found.append(text)
            if len(found) >= limit:
                break
        return found
    except (discord.HTTPException, discord.Forbidden, AttributeError):
        return []


async def _reveal_team_stats(ctx, draft_ref: str, emoji_text: str):
    """!theme reveal <draft> <emoji> — not the full-draft reveal (that's the
    single-argument form); looks up ONE team by its logo and shows a
    scouting-report-style summary: skip count, average pick time, budget
    spent, and a couple of their own real DM messages for flavor.
    Main-server only — every other admin command here works from any guild
    an admin has permission in, but this one's restricted to where it's
    actually used."""
    if not ctx.guild or ctx.guild.id != _STATS_LOOKUP_GUILD_ID:
        await ctx.send('❌ This command only works in the main server.')
        return
    draft = _resolve_draft(draft_ref)
    if not draft:
        await ctx.send('❌ Draft not found.')
        return
    theme = themes.get_theme(draft['theme'])
    draft_ctx = DraftContext(bot, draft['id'])

    team_name = theme.resolve_team_logo(emoji_text.strip())

    seen_keys: set[str] = set()
    matches: list[dict] = []
    for d in db.list_drafters(draft['id']):
        key = theme.team_key(draft_ctx, d)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        if theme.team_label(draft_ctx, d) == team_name:
            matches.append(d)

    if not matches:
        await ctx.send(f"❌ No team found matching {emoji_text} (resolved to **{team_name}**).")
        return

    if len(matches) > 1:
        await ctx.send(f"⚠️ {len(matches)} teams share the name/logo **{team_name}** — showing all of them.")

    emoji_name = None
    m = re.search(r'<a?:(\w+):(\d+)>', emoji_text)
    if m:
        emoji_name = m.group(1)
    elif emoji_text.strip():
        emoji_name = emoji_text.strip()
        # Same shortcode-colon stripping as resolve_team_logo — a bare
        # ":Blazers:" needs its colons stripped before matching the
        # guild's real emoji name ("Blazers"), same reasoning as above.
        if len(emoji_name) > 2 and emoji_name.startswith(':') and emoji_name.endswith(':'):
            emoji_name = emoji_name[1:-1]
    emoji_obj = discord.utils.get(ctx.guild.emojis, name=emoji_name) if emoji_name else None

    price_lookup, budget = theme.roster_requirements(draft_ctx)

    for d in matches:
        linked_ids = theme.linked_drafter_ids(draft_ctx, d)
        linked = [l for l in (db.get_drafter(lid) for lid in linked_ids) if l]
        # Only one member of a duo/trio actually accrues skip_count (see
        # _commit_pick) — the rest sit at 0, so take whichever is highest
        # rather than assuming a particular member is "the" primary.
        skip_count = max((l['skip_count'] for l in linked), default=0)
        gm_names = ', '.join(l['display_name'] for l in linked) or d['display_name']

        picks = db.get_picks(draft['id'], d['id'])
        timestamps = db.get_pick_timestamps(draft['id'], d['id'])
        avg_gap = _average_pick_gap_seconds(timestamps)

        embed = discord.Embed(
            title=f"{str(emoji_obj) + ' ' if emoji_obj else ''}{team_name}",
            color=discord.Color.gold(),
        )
        if emoji_obj:
            embed.set_thumbnail(url=emoji_obj.url)
        embed.add_field(name='GM(s)', value=gm_names, inline=False)
        embed.add_field(name='Skipped', value=f"{skip_count} time(s)", inline=True)
        embed.add_field(name='Avg. pick time', value=_format_duration(avg_gap) if avg_gap is not None else '—', inline=True)

        if price_lookup and picks:
            priced = [(p, price_lookup.get(p) or price_lookup.get(p.lower())) for p in picks]
            priced = [(p, pr) for p, pr in priced if pr is not None]
            total = sum(pr for _p, pr in priced)
            budget_str = f"${total:.0f} / ${budget:.0f}" if budget else f"${total:.0f}"
            embed.add_field(name='Spent', value=f"{budget_str} ({len(picks)} picks)", inline=True)
            if priced:
                best_player, best_price = max(priced, key=lambda x: x[1])
                embed.add_field(name='Best pick', value=f"{best_player} — ${best_price:.0f}", inline=True)
        else:
            embed.add_field(name='Picks', value=str(len(picks)), inline=True)

        excerpts = await _dm_excerpts(linked[0]['user_id']) if linked else []
        embed.add_field(
            name='From their DMs',
            value='\n'.join(f"> {e[:200]}" for e in excerpts) if excerpts else '_Nothing on file._',
            inline=False,
        )
        embed.set_footer(text=draft['name'])
        await ctx.send(embed=embed)


# ── Pack Draft literal moderator commands ───────────────────────────────────────
# Thin wrappers over the theme hooks, kept as their own top-level commands
# (rather than folded into `!theme <verb>`) since the format's own spec names
# them explicitly and they resolve the draft implicitly from the channel —
# no draft id needed, mirroring how ATD Timer Bot resolves sessions by channel.
@bot.command(name='importorder')
@commands.has_permissions(administrator=True)
async def cmd_importorder(ctx, division: str = ''):
    """!importorder <division> — reply to that division's lotto message to set draft order, division, and
    each seat's team name (e.g. `1. Philadelphia 76ers - @Alice`)."""
    draft = _resolve_draft_for_channel(ctx.channel.id)
    if not draft:
        await ctx.send('No draft is set up in this channel yet — run `!theme create pack_draft <name>` first.')
        return
    if not division:
        await ctx.send('Usage: reply to the lotto message with `!importorder <division>` (e.g. `!importorder A`).')
        return
    if not ctx.message.reference:
        await ctx.send('Reply directly to the lotto message with this command.')
        return
    try:
        ref_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
    except Exception as e:
        await ctx.send(f"❌ Couldn't fetch the replied message: {e}")
        return
    team_lines = lotto.parse_team_lines(ref_msg.content)
    if not team_lines:
        await ctx.send('No valid lotto lines found in that message.')
        return
    theme = themes.get_theme(draft['theme'])
    result = await theme.import_draft_order(DraftContext(bot, draft['id']), division, team_lines)
    await ctx.send(result)


@bot.command(name='generatepacks')
@commands.has_permissions(administrator=True)
async def cmd_generatepacks(ctx, division: str = ''):
    """!generatepacks <division> — auto-generate that division's packs from the ADP sheet."""
    draft = _resolve_draft_for_channel(ctx.channel.id)
    if not draft:
        await ctx.send('No draft is set up in this channel yet.')
        return
    if not division:
        await ctx.send('Usage: `!generatepacks <division>` (e.g. `!generatepacks A`).')
        return
    theme = themes.get_theme(draft['theme'])
    result = await theme.generate_packs(DraftContext(bot, draft['id']), division)
    await ctx.send(result)


@bot.command(name='loadpacks')
@commands.has_permissions(administrator=True)
async def cmd_loadpacks(ctx, division: str = '', *, text: str = ''):
    """!loadpacks <division> then Pack N: Player, Player, ... lines — manually supply packs."""
    draft = _resolve_draft_for_channel(ctx.channel.id)
    if not draft:
        await ctx.send('No draft is set up in this channel yet.')
        return
    if not division or not text:
        await ctx.send('Usage:\n```\n!loadpacks A\nPack 1: Player One, Player Two, ...\nPack 2: ...\n```')
        return
    theme = themes.get_theme(draft['theme'])
    result = await theme.load_packs(DraftContext(bot, draft['id']), division, text)
    await ctx.send(result)


@bot.command(name='startpackdraft')
@commands.has_permissions(administrator=True)
async def cmd_startpackdraft(ctx):
    """!startpackdraft — start the pack draft set up in this channel."""
    draft = _resolve_draft_for_channel(ctx.channel.id)
    if not draft:
        await ctx.send('No draft is set up in this channel yet.')
        return
    theme = themes.get_theme(draft['theme'])
    draft_ctx = DraftContext(bot, draft['id'])
    try:
        summary = await theme.start(draft_ctx)
    except ValueError as e:
        await ctx.send(f"❌ Can't start: {e}")
        return
    db.set_draft_status(draft['id'], 'active')
    await ctx.send(f"🚀 {summary}")


@bot.command(name='pausepackdraft')
@commands.has_permissions(administrator=True)
async def cmd_pausepackdraft(ctx):
    """!pausepackdraft — pause the pack draft set up in this channel."""
    draft = _resolve_draft_for_channel(ctx.channel.id)
    if not draft:
        await ctx.send('No draft is set up in this channel yet.')
        return
    db.set_draft_status(draft['id'], 'paused')
    db.update_draft_state(draft['id'], {'night_paused': False})  # manual pause, not the overnight window job
    theme = themes.get_theme(draft['theme'])
    await theme.cancel(DraftContext(bot, draft['id']))  # stop in-memory timers too — pausing shouldn't leave one ticking toward an auto-pick
    await ctx.send('⏸️ Paused — timers stopped.')


@bot.command(name='resumepackdraft')
@commands.has_permissions(administrator=True)
async def cmd_resumepackdraft(ctx):
    """!resumepackdraft — resume the pack draft set up in this channel."""
    draft = _resolve_draft_for_channel(ctx.channel.id)
    if not draft:
        await ctx.send('No draft is set up in this channel yet.')
        return
    db.set_draft_status(draft['id'], 'active')
    db.update_draft_state(draft['id'], {'night_paused': False})
    theme = themes.get_theme(draft['theme'])
    resumed = await theme.resume_timers(DraftContext(bot, draft['id']))
    await ctx.send(f'▶️ Resumed — {resumed} timer(s) restarted.')


@bot.command(name='packskip')
@commands.has_permissions(administrator=True)
async def cmd_packskip(ctx, *, target: str = ''):
    """!packskip @user — force-skip a drafter's current pick, same as a timeout."""
    draft = _resolve_draft_for_channel(ctx.channel.id)
    if not draft:
        await ctx.send('No draft is set up in this channel yet.')
        return
    if not ctx.message.mentions:
        await ctx.send('Usage: `!packskip @user`')
        return
    member = ctx.message.mentions[0]
    drafter = db.get_drafter_by_user(draft['id'], member.id)
    if not drafter:
        await ctx.send(f"❌ {member.display_name} isn't in this draft.")
        return
    theme = themes.get_theme(draft['theme'])
    result = await theme.force_skip(DraftContext(bot, draft['id']), drafter)
    await ctx.send(result)


@bot.command(name='addteammate')
@commands.has_permissions(administrator=True)
async def cmd_addteammate(ctx):
    """!addteammate @existing_gm @new_gm — add a new GM to an existing GM's
    team/seat (turns a solo seat into a duo, or a duo into a trio)."""
    draft = _resolve_draft_for_channel(ctx.channel.id)
    if not draft:
        await ctx.send('No draft is set up in this channel yet.')
        return
    # ctx.message.mentions isn't guaranteed to preserve the order they were
    # typed in (a known discord.py quirk) — regex the raw content instead,
    # same as lotto.py already does, so "existing" and "new" can't get
    # silently swapped.
    ids = [int(uid) for uid in re.findall(r'<@!?(\d+)>', ctx.message.content)]
    if len(ids) < 2:
        await ctx.send('Usage: `!addteammate @existing_gm @new_gm`')
        return
    existing_id, new_id = ids[0], ids[1]
    theme = themes.get_theme(draft['theme'])
    result = await theme.add_teammate(DraftContext(bot, draft['id']), existing_id, new_id)
    await ctx.send(result)


@bot.command(name='movedrafter')
@commands.has_permissions(administrator=True)
async def cmd_movedrafter(ctx, *, rest: str = ''):
    """!movedrafter @user <division> <seat> — move a GM to a different
    division/seat (e.g. `!movedrafter @user C 4`). Only supported before
    that GM has made any picks — moving them after would orphan their
    picks against the wrong division's pool."""
    draft = _resolve_draft_for_channel(ctx.channel.id)
    if not draft:
        await ctx.send('No draft is set up in this channel yet.')
        return
    ids = [int(uid) for uid in re.findall(r'<@!?(\d+)>', ctx.message.content)]
    if not ids:
        await ctx.send('Usage: `!movedrafter @user <division> <seat>` (e.g. `!movedrafter @user C 4`)')
        return
    remainder = re.sub(r'<@!?\d+>', '', rest).strip()
    parts = remainder.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await ctx.send('Usage: `!movedrafter @user <division> <seat>` (e.g. `!movedrafter @user C 4`)')
        return
    division_ref, seat_str = parts
    theme = themes.get_theme(draft['theme'])
    result = await theme.move_drafter(DraftContext(bot, draft['id']), ids[0], division_ref, int(seat_str))
    await ctx.send(result)


@bot.command(name='divisionroster')
@commands.has_permissions(administrator=True)
async def cmd_divisionroster(ctx):
    """!divisionroster — every GM, grouped by division and seat (works
    before !startpackdraft too, unlike !packstatus)."""
    draft = _resolve_draft_for_channel(ctx.channel.id)
    if not draft:
        await ctx.send('No draft is set up in this channel yet.')
        return
    theme = themes.get_theme(draft['theme'])
    result = await theme.division_roster(DraftContext(bot, draft['id']))
    chunk = ''
    for line in result.splitlines(keepends=True):
        if len(chunk) + len(line) > 1900:
            await ctx.send(chunk)
            chunk = ''
        chunk += line
    if chunk.strip():
        await ctx.send(chunk)


@bot.command(name='packstatus')
@commands.has_permissions(administrator=True)
async def cmd_packstatus(ctx):
    """!packstatus — moderator-facing per-division progress (pack sizes/queues, not contents)."""
    draft = _resolve_draft_for_channel(ctx.channel.id)
    if not draft:
        await ctx.send('No draft is set up in this channel yet.')
        return
    theme = themes.get_theme(draft['theme'])
    result = await theme.admin_status(DraftContext(bot, draft['id']))
    # 5 divisions x 8 seats' worth of lines can blow well past Discord's
    # 2000-char message limit — chunk on line boundaries instead of sending
    # it as one message (which 400s with "Invalid Form Body").
    chunk = ''
    for line in result.splitlines(keepends=True):
        if len(chunk) + len(line) > 1900:
            await ctx.send(chunk)
            chunk = ''
        chunk += line
    if chunk.strip():
        await ctx.send(chunk)


class _RosterPager(discord.ui.View):
    """◀️/▶️ paginator, one team per page — 40 teams across 5 divisions is
    too much to dump as a wall of chunked text messages."""

    def __init__(self, pages: list[discord.Embed], author_id: int):
        super().__init__(timeout=300)
        self.pages = pages
        self.index = 0
        self.author_id = author_id
        self.message: discord.Message | None = None
        self._sync_buttons()

    def _sync_buttons(self):
        self.prev_button.disabled = self.index == 0
        self.next_button.disabled = self.index == len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the commish who ran this command can page through it.", ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label='◀️ Previous', style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index -= 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label='Next ▶️', style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index += 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)


def _team_roster_embed(theme, draft_ctx: DraftContext, d: dict) -> discord.Embed:
    """One team's submission status/roster as a Discord embed — shared by
    !submittedrosters (one page per team) and the auto-post to the admin
    channel whenever a fresh !submitroster comes in."""
    label = theme.team_label(draft_ctx, d)
    gm_names = ', '.join(
        gm['display_name'] for lid in theme.linked_drafter_ids(draft_ctx, d)
        if (gm := db.get_drafter(lid))
    ) or d['display_name']

    if not d['roster_submitted']:
        return discord.Embed(
            title=f"❌ {label}",
            description=f"**GM:** {gm_names}\n\n_Not submitted yet._",
            color=discord.Color.red(),
        )

    rows = theme.final_roster_rows(draft_ctx, d)
    total = 0.0
    priced = False
    player_lines = []
    for i, row in enumerate(rows, 1):
        if isinstance(row, (tuple, list)):
            player, year, price = row
            extra = f" ({year})" if year else ''
            price_part = f" — {price}" if price else ''
            if price:
                try:
                    total += float(price.lstrip('$'))
                    priced = True
                except ValueError:
                    pass
            player_lines.append(f"{i}. {player}{extra}{price_part}")
        else:
            player_lines.append(f"{i}. {row}")
    total_line = f"\n**Total:** ${total:.0f}" if priced else ''
    description = f"**GM:** {gm_names}{total_line}\n\n" + '\n'.join(player_lines)
    return discord.Embed(title=f"✅ {label}", description=description, color=discord.Color.green())


def _dedup_teams(theme, draft_ctx: DraftContext, draft_id: int) -> list[dict]:
    """One drafter row per team (duo/trio seats share one), in draft order."""
    seen_keys: set[str] = set()
    team_drafters: list[dict] = []
    for d in db.list_drafters(draft_id):
        key = theme.team_key(draft_ctx, d)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        team_drafters.append(d)
    return team_drafters


@bot.command(name='submittedrosters')
@commands.has_permissions(administrator=True)
async def cmd_submittedrosters(ctx):
    """!submittedrosters — commish-only: page through every team's
    submission status, and the submitted roster for anyone who's already
    turned one in, one team per page. Unlike !allteams (owner-only, shows
    raw in-progress picks), this shows the actual final submission — safe
    for any admin, since pre-reveal secrecy is about hiding rosters from
    OTHER GMs, not from the commish."""
    draft = _resolve_draft_for_channel(ctx.channel.id)
    if not draft:
        await ctx.send('No draft is set up in this channel yet.')
        return
    theme = themes.get_theme(draft['theme'])
    draft_ctx = DraftContext(bot, draft['id'])

    team_drafters = _dedup_teams(theme, draft_ctx, draft['id'])
    if not team_drafters:
        await ctx.send('No drafters yet.')
        return

    submitted_count = sum(1 for d in team_drafters if d['roster_submitted'])
    pages = []
    for i, d in enumerate(team_drafters):
        embed = _team_roster_embed(theme, draft_ctx, d)
        embed.set_footer(text=f"{draft['name']} — Team {i + 1}/{len(team_drafters)} — {submitted_count}/{len(team_drafters)} submitted")
        pages.append(embed)

    view = _RosterPager(pages, ctx.author.id)
    view.message = await ctx.send(embed=pages[0], view=view)


@bot.command(name='missingyears')
@commands.has_permissions(administrator=True)
async def cmd_missingyears(ctx):
    """!missingyears — commish-only: DM every GM whose SUBMITTED roster has
    a player with no year set (via !addyear), listing exactly who's missing
    one. Only checks teams that have already submitted — nothing to check
    for anyone still mid-draft."""
    draft = _resolve_draft_for_channel(ctx.channel.id)
    if not draft:
        await ctx.send('No draft is set up in this channel yet.')
        return
    theme = themes.get_theme(draft['theme'])
    draft_ctx = DraftContext(bot, draft['id'])

    seen_keys: set[str] = set()
    notified = 0
    clean = 0
    for d in db.list_drafters(draft['id']):
        if not d['roster_submitted']:
            continue
        key = theme.team_key(draft_ctx, d)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        rows = theme.final_roster_rows(draft_ctx, d)
        if not rows or not isinstance(rows[0], (tuple, list)):
            continue  # this theme doesn't track per-player years at all
        missing = [player for player, year, _price in rows if not year]
        if not missing:
            clean += 1
            continue

        label = theme.team_label(draft_ctx, d)
        missing_str = ', '.join(f"**{p}**" for p in missing)
        for linked_id in theme.linked_drafter_ids(draft_ctx, d):
            linked = db.get_drafter(linked_id)
            if linked:
                await draft_ctx.dm(
                    linked['user_id'],
                    f"⚠️ Your submitted roster for **{label}** is missing a year for: {missing_str}.\n"
                    f"Run `!addyear <player> <year>` to add it before reveal.",
                )
        notified += 1

    await ctx.send(
        f"📋 Checked {len(seen_keys)} submitted team(s) — {notified} notified about missing years, "
        f"{clean} already complete."
    )


@bot.command(name='teamlist')
@commands.has_permissions(administrator=True)
async def cmd_teamlist(ctx):
    """!teamlist — commish-only: every team's name, logo, and GM(s),
    10 per page."""
    draft = _resolve_draft_for_channel(ctx.channel.id)
    if not draft:
        await ctx.send('No draft is set up in this channel yet.')
        return
    theme = themes.get_theme(draft['theme'])
    draft_ctx = DraftContext(bot, draft['id'])
    guild = bot.get_guild(draft['guild_id']) if draft.get('guild_id') else None

    # Several emoji names can map to the same team in EMOJI_TEAM_MAP (e.g.
    # "Heat"/"Heat2"/"MIA" all -> "Miami Heat") — the map only stores names,
    # not IDs, so a real renderable <:name:id> tag has to come from the
    # guild's actual emoji list. Reversing to team -> emoji NAME just gives
    # us something to look up there.
    team_to_emoji_name: dict[str, str] = {}
    for emoji_name, team_name in EMOJI_TEAM_MAP.items():
        team_to_emoji_name.setdefault(team_name, emoji_name)

    team_drafters = _dedup_teams(theme, draft_ctx, draft['id'])
    if not team_drafters:
        await ctx.send('No drafters yet.')
        return

    lines = []
    for d in team_drafters:
        label = theme.team_label(draft_ctx, d)
        gm_names = ', '.join(
            gm['display_name'] for lid in theme.linked_drafter_ids(draft_ctx, d)
            if (gm := db.get_drafter(lid))
        ) or d['display_name']

        logo = ''
        emoji_name = team_to_emoji_name.get(label)
        if emoji_name and guild:
            emoji_obj = discord.utils.get(guild.emojis, name=emoji_name)
            if emoji_obj:
                logo = f"{emoji_obj} "
        lines.append(f"{logo}**{label}** — GM: {gm_names}")

    per_page = 10
    pages = []
    for i in range(0, len(lines), per_page):
        chunk = lines[i:i + per_page]
        embed = discord.Embed(
            title=f"Teams — {draft['name']}",
            description='\n'.join(chunk),
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"Teams {i + 1}-{i + len(chunk)} of {len(lines)}")
        pages.append(embed)

    view = _RosterPager(pages, ctx.author.id)
    view.message = await ctx.send(embed=pages[0], view=view)


# ── DM commands (drafters) ──────────────────────────────────────────────────────
@bot.command(name='pick')
async def cmd_pick(ctx, *, text: str = ''):
    """!pick <player> — make your pick (Pack Draft, Anonymous Draft)."""
    if not _dm_only(ctx):
        return
    resolved = _resolve_active_drafter(ctx.author.id)
    if not resolved:
        await ctx.send("You're not in an active draft right now.")
        return
    draft, drafter = resolved
    if draft['status'] != 'active':
        await ctx.send(_not_active_message(draft))
        return
    theme = themes.get_theme(draft['theme'])
    reply = await theme.on_pick(DraftContext(bot, draft['id'], message=ctx.message), drafter, text)
    # Themes may already have replied directly to ctx.message themselves
    # (e.g. a makeup-pick confirmation) — only send this if there's more to say.
    if reply:
        await ctx.send(reply)


@bot.command(name='activate')
async def cmd_activate(ctx):
    """!activate — propose activating your nation's one-time power (Nation Flux)."""
    if not _dm_only(ctx):
        return
    resolved = _resolve_active_drafter(ctx.author.id)
    if not resolved:
        await ctx.send("You're not in an active draft right now.")
        return
    draft, drafter = resolved
    if draft['status'] != 'active':
        await ctx.send(_not_active_message(draft))
        return
    theme = themes.get_theme(draft['theme'])
    reply = await theme.propose_power(DraftContext(bot, draft['id']), drafter)
    await ctx.send(reply)


@bot.command(name='airreact')
async def cmd_airreact(ctx):
    """!airreact — Air's reactive counter-power while another nation's vote is open (Nation Flux)."""
    if not _dm_only(ctx):
        return
    resolved = _resolve_active_drafter(ctx.author.id)
    if not resolved:
        await ctx.send("You're not in an active draft right now.")
        return
    draft, drafter = resolved
    if draft['status'] != 'active':
        await ctx.send(_not_active_message(draft))
        return
    theme = themes.get_theme(draft['theme'])
    reply = await theme.air_react(DraftContext(bot, draft['id']), drafter)
    await ctx.send(reply)


@bot.command(name='list')
async def cmd_list(ctx, *, text: str = ''):
    """!list Player One, Player Two, Player Three — submit your round list (Anonymous Partner Draft)."""
    if not _dm_only(ctx):
        return
    resolved = _resolve_active_drafter(ctx.author.id)
    if not resolved:
        await ctx.send("You're not in an active draft right now.")
        return
    draft, drafter = resolved
    if draft['status'] != 'active':
        await ctx.send(_not_active_message(draft))
        return
    theme = themes.get_theme(draft['theme'])
    reply = await theme.on_list(DraftContext(bot, draft['id']), drafter, text)
    await ctx.send(reply)


@bot.command(name='msg')
async def cmd_msg(ctx, *, text: str = ''):
    """!msg <text> — relay a message to your partner (Anonymous Partner Draft)."""
    if not _dm_only(ctx):
        return
    resolved = _resolve_active_drafter(ctx.author.id)
    if not resolved:
        await ctx.send("You're not in an active draft right now.")
        return
    draft, drafter = resolved
    if draft['status'] != 'active':
        await ctx.send(_not_active_message(draft))
        return
    theme = themes.get_theme(draft['theme'])
    reply = await theme.on_message(DraftContext(bot, draft['id']), drafter, text)
    await ctx.send(reply)


@bot.command(name='status')
async def cmd_status(ctx):
    """!status — what you're currently waiting on."""
    if not _dm_only(ctx):
        return
    resolved = _resolve_active_drafter(ctx.author.id)
    if not resolved:
        await ctx.send("You're not in an active draft right now.")
        return
    draft, drafter = resolved
    theme = themes.get_theme(draft['theme'])
    reply = await theme.on_status(DraftContext(bot, draft['id']), drafter)
    await ctx.send(f"**{draft['name']}** ({theme.display_name}) — status `{draft['status']}`\n{reply}")


@bot.command(name='pack')
async def cmd_pack(ctx):
    """!pack — resend your current active pack (Pack Draft)."""
    if not _dm_only(ctx):
        return
    resolved = _resolve_active_drafter(ctx.author.id)
    if not resolved:
        await ctx.send("You're not in an active draft right now.")
        return
    draft, drafter = resolved
    theme = themes.get_theme(draft['theme'])
    reply = await theme.resend_current(DraftContext(bot, draft['id']), drafter)
    await ctx.send(reply)


@bot.command(name='mypicks')
async def cmd_mypicks(ctx):
    """!mypicks — everything you've drafted so far, secretly (sorted by price where available)."""
    if not _dm_only(ctx):
        return
    resolved = _resolve_active_drafter(ctx.author.id)
    if not resolved:
        await ctx.send("You're not in an active draft right now.")
        return
    draft, drafter = resolved
    theme = themes.get_theme(draft['theme'])
    picks = db.get_picks(draft['id'], drafter['id'])
    text = await theme.format_picks(DraftContext(bot, draft['id']), drafter, picks)
    await ctx.send(text)


@bot.command(name='myteam')
async def cmd_myteam(ctx):
    """!myteam — your current roster/lineup view."""
    if not _dm_only(ctx):
        return
    resolved = _resolve_active_drafter(ctx.author.id)
    if not resolved:
        await ctx.send("You're not in an active draft right now.")
        return
    draft, drafter = resolved
    theme = themes.get_theme(draft['theme'])
    picks = db.get_picks(draft['id'], drafter['id'])
    text = await theme.format_team(DraftContext(bot, draft['id']), drafter, picks)
    await ctx.send(text)


@bot.command(name='add')
async def cmd_add(ctx, *, player: str = ''):
    """!add <player> — place a drafted player into an open roster slot automatically."""
    if not _dm_only(ctx):
        return
    resolved = _resolve_active_drafter(ctx.author.id)
    if not resolved:
        await ctx.send("You're not in an active draft right now.")
        return
    if not player.strip():
        await ctx.send('Usage: `!add <player>`')
        return
    draft, drafter = resolved
    theme = themes.get_theme(draft['theme'])
    reply = await theme.add_player(DraftContext(bot, draft['id']), drafter, player.strip())
    await ctx.send(reply)


@bot.command(name='allteams')
async def cmd_allteams(ctx, *, arg: str = ''):
    """!allteams <draft_id_or_name> — owner-only: every team's picks so far,
    bypassing the normal hidden-until-reveal rule. DM only."""
    if ctx.author.id != OWNER_ID:
        return
    if not _dm_only(ctx):
        await ctx.send('Please DM this command to me directly.')
        return
    if not arg:
        await ctx.send('Usage: `!allteams <draft_id_or_name>`')
        return
    draft = _resolve_draft(arg)
    if not draft:
        await ctx.send('❌ Draft not found.')
        return

    theme = themes.get_theme(draft['theme'])
    draft_ctx = DraftContext(bot, draft['id'])

    teams: list[tuple[str, list[str]]] = []
    seen_keys: set[str] = set()
    for d in db.list_drafters(draft['id']):
        key = theme.team_key(draft_ctx, d)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        label = theme.team_label(draft_ctx, d)
        picks = db.get_picks(draft['id'], d['id'])
        teams.append((label, picks))

    if not teams:
        await ctx.send('No drafters yet.')
        return

    header = f"**All teams — {draft['name']}** (owner view — status `{draft['status']}`, bypasses reveal)\n"
    chunk = header
    for label, picks in teams:
        block = f"\n**{label}** ({len(picks)} picks): " + (', '.join(picks) if picks else '_none yet_') + '\n'
        if len(chunk) + len(block) > 1900:
            await ctx.send(chunk)
            chunk = ''
        chunk += block
    if chunk.strip():
        await ctx.send(chunk)


@bot.command(name='swap')
async def cmd_swap(ctx, *, text: str = ''):
    """!swap <Player A> / <Player B> — swap two players' roster slots.
    !swap <Player> / <slot number> — move a player to a specific slot (see `!mypicks` for numbers)."""
    if not _dm_only(ctx):
        return
    resolved = _resolve_active_drafter(ctx.author.id)
    if not resolved:
        await ctx.send("You're not in an active draft right now.")
        return
    draft, drafter = resolved
    if '/' not in text:
        await ctx.send('Usage: `!swap <Player A> / <Player B>` or `!swap <Player> / <slot number>`')
        return
    arg_a, arg_b = (p.strip() for p in text.split('/', 1))
    if not arg_a or not arg_b:
        await ctx.send('Usage: `!swap <Player A> / <Player B>` or `!swap <Player> / <slot number>`')
        return
    theme = themes.get_theme(draft['theme'])
    reply = await theme.swap_slots(DraftContext(bot, draft['id']), drafter, arg_a, arg_b)
    await ctx.send(reply)


@bot.command(name='addyear')
async def cmd_addyear(ctx, *, text: str = ''):
    """!addyear <Player> <Year> — set or update a drafted player's year."""
    if not _dm_only(ctx):
        return
    resolved = _resolve_active_drafter(ctx.author.id)
    if not resolved:
        await ctx.send("You're not in an active draft right now.")
        return
    draft, drafter = resolved
    parts = text.rsplit(maxsplit=1)
    if len(parts) != 2:
        await ctx.send('Usage: `!addyear <Player> <Year>` (e.g. `!addyear LeBron James 2013`)')
        return
    player, year = parts
    theme = themes.get_theme(draft['theme'])
    reply = await theme.set_player_year(DraftContext(bot, draft['id']), drafter, player, year)
    await ctx.send(reply)


@bot.command(name='pref')
async def cmd_pref(ctx, *, text: str = ''):
    """!pref <Player A>, <Player B>, ... — standing pick-priority list, auto-picked
    the instant one of them is in a pack you receive (no need to be online).
    !pref clear to remove it. !pref with no arguments shows your current list."""
    if not _dm_only(ctx):
        return
    resolved = _resolve_active_drafter(ctx.author.id)
    if not resolved:
        await ctx.send("You're not in an active draft right now.")
        return
    draft, drafter = resolved
    theme = themes.get_theme(draft['theme'])
    reply = await theme.set_preferences(DraftContext(bot, draft['id']), drafter, text)
    await ctx.send(reply)


@bot.command(name='submitroster')
async def cmd_submitroster(ctx, *, text: str = ''):
    """!submitroster Player One, Player Two, ... — submit your final roster for validation.
    Run with no arguments to submit your current !mypicks arrangement instead, if every slot is filled."""
    if not _dm_only(ctx):
        return
    resolved = _resolve_active_drafter(ctx.author.id)
    if not resolved:
        await ctx.send("You're not in an active draft right now.")
        return
    draft, drafter = resolved
    theme = themes.get_theme(draft['theme'])
    draft_ctx = DraftContext(bot, draft['id'])

    if not theme.is_pick_phase_complete(draft_ctx, drafter):
        scope = 'your division' if drafter.get('division') is not None else 'everyone'
        await ctx.send(f"Picking isn't finished yet — hang tight until {scope}'s done.")
        return

    if text.strip():
        submitted = [p.strip() for p in text.replace('\n', ',').split(',') if p.strip()]
    else:
        submitted = theme.current_roster_list(draft_ctx, drafter)
    if not submitted:
        await ctx.send(
            'List your final roster, comma- or line-separated: `!submitroster Player One, Player Two, ...`\n'
            'Or fill every slot first (see `!mypicks` / `!swap`) and run `!submitroster` with no arguments.'
        )
        return

    picks = db.get_picks(draft['id'], drafter['id'])
    price_lookup, budget = theme.roster_requirements(draft_ctx)
    ok, errors = roster.validate_roster(picks, submitted, price_lookup, budget, theme.filler_player_names(draft_ctx))
    if ok:
        ok, errors = theme.validate_final_roster(draft_ctx, drafter, submitted)
    if not ok:
        await ctx.send('❌ Roster rejected:\n' + '\n'.join(f'• {e}' for e in errors))
        return

    for linked_id in theme.linked_drafter_ids(draft_ctx, drafter):
        db.set_roster_submitted(linked_id, True)
        db.update_drafter_state(linked_id, {'final_roster': submitted})

    await ctx.send(f"✅ Roster accepted ({len(submitted)} players). Teams are revealed once everyone has submitted.")
    if draft.get('admin_channel_id'):
        channel = bot.get_channel(draft['admin_channel_id'])
        if channel:
            fresh_drafter = db.get_drafter(drafter['id'])
            embed = _team_roster_embed(theme, draft_ctx, fresh_drafter)
            team_drafters = _dedup_teams(theme, draft_ctx, draft['id'])
            submitted_count = sum(1 for d in team_drafters if d['roster_submitted'])
            embed.set_footer(text=f"{draft['name']} — {submitted_count}/{len(team_drafters)} teams submitted")
            await channel.send('📋 New roster submitted:', embed=embed)
    await theme.on_roster_submitted(draft_ctx, drafter)


@bot.command(name='help')
async def cmd_help(ctx):
    embed = discord.Embed(
        title='ATD Draft Theme Bot',
        description='DM me these commands during a draft. Which ones apply depends on the draft format.',
        color=discord.Color.blue(),
    )
    embed.add_field(
        name='Drafting',
        value=(
            '`!pick <player>` — make your pick (Pack Draft)\n'
            '`!pack` — resend your current pack (Pack Draft)\n'
            '`!list P1, P2, P3` — submit your round list (Anonymous Partner Draft / Auto Draft)\n'
            '`!msg <text>` — message your partner (Anonymous Partner Draft)\n'
            '`!status` — what you\'re currently waiting on\n'
            '`!mypicks` — everything you\'ve drafted so far, sorted by price (privately)\n'
            '`!pref <Player A>, <Player B>, ...` — standing pick-priority list, auto-picked the instant '
            'one is in a pack you receive (Pack Draft)\n'
            '`!activate` — propose activating your nation\'s one-time power (Nation Flux)\n'
            '`!airreact` — Air\'s reactive counter-power, only while another nation\'s vote is open (Nation Flux)'
        ),
        inline=False,
    )
    embed.add_field(
        name='After picking',
        value=(
            '`!myteam` — your current roster/lineup view (Pack Draft)\n'
            '`!add <Player>` — auto-place a drafted player into an open slot (Pack Draft)\n'
            '`!swap <Player A> / <Player B>` — swap two players\' roster slots (Pack Draft)\n'
            '`!swap <Player> / <slot>` — move a player to a specific slot, 1-10 (Pack Draft)\n'
            '`!addyear <Player> <Year>` — set a drafted player\'s year (Pack Draft)\n'
            '`!submitroster P1, P2, ...` — submit your final roster for validation\n'
            '`!submitroster` (no arguments) — submit your current arrangement, once every slot is filled'
        ),
        inline=False,
    )
    if not isinstance(ctx.channel, discord.DMChannel):
        embed.add_field(
            name='Admin (this channel)',
            value=(
                '`!theme list` / `drafts` / `create` / `adddrafter` / `settings` / `start` / '
                '`status` / `team` / `pause` / `resume` / `reveal` / `cancel`'
            ),
            inline=False,
        )
        embed.add_field(
            name='Pack Draft moderator commands',
            value=(
                'Resolve implicitly from this channel — no draft id needed:\n'
                '`!importorder <division>` — reply to that division\'s lotto message\n'
                '`!generatepacks <division>` / `!loadpacks <division>` — build that division\'s packs\n'
                '`!startpackdraft` / `!pausepackdraft` / `!resumepackdraft`\n'
                '`!packskip @user` — force a skip\n'
                '`!packstatus` — per-division progress\n'
                '`!addteammate @existing_gm @new_gm` — add a GM to an existing team (solo → duo/trio)\n'
                '`!movedrafter @user <division> <seat>` — move a GM to a different division/seat (before they\'ve picked)\n'
                '`!divisionroster` — everyone, grouped by division and seat\n'
                '`!submittedrosters` — every team\'s submission status, and the roster for anyone who\'s submitted\n'
                '`!missingyears` — DM every submitted GM missing a year on one of their players\n'
                '`!teamlist` — every team\'s name, logo, and GM(s), 10 per page'
            ),
            inline=False,
        )
        embed.add_field(
            name='Anonymous Draft moderator commands',
            value=(
                '`!setteams <id>` — **DM only**, carries the secret logo→GM mapping\n'
                '`!theme start <id>` — opens pick #1; turn order runs itself from here\n'
                '`!anonoverride <id> <pick#> <team#/logo>` — safety valve to force-open a pick\n'
                'Picks post automatically to the results channel as each one finalizes.'
            ),
            inline=False,
        )
        embed.add_field(
            name='Nation Flux moderator commands',
            value=(
                '`!setteams <id>` — **DM only**, `EARTH`/`FIRE`/`WATER`/`AIR` headers, 8 teams each\n'
                '`!theme start <id>` — opens pick #1 in all 4 nations at once\n'
                'Each nation votes to activate its own one-time power with `!activate` / `!airreact`; '
                'prices are read live from the Flux Bot sheet, never typed by a GM.'
            ),
            inline=False,
        )
        embed.add_field(
            name='Auto Draft moderator commands',
            value=(
                '`!setteams <id>` — **DM only**, logos must match ATD Team Sheet Bot\'s emoji_map.py; line '
                'order also sets the starting tiebreaker waiver order\n'
                '`!theme start <id>` — opens round 1 for every team at once\n'
                'Each round, every team DMs `!list P1, P2, ..., P10` in priority order; once everyone\'s in '
                '(or the grace deadline hits) the round resolves automatically — same-rank ties on a player '
                'go to an equal-odds hidden lottery, and the winner moves to the back of the tiebreaker order. '
                'Each finalized pick posts as a bare `<logo> <Player>` line in this draft\'s admin channel, so '
                'ATD Team Sheet Bot (if it has that same channel mapped via `!setsheet`) writes it straight to '
                'the sheet — no `!submitroster`/`!theme reveal` needed for this format.'
            ),
            inline=False,
        )
    await ctx.send(embed=embed)


@bot.event
async def on_ready():
    log.info('ATD Draft Theme Bot ready — %s', bot.user)
    resumed_total = 0
    for draft in db.list_drafts(status='active'):
        theme = themes.get_theme(draft['theme'])
        draft_ctx = DraftContext(bot, draft['id'])
        try:
            resumed_total += await theme.resume_timers(draft_ctx)
        except Exception:
            log.exception('on_ready | failed to resume timers for draft_id=%s', draft['id'])

        deadline = draft['state'].get('roster_deadline')
        if deadline:
            remaining = deadline - time.time()
            try:
                if remaining <= 0:
                    await _resolve_roster_deadline(draft['id'])
                else:
                    await _start_roster_timer(draft['id'], remaining / 60)
                resumed_total += 1
            except Exception:
                log.exception('on_ready | failed to resume roster deadline for draft_id=%s', draft['id'])
    if resumed_total:
        log.info('on_ready | resumed %d timer(s) lost to a restart', resumed_total)

    # Catch up on the overnight window if a restart happened while it should
    # already be closed/open — otherwise a draft could sit wrongly active (or
    # wrongly paused) for up to ~22h until the next scheduled tick fires.
    if datetime.now(_ET).hour < 10:
        for draft in db.list_drafts(status='active'):
            await _apply_night_pause(draft)
    else:
        for draft in db.list_drafts(status='paused'):
            if draft['state'].get('night_paused'):
                await _apply_morning_resume(draft)

    if not _nightly_pause.is_running():
        _nightly_pause.start()
    if not _morning_resume.is_running():
        _morning_resume.start()


async def _handle_dm_shortcut(message: discord.Message):
    """Anonymous Draft's proposal prompt tells a teammate to just reply `1`
    or `2` — bare, with no `!` prefix. discord.py's command framework only
    ever looks at prefixed messages, so a bare "1"/"2" silently went
    nowhere. Route it through the same on_pick path `!pick 1`/`!pick 2`
    already handles correctly."""
    resolved = _resolve_active_drafter(message.author.id)
    if not resolved:
        return
    draft, drafter = resolved
    if draft['status'] != 'active':
        return
    theme = themes.get_theme(draft['theme'])
    reply = await theme.on_pick(DraftContext(bot, draft['id'], message=message), drafter, message.content.strip())
    if reply:
        await message.channel.send(reply)


@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)

    if not message.author.bot:
        if isinstance(message.channel, discord.DMChannel) and message.content.strip() in ('1', '2'):
            await _handle_dm_shortcut(message)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        return
    log.error('Command error in %s: %s', ctx.command, error)
    try:
        await ctx.send(f"❌ Error: `{error}`")
    except Exception:
        pass


if __name__ == '__main__':
    bot.run(DISCORD_TOKEN)
