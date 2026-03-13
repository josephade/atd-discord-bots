"""
NBA data layer — wraps nba_api calls and returns plain dicts for the embed builders.
All functions are synchronous (run via asyncio.to_thread in the cogs).

Return convention: every public function returns (data_dict | None, error_str | None).
  - On success:  (dict, None)
  - On failure:  (None, "Human-readable error message")
"""

import logging
import time
from difflib import get_close_matches

from nba_api.stats.static import players, teams
from nba_api.stats.endpoints import (
    leaguedashteamstats,
    leaguestandingsv3,
    playercareerstats,
    playerdashboardbygeneralsplits,
    playergamelog,
    playervsplayer,
    teamplayeronoffsummary,
)

log = logging.getLogger("nba-bot")

_DELAY   = 0.3
_TIMEOUT = 60

# Required by stats.nba.com — without these headers requests get blocked/timed out
# Note: do NOT include "Host" — requests sets it automatically; manual Host causes resets
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
    "Origin": "https://www.nba.com",
    "Referer": "https://www.nba.com/",
    "Connection": "keep-alive",
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _find_player(name: str) -> dict | None:
    all_players = players.get_players()
    name_lower = name.lower()
    for p in all_players:
        if p["full_name"].lower() == name_lower:
            return p
    names = [p["full_name"].lower() for p in all_players]
    matches = get_close_matches(name_lower, names, n=1, cutoff=0.6)
    if matches:
        for p in all_players:
            if p["full_name"].lower() == matches[0]:
                return p
    return None


def _find_team(name: str) -> dict | None:
    all_teams = teams.get_teams()
    name_lower = name.lower()
    for t in all_teams:
        if (
            t["full_name"].lower() == name_lower
            or t["abbreviation"].lower() == name_lower
            or t["nickname"].lower() == name_lower
            or t["city"].lower() == name_lower
        ):
            return t
    full_names = [t["full_name"].lower() for t in all_teams]
    matches = get_close_matches(name_lower, full_names, n=1, cutoff=0.5)
    if matches:
        for t in all_teams:
            if t["full_name"].lower() == matches[0]:
                return t
    return None


def _round(val, n=1) -> str:
    try:
        return str(round(float(val), n))
    except (TypeError, ValueError):
        return "N/A"


def _pct(val) -> str:
    """Decimal fraction (0.456) → percentage string ('45.6')."""
    try:
        return str(round(float(val) * 100, 1))
    except (TypeError, ValueError):
        return "N/A"


# ── public functions ───────────────────────────────────────────────────────────

def get_player_stats(player_name: str, season: str) -> tuple[dict | None, str | None]:
    """Full per-game stat line for a player in a given season."""
    player = _find_player(player_name)
    if not player:
        return None, f"❌ Player **{player_name}** not found. Check spelling."

    log.info(f"[stats] {player['full_name']} (id={player['id']}) season={season}")
    time.sleep(_DELAY)

    # ── career per-game stats (fast, player-centric) ──
    try:
        ep = playercareerstats.PlayerCareerStats(
            player_id=player["id"],
            per_mode36="PerGame",
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        df = ep.season_totals_regular_season.get_data_frame()
        row_df = df[df["SEASON_ID"] == season]
        if row_df.empty:
            available = sorted(df["SEASON_ID"].tolist())
            hint = f" Available seasons: {', '.join(available[-5:])}" if available else ""
            return None, f"❌ No stats found for **{player['full_name']}** in **{season}**.{hint}"
        r = row_df.iloc[0]
    except Exception as e:
        log.error(f"[stats] career fetch error: {e}")
        return None, f"❌ NBA Stats API error fetching stats for **{player['full_name']}**. Try again."

    # ── USG% + PLUS_MINUS via player dashboard ──
    time.sleep(_DELAY)
    usg = plus_minus = "N/A"
    try:
        dash = playerdashboardbygeneralsplits.PlayerDashboardByGeneralSplits(
            player_id=player["id"],
            season=season,
            per_mode_detailed="PerGame",
            measure_type_detailed="Advanced",
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        dash_df = dash.overall_player_dashboard.get_data_frame()
        if not dash_df.empty:
            d = dash_df.iloc[0]
            if "USG_PCT" in d.index:
                usg = _pct(d["USG_PCT"])
            if "PLUS_MINUS" in d.index:
                plus_minus = _round(d["PLUS_MINUS"])
    except Exception as e:
        log.warning(f"[stats] advanced dash failed: {e}")

    try:
        pts = float(r["PTS"]); fga = float(r["FGA"]); fta = float(r["FTA"])
        ts = str(round(pts / (2 * (fga + 0.44 * fta)) * 100, 1)) if (fga + fta) > 0 else "N/A"
    except Exception:
        ts = "N/A"

    log.info(f"[stats] OK: {player['full_name']} {season} — {r['PTS']}pts")
    return {
        "name":       player["full_name"],
        "season":     season,
        "team":       str(r.get("TEAM_ABBREVIATION", "N/A")),
        "position":   "N/A",
        "age":        _round(r.get("PLAYER_AGE"), 0),
        "pts":        _round(r.get("PTS")),
        "reb":        _round(r.get("REB")),
        "ast":        _round(r.get("AST")),
        "stl":        _round(r.get("STL")),
        "blk":        _round(r.get("BLK")),
        "tov":        _round(r.get("TOV")),
        "fg_pct":     _pct(r["FG_PCT"]) if r.get("FG_PCT") is not None else "N/A",
        "fg3_pct":    _pct(r["FG3_PCT"]) if r.get("FG3_PCT") is not None else "N/A",
        "ft_pct":     _pct(r["FT_PCT"]) if r.get("FT_PCT") is not None else "N/A",
        "ts_pct":     ts,
        "usg_pct":    usg,
        "min":        _round(r.get("MIN")),
        "gp":         str(int(r["GP"])) if "GP" in r.index else "N/A",
        "plus_minus": plus_minus,
    }, None


def get_on_off(player_name: str, season: str) -> tuple[dict | None, str | None]:
    """On/Off net rating splits for a player."""
    player = _find_player(player_name)
    if not player:
        return None, f"❌ Player **{player_name}** not found. Check spelling."

    log.info(f"[onoff] {player['full_name']} season={season}")

    # Get team_id from career stats
    time.sleep(_DELAY)
    try:
        ep = playercareerstats.PlayerCareerStats(
            player_id=player["id"],
            per_mode36="PerGame",
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        df = ep.season_totals_regular_season.get_data_frame()
        row_df = df[df["SEASON_ID"] == season]
        if row_df.empty:
            available = sorted(df["SEASON_ID"].tolist())
            hint = f" Available seasons: {', '.join(available[-5:])}" if available else ""
            return None, f"❌ No data for **{player['full_name']}** in **{season}**.{hint}"
        team_id   = int(row_df.iloc[0]["TEAM_ID"])
        team_abbr = str(row_df.iloc[0]["TEAM_ABBREVIATION"])
    except Exception as e:
        log.error(f"[onoff] career fetch error: {e}")
        return None, f"❌ NBA Stats API error for **{player['full_name']}**. Try again."

    log.info(f"[onoff] team_id={team_id} ({team_abbr})")
    time.sleep(_DELAY)
    try:
        onoff_ep = teamplayeronoffsummary.TeamPlayerOnOffSummary(
            team_id=team_id,
            season=season,
            per_mode_detailed="PerGame",
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        on_df  = onoff_ep.players_on_court_team_player_on_off_summary.get_data_frame()
        off_df = onoff_ep.players_off_court_team_player_on_off_summary.get_data_frame()
    except Exception as e:
        log.error(f"[onoff] onoff fetch error: {e}")
        return None, f"❌ NBA Stats API error fetching on/off data. On/off data is available from **1996-97** onwards. Try again."

    on_row  = on_df[on_df["VS_PLAYER_ID"] == player["id"]]
    off_row = off_df[off_df["VS_PLAYER_ID"] == player["id"]]

    if on_row.empty or off_row.empty:
        log.warning(f"[onoff] Player not in on/off rows for {team_abbr}. IDs: {list(on_df['VS_PLAYER_ID'].unique())[:5]}")
        return None, f"❌ No on/off data for **{player['full_name']}** in **{season}**. They may not have enough minutes to qualify."

    on  = on_row.iloc[0]
    off = off_row.iloc[0]

    return {
        "season":      season,
        "team":        team_abbr,
        "on_net":      _round(on.get("NET_RATING")),
        "on_off_rtg":  _round(on.get("OFF_RATING")),
        "on_def_rtg":  _round(on.get("DEF_RATING")),
        "on_min":      _round(on.get("MIN")),
        "off_net":     _round(off.get("NET_RATING")),
        "off_off_rtg": _round(off.get("OFF_RATING")),
        "off_def_rtg": _round(off.get("DEF_RATING")),
        "off_min":     _round(off.get("MIN")),
    }, None


def get_wowy(player1_name: str, player2_name: str, season: str) -> tuple[dict | None, str | None]:
    """With/Without You lineup splits between two players."""
    p1 = _find_player(player1_name)
    p2 = _find_player(player2_name)
    if not p1:
        return None, f"❌ Player **{player1_name}** not found. Check spelling."
    if not p2:
        return None, f"❌ Player **{player2_name}** not found. Check spelling."

    log.info(f"[wowy] {p1['full_name']} vs {p2['full_name']} season={season}")
    time.sleep(_DELAY)
    try:
        ep = playervsplayer.PlayerVsPlayer(
            player_id=p1["id"],
            vs_player_id=p2["id"],
            season=season,
            per_mode_detailed="PerGame",
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        df = ep.on_off_court.get_data_frame()
    except Exception as e:
        log.error(f"[wowy] fetch error: {e}")
        return None, f"❌ NBA Stats API error fetching WOWY data. WOWY data is available from **1996-97** onwards. Try again."

    log.info(f"[wowy] on_off_court rows={len(df)}, statuses={list(df['COURT_STATUS'].unique()) if not df.empty else []}")
    if df.empty:
        return None, f"❌ No WOWY data for **{p1['full_name']}** & **{p2['full_name']}** in **{season}**. They may not have shared the court that season, or data isn't available before **1996-97**."

    both_on = df[df["COURT_STATUS"] == "On"]
    p1_only = df[df["COURT_STATUS"] == "Off"]

    def _get(subset, col):
        if subset.empty:
            return "N/A"
        return _round(subset.iloc[0].get(col))

    return {
        "season":       season,
        "both_on_net":  _get(both_on, "PLUS_MINUS"),
        "both_on_off":  "N/A",
        "both_on_def":  "N/A",
        "both_on_min":  _get(both_on, "MIN"),
        "p1_on_net":    _get(p1_only, "PLUS_MINUS"),
        "p1_on_off":    "N/A",
        "p1_on_def":    "N/A",
        "p1_on_min":    _get(p1_only, "MIN"),
        "both_off_net": "N/A",
        "both_off_off": "N/A",
        "both_off_def": "N/A",
        "both_off_min": "N/A",
    }, None


def get_last_x_games(player_name: str, games: int, season: str) -> tuple[dict | None, str | None]:
    """Average stats over the last `games` regular-season games."""
    player = _find_player(player_name)
    if not player:
        return None, f"❌ Player **{player_name}** not found. Check spelling."

    log.info(f"[lastx] {player['full_name']} last {games} games season={season}")
    time.sleep(_DELAY)
    try:
        ep = playergamelog.PlayerGameLog(
            player_id=player["id"],
            season=season,
            season_type_all_star="Regular Season",
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        df = ep.player_game_log.get_data_frame()
    except Exception as e:
        log.error(f"[lastx] fetch error: {e}")
        return None, f"❌ NBA Stats API error fetching game log for **{player['full_name']}**. Try again."

    if df.empty:
        return None, f"❌ No game log for **{player['full_name']}** in **{season}**. Game logs are available from **1996-97** onwards."

    # PlayerGameLog returns most-recent first — take top N
    df = df.head(games)
    log.info(f"[lastx] Using {len(df)} games")

    def avg(col):
        try:
            return _round(df[col].mean())
        except Exception:
            return "N/A"

    def pct_avg(col):
        try:
            return _round(df[col].mean() * 100)
        except Exception:
            return "N/A"

    try:
        ts_vals = []
        for _, row in df.iterrows():
            pts = float(row["PTS"]); fga = float(row["FGA"]); fta = float(row["FTA"])
            if fga + fta > 0:
                ts_vals.append(pts / (2 * (fga + 0.44 * fta)) * 100)
        ts = _round(sum(ts_vals) / len(ts_vals)) if ts_vals else "N/A"
    except Exception:
        ts = "N/A"

    # MATCHUP format is "TEAM vs. OPP" or "TEAM @ OPP" — first token is player's team
    team = str(df.iloc[0]["MATCHUP"]).split()[0] if "MATCHUP" in df.columns else "N/A"

    game_log = []
    for _, row in df.iterrows():
        try:
            game_log.append({
                "date":    str(row.get("GAME_DATE", ""))[:10],
                "matchup": str(row.get("MATCHUP", "")),
                "pts":     int(row["PTS"]),
                "reb":     int(row["REB"]),
                "ast":     int(row["AST"]),
            })
        except Exception:
            pass

    return {
        "season":     season,
        "team":       team,
        "pts":        avg("PTS"),
        "reb":        avg("REB"),
        "ast":        avg("AST"),
        "stl":        avg("STL"),
        "blk":        avg("BLK"),
        "tov":        avg("TOV"),
        "fg_pct":     pct_avg("FG_PCT"),
        "fg3_pct":    pct_avg("FG3_PCT"),
        "ft_pct":     pct_avg("FT_PCT"),
        "ts_pct":     ts,
        "plus_minus": avg("PLUS_MINUS"),
        "min":        avg("MIN"),
        "game_log":   game_log,
    }, None


def get_team_stats(team_name: str, season: str) -> tuple[dict | None, str | None]:
    """Team advanced + basic stats for a season."""
    team = _find_team(team_name)
    if not team:
        return None, f"❌ Team **{team_name}** not found. Try a full name, nickname, or abbreviation (e.g. Lakers, GSW, Golden State Warriors)."

    log.info(f"[team] {team['full_name']} (id={team['id']}) season={season}")
    time.sleep(_DELAY)
    try:
        adv_ep = leaguedashteamstats.LeagueDashTeamStats(
            season=season,
            measure_type_detailed_defense="Advanced",
            per_mode_detailed="PerGame",
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        adv_df = adv_ep.league_dash_team_stats.get_data_frame()

        time.sleep(_DELAY)
        base_ep = leaguedashteamstats.LeagueDashTeamStats(
            season=season,
            measure_type_detailed_defense="Base",
            per_mode_detailed="PerGame",
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        base_df = base_ep.league_dash_team_stats.get_data_frame()
    except Exception as e:
        log.error(f"[team] league dash fetch error: {e}")
        return None, f"❌ NBA Stats API error fetching team data for **{season}**. Team stats are available from **1996-97** onwards. Try again."

    a_rows = adv_df[adv_df["TEAM_ID"] == team["id"]]
    b_rows = base_df[base_df["TEAM_ID"] == team["id"]]
    log.info(f"[team] adv rows={len(adv_df)}, team match={len(a_rows)}")

    if a_rows.empty or b_rows.empty:
        return None, f"❌ No data for **{team['full_name']}** in **{season}**. The team may not have existed that season."

    a = a_rows.iloc[0]
    b = b_rows.iloc[0]

    # Standings
    time.sleep(_DELAY)
    wins = losses = conf = conf_rank = "N/A"
    try:
        st_ep = leaguestandingsv3.LeagueStandingsV3(
            season=season,
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        st_df = st_ep.standings.get_data_frame()
        st_row = st_df[st_df["TeamID"] == team["id"]]
        if not st_row.empty:
            s = st_row.iloc[0]
            wins      = int(s["WINS"])
            losses    = int(s["LOSSES"])
            conf      = str(s["Conference"])
            conf_rank = int(s["PlayoffRank"])
    except Exception as e:
        log.warning(f"[team] standings fetch error: {e}")

    return {
        "name":      team["full_name"],
        "season":    season,
        "wins":      wins,
        "losses":    losses,
        "conf":      conf,
        "conf_rank": conf_rank,
        "off_rtg":   _round(a.get("OFF_RATING")),
        "def_rtg":   _round(a.get("DEF_RATING")),
        "net_rtg":   _round(a.get("NET_RATING")),
        "pace":      _round(a.get("PACE")),
        "efg_pct":   _pct(a["EFG_PCT"]) if a.get("EFG_PCT") is not None else "N/A",
        "ts_pct":    _pct(a["TS_PCT"]) if a.get("TS_PCT") is not None else "N/A",
        "tov_pct":   _pct(a["TM_TOV_PCT"]) if a.get("TM_TOV_PCT") is not None else "N/A",
        "oreb_pct":  _pct(a["OREB_PCT"]) if a.get("OREB_PCT") is not None else "N/A",
        "ft_rate":   _round(a.get("FTA_RATE")),
        "pts":       _round(b.get("PTS")),
        "opp_pts":   _round(b.get("OPP_PTS")),
        "fg3a":      _round(b.get("FG3A")),
    }, None
