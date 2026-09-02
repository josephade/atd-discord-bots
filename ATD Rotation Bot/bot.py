import asyncio
import io
import re

import aiohttp
import discord
from discord.ext import commands

from config import DISCORD_TOKEN
from emoji_map import EMOJI_TEAM_MAP, UNICODE_EMOJI_MAP
from sheets import get_roster
from storage import get_rotation, set_rotation, clear_rotation, count_rotations, clear_all_rotations
from parser import parse_rotation_body
from render import compute_player_totals, render_rotation_card, render_minutes_chart
from team_colors import get_team_colors

_CUSTOM_EMOJI_RE = re.compile(r'<a?:([\w~]+):(\d+)>')

COMMISSIONER_ROLE = "LeComissioner"


def _is_commissioner(ctx) -> bool:
    if ctx.author.guild_permissions.administrator:
        return True
    return any(r.name == COMMISSIONER_ROLE for r in ctx.author.roles)


def require_commissioner():
    async def predicate(ctx):
        if _is_commissioner(ctx):
            return True
        raise commands.CheckFailure("not_commissioner")
    return commands.check(predicate)


HELP_TEXT = (
    "**Rotation Bot**\n"
    "`!setrotation <team emoji>` then, on following lines:\n"
    "  optional nicknames — `Nick=Full Name` (one per line)\n"
    "  segments — `<minutes> PG SG SF PF C` (one per line, names or nicknames, in that position order)\n\n"
    "Last names work directly if they're unambiguous on the roster (no legend needed), "
    "and common nicknames like `DrJ` or `Spida` are recognized automatically.\n\n"
    "Example:\n"
    "```\n"
    "!setrotation <:Lakers:12345>\n"
    "DrJ=Julius Erving\n"
    "Roco=Robert Covington\n"
    "10 Billups Luka DrJ Kemp Horford\n"
    "10 Luka DJ Roco Kemp Horford\n"
    "8 Billups Redd Roco DrJ Kemp\n"
    "```\n"
    "Minutes and totals are computed automatically from the segments.\n\n"
    "`!rotation <team emoji>` — show the team's saved rotation.\n\n"
    f"**Commissioner / Administrator only** (requires the **{COMMISSIONER_ROLE}** role or server Administrator):\n"
    "`!clearrotation <team emoji>` — delete one team's saved rotation.\n"
    "`!clearallrotations confirm` — wipe every team's saved rotation (e.g. for a new ATD)."
)


def _friendly_sheet_error(exc: Exception) -> str:
    print(f"[SheetError] {exc}")
    text = str(exc)
    if "<html" in text.lower() or "500" in text or "502" in text or "503" in text:
        return "Google Sheets is temporarily unavailable. Please try again in a moment."
    return text.splitlines()[0][:200]


def _resolve_team(text_line: str):
    """Returns (team_name, logo_url, error). logo_url is None for a plain
    unicode emoji (no image to fetch) or when nothing matched."""
    m = _CUSTOM_EMOJI_RE.search(text_line or "")
    if m:
        emoji_name = m.group(1)
        team = EMOJI_TEAM_MAP.get(emoji_name)
        if not team:
            return None, None, f"Unrecognised emoji **:{emoji_name}:**. Add it to `emoji_map.py`."
        logo_url = str(discord.PartialEmoji.from_str(m.group(0)).url)
        return team, logo_url, None
    for char, team_name in UNICODE_EMOJI_MAP.items():
        if char in (text_line or ""):
            return team_name, None, None
    return None, None, "No team emoji found. Include your team's emoji, e.g. `!rotation <:YourTeam:>`."


async def _fetch_logo_bytes(url: str | None) -> bytes | None:
    """Download the GM's team emoji image from Discord's CDN to use as the
    rotation card's logo. Best-effort — any failure just means no logo,
    never a hard error for the command."""
    if not url:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception as e:
        print(f"[Logo] Failed to fetch {url}: {e}")
    return None


async def _fetch_roster(ctx, team):
    loop = asyncio.get_event_loop()
    try:
        roster, err = await loop.run_in_executor(None, get_roster, team)
    except Exception as e:
        await ctx.send(f"❌ Sheet error: {_friendly_sheet_error(e)}")
        return None
    if err:
        await ctx.send(f"❌ {err}")
        return None
    if not roster:
        await ctx.send(f"❌ No roster found for **{team}** — add players via the team sheet first.")
        return None
    return roster


async def _send_rotation(ctx, team, roster, segments, logo_bytes=None):
    totals = compute_player_totals(roster, segments)
    colors = get_team_colors(team)
    loop = asyncio.get_event_loop()
    card_bytes, chart_bytes = await loop.run_in_executor(
        None,
        lambda: (
            render_rotation_card(team, colors, totals, segments, logo_bytes=logo_bytes),
            render_minutes_chart(team, totals),
        ),
    )
    files = [
        discord.File(io.BytesIO(card_bytes), filename="rotation.png"),
        discord.File(io.BytesIO(chart_bytes), filename="minutes.png"),
    ]
    await ctx.send(files=files)


intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # required to read ctx.author.roles for the commissioner check

bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.send(
            f"❌ You don't have permission to use this command. "
            f"Requires the **{COMMISSIONER_ROLE}** role or server Administrator."
        )
        return
    print(f"[Error] {ctx.command}: {error}")
    await ctx.send(f"❌ Something went wrong: {error}")


@bot.command(name='setrotation')
async def setrotation(ctx, *, body: str = ""):
    lines = body.splitlines()
    if not lines or not lines[0].strip():
        await ctx.send("❌ Usage: `!setrotation <team emoji>` then rotation lines. See `!rotationhelp`.")
        return

    emoji_line, *rest_lines = lines
    team, logo_url, err = _resolve_team(emoji_line)
    if err:
        await ctx.send(f"❌ {err}")
        return

    async with ctx.typing():
        roster = await _fetch_roster(ctx, team)
        if roster is None:
            return

        nicknames, segments, err = parse_rotation_body(rest_lines, roster)
        if err:
            await ctx.send(f"❌ {err}")
            return

        set_rotation(team, nicknames, segments, ctx.author.id)
        logo_bytes = await _fetch_logo_bytes(logo_url)
        await _send_rotation(ctx, team, roster, segments, logo_bytes=logo_bytes)


@bot.command(name='rotation')
async def rotation(ctx, *, body: str = ""):
    team, logo_url, err = _resolve_team(body)
    if err:
        await ctx.send(f"❌ {err}")
        return

    saved = get_rotation(team)
    if not saved:
        await ctx.send(
            f"❌ No rotation saved for **{team}** yet. "
            f"Set one with `!setrotation <emoji> ...` — see `!rotationhelp`."
        )
        return

    async with ctx.typing():
        roster = await _fetch_roster(ctx, team)
        if roster is None:
            return
        logo_bytes = await _fetch_logo_bytes(logo_url)
        await _send_rotation(ctx, team, roster, saved["segments"], logo_bytes=logo_bytes)


@bot.command(name='clearrotation')
@require_commissioner()
async def clearrotation(ctx, *, body: str = ""):
    team, _logo_url, err = _resolve_team(body)
    if err:
        await ctx.send(f"❌ {err}")
        return

    if clear_rotation(team):
        await ctx.send(f"🗑️ Rotation cleared for **{team}**.")
    else:
        await ctx.send(f"Nothing saved for **{team}**.")


@bot.command(name='clearallrotations')
@require_commissioner()
async def clearallrotations(ctx, confirm: str = ""):
    count = count_rotations()
    if count == 0:
        await ctx.send("Nothing saved — no rotations to clear.")
        return

    if confirm.lower() != "confirm":
        await ctx.send(
            f"⚠️ This wipes **{count}** saved rotation{'s' if count != 1 else ''} for **every** team, not just one. "
            f"Run `!clearallrotations confirm` to proceed."
        )
        return

    removed = clear_all_rotations()
    await ctx.send(f"🗑️ Cleared all **{removed}** saved rotation{'s' if removed != 1 else ''} — fresh start.")


@bot.command(name='rotationhelp')
async def rotationhelp(ctx):
    await ctx.send(HELP_TEXT)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
