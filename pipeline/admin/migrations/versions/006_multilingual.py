"""multilingual schema — brands.languages, topics.language, runs.languages_completed.

Revision ID: 006_multilingual
Revises: 004_draft_approvals
Create Date: 2026-05-24

S6.1 — fanout per language. Three additive columns:

* ``brands.languages``           JSON TEXT, default ``'["en"]'``.
                                 Icon backfills to ``["en","ru","uk","pl"]``.
* ``topics.language``            VARCHAR(8), NOT NULL, default ``'en'``.
                                 Plus an index ``ix_topics_topic_lang`` so
                                 dedup checks "this topic_id already drafted
                                 in this language?" stay O(log n).
* ``runs.languages_completed``   JSON TEXT, default ``'[]'``. Pipeline appends
                                 each language as its branch finishes.

JSON stored as TEXT (SQLite has no native JSON column type); Postgres reads
it as text and parses at the application layer.

SQLite-safe via ``batch_alter_table`` so the ALTER works on the dev/admin.db
file (which lacks the ALTER COLUMN support Postgres has).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "006_multilingual"
down_revision: str | None = "004_draft_approvals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- brands.languages -------------------------------------------------
    with op.batch_alter_table("brands", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "languages",
                sa.Text(),
                nullable=False,
                server_default='["en"]',
            )
        )

    # Backfill Icon (slug='icon') with the full RU/UK/PL/EN matrix the
    # business already publishes. All other brands keep the default.
    op.execute(
        "UPDATE brands SET languages = '[\"en\",\"ru\",\"uk\",\"pl\"]' "
        "WHERE slug = 'icon'"
    )

    # --- topics.language --------------------------------------------------
    # Fanout produces N rows per (run_id, topic_id) — one per language —
    # so the old UNIQUE (run_id, topic_id) must yield to a wider key that
    # includes language. ``batch_alter_table`` rebuilds the SQLite table
    # so the constraint swap is safe.
    with op.batch_alter_table("topics", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "language",
                sa.String(length=8),
                nullable=False,
                server_default="en",
            )
        )
        batch_op.drop_constraint(
            "uq_topics_run_topic", type_="unique"
        )
        batch_op.create_unique_constraint(
            "uq_topics_run_topic_lang",
            ["run_id", "topic_id", "language"],
        )
        batch_op.create_index(
            "ix_topics_topic_lang",
            ["topic_id", "language"],
            unique=False,
        )

    # --- runs.languages_completed -----------------------------------------
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "languages_completed",
                sa.Text(),
                nullable=False,
                server_default="[]",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_column("languages_completed")

    with op.batch_alter_table("topics", schema=None) as batch_op:
        batch_op.drop_index("ix_topics_topic_lang")
        batch_op.drop_constraint(
            "uq_topics_run_topic_lang", type_="unique"
        )
        batch_op.create_unique_constraint(
            "uq_topics_run_topic", ["run_id", "topic_id"]
        )
        batch_op.drop_column("language")

    with op.batch_alter_table("brands", schema=None) as batch_op:
        batch_op.drop_column("languages")
