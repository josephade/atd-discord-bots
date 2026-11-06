"""Turn stored votes into admin results. These describe votes; they aren't new prices."""
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from pricing import options_for

GAP_MIN_VOTES = 5
GAP_LIMIT = 20
FLAG_MIN_VOTES = 20
FLAG_LABELS = {
    'same_answer': 'Same answer',
    'extreme_prices': 'Extreme prices',
    'too_fast': 'Too fast',
    'repeat_username': 'Repeat username',
    'same_device': 'Shared IP address',
}
FLAG_DETAILS = {
    'same_answer': 'At least 20 answers use the same price-button position. This can be a legitimate preference.',
    'extreme_prices': 'More than 25% of price votes are at least 3 times, or below one third of, the current price.',
    'too_fast': 'At least 20 initial answer timestamps average less than one second apart.',
    'repeat_username': 'Multiple voter sessions use this username. Identity is not verified.',
    'same_device': 'Different usernames share an IP address. This may be a shared network, not the same person.',
}


def _number(value):
    return int(value) if value == int(value) else round(value, 2)


def player_rows(players, active_votes):
    prices = defaultdict(list)
    dont_know = Counter()
    for vote in active_votes:
        if vote['answer_type'] == 'dont_know':
            dont_know[vote['player_id']] += 1
        else:
            prices[vote['player_id']].append(vote['price'])

    rows = []
    for player in players:
        votes = prices[player['id']]
        row = dict(
            id=player['id'],
            name=player['name'],
            current_price=player['current_price'],
            votes=len(votes),
            dont_know=dont_know[player['id']],
            median=None,
            average=None,
            low=None,
            high=None,
            gap=None,
            q1=None,
            q3=None,
        )
        if votes:
            median = statistics.median(votes)
            row.update(
                median=_number(median),
                average=round(statistics.fmean(votes), 2),
                low=min(votes),
                high=max(votes),
                gap=_number(median - player['current_price']),
            )
            quartiles = statistics.quantiles(votes, n=4, method='inclusive') if len(votes) > 1 else [votes[0]] * 3
            row.update(q1=_number(quartiles[0]), q3=_number(quartiles[2]))
        rows.append(row)
    return rows


def biggest_gaps(rows):
    eligible = [row for row in rows if row['votes'] >= GAP_MIN_VOTES and row['gap']]
    overpriced = sorted((r for r in eligible if r['gap'] < 0), key=lambda r: (r['gap'], -r['votes'], r['name']))
    underpriced = sorted((r for r in eligible if r['gap'] > 0), key=lambda r: (-r['gap'], -r['votes'], r['name']))
    return dict(overpriced=overpriced[:GAP_LIMIT], underpriced=underpriced[:GAP_LIMIT])


def histogram(current_price, votes):
    counts = Counter(v['price'] for v in votes if v['answer_type'] != 'dont_know')
    counts.setdefault(current_price, 0)
    return [dict(price=price, count=counts[price]) for price in sorted(counts)]


def overview(players, voters, all_votes, active_votes):
    answered = Counter(v['voter_id'] for v in all_votes)
    total = len(players)
    return dict(
        total_players=total,
        total_voters=len(voters),
        finished_voters=sum(1 for v in voters if total and answered[v['id']] >= total),
        total_votes=len(active_votes),
        price_votes=sum(1 for v in active_votes if v['answer_type'] != 'dont_know'),
        dont_know_votes=sum(1 for v in active_votes if v['answer_type'] == 'dont_know'),
    )


def _timestamp(value):
    return datetime.fromisoformat(value).timestamp()


def _vote_flags(votes, price_by_player):
    """Flags read from one voter's own votes that are still in results."""
    flags = []
    count = len(votes)
    if count >= FLAG_MIN_VOTES:
        positions = set()
        for vote in votes:
            if vote['answer_type'] != 'option':
                positions.add(None)
                continue
            options = options_for(price_by_player[vote['player_id']])
            positions.add(options.index(vote['price']) if vote['price'] in options else None)
        if len(positions) == 1 and None not in positions:
            flags.append('same_answer')

        times = sorted(_timestamp(v['created_at']) for v in votes)
        if (times[-1] - times[0]) / (count - 1) < 1:
            flags.append('too_fast')

    priced = [v for v in votes if v['answer_type'] != 'dont_know']
    extreme = 0
    for vote in priced:
        current = price_by_player[vote['player_id']]
        if vote['price'] >= current * 3 or vote['price'] * 3 < current:
            extreme += 1
    if priced and extreme / len(priced) > 0.25:
        flags.append('extreme_prices')
    return flags


def voter_rows(players, voters, all_votes):
    total = len(players)
    price_by_player = {p['id']: p['current_price'] for p in players}
    answered = Counter(v['voter_id'] for v in all_votes)
    active = defaultdict(list)
    for vote in all_votes:
        if vote['deleted_at'] is None:
            active[vote['voter_id']].append(vote)

    name_counts = Counter(v['discord_username'].casefold() for v in voters)
    device_names = defaultdict(set)
    for voter in voters:
        device_names[voter['ip_hash']].add(voter['discord_username'].casefold())

    rows = []
    for voter in voters:
        votes = active[voter['id']]
        flags = _vote_flags(votes, price_by_player)
        if name_counts[voter['discord_username'].casefold()] > 1:
            flags.append('repeat_username')
        if len(device_names[voter['ip_hash']]) > 1:
            flags.append('same_device')
        rows.append(dict(
            id=voter['id'],
            username=voter['discord_username'],
            answered=answered[voter['id']],
            total=total,
            finished=bool(total) and answered[voter['id']] >= total,
            votes_in_results=len(votes),
            dont_know=sum(1 for v in votes if v['answer_type'] == 'dont_know'),
            flags=[dict(id=flag, label=FLAG_LABELS[flag], detail=FLAG_DETAILS[flag]) for flag in flags],
            created_at=voter['created_at'],
            last_seen_at=voter['last_seen_at'],
        ))
    return rows


def dashboard(players, voters, all_votes, active_votes, player_results, voter_results, now=None):
    """Explain participation, coverage and recent saves from the same snapshot."""
    now = now or datetime.now(timezone.utc)
    total = len(players)
    answered = Counter(v['voter_id'] for v in all_votes)
    buckets = [0] * 6
    for voter in voters:
        progress = answered[voter['id']] / total if total else 0
        index = 0 if progress == 0 else 5 if progress >= 1 else min(4, int(progress * 4) + 1)
        buckets[index] += 1
    start = now.date() - timedelta(days=6)
    days = {(start + timedelta(days=i)).isoformat(): dict(date=(start + timedelta(days=i)).isoformat(),
                                                      price_votes=0, dont_know=0) for i in range(7)}
    for vote in active_votes:
        day = datetime.fromisoformat(vote['created_at']).astimezone(timezone.utc).date().isoformat()
        if day in days:
            days[day]['dont_know' if vote['answer_type'] == 'dont_know' else 'price_votes'] += 1
    players_by_id = {p['id']: p for p in players}
    voters_by_id = {v['id']: v for v in voters}
    recent = [dict(vote_id=v['id'], voter_id=v['voter_id'], player_id=v['player_id'],
                   username=voters_by_id[v['voter_id']]['discord_username'],
                   player=players_by_id[v['player_id']]['name'], answer_type=v['answer_type'],
                   price=v['price'], saved_at=v['updated_at'])
              for v in sorted(active_votes, key=lambda v: (v['updated_at'], v['id']), reverse=True)[:8]]
    qualified = sum(row['votes'] >= GAP_MIN_VOTES for row in player_results)
    return dict(
        average_completion=round(sum(answered.values()) / (len(voters) * total) * 100, 1) if voters and total else 0,
        active_voters_24h=sum(_timestamp(v['last_seen_at']) >= now.timestamp() - 86400 for v in voters),
        qualified_players=qualified, low_coverage_players=total - qualified,
        coverage_percent=round(qualified / total * 100, 1) if total else 0,
        unpriced_players=sum(row['votes'] == 0 for row in player_results),
        excluded_answers=len(all_votes) - len(active_votes),
        flagged_voters=sum(bool(v['flags']) for v in voter_results),
        in_progress_voters=sum(0 < answered[v['id']] < total for v in voters),
        not_started_voters=sum(answered[v['id']] == 0 for v in voters),
        minimum_price_votes=GAP_MIN_VOTES,
        completion=[dict(label=label, count=count) for label, count in zip(
            ['Not started', 'Under 25%', '25–49%', '50–74%', '75–99%', 'Finished'], buckets)],
        activity=list(days.values()), recent_answers=recent,
        review_voters=sorted((v for v in voter_results if v['flags']),
                            key=lambda v: (-len(v['flags']), v['username'].casefold()))[:4],
    )
