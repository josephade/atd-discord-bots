"""Read-only team vote summaries; delivery status never affects analysis."""
import json
import re
import statistics
from datetime import datetime
from pathlib import Path
from dataclasses import asdict
from decimal import Decimal, InvalidOperation

from votes import normalize, resolve_member, parse_rows

EMOJI_ALIASES = json.loads(Path(__file__).with_name('team_emojis.json').read_text(encoding='utf-8'))


ALIASES = dict(zip(
    'ATL BOS BKN CHA CHI CLE DAL DEN DET GSW HOU IND LAC LAL MEM MIA MIL MIN NOP NYK OKC ORL PHI PHX POR SAC SAS TOR UTA WAS'.lower().split(),
    ['Atlanta Hawks', 'Boston Celtics', 'Brooklyn Nets', 'Charlotte Hornets',
     'Chicago Bulls', 'Cleveland Cavaliers', 'Dallas Mavericks', 'Denver Nuggets',
     'Detroit Pistons', 'Golden State Warriors', 'Houston Rockets', 'Indiana Pacers',
     'Los Angeles Clippers', 'Los Angeles Lakers', 'Memphis Grizzlies', 'Miami Heat',
     'Milwaukee Bucks', 'Minnesota Timberwolves', 'New Orleans Pelicans', 'New York Knicks',
     'Oklahoma City Thunder', 'Orlando Magic', 'Philadelphia 76ers', 'Phoenix Suns',
     'Portland Trail Blazers', 'Sacramento Kings', 'San Antonio Spurs', 'Toronto Raptors',
     'Utah Jazz', 'Washington Wizards']))


def compact(text):
    return re.sub(r'[^a-z0-9]', '', normalize(text))


def match_team(query, teams):
    emoji = re.fullmatch(r'<a?:([^:>]+):\d+>', query.strip())
    query = emoji[1] if emoji else query.strip(': ')
    literal = [t for t in teams if compact(t) == compact(query)]
    if len(literal) == 1:
        return literal[0]
    aliases = {normalize(name): team for name, team in EMOJI_ALIASES.items()}
    mapped = ALIASES.get(normalize(query), aliases.get(normalize(query), query))
    # A known alias must retain its identity even when that team is absent.
    # Otherwise "Rockets" (Houston) can fall through to Denver Rockets.
    query = mapped
    q = compact(query)
    exact = [t for t in teams if compact(t) == q]
    matches = exact or [t for t in teams if q and q in compact(t)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ValueError('That emoji/name matches multiple teams: ' + ', '.join(matches) + '. Use the full team name.')
    raise ValueError('No team matches that emoji/name in this draft. Use a team name or emoji from this draft\'s ballot.')


def team_emoji_candidates(team, emojis):
    """Logo selection requires an exact identity, never a partial team match."""
    aliases = {normalize(name): value for name, value in EMOJI_ALIASES.items()}
    canonical_team = ALIASES.get(normalize(team), aliases.get(normalize(team), team))
    return [emoji for emoji in emojis if compact(
        ALIASES.get(normalize(emoji.name), aliases.get(normalize(emoji.name), emoji.name))) == compact(canonical_team)]


def timestamp(value):
    for fmt in ('%m/%d/%Y %H:%M:%S', '%d/%m/%Y %H:%M:%S',
                '%d.%m.%Y %H:%M:%S', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return datetime.min


def report_from_rows(rows, query, tier_count=10, members=(), aliases=None, name_column='Enter Discord Name'):
    """Only current response rows participate; never merge the delivery archive."""
    ballots = parse_rows(rows, name_column)
    calculated = calculated_rows(rows, name_column)
    records = []
    for ballot in reversed(ballots):
        payload = asdict(ballot)
        candidates = calculated.get(normalize(ballot.name), [])
        # Copied formula-table timestamps can predate edits to the live ballot.
        # A unique exact name is safe; duplicate names require a timestamp match.
        if len(candidates) > 1:
            candidates = [r for r in candidates if r[0] == ballot.timestamp]
        payload['calculated_values'] = candidates[0][1] if len(candidates) == 1 else {}
        records.append({'payload': json.dumps(payload)})
    return team_report(records, query, tier_count, members, aliases)


def calculated_rows(rows, name_column):
    """Read the second table separately, without creating ballots or receipts."""
    headers = []
    for index, row in enumerate(rows):
        times = [i for i, c in enumerate(row) if normalize(c) in {'timestamp', 'tidsmerke'}]
        names = [i for i, c in enumerate(row) if normalize(c) == normalize(name_column)]
        if len(times) == len(names) == 1:
            headers.append((index, times[0], names[0]))
    if len(headers) < 2:
        return {}
    start, time_col, name_col = headers[1]
    end = headers[2][0] if len(headers) > 2 else len(rows)
    columns = [(i, m.group(1).strip()) for i, h in enumerate(rows[start])
               if (m := re.search(r'\[([^\]]+)\]\s*$', h.strip()))]
    result = {}
    for row in rows[start+1:end]:
        cell = lambda i: row[i].strip() if i < len(row) else ''
        if not re.match(r'^\d{1,4}[./-]\d{1,2}[./-]\d{1,4}\s', cell(time_col)) or not cell(name_col):
            continue
        values = {}
        for i, team in columns:
            try:
                value = Decimal(cell(i))
            except InvalidOperation:
                continue
            if value.is_finite():
                values[team] = format(value, 'f').rstrip('0').rstrip('.') if '.' in format(value, 'f') else str(value)
        result.setdefault(normalize(cell(name_col)), []).append((cell(time_col), values))
    return result


def team_report(records, query, tier_count=10, members=(), aliases=None):
    ballots = [json.loads(r['payload']) for r in records]
    teams = sorted({team for b in ballots for team, _ in b['teams']})
    if not teams:
        raise ValueError('No current responses are available for this draft yet.')
    team = match_team(query, teams)
    # Newest timestamp wins; caller supplies newest inserted revision first for ties.
    ballots.sort(key=lambda b: timestamp(b['timestamp']), reverse=True)
    seen, votes = set(), []
    for ballot in ballots:
        if ballot['error'] or not ballot['name'].strip() or any(not 1 <= t <= tier_count for _, t in ballot['teams']):
            continue
        member = resolve_member(ballot['name'], members, aliases or {})
        identity = ('member', member.id) if member else ('name', normalize(ballot['name']))
        if identity in seen:
            continue
        seen.add(identity)
        chosen = next((t for name, t in ballot['teams'] if name == team), None)
        if chosen is not None:
            vote = {'name': ballot['name'], 'tier': chosen}
            if 'calculated_values' in ballot:
                vote['calculated_value'] = ballot['calculated_values'].get(team)
            votes.append(vote)
    if not votes:
        raise ValueError('No valid votes for this team remain in the current response sheet.')
    values = [v['tier'] for v in votes]
    median = statistics.median(values)
    q1, _, q3 = statistics.quantiles(values, n=4, method='inclusive') if len(values) >= 4 else (min(values), median, max(values))
    # A two-tier minimum fence prevents ties/tiny spreads flagging adjacent tiers.
    fence = max(2, 1.5 * (q3 - q1))
    for vote in votes:
        vote['standout'] = len(values) >= 5 and (vote['tier'] < q1 - fence or vote['tier'] > q3 + fence)
    return dict(team=team, votes=sorted(votes, key=lambda v: (v['tier'], normalize(v['name']))),
                average=statistics.mean(values), median=median, q1=q1, q3=q3,
                counts=[values.count(t) for t in range(1, tier_count + 1)], tiers=tier_count)


