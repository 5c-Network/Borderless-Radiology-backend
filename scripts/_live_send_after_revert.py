"""Live sample send via the reverted (webhook-only) send_slack_alert."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from app.config import get_settings  # noqa: E402
from app.models import CheckpointKind  # noqa: E402
from app.services import slack as slack_mod  # noqa: E402


async def main() -> None:
    settings = get_settings()
    print("config:", "webhook=" + ("set" if settings.slack_webhook_url else "EMPTY"))

    body = slack_mod.build_slack_text(
        kind=CheckpointKind.gate_20,
        rad_id="2107",
        cases_evaluated=20,
        avg_score=6.35,
        overall_grade="3A",
        quality_met=False,
        summary=(
            "Avg 6.35/10, overall grade 3A. 7/20 clean, 4 minor miss, "
            "9 major miss. 9 critical miss; 3 overcall"
        ),
    )
    text = "[TEST — please ignore] " + body

    print("\n--- body to post ---")
    print(text)
    print("--------------------\n")

    ok, err = await slack_mod.send_slack_alert(text)
    print(f"result: ok={ok} err={err!r}")


if __name__ == "__main__":
    asyncio.run(main())
