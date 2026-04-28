"""Activation-data endpoint.

    GET /api/v1/activation-data/
        ?rad_id=<id>
        &study_iuids=<uid1,uid2>           (optional — direct lookup mode)
        &event=start-reporting|case-submitted   (optional — informational)
        &modalities=CT,MRI                 (optional — stored as modality_preferred
                                            on first call; restricts the pick)
    Authorization: <api_auth_key>

Response: JSON array of {history, rules, dicomData}.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas import ActivationDataItem
from app.security import require_api_key
from app.services.activation_service import get_activation_data

router = APIRouter(prefix="/api/v1", tags=["activation"], dependencies=[Depends(require_api_key)])


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
    uid_list: list[str] | None = None
    if study_iuids:
        uid_list = [u.strip() for u in study_iuids.split(",") if u.strip()]
        if not uid_list:
            uid_list = None

    modality_list: list[str] | None = None
    if modalities:
        modality_list = [m.strip().upper() for m in modalities.split(",") if m.strip()]
        if not modality_list:
            modality_list = None

    result = await get_activation_data(
        session,
        rad_id=rad_id,
        study_iuids=uid_list,
        event=event,
        modalities=modality_list,
    )
    await session.commit()
    return result.items
