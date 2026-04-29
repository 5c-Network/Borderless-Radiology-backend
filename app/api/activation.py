"""Activation-data endpoints.

    GET /api/v1/activation-data/
        ?rad_id=<id>
        &study_iuids=<uid1,uid2>           (optional — direct lookup mode)
        &event=start-reporting|case-submitted   (optional — informational)
        &modalities=CT,MRI                 (optional — stored as modality_preferred
                                            on first call; restricts the pick)
    Authorization: <api_auth_key>

    GET /api/v1/activation-data/test/
        Same query params. Random-pick mode returns ONLY rows whose
        Study_Groundtruth.case_type == 'test' (DICOMs already pre-loaded
        on the destination server, no yotta hop). The default endpoint
        excludes those rows. UID-lookup mode is unfiltered on both routes.

The `modalities` and `study_iuids` params accept three serializations
(handy because n8n's HTTP node serializes JS arrays differently across
versions):

    1. CSV in one key:        modalities=CT,MRI
    2. Repeated key:          modalities=CT&modalities=MRI
    3. Bracket-encoded:       modalities[0]=CT&modalities[1]=MRI

All three resolve to the same list internally.

Response: JSON array of {history, rules, dicomData, for_candidate}.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas import ActivationDataItem
from app.security import require_api_key
from app.services.activation_service import get_activation_data

router = APIRouter(prefix="/api/v1", tags=["activation"], dependencies=[Depends(require_api_key)])


def _extract_multi(request: Request, name: str, *, upper: bool = False) -> list[str] | None:
    """Pull a multi-value query param accepting CSV, repeated-key, or
    bracket-encoded forms (see module docstring). Returns a deduped list
    in first-seen order, or None if no value was supplied.
    """
    bracket_prefix = f"{name}["
    tokens: list[str] = []
    for key, raw in request.query_params.multi_items():
        if key == name or (key.startswith(bracket_prefix) and key.endswith("]")):
            for piece in raw.split(","):
                t = piece.strip()
                if not t:
                    continue
                tokens.append(t.upper() if upper else t)
    if not tokens:
        return None
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


@router.get(
    "/activation-data/",
    response_model=list[ActivationDataItem],
)
async def activation_data(
    request: Request,
    rad_id: str = Query(..., description="Radiologist identifier"),
    study_iuids: str | None = Query(
        default=None,
        description=(
            "Comma-separated list of study_iuids (also accepts "
            "study_iuids=<x>&study_iuids=<y> or study_iuids[0]=<x>...). "
            "If omitted, we pick randomly from unused pool cases."
        ),
    ),
    event: str | None = Query(
        default=None,
        description="start-reporting | case-submitted (informational; pick logic still derives first-vs-subsequent from assignments).",
    ),
    modalities: str | None = Query(
        default=None,
        description=(
            "Modality tokens. Accepts CSV ('CT,MRI'), repeated-key "
            "(modalities=CT&modalities=MRI), or bracket-encoded "
            "(modalities[0]=CT&modalities[1]=MRI). Stored as "
            "modality_preferred on the rad's first call."
        ),
    ),
    session: AsyncSession = Depends(get_session),
) -> list[ActivationDataItem]:
    result = await get_activation_data(
        session,
        rad_id=rad_id,
        study_iuids=_extract_multi(request, "study_iuids"),
        event=event,
        modalities=_extract_multi(request, "modalities", upper=True),
        case_type_filter=None,
    )
    await session.commit()
    return result.items


@router.get(
    "/activation-data/test/",
    response_model=list[ActivationDataItem],
)
async def activation_data_test(
    request: Request,
    rad_id: str = Query(..., description="Radiologist identifier"),
    study_iuids: str | None = Query(default=None),
    event: str | None = Query(default=None),
    modalities: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> list[ActivationDataItem]:
    result = await get_activation_data(
        session,
        rad_id=rad_id,
        study_iuids=_extract_multi(request, "study_iuids"),
        event=event,
        modalities=_extract_multi(request, "modalities", upper=True),
        case_type_filter="test",
    )
    await session.commit()
    return result.items
