#!/usr/bin/env python3
"""
ATD WOWY Bot — screenshots databallr.com WOWY lineup data.

Command: !WOWY <team> <player1>| <player2>| <player3> <year>
Example: !WOWY SAS Victor Wembanyama| De'Aaron Fox| Stephon Castle 2026
         !WOWY LAL LeBron James| Anthony Davis 2024
         !WOWY GSW Stephen Curry| Klay Thompson| Draymond Green 2022-2025
"""

import asyncio
import difflib
import io
import json
import logging
import os
import re
import unicodedata

import discord
from discord.ext import commands
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("wowy-bot")


def need(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise SystemExit(f"Missing env: {name}")
    return v


DISCORD_TOKEN = need("DISCORD_TOKEN")

# ==========================================================
# PLAYER LOOKUP
# ==========================================================

def _find_players_json() -> str:
    """Find players.json — same dir first, then sibling ATD Advanced Stats Bot."""
    here = os.path.dirname(os.path.abspath(__file__))
    local = os.path.join(here, "players.json")
    if os.path.exists(local):
        return local
    sibling = os.path.join(here, "..", "ATD Advanced Stats Bot", "players.json")
    if os.path.exists(sibling):
        return sibling
    raise SystemExit("players.json not found. Copy it from 'ATD Advanced Stats Bot/players.json'.")


with open(_find_players_json(), encoding="utf-8") as _f:
    _players_raw = json.load(_f)


def _norm(s: str) -> str:
    """Normalize name: strip accents, lowercase, remove punctuation."""
    s = unicodedata.normalize("NFD", s)
    s = s.encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


_players_norm = [(_norm(p["full_name"]), p) for p in _players_raw]
_player_cache: dict = {}


def find_player(name: str):
    """Fuzzy player name → {id, full_name, team, is_active}. Returns None if not found."""
    query = _norm(name)
    if not query:
        return None
    if query in _player_cache:
        return _player_cache[query]
    # Exact match
    for norm, p in _players_norm:
        if norm == query:
            _player_cache[query] = p
            return p
    # Partial match — prefer active players, then shorter names
    matches = [(norm, p) for norm, p in _players_norm if query in norm or norm in query]
    result = None
    if matches:
        matches.sort(key=lambda x: (not x[1]["is_active"], len(x[0])))
        result = matches[0][1]
    _player_cache[query] = result
    return result


# ==========================================================
# TEAM VALIDATION
# ==========================================================

VALID_TEAMS = {
    "ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW",
    "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK",
    "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS",
}

# Common wrong abbreviations → correct ones
TEAM_ALIASES = {
    "PHO": "PHX",  "GS": "GSW",   "NY": "NYK",   "SA": "SAS",
    "NO": "NOP",   "NOH": "NOP",  "NOK": "NOP",  "NJ": "BKN",
    "NJN": "BKN",  "SEA": "OKC",  "VAN": "MEM",  "CHH": "CHA",
    "WSB": "WAS",  "SDC": "LAC",  "KCK": "SAC",  "KC": "SAC",
    "GS": "GSW",   "OKL": "OKC",  "UTAH": "UTA", "NETS": "BKN",
}

def find_closest_team(abbr: str):
    """Return (corrected_abbr, is_alias) or None."""
    if abbr in TEAM_ALIASES:
        return TEAM_ALIASES[abbr], True
    matches = difflib.get_close_matches(abbr, VALID_TEAMS, n=1, cutoff=0.5)
    return (matches[0], False) if matches else None


# ==========================================================
# COMMAND PARSING
# ==========================================================

def parse_wowy_args(args: str):
    """
    Parse: SAS Victor Wembanyama| De'Aaron Fox| Stephon Castle 2026
    Supports:
        - Single year:  2026         → start=2026, end=2026
        - Year range:   2022-2026    → start=2022, end=2026
        - No year:      defaults to current season
    Returns (team, [player_names], start_year, end_year)
    Raises ValueError on bad input.
    """
    args = args.strip()
    if not args:
        raise ValueError("No arguments provided.")

    # Reject "2012-13" style season format — must use a single end year
    if re.search(r'\b\d{4}-\d{2}\b', args):
        raise ValueError(
            "Use a single year, not a season format.\n"
            "e.g. `2013` for the 2012-13 season, `2024` for 2023-24."
        )

    # Extract year or year range from end of string
    year_range_match = re.search(r'\b(\d{4})-(\d{4})\s*$', args)
    year_single_match = re.search(r'\b(\d{4})\s*$', args)

    if year_range_match:
        start_year = int(year_range_match.group(1))
        end_year = int(year_range_match.group(2))
        args = args[:year_range_match.start()].strip()
    elif year_single_match:
        end_year = int(year_single_match.group(1))
        start_year = end_year
        args = args[:year_single_match.start()].strip()
    else:
        raise ValueError(
            "Please include a year.\n"
            "e.g. `2026` for the 2025-26 season, `2014` for 2013-14."
        )

    # First token is team abbreviation
    tokens = args.split(None, 1)
    if not tokens:
        raise ValueError("No team specified.")
    team = tokens[0].upper()
    rest = tokens[1].strip() if len(tokens) > 1 else ""

    # Validate team abbreviation
    if team not in VALID_TEAMS:
        result = find_closest_team(team)
        if result:
            closest, _ = result
            raise ValueError(f"SUGGEST_TEAM:{team}:{closest}")
        else:
            raise ValueError(f"`{team}` is not a valid NBA team abbreviation.")

    # Split players by |
    player_names = [p.strip() for p in rest.split("|") if p.strip()]
    if not player_names:
        raise ValueError("No players specified.")
    if len(player_names) > 5:
        raise ValueError("Too many players (max 5).")

    return team, player_names, start_year, end_year


def season_label(start_year: int, end_year: int) -> str:
    """e.g. 2026 → '2025-26', 2022/2026 → '2021-22 to 2025-26'"""
    def fmt(y):
        return f"{y - 1}-{str(y)[-2:]}"
    if start_year == end_year:
        return fmt(end_year)
    return f"{fmt(start_year)} to {fmt(end_year)}"


# ==========================================================
# SCREENSHOT CACHE
# ==========================================================

CACHE_DIR = os.environ.get("CACHE_DIR", "/cache")


def _cache_path(team: str, player_ids: list, start_year: int, end_year: int) -> str:
    key = f"{team}_{'_'.join(str(p) for p in player_ids)}_{start_year}_{end_year}"
    return os.path.join(CACHE_DIR, f"{key}.png")


def _cache_get(team, player_ids, start_year, end_year):
    path = _cache_path(team, player_ids, start_year, end_year)
    if os.path.exists(path):
        log.info(f"[CACHE] Hit: {path}")
        with open(path, "rb") as f:
            return f.read()
    return None


def _cache_put(team, player_ids, start_year, end_year, data: bytes):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = _cache_path(team, player_ids, start_year, end_year)
        with open(path, "wb") as f:
            f.write(data)
        log.info(f"[CACHE] Saved: {path}")
    except Exception as e:
        log.warning(f"[CACHE] Write failed: {e}")


# ==========================================================
# SCREENSHOT
# ==========================================================

DATABALLR_URL = "https://databallr.com/wowy/{team}/{start}/{end}/regular/all/wowy/{players}"

# Persistent browser — launched once, reused across all requests
_pw_instance = None
_browser = None
_browser_launch_lock = asyncio.Lock()
_page_semaphore = asyncio.Semaphore(3)  # Max 3 concurrent pages


async def _get_browser():
    """Return the shared browser, (re)launching if needed."""
    global _pw_instance, _browser
    if _browser and _browser.is_connected():
        return _browser
    async with _browser_launch_lock:
        if _browser and _browser.is_connected():
            return _browser
        if _pw_instance:
            try:
                await _pw_instance.stop()
            except Exception:
                pass
        _pw_instance = await async_playwright().start()
        _browser = await _pw_instance.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        log.info("[BROWSER] Launched persistent browser")
        return _browser


async def screenshot_wowy(team: str, player_ids: list, start_year: int, end_year: int) -> bytes:
    # Check disk cache first
    cached = _cache_get(team, player_ids, start_year, end_year)
    if cached:
        return cached

    players_path = "/".join(str(pid) for pid in player_ids)
    url = DATABALLR_URL.format(
        team=team, start=start_year, end=end_year, players=players_path
    )
    log.info(f"[SCREENSHOT] {url}")

    async with _page_semaphore:
        browser = await _get_browser()
        page = await browser.new_page(viewport={"width": 1440, "height": 2400})
        try:
            # networkidle waits for React data fetches to complete.
            # Cap at 20s then proceed anyway — page usually has data by then.
            try:
                await page.goto(url, wait_until="networkidle", timeout=20000)
            except Exception:
                pass  # Timeout is fine — React data is likely loaded, just still polling

            # Wait for the WOWY lineup card to render.
            # We check for 'LINEUP' (the WOWY column header) + 'databallr' watermark.
            # 'LINEUP' only appears in the WOWY view — not in the ON-OFF player view
            # that shows when a player wasn't on the team.
            try:
                await page.wait_for_function(
                    """() => {
                        const t = document.body.textContent || '';
                        return t.includes('databallr') && t.includes('LINEUP');
                    }""",
                    timeout=30000,
                )
                await asyncio.sleep(0.5)
            except Exception:
                await asyncio.sleep(5)

            # Hide the scroll-to-top button and quick hotkeys bar via TreeWalker
            await page.evaluate("""
                () => {
                    function hideFixedAncestor(el) {
                        let cur = el;
                        while (cur && cur !== document.body) {
                            const s = window.getComputedStyle(cur);
                            if (s.position === 'fixed' || s.position === 'absolute') {
                                cur.style.display = 'none';
                                return true;
                            }
                            cur = cur.parentElement;
                        }
                        el.style.display = 'none';
                        return true;
                    }

                    // Use TreeWalker to find the exact text node with QUICK HOTKEYS
                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                    let node;
                    while ((node = walker.nextNode())) {
                        if (node.textContent.includes('QUICK HOTKEYS') || node.textContent.includes('QUICK HOT')) {
                            hideFixedAncestor(node.parentElement);
                            break;
                        }
                    }

                    // Hide fixed small elements (scroll-to-top button)
                    document.querySelectorAll('*').forEach(el => {
                        const s = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        if (s.position === 'fixed' && rect.width > 0 && rect.width < 70 && rect.height > 0 && rect.height < 70) {
                            el.style.display = 'none';
                        }
                    });
                }
            """)
            await asyncio.sleep(0.3)

            # Check for "Select Team" modal (invalid team abbreviation)
            has_team_modal = await page.evaluate("""
                () => {
                    const body = document.body.textContent || '';
                    return body.includes('Select Team') && body.includes('League Leaders');
                }
            """)
            if has_team_modal:
                raise ValueError("INVALID_TEAM")

            # If 'LINEUP' is not in the page, the player wasn't on the team —
            # databallr shows a PLAYER ON-OFF view (with dashes) instead of WOWY combinations.
            has_lineup_view = await page.evaluate("""
                () => document.body.textContent.includes('LINEUP')
            """)
            if not has_lineup_view:
                raise ValueError("NOT_ON_TEAM")

            # Find the data card — anchor on 'databallr' watermark which is inside the card.
            element_handle = await page.evaluate_handle("""
                () => {
                    const divs = Array.from(document.querySelectorAll('div'));
                    const candidates = divs.filter(el => {
                        const t = el.textContent || '';
                        return t.includes('databallr') && t.includes('LINEUP') && t.includes('MIN');
                    });
                    if (!candidates.length) return null;
                    candidates.sort((a, b) => a.textContent.length - b.textContent.length);
                    return candidates[0];
                }
            """)

            # Check for explicit "no combinations" message
            no_data = await page.evaluate("""
                () => {
                    const body = document.body.textContent || '';
                    return body.includes('No combinations found') ||
                           body.includes('no combinations found');
                }
            """)
            if no_data:
                raise ValueError("NO_DATA")

            element = element_handle.as_element()
            if not element:
                # Table didn't render — player(s) not on this team that year
                raise ValueError("NOT_ON_TEAM")

            box = await element.bounding_box()
            log.info(f"[SCREENSHOT] Table element found, box={box}")
            img = await element.screenshot()

        finally:
            await page.close()

    _cache_put(team, player_ids, start_year, end_year, img)
    return img


# ==========================================================
# BOT
# ==========================================================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


@bot.command(name="WOWY", aliases=["wowy"])
async def wowy_cmd(ctx, *, args: str = ""):
    """
    !WOWY <team> <player1>| <player2>| ... <year>
    Example: !WOWY SAS Victor Wembanyama| De'Aaron Fox| Stephon Castle 2026
    """
    if not args:
        await ctx.send(
            "Usage: `!WOWY <team> <player1>| <player2>| ... <year>`\n"
            "Example: `!WOWY SAS Victor Wembanyama| De'Aaron Fox| Stephon Castle 2026`\n"
            "Type `!WOWYhelp` for more info."
        )
        return

    async with ctx.typing():
        # Parse command
        try:
            team, player_names, start_year, end_year = parse_wowy_args(args)
        except ValueError as e:
            msg = str(e)
            if msg.startswith("SUGGEST_TEAM:"):
                _, typed, closest = msg.split(":")
                # Rebuild the command with the suggested team
                rest_of_cmd = args.strip()[len(typed):].strip()
                await ctx.send(
                    f"❌ `{typed}` isn't a valid team abbreviation.\n"
                    f"Did you mean **{closest}**? Try: `!WOWY {closest} {rest_of_cmd}`"
                )
            else:
                await ctx.send(f"❌ {msg}")
            return

        # Resolve player names → IDs
        player_ids = []
        resolved_names = []
        not_found = []
        for name in player_names:
            p = find_player(name)
            if not p:
                not_found.append(name)
            else:
                player_ids.append(p["id"])
                resolved_names.append(p["full_name"])

        if not_found:
            await ctx.send(
                f"❌ Could not find: **{', '.join(not_found)}**\n"
                "Check spelling or try a shorter name (e.g. `Wembanyama` instead of full name)."
            )
            return

        log.info(f"[WOWY] team={team} players={resolved_names} {start_year}-{end_year} by={ctx.author}")

        # Take screenshot
        try:
            img_bytes = await screenshot_wowy(team, player_ids, start_year, end_year)
        except ValueError as e:
            msg = str(e)
            if msg == "NOT_ON_TEAM":
                players_str = " + ".join(resolved_names)
                await ctx.send(
                    f"❌ **{players_str}** may not have been on **{team}** in **{season_label(start_year, end_year)}**.\n"
                    f"Check the team and year, then try again."
                )
            elif msg == "NO_DATA":
                await ctx.send(
                    f"❌ No lineup combinations found for **{team}** with those players in **{season_label(start_year, end_year)}**.\n"
                    f"Try a different year — e.g. `!WOWY {team} {' | '.join(player_names)} 2025`"
                )
            elif msg == "INVALID_TEAM":
                result = find_closest_team(team)
                if result:
                    closest, _ = result
                    await ctx.send(f"❌ `{team}` isn't a valid team. Did you mean **{closest}**?")
                else:
                    await ctx.send(f"❌ `{team}` isn't a valid NBA team abbreviation.")
            else:
                await ctx.send(f"❌ {msg}")
            return
        except Exception as e:
            log.error(f"[WOWY] Screenshot failed: {e}")
            await ctx.send("❌ Failed to load WOWY data. Try again in a moment.")
            return

        label = season_label(start_year, end_year)
        players_str = " + ".join(resolved_names)
        await ctx.send(
            f"**WOWY** | `{team}` | {players_str} | {label}",
            file=discord.File(io.BytesIO(img_bytes), filename="wowy.png"),
        )


@bot.command(name="WOWYhelp")
async def wowy_help_cmd(ctx):
    embed = discord.Embed(
        title="📊 WOWY Lineup Bot",
        description="With Or Without You — see how your team performs with different player combinations on/off the court.",
        color=0x1a1a2e,
    )
    embed.add_field(
        name="Command",
        value="`!WOWY <team> <player1>| <player2>| ... <year>`",
        inline=False,
    )
    embed.add_field(
        name="Examples",
        value=(
            "`!WOWY SAS Victor Wembanyama| De'Aaron Fox| Stephon Castle 2026`\n"
            "`!WOWY LAL LeBron James| Anthony Davis 2024`\n"
            "`!WOWY GSW Stephen Curry| Klay Thompson| Draymond Green 2022`\n"
            "`!WOWY BOS Jayson Tatum| Jaylen Brown 2022-2025`  ← multi-year"
        ),
        inline=False,
    )
    embed.add_field(
        name="Year Format",
        value=(
            "`2026` → 2025-26 season only\n"
            "`2022-2026` → spans 2021-22 through 2025-26\n"
            "Omit year → defaults to current season"
        ),
        inline=False,
    )
    embed.add_field(
        name="What the Table Shows",
        value=(
            "**MIN** — minutes played together\n"
            "**OFF** — offensive rating (pts per 100 possessions)\n"
            "**DEF** — defensive rating\n"
            "**NET** — net rating (OFF − DEF)\n\n"
            "Player portraits with gold ring = **ON** court\n"
            "Player portraits dimmed = **OFF** court\n"
            "Each row = a different on/off combination"
        ),
        inline=False,
    )
    embed.add_field(
        name="Players",
        value="Separate players with `|`. 1–5 players supported.",
        inline=False,
    )
    embed.add_field(
        name="Team Abbreviations",
        value=(
            "ATL BOS BKN CHA CHI CLE DAL DEN DET GSW\n"
            "HOU IND LAC LAL MEM MIA MIL MIN NOP NYK\n"
            "OKC ORL PHI PHX POR SAC SAS TOR UTA WAS"
        ),
        inline=False,
    )
    embed.set_footer(text="Data from databallr.com")
    await ctx.send(embed=embed)


@bot.event
async def on_ready():
    log.info(f"WOWY Bot ready as {bot.user}")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
