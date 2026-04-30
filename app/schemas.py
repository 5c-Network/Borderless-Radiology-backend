from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# ---------- Study_Groundtruth ingestion ----------


class StudyGroundtruthIngest(BaseModel):
    study_id: int
    study_iuid: str
    modstudy: str
    groundtruth_pathology: str
    modality: str | None = None
    history: str | None = None
    dicom_metadata: str | None = None  # stored as text; JSON string expected
    rules: str | None = None  # stored as text; JSON string expected
    is_complex: bool = False


class StudyGroundtruthOut(BaseModel):
    study_id: int
    study_iuid: str
    modstudy: str
    modality: str | None
    is_complex: bool
    main_pathologies: list[str]
    incidental_findings: list[str]
    classified: bool


# ---------- Activation-data endpoint ----------


class RulesEntry(BaseModel):
    """Shape matches the QA example. All fields passthrough; we store as text
    and serve after JSON.parse, so unknown/extra keys are allowed."""

    model_config = {"extra": "allow", "populate_by_name": True}

    id: int | None = None
    list_: list[Any] | None = Field(default=None, alias="list")
    hidden: bool | None = None
    keywords: list[str] | None = None
    mandatory: list[dict[str, Any]] | None = None
    mod_study: int | str | None = None
    sub_speciality_id: int | str | None = None


class DicomData(BaseModel):
    model_config = {"extra": "allow"}

    created_time: str | None = None
    study_date: str | None = None
    study_iuid: str | None = None
    pat_sex: str | None = None
    pat_birthdate: str | None = None
    pat_id: str | None = None
    mods_in_study: str | None = None
    num_instances: str | None = None
    num_series: str | None = None
    pat_name_fk: str | None = None
    accession_number: str | None = None
    study_time: str | None = None

    @field_validator("mods_in_study", mode="before")
    @classmethod
    def _coerce_mods_in_study(cls, v: object) -> str | None:
        """Accept str ("CT" / "CT,MRI") or list (["CT","SR"]) from upstream
        DICOM metadata. Output: comma-joined uppercase string of ONLY
        allowed viewing modalities (CT/MRI/XRAY/NM). Anything else —
        including 'SR' (Structured Report, not a viewing modality) — is
        dropped. Returns None if nothing survives the filter.
        """
        if v is None:
            return None
        if isinstance(v, str):
            tokens = [t.strip().upper() for t in v.split(",") if t.strip()]
        elif isinstance(v, list):
            tokens = [str(t).strip().upper() for t in v if str(t).strip()]
        else:
            return v  # let pydantic surface the wrong-type error
        kept = [t for t in tokens if t in ALLOWED_MODALITIES]
        return ",".join(kept) if kept else None


class ActivationDataItem(BaseModel):
    history: str = ""
    rules: list[RulesEntry] | list[dict[str, Any]] = Field(default_factory=list)
    dicomData: DicomData
    for_candidate: bool = True


# ---------- Grading ----------


class Report(BaseModel):
    observation: str = ""
    impression: str = ""
    history: str = ""
    modstudy: str = ""
    study_iuid: str


class GradeCaseRequest(BaseModel):
    rad_id: str
    report: Report


class GradeCaseResponse(BaseModel):
    grading_id: str
    status: Literal["queued", "running", "done", "error"]


class GradeResult(BaseModel):
    grading_id: str
    rad_id: str
    study_iuid: str
    case_number: int
    status: str
    grade: str | None
    score_10pt: float | None
    critical_miss: bool | None
    overcall_detected: bool | None
    related_to_primary_indication: bool | None
    rationale: str | None
    graded_at: datetime | None


# ---------- LLM structured I/O ----------


Grade = Literal["1", "2A", "2B", "3A", "3B"]


class PoolClassificationOutput(BaseModel):
    """LLM output when we pre-classify the prose pathology blob at ingestion."""

    main_pathologies: list[str]
    incidental_findings: list[str]
    rationale: str = ""


class GradingLLMOutput(BaseModel):
    """LLM output for a single graded case."""

    grade: Grade
    score_10pt: float
    critical_miss: bool
    overcall_detected: bool
    related_to_primary_indication: bool
    main_pathologies_detected: list[str] = Field(default_factory=list)
    main_pathologies_missed: list[str] = Field(default_factory=list)
    incidental_findings_detected: list[str] = Field(default_factory=list)
    incidental_findings_missed: list[str] = Field(default_factory=list)
    overcalls: list[str] = Field(default_factory=list)
    rationale: str = ""


# ---------- Checkpoint / callback ----------


class CheckpointPerCase(BaseModel):
    case_number: int
    study_iuid: str
    grade: str
    score_10pt: float
    critical_miss: bool


class CheckpointPayload(BaseModel):
    rad_id: str
    kind: Literal["gate_20", "terminal_80", "terminal_7_days"]
    cases_evaluated: int
    avg_score: float
    overall_grade: str
    quality_met: bool
    grade_counts: dict[str, int]
    summary: str
    per_case: list[CheckpointPerCase]
    evaluated_at: datetime


# ---------- Incubation webhook (event-driven activation) ----------


ALLOWED_MODALITIES = ("CT", "MRI", "XRAY", "NM")


class WebhookEvent(str, enum.Enum):
    start_reporting = "start-reporting"
    case_submitted = "case-submitted"


class IncubationWebhookRequest(BaseModel):
    event: WebhookEvent
    rad_id: int = Field(ge=1, description="Numeric radiologist id from upstream")
    modalities: list[str] = Field(min_length=1)

    @field_validator("modalities")
    @classmethod
    def _normalize_modalities(cls, v: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in v:
            if not isinstance(raw, str):
                raise ValueError("modalities must be strings")
            token = raw.strip().upper()
            if not token:
                continue
            if token not in ALLOWED_MODALITIES:
                raise ValueError(
                    f"modality {raw!r} not allowed; expected one of {ALLOWED_MODALITIES}"
                )
            if token not in cleaned:
                cleaned.append(token)
        if not cleaned:
            raise ValueError("modalities must not be empty after normalization")
        cleaned.sort()
        return cleaned


class IncubationWebhookResponse(BaseModel):
    rad_id: str
    event: WebhookEvent
    rad_status: str
    cases_completed: int
    cases_assigned_now: int
    items: list[ActivationDataItem]
    message: str | None = None
