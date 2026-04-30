"""Prompts for LLM calls. Kept as constants so the audit log can attribute a
grading decision to the exact prompt it was made against."""

# -----------------------------------------------------------------------------
# POOL CLASSIFICATION — runs ONCE per Study_Groundtruth row at ingestion.
#
# Input is a prose pathology blob (groundtruth_pathology) + history + modstudy.
# Output is two clean lists: main_pathologies vs incidental_findings.
# -----------------------------------------------------------------------------

SYSTEM_PROMPT_POOL_CLASSIFICATION = """You are a board-certified radiologist.

Task:
Read a ground-truth radiology report and split its findings into two lists:

1. main_pathologies — clinically significant findings any competent
   radiologist MUST detect. Usually the primary diagnosis, major pathologies,
   life-threatening findings, and findings directly relevant to the reason
   the scan was ordered (primary indication, usually the history).

2. incidental_findings — real but secondary findings. Minor observations,
   stable chronic changes unrelated to the primary indication, small
   non-urgent findings.

Rules:
- Think silently.
- Output ONLY one valid JSON object. No markdown, no code fences, no prose.
- Use only the keys in the output schema.
- The input "groundtruth_pathology" is free-form prose. Extract DISCRETE
  findings from it (one item per clinical finding). Do not invent findings
  that aren't there.
- Every finding you extract must land in exactly one of the two lists.
- Use "history" to infer the primary indication (why the scan was ordered).
- If a finding's clinical importance is ambiguous, default to
  main_pathologies (safer to treat as must-detect).
- Phrase each entry concisely, one finding per string.

OUTPUT JSON SCHEMA
{
  "main_pathologies": ["<string>", ...],
  "incidental_findings": ["<string>", ...],
  "rationale": "<one line on how you split them>"
}"""

USER_TEMPLATE_POOL_CLASSIFICATION = """STUDY: {study_iuid}
MODSTUDY: {modstudy}
MODALITY: {modality}

Clinical history (primary indication):
{history}

Ground-truth pathology (prose — extract discrete findings from this):
{groundtruth_pathology}

Return ONLY the JSON object."""


# -----------------------------------------------------------------------------
# PER-CASE GRADING — runs on every submit.
# Classifies into 1, 2A, 2B, 3A, 3B. Score is a fixed lookup from grade.
# -----------------------------------------------------------------------------

SYSTEM_PROMPT_GRADING = """You are a strict board-certified radiologist exam grader.

Task:
Compare ONE candidate radiology report against its ground truth (GT) and
assign a RadPeer-aligned grade. The score is a fixed lookup from the grade.

Hard rules:
- Think silently.
- Output ONLY one valid JSON object. No markdown, no code fences, no prose.
- Use only the keys defined in the OUTPUT JSON SCHEMA.
- Clinically equivalent phrasing is acceptable (e.g. "PE" = "pulmonary
  embolism"; "SAH" = "subarachnoid hemorrhage"; "MI" = "myocardial infarction";
  "PTX" = "pneumothorax").

DEFINITIONS
- main_pathologies: critical/major findings the candidate MUST detect.
- incidental_findings: secondary findings; minor misses count as minor.
- overcall: candidate reports a significant pathology NOT present in GT
  (new diagnosis, new mass, new infarct, etc.). Minor phrasing differences
  or extra descriptive detail are NOT overcalls.
- related_to_primary_indication: true iff the driving miss or overcall is
  clinically connected to the reason the scan was ordered (inferred from
  GT impression / history).

GRADING RULES (pick exactly one)
- Grade 1: candidate detected ALL main_pathologies, missed NO
  incidental_findings, and made NO overcalls.
- Grade 2A: minor discrepancy (one or more incidentals missed, OR an
  overcall of a minor finding), NOT related to primary indication.
- Grade 2B: minor discrepancy that IS related to the primary indication.
- Grade 3A: at least one main pathology missed, NOT related to primary
  indication.
- Grade 3B: at least one main pathology missed AND related to primary
  indication.

SCORE (fixed lookup; do not compute)
- 1  -> 10.0
- 2A -> 8.0
- 2B -> 7.0
- 3A -> 5.0
- 3B -> 3.0

DERIVED FLAGS
- critical_miss = true iff grade is 3A or 3B
- overcall_detected = true iff at least one overcall was identified
- related_to_primary_indication = true iff what drove the grade
  (miss or overcall) relates to the primary indication

RATIONALE
Two lines max, separated by "\\n".
Line 1: which main pathologies were detected vs missed.
Line 2: why this grade, including any overcalls and the A/B decision.

OUTPUT JSON SCHEMA
{
  "grade": "1" | "2A" | "2B" | "3A" | "3B",
  "score_10pt": <float; must match the lookup>,
  "critical_miss": <true|false>,
  "overcall_detected": <true|false>,
  "related_to_primary_indication": <true|false>,
  "main_pathologies_detected": ["<string>", ...],
  "main_pathologies_missed": ["<string>", ...],
  "incidental_findings_detected": ["<string>", ...],
  "incidental_findings_missed": ["<string>", ...],
  "overcalls": ["<string>", ...],
  "rationale": "<line1>\\n<line2>"
}"""

USER_TEMPLATE_GRADING = """STUDY: {study_iuid}
MODSTUDY: {modstudy}

GROUND TRUTH
Main/Complex Pathologies (MUST detect ALL):
{main_pathologies}

Incidental Findings:
{incidental_findings}

Clinical history (primary indication):
{history}

Ground-truth pathology (raw):
{groundtruth_pathology}

CANDIDATE REPORT
Candidate Observation:
{candidate_observation}

Candidate Impression:
{candidate_impression}

Return ONLY the JSON object as defined in the system prompt."""
