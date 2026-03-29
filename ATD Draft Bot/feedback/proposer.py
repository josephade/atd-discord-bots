"""
feedback/proposer.py
Converts analyzer signals into concrete weight change proposals, writes them
to weights.json on confirmation, and formats Discord-ready messages.
"""

import json
import os
from datetime import datetime

from feedback.analyzer import WEIGHT_BOUNDS, REASON_LABELS, compute_signals
from feedback import db as fdb

_WEIGHTS_PATH = os.path.join(os.path.dirname(__file__), "..", "weights.json")


def _load_weights() -> dict:
    with open(_WEIGHTS_PATH) as f:
        return json.load(f)


def _save_weights(data: dict) -> None:
    data["_last_updated"] = datetime.utcnow().strftime("%Y-%m-%d")
    with open(_WEIGHTS_PATH, "w") as f:
        json.dump(data, f, indent=2)


# ── Proposal generation ───────────────────────────────────────────────────────

def build_proposals(draft_id: int) -> list[dict]:
    """
    Run analysis on a completed draft and return a list of proposed changes:
    [
        {
            "key":        "ball_dominant_single",
            "old_value":  40,
            "new_value":  50,
            "pct_change": 25.0,
            "reason":     "4 teams rejected for ball-dominant conflict (50% of rejections)"
        },
        ...
    ]
    Returns an empty list if there are no actionable signals.
    """
    summary = fdb.get_review_summary(draft_id)
    signals = compute_signals(summary)
    if not signals:
        return []

    weights_data = _load_weights()
    proposals = []

    for key, nudge in signals.items():
        if key not in weights_data:
            continue
        current = float(weights_data[key])
        proposed = round(current * (1 + nudge))

        # Clamp to bounds
        lo, hi = WEIGHT_BOUNDS.get(key, (1, 9999))
        proposed = max(lo, min(hi, proposed))

        if proposed == current:
            continue  # already at boundary, skip

        pct = round((proposed - current) / current * 100, 1)

        # Find which reasons drove this key
        from feedback.analyzer import REASON_WEIGHTS
        driving_reasons = []
        for reason, count in summary["reason_counts"].items():
            if reason in REASON_WEIGHTS:
                if any(w["key"] == key for w in REASON_WEIGHTS[reason]):
                    label = REASON_LABELS.get(reason, reason)
                    driving_reasons.append(f"{count} rejections: {label}")

        proposals.append({
            "key":        key,
            "old_value":  current,
            "new_value":  proposed,
            "pct_change": pct,
            "reason":     "; ".join(driving_reasons),
        })

    # Sort by largest absolute change first
    proposals.sort(key=lambda p: abs(p["pct_change"]), reverse=True)
    return proposals


# ── Discord message formatting ────────────────────────────────────────────────

def format_summary_message(draft_id: int, summary: dict) -> str:
    """Single embed description showing draft review results."""
    total    = summary["total"]
    approved = summary["approved"]
    rejected = summary["rejected"]
    pct      = round(approved / total * 100) if total else 0

    lines = [
        f"**Draft #{draft_id} Review Complete**",
        f"✅ Approved: **{approved}/{total}** ({pct}%)",
        f"❌ Rejected: **{rejected}/{total}**",
        "",
    ]

    if summary["reason_counts"]:
        lines.append("**Rejection Breakdown:**")
        for reason, count in sorted(summary["reason_counts"].items(), key=lambda x: -x[1]):
            label = REASON_LABELS.get(reason, reason)
            pct_r = round(count / rejected * 100) if rejected else 0
            lines.append(f"  • {label}: **{count}** teams ({pct_r}% of rejections)")
    else:
        lines.append("No rejection patterns detected.")

    return "\n".join(lines)


def format_proposals_message(proposals: list[dict], proposal_id: int) -> str:
    """Format the list of proposals for a Discord embed."""
    if not proposals:
        return "✅ No weight changes needed — all signals are within acceptable range."

    lines = [
        f"**💡 Proposed Weight Changes** (Proposal #{proposal_id})",
        "",
    ]
    for i, p in enumerate(proposals, 1):
        arrow = "↑" if p["new_value"] > p["old_value"] else "↓"
        lines.append(
            f"**#{i}** `{p['key']}`  {p['old_value']} → **{p['new_value']}** "
            f"({arrow}{abs(p['pct_change'])}%)"
        )
        lines.append(f"    _{p['reason']}_")
        lines.append("")

    lines += [
        "**Commands:**",
        "`!confirmweights` — apply all changes",
        "`!skipweights 1 3` — skip specific proposals (e.g. skip #1 and #3)",
        "`!setweight 2 55` — manually override a proposed value",
        "`!cancelweights` — discard all proposals",
    ]
    return "\n".join(lines)


# ── Applying confirmed proposals ──────────────────────────────────────────────

def apply_proposals(
    proposal_id: int,
    proposals: list[dict],
    skip_indices: list[int] | None = None,   # 1-based
    overrides: dict[int, float] | None = None,  # 1-based index → manual value
    draft_id: int | None = None,
) -> list[str]:
    """
    Write the confirmed proposals to weights.json.
    Returns list of human-readable lines describing what was changed.
    skip_indices: 1-based proposal numbers to skip entirely.
    overrides: 1-based proposal number → manually set value.
    """
    skip_indices = set(skip_indices or [])
    overrides = overrides or {}

    weights_data = _load_weights()
    applied: list[str] = []
    applied_keys: list[str] = []

    for i, p in enumerate(proposals, 1):
        if i in skip_indices:
            continue

        key      = p["key"]
        old_val  = p["old_value"]
        new_val  = overrides.get(i, p["new_value"])

        # Safety: clamp to bounds
        lo, hi = WEIGHT_BOUNDS.get(key, (1, 9999))
        new_val = max(lo, min(hi, float(new_val)))

        weights_data[key] = new_val
        fdb.log_weight_change(key, old_val, new_val, draft_id=draft_id,
                              note=p.get("reason", ""))
        applied.append(f"`{key}`: {old_val} → **{new_val}**")
        applied_keys.append(key)

    _save_weights(weights_data)
    fdb.confirm_proposal(proposal_id, applied_keys)

    # Tell ai_drafter to reload weights immediately
    import ai_drafter
    ai_drafter.reload_weights()

    return applied
