#!/usr/bin/env python3
"""
ATD RAPM Bot — screenshots nbarapm.com player impact data.

Command: !rapm <player name>
Example: !rapm lebron james
         !rapm shaquille o'neal
         !rapm luka doncic
"""

import asyncio
import io
import json
import logging
import os
import re
import time
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
log = logging.getLogger("rapm-bot")


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
    """Fuzzy player name -> {id, full_name, team, is_active}. Returns None if not found."""
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


def slugify_rapm(name: str) -> str:
    """nbarapm.com slug: lowercase, accents stripped, apostrophes kept, spaces -> hyphens."""
    s = unicodedata.normalize("NFD", name)
    s = s.encode("ascii", "ignore").decode()
    s = s.lower().replace(".", "")
    s = re.sub(r"[^a-z0-9' -]", "", s)
    s = re.sub(r"\s+", "-", s.strip())
    return s


# ==========================================================
# SCREENSHOT CACHE
# ==========================================================

CACHE_DIR = os.environ.get("CACHE_DIR", "/cache")
CACHE_TTL_SECS = 30 * 60  # RAPM values shift as games are played — keep this short


def _current_season_end_year() -> int:
    """2026 = the 2025-26 season. Oct-Dec -> season just started; Jan-Sep -> season ongoing."""
    from datetime import date
    today = date.today()
    return today.year + 1 if today.month >= 10 else today.year


def _cache_path(slug: str) -> str:
    return os.path.join(CACHE_DIR, f"{slug}.json")


def _cache_get(cache_key: str, permanent: bool = False):
    """permanent=True skips the TTL check entirely — used for completed past
    seasons (like the Shotmap/WOWY bots' cache), which can never change, so a
    second user's request is served instantly instead of re-scraping."""
    path = _cache_path(cache_key)
    if not os.path.exists(path):
        return None
    if not permanent and time.time() - os.path.getmtime(path) > CACHE_TTL_SECS:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        images = [(bytes.fromhex(h), label) for h, label in data["images"]]
        return images, data["name"], data["team"], data["url"]
    except Exception:
        return None


def _cache_put(cache_key: str, images: list, name: str, team: str, url: str):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_cache_path(cache_key), "w", encoding="utf-8") as f:
            json.dump({
                "images": [[img.hex(), label] for img, label in images],
                "name": name, "team": team, "url": url,
            }, f)
    except Exception as e:
        log.warning(f"[CACHE] Write failed: {e}")


# ==========================================================
# SCREENSHOT
# ==========================================================

BASE_URL = "https://www.nbarapm.com"

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


async def _reset_browser():
    """Discard the shared browser/playwright instance so the next request
    launches a fresh one. Used when a request times out — a wedged CDP
    connection usually means the whole browser is broken, not just one page."""
    global _browser, _pw_instance
    old_browser, old_pw = _browser, _pw_instance
    _browser = None
    _pw_instance = None
    if old_browser:
        try:
            await asyncio.wait_for(old_browser.close(), timeout=5)
        except Exception:
            pass
    if old_pw:
        try:
            await asyncio.wait_for(old_pw.stop(), timeout=5)
        except Exception:
            pass


SCREENSHOT_TIMEOUT_SECS = 60

# Tables to check, in priority order. Older/retired players often have no
# current-season model output (DARKO/LEBRON/etc. don't retroactively cover
# their era) but do still have historical impact tables — show whichever
# tables actually have data instead of just erroring out.
CANDIDATE_SECTIONS = [
    ("current-metrics-rapm-section", "Current Impact Metrics"),
    ("six-factor-impact-section", "4-Year Factor RAPMs"),
    ("career-metrics-rapm-section", "Peak/Career Metrics"),
]

MODE_MAP = {
    "advanced": "advanced",
    "pergame": "pergame",
    "per game": "pergame",
    "per-game": "pergame",
    "per75": "per75",
    "per 75": "per75",
    "per100": "per100",
    "per 100": "per100",
}
MODE_LABELS = {
    "advanced": "Advanced",
    "pergame": "Per Game",
    "per75": "Per 75",
    "per100": "Per 100",
}


def parse_mode(text: str):
    """Map free-text like 'per game' / 'per75' -> internal mode value, or None."""
    key = re.sub(r"\s+", " ", text.strip().lower())
    return MODE_MAP.get(key)


async def screenshot_rapm_bundle(display_name: str, slug: str) -> tuple:
    """Returns (images, player_name, team_position, page_url) where images is
    a list of (img_bytes, label) — one per populated impact-tab table.
    Raises ValueError on failure."""
    cache_key = slug
    cached = _cache_get(cache_key)
    if cached:
        return cached

    try:
        result = await asyncio.wait_for(
            _screenshot_bundle_inner(display_name, slug),
            timeout=SCREENSHOT_TIMEOUT_SECS,
        )
    except asyncio.TimeoutError:
        log.error(f"[SCREENSHOT] Timed out after {SCREENSHOT_TIMEOUT_SECS}s — resetting browser")
        await _reset_browser()
        raise ValueError("SCREENSHOT_TIMEOUT")

    _cache_put(cache_key, *result)
    return result


async def _find_all_populated_sections(page):
    """Return [(section_id, label), ...] for every candidate table that has actual rows."""
    found = []
    for section_id, label in CANDIDATE_SECTIONS:
        count = await page.evaluate(f"""
            () => {{
                const sec = document.getElementById('{section_id}');
                return sec ? sec.querySelectorAll('.tabulator-row').length : 0;
            }}
        """)
        if count > 0:
            found.append((section_id, label))
    return found


# Tabulator (the grid library nbarapm.com uses) virtualizes rows inside a
# fixed-height scrollable holder and caps the outer .tabulator element with
# max-height/overflow:hidden — so a plain element screenshot clips rows that
# don't fit in the default viewport-sized box. Force both to their true
# measured content height before capturing.
_EXPAND_TABLE_JS = """
(sectionId) => {
    const sec = document.getElementById(sectionId);
    const holder = sec.querySelector('.tabulator-tableholder');
    const tab = sec.querySelector('.tabulator');
    const rows = Array.from(sec.querySelectorAll('.tabulator-row'));
    let maxBottom = 0;
    for (const r of rows) {
        const bottom = r.offsetTop + r.offsetHeight;
        if (bottom > maxBottom) maxBottom = bottom;
    }
    holder.style.setProperty('overflow', 'visible', 'important');
    holder.style.setProperty('height', (maxBottom + 4) + 'px', 'important');
    const header = sec.querySelector('.tabulator-header');
    const headerH = header ? header.offsetHeight : 0;
    tab.style.setProperty('overflow', 'visible', 'important');
    tab.style.setProperty('max-height', 'none', 'important');
    tab.style.setProperty('height', (headerH + maxBottom + 8) + 'px', 'important');
}
"""

# The site shows a fixed-position bottom bar (teammates/search icons) that
# overlaps whatever happens to be scrolled into that screen region — hide
# any fixed-position element before screenshotting.
_HIDE_FIXED_JS = """
() => {
    document.querySelectorAll('*').forEach(el => {
        if (window.getComputedStyle(el).position === 'fixed') {
            el.style.setProperty('display', 'none', 'important');
        }
    });
}
"""


async def _goto_player_page(page, display_name: str, slug: str):
    """Navigate to the player's page, trying the guessed slug first and
    falling back to the site's own search box. Raises ValueError('NOT_FOUND')
    if neither resolves to a real, recognized player."""
    url = f"{BASE_URL}/player/{slug}"
    log.info(f"[SCREENSHOT] {url}")
    try:
        await page.goto(url, wait_until="networkidle", timeout=30000)
    except Exception:
        pass

    if await _find_all_populated_sections(page):
        return

    log.info(f"[SCREENSHOT] Slug '{slug}' had no populated tables, falling back to search for '{display_name}'")
    try:
        await page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
        await page.fill("#landing-search-input", display_name)
        await page.wait_for_selector(".landing-suggestion", timeout=5000)
        if await page.locator(".landing-suggestion").count() == 0:
            raise ValueError("NOT_FOUND")
        await page.locator(".landing-suggestion").first.click()
        await page.wait_for_load_state("networkidle", timeout=30000)
    except ValueError:
        raise
    except Exception:
        raise ValueError("NOT_FOUND")


async def _extract_name_team(page):
    name = await page.evaluate("""
        () => document.querySelector('.t-stagger.is-shown p.t-stagger-line')?.textContent.trim() || null
    """)
    team = await page.evaluate("""
        () => document.querySelector('.bio-team-position')?.textContent.trim() || null
    """)
    return name, team


async def _screenshot_bundle_inner(display_name: str, slug: str) -> tuple:
    # Expanding one section's height (to defeat Tabulator's virtual-scroll
    # clipping) reflows the whole page's CSS grid, which can jumble the
    # size/position of *other* sections we haven't captured yet. Rather than
    # fight that, each populated section gets its own fresh page load so its
    # expand-and-capture happens in isolation.
    async with _page_semaphore:
        browser = await _get_browser()
        page = await browser.new_page(viewport={"width": 900, "height": 2000})
        try:
            await _goto_player_page(page, display_name, slug)
            found = await _find_all_populated_sections(page)
            if not found:
                # Site knows this player (search resolved them) but has no
                # data in any of the tables we check — e.g. pre-tracking-era retirees.
                raise ValueError("NO_DATA")
            name, team = await _extract_name_team(page)
            page_url = page.url
        finally:
            try:
                await asyncio.wait_for(page.close(), timeout=5)
            except Exception:
                pass

        images = []
        for section_id, label in found:
            img = await _capture_one_section(browser, display_name, slug, section_id)
            if img is not None:
                images.append((img, label))

        if not images:
            raise ValueError("NO_DATA")

        return images, name or display_name, team, page_url


async def _capture_one_section(browser, display_name: str, slug: str, section_id: str):
    page = await browser.new_page(viewport={"width": 900, "height": 2000})
    try:
        await _goto_player_page(page, display_name, slug)
        await asyncio.sleep(0.3)  # let the tabulator grid finish painting
        await page.evaluate(_HIDE_FIXED_JS)
        await page.evaluate(_EXPAND_TABLE_JS, section_id)
        await asyncio.sleep(0.2)
        element = await page.query_selector(f"#{section_id} .tabulator")
        if not element:
            return None
        return await element.screenshot()
    finally:
        try:
            await asyncio.wait_for(page.close(), timeout=5)
        except Exception:
            pass


async def screenshot_dashboard(display_name: str, slug: str, mode: str, year: str) -> tuple:
    """Returns (img_bytes, player_name, team_position, page_url, title_label).
    Raises ValueError('NOT_FOUND' | 'INVALID_YEAR:<min>:<max>' | 'SCREENSHOT_TIMEOUT') on failure."""
    cache_key = f"{slug}__dash__{mode}__{year}"
    is_current_season = int(year) >= _current_season_end_year()
    cached = _cache_get(cache_key, permanent=not is_current_season)
    if cached:
        images, name, team, url = cached
        return images[0][0], name, team, url, images[0][1]

    try:
        result = await asyncio.wait_for(
            _screenshot_dashboard_inner(display_name, slug, mode, year),
            timeout=SCREENSHOT_TIMEOUT_SECS,
        )
    except asyncio.TimeoutError:
        log.error(f"[SCREENSHOT] Timed out after {SCREENSHOT_TIMEOUT_SECS}s — resetting browser")
        await _reset_browser()
        raise ValueError("SCREENSHOT_TIMEOUT")

    img, name, team, page_url, title = result
    _cache_put(cache_key, [(img, title)], name, team, page_url)
    return img, name, team, page_url, title


async def _apply_dashboard_selection(page, mode: str, year: str,
                                      timeout_secs: float = 8.0, poll_secs: float = 0.15,
                                      settle_secs: float = 0.5) -> bool:
    """Select mode+year on the dashboard and wait for the stat tiles to
    actually finish re-rendering before returning True. A fixed sleep isn't
    reliable under real load — confirmed in production: a stale/wrong-year
    screenshot got permanently cached because the tiles hadn't updated yet
    when the screenshot was taken.

    The re-render isn't a single atomic swap either: the site updates the
    main stat numbers first and the percentile bars/ranks a bit after (seen
    directly — content kept changing for ~0.6s after the *first* visible
    change), so stopping at the first difference still risks capturing a
    half-updated frame. This waits for the content to stop changing for
    `settle_secs` straight before accepting it as done.

    Some dashboard modes (e.g. 'advanced') don't render any title text with
    the year in it at all, so instead of looking for a specific string this
    snapshots the container's content before selecting and compares against
    that — which works the same way regardless of mode. Returns True
    immediately if the selectors already matched mode/year on load (nothing
    to re-render)."""
    current_mode = await page.eval_on_selector("#configSelector", "el => el.value")
    current_year = await page.eval_on_selector("#yearSelector", "el => el.value")
    if current_mode == mode and current_year == year:
        return True

    before_text = await page.eval_on_selector("#stats-container", "el => el.innerText")
    if current_mode != mode:
        await page.select_option("#configSelector", mode)
    if current_year != year:
        await page.select_option("#yearSelector", year)

    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_secs
    last_text = before_text
    stable_since = None

    while loop.time() < deadline:
        current_text = await page.eval_on_selector("#stats-container", "el => el.innerText")
        if current_text == before_text:
            stable_since = None  # hasn't started changing yet
        elif current_text == last_text:
            if stable_since is None:
                stable_since = loop.time()
            elif loop.time() - stable_since >= settle_secs:
                return True
        else:
            stable_since = None  # still actively changing
        last_text = current_text
        await asyncio.sleep(poll_secs)
    return False


async def _screenshot_dashboard_inner(display_name: str, slug: str, mode: str, year: str) -> tuple:
    async with _page_semaphore:
        browser = await _get_browser()
        page = await browser.new_page(viewport={"width": 900, "height": 2000})
        try:
            await _goto_player_page(page, display_name, slug)

            clicked = await page.evaluate("""
                () => {
                    const els = Array.from(document.querySelectorAll('button.tablinks'));
                    const el = els.find(e => e.textContent.trim().toLowerCase() === 'dashboard');
                    if (el) { el.click(); return true; }
                    return false;
                }
            """)
            if not clicked:
                raise ValueError("NOT_FOUND")

            await page.wait_for_selector("#configSelector", timeout=10000)
            await page.wait_for_selector("#yearSelector", timeout=10000)

            available_years = await page.eval_on_selector_all(
                "#yearSelector option", "opts => opts.map(o => o.value)"
            )
            if year not in available_years:
                nums = [int(y) for y in available_years if y.isdigit()]
                lo, hi = (min(nums), max(nums)) if nums else (None, None)
                raise ValueError(f"INVALID_YEAR:{lo}:{hi}")

            if not await _apply_dashboard_selection(page, mode, year):
                # Under real load a fixed sleep isn't always enough time for
                # the tiles to re-render before we screenshot — better to
                # error out (and not cache a bad result) than silently show
                # stale data for the wrong year.
                raise ValueError("RENDER_TIMEOUT")

            await page.evaluate(_HIDE_FIXED_JS)

            element = await page.query_selector("#stats-container")
            if not element:
                raise ValueError("NOT_FOUND")

            name, team = await _extract_name_team(page)
            page_url = page.url
            title = f"{MODE_LABELS.get(mode, mode)} · {year}"

            img = await element.screenshot()
            return img, name or display_name, team, page_url, title
        finally:
            try:
                await asyncio.wait_for(page.close(), timeout=5)
            except Exception:
                pass


# ==========================================================
# BOT
# ==========================================================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


# Trailing "<mode> <year>" or just "<year>" at the end of the command args.
_MODE_PHRASES = sorted(MODE_MAP.keys(), key=len, reverse=True)  # longest first so "per game" beats "per"


_TRAILING_YEAR_RE = re.compile(r"(\d{4})\s*$")


def _parse_trailing_year_mode(args: str):
    """Split '<player> [<mode>] [<year>]' from the end of the string, in
    either order — 'per 100 2017' or '2017 per 100' both work.
    Returns (player_name, mode_or_None, year_or_None). If no trailing year is
    found at all, returns (args, None, None) — the plain lookup case."""
    text = args

    year = None
    m = _TRAILING_YEAR_RE.search(text)
    if m:
        year = m.group(1)
        text = text[:m.start()].rstrip()

    mode = None
    for phrase in _MODE_PHRASES:
        pattern = re.compile(r"\b" + re.escape(phrase) + r"\b\s*$", re.IGNORECASE)
        pm = pattern.search(text)
        if pm:
            mode = MODE_MAP[phrase]
            text = text[:pm.start()].rstrip()
            break

    if year is None:
        # The year might have been written *before* the mode phrase (e.g.
        # "kyle lowry 2017 per 100") — now that the mode's peeled off, check
        # the new trailing edge of the string.
        m2 = _TRAILING_YEAR_RE.search(text)
        if m2:
            year = m2.group(1)
            text = text[:m2.start()].rstrip()

    if year is None:
        return args, None, None

    return text, mode, year


async def _send_dashboard_reply(ctx, display_name: str, slug: str, mode: str, year: str):
    try:
        img_bytes, name, team, page_url, title = await screenshot_dashboard(display_name, slug, mode, year)
    except ValueError as e:
        msg = str(e)
        if msg == "NOT_FOUND":
            await ctx.send(f"❌ Couldn't find a player matching **{display_name}** on nbarapm.com.")
        elif msg.startswith("INVALID_YEAR"):
            _, lo, hi = msg.split(":")
            await ctx.send(f"❌ **{display_name}** doesn't have data for {year}. Valid range: {lo}-{hi}.")
        elif msg == "SCREENSHOT_TIMEOUT":
            await ctx.send(
                "❌ That took too long and the browser got stuck — it's been reset automatically. Please try again."
            )
        elif msg == "RENDER_TIMEOUT":
            await ctx.send("❌ The site took too long to load that year's data. Please try again.")
        else:
            await ctx.send(f"❌ {msg}")
        return
    except Exception as e:
        log.error(f"[RAPM] Dashboard screenshot failed: {e}")
        await ctx.send("❌ Failed to load dashboard data. Try again in a moment.")
        return

    caption = f"**{name}**"
    if team:
        caption += f" · {team}"
    caption += f"\n{title}"
    await ctx.send(caption, file=discord.File(io.BytesIO(img_bytes), filename="rapm_dashboard.png"))


@bot.command(name="rapm")
async def rapm_cmd(ctx, *, args: str = ""):
    """
    !rapm <player name>
    !rapm <player name> <advanced|per game|per 75|per 100> <year>
    Example: !rapm lebron james
             !rapm lebron james advanced 2024
    """
    if not args:
        await ctx.send(
            "Usage: `!rapm <player name>` or `!rapm <player name> <advanced|per game|per 75|per 100> <year>`\n"
            "Example: `!rapm lebron james` or `!rapm lebron james advanced 2024`"
        )
        return

    name_part, mode, year = _parse_trailing_year_mode(args)

    async with ctx.typing():
        player = find_player(name_part)
        display_name = player["full_name"] if player else name_part.strip()
        slug = slugify_rapm(display_name)

        if year and mode:
            log.info(f"[RAPM] query='{args}' resolved='{display_name}' slug='{slug}' mode={mode} year={year} by={ctx.author}")
            await _send_dashboard_reply(ctx, display_name, slug, mode, year)
            return

        if year and not mode:
            # Have a year but no recognizable stat-type keyword — ask for it.
            await ctx.send(
                f"Which stat view do you want for **{display_name}** ({year})? "
                "Reply with `advanced`, `per game`, `per 75`, or `per 100`."
            )

            def check(m):
                return m.author == ctx.author and m.channel == ctx.channel

            try:
                reply = await bot.wait_for("message", check=check, timeout=60)
            except asyncio.TimeoutError:
                await ctx.send("❌ Timed out waiting for a reply.")
                return

            mode = parse_mode(reply.content)
            if not mode:
                await ctx.send(
                    f"❌ Didn't recognize `{reply.content}`. "
                    "Please redo the command with one of: `advanced`, `per game`, `per 75`, `per 100`."
                )
                return

            log.info(f"[RAPM] query='{args}' resolved='{display_name}' slug='{slug}' mode={mode} (clarified) year={year} by={ctx.author}")
            await _send_dashboard_reply(ctx, display_name, slug, mode, year)
            return

        # Plain lookup — no year given, send the impact-tab table bundle.
        log.info(f"[RAPM] query='{args}' resolved='{display_name}' slug='{slug}' by={ctx.author}")

        try:
            images, name, team, page_url = await screenshot_rapm_bundle(display_name, slug)
        except ValueError as e:
            msg = str(e)
            if msg == "NOT_FOUND":
                await ctx.send(f"❌ Couldn't find a player matching **{args.strip()}** on nbarapm.com.")
            elif msg == "NO_DATA":
                await ctx.send(
                    f"❌ **{display_name}** is on nbarapm.com, but has no impact data in any table "
                    "(common for players from before the site's tracking-data coverage)."
                )
            elif msg == "SCREENSHOT_TIMEOUT":
                await ctx.send(
                    "❌ That took too long and the browser got stuck — it's been reset automatically. Please try again."
                )
            else:
                await ctx.send(f"❌ {msg}")
            return
        except Exception as e:
            log.error(f"[RAPM] Screenshot failed: {e}")
            await ctx.send("❌ Failed to load RAPM data. Try again in a moment.")
            return

        caption = f"**{name}**"
        if team:
            caption += f" · {team}"
        files = [
            discord.File(io.BytesIO(img), filename=f"rapm_{i}.png")
            for i, (img, _label) in enumerate(images)
        ]
        await ctx.send(caption, files=files)


@bot.command(name="rapmhelp")
async def rapm_help_cmd(ctx):
    embed = discord.Embed(
        title="📈 RAPM Bot",
        description="Impact metrics (RAPM/DARKO/LEBRON/LAKER) and dashboard stats from nbarapm.com.",
        color=0x1a1a2e,
    )
    embed.add_field(
        name="Impact Lookup",
        value=(
            "`!rapm <player name>`\n"
            "Sends the Current Impact Metrics table, plus 4-Year Factor RAPMs and "
            "Peak/Career Metrics if populated (older/retired players may lack current-season data)."
        ),
        inline=False,
    )
    embed.add_field(
        name="Dashboard Stats",
        value=(
            "`!rapm <player name> <advanced|per game|per 75|per 100> <year>`\n"
            "Sends that year's stat-tile dashboard in the given view. "
            "If you leave out the stat-type, the bot will ask which one you want."
        ),
        inline=False,
    )
    embed.add_field(
        name="Examples",
        value=(
            "`!rapm lebron james`\n"
            "`!rapm shaquille o'neal`\n"
            "`!rapm lebron james advanced 2024`\n"
            "`!rapm lebron james 2024` → bot asks which stat view"
        ),
        inline=False,
    )
    embed.set_footer(text="Data from nbarapm.com")
    await ctx.send(embed=embed)


@bot.event
async def on_ready():
    log.info(f"RAPM Bot ready as {bot.user}")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
