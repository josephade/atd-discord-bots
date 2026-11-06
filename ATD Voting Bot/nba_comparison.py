"""Historical flavour for reveals, using a bundled source-backed record table."""
import json
from pathlib import Path

from reveal import record_text, ordinal

RECORDS = json.loads(Path(__file__).with_name('nba_records.json').read_text(encoding='utf-8'))


def historical_comparison(row, *, chosen=None, reaction=None):
    wins, losses = map(int, record_text(row).split('-'))
    exact = [r for r in RECORDS if (r['wins'], r['losses']) == (wins, losses)]
    candidates = exact or RECORDS
    preferred = {41: ('2008-09', 'Chicago Bulls'), 72: ('1995-96', 'Chicago Bulls'),
                 73: ('2015-16', 'Golden State Warriors'), 9: ('1972-73', 'Philadelphia 76ers')}
    chosen = chosen or min(candidates, key=lambda r: (
        abs(r['wins']-wins)+abs(r['losses']-losses),
        (r['season'], r['team']) != preferred.get(wins),
        r['team'] != row.get('team'), -int(r['season'][:4]), r['team']))
    source = chosen['source']
    if 'espn.com' in source:
        source = 'https://www.espn.com/nba/standings/_/season/'+source.rsplit('=', 1)[-1]
    team = f"[{chosen['season']} {chosen['team']}](<{source}>)"
    historical_record = f"{chosen['wins']}-{chosen['losses']}"
    is_exact = (chosen['wins'], chosen['losses']) == (wins, losses)
    if is_exact:
        if wins < 30 and reaction is None:
            return f"You're just as bad as the {team} ({historical_record}). Lock in, bro."
        lead = f'Same record as the {team} ({historical_record}). '
    else:
        label = 'Closest historical comparison available for this reveal' if reaction is not None else 'Closest historical comparison in the record library'
        lead = f'{label}: the {team} ({historical_record}). '
    if reaction is not None:
        return lead + reaction
    if wins >= 50:
        return lead+'Elite territory. Now back it up in the playoffs.'
    if wins == losses:
        return lead+"Perfectly average. Time to push past the middle of the pack."
    if wins > losses:
        return lead+"A winning record, but there's another level. Push for 50."
    return lead+"Below .500. Lock in, bro — you've got work to do."


def record_reaction(row, index=0):
    """Comment on the revealed result, not invented roster strengths or weaknesses."""
    wins, losses = map(int, record_text(row).split('-'))
    seed = ordinal(row['seed'])
    record = f'{wins}-{losses}'
    total = wins + losses
    percentage = f'{100*wins/total:.1f}%' if total else '0.0%'
    margin = abs(wins-losses)
    target = (total // 2 + total % 2) - wins
    if wins >= 50:
        lines = (
            f'{record} and the {seed} seed: a 50-win team. That is elite regular-season company.',
            f'The {seed} seed won {percentage} of its games. At {record}, this was a seriously strong regular season.',
            f'{wins} wins put the {seed} seed in the 50-win club. That is the standard this draft set for your team.',
            f'{margin} more wins than losses: the {seed} seed earned that {record} record.',
            f'At {record}, the {seed} seed cleared 50 wins by {wins-50}. An elite regular-season total.',
            f'The {seed} seed finishes with {wins} wins against {losses} losses. That is a contender-level record.',
            f'{percentage} of games won, {wins} wins overall, and the {seed} seed. Those are elite regular-season numbers.',
            f'The {seed} seed finishes {record}. Winning {wins} games puts this team in the 50-win club.',
        )
    elif wins > losses:
        lines = (
            f'{record} puts the {seed} seed {50-wins} wins short of 50. A winning season, with a clear next target.',
            f'The {seed} seed won {percentage} of its games: good enough for {record}, short of the 50-win club.',
            f'{margin} more wins than losses for the {seed} seed. That {record} record is solid, but it is not 50 wins yet.',
            f'{wins} wins and the {seed} seed. Turning {50-wins} of those losses into wins would have made this a 50-win season.',
            f'At {record}, the {seed} seed finishes above .500. The gap to 50 wins is {50-wins}.',
            f'The {seed} seed finishes at {record}. A winning record; {50-wins} more wins would reach the elite benchmark.',
            f'The {seed} seed lost {losses} games but won {wins}. That is {percentage} won, with room below the 50-win mark.',
            f'{record} is the final record for the {seed} seed. You cleared .500; the next milestone is {50-wins} wins away.',
        )
    elif wins == losses:
        lines = (
            f'{record} and the {seed} seed. Exactly .500: one more win instead of a loss would have made it a winning season.',
            f'The {seed} seed split all {total} games evenly. At {record}, you are the definition of average.',
            f'{wins} wins, {losses} losses, and the {seed} seed. Not one win above .500.',
            f'The {seed} seed finishes with a dead-even {record}. A winning season was one changed result away.',
            f'The {seed} seed won exactly half its games. That {record} record needs a push past .500.',
            f'At {record}, the {seed} seed has no winning margin at all: {wins} won and {losses} lost.',
            f'The {seed} seed lands at {record}. Half the schedule won, half lost; one extra win would break the tie.',
            f'{record} leaves the {seed} seed right on .500. The first target is simply winning more games than you lose.',
        )
    else:
        lines = (
            f'{record} and the {seed} seed: {losses} losses in {total} games. You needed {target} more wins just to reach .500. Lock in, bro.',
            f'The {seed} seed won only {percentage} of its games. That {record} record is {target} wins short of .500.',
            f'{margin} more losses than wins for the {seed} seed. At {record}, the gap to a .500 season is {target} wins.',
            f'The {seed} seed finishes with {wins} wins and {losses} losses. That is {percentage} won. Lock in, bro.',
            f'{record} leaves the {seed} seed below .500 by {target} wins. Reaching that mark would mean turning that many losses into wins.',
            f'The {seed} seed lost {losses} of {total} games. At {record}, there were {margin} more losses than wins.',
            f'Only {wins} wins for the {seed} seed. The {record} finish needed {target} losses turned into wins to reach .500.',
            f'The {seed} seed finishes {record}, winning {percentage} of games. Getting back to .500 means finding {target} more wins.',
        )
    return lines[index % len(lines)]


def comparisons_for_standings(rows):
    """Assign across the whole draft, so individual and bulk reveals agree.

    Scarce exact matches are allocated first. If they run out, choose an unused
    nearest record and explicitly label it as a comparison rather than a match.
    """
    used, selected = set(), {}
    def displayed(row):
        return tuple(map(int, record_text(row).split('-')))
    def exact_count(row):
        return sum((r['wins'], r['losses']) == displayed(row) for r in RECORDS)
    for row in sorted(rows, key=lambda r: (exact_count(r), r['seed'])):
        wins, losses = displayed(row)
        available = [r for r in RECORDS if (r['season'], r['team']) not in used]
        if not available:
            raise ValueError('Not enough historical team seasons for unique comparisons.')
        chosen = min(available, key=lambda r: (
            abs(r['wins']-wins)+abs(r['losses']-losses),
            r['team'] != row.get('team'), -int(r['season'][:4]), r['team']))
        used.add((chosen['season'], chosen['team']))
        selected[row['seed']] = chosen
    counts, result = {}, {}
    for row in sorted(rows, key=lambda r: r['seed']):
        wins, losses = displayed(row)
        band = 'elite' if wins >= 50 else 'average' if wins == losses else 'winning' if wins > losses else 'losing'
        index = counts.get(band, 0)
        counts[band] = index + 1
        reaction = record_reaction(row, index)
        result[row['seed']] = historical_comparison(row, chosen=selected[row['seed']], reaction=reaction)
    return result
