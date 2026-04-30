"""Notice copy + decision logic for the platform notification API.

Owns all rad-facing strings sent to the platform's borderless notice slot.
Pure functions; no IO. Wording is second-person (addresses the rad as "you")
because the platform renders title + body verbatim in the rad's app.
"""

from __future__ import annotations

from app.models import CheckpointKind

# Titles — what the rad sees as the modal heading.
_GATE_BLOCK_TITLE = "Quality below threshold"
_TERMINAL_ELIGIBLE_TITLE = "Welcome to Borderless"
_TERMINAL_BLOCK_TITLE = "Quality below threshold"

# Body templates — interpolated with case range, modality breakdown, grade,
# avg_score. Rendered verbatim in the rad's app.
_GATE_BLOCK_BODY = (
    "Your quality score on the first 20 cases is below the bar required to "
    "continue. A team member from Borderless will reach out shortly."
)
_TERMINAL_ELIGIBLE_BODY = (
    "You completed cases {case_min} to {case_max} ({modality_breakdown}) "
    "with an overall grade of 1 ({avg_score}/10). You have cleared the "
    "assessment. Your Borderless access is now live, and cases from our "
    "partner hospitals will arrive in your queue during your committed slots."
)
_TERMINAL_BLOCK_BODY = (
    "Your quality score on the last 20 cases is below the bar required to "
    "continue. A team member from Borderless will reach out shortly."
)


def build_notice(
    *,
    kind: CheckpointKind,
    overall_grade: str,
    cases_evaluated: int,
    case_min: int,
    case_max: int,
    avg_score: float,
    modality_breakdown: str,
) -> dict | None:
    """Resolve checkpoint outcome to the notice dict the platform expects.

    Returns:
        - None when no PATCH should fire (gate_20 with quality met).
        - {"kind": "INFO"|"BLOCK", "title": str, "body": str} otherwise.

    The terminal-eligible notice is paired upstream with the phase-flip
    fields (phase, qualified_at) before being sent to endpoint B.
    """
    quality_met = overall_grade == "1"
    avg_str = f"{avg_score:.1f}"
    common = {
        "case_min": case_min,
        "case_max": case_max,
        "cases_evaluated": cases_evaluated,
        "modality_breakdown": modality_breakdown,
        "grade": overall_grade,
        "avg_score": avg_str,
    }

    if kind == CheckpointKind.gate_20:
        if quality_met:
            return None
        return {
            "kind": "BLOCK",
            "title": _GATE_BLOCK_TITLE,
            "body": _GATE_BLOCK_BODY.format(**common),
        }

    # Terminal: case-80 or 7-day timeout.
    if quality_met:
        return {
            "kind": "INFO",
            "title": _TERMINAL_ELIGIBLE_TITLE,
            "body": _TERMINAL_ELIGIBLE_BODY.format(**common),
        }
    return {
        "kind": "BLOCK",
        "title": _TERMINAL_BLOCK_TITLE,
        "body": _TERMINAL_BLOCK_BODY.format(**common),
    }
