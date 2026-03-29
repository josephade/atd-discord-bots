"""
feedback/analyzer.py
Analyses review summaries from a draft and produces a signal per weight key.
Signal > 0 means the weight should be STRONGER (penalty higher / bonus higher).
Signal < 0 means the weight should be WEAKER.
"""

# ── Reason → weight mapping ───────────────────────────────────────────────────
# Each rejection reason maps to the weight keys it affects.
# "direction" is "penalty" (increase = stronger deterrent) or
#               "bonus"   (increase = stronger attraction).

REASON_WEIGHTS: dict[str, list[dict]] = {
    "ball_dominant_conflict": [
        {"key": "ball_dominant_single",  "direction": "penalty"},
        {"key": "ball_dominant_double",  "direction": "penalty"},
    ],
    "no_scorer": [
        {"key": "scorer_pull_0scorers_base", "direction": "bonus"},
        {"key": "scorer_pull_0scorers_max",  "direction": "bonus"},
        {"key": "scorer_pull_1scorer_base",  "direction": "bonus"},
        {"key": "scorer_pull_1scorer_max",   "direction": "bonus"},
        {"key": "bench_scorer_pull_0_base",  "direction": "bonus"},
        {"key": "bench_scorer_pull_0_max",   "direction": "bonus"},
    ],
    "position_stack": [
        {"key": "r6_r7_duplicate_penalty",   "direction": "penalty"},
        {"key": "bench_only_starter_phase",  "direction": "penalty"},
        {"key": "bench_only_early_bench",    "direction": "penalty"},
    ],
    "star_not_supported": [
        {"key": "pnr_big_pull_early",        "direction": "bonus"},
        {"key": "pnr_big_pull_late",         "direction": "bonus"},
        {"key": "scoring_wing_pull_0",       "direction": "bonus"},
        {"key": "scoring_wing_pull_1",       "direction": "bonus"},
        {"key": "elite_playmaker_pull_1nsb", "direction": "bonus"},
        {"key": "elite_playmaker_pull_2nsb", "direction": "bonus"},
    ],
    "no_defense": [
        {"key": "bench_perimeter_pull",      "direction": "bonus"},
        {"key": "defensive_c_pull",          "direction": "bonus"},
        {"key": "defensive_pf_pull",         "direction": "bonus"},
        {"key": "vd_pull_soft_c",            "direction": "bonus"},
    ],
    "bench_issues": [
        {"key": "backup_c_pull",             "direction": "bonus"},
        {"key": "backup_c_defensive_pull",   "direction": "bonus"},
        {"key": "bench_scorer_pull_0_base",  "direction": "bonus"},
        {"key": "bench_scorer_pull_1",       "direction": "bonus"},
        {"key": "portability_bonus",         "direction": "bonus"},
    ],
    "wrong_tier_balance": [
        {"key": "nsb_redundancy_single",     "direction": "penalty"},
        {"key": "nsb_redundancy_double",     "direction": "penalty"},
        {"key": "soft_big_stack",            "direction": "penalty"},
        {"key": "elite_starter_redundancy",  "direction": "penalty"},
    ],
    "no_shooting": [
        {"key": "shooter_pull_0_base",          "direction": "bonus"},
        {"key": "shooter_pull_0_max",           "direction": "bonus"},
        {"key": "shooter_pull_1_base",          "direction": "bonus"},
        {"key": "shooter_pull_1_max",           "direction": "bonus"},
        {"key": "spacing_urgency_penalty_max",  "direction": "penalty"},
    ],
}

# Human-readable labels for Discord display
REASON_LABELS: dict[str, str] = {
    "ball_dominant_conflict": "🔁 Ball-dominant conflict (2+ iso scorers)",
    "no_scorer":              "🎯 No scorer (team can't generate offense)",
    "position_stack":         "📍 Position stack (too many same position)",
    "star_not_supported":     "🔑 Star not supported (wrong complement)",
    "no_defense":             "🛡️ No defense (perimeter or frontcourt)",
    "bench_issues":           "🪑 Bench issues (depth doesn't fit starters)",
    "wrong_tier_balance":     "📊 Wrong tier balance (too many similar archetypes)",
    "no_shooting":            "🏹 No shooting (starting 5 lacks spacing, bench doesn't cover it)",
}

# Weight constraints — proposals will never go outside these bounds
WEIGHT_BOUNDS: dict[str, tuple[float, float]] = {
    "ball_dominant_single":      (20,  200),
    "ball_dominant_double":      (60,  300),
    "nsb_redundancy_single":     (15,  120),
    "nsb_redundancy_double":     (30,  180),
    "soft_big_stack":            (20,  120),
    "soft_big_immob_c":          (20,  120),
    "elite_starter_redundancy":  (15,  100),
    "bench_only_starter_phase":  (60,  200),
    "bench_only_early_bench":    (25,  120),
    "guard_frontcourt_penalty":  (15,  100),
    "guard_pg_only_penalty":     (5,    40),
    "pure_frontcourt_pg_needed": (15,  100),
    "r6_r7_duplicate_penalty":   (10,   80),
    "scorer_pull_0scorers_base": (10,   40),
    "scorer_pull_0scorers_max":  (30,   80),
    "scorer_pull_1scorer_base":  (6,    30),
    "scorer_pull_1scorer_max":   (15,   50),
    "non_scorer_penalty_max":    (10,   50),
    "bench_scorer_pull_0_base":  (8,    35),
    "bench_scorer_pull_0_max":   (20,   55),
    "bench_scorer_pull_1":       (4,    25),
    "bench_non_scorer_penalty":  (8,    35),
    "c_priority_pull":           (8,    30),
    "sf_priority_pull":          (4,    20),
    "other_pos_priority_pull":   (2,    15),
    "pg_pull_with_c":            (10,   40),
    "pure_frontcourt_pg_push":   (15,   60),
    "vd_pull_soft_c":            (10,   40),
    "elite_playmaker_pull_1nsb": (8,    35),
    "elite_playmaker_pull_2nsb": (10,   40),
    "pnr_big_pull_early":        (10,   40),
    "pnr_big_pull_late":         (6,    30),
    "scoring_wing_pull_0":       (8,    40),
    "scoring_wing_pull_1":       (4,    20),
    "starter_slot_pull_max":     (5,    30),
    "rim_protector_urgency_base":(5,    25),
    "rim_protector_urgency_max": (15,   50),
    "portability_bonus":         (3,    20),
    "shooter_pull_0_base":       (8,    30),
    "shooter_pull_0_max":        (20,   55),
    "shooter_pull_1_base":       (4,    20),
    "shooter_pull_1_max":        (10,   35),
    "spacing_urgency_penalty_max":(10,  40),
    "non_scoring_c_compensation":(8,    35),
    "backup_c_pull":             (6,    30),
    "backup_c_defensive_pull":   (8,    40),
    "defensive_c_pull":          (8,    35),
    "defensive_pf_pull":         (6,    28),
    "bench_perimeter_pull":      (6,    32),
}

# Minimum number of rejections citing a reason before we'll propose a change
MIN_REJECTION_SIGNAL = 3

# Maximum nudge per review cycle (as a fraction of current value)
MAX_NUDGE_FRACTION = 0.30


def compute_signals(summary: dict) -> dict[str, float]:
    """
    Given a review summary dict from db.get_review_summary(), return a dict of
    {weight_key: signal} where signal is in [-1, 1].
    Positive signal → weight should increase.
    Negative signal → weight should decrease.
    Only keys with signal above noise threshold are included.
    """
    total_rejections = summary["rejected"]
    total_teams = summary["total"]

    if total_rejections == 0 or total_teams == 0:
        return {}

    reason_counts = summary["reason_counts"]
    signals: dict[str, float] = {}

    for reason, count in reason_counts.items():
        if count < MIN_REJECTION_SIGNAL:
            continue  # not enough signal
        if reason not in REASON_WEIGHTS:
            continue

        # Rejection rate for this reason (as share of all rejections)
        rejection_rate = count / total_rejections

        # Nudge magnitude: strong signal (>50% of rejections) → 25%, weak → 10%
        if rejection_rate >= 0.5:
            nudge = 0.25
        elif rejection_rate >= 0.35:
            nudge = 0.18
        elif rejection_rate >= 0.20:
            nudge = 0.12
        else:
            nudge = 0.08

        nudge = min(nudge, MAX_NUDGE_FRACTION)

        for weight_entry in REASON_WEIGHTS[reason]:
            key = weight_entry["key"]
            # Always positive: both penalties and bonuses increase in response to rejections
            signals[key] = max(signals.get(key, 0.0), nudge)

    return signals
