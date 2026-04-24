"""Inbound auth. n8n sends a bare token in the Authorization header:
    Authorization: <api_auth_key>
We compare against API_AUTH_KEY in the environment.
"""

from __future__ import annotations

from fastapi import Header, HTTPException, status

from app.config import get_settings


async def require_api_key(authorization: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not settings.api_auth_key:
        # Dev mode: auth disabled when key not configured.
        return

    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Authorization header",
        )

    token = authorization.strip()
    # Accept either bare token or "Bearer <token>"
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    if token != settings.api_auth_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid API key",
        )
