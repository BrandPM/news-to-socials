"""The production run: rank weights, the flag, the daily batch, the return scope.

Revision ID: 026_production_run
Revises: 025_candidate_traceability
Create Date: 2026-09-06

S4 (NTS_114) turns four config keys that NTS_121 §2 found with **no reader at
all** — ``weekly_draft_budget``, ``production_timeout_min``,
``candidate_ttl_days``, ``retention_days_rejected`` — into the parameters of an
actual run. This migration adds the four things that run needs and that no
earlier migration could have known the shape of:

1. ``pipeline_config.production_enabled`` — a new mode, so it ships **OFF**
   (NTS_103 шаг 3, same rule as ``intake_enabled`` in 022). The deploy that
   lands S4 must not start spending on generation because it landed.
2. ``pipeline_config.rank_weights`` — NTS_100 §2 puts the seven weights of the
   rank formula "в конфиге бренда", not in code, precisely because they were
   "подобраны на глаз" and are meant to be corrected against editor decisions.
   JSON-as-TEXT, like the other five composite keys.
3. ``production_batches`` — NTS_100 §3.3: "``production_batch`` = (brand, date)
   уникален — повторный запуск в тот же день ничего не берёт". The uniqueness
   lives in a UNIQUE constraint rather than a query, because the failure mode
   is two runs a second apart and a SELECT-then-INSERT loses that race. An
   empty portfolio still writes its batch row, so "the run happened and found
   nothing" and "the run never happened" stay different facts.
4. ``candidates.production_batch`` / ``return_scope`` — which batch took the
   candidate, and which stage the editor sent back (NTS_100 §5,
   ``plan`` / ``text`` / ``translation:<lang>`` / ``blocks`` / ``cover`` /
   ``sources``). The scope is what makes a return cost one stage instead of a
   whole article.

All additive and idempotent; nothing is rewritten, so the intake timer does not
need to stop. ``downgrade`` reverses all four — the two column drops on
``candidates`` and the two on ``pipeline_config`` go through batch mode
(SQLite < 3.35 cannot drop a column), which rebuilds those tables; both are
small, so unlike 025 this downgrade is operational rather than a rehearsal.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "026_production_run"
down_revision: str | None = "025_candidate_traceability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# NTS_100 §2 "Стартовые веса". Written as the server default so a config row
# created before this migration and one created after answer identically.
DEFAULT_RANK_WEIGHTS = json.dumps(
    {
        "w_conf": 0.30,
        "w_depth": 0.25,
        "w_fresh": 0.15,
        "w_juris": 0.15,
        "w_kind": 0.05,
        "w_div": 0.20,
        "w_juris_div": 0.10,
    },
    separators=(", ", ": "),
)


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in set(inspector.get_table_names()):
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def _indexes(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in set(inspector.get_table_names()):
        return set()
    return {str(i["name"]) for i in inspector.get_indexes(table)}


# --- upgrade ---------------------------------------------------------------


def upgrade() -> None:
    # 1 + 2. config keys ---------------------------------------------------
    config_columns = _columns("pipeline_config")
    if "pipeline_config" in _tables() and "production_enabled" not in config_columns:
        op.add_column(
            "pipeline_config",
            sa.Column(
                "production_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
    if "pipeline_config" in _tables() and "rank_weights" not in config_columns:
        op.add_column(
            "pipeline_config",
            sa.Column(
                "rank_weights",
                sa.Text(),
                nullable=False,
                server_default=DEFAULT_RANK_WEIGHTS,
            ),
        )

    # 3. production_batches ------------------------------------------------
    if "production_batches" not in _tables():
        op.create_table(
            "production_batches",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "brand_id_fk",
                sa.Integer(),
                sa.ForeignKey(
                    "brands.id",
                    ondelete="RESTRICT",
                    name="fk_production_batches_brand",
                ),
                nullable=False,
            ),
            # The brand's local date, not UTC: the batch key has to mean the
            # same day the operator means (NTS_098 §5).
            sa.Column("batch_date", sa.Date(), nullable=False),
            sa.Column("run_id", sa.Integer(), nullable=True),
            sa.Column(
                "selected_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "brand_id_fk", "batch_date", name="uq_production_batch_day"
            ),
        )

    # 4. candidate columns -------------------------------------------------
    candidate_columns = _columns("candidates")
    if "candidates" in _tables() and "production_batch" not in candidate_columns:
        op.add_column(
            "candidates",
            sa.Column("production_batch", sa.String(), nullable=True),
        )
    if "candidates" in _tables() and "return_scope" not in candidate_columns:
        op.add_column(
            "candidates", sa.Column("return_scope", sa.String(), nullable=True)
        )
    if "candidates" in _tables() and "ix_candidates_batch" not in _indexes(
        "candidates"
    ):
        op.create_index(
            "ix_candidates_batch", "candidates", ["production_batch"]
        )


# --- downgrade -------------------------------------------------------------


def downgrade() -> None:
    if "ix_candidates_batch" in _indexes("candidates"):
        op.drop_index("ix_candidates_batch", table_name="candidates")
    dropping = [
        c for c in ("production_batch", "return_scope") if c in _columns("candidates")
    ]
    if dropping:
        with op.batch_alter_table("candidates") as batch:
            for column in dropping:
                batch.drop_column(column)

    if "production_batches" in _tables():
        op.drop_table("production_batches")

    dropping = [
        c
        for c in ("production_enabled", "rank_weights")
        if c in _columns("pipeline_config")
    ]
    if dropping:
        with op.batch_alter_table("pipeline_config") as batch:
            for column in dropping:
                batch.drop_column(column)
