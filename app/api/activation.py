"""Activation-data endpoints.

    GET /api/v1/activation-data/
        ?rad_id=<id>
        &study_iuids=<uid1,uid2>           (optional — direct lookup mode)
        &event=start-reporting|case-submitted   (optional — informational)
        &modalities=CT,MRI                 (optional — stored as modality_preferred
                                            on first call; restricts the pick)
    Authorization: <api_auth_key>

    GET /api/v1/activation-data/test
        Same query params. Random-pick mode returns ONLY rows whose
        Study_Groundtruth.case_type == 'test' (DICOMs already pre-loaded
        on the destination server, no yotta hop). The default endpoint
        excludes those rows. UID-lookup mode is unfiltered on both routes.

Response: JSON array of {history, rules, dicomData, for_candidate}.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas import ActivationDataItem
from app.security import require_api_key
from app.services.activation_service import get_activation_data

router = APIRouter(prefix="/api/v1", tags=["activation"], dependencies=[Depends(require_api_key)])


def _parse_csv(raw: str | None, *, upper: bool = False) -> list[str] | None:
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return None
    return [p.upper() for p in parts] if upper else parts


@router.get(
    "/activation-data/",
    response_model=list[ActivationDataItem],
)
async def activation_data(
    rad_id: str = Query(..., description="Radiologist identifier"),
    study_iuids: str | None = Query(
        default=None,
        description="Comma-separated list of study_iuids. If omitted, we pick randomly from unused pool cases.",
    ),
    event: str | None = Query(
        default=None,
        description="start-reporting | case-submitted (informational; pick logic still derives first-vs-subsequent from assignments).",
    ),
    modalities: str | None = Query(
        default=None,
        description="Comma-separated modality tokens (e.g. 'CT,MRI'). Stored as modality_preferred on the rad's first call; subsequent calls reuse the stored value.",
    ),
    session: AsyncSession = Depends(get_session),
) -> list[ActivationDataItem]:
    result = await get_activation_data(
        session,
        rad_id=rad_id,
        study_iuids=_parse_csv(study_iuids),
        event=event,
        modalities=_parse_csv(modalities, upper=True),
        case_type_filter=None,
    )
    await session.commit()
    return result.items


@router.get(
    "/activation-data/test",
    response_model=list[ActivationDataItem],
)
async def activation_data_test(
    rad_id: str = Query(..., description="Radiologist identifier"),
    study_iuids: str | None = Query(default=None),
    event: str | None = Query(default=None),
    modalities: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> list[ActivationDataItem]:
    result = await get_activation_data(
        session,
        rad_id=rad_id,
        study_iuids=_parse_csv(study_iuids),
        event=event,
        modalities=_parse_csv(modalities, upper=True),
        case_type_filter="test",
    )
    await session.commit()
    return result.items
