"""Scrapes surface-level player bios and career averages from Basketball-Reference."""

import re
import time
import asyncio
import difflib

import aiohttp
from bs4 import BeautifulSoup

BASE_URL = "https://www.basketball-reference.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

# Basketball-Reference asks for a 3s crawl-delay between requests (robots.txt).
CRAWL_DELAY = 3.5
_last_request = 0.0
_throttle_lock = asyncio.Lock()

CACHE_TTL = 6 * 60 * 60  # 6 hours
MAX_CANDIDATES = 5

_search_cache: dict[str, tuple[float, list[dict]]] = {}
_player_cache: dict[str, tuple[float, dict]] = {}
_season_cache: dict[tuple[str, str, str], tuple[float, dict]] = {}
_team_season_cache: dict[tuple[str, str], tuple[float, dict]] = {}

# output key -> (bref data-stat name, value mode)
#   "raw"     - use the cell text as-is
#   "frac"    - cell text is a fraction like ".669", multiply by 100 for a %
#   "num_pct" - cell text is already a percentage number like "32.6", just append %
BASIC_SEASON_FIELDS = {
    "season": ("year_id", "raw"),
    "team": ("team_name_abbr", "raw"),
    "games": ("games", "raw"),
    "mp": ("mp_per_g", "raw"),
    "ppg": ("pts_per_g", "raw"),
    "rpg": ("trb_per_g", "raw"),
    "apg": ("ast_per_g", "raw"),
    "topg": ("tov_per_g", "raw"),
    "orpg": ("orb_per_g", "raw"),
    "spg": ("stl_per_g", "raw"),
    "bpg": ("blk_per_g", "raw"),
    "fg_pct": ("fg_pct", "frac"),
    "fgm": ("fg_per_g", "raw"),
    "fga": ("fga_per_g", "raw"),
    "three_pct": ("fg3_pct", "frac"),
    "three_m": ("fg3_per_g", "raw"),
    "three_a": ("fg3a_per_g", "raw"),
    "ft_pct": ("ft_pct", "frac"),
    "ftm": ("ft_per_g", "raw"),
    "fta": ("fta_per_g", "raw"),
}

ADVANCED_SEASON_FIELDS = {
    "per": ("per", "raw"),
    "ts_pct": ("ts_pct", "frac"),
    "usg_pct": ("usg_pct", "num_pct"),
    "ws": ("ws", "raw"),
    "bpm": ("bpm", "raw"),
    "vorp": ("vorp", "raw"),
    "awards": ("awards", "raw"),
}


class BrefUnavailable(Exception):
    """Basketball-Reference didn't respond (timeout, connection error, non-200 status)."""


class BrefStructureChanged(Exception):
    """A page loaded fine, but an expected element wasn't where the scraper expects it —
    Basketball-Reference likely changed their HTML and the scraper needs updating."""

TEAM_NAMES = {
    "ATL": "Atlanta Hawks", "BOS": "Boston Celtics", "BRK": "Brooklyn Nets",
    "CHA": "Charlotte Bobcats", "CHO": "Charlotte Hornets", "CHI": "Chicago Bulls",
    "CLE": "Cleveland Cavaliers", "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets",
    "DET": "Detroit Pistons", "GSW": "Golden State Warriors", "HOU": "Houston Rockets",
    "IND": "Indiana Pacers", "LAC": "LA Clippers", "LAL": "Los Angeles Lakers",
    "MEM": "Memphis Grizzlies", "MIA": "Miami Heat", "MIL": "Milwaukee Bucks",
    "MIN": "Minnesota Timberwolves", "NOP": "New Orleans Pelicans",
    "NYK": "New York Knicks", "OKC": "Oklahoma City Thunder", "ORL": "Orlando Magic",
    "PHI": "Philadelphia 76ers", "PHO": "Phoenix Suns", "POR": "Portland Trail Blazers",
    "SAC": "Sacramento Kings", "SAS": "San Antonio Spurs", "TOR": "Toronto Raptors",
    "UTA": "Utah Jazz", "WAS": "Washington Wizards",
    "SEA": "Seattle SuperSonics", "VAN": "Vancouver Grizzlies",
    "NOH": "New Orleans Hornets", "NOK": "New Orleans/Oklahoma City Hornets",
    "NJN": "New Jersey Nets", "WSB": "Washington Bullets", "KCK": "Kansas City Kings",
    "SDC": "San Diego Clippers", "BUF": "Buffalo Braves", "STL": "St. Louis Hawks",
}

# Common team names/nicknames -> a current abbreviation for that franchise. Doesn't need to be
# the franchise's "canonical" bref abbreviation - _fetch_franchise_page follows bref's own
# redirect to whichever page actually holds the season-by-season history table.
TEAM_ALIASES = {
    "atlanta": "ATL", "hawks": "ATL", "atlanta hawks": "ATL", "atl": "ATL",
    "boston": "BOS", "celtics": "BOS", "boston celtics": "BOS", "bos": "BOS",
    "brooklyn": "BRK", "nets": "BRK", "brooklyn nets": "BRK", "brk": "BRK", "bkn": "BRK",
    "charlotte": "CHO", "hornets": "CHO", "charlotte hornets": "CHO", "bobcats": "CHO",
    "cho": "CHO", "cha": "CHO",
    "chicago": "CHI", "bulls": "CHI", "chicago bulls": "CHI", "chi": "CHI",
    "cleveland": "CLE", "cavaliers": "CLE", "cavs": "CLE", "cleveland cavaliers": "CLE", "cle": "CLE",
    "dallas": "DAL", "mavericks": "DAL", "mavs": "DAL", "dallas mavericks": "DAL", "dal": "DAL",
    "denver": "DEN", "nuggets": "DEN", "denver nuggets": "DEN", "den": "DEN",
    "detroit": "DET", "pistons": "DET", "detroit pistons": "DET", "det": "DET",
    "golden state": "GSW", "warriors": "GSW", "golden state warriors": "GSW", "gsw": "GSW", "dubs": "GSW",
    "houston": "HOU", "rockets": "HOU", "houston rockets": "HOU", "hou": "HOU",
    "indiana": "IND", "pacers": "IND", "indiana pacers": "IND", "ind": "IND",
    "clippers": "LAC", "la clippers": "LAC", "los angeles clippers": "LAC", "lac": "LAC",
    "lakers": "LAL", "la lakers": "LAL", "los angeles lakers": "LAL", "lal": "LAL",
    "memphis": "MEM", "grizzlies": "MEM", "grizz": "MEM", "memphis grizzlies": "MEM", "mem": "MEM",
    "miami": "MIA", "heat": "MIA", "miami heat": "MIA", "mia": "MIA",
    "milwaukee": "MIL", "bucks": "MIL", "milwaukee bucks": "MIL", "mil": "MIL",
    "minnesota": "MIN", "timberwolves": "MIN", "wolves": "MIN", "twolves": "MIN", "t-wolves": "MIN",
    "minnesota timberwolves": "MIN", "min": "MIN",
    "new orleans": "NOP", "pelicans": "NOP", "pels": "NOP", "new orleans pelicans": "NOP", "nop": "NOP",
    "new york": "NYK", "knicks": "NYK", "new york knicks": "NYK", "nyk": "NYK",
    "oklahoma city": "OKC", "thunder": "OKC", "okc thunder": "OKC", "oklahoma city thunder": "OKC", "okc": "OKC",
    "orlando": "ORL", "magic": "ORL", "orlando magic": "ORL", "orl": "ORL",
    "philadelphia": "PHI", "76ers": "PHI", "sixers": "PHI", "philadelphia 76ers": "PHI", "phi": "PHI",
    "phoenix": "PHO", "suns": "PHO", "phoenix suns": "PHO", "pho": "PHO", "phx": "PHO",
    "portland": "POR", "trail blazers": "POR", "blazers": "POR", "portland trail blazers": "POR", "por": "POR",
    "sacramento": "SAC", "kings": "SAC", "sacramento kings": "SAC", "sac": "SAC",
    "san antonio": "SAS", "spurs": "SAS", "san antonio spurs": "SAS", "sas": "SAS",
    "toronto": "TOR", "raptors": "TOR", "toronto raptors": "TOR", "tor": "TOR",
    "utah": "UTA", "jazz": "UTA", "utah jazz": "UTA", "uta": "UTA",
    "washington": "WAS", "wizards": "WAS", "wiz": "WAS", "washington wizards": "WAS", "was": "WAS",
}

# output key -> (bref data-stat name on the team-misc / league advanced-team tables, value mode)
TEAM_MISC_FIELDS = {
    "wins": ("wins", "raw"),
    "losses": ("losses", "raw"),
    "off_rtg": ("off_rtg", "raw"),
    "def_rtg": ("def_rtg", "raw"),
    "net_rtg": ("net_rtg", "raw"),
    "pace": ("pace", "raw"),
    "ftr": ("fta_per_fga_pct", "raw"),
    "three_par": ("fg3a_per_fga_pct", "raw"),
    "efg_pct": ("efg_pct", "frac"),
    "tov_pct": ("tov_pct", "num_pct"),
    "orb_pct": ("orb_pct", "num_pct"),
    "ft_fga": ("ft_rate", "raw"),
    "opp_efg_pct": ("opp_efg_pct", "frac"),
    "opp_tov_pct": ("opp_tov_pct", "num_pct"),
    "drb_pct": ("drb_pct", "num_pct"),
    "opp_ft_fga": ("opp_ft_rate", "raw"),
}

# out_key -> True if a higher raw value ranks better (rank #1), mirroring bref's own default
# sort direction for each column (e.g. lower DRtg/TOV%/opponent shooting is "better").
TEAM_RANK_DIRECTIONS = {
    "wins": True,
    "off_rtg": True,
    "def_rtg": False,
    "net_rtg": True,
    "pace": True,
    "ftr": True,
    "three_par": True,
    "efg_pct": True,
    "tov_pct": False,
    "orb_pct": True,
    "ft_fga": True,
    "opp_efg_pct": False,
    "opp_tov_pct": True,
    "drb_pct": True,
    "opp_ft_fga": False,
}

_TEAM_REDIRECT_RE = re.compile(r'URL=(/teams/[A-Z]+/)"')

TEAM_MATCH_CUTOFF = 0.75


def match_teams(query: str, limit: int = 3) -> list[str]:
    """Returns up to `limit` team abbreviations whose alias spelling is close to query,
    best match first. Empty list if nothing is close enough to be worth suggesting."""
    normalized = query.strip().lower()
    if not normalized:
        return []

    if normalized in TEAM_ALIASES:
        return [TEAM_ALIASES[normalized]]

    close_aliases = difflib.get_close_matches(normalized, TEAM_ALIASES.keys(), n=10, cutoff=TEAM_MATCH_CUTOFF)
    abbrs = []
    for alias in close_aliases:
        abbr = TEAM_ALIASES[alias]
        if abbr not in abbrs:
            abbrs.append(abbr)
        if len(abbrs) >= limit:
            break
    return abbrs


def _frac_to_pct(raw: str) -> str:
    """A fraction like '.669' -> '66.9%'."""
    if not raw or raw == "-":
        return "-"
    try:
        return f"{float(raw) * 100:.1f}%"
    except ValueError:
        return raw


def _append_pct(raw: str) -> str:
    """A number already in percentage form like '32.6' -> '32.6%'."""
    return f"{raw}%" if raw and raw != "-" else "-"


async def _throttled_get(session: aiohttp.ClientSession, url: str, **kwargs) -> str:
    """Fetch a URL, respecting the crawl-delay. Raises BrefUnavailable on any network failure."""
    text, _ = await _throttled_get_with_url(session, url, **kwargs)
    return text


async def _throttled_get_with_url(session: aiohttp.ClientSession, url: str, **kwargs) -> tuple[str, str]:
    """Like _throttled_get, but also returns the final URL (post-redirects) as a string."""
    global _last_request
    async with _throttle_lock:
        wait = CRAWL_DELAY - (time.monotonic() - _last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10), **kwargs) as resp:
                _last_request = time.monotonic()
                if resp.status != 200:
                    raise BrefUnavailable(f"HTTP {resp.status} from {url}")
                return await resp.text(), str(resp.url)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            _last_request = time.monotonic()
            raise BrefUnavailable(str(exc)) from exc


async def search_players(query: str) -> list[dict]:
    """Returns up to MAX_CANDIDATES matches as [{'name': ..., 'url': ...}, ...] in
    Basketball-Reference's own ranking order. Empty list means no matches at all."""
    cache_key = query.strip().lower()
    cached = _search_cache.get(cache_key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]

    async with aiohttp.ClientSession() as session:
        html, final_url = await _throttled_get_with_url(
            session, f"{BASE_URL}/search/search.fcgi", params={"search": query}
        )

    # A very unambiguous query (e.g. "Lebron") makes Basketball-Reference redirect straight to
    # the player's page instead of showing a search-results list.
    final_path = final_url[len(BASE_URL):] if final_url.startswith(BASE_URL) else final_url
    if re.match(r"^/players/[a-z]/[a-z0-9]+\.html$", final_path):
        soup = BeautifulSoup(html, "html.parser")
        meta = soup.find("div", id="meta")
        name_el = meta.find("h1") if meta else None
        if not name_el:
            raise BrefStructureChanged("redirected straight to a player page but #meta/h1 not found")
        candidates = [{"name": name_el.get_text(strip=True), "url": final_path}]
        _search_cache[cache_key] = (time.time(), candidates)
        return candidates

    soup = BeautifulSoup(html, "html.parser")

    if not soup.find("div", id="content"):
        raise BrefStructureChanged("search results page shell (#content) not found")

    if re.search(r"Found\s*<strong>\s*0\s*hits", html):
        _search_cache[cache_key] = (time.time(), [])
        return []  # genuinely no matches for this query

    players_section = soup.find("div", id="players")
    if not players_section:
        # bref omits the whole #players div (rather than rendering it empty) when a query
        # matches only non-player categories, e.g. a pure team name like "Warriors".
        _search_cache[cache_key] = (time.time(), [])
        return []

    items = players_section.find_all("div", class_="search-item")
    if not items:
        _search_cache[cache_key] = (time.time(), [])
        return []  # players section rendered but empty -> no matches in this category

    candidates = []
    for item in items[:MAX_CANDIDATES]:
        name_el = item.find("div", class_="search-item-name")
        url_div = item.find("div", class_="search-item-url")
        link = name_el.find("a") if name_el else None
        if not link or not url_div:
            raise BrefStructureChanged("search-item name/url not found inside a search result")
        candidates.append({"name": link.get_text(strip=True), "url": url_div.get_text(strip=True)})

    _search_cache[cache_key] = (time.time(), candidates)
    return candidates


def _parse_career_row(soup: BeautifulSoup) -> dict | None:
    table = soup.find("table", id="per_game_stats")
    if not table:
        return None

    row = table.find("tr", id=lambda v: v and v.endswith("_stats.Yrs"))
    if not row:
        return None

    def stat(name: str) -> str:
        cell = row.find(attrs={"data-stat": name})
        return cell.get_text(strip=True) if cell else "-"

    years_cell = row.find(attrs={"data-stat": "year_id"})
    years_text = years_cell.get_text(strip=True) if years_cell else ""
    years_match = re.search(r"(\d+)\s*Yrs?", years_text)

    def pct(name: str) -> str:
        return _frac_to_pct(stat(name))

    return {
        "seasons": years_match.group(1) if years_match else "-",
        "games": stat("games"),
        "ppg": stat("pts_per_g"),
        "rpg": stat("trb_per_g"),
        "apg": stat("ast_per_g"),
        "spg": stat("stl_per_g"),
        "bpg": stat("blk_per_g"),
        "fg_pct": pct("fg_pct"),
        "three_pct": pct("fg3_pct"),
        "ft_pct": pct("ft_pct"),
    }


def _parse_stat_row(soup: BeautifulSoup, table_id: str, row_id: str, field_map: dict) -> dict | None:
    """Extracts one season row (keyed like 'per_game_stats.2016' or 'advanced_post.2016')
    into a dict of output_key -> formatted value, per field_map's (data-stat, is_pct) entries."""
    table = soup.find("table", id=table_id)
    if not table:
        return None

    row = table.find("tr", id=row_id)
    if not row:
        return None

    def stat(name: str) -> str:
        cell = row.find(attrs={"data-stat": name})
        return cell.get_text(strip=True) if cell else "-"

    def frac_pct(name: str) -> str:
        return _frac_to_pct(stat(name))

    def num_pct(name: str) -> str:
        return _append_pct(stat(name))

    modes = {"raw": stat, "frac": frac_pct, "num_pct": num_pct}
    return {out_key: modes[mode](data_stat) for out_key, (data_stat, mode) in field_map.items()}


def _parse_teams(soup: BeautifulSoup) -> list[str]:
    table = soup.find("table", id="per_game_stats")
    if not table:
        return []

    body = table.find("tbody")
    if not body:
        return []

    teams = []
    for cell in body.find_all(attrs={"data-stat": "team_name_abbr"}):
        link = cell.find("a")
        if not link:
            continue  # non-team rows (e.g. "Did not play - other pro league") have no team link
        abbr = link.get_text(strip=True)
        if abbr and abbr != "TOT" and abbr not in teams:
            teams.append(abbr)

    return [TEAM_NAMES.get(abbr, abbr) for abbr in teams]


def _parse_bio(soup: BeautifulSoup) -> dict:
    meta = soup.find("div", id="meta")
    bio = {"name": None, "image": None, "height": "-", "weight": "-", "position": "-"}
    if not meta:
        raise BrefStructureChanged("#meta block not found on player page")

    name_el = meta.find("h1")
    if name_el:
        bio["name"] = name_el.get_text(strip=True)

    img = meta.find("img")
    if img and img.get("src"):
        bio["image"] = img["src"]

    for p in meta.find_all("p"):
        strong = p.find("strong")
        if strong and "Position" in strong.get_text():
            text = p.get_text(" ", strip=True)
            text = text.split("▪")[0]
            bio["position"] = text.replace("Position:", "").strip()

        spans = p.find_all("span")
        if len(spans) >= 2 and re.match(r"^\d+-\d+$", spans[0].get_text(strip=True)):
            bio["height"] = spans[0].get_text(strip=True)
            bio["weight"] = spans[1].get_text(strip=True)

    return bio


def _parse_accolades(soup: BeautifulSoup, limit: int = 8) -> list[str]:
    bling = soup.find("ul", id="bling")
    if not bling:
        return []
    return [li.get_text(strip=True) for li in bling.find_all("li")][:limit]


async def get_player_by_url(player_url: str) -> dict:
    """Fetch full bio + career averages for a known Basketball-Reference player URL
    (a relative path like '/players/c/curryst01.html', as returned by search_players)."""
    cache_key = player_url
    cached = _player_cache.get(cache_key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]

    async with aiohttp.ClientSession() as session:
        page_html = await _throttled_get(session, f"{BASE_URL}{player_url}")

    soup = BeautifulSoup(page_html, "html.parser")

    bio = _parse_bio(soup)  # raises BrefStructureChanged if the bio block is missing/malformed
    if not bio["name"]:
        raise BrefStructureChanged("player name (h1) not found in #meta")

    result = {
        **bio,
        "url": f"{BASE_URL}{player_url}",
        "teams": _parse_teams(soup),
        "accolades": _parse_accolades(soup),
        "career": _parse_career_row(soup),
    }

    _player_cache[cache_key] = (time.time(), result)
    return result


async def get_player_season(player_url: str, end_year: str, season_type: str) -> dict:
    """Fetch one season's basic + advanced stats for a known player URL.

    end_year is Basketball-Reference's season-ending year (e.g. '2016' for the 2015-16 season).
    season_type is 'regular' or 'playoffs'. If the player didn't play that season/type, 'basic'
    and 'advanced' come back as None rather than raising."""
    cache_key = (player_url, end_year, season_type)
    cached = _season_cache.get(cache_key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]

    async with aiohttp.ClientSession() as session:
        page_html = await _throttled_get(session, f"{BASE_URL}{player_url}")

    soup = BeautifulSoup(page_html, "html.parser")

    bio = _parse_bio(soup)  # raises BrefStructureChanged if the bio block is missing/malformed
    if not bio["name"]:
        raise BrefStructureChanged("player name (h1) not found in #meta")

    basic_table = "per_game_stats_post" if season_type == "playoffs" else "per_game_stats"
    adv_table = "advanced_post" if season_type == "playoffs" else "advanced"

    result = {
        "name": bio["name"],
        "image": bio["image"],
        "url": f"{BASE_URL}{player_url}",
        "season_type": season_type,
        "basic": _parse_stat_row(soup, basic_table, f"{basic_table}.{end_year}", BASIC_SEASON_FIELDS),
        "advanced": _parse_stat_row(soup, adv_table, f"{adv_table}.{end_year}", ADVANCED_SEASON_FIELDS),
    }

    _season_cache[cache_key] = (time.time(), result)
    return result


async def _fetch_franchise_page(session: aiohttp.ClientSession, abbr: str) -> tuple[str, str]:
    """Fetch a franchise's season-by-season history page. Relocated/renamed franchises (e.g.
    Seattle -> OKC) live under one abbreviation with bref meta-refreshing the others into it;
    this follows that chain until it lands on the page that actually has the history table."""
    seen = set()
    current = abbr
    while current not in seen:
        seen.add(current)
        html = await _throttled_get(session, f"{BASE_URL}/teams/{current}/")
        match = _TEAM_REDIRECT_RE.search(html)
        if not match:
            return html, current
        redirect_match = re.match(r"^/teams/([A-Z]+)/$", match.group(1))
        if not redirect_match:
            raise BrefStructureChanged(f"unrecognized franchise redirect target {match.group(1)!r}")
        current = redirect_match.group(1)
    raise BrefStructureChanged(f"franchise redirect loop starting at {abbr}")


def _find_season_row(html: str, canonical_abbr: str, end_year: str) -> tuple[str, str, str] | None:
    """Finds the row for end_year in a franchise history page. Returns
    (season_abbr, display_name, season_label), or None if the franchise didn't exist that year."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id=canonical_abbr)
    if not table:
        raise BrefStructureChanged(f"franchise history table #{canonical_abbr} not found")

    body = table.find("tbody")
    if not body:
        raise BrefStructureChanged(f"franchise history table #{canonical_abbr} has no rows")

    for row in body.find_all("tr"):
        season_cell = row.find(attrs={"data-stat": "season"})
        link = season_cell.find("a") if season_cell else None
        if not link or not link.get("href"):
            continue
        href_match = re.match(r"^/teams/([A-Z]+)/(\d{4})\.html$", link["href"])
        if not href_match or href_match.group(2) != end_year:
            continue
        season_label = link.get_text(strip=True)
        name_cell = row.find(attrs={"data-stat": "team_name"})
        name = name_cell.get_text(strip=True).rstrip("*") if name_cell else season_label
        return href_match.group(1), name, season_label

    return None


def _parse_league_team_stats(soup: BeautifulSoup) -> dict[str, dict[str, str]]:
    """Returns team_abbr -> {out_key: raw_cell_text} for every team on a league season page."""
    table = soup.find("table", id="advanced-team")
    if not table:
        raise BrefStructureChanged("league advanced-team table not found")

    body = table.find("tbody")
    if not body:
        raise BrefStructureChanged("league advanced-team table has no rows")

    teams = {}
    for row in body.find_all("tr"):
        team_cell = row.find(attrs={"data-stat": "team"})
        link = team_cell.find("a") if team_cell else None
        if not link or not link.get("href"):
            continue
        abbr_match = re.match(r"^/teams/([A-Z]+)/", link["href"])
        if not abbr_match:
            continue

        raw = {}
        for out_key, (data_stat, _mode) in TEAM_MISC_FIELDS.items():
            cell = row.find(attrs={"data-stat": data_stat})
            raw[out_key] = cell.get_text(strip=True) if cell else "-"
        teams[abbr_match.group(1)] = raw

    if not teams:
        raise BrefStructureChanged("no team rows parsed from league advanced-team table")
    return teams


def _format_team_stats(raw: dict[str, str]) -> dict[str, str]:
    modes = {"raw": lambda v: v, "frac": _frac_to_pct, "num_pct": _append_pct}
    return {out_key: modes[TEAM_MISC_FIELDS[out_key][1]](raw.get(out_key, "-")) for out_key in TEAM_MISC_FIELDS}


def _compute_rank(teams_raw: dict[str, dict[str, str]], team_abbr: str, out_key: str) -> tuple[int, int] | None:
    """1-indexed (rank, total) for team_abbr on out_key, best-first per TEAM_RANK_DIRECTIONS."""
    parsed = []
    for abbr, raw in teams_raw.items():
        try:
            parsed.append((abbr, float(raw[out_key])))
        except (KeyError, ValueError):
            continue

    if not any(abbr == team_abbr for abbr, _ in parsed):
        return None

    parsed.sort(key=lambda pair: pair[1], reverse=TEAM_RANK_DIRECTIONS[out_key])
    for i, (abbr, _) in enumerate(parsed, start=1):
        if abbr == team_abbr:
            return i, len(parsed)
    return None  # pragma: no cover - unreachable, team_abbr was confirmed present above


async def get_team_season(team_abbr: str, end_year: str) -> dict:
    """Fetch one team's regular-season four-factors/ratings stats plus its league rank on each,
    for the season ending in end_year. team_abbr is any known abbreviation for the franchise
    (see TEAM_ALIASES) - not necessarily the one bref used that particular season.

    Returns {'name': None} if the franchise didn't exist / have no season for that year."""
    cache_key = (team_abbr, end_year)
    cached = _team_season_cache.get(cache_key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]

    async with aiohttp.ClientSession() as session:
        franchise_html, canonical_abbr = await _fetch_franchise_page(session, team_abbr)
        found = _find_season_row(franchise_html, canonical_abbr, end_year)
        if not found:
            result = {"name": None}
            _team_season_cache[cache_key] = (time.time(), result)
            return result

        season_abbr, display_name, season_label = found
        league_html = await _throttled_get(session, f"{BASE_URL}/leagues/NBA_{end_year}.html")

    teams_raw = _parse_league_team_stats(BeautifulSoup(league_html, "html.parser"))
    if season_abbr not in teams_raw:
        raise BrefStructureChanged(f"team {season_abbr} missing from {end_year} league advanced-team table")

    result = {
        "name": display_name,
        "abbr": season_abbr,
        "url": f"{BASE_URL}/teams/{season_abbr}/{end_year}.html",
        "season": season_label,
        "stats": _format_team_stats(teams_raw[season_abbr]),
        "ranks": {
            out_key: _compute_rank(teams_raw, season_abbr, out_key)
            for out_key in TEAM_RANK_DIRECTIONS
        },
        "total_teams": len(teams_raw),
    }

    _team_season_cache[cache_key] = (time.time(), result)
    return result
