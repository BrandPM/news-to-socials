"""``cover_mode`` — data covers or diffusion (NTS_112, S9).

Revision ID: 030_cover_mode
Revises: 029_alert_delivery_and_recall
Create Date: 2026-09-06

One key. It defaults to ``flux``, the current behaviour, for the reason every
other v3 flag defaults to its old value: the deploy that lands a new mode must
not switch modes. Turning it to ``data`` is a Settings edit, and the effect is
immediate and reversible — the generator is chosen per run, nothing is baked
into a draft that already exists.

``data`` is the one NTS_112 argues for: the cover is drawn from the article's
own figures, costs nothing, takes fifty milliseconds and actually differs
between two articles. ``flux`` stays because a human sometimes wants a picture,
and the Regenerate button in Публикации is where they ask for it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "030_cover_mode"
down_revision: str | None = "029_alert_delivery_and_recall"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in set(inspector.get_table_names()):
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    if "cover_mode" not in _columns("pipeline_config"):
        op.add_column(
            "pipeline_config",
            sa.Column(
                "cover_mode", sa.Text(), nullable=False, server_default="flux"
            ),
        )


def downgrade() -> None:
    if "cover_mode" in _columns("pipeline_config"):
        with op.batch_alter_table("pipeline_config") as batch:
            batch.drop_column("cover_mode")
