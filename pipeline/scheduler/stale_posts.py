"""Detect stale posts and alert (Stage 7, W5 partial mitigation).

Two cases trigger an alert into the monitoring TG channel:

* status=pending_approval, age > 24h → "auto-approve in N hours"
* status=scheduled, age > 48h → "candidate for drop"

The W5 auto-publish-after-48h policy is implemented by the approval bot,
not here — this script is read-only and only notifies.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from ..common.config import get_settings
from ..common.logging import configure_logging, get_logger
from ..publisher.directus import DirectusClient

log = get_logger(__name__)


async def main() -> None:
    configure_logging()
    settings = get_settings()
    directus = DirectusClient()

    now = datetime.now(tz=timezone.utc)
    pending_cutoff = (now - timedelta(hours=24)).isoformat()
    scheduled_cutoff = (now - timedelta(hours=48)).isoformat()

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Pending > 24h
        r1 = await client.get(
            f"{directus.base_url}/items/posts",
            headers=directus._headers(),
            params={
                "filter[status][_eq]": "pending_approval",
                "filter[created_at][_lt]": pending_cutoff,
                "limit": 100,
            },
        )
        r1.raise_for_status()
        stale_pending = r1.json().get("data", [])

        # Scheduled > 48h
        r2 = await client.get(
            f"{directus.base_url}/items/posts",
            headers=directus._headers(),
            params={
                "filter[status][_eq]": "scheduled",
                "filter[scheduled_at][_lt]": scheduled_cutoff,
                "limit": 100,
            },
        )
        r2.raise_for_status()
        stale_scheduled = r2.json().get("data", [])

    if not (stale_pending or stale_scheduled):
        log.info("stale.none")
        return

    if not settings.telegram_monitoring_chat_id or not settings.telegram_bot_token:
        log.warning("stale.no_monitoring_chat", n_pending=len(stale_pending), n_sched=len(stale_scheduled))
        return

    msg = "<b>Stale posts</b>\n"
    if stale_pending:
        msg += f"\nPending &gt; 24h: <b>{len(stale_pending)}</b>"
    if stale_scheduled:
        msg += f"\nScheduled &gt; 48h: <b>{len(stale_scheduled)}</b>"

    async with httpx.AsyncClient(timeout=30.0) as client:
        await client.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={
                "chat_id": settings.telegram_monitoring_chat_id,
                "text": msg,
                "parse_mode": "HTML",
            },
        )
    log.info("stale.alerted", pending=len(stale_pending), scheduled=len(stale_scheduled))


if __name__ == "__main__":
    asyncio.run(main())
