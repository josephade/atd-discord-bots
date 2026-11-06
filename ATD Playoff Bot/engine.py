"""Odds decide the winner; synthetic basketball statistics explain the result.

No ratings alter the commissioned probabilities. All randomness derives from
one saved cryptographic seed, so a game can be independently reproduced.
"""
import hashlib
import math
import random
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

VERSION = '1.0'
HOME_HIGH = (True, True, False, False, True, False, True)
POSITIONS = ('PG', 'SG', 'SF', 'PF', 'C') * 2
ROTATION = [34, 34, 34, 34, 34, 18, 16, 14, 12, 10]


def percentage(value):
    try:
        value = Decimal(str(value))
        if not value.is_finite() or not 0 <= value <= 100:
            raise ValueError
        return int((value * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        raise ValueError('Enter a percentage from 0 to 100, for example 47 or 47.5.') from None


def probability(base, number, penalty_high=0, penalty_low=0, hca=1000):
    if number not in range(1, 8):
        raise ValueError('Game number must be between 1 and 7.')
    value = base + (hca if HOME_HIGH[number-1] else -hca) - penalty_high + penalty_low
    return max(0, min(10000, value))


def default_profile(position):
    if position in ('PG', 'SG'):
        return dict(pts=20, reb=4, ast=6, source='estimated positional baseline')
    if position == 'SF':
        return dict(pts=20, reb=6, ast=4, source='estimated positional baseline')
    return dict(pts=20, reb=10, ast=3, source='estimated positional baseline')


def validate_roster(players):
    if len(players) != 10:
        raise ValueError('Provide exactly 10 players: PG, SG, SF, PF, C, then Bench 1-5.')
    seen = set()
    for i, p in enumerate(players):
        name = str(p.get('name', '')).strip()
        if not name or len(name) > 70 or name.casefold() in seen:
            raise ValueError('Each roster needs 10 distinct player names (maximum 70 characters each).')
        seen.add(name.casefold())
        p['name'] = name
        p['year'] = str(p.get('year', '')).strip()[:12]
        p['position'] = p.get('position', POSITIONS[i])
        profile = p.setdefault('profile', default_profile(p['position']))
        for key in ('pts', 'reb', 'ast'):
            try:
                n = float(profile[key])
            except (TypeError, ValueError, KeyError):
                raise ValueError('Player baselines must contain numeric pts, reb and ast.') from None
            if not math.isfinite(n) or not 0 <= n <= 80:
                raise ValueError('Player baseline values must be between 0 and 80.')
            profile[key] = n
        profile.setdefault('source', 'commissioner-entered baseline')
        if 'three_share' in profile:
            share=float(profile['three_share'])
            if not math.isfinite(share) or not 0<=share<=1:
                raise ValueError('Three-point share must be between 0 and 1.')
    return players


def allocate(rng, count, weights):
    result = [0] * len(weights)
    weights=[max(0,w) for w in weights]
    for i in rng.choices(range(len(weights)), weights=weights if sum(weights)>0 else [1]*len(weights), k=count):
        result[i] += 1
    return result


def quarter_scores(rng, total):
    # Keep quarters within basketball-like ranges; distribute all remaining points.
    parts = [max(12, round(total / 4 + rng.gauss(0, 4))) for _ in range(3)]
    while sum(parts) > total-12:
        parts[parts.index(max(parts))] -= 1
    return parts + [total-sum(parts)]


def make_box(rng, players, score, ot):
    minutes = ROTATION.copy()
    for _ in range(15):
        a, b = rng.sample(range(10), 2)
        if minutes[a] > 6 and minutes[b] < 42:
            minutes[a] -= 1
            minutes[b] += 1
    if ot:
        for i in range(5):
            minutes[i] += 5
    usage = [max(1, p['profile']['pts']) * minutes[i] * rng.uniform(.75, 1.25) for i,p in enumerate(players)]
    arc = [usage[i] * p['profile'].get('three_share',.2 if p['position'] in ('PF', 'C') else 1) for i,p in enumerate(players)]
    ft = rng.randint(10, 24)
    three = rng.randint(7, 17) if sum(arc)>0 else 0
    if (score-ft-3*three) % 2:
        ft += 1
    two = (score-ft-3*three)//2
    two_m, three_m, ft_m = (allocate(rng,n,w) for n,w in ((two,usage),(three,arc),(ft,usage)))
    two_x, three_x, ft_x = (allocate(rng,n,w) for n,w in ((rng.randint(15,26),usage),(rng.randint(10,24) if sum(arc)>0 else 0,arc),(rng.randint(2,8),usage)))
    ast = allocate(rng, rng.randint(max(1,(two+three)//2), max(1,two+three-2)),
                   [max(1,p['profile']['ast'])*minutes[i] for i,p in enumerate(players)])
    turnovers = allocate(rng, rng.randint(10,17), usage)
    # A single player's assists cannot exceed teammates' made field goals.
    for i in range(10):
        ast[i] = min(ast[i],two+three-two_m[i]-three_m[i])
    box = []
    for i,p in enumerate(players):
        box.append(dict(name=p['name'],year=p['year'],position=p['position'],min=minutes[i],
            pts=2*two_m[i]+3*three_m[i]+ft_m[i],fgm=two_m[i]+three_m[i],fga=two_m[i]+three_m[i]+two_x[i]+three_x[i],
            tpm=three_m[i],tpa=three_m[i]+three_x[i],ftm=ft_m[i],fta=ft_m[i]+ft_x[i],
            ast=ast[i],tov=turnovers[i],reb=0,oreb=0,dreb=0,stl=0,blk=0,pf=0))
    return box


def finish_defense(rng, boxes, players):
    for side in (0,1):
        box, other = boxes[side], boxes[1-side]
        weights = [max(1,p['profile']['reb'])*r['min'] for p,r in zip(players[side],box)]
        # Every live rebound comes from an opponent/own missed field goal.
        misses = sum(r['fga']-r['fgm'] for r in box)
        offensive = int(misses*rng.uniform(.17,.30))
        defensive = misses-offensive-rng.randint(0,3)
        for i,n in enumerate(allocate(rng,offensive,weights)):
            box[i]['oreb'] = n
        other_w = [max(1,p['profile']['reb'])*r['min'] for p,r in zip(players[1-side],other)]
        for i,n in enumerate(allocate(rng,defensive,other_w)):
            other[i]['dreb'] = n
        steals = allocate(rng,rng.randint(4,min(10,sum(r['tov'] for r in other))),[r['min'] for r in box])
        blocks = allocate(rng,rng.randint(2,7),[r['min']*(3 if p['position'] in ('PF','C') else 1) for p,r in zip(players[side],box)])
        fouls = allocate(rng,rng.randint(15,23),[r['min'] for r in box])
        for i,row in enumerate(box):
            row.update(stl=steals[i],blk=blocks[i],pf=min(5,fouls[i]))
    for box in boxes:
        for row in box:
            row['reb'] = row['oreb']+row['dreb']


def simulate(series, number, seed):
    rng = random.Random(int(hashlib.sha256(seed.encode()).hexdigest(),16))
    odds = probability(series['base'], number, series['penalty_high'], series['penalty_low'], series['hca'])
    roll = rng.randint(1,10000)
    winner = 0 if roll <= odds else 1
    margin = rng.choices([rng.randint(1,5),rng.randint(6,12),rng.randint(13,24)], weights=[40,42,18])[0]
    losing_score = rng.randint(88,116)
    scores = [losing_score,losing_score]
    scores[winner] += margin
    ot = margin <= 5 and rng.random() < .16
    if ot:
        loser_ot = rng.randint(7,13)
        tied = losing_score-loser_ot
        quarters = [quarter_scores(rng,tied),quarter_scores(rng,tied)]
        quarters[winner].append(loser_ot+margin)
        quarters[1-winner].append(loser_ot)
    else:
        quarters = [quarter_scores(rng,scores[0]),quarter_scores(rng,scores[1])]
    players = [series['high']['roster'],series['low']['roster']]
    boxes = [make_box(rng,roster,score,ot) for roster,score in zip(players,scores)]
    finish_defense(rng,boxes,players)
    star = max(boxes[winner],key=lambda p:p['pts']+1.2*p['reb']+1.5*p['ast']+2*p['stl']+2*p['blk']-p['tov'])
    before_fourth = [sum(q[:3]) for q in quarters]
    story = ('survived an overtime battle' if ot else
             'rallied in the fourth quarter' if before_fourth[winner]<before_fourth[1-winner] else
             'held on in a tight finish' if margin<=5 else
             'controlled the game from the front' if margin>=13 else 'finished strong to secure the win')
    return dict(version=VERSION,number=number,seed=seed,roll=roll,odds_high=odds,
                winner=winner,home=0 if HOME_HIGH[number-1] else 1,scores=scores,quarters=quarters,
                ot=ot,boxes=boxes,star=star,story=story)


def vote_odds(choices):
    if not choices:
        raise ValueError('There are no ballots. Enter official odds with !simodds instead.')
    # Choices 0..7: higher seed in 4/5/6/7; lower seed in 7/6/5/4.
    expected = ((4,0),(4,1),(4,2),(4,3),(3,4),(2,4),(1,4),(0,4))
    a,b = map(sum,zip(*(expected[c] for c in choices)))
    return percentage(Decimal(a)*100/Decimal(a+b))
