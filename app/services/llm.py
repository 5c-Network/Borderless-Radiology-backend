"""Gemini client wrapper. Forces JSON output, retries transient failures, and
returns validated Pydantic objects."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from google import genai
from google.genai import types
from pydantic import ValidationError

from app.config import get_settings
from app.prompts import (
    SYSTEM_PROMPT_GRADING,
    SYSTEM_PROMPT_POOL_CLASSIFICATION,
    USER_TEMPLATE_GRADING,
    USER_TEMPLATE_POOL_CLASSIFICATION,
)
from app.schemas import GradingLLMOutput, PoolClassificationOutput
from app.services.grade_utils import score_from_grade

logger = logging.getLogger(__name__)


class LLMError(Exception):
    pass


class GeminiService:
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.gemini_api_key:
            raise LLMError("GEMINI_API_KEY is not set")
        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._model = settings.gemini_model

    async def classify_pool_case(
        self,
        *,
        study_iuid: str,
        modstudy: str,
        modality: str | None,
        history: str | None,
        groundtruth_pathology: str,
    ) -> PoolClassificationOutput:
        user = USER_TEMPLATE_POOL_CLASSIFICATION.format(
            study_iuid=study_iuid,
            modstudy=modstudy or "(none)",
            modality=modality or "(none)",
            history=history or "(none)",
            groundtruth_pathology=groundtruth_pathology or "(none)",
        )
        raw = await self._generate_json(
            system=SYSTEM_PROMPT_POOL_CLASSIFICATION,
            user=user,
        )
        try:
            return PoolClassificationOutput.model_validate(raw)
        except ValidationError as e:
            raise LLMError(f"pool classification schema mismatch: {e}") from e

    async def grade_case(
        self,
        *,
        study_iuid: str,
        main_pathologies: list[str],
        incidental_findings: list[str],
        history: str | None,
        groundtruth_pathology: str,
        candidate_observation: str,
        candidate_impression: str,
    ) -> GradingLLMOutput:
        user = USER_TEMPLATE_GRADING.format(
            study_iuid=study_iuid,
            main_pathologies=(
                "\n".join(f"- {p}" for p in main_pathologies)
                if main_pathologies
                else "(none)"
            ),
            incidental_findings=(
                "\n".join(f"- {p}" for p in incidental_findings)
                if incidental_findings
                else "(none)"
            ),
            history=history or "(none)",
            groundtruth_pathology=groundtruth_pathology or "(none)",
            candidate_observation=candidate_observation or "(none)",
            candidate_impression=candidate_impression or "(none)",
        )
        raw = await self._generate_json(
            system=SYSTEM_PROMPT_GRADING,
            user=user,
        )
        try:
            out = GradingLLMOutput.model_validate(raw)
        except ValidationError as e:
            raise LLMError(f"grading schema mismatch: {e}") from e

        # Enforce the fixed score lookup regardless of what the LLM said.
        out.score_10pt = score_from_grade(out.grade)
        out.critical_miss = out.grade in ("3A", "3B")
        return out

    async def _generate_json(self, system: str, user: str) -> dict[str, Any]:
        cfg = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.0,
            response_mime_type="application/json",
        )

        last_err: Exception | None = None
        for attempt in range(3):
            try:
                resp = await asyncio.to_thread(
                    self._client.models.generate_content,
                    model=self._model,
                    contents=user,
                    config=cfg,
                )
                text = (resp.text or "").strip()
                if not text:
                    raise LLMError("empty response")
                return json.loads(text)
            except json.JSONDecodeError as e:
                last_err = LLMError(f"invalid JSON from model: {e}")
            except Exception as e:  # noqa: BLE001
                last_err = e
            await asyncio.sleep(0.5 * (2**attempt))

        raise LLMError(f"LLM call failed after retries: {last_err}") from last_err


_service: GeminiService | None = None


def get_llm() -> GeminiService:
    global _service
    if _service is None:
        _service = GeminiService()
    return _service
