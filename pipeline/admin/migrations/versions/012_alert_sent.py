"""alert_sent — dedup ledger for the Telegram push-alerter.

Revision ID: 012_alert_sent
Revises: 011_resync_nts070
Create Date: 2026-06-23

IT_PROJ_NTS_073 — push-alerts to the monitoring chat. The alerter computes
the same notifications as the ``/api/v1/notifications`` route, then pushes the
danger/source-unhealthy ones to Telegram. This table is how it avoids
re-sending the same alert every 15 minutes:

* ``notification_id``  the synthetic id (``run-47``, ``source-3``) — PK.
* ``sent_at``          when it was first pushed (UTC).

Only ids absent from this table are sent; after a successful send the id is
recorded. When a notification clears the row is removed so a recurrence
re-alerts. ``downgrade`` drops the table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "012_alert_sent"
down_revision: str | None = "011_resync_nts070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alert_sent",
        sa.Column("notification_id", sa.String(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("notification_id"),
    )


def downgrade() -> None:
    op.drop_table("alert_sent")
