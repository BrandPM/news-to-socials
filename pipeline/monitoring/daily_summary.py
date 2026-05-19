"""Daily TG summary: yesterday's published/rejected counts per brand & channel.

Cron: 09:00 Europe/Madrid. Read-only against Directus.
"""

from __future__ import annotations

import asyncio
from collections import Counter
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
    since = (now - timedelta(hours=24)).isoformat()

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{directus.base_url}/items/posts",
            headers=directus._headers(),
            params={
                "filter[updated_at][_gte]": since,
                "fields": "status,brand_id,channel",
                "limit": 1000,
            },
        )
        resp.raise_for_status()
        rows = resp.json().get("data", [])

    by_status: Counter[str] = Counter()
    by_brand_channel: Counter[tuple[str, str]] = Counter()
    for row in rows:
        by_status[row.get("status", "?")] += 1
        if row.get("status") == "published":
            by_brand_channel[(row.get("brand_id", "?"), row.get("channel", "?"))] += 1

    lines = ["<b>Daily summary (last 24h)</b>"]
    for status in ("published", "pending_approval", "rejected", "failed"):
        if by_status.get(status):
            lines.append(f"  {status}: <b>{by_status[status]}</b>")
    if by_brand_channel:
        lines.append("\n<b>Published by brand × channel:</b>")
        for (brand, channel), count in sorted(by_brand_channel.items()):
            lines.append(f"  {brand}/{channel}: {count}")

    msg = "\n".join(lines)
    log.info("daily.summary", **dict(by_status))

    if settings.telegram_monitoring_chat_id and settings.telegram_bot_token:
        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                json={
                    "chat_id": settings.telegram_monitoring_chat_id,
                    "text": msg,
                    "parse_mode": "HTML",
                },
            )


if __name__ == "__main__":
    asyncio.run(main())
