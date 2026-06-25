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

VALID_LEVERAGES = {"all", "high", "medium", "low"}

def parse_wowy_args(args: str):
    """
    Parse: SAS Victor Wembanyama| De'Aaron Fox 2026 [PS] [high|low|medium|all]
    Supports:
        - Single year:        2026         → start=2026, end=2026
        - Dash range:         2022-2026    → start=2022, end=2026
        - Space range:        2022 2026    → start=2022, end=2026
        - Playoffs:           PS or playoffs keyword
        - Leverage:           high / low / medium / all (default: all)
    Returns (team, [player_names], start_year, end_year, season_type, leverage)
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

    # Extract playoffs flag (PS or playoffs, case-insensitive)
    season_type = "regular"
    ps_match = re.search(r'\b(PS|playoffs)\b', args, re.IGNORECASE)
    if ps_match:
        season_type = "playoffs"
        args = (args[:ps_match.start()] + args[ps_match.end():]).strip()

    # Extract leverage (high/low/medium/all) — standalone word
    leverage = "all"
    lev_match = re.search(r'\b(high|low|medium|all)\b', args, re.IGNORECASE)
    if lev_match:
        leverage = lev_match.group(1).lower()
        args = (args[:lev_match.start()] + args[lev_match.end():]).strip()

    # Extract year(s) from end of string
    # Space-separated range: "2022 2026"
    two_year_match = re.search(r'\b(\d{4})\s+(\d{4})\s*$', args)
    # Dash-separated range: "2022-2026"
    dash_range_match = re.search(r'\b(\d{4})-(\d{4})\s*$', args)
    # Single year
    single_year_match = re.search(r'\b(\d{4})\s*$', args)

    if two_year_match:
        start_year = int(two_year_match.group(1))
        end_year = int(two_year_match.group(2))
        args = args[:two_year_match.start()].strip()
    elif dash_range_match:
        start_year = int(dash_range_match.group(1))
        end_year = int(dash_range_match.group(2))
        args = args[:dash_range_match.start()].strip()
    elif single_year_match:
        end_year = int(single_year_match.group(1))
        start_year = end_year
        args = args[:single_year_match.start()].strip()
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

    return team, player_names, start_year, end_year, season_type, leverage


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


def _current_season_end_year() -> int:
    """
    Returns the end year of the current NBA season.
    WOWY uses end year: 2026 = the 2025-26 season.
    Oct–Dec 2025 → end year 2026 (season just started).
    Jan–Sep 2026 → end year 2026 (season ongoing).
    """
    from datetime import date
    today = date.today()
    return today.year + 1 if today.month >= 10 else today.year


def _is_current_season(start_year: int, end_year: int) -> bool:
    """
    Returns True if the request touches the ongoing season.
    A range query (e.g. 2022-2026) also counts as live if end_year
    reaches the current season, since the final year's data is still updating.
    """
    return end_year >= _current_season_end_year()


def _cache_path(team: str, player_ids: list, start_year: int, end_year: int, season_type: str, leverage: str) -> str:
    key = f"{team}_{'_'.join(str(p) for p in player_ids)}_{start_year}_{end_year}_{season_type}_{leverage}"
    return os.path.join(CACHE_DIR, f"{key}.png")


def _cache_get(team, player_ids, start_year, end_year, season_type, leverage):
    if _is_current_season(start_year, end_year):
        log.info(f"[CACHE] Live season ({start_year}-{end_year}) — skipping cache read")
        return None
    path = _cache_path(team, player_ids, start_year, end_year, season_type, leverage)
    if os.path.exists(path):
        log.info(f"[CACHE] Hit: {path}")
        with open(path, "rb") as f:
            return f.read()
    return None


def _cache_put(team, player_ids, start_year, end_year, season_type, leverage, data: bytes):
    if _is_current_season(start_year, end_year):
        log.info(f"[CACHE] Live season ({start_year}-{end_year}) — screenshot not cached")
        return
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = _cache_path(team, player_ids, start_year, end_year, season_type, leverage)
        with open(path, "wb") as f:
            f.write(data)
        log.info(f"[CACHE] Saved: {path}")
    except Exception as e:
        log.warning(f"[CACHE] Write failed: {e}")


# ==========================================================
# SCREENSHOT
# ==========================================================

DATABALLR_URL = "https://databallr.com/wowy/{team}/{start}/{end}/{season_type}/{leverage}/wowy/{players}"

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


async def screenshot_wowy(team: str, player_ids: list, start_year: int, end_year: int,
                          season_type: str = "regular", leverage: str = "all") -> bytes:
    # Check disk cache first
    cached = _cache_get(team, player_ids, start_year, end_year, season_type, leverage)
    if cached:
        return cached

    players_path = "/".join(str(pid) for pid in player_ids)
    url = DATABALLR_URL.format(
        team=team, start=start_year, end=end_year,
        season_type=season_type, leverage=leverage, players=players_path
    )
    log.info(f"[SCREENSHOT] {url}")

    async with _page_semaphore:
        browser = await _get_browser()
        page = await browser.new_page(viewport={"width": 1440, "height": 2400})
        try:
            # networkidle waits for React data fetches to complete.
            # Large queries (multi-year, high leverage) can take 30s+, so cap at 45s.
            try:
                await page.goto(url, wait_until="networkidle", timeout=45000)
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
                    timeout=60000,
                )
                await asyncio.sleep(0.5)
            except Exception:
                await asyncio.sleep(15)

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
                # Check if the page loaded at all (has databallr watermark)
                has_databallr = await page.evaluate("""
                    () => document.body.textContent.includes('databallr')
                """)
                if not has_databallr:
                    raise ValueError("SLOW_LOAD")
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

    _cache_put(team, player_ids, start_year, end_year, season_type, leverage, img)
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
            team, player_names, start_year, end_year, season_type, leverage = parse_wowy_args(args)
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

        log.info(f"[WOWY] team={team} players={resolved_names} {start_year}-{end_year} {season_type} leverage={leverage} by={ctx.author}")

        # Take screenshot
        try:
            img_bytes = await screenshot_wowy(team, player_ids, start_year, end_year, season_type, leverage)
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
            elif msg == "SLOW_LOAD":
                await ctx.send(
                    f"❌ The page took too long to load. Large date ranges or tight leverage filters can be slow — try again or narrow the range."
                )
            else:
                await ctx.send(f"❌ {msg}")
            return
        except Exception as e:
            log.error(f"[WOWY] Screenshot failed: {e}")
            await ctx.send("❌ Failed to load WOWY data. Try again in a moment.")
            return

        label = season_label(start_year, end_year)
        if season_type == "playoffs":
            label += " Playoffs"
        if leverage != "all":
            label += f" ({leverage} leverage)"
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
            "`!WOWY LAL LeBron James| Anthony Davis 2024 PS`  ← playoffs\n"
            "`!WOWY GSW Stephen Curry| Klay Thompson 2022 high`  ← high leverage\n"
            "`!WOWY BOS Jayson Tatum| Jaylen Brown 2022 2025`  ← multi-year\n"
            "`!WOWY BOS Larry Bird 1985 PS high`  ← playoffs + leverage"
        ),
        inline=False,
    )
    embed.add_field(
        name="Year Format",
        value=(
            "`2026` → 2025-26 season only\n"
            "`2022 2026` or `2022-2026` → spans 2021-22 through 2025-26"
        ),
        inline=False,
    )
    embed.add_field(
        name="Playoffs",
        value="Append `PS` or `playoffs` for playoff data.\nExample: `!WOWY BOS Larry Bird 1985 PS`",
        inline=False,
    )
    embed.add_field(
        name="Leverage",
        value=(
            "Filter by game leverage: `high` `low` `medium` `all` (default: `all`)\n"
            "Example: `!WOWY GSW Stephen Curry 2016 high`"
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
