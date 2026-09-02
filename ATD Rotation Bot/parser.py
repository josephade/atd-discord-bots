from known_nicknames import resolve_known_nickname

POSITIONS = ["PG", "SG", "SF", "PF", "C"]


def _normalize(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _lookup_declared_nickname(token: str, nicknames: dict[str, str]):
    """Case/punctuation-insensitive lookup into a team's own `Nick=Full Name`
    legend, so `DrJ`, `drj`, and `DRJ` all match the same declared entry."""
    norm = _normalize(token)
    for nick, player in nicknames.items():
        if _normalize(nick) == norm:
            return player
    return None


def _resolve_roster_name(name: str, roster: list[str]):
    """Match `name` against a full roster name. Returns (resolved_name, error).
    Tries an exact case-insensitive match first, then falls back to a
    unique substring / whole-word match (e.g. "Kemp" -> "Shawn Kemp")."""
    name_lower = name.lower().strip()

    for player in roster:
        if player.lower() == name_lower:
            return player, None

    candidates = []
    for player in roster:
        player_lower = player.lower()
        if name_lower in player_lower or any(w == name_lower for w in player_lower.split()):
            candidates.append(player)

    if len(candidates) == 1:
        return candidates[0], None
    if len(candidates) > 1:
        return None, f"**{name}** is ambiguous — could be {', '.join(candidates)}. Use a fuller name."
    return None, None


def parse_rotation_body(lines: list[str], roster: list[str]):
    """Parse the lines following `!setrotation <emoji>`.

    Returns (nicknames, segments, error). On success, error is None:
      nicknames: {nickname_as_typed: resolved_full_roster_name}
      segments:  [{"minutes": int, "positions": {"PG": {"nickname": str, "player": str}, ...}}, ...]
    On failure, nicknames/segments are None and error is a user-facing string.
    """
    nicknames: dict[str, str] = {}
    segments = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line == "---":
            continue

        if "=" in line:
            nick, _, full_name = line.partition("=")
            nick = nick.strip()
            full_name = full_name.strip()
            if not nick or not full_name:
                return None, None, f"Couldn't parse nickname line: `{line}`. Expected `Nick=Full Name`."
            resolved, err = _resolve_roster_name(full_name, roster)
            if err:
                return None, None, err
            if not resolved:
                return None, None, (
                    f"**{full_name}** (from `{line}`) isn't on this team's roster. "
                    f"Roster: {', '.join(roster)}"
                )
            nicknames[nick] = resolved
            continue

        tokens = line.split()
        if not tokens or not tokens[0].isdigit():
            return None, None, (
                f"Couldn't understand line: `{line}`. Expected either `Nick=Full Name` "
                f"or `<minutes> PG SG SF PF C` (5 players)."
            )
        if len(tokens) != 6:
            return None, None, (
                f"Line `{line}` needs exactly 5 players after the minutes "
                f"(PG SG SF PF C) — found {len(tokens) - 1}."
            )

        minutes = int(tokens[0])
        positions = {}
        for pos, token in zip(POSITIONS, tokens[1:]):
            player = _lookup_declared_nickname(token, nicknames)

            if not player:
                player, err = _resolve_roster_name(token, roster)
                if err:
                    return None, None, err

            if not player:
                known_full_name = resolve_known_nickname(token)
                if known_full_name:
                    player, _err = _resolve_roster_name(known_full_name, roster)

            if not player:
                return None, None, (
                    f"Unrecognized player `{token}` in line `{line}`. "
                    f"Define a nickname for them first (e.g. `{token}=Full Name`) "
                    f"or check the spelling against the roster."
                )
            positions[pos] = {"nickname": token, "player": player}

        segments.append({"minutes": minutes, "positions": positions})

    if not segments:
        return None, None, (
            "No rotation segments found. Add at least one line like "
            "`10 Billups Luka DrJ Kemp Horford` (minutes then PG SG SF PF C)."
        )

    return nicknames, segments, None
