"""SQLite-backed publish queue.

State machine for a queue entry:
::

    pending --(dequeue)--> in_flight --(success)--> published
                                   \\--(error)----> failed (attempts < 5: retry; else terminal)

We keep the queue in SQLite (file path from settings) rather than Postgres
because:
* it's per-worker state — no cross-instance contention concerns yet
* zero ops; backup is a cp of one file
* easy to inspect with ``sqlite3 pipeline.db``

When the worker grows to 2+ instances we'll migrate this table to Postgres.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from ..common.logging import get_logger

log = get_logger(__name__)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS publish_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id         TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    scheduled_at    TIMESTAMP NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMP,
    status          TEXT NOT NULL DEFAULT 'pending',
    last_error      TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS publish_queue_status_sched
    ON publish_queue(status, scheduled_at);
"""

_MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class QueueEntry:
    id: int
    post_id: str
    channel_id: str
    scheduled_at: datetime
    attempts: int
    status: str


class PublishQueue:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    async def enqueue(
        self, post_id: str, channel_id: str, scheduled_at: datetime | None = None
    ) -> int:
        sched = scheduled_at or datetime.now(tz=timezone.utc)
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "INSERT INTO publish_queue (post_id, channel_id, scheduled_at) "
                "VALUES (?, ?, ?)",
                (post_id, channel_id, sched.isoformat()),
            )
            await db.commit()
            log.info("queue.enqueued", post=post_id, channel=channel_id, queue_id=cur.lastrowid)
            return int(cur.lastrowid or 0)

    async def dequeue_ready(self, now: datetime | None = None) -> list[QueueEntry]:
        n = (now or datetime.now(tz=timezone.utc)).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, post_id, channel_id, scheduled_at, attempts, status "
                "FROM publish_queue "
                "WHERE status='pending' AND scheduled_at <= ? "
                "ORDER BY scheduled_at ASC LIMIT 100",
                (n,),
            ) as cur:
                rows = await cur.fetchall()
        return [
            QueueEntry(
                id=r["id"],
                post_id=r["post_id"],
                channel_id=r["channel_id"],
                scheduled_at=datetime.fromisoformat(r["scheduled_at"]),
                attempts=r["attempts"],
                status=r["status"],
            )
            for r in rows
        ]

    async def mark_published(self, entry_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE publish_queue SET status='published', "
                "last_attempt_at=CURRENT_TIMESTAMP WHERE id=?",
                (entry_id,),
            )
            await db.commit()

    async def mark_failed(self, entry_id: int, error: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT attempts FROM publish_queue WHERE id=?", (entry_id,)
            ) as cur:
                row = await cur.fetchone()
            attempts = (row[0] if row else 0) + 1
            new_status = "failed" if attempts >= _MAX_ATTEMPTS else "pending"
            await db.execute(
                "UPDATE publish_queue SET status=?, attempts=?, "
                "last_attempt_at=CURRENT_TIMESTAMP, last_error=? WHERE id=?",
                (new_status, attempts, error[:500], entry_id),
            )
            await db.commit()
            log.warning(
                "queue.failed",
                queue_id=entry_id,
                attempts=attempts,
                terminal=(new_status == "failed"),
            )
