"""pipeline_config: add ``images_on_demand`` (default False).

Revision ID: 017_images_on_demand
Revises: 016_draft_scores
Create Date: 2026-08-27

IT_PROJ_NTS_094 — stop paying Replicate for covers nobody publishes. With the
flag ON, a pipeline run writes its drafts with ``coverImage: null`` on purpose
and the manager generates the cover for the one draft they picked, from the
button NTS_091 already shipped.

``server_default='0'`` is deliberate: applying this migration must leave
production behaviour byte-identical (covers still generated during the run).
The operator flips the flag from Settings once the deploy verifies clean —
that, not this migration, is the moment the cost change starts.

Additive + defaulted → safe on existing rows. ``downgrade`` drops the column.
The upgrade is a no-op when the column already exists, so a partially applied
deploy can be re-run without hand-editing ``alembic_version``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "017_images_on_demand"
down_revision: str | None = "016_draft_scores"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "pipeline_config"
_COLUMN = "images_on_demand"


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    return _COLUMN in {c["name"] for c in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    if _has_column():
        return
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                _COLUMN,
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    if not _has_column():
        return
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        batch_op.drop_column(_COLUMN)
