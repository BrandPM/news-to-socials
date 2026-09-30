"""The attribution check runs on gpt-4o (NTS_131 P3 Ф1).

Revision ID: 040_attribution_on_gpt4o
Revises: 039_event_clusters
Create Date: 2026-09-30

With the judge gone, the attribution check is the only thing between the
draft and "ноль выдумок", and NTS_131 raises its bar from zero distorted to
zero distorted AND zero uncovered. On gpt-4o-mini it passed, on the first live
cluster, "Julius Baer must write down CHF 586 million by the end of 2023" —
an obligation where the source reports a completed write-down — while
flagging the article's own open questions as uncovered. The first is the
failure this check exists to catch.

The check reads a whole cluster (up to ~90 000 characters) three times at
most per article; on gpt-4o that is about USD 0.15 an article.

Changed only where the value is still the shipped ``gpt-4o-mini``: an operator
who chose a model keeps it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "040_attribution_on_gpt4o"
down_revision: str | None = "039_event_clusters"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            "UPDATE pipeline_config SET attribution_model = 'gpt-4o' "
            "WHERE attribution_model = 'gpt-4o-mini'"
        )
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text(
            "UPDATE pipeline_config SET attribution_model = 'gpt-4o-mini' "
            "WHERE attribution_model = 'gpt-4o'"
        )
    )
