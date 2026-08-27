"""pipeline_config: research stage switch + budgets (NTS_092).

Revision ID: 018_research_budgets
Revises: 017_images_on_demand
Create Date: 2026-08-27

IT_PROJ_NTS_092 — the drafter now works from a web-research fact pack instead
of an RSS headline. The three budgets the research call runs under live here
so they are tunable from Settings without a deploy:

* ``research_max_sources``      distinct outlets allowed in ``context`` (5)
* ``research_max_tokens``       output ceiling on the research call (2000)
* ``research_timeout_seconds``  hard ceiling on the whole call (60)

``research_enabled`` is the master switch. It defaults to **1** — unlike
NTS_094's ``images_on_demand``, which shipped inert. That is deliberate and it
is a real cost change on apply: the prompt half of NTS_092 raises the article
to 600-800 words and gives ``writer_draft`` a ``{fact_pack}`` placeholder, so
a deploy with research OFF would produce longer articles with nothing new to
be specific about — precisely the padding this project exists to remove. The
switch is here so the spend can be stopped from Settings in seconds if the
measured $/article turns out wrong, not so it starts off.

Additive + defaulted → safe on existing rows. Each column is added only when
absent, so a partially applied deploy can be re-run without hand-editing
``alembic_version``. ``downgrade`` drops all four.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "018_research_budgets"
down_revision: str | None = "017_images_on_demand"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "pipeline_config"

_COLUMNS: tuple[tuple[str, sa.types.TypeEngine, str], ...] = (
    ("research_enabled", sa.Boolean(), "1"),
    ("research_max_sources", sa.Integer(), "5"),
    ("research_max_tokens", sa.Integer(), "2000"),
    ("research_timeout_seconds", sa.Integer(), "60"),
)


def _existing() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {c["name"] for c in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    present = _existing()
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        for name, coltype, default in _COLUMNS:
            if name in present:
                continue
            batch_op.add_column(
                sa.Column(
                    name,
                    coltype,
                    nullable=False,
                    server_default=sa.text(default),
                )
            )


def downgrade() -> None:
    present = _existing()
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        for name, _coltype, _default in _COLUMNS:
            if name in present:
                batch_op.drop_column(name)
