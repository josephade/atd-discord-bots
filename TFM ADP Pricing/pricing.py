"""Pure price rules: the five options, price bands and each voter's saved order."""
import random

OTHER_MIN = 0
OTHER_MAX = 100
BATCH_MIX = {1: 2, 2: 2, 3: 6}


def band_for(price):
    if price >= 20:
        return 1
    if price >= 10:
        return 2
    return 3


def step_for(price):
    # 10% of the price, rounded half up, never below $1.
    return max(1, (price + 5) // 10)


def options_for(price):
    step = step_for(price)
    lowest = price - 2 * step
    shift = max(0, 1 - lowest)
    return [lowest + shift + i * step for i in range(5)]


def valid_other(value):
    return type(value) is int and OTHER_MIN <= value <= OTHER_MAX


def build_order(players, rng=None):
    """Deal shuffled bands into shuffled batches of 2 + 2 + 6, then one final batch."""
    rng = rng or random.SystemRandom()
    bands = {band: [] for band in BATCH_MIX}
    for player in players:
        bands[player['band']].append(player['id'])
    for ids in bands.values():
        rng.shuffle(ids)

    order = []
    while any(bands.values()):
        full_batch = all(len(bands[band]) >= count for band, count in BATCH_MIX.items())
        batch = []
        for band, count in BATCH_MIX.items():
            take = count if full_batch else len(bands[band])
            batch.extend(bands[band][:take])
            del bands[band][:take]
        rng.shuffle(batch)
        order.extend(batch)
    return order
