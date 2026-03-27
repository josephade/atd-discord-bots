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
    is_shot_creator,
    is_high_portability,
    is_shooter,
    is_non_scoring_big,
    is_soft_big,
    is_immobile_center,
    is_versatile_defender,
    is_perimeter_defender,
    is_elite_rim_protector,
    is_elite_playmaker,
    is_pnr_creator,
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


def _is_pnr_big(player: str) -> bool:
    """C/PF who can screen and finish at the rim or pop for a jumper.
    Must be a shot creator — non-scoring bigs (Mutombo, Rodman) don't qualify."""
    positions = get_positions(player)
    return (
        any(p in ('PF', 'C') for p in positions)
        and is_shot_creator(player)
        and not is_non_scoring_big(player)
    )


def _is_scoring_wing(player: str) -> bool:
    """Wing/forward who can both shoot AND self-create — not a pure spot-up guy.
    Ideal complement for elite distributors (LeBron, Magic) who kick out off drives."""
    positions = get_positions(player)
    return (
        is_shot_creator(player)
        and is_shooter(player)
        and not is_ball_dominant(player)
        and any(p in ('SG', 'SF', 'PF') for p in positions)
    )


# ── Core scoring ──────────────────────────────────────────────────────────────

def _effective_adp(
    player:            str,
    team_picks:        list[str],
    player_adp:        dict[str, float],
    round_num:         int,
    slots:             dict[str, int],
    bd_count:          int,
    shooter_count:     int,
    nsb_count:         int,
    soft_big_ct:       int,
    has_immob_c:       bool,
    tiers_present:      set[int],
    missing_tier_count: int,
    starter_scorers:    int,   # shot creators among picks 1-5 (starter phase)
    bench_scorers:      int,   # shot creators among picks 6-10 (bench phase)
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
    # Starts from round 3 — rounds 1-2 are pure ADP so elite players go in order.
    # In round 1, every team is missing all 10 tiers, which would give a flat -33
    # bonus to every player and collapse ADP differences (KG and Jokic look equal).
    elif player_tier not in tiers_present and round_num >= 3:
        rounds_left = ROUNDS - round_num          # picks remaining after this one
        overdue     = max(0, round_num - player_tier)   # rounds past ideal timing
        pull        = min(8 + overdue * 5, 40)
        # Critical: more missing tiers than rounds left → must fill NOW
        if rounds_left < missing_tier_count:
            pull = min(pull + 15, 50)   # was +25/65 — reduced so ADP still matters
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

    # ── Scorer distribution: 2-3 in starting 5, 1-2 on bench ────────────────
    # Rounds 1-5 (starter phase): target 2-3 shot creators.
    # Rounds 6-10 (bench phase): target 1-2 shot creators.
    # Round 1 is pure ADP — no scorer pull yet (it would artificially boost Jokic/SGA/Kawhi
    # over players with better raw ADP like KG).
    if round_num >= 2 and round_num <= 5:
        if starter_scorers == 0:
            # No scorer yet — strong escalating pull
            if is_shot_creator(player):
                pull = min(20 + (round_num - 2) * 8, 55)
                adp -= pull
            elif round_num >= 4:
                adp += min((round_num - 3) * 6, 30)  # non-scorers penalized when empty
        elif starter_scorers == 1:
            # One starter scorer — need a second
            if is_shot_creator(player):
                pull = min(14 + (round_num - 2) * 4, 30)
                adp -= pull
        # starter_scorers >= 2: starter scoring covered; 3rd scorer still welcome via ADP
    else:
        # Bench phase (rounds 6-10)
        if bench_scorers == 0:
            # Bench has no scorer — pull toward one, escalating with urgency
            if is_shot_creator(player):
                pull = min(18 + (round_num - 6) * 6, 35)
                adp -= pull
            elif round_num >= 8:
                adp += min((round_num - 7) * 8, 20)  # penalize non-scorers late
        elif bench_scorers == 1:
            # One bench scorer secured — small pull for a second
            if is_shot_creator(player):
                adp -= 10.0

    # ── Ball-dominance conflict — active from round 2 ────────────────────────
    # Stronger penalties: teams can't function with 2+ isolation-first players.
    if is_ball_dominant(player):
        if bd_count >= 2:
            adp += 120.0
        elif bd_count == 1 and round_num >= 2:
            adp += 40.0

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
    # After drafting Harden/Kobe/Westbrook, filling frontcourt is far more important
    # than adding another guard — the bd player already handles PG duties.
    if bd_count >= 1 and round_num <= 5:
        if all(p in ('PG', 'SG') for p in positions):   # guard-only player
            if 'C' not in positions and 'SF' not in positions:
                open_frontcourt = (
                    slots.get('C', 0) == 0 or
                    slots.get('PF', 0) == 0 or
                    slots.get('SF', 0) == 0
                )
                if open_frontcourt:
                    adp += 40.0   # strong penalty — fill frontcourt first
                else:
                    adp += 12.0   # mild — PG is the only open slot remaining

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
    # actively seek ONE versatile defender to compensate. Stop after the first one —
    # stacking versatile defenders starves the team of scoring.
    has_vd = any(is_versatile_defender(p) for p in team_picks)
    if (soft_big_ct >= 1 or has_immob_c) and is_versatile_defender(player) and not has_vd:
        if round_num >= 2:
            adp -= 22.0   # strong pull — cater to the defensive weakness immediately

    # ── Elite playmaker need — when team has a dominant non-scoring big ───────
    # Bigs like Shaq, Gobert, Dwight, and Mobley need an elite floor general to
    # maximize their impact. Generic guards (Jrue, Frazier) won't do — pull toward
    # elite pass-first creators when the PG slot is still open.
    if nsb_count >= 1 and slots.get('PG', 0) == 0 and 'PG' in positions:
        pull = 20 if nsb_count >= 2 else 15
        if is_elite_playmaker(player):
            adp -= pull

    # ── PnR creator needs a scoring big ──────────────────────────────────────
    # Guards and wings who thrive off pick-and-roll (Harden, Nash, CP3, Luka,
    # LeBron, etc.) need a C/PF who can screen and finish at the rim or pop
    # for a jumper. Without one, a huge dimension of their offense is missing.
    # Trigger: team has a PnR creator, no scoring big yet, rounds 2-5.
    if 2 <= round_num <= 5 and any(is_pnr_creator(p) for p in team_picks):
        if not any(_is_pnr_big(p) for p in team_picks) and _is_pnr_big(player):
            pull = 22 if round_num <= 3 else 14
            adp -= pull

    # ── Elite distributor needs scoring wings ─────────────────────────────────
    # Elite playmakers who drive and kick (LeBron, Magic, Oscar, CP3) need wings
    # who can hit open 3s AND self-create when defenders rotate. Pure spot-up
    # shooters aren't enough — they need shot creators who also space the floor.
    # Trigger: team has elite playmaker, fewer than 2 scoring wings, rounds 2-6.
    if 2 <= round_num <= 6 and any(is_elite_playmaker(p) for p in team_picks):
        if _is_scoring_wing(player):
            scoring_wings = sum(1 for p in team_picks if _is_scoring_wing(p))
            if scoring_wings == 0:
                adp -= 20.0   # urgently need at least one
            elif scoring_wings == 1:
                adp -= 9.0    # second scoring wing rounds out the offense

    # ── Creative fit adjustments — rounds 4+ only ────────────────────────────
    if round_num >= 4:

        # Starter-slot need → pull the player earlier (starter slots only).
        starter_needed = {pos for pos, n in slots.items() if n == 0}
        if any(p in starter_needed for p in positions):
            pull = min((round_num - 3) * 2, 15)
            adp -= pull

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

        # Missing-shooter pull — team needs at least 2 shooters for spacing.
        # First shooter: strong pull. Second shooter: moderate pull (round 4-7).
        if shooter_count == 0 and is_shooter(player):
            pull = 15 + max(0, (round_num - 4) * 5)
            adp -= min(pull, 35)
        elif shooter_count == 1 and is_shooter(player) and round_num <= 7:
            pull = 8 + max(0, (round_num - 4) * 3)
            adp -= min(pull, 20)

        # Spacing urgency push — non-shooters penalised when no spacing (round 5+).
        if shooter_count == 0 and not is_shooter(player) and round_num >= 5:
            adp += min(8 + (round_num - 5) * 4, 24)

    # ── Non-scoring C compensation — rounds 2-5 ──────────────────────────────
    # If the starter C is a non-scorer (rim protector only, like Gobert/Ben Wallace),
    # the team needs a shot-creator elsewhere — pull hard toward scorers at PF/SF/SG.
    if slots.get('C', 0) >= 1 and round_num <= 5:
        c_starter = _starter_at('C', team_picks)
        if c_starter and is_non_scoring_big(c_starter):
            if is_shot_creator(player) and any(
                p in positions for p in ('PG', 'SG', 'SF', 'PF')
            ):
                adp -= 18.0   # we NEED scoring elsewhere to compensate

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

    # ── Weak perimeter defense compensation ──────────────────────────────────
    # If 2+ of the starting PG/SG/SF are not versatile defenders (e.g. Curry/Allen/Peja),
    # the team's perimeter defense is a liability. Compensate by:
    #   (a) Prioritising a defensive PF or C to anchor the interior
    #   (b) Pulling wing/perimeter versatile defenders for the bench (rounds 6-9)
    weak_perimeter_ct = sum(
        1 for _pos in ('PG', 'SG', 'SF')
        if (_s := _starter_at(_pos, team_picks)) and not is_perimeter_defender(_s)
    )
    if weak_perimeter_ct >= 2:
        # (a) Defensive frontcourt starter pull — rounds 2-5
        # Versatile forward-type defenders (KG, Draymond, Pippen) to anchor the interior
        if round_num <= 5:
            if slots.get('C', 0) == 0 and 'C' in positions and is_versatile_defender(player):
                adp -= 18.0   # defensive C is the highest priority to cover weak perimeter
            if slots.get('PF', 0) == 0 and 'PF' in positions and is_versatile_defender(player):
                adp -= 14.0   # defensive PF as the second safety net
        # (b) Bench perimeter/wing defenders — rounds 6-9
        # Pull good-to-elite perimeter defenders to sub in vs strong offensive guards/wings
        if 6 <= round_num <= 9:
            if is_perimeter_defender(player):
                adp -= 16.0   # sub-in defenders to matchup vs strong offensive 1-3

    # ── Round 6-7: avoid stacking bench at R1/R2 star positions ──────────────
    # The R1/R2 picks will play the most minutes. In early bench rounds,
    # fill other positions first — don't add a backup at the same spot as the stars.
    if 6 <= round_num <= 7 and len(team_picks) >= 2:
        star_positions: set[str] = set()
        for _p in team_picks[:2]:
            star_positions.update(get_positions(_p))
        if positions and all(pos in star_positions for pos in positions):
            adp += 25.0   # prefer filling other positions' bench slots first

    # ── Fall protection floor ──────────────────────────────────────────────────
    # No player should fall too far past their raw ADP due to fit penalties.
    # Exemptions: DO_NOT_DRAFT (+300), tier 11 (+150), and unknown ADP.
    #
    # R1:   max +3  — nearly pure ADP order, ±1-2 places variation only
    # R2:   max +5  — penalties begin but elite players stay in range
    # R3-5: max +7  — fit logic has influence; these rounds make or break teams
    # R6+:  max +15 — bench phase, more flexibility
    player_is_pos_full = bool(positions) and all(slots.get(p, 0) >= 2 for p in positions)
    # bench_blocked: player would only go to bench while starter slots are open.
    # The +100 penalty for this case must NOT be capped — it's a hard stop.
    bench_blocked = player_is_bench_only and open_starters
    if (not is_do_not_draft(player) and player_tier < 11
            and this_adp < _UNKNOWN_ADP
            and not player_is_pos_full
            and not bench_blocked):
        if round_num == 1:
            max_fall = 3
        elif round_num == 2:
            max_fall = 5
        elif round_num <= 5:
            max_fall = 7
        else:
            max_fall = 15
        adp = min(adp, this_adp + max_fall)

    return adp


def pick(
    team_picks:   list[str],
    available:    list[str],
    player_adp:   dict[str, float] | None = None,
    pool_size:    int                     = 450,
    overall_pick: int                     = 0,
    num_teams:    int                     = 30,
) -> str:
    """
    Choose the best available player for an AI team.

    Parameters
    ----------
    team_picks   : players already drafted by this AI team
    available    : undrafted players still in the pool
    player_adp   : mapping of player name → ADP (lower = better)
    overall_pick : current global pick number across all teams (1-based)
    num_teams    : total number of teams in the draft (used for tier deadlines)
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
    shooter_count      = sum(1 for p in team_picks if is_shooter(p))
    # Rounds 1-5 = starter phase, rounds 6-10 = bench phase (approximate but accurate)
    starter_scorers    = sum(1 for p in team_picks[:5]  if is_shot_creator(p))
    bench_scorers      = sum(1 for p in team_picks[5:]  if is_shot_creator(p))
    tiers_present      = {get_tier(p) for p in team_picks if get_tier(p) <= 10}
    missing_tier_count = sum(1 for t in range(1, 11) if t not in tiers_present)

    # Compute effective ADP for every available player
    eff: dict[str, float] = {}
    for player in available:
        eff[player] = _effective_adp(
            player, team_picks, player_adp,
            round_num, slots, bd_count, shooter_count,
            nsb_count, soft_big_ct, has_immob_c,
            tiers_present, missing_tier_count,
            starter_scorers, bench_scorers,
        )

    # ── Global overdue override ───────────────────────────────────────────────
    # Per-team fall caps prevent a single team from seeing a player as terrible,
    # but they can't stop the player from being skipped by many teams in a row.
    # Two override triggers:
    #   1. ADP-based: player has fallen past their ADP + max_fall picks
    #   2. Tier-based: tier N player still available past round N (pick N*num_teams)
    #      Tier 2 player must be gone by pick 60 in a 30-team draft, etc.
    if overall_pick > 0:
        max_fall_global = 3 if round_num == 1 else (5 if round_num == 2 else (7 if round_num <= 5 else 15))
        for player in available:
            raw = player_adp.get(player, _UNKNOWN_ADP)
            if raw >= _UNKNOWN_ADP:
                continue
            if is_do_not_draft(player) or get_tier(player) >= 11:
                continue
            # Never override position-full or bench-blocked players —
            # their +500/+100 hard-stop penalties must not be bypassed.
            p_positions = get_positions(player)
            if bool(p_positions) and all(slots.get(p, 0) >= 2 for p in p_positions):
                continue
            open_s = any(n == 0 for n in slots.values())
            p_bench_only = bool(p_positions) and all(slots.get(p, 0) >= 1 for p in p_positions)
            if p_bench_only and open_s:
                continue
            player_tier = get_tier(player)
            # Tier-based deadline: tier N player should be drafted by end of round N
            tier_deadline = player_tier * num_teams
            if player_tier <= 5 and overall_pick > tier_deadline:
                # Tier-overdue — decisive pull: must beat any competition even with jitter
                eff[player] = raw - 50.0
            elif overall_pick > raw + max_fall_global:
                # ADP-overdue — strong pull: player has fallen too far past their ADP
                eff[player] = raw - 20.0

    # Jitter for variety:
    # R1:  ±1 — nearly strict ADP order; only adjacent players can swap
    # R2+: ±4 — penalties and fit logic drive picks; wider window for different archetypes
    jitter = 1.0 if round_num == 1 else 4.0
    best = min(available, key=lambda p: eff[p] + random.uniform(-jitter, jitter))

    # Debug: show top 5 candidates
    top5 = sorted(available, key=lambda p: eff[p])[:5]
    print(f"[AI R{round_num}] Top candidates: " +
          " | ".join(
              f"{p} (adp={player_adp.get(p, '?'):.1f}, eff={eff[p]:.1f})"
              if p in player_adp else f"{p} (adp=?, eff={eff[p]:.1f})"
              for p in top5
          ))

    return best
