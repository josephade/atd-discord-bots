"""Read-only team vote summaries; delivery status never affects analysis."""
import json
import re
import statistics
from datetime import datetime
from pathlib import Path
from dataclasses import asdict

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
    aliases = {normalize(name): team for name, team in EMOJI_ALIASES.items()}
    mapped = ALIASES.get(normalize(query), aliases.get(normalize(query), query))
    if any(compact(t) == compact(mapped) for t in teams):
        query = mapped
    q = compact(query)
    exact = [t for t in teams if compact(t) == q]
    matches = exact or [t for t in teams if q and q in compact(t)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ValueError('That emoji/name matches multiple teams: ' + ', '.join(matches) + '. Use the full team name.')
    raise ValueError('No team matches that emoji/name in this draft. Use a team name or emoji from this draft\'s ballot.')


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
    records = [{'payload': json.dumps(asdict(ballot))} for ballot in reversed(ballots)]
    return team_report(records, query, tier_count, members, aliases)


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
            votes.append({'name': ballot['name'], 'tier': chosen})
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


