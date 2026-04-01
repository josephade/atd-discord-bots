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

import json
import os
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

# ── Weight loader ─────────────────────────────────────────────────────────────
# All tunable penalty/bonus values live in weights.json.
# Call reload_weights() after confirming a weight change proposal.
_WEIGHTS_PATH = os.path.join(os.path.dirname(__file__), "weights.json")

def _load_weights() -> dict:
    with open(_WEIGHTS_PATH) as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}

W = _load_weights()

def reload_weights() -> None:
    """Reload weights from disk — call after writing a confirmed proposal."""
    global W
    W = _load_weights()


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


def _flex_covered_positions(picks: list[str]) -> set[str]:
    """
    Returns bench positions 'covered' by starter versatility.
    A covered position can wait until rounds 9-10 for a dedicated backup
    rather than being sought urgently in rounds 7-8.

    Coverage rules (ATD-specific):
    - PG bench: covered if the SG starter is ball-dominant (handles bench
                ball-handling duties — Wade, Kobe, Harden, Iverson, etc.)
    - SG bench: covered if the SF starter is listed as SG/SF (can slide down)
    - SF bench: covered if the PF starter is listed as SF/PF (can slide up)

    LeBron is intentionally excluded — he plays SF/PF only in ATD context
    and does NOT cover PG bench duties.
    """
    covered: set[str] = set()

    sg_starter = _starter_at('SG', picks)
    if sg_starter and is_ball_dominant(sg_starter):
        covered.add('PG')

    sf_starter = _starter_at('SF', picks)
    if sf_starter and 'SG' in get_positions(sf_starter):
        covered.add('SG')

    pf_starter = _starter_at('PF', picks)
    if pf_starter and 'SF' in get_positions(pf_starter):
        covered.add('SF')

    return covered


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
            adp += W["bench_only_starter_phase"]
        elif round_num <= 7 and this_adp > 15.0:
            adp += W["bench_only_early_bench"]

    # ── Scorer distribution: 2-3 in starting 5, 1-2 on bench ────────────────
    # Rounds 1-5 (starter phase): target 2-3 shot creators.
    # Rounds 6-10 (bench phase): target 1-2 shot creators.
    # Round 1 is pure ADP — no scorer pull yet (it would artificially boost Jokic/SGA/Kawhi
    # over players with better raw ADP like KG).
    if round_num >= 2 and round_num <= 5:
        if starter_scorers == 0:
            # No scorer yet — strong escalating pull
            if is_shot_creator(player):
                pull = min(W["scorer_pull_0scorers_base"] + (round_num - 2) * 8, W["scorer_pull_0scorers_max"])
                adp -= pull
            elif round_num >= 4:
                adp += min((round_num - 3) * 6, W["non_scorer_penalty_max"])
        elif starter_scorers == 1:
            # One starter scorer — need a second
            if is_shot_creator(player):
                pull = min(W["scorer_pull_1scorer_base"] + (round_num - 2) * 4, W["scorer_pull_1scorer_max"])
                adp -= pull
        # starter_scorers >= 2: starter scoring covered; 3rd scorer still welcome via ADP
    else:
        # Bench phase (rounds 6-10)
        if bench_scorers == 0:
            # Bench has no scorer — pull toward one, escalating with urgency
            if is_shot_creator(player):
                pull = min(W["bench_scorer_pull_0_base"] + (round_num - 6) * 6, W["bench_scorer_pull_0_max"])
                adp -= pull
            elif round_num >= 8:
                adp += min((round_num - 7) * 8, W["bench_non_scorer_penalty"])
        elif bench_scorers == 1:
            # One bench scorer secured — small pull for a second
            if is_shot_creator(player):
                adp -= W["bench_scorer_pull_1"]

    # ── Ball-dominance conflict — active from round 2 ────────────────────────
    # Stronger penalties: teams can't function with 2+ isolation-first players.
    if is_ball_dominant(player):
        if bd_count >= 2:
            adp += W["ball_dominant_double"]
        elif bd_count == 1 and round_num >= 2:
            adp += W["ball_dominant_single"]

    # ── Non-scoring big redundancy — always active ───────────────────────────
    if is_non_scoring_big(player):
        if nsb_count >= 2:
            adp += W["nsb_redundancy_double"]
        elif nsb_count == 1:
            adp += W["nsb_redundancy_single"]

    # ── Frontcourt compatibility — always active ──────────────────────────────
    if is_soft_big(player):
        if soft_big_ct >= 1:
            adp += W["soft_big_stack"]
        if has_immob_c:
            adp += W["soft_big_immob_c"]

    # ── Elite starter redundancy — active from round 2 ───────────────────────
    # Don't waste a high-ADP pick on bench depth behind an elite starter.
    # (Previously only round 4+; moved to round 2 to catch C+C duos early.)
    if round_num >= 2:
        for pos in positions:
            if slots.get(pos, 0) == 1:
                starter = _starter_at(pos, team_picks)
                if starter and player_adp.get(starter, _UNKNOWN_ADP) <= 30.0:
                    adp += W["elite_starter_redundancy"]
                    break

    # ── Position-priority pull — rounds 2-3 ──────────────────────────────────
    # C is the hardest position to fill and anchors defense — always the top
    # priority when empty. SF second. PG/SG are less urgent when the team
    # already has a ball-dominant creator (e.g. Harden covers PG duties).
    if round_num in (2, 3) and not player_is_bench_only:
        if 'C' in positions and slots.get('C', 0) == 0:
            adp -= W["c_priority_pull"]
        elif 'PG' in positions and slots.get('PG', 0) == 0:
            adp -= W["pg_priority_pull"]
        elif 'SF' in positions and slots.get('SF', 0) == 0:
            adp -= W["sf_priority_pull"]
        elif any(slots.get(p, 0) == 0 for p in positions):
            adp -= W["other_pos_priority_pull"]

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
                    adp += W["guard_frontcourt_penalty"]
                else:
                    adp += W["guard_pg_only_penalty"]

    # ── Both frontcourt slots empty penalty — rounds 3-5 ─────────────────────
    # If neither PF nor C has a starter by round 3, penalise ANY perimeter-only
    # player (PG/SG/SF — no PF or C in their positions).  This catches wing
    # players like Battier who slip through the guard-only check above but still
    # deepen the backcourt when the frontcourt is completely unaddressed.
    both_frontcourt_empty = slots.get('C', 0) == 0 and slots.get('PF', 0) == 0
    if both_frontcourt_empty and 3 <= round_num <= 5:
        if positions and all(p in ('PG', 'SG', 'SF') for p in positions):
            adp += W["both_frontcourt_empty_penalty"]

    # ── RULE 2: C + PG synergy — rounds 2-5 ──────────────────────────────────
    # Every center needs an elite PG to run pick-and-roll with.
    # If the team has a C but no PG starter, push pure bigs back and pull PGs.
    if slots.get('C', 0) >= 1 and slots.get('PG', 0) == 0:
        if 'PG' in positions and is_ball_dominant(player):
            adp -= W["pg_pull_with_c"]
        elif all(p in ('PF', 'C') for p in positions) and round_num <= 5:
            adp += W["pure_frontcourt_pg_needed"]

    # ── Versatile defender urgency — active from round 2 ─────────────────────
    # When the team's frontcourt has a soft big or immobile center (e.g. Jokic),
    # actively seek ONE versatile defender to compensate. Stop after the first one —
    # stacking versatile defenders starves the team of scoring.
    has_vd = any(is_versatile_defender(p) for p in team_picks)
    if (soft_big_ct >= 1 or has_immob_c) and is_versatile_defender(player) and not has_vd:
        if round_num >= 2:
            adp -= W["vd_pull_soft_c"]

    # ── Elite playmaker need — when team has a dominant non-scoring big ───────
    # Bigs like Shaq, Gobert, Dwight, and Mobley need an elite floor general to
    # maximize their impact. Generic guards (Jrue, Frazier) won't do — pull toward
    # elite pass-first creators when the PG slot is still open.
    if nsb_count >= 1 and slots.get('PG', 0) == 0 and 'PG' in positions:
        pull = W["elite_playmaker_pull_2nsb"] if nsb_count >= 2 else W["elite_playmaker_pull_1nsb"]
        if is_elite_playmaker(player):
            adp -= pull

    # ── PnR creator needs a scoring big ──────────────────────────────────────
    if 2 <= round_num <= 5 and any(is_pnr_creator(p) for p in team_picks):
        if not any(_is_pnr_big(p) for p in team_picks) and _is_pnr_big(player):
            pull = W["pnr_big_pull_early"] if round_num <= 3 else W["pnr_big_pull_late"]
            adp -= pull

    # ── Elite distributor needs scoring wings ─────────────────────────────────
    if 2 <= round_num <= 6 and any(is_elite_playmaker(p) for p in team_picks):
        if _is_scoring_wing(player):
            scoring_wings = sum(1 for p in team_picks if _is_scoring_wing(p))
            if scoring_wings == 0:
                adp -= W["scoring_wing_pull_0"]
            elif scoring_wings == 1:
                adp -= W["scoring_wing_pull_1"]

    # ── Creative fit adjustments — rounds 4+ only ────────────────────────────
    if round_num >= 4:

        # Starter-slot need → pull the player earlier (starter slots only).
        starter_needed = {pos for pos, n in slots.items() if n == 0}
        if any(p in starter_needed for p in positions):
            pull = min((round_num - 3) * 2, W["starter_slot_pull_max"])
            adp -= pull

        # Rim protector urgency — every team needs a defensive anchor in the paint.
        has_rim_protector = any(is_elite_rim_protector(p) for p in team_picks)
        if not has_rim_protector and is_elite_rim_protector(player):
            pull = min(W["rim_protector_urgency_base"] + (round_num - 4) * 5, W["rim_protector_urgency_max"])
            adp -= pull

        # Portability bonus (round 6+)
        if round_num >= 6 and is_high_portability(player):
            adp -= W["portability_bonus"]

        # Missing-shooter pull — team needs at least 2 shooters for spacing.
        if shooter_count == 0 and is_shooter(player):
            pull = W["shooter_pull_0_base"] + max(0, (round_num - 4) * 5)
            adp -= min(pull, W["shooter_pull_0_max"])
        elif shooter_count == 1 and is_shooter(player) and round_num <= 7:
            pull = W["shooter_pull_1_base"] + max(0, (round_num - 4) * 3)
            adp -= min(pull, W["shooter_pull_1_max"])

        # Spacing urgency push — non-shooters penalised when no spacing (round 5+).
        if shooter_count == 0 and not is_shooter(player) and round_num >= 5:
            adp += min(8 + (round_num - 5) * 4, W["spacing_urgency_penalty_max"])

    # ── Non-scoring C compensation — rounds 2-5 ──────────────────────────────
    # If the starter C is a non-scorer (rim protector only, like Gobert/Ben Wallace),
    # the team needs a shot-creator elsewhere — pull hard toward scorers at PF/SF/SG.
    if slots.get('C', 0) >= 1 and round_num <= 5:
        c_starter = _starter_at('C', team_picks)
        if c_starter and is_non_scoring_big(c_starter):
            if is_shot_creator(player) and any(
                p in positions for p in ('PG', 'SG', 'SF', 'PF')
            ):
                adp -= W["non_scoring_c_compensation"]

    # ── RULE 3: Backup center — rounds 6-9 ───────────────────────────────────
    if slots.get('C', 0) == 1 and 6 <= round_num <= 9:
        c_starter = _starter_at('C', team_picks)
        # If a PF/C swing player (e.g. Elton Brand, KG) is already on the roster,
        # they cover C flex duties — no need for a dedicated backup C.
        has_c_flex = any(
            'C' in get_positions(p) and p != c_starter
            for p in team_picks
        )
        if not has_c_flex and 'C' in positions:
            adp -= W["backup_c_pull"]

        if c_starter and (is_soft_big(c_starter) or is_immobile_center(c_starter)):
            if not has_c_flex and 'C' in positions and is_versatile_defender(player):
                adp -= W["backup_c_defensive_pull"]

    # ── Bench playmaker need ──────────────────────────────────────────────────
    # If the starting PG is the team's ONLY real ball handler / initiator,
    # the bench unit has nobody to run the offense when that PG sits.
    # Escalate backup PG urgency strongly in rounds 6-8 for such teams.
    if 6 <= round_num <= 8 and 'PG' in positions and slots.get('PG', 0) == 1:
        # Only pull toward PGs who can actually run an ATD-level bench offense.
        # Generic backup PGs (Brogdon, etc.) are not capable initiators at this level.
        player_can_initiate = (
            is_ball_dominant(player)
            or is_elite_playmaker(player)
            or is_shot_creator(player)
        )
        if player_can_initiate:
            pg_starter = _starter_at('PG', team_picks)
            other_starters = [p for p in team_picks[:5] if p != pg_starter]
            # Check if anyone else in the starting 5 can initiate offense
            has_secondary_handler = any(
                is_ball_dominant(p) or is_elite_playmaker(p)
                for p in other_starters
            )
            # Check if bench already has a capable ball handler drafted
            bench_picks = team_picks[5:]
            bench_has_handler = any(
                (is_ball_dominant(p) or is_elite_playmaker(p)) and 'PG' in get_positions(p)
                for p in bench_picks
            )
            if not has_secondary_handler and not bench_has_handler:
                adp -= W["bench_playmaker_pull"]

    # ── Backup position urgency (non-C) — flex-coverage aware ────────────────
    # In ATD, starters with position flex can cover bench minutes at adjacent
    # positions (e.g. a ball-dominant SG covering PG bench duties), so dedicated
    # backup urgency is delayed until rounds 9-10 for those slots.
    # Non-covered positions get gentle urgency starting round 7.
    # C has its own dedicated pull above and is excluded here.
    flex_covered = _flex_covered_positions(team_picks)
    for bpos in ('PG', 'SG', 'SF', 'PF'):
        if slots.get(bpos, 0) == 1 and bpos in positions:
            urgency_start = 9 if bpos in flex_covered else 7
            if urgency_start <= round_num <= 10:
                adp -= W["backup_position_pull"]
            break  # only apply once even for multi-position players

    # ── Weak perimeter defense compensation ──────────────────────────────────
    # If 2+ of the starting PG/SG/SF are not versatile defenders (e.g. Curry/Allen/Peja),
    # the team's perimeter defense is a liability. Compensate by:
    #   (a) Prioritising a defensive PF or C to anchor the interior
    #   (b) Pulling wing/perimeter versatile defenders for the bench (rounds 6-9)
    #
    # Exception: if the team's star (top-2 pick) is an elite shooter, the team
    # identity is offensive (spacing, ball movement). Raise the threshold so the
    # AI leans toward shooters and passers instead of forcing a defensive roster
    # around a player like Curry.
    star_is_shooter = any(
        is_shooter(p) and is_shot_creator(p) and player_adp.get(p, _UNKNOWN_ADP) <= 25
        for p in team_picks[:2]
    )
    weak_perimeter_ct = sum(
        1 for _pos in ('PG', 'SG', 'SF')
        if (_s := _starter_at(_pos, team_picks)) and not is_perimeter_defender(_s)
    )
    effective_weak_threshold = W["weak_perimeter_threshold"] + (1 if star_is_shooter else 0)
    if weak_perimeter_ct >= effective_weak_threshold:
        # (a) Defensive frontcourt starter pull — rounds 2-5
        if round_num <= 5:
            if slots.get('C', 0) == 0 and 'C' in positions and is_versatile_defender(player):
                adp -= W["defensive_c_pull"]
            if slots.get('PF', 0) == 0 and 'PF' in positions and is_versatile_defender(player):
                adp -= W["defensive_pf_pull"]
        # (b) Bench perimeter/wing defenders — rounds 6-9
        if 6 <= round_num <= 9:
            if is_perimeter_defender(player):
                adp -= W["bench_perimeter_pull"]

    # ── Defense saturation penalty ────────────────────────────────────────────
    # Once the team has 2+ defensive specialists (versatile/perimeter defenders),
    # additional pure defenders crowd out offensive contributors.
    # Two-way players (shot creators who also defend) are exempt — they add
    # value beyond just defense and shouldn't be discouraged.
    defender_count = sum(
        1 for p in team_picks
        if is_versatile_defender(p) or is_perimeter_defender(p)
    )
    if defender_count >= 2:
        if (is_versatile_defender(player) or is_perimeter_defender(player)) and not is_shot_creator(player):
            adp += W["defense_saturation_penalty"]

    # ── Bench guard redundancy — active from round 6 ─────────────────────────
    # Don't stack multiple scoring guards on the bench — they add the same thing.
    # One bench scoring guard (Dragic, Terry, etc.) is fine; a second gives
    # the same production and crowds out a rebounder, defender, or big.
    if round_num >= 6:
        bench_picks = team_picks[5:]
        bench_scoring_guards = sum(
            1 for p in bench_picks
            if is_shot_creator(p) and positions and all(pos in ('PG', 'SG') for pos in get_positions(p))
        )
        if bench_scoring_guards >= 1:
            if is_shot_creator(player) and positions and all(pos in ('PG', 'SG') for pos in positions):
                adp += W["bench_guard_redundancy"]

    # ── Round 6-7: avoid stacking bench at R1/R2 star positions ──────────────
    if 6 <= round_num <= 7 and len(team_picks) >= 2:
        star_positions: set[str] = set()
        for _p in team_picks[:2]:
            star_positions.update(get_positions(_p))
        if positions and all(pos in star_positions for pos in positions):
            adp += W["r6_r7_duplicate_penalty"]

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
            # Tier 1-2 players are too valuable to fall far — 2-pick max in R2.
            # Tier 3+ gets the normal 5-pick window.
            max_fall = 2 if player_tier <= 2 else 5
        elif round_num == 3:
            # Tier 1-2: already protected above. Tier 3: tighten to +4 so players
            # like Gary Payton (ADP ~80, PG-only) don't drift to pick 90+.
            # Tier 4+: normal 7-pick window.
            max_fall = 4 if player_tier <= 3 else 7
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
        max_fall_global = 3 if round_num == 1 else (5 if round_num == 2 else (4 if round_num == 3 else (7 if round_num <= 5 else 15)))
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
            # Tier-based deadline: by what overall pick should this tier be gone?
            # Tier 1 → end of R1 (pick 30). Tier 2 → early R2 (pick ~35).
            # Tier 3+ → end of round N (pick N * num_teams).
            if player_tier == 1:
                tier_deadline = num_teams
            elif player_tier == 2:
                tier_deadline = num_teams + 5        # gone by pick 35 in a 30-team draft
            elif player_tier == 3:
                tier_deadline = num_teams * 3 - 5   # gone by pick 85 in a 30-team draft
            else:
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
