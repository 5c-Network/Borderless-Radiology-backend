"""Event-driven incubation webhook.

Single endpoint that n8n calls for two UI events:

  POST /api/v1/incubation/webhook
  Authorization: <api_auth_key>
  Content-Type: application/json

  Body:
      { "event": "start-reporting" | "case-submitted",
        "rad_id": <int>,
        "modalities": ["CT","MRI","XRAY","NM"] }

Response: IncubationWebhookResponse envelope with the picked cases in `items`.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas import IncubationWebhookRequest, IncubationWebhookResponse, WebhookEvent
from app.security import require_api_key
from app.services.incubation_webhook import handle_webhook
from app.services.rad_commitment import populate_other_details

router = APIRouter(
    prefix="/api/v1/incubation",
    tags=["incubation-webhook"],
    dependencies=[Depends(require_api_key)],
)


@router.post("/webhook", response_model=IncubationWebhookResponse)
async def incubation_webhook(
    payload: IncubationWebhookRequest,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> IncubationWebhookResponse:
    result = await handle_webhook(session, payload)
    await session.commit()
    # T1 hook (rad_slot_status feature): on every start-reporting event,
    # fetch other_details from ClickHouse and populate rad_state. Idempotent
    # — populate_other_details no-ops if the column is already set, so
    # repeated webhook fires don't cause work. Fire-and-forget; the
    # function never raises and runs its own DB session.
    if payload.event == WebhookEvent.start_reporting:
        background.add_task(populate_other_details, str(payload.rad_id))
    return result
