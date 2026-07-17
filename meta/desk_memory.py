"""meta/desk_memory.py — Cross-agent desk knowledge digest.

M11.1: Summarises validated and falsified hypotheses into a structured
text digest that the Head of Desk (chat) and the overview page can
consume.  The digest is the desk's institutional memory — what the
ensemble has learned so far.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def get_desk_digest(conn, max_items: int = 20) -> str:
    """Return a structured text digest of desk knowledge.

    Queries the hypotheses table for:
      - Top validated hypotheses (ordered by effect_observed desc, limit max_items/2)
      - All falsified hypotheses (limit max_items/2)

    Each entry includes: agent_id, claim, feature, direction,
    regime_context, effect_size, sample_size (via effect_observed sign
    convention: positive = validated, negative = falsified).

    Returns a human-readable string suitable for prompt injection or
    template rendering.
    """
    half = max(1, max_items // 2)

    validated = conn.execute(
        """SELECT agent_id, claim, feature, direction, regime_context,
                  effect_observed
           FROM hypotheses
           WHERE status = 'validated'
             AND effect_observed IS NOT NULL
           ORDER BY effect_observed DESC
           LIMIT ?""",
        (half,),
    ).fetchall()

    falsified = conn.execute(
        """SELECT agent_id, claim, feature, direction, regime_context,
                  effect_observed
           FROM hypotheses
           WHERE status = 'falsified'
             AND effect_observed IS NOT NULL
           ORDER BY effect_observed ASC
           LIMIT ?""",
        (half,),
    ).fetchall()

    lines: list[str] = []
    lines.append("DESK KNOWLEDGE DIGEST")
    lines.append("=" * 40)

    # --- validated ---
    lines.append("")
    lines.append(f"VALIDATED HYPOTHESES ({len(validated)}):")
    lines.append("-" * 40)
    if validated:
        for row in validated:
            agent_id = row["agent_id"]
            claim = row["claim"] or "(no claim)"
            feature = row["feature"] or "—"
            direction = row["direction"] or "—"
            regime = row["regime_context"] or "any"
            effect = row["effect_observed"]
            effect_str = f"{effect:+.4f}" if effect is not None else "—"
            lines.append(
                f"  [{agent_id}] {claim}"
            )
            lines.append(
                f"    feature={feature}  dir={direction}  regime={regime}  effect={effect_str}"
            )
    else:
        lines.append("  (none)")

    # --- falsified ---
    lines.append("")
    lines.append(f"FALSIFIED HYPOTHESES ({len(falsified)}):")
    lines.append("-" * 40)
    if falsified:
        for row in falsified:
            agent_id = row["agent_id"]
            claim = row["claim"] or "(no claim)"
            feature = row["feature"] or "—"
            direction = row["direction"] or "—"
            regime = row["regime_context"] or "any"
            effect = row["effect_observed"]
            effect_str = f"{effect:+.4f}" if effect is not None else "—"
            lines.append(
                f"  [{agent_id}] {claim}"
            )
            lines.append(
                f"    feature={feature}  dir={direction}  regime={regime}  effect={effect_str}"
            )
    else:
        lines.append("  (none)")

    lines.append("")
    total = conn.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0]
    lines.append(f"Total hypotheses tracked: {total}")

    return "\n".join(lines)
