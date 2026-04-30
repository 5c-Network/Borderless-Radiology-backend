"""Pure helpers for grade / score / aggregate math. No IO."""

from __future__ import annotations

GRADE_TO_SCORE: dict[str, float] = {
    "1": 10.0,
    "2A": 8.0,
    "2B": 7.0,
    "3A": 5.0,
    "3B": 3.0,
}


def score_from_grade(grade: str) -> float:
    return GRADE_TO_SCORE[grade]


def grade_from_avg_score(avg: float) -> str:
    """Map an averaged score back to an overall grade bucket.

    Boundaries match the product spec:
      9.0 – 10.0  -> 1
      7.5 – 8.9   -> 2A
      6.5 – 7.4   -> 2B
      4.0 – 6.4   -> 3A
      < 4.0       -> 3B
    """
    if avg >= 9.0:
        return "1"
    if avg >= 7.5:
        return "2A"
    if avg >= 6.5:
        return "2B"
    if avg >= 4.0:
        return "3A"
    return "3B"


def quality_met(overall_grade: str) -> bool:
    """Quality bar = overall grade 1 (i.e. avg >= 9.0)."""
    return overall_grade == "1"


def count_grades(grades: list[str]) -> dict[str, int]:
    counts = {"1": 0, "2A": 0, "2B": 0, "3A": 0, "3B": 0}
    for g in grades:
        if g in counts:
            counts[g] += 1
    return counts


def count_modalities(modalities: list[str | None]) -> dict[str, int]:
    """Tally modality strings. Sorted by count desc, alphabetical on ties."""
    counts: dict[str, int] = {}
    for m in modalities:
        if not m:
            continue
        counts[m] = counts.get(m, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def format_modality_breakdown(counts: dict[str, int]) -> str:
    """Render '{CT: 12, MRI: 6, XRAY: 2}' for notice bodies."""
    if not counts:
        return ""
    return ", ".join(f"{m}: {n}" for m, n in counts.items())
