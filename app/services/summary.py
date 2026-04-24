"""Rule-based 2-line summary for Slack + callback payloads.

Deterministic, no LLM. Swap for an LLM-generated version later if product
wants more natural phrasing.
"""

from __future__ import annotations

from app.services.grade_utils import count_grades


def build_summary(
    *,
    cases_evaluated: int,
    avg_score: float,
    overall_grade: str,
    grades: list[str],
    critical_miss_count: int,
    overcall_count: int,
) -> str:
    counts = count_grades(grades)
    clean = counts["1"]
    minor = counts["2A"] + counts["2B"]
    major = counts["3A"] + counts["3B"]

    line1 = (
        f"Avg {avg_score:.2f}/10, overall grade {overall_grade}. "
        f"{clean}/{cases_evaluated} clean, {minor} minor miss, {major} major miss."
    )
    extras = []
    if critical_miss_count:
        extras.append(f"{critical_miss_count} critical miss")
    if overcall_count:
        extras.append(f"{overcall_count} overcall")
    line2 = "; ".join(extras) if extras else "No critical misses or overcalls."

    return f"{line1}\n{line2}"
