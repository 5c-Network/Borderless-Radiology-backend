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

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas import IncubationWebhookRequest, IncubationWebhookResponse
from app.security import require_api_key
from app.services.incubation_webhook import handle_webhook

router = APIRouter(
    prefix="/api/v1/incubation",
    tags=["incubation-webhook"],
    dependencies=[Depends(require_api_key)],
)


@router.post("/webhook", response_model=IncubationWebhookResponse)
async def incubation_webhook(
    payload: IncubationWebhookRequest,
    session: AsyncSession = Depends(get_session),
) -> IncubationWebhookResponse:
    result = await handle_webhook(session, payload)
    await session.commit()
    return result
