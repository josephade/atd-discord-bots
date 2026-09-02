# positions.py
# Player -> NBA position eligibility, for the positional roster check
# (5 starters + 5 bench, PG/SG/SF/PF/C). Data copied from ATD Team Sheet
# Bot's player_positions.py — that's the shared source of truth other bots
# edit to correct/expand position eligibility; re-sync this file if that
# one changes. (ATD Draft Bot also carries an older copy of this same file;
# Team Sheet Bot's is the current one since it's what live rosters use.)

from player_positions import PLAYER_POSITIONS

_VALID = {'PG', 'SG', 'SF', 'PF', 'C'}


def get_positions(player: str) -> list[str]:
    """Return valid positions for a player (e.g. ['SF', 'PF']), or [] if unlisted."""
    for name, pos_str in PLAYER_POSITIONS.items():
        if name.lower() == player.lower():
            return [p.strip().upper() for p in pos_str.split('/') if p.strip().upper() in _VALID]
    return []
