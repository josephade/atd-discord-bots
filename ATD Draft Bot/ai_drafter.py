# ai_drafter.py
# ATD-strategy AI drafter.
#
# Philosophy
# ──────────
# Rounds 1-3  → Follow ADP closely. Hard-stop penalties only.
# Rounds 4-10 → Full creative fit logic.
#
# Key rules:
#   1. Fill all 5 STARTER slots before drafting bench depth.
#      A player who can only go to bench gets a large penalty while any
#      starter slots remain open — unless they are absolute top-5 talent.
#   2. Every center needs an elite PG for pick-and-roll. Prioritise this
#      in the first 4 picks after drafting a C.
#   3. Avoid stacking two similar positional archetypes early (C+C, soft+soft).
#   4. Don't waste a top ADP pick sitting on the bench behind an elite starter.
#   5. Get a backup C — and if the starter C is a defensive liability, make
#      the backup a strong defender.

import random
from config import ROUNDS
from player_data import (
    get_positions,
    get_tier,
    is_ball_dominant,
    is_high_portability,
    is_shooter,
    is_non_scoring_big,
    is_soft_big,
    is_immobile_center,
    is_versatile_defender,
    is_elite_rim_protector,
    is_do_not_draft,
)

POSITIONS = ['PG', 'SG', 'SF', 'PF', 'C']
_UNKNOWN_ADP = 9999.0


# ── Team-state helpers ────────────────────────────────────────────────────────

def _slots_filled(picks: list[str]) -> dict[str, int]:
    """Count how many players occupy each position slot (0, 1, or 2).
    Prefers filling an open starter slot (count=0) before a bench slot (count=1),
    so a SG/SF player goes to SF starter if SG is already taken.
    """
    counts: dict[str, int] = {p: 0 for p in POSITIONS}
    for player in picks:
        positions = get_positions(player)
        # Prefer open starter slot first
        starter_pos = next((p for p in positions if counts[p] == 0), None)
        if starter_pos:
            counts[starter_pos] += 1
        else:
            # No open starter for this player — fill the first available bench slot
            bench_pos = next((p for p in positions if counts[p] < 2), None)
            if bench_pos:
                counts[bench_pos] += 1
    return counts


def _ball_dominant_count(picks: list[str]) -> int:
    return sum(1 for p in picks if is_ball_dominant(p))


def _non_scoring_big_count(picks: list[str]) -> int:
    return sum(1 for p in picks if is_non_scoring_big(p))


def _soft_big_count(picks: list[str]) -> int:
    return sum(1 for p in picks if is_soft_big(p))


def _has_immobile_center(picks: list[str]) -> bool:
    return any(is_immobile_center(p) for p in picks)


def _starter_at(pos: str, picks: list[str]) -> str | None:
    """Return the first player drafted at a given position (the starter)."""
    return next((p for p in picks if pos in get_positions(p)), None)


# ── Core scoring ──────────────────────────────────────────────────────────────

def _effective_adp(
    player:            str,
    team_picks:        list[str],
    player_adp:        dict[str, float],
    round_num:         int,
    slots:             dict[str, int],
    bd_count:          int,
    has_shot:          bool,
    nsb_count:         int,
    soft_big_ct:       int,
    has_immob_c:       bool,
    tiers_present:     set[int],
    missing_tier_count: int,
) -> float:
    """
    Return an effective ADP for this player given team context.
    LOWER = drafted earlier = better.
    """
    adp = player_adp.get(player, _UNKNOWN_ADP)
    positions = get_positions(player)
    this_adp  = adp   # keep original for threshold checks below

    # ── Do-Not-Draft penalty (always active) ─────────────────────────────────
    # These players are rock-bottom priority and should essentially never be picked.
    if is_do_not_draft(player):
        adp += 300.0

    # ── Tier 11 penalty (always active) ──────────────────────────────────────
    # Players not on the tier 1-10 list are unknowns/fillers — deprioritize them.
    player_tier = get_tier(player)
    if player_tier >= 11:
        adp += 150.0

    # ── Tier diversity — fill all 10 tiers before repeating ──────────────────
    # Every team should have at least 1 player from each tier 1-10.
    # Pull grows the more overdue a tier is; becomes critical when rounds run low.
    elif player_tier not in tiers_present:
        rounds_left = ROUNDS - round_num          # picks remaining after this one
        overdue     = max(0, round_num - player_tier)   # rounds past ideal timing
        pull        = min(8 + overdue * 5, 40)
        # Critical: more missing tiers than rounds left → must fill NOW
        if rounds_left < missing_tier_count:
            pull = min(pull + 25, 65)
        adp -= pull

    # ── Position-full penalty (always active) ────────────────────────────────
    if positions and all(slots.get(p, 0) >= 2 for p in positions):
        adp += 500.0

    # ── RULE 1: Fill all starter slots before drafting bench depth ────────────
    # If every position this player can play already has a starter, they would
    # only go to bench. While any starter slot is still empty, strongly
    # discourage bench-only picks (except absolute top-5 ADP talent).
    open_starters = any(n == 0 for n in slots.values())
    player_is_bench_only = bool(positions) and all(slots.get(p, 0) >= 1 for p in positions)

    if player_is_bench_only and open_starters:
        if round_num <= 5 and this_adp > 8.0:
            adp += 100.0   # essentially remove from consideration
        elif round_num <= 7 and this_adp > 15.0:
            adp += 50.0    # still strongly discouraged

    # ── Ball-dominance conflict — active from round 2 ────────────────────────
    if is_ball_dominant(player):
        if bd_count >= 2:
            adp += 80.0
        elif bd_count == 1 and round_num >= 2:
            adp += 25.0

    # ── Non-scoring big redundancy — always active ───────────────────────────
    if is_non_scoring_big(player):
        if nsb_count >= 2:
            adp += 80.0
        elif nsb_count == 1:
            adp += 40.0

    # ── Frontcourt compatibility — always active ──────────────────────────────
    if is_soft_big(player):
        if soft_big_ct >= 1:
            adp += 50.0
        if has_immob_c:
            adp += 50.0

    # ── Elite starter redundancy — active from round 2 ───────────────────────
    # Don't waste a high-ADP pick on bench depth behind an elite starter.
    # (Previously only round 4+; moved to round 2 to catch C+C duos early.)
    if round_num >= 2:
        for pos in positions:
            if slots.get(pos, 0) == 1:
                starter = _starter_at(pos, team_picks)
                if starter and player_adp.get(starter, _UNKNOWN_ADP) <= 30.0:
                    adp += 40.0
                    break

    # ── Position-priority pull — rounds 2-3 ──────────────────────────────────
    # C is the hardest position to fill and anchors defense — always the top
    # priority when empty. SF second. PG/SG are less urgent when the team
    # already has a ball-dominant creator (e.g. Harden covers PG duties).
    if round_num in (2, 3) and not player_is_bench_only:
        if 'C' in positions and slots.get('C', 0) == 0:
            adp -= 16.0   # strongest pull — C is hardest to fill later
        elif 'SF' in positions and slots.get('SF', 0) == 0:
            adp -= 9.0    # wing defense/scoring is high priority
        elif any(slots.get(p, 0) == 0 for p in positions):
            adp -= 5.0    # any other open starter slot

    # PG is less urgent when team already has a ball-dominant creator.
    # After drafting Harden/Magic/CP3, don't rush to fill PG with another handler.
    if bd_count >= 1 and slots.get('PG', 0) == 0 and round_num <= 5:
        if all(p in ('PG', 'SG') for p in positions):   # guard-only player
            if 'C' not in positions and 'SF' not in positions:
                adp += 12.0   # reduce PG/SG urgency — bigger needs exist

    # ── RULE 2: C + PG synergy — rounds 2-5 ──────────────────────────────────
    # Every center needs an elite PG to run pick-and-roll with.
    # If the team has a C but no PG starter, push pure bigs back and pull PGs.
    if slots.get('C', 0) >= 1 and slots.get('PG', 0) == 0:
        if 'PG' in positions and is_ball_dominant(player):
            adp -= 20.0   # strong pull toward elite PGs
        elif all(p in ('PF', 'C') for p in positions) and round_num <= 5:
            adp += 35.0   # push pure-frontcourt additions until PG is secured

    # ── Versatile defender urgency — active from round 2 ─────────────────────
    # When the team's frontcourt has a soft big or immobile center (e.g. Jokic),
    # actively seek a versatile defender to compensate early — don't wait until round 4.
    if (soft_big_ct >= 1 or has_immob_c) and is_versatile_defender(player):
        if round_num >= 2:
            adp -= 22.0   # strong pull — cater to the defensive weakness immediately

    # ── Creative fit adjustments — rounds 4+ only ────────────────────────────
    if round_num >= 4:

        # Starter-slot need → pull the player earlier (starter slots only).
        starter_needed = {pos for pos, n in slots.items() if n == 0}
        if any(p in starter_needed for p in positions):
            pull = min((round_num - 3) * 2, 15)
            adp -= pull

        # Volume scorer floor — if no ball-dominant player yet, prize them.
        if bd_count == 0 and is_ball_dominant(player):
            adp -= 20.0

        # Rim protector urgency — every team needs a defensive anchor in the paint.
        # If no elite shot-blocker on the roster, strongly pull toward one.
        # Escalates each round so the AI can't keep ignoring this need.
        has_rim_protector = any(is_elite_rim_protector(p) for p in team_picks)
        if not has_rim_protector and is_elite_rim_protector(player):
            pull = min(10 + (round_num - 4) * 5, 30)   # −10 at r4, −20 at r6, max −30
            adp -= pull

        # Portability bonus (round 6+)
        if round_num >= 6 and is_high_portability(player):
            adp -= 8.0

        # Missing-shooter pull — escalates each round to force spacing.
        if not has_shot and is_shooter(player):
            pull = 15 + max(0, (round_num - 4) * 5)
            adp -= min(pull, 35)

        # Spacing urgency push — non-shooters penalised when no spacing (round 5+).
        if not has_shot and not is_shooter(player) and round_num >= 5:
            adp += min(8 + (round_num - 5) * 4, 24)

    # ── RULE 3: Backup center — rounds 6-9 ───────────────────────────────────
    # Big man depth goes fast. If starter C is set but bench C is empty,
    # actively pull toward backup Cs.
    if slots.get('C', 0) == 1 and 6 <= round_num <= 9:
        if 'C' in positions:
            adp -= 15.0   # pull any available C for bench

        # If starter C is a defensive liability (soft big or immobile),
        # the backup MUST be a strong defender — extra pull for versatile defenders.
        c_starter = _starter_at('C', team_picks)
        if c_starter and (is_soft_big(c_starter) or is_immobile_center(c_starter)):
            if 'C' in positions and is_versatile_defender(player):
                adp -= 20.0   # additional pull for defensive C backup

    return adp


def pick(
    team_picks:  list[str],
    available:   list[str],
    player_adp:  dict[str, float] | None = None,
    pool_size:   int                     = 450,
) -> str:
    """
    Choose the best available player for an AI team.

    Parameters
    ----------
    team_picks  : players already drafted by this AI team
    available   : undrafted players still in the pool
    player_adp  : mapping of player name → ADP (lower = better)
    pool_size   : unused; kept for API compatibility
    """
    if not available:
        raise ValueError("No players available to draft.")

    if player_adp is None:
        player_adp = {}

    round_num          = len(team_picks) + 1
    slots              = _slots_filled(team_picks)
    bd_count           = _ball_dominant_count(team_picks)
    nsb_count          = _non_scoring_big_count(team_picks)
    soft_big_ct        = _soft_big_count(team_picks)
    has_immob_c        = _has_immobile_center(team_picks)
    has_shot           = any(is_shooter(p) for p in team_picks)
    tiers_present      = {get_tier(p) for p in team_picks if get_tier(p) <= 10}
    missing_tier_count = sum(1 for t in range(1, 11) if t not in tiers_present)

    # Compute effective ADP for every available player
    eff: dict[str, float] = {}
    for player in available:
        eff[player] = _effective_adp(
            player, team_picks, player_adp,
            round_num, slots, bd_count, has_shot,
            nsb_count, soft_big_ct, has_immob_c,
            tiers_present, missing_tier_count,
        )

    # Tiny jitter (±0.4 ADP units) — variety between similarly-ranked players
    # without ever swapping players 1+ ADP apart.
    best = min(available, key=lambda p: eff[p] + random.uniform(-0.4, 0.4))

    # Debug: show top 5 candidates
    top5 = sorted(available, key=lambda p: eff[p])[:5]
    print(f"[AI R{round_num}] Top candidates: " +
          " | ".join(
              f"{p} (adp={player_adp.get(p, '?'):.1f}, eff={eff[p]:.1f})"
              if p in player_adp else f"{p} (adp=?, eff={eff[p]:.1f})"
              for p in top5
          ))

    return best
