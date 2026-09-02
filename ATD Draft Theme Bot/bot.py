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

import discord
from discord.ext import commands

import db
import lotto
import roster
import sheets
import themes
from config import DISCORD_TOKEN, OWNER_ID, ROSTER_SPREADSHEET_ID, ROSTER_TAB_NAME, TIMER_BOT_ID
from themes.base import DraftContext

_ANON_THEME_KEYS = {'anon_draft', 'anon_budget_draft'}

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


def _validate_submission(theme, draft_ctx, drafter: dict, picks: list[str], submitted: list[str] | None,
                          price_lookup, budget) -> tuple[bool, list[str]]:
    if not submitted:
        return False, ["nothing to submit"]
    ok, errors = roster.validate_roster(picks, submitted, price_lookup, budget)
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

    if action in ('pause', 'resume'):
        draft = _resolve_draft(rest)
        if not draft:
            await ctx.send(f'Usage: `!theme {action} <draft_id_or_name>`')
            return
        db.set_draft_status(draft['id'], 'paused' if action == 'pause' else 'active')
        await ctx.send(f"⏸️ Paused." if action == 'pause' else "▶️ Resumed.")
        return

    if action == 'reveal':
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

    await ctx.send(
        "Usage: `!theme <list|drafts|create|adddrafter|settings|start|status|pause|resume|reveal|cancel|rostertimer> ...`\n"
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


@bot.command(name='fakelotto')
async def cmd_fakelotto(ctx, *, draft_ref: str = ''):
    """!fakelotto <draft_id_or_name> — generates the message to post in Timer
    Bot's channel so it registers every anonymous team without ever seeing
    a real GM (every line mentions this bot instead)."""
    if ctx.author.id != OWNER_ID:
        return
    draft = _resolve_draft(draft_ref)
    if not draft:
        await ctx.send('Usage: `!fakelotto <draft_id_or_name>`')
        return
    theme = themes.get_theme(draft['theme'])
    result = await theme.generate_fake_lotto(DraftContext(bot, draft['id']))
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
        teams[label] = theme.final_roster_rows(draft_ctx, d)

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
    await ctx.send('⏸️ Paused.')


@bot.command(name='resumepackdraft')
@commands.has_permissions(administrator=True)
async def cmd_resumepackdraft(ctx):
    """!resumepackdraft — resume the pack draft set up in this channel."""
    draft = _resolve_draft_for_channel(ctx.channel.id)
    if not draft:
        await ctx.send('No draft is set up in this channel yet.')
        return
    db.set_draft_status(draft['id'], 'active')
    await ctx.send('▶️ Resumed.')


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
    await ctx.send(result)


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
        await ctx.send(f"This draft is currently `{draft['status']}`.")
        return
    theme = themes.get_theme(draft['theme'])
    reply = await theme.on_pick(DraftContext(bot, draft['id'], message=ctx.message), drafter, text)
    # Themes may already have replied directly to ctx.message themselves
    # (e.g. a makeup-pick confirmation) — only send this if there's more to say.
    if reply:
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
        await ctx.send(f"This draft is currently `{draft['status']}`.")
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
        await ctx.send(f"This draft is currently `{draft['status']}`.")
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

    if not theme.is_pick_phase_complete(draft_ctx):
        await ctx.send("Picking isn't finished yet — hang tight until everyone's done.")
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
    ok, errors = roster.validate_roster(picks, submitted, price_lookup, budget)
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
            await channel.send(f"📋 **{drafter['display_name']}** submitted a roster for **{draft['name']}**.")
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
            '`!list P1, P2, P3` — submit your round list (Anonymous Partner Draft)\n'
            '`!msg <text>` — message your partner (Anonymous Partner Draft)\n'
            '`!status` — what you\'re currently waiting on\n'
            '`!mypicks` — everything you\'ve drafted so far, sorted by price (privately)'
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
                '`status` / `pause` / `resume` / `reveal` / `cancel`'
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
                '`!packstatus` — per-division progress'
            ),
            inline=False,
        )
        embed.add_field(
            name='Anonymous Draft moderator commands',
            value=(
                '`!setteams <id>` — **DM only**, carries the secret logo→GM mapping\n'
                '`!fakelotto <id>` — generates Timer Bot\'s registration message\n'
                '`!anonoverride <id> <pick#> <team#/logo>` — safety valve if tracking drifts\n'
                'Picks then relay automatically off Timer Bot\'s own turn pings in this channel.'
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


_TIMER_PICK_TITLE_RE = re.compile(r'Pick\s+(\d+)')
_TIMER_DEADLINE_RE = re.compile(r'<t:(\d+):R>')


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
    # discord.py only auto-processes commands when on_message is left
    # untouched — since Anonymous Draft needs to also watch for Timer Bot's
    # own ping embeds, this now has to do both explicitly.
    await bot.process_commands(message)

    if not message.author.bot:
        if isinstance(message.channel, discord.DMChannel) and message.content.strip() in ('1', '2'):
            await _handle_dm_shortcut(message)
        return

    if not message.embeds:
        return
    if TIMER_BOT_ID and message.author.id != TIMER_BOT_ID:
        return

    draft = _resolve_draft_for_channel(message.channel.id)
    if not draft or draft['theme'] not in _ANON_THEME_KEYS or draft['status'] != 'active':
        return

    embed = message.embeds[0]
    description = (embed.description or '').lower()
    if 'your turn' not in description:
        return

    title_match = _TIMER_PICK_TITLE_RE.search(embed.title or '')
    if not title_match:
        return
    pick_number = int(title_match.group(1))

    # Timer Bot's own "draft window is closed" ping (see _auto_pause_for_window
    # in ATD Timer Bot/bot.py) never carries a <t:...:R> deadline — its real
    # timer doesn't start until 10 AM ET. Treating that ping the same as a
    # live one used our fallback_deadline_minutes as a fake immediate
    # countdown, pressuring GMs with a deadline that wasn't real.
    theme = themes.get_theme(draft['theme'])
    window_closed = 'window is closed' in description
    if window_closed:
        deadline_ts = None
    else:
        deadline_match = _TIMER_DEADLINE_RE.search(embed.description or '')
        fallback_minutes = theme.default_settings().get('fallback_deadline_minutes', 60)
        deadline_ts = int(deadline_match.group(1)) if deadline_match else int(time.time()) + fallback_minutes * 60

    try:
        await theme.handle_timer_ping(DraftContext(bot, draft['id']), pick_number, deadline_ts, window_closed=window_closed)
    except Exception:
        log.exception('on_message | handle_timer_ping failed for draft_id=%s pick=%s', draft['id'], pick_number)


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
