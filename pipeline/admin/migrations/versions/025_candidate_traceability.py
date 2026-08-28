"""Close the traceability chain: cost→candidate, candidate→doc sections, fact packs.

Revision ID: 025_candidate_traceability
Revises: 024_resync_editorial_guard_rubric
Create Date: 2026-08-28

Out of the NTS_114 session order — the audit in
IT_PROJ_NTS_121_service_inventory found that the chain the whole v3 contour is
supposed to be traceable along is broken in three places at the schema level.
On the production database of 2026-08-28:

* ``candidates.sanity_draft_id`` filled on **0** of 337 rows and
  ``draft_approvals.candidate_id_fk`` on **0** of 137 — declared in NTS_098 §1,
  written by nobody. No migration needed for those two (the columns exist);
  the writer does, and it lands in ``selector/candidate_lifecycle.py``.
* ``cost_records`` had no way to name a candidate at all, so
  ``max_cost_per_candidate_usd`` (NTS_106 §3) was arithmetically impossible:
  ``topic_id`` only exists on the v2 path. **This migration adds it.**
* nothing recorded which parts of a primary document an article was built
  from, and the fact pack was discarded after every research call
  (NTS_096 §"Материал нигде не живёт"). **This migration adds both.**

Three changes, all additive, all idempotent, none of which requires the intake
timer to stop:

1. ``cost_records.candidate_id_fk`` — nullable, ``ON DELETE SET NULL``, indexed.
   35 458 historical rows keep NULL: back-filling would mean guessing which
   candidate a v2 draft call belonged to, and there is no honest mapping.
2. ``candidates.doc_sections_used`` — nullable TEXT holding a JSON array of the
   section labels the extraction read. **Written from S5** (NTS_101 §2-7); the
   column exists now so the chain is complete in the schema and the e2e
   walkthrough can name the gap rather than invent data.
3. ``fact_packs`` — NTS_096 part A. One row per research call, including calls
   for topics that never publish. Carries the whole chain of ids: candidate,
   Sanity draft, v2 topic_id, primary document + version + sections.

``downgrade`` reverses all three. Because SQLite before 3.35 cannot drop a
column, the two column drops go through Alembic's batch mode, which rebuilds
the table — and rebuilding a 35 458-row ``cost_records`` is the reason the
downgrade is a rehearsal step and not an operational one.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "025_candidate_traceability"
down_revision: str | None = "024_resync_editorial_guard_rubric"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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
    # 1. cost_records.candidate_id_fk ------------------------------------
    if "cost_records" in _tables() and "candidate_id_fk" not in _columns(
        "cost_records"
    ):
        # No inline ForeignKey: SQLite's ALTER TABLE ADD COLUMN accepts a
        # REFERENCES clause, but Alembic renders it only inside batch mode, and
        # rebuilding a 35k-row table for a nullable audit pointer is not worth
        # the downtime. The constraint is declared on the model (SQLAlchemy
        # emits it for a freshly created DB) and enforced by the one writer.
        op.add_column(
            "cost_records",
            sa.Column("candidate_id_fk", sa.Integer(), nullable=True),
        )
    if "cost_records" in _tables() and "ix_cost_records_candidate" not in _indexes(
        "cost_records"
    ):
        op.create_index(
            "ix_cost_records_candidate", "cost_records", ["candidate_id_fk"]
        )

    # 2. candidates.doc_sections_used ------------------------------------
    if "candidates" in _tables() and "doc_sections_used" not in _columns(
        "candidates"
    ):
        op.add_column(
            "candidates",
            sa.Column("doc_sections_used", sa.Text(), nullable=True),
        )

    # 3. fact_packs -------------------------------------------------------
    if "fact_packs" not in _tables():
        op.create_table(
            "fact_packs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "brand_id_fk",
                sa.Integer(),
                sa.ForeignKey(
                    "brands.id", ondelete="RESTRICT", name="fk_fact_packs_brand"
                ),
                nullable=False,
            ),
            # SET NULL, not RESTRICT: the rejected-candidate prune (NTS_098 §2)
            # must not be blocked by an audit row, and the pack is still
            # readable by draft id and topic id after the candidate is gone.
            sa.Column(
                "candidate_id_fk",
                sa.Integer(),
                sa.ForeignKey(
                    "candidates.id",
                    ondelete="SET NULL",
                    name="fk_fact_packs_candidate",
                ),
                nullable=True,
            ),
            sa.Column("topic_id", sa.String(), nullable=True),
            sa.Column("sanity_draft_id", sa.String(), nullable=True),
            sa.Column("pack", sa.Text(), nullable=False),
            sa.Column(
                "sources", sa.Text(), nullable=False, server_default="[]"
            ),
            sa.Column("primary_doc_url", sa.Text(), nullable=True),
            sa.Column("doc_version_id", sa.String(), nullable=True),
            sa.Column("doc_sections_used", sa.Text(), nullable=True),
            sa.Column("doc_text", sa.Text(), nullable=True),
            sa.Column("model", sa.String(), nullable=True),
            sa.Column(
                "cost_usd", sa.Float(), nullable=False, server_default="0"
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_fact_packs_candidate", "fact_packs", ["candidate_id_fk"]
        )
        op.create_index(
            "ix_fact_packs_draft", "fact_packs", ["sanity_draft_id"]
        )
        op.create_index("ix_fact_packs_topic", "fact_packs", ["topic_id"])
        op.create_index(
            "ix_fact_packs_brand_created",
            "fact_packs",
            ["brand_id_fk", "created_at"],
        )


# --- downgrade -------------------------------------------------------------


def downgrade() -> None:
    if "fact_packs" in _tables():
        op.drop_table("fact_packs")

    if "doc_sections_used" in _columns("candidates"):
        with op.batch_alter_table("candidates") as batch:
            batch.drop_column("doc_sections_used")

    if "ix_cost_records_candidate" in _indexes("cost_records"):
        op.drop_index("ix_cost_records_candidate", table_name="cost_records")
    if "candidate_id_fk" in _columns("cost_records"):
        with op.batch_alter_table("cost_records") as batch:
            batch.drop_column("candidate_id_fk")
