"""The event is the unit of work: clusters, and a ``deep`` a cluster can reach.

Revision ID: 039_event_clusters
Revises: 038_judge_deprecated
Create Date: 2026-09-30

NTS_131 P3 Ф2. Until now one feed item became one candidate became one article
written from one document, and the second outlet on the same decision was a
duplicate the dedup threw away. Two consequences, both measured: fact packs of
six where the event held fifteen, and ``deep`` never reached — so the data
blocks, which are only built on ``deep``, were unreachable in production.

**1. ``event_clusters`` + ``event_cluster_sources``.** A candidate is the
*anchor* of a cluster; every other material about the same event — another
regulator's notice, a law-firm alert, trade press — is a cluster source. The
fact pack is built across all of them.

**2. ``deep`` is recomputed on the cluster.** Three new/changed keys, each
changed only where it still holds the shipped default (NTS_071's rule, applied
to config):

    depth_deep_min_facts    10 → 11   facts carrying a figure or a date
    depth_deep_min_pairs    new, 1    comparable pairs WITH a value
    depth_deep_min_sources  new, 2    distinct documents the facts come from

The pairs rule used to require two pairs counted on facts with a figure *or* a
date, while blocks were only built from facts with a filled ``value``. The two
halves disagreed, which is why ``keyFigures`` was practically unreachable. Now
both count value-bearing facts, and one pair — exactly what a two-card
keyFigures block needs — is enough.

``deep`` length moves from "1200 and up" to 900-1300 where the target is still
the shipped one: a cluster of 11-15 facts supports 900 words well, and an open
ceiling is the wording NTS_102 §Риски warned would be read as a quota.

**3. Research budget.** A cluster pack is two to three times the size of a
single-document pack; a 2 000-token output ceiling truncates the JSON of a
fifteen-fact pack mid-object. ``research_max_tokens`` 2000 → 5000 and
``research_timeout_seconds`` 60 → 180, where still at the defaults.

**4. Cluster join keys:** ``cluster_threshold`` (0.80) and
``cluster_max_sources`` (5).

Downgrade drops the two tables and the four new columns, and puts the changed
defaults back only where they still hold this migration's values.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "039_event_clusters"
down_revision: str | None = "038_judge_deprecated"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_COLUMNS: tuple[tuple[str, sa.types.TypeEngine, str], ...] = (
    ("depth_deep_min_pairs", sa.Integer(), "1"),
    ("depth_deep_min_sources", sa.Integer(), "2"),
    ("cluster_threshold", sa.Float(), "0.80"),
    ("cluster_max_sources", sa.Integer(), "5"),
)

# (column, shipped default, new value)
_DEFAULT_MOVES: tuple[tuple[str, int, int], ...] = (
    ("depth_deep_min_facts", 10, 11),
    ("research_max_tokens", 2000, 5000),
    ("research_timeout_seconds", 60, 180),
)

_DEEP_BEFORE = [1200, None]
_DEEP_AFTER = [900, 1300]


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _config_columns() -> set[str]:
    return {
        c["name"] for c in sa.inspect(op.get_bind()).get_columns("pipeline_config")
    }


def _move_deep_target(before: list, after: list) -> None:
    bind = op.get_bind()
    for brand_id, raw in bind.execute(
        sa.text("SELECT brand_id_fk, depth_length_targets FROM pipeline_config")
    ).fetchall():
        try:
            targets = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(targets, dict) or targets.get("deep") != before:
            continue
        targets["deep"] = after
        bind.execute(
            sa.text(
                "UPDATE pipeline_config SET depth_length_targets = :t "
                "WHERE brand_id_fk = :b"
            ),
            {"t": json.dumps(targets), "b": brand_id},
        )


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables()
    if "event_clusters" not in tables:
        op.create_table(
            "event_clusters",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "brand_id_fk",
                sa.Integer(),
                sa.ForeignKey("brands.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column(
                "anchor_candidate_id",
                sa.Integer(),
                sa.ForeignKey("candidates.id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
            ),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
    if "event_cluster_sources" not in tables:
        op.create_table(
            "event_cluster_sources",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "cluster_id",
                sa.Integer(),
                sa.ForeignKey("event_clusters.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("url_key", sa.Text(), nullable=False),
            sa.Column("title", sa.Text(), nullable=True),
            sa.Column("publisher", sa.String(), nullable=True),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("role", sa.String(), nullable=False),
            sa.Column("origin", sa.String(), nullable=False),
            sa.Column(
                "source_id_fk",
                sa.Integer(),
                sa.ForeignKey("sources.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("similarity", sa.Float(), nullable=True),
            sa.Column("published_at", sa.DateTime(), nullable=True),
            sa.Column("added_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "cluster_id", "url_key", name="uq_event_cluster_source_url"
            ),
            sa.CheckConstraint(
                "role IN ('primary', 'secondary')",
                name="ck_event_cluster_sources_role",
            ),
            sa.CheckConstraint(
                "origin IN ('intake_dedup', 'manual', 'sourcing')",
                name="ck_event_cluster_sources_origin",
            ),
        )
        op.create_index(
            "ix_event_cluster_sources_cluster",
            "event_cluster_sources",
            ["cluster_id"],
        )

    have = _config_columns()
    for name, type_, default in _NEW_COLUMNS:
        if name not in have:
            op.add_column(
                "pipeline_config",
                sa.Column(name, type_, nullable=False, server_default=default),
            )
    for column, old, new in _DEFAULT_MOVES:
        bind.execute(
            sa.text(f"UPDATE pipeline_config SET {column} = :new WHERE {column} = :old"),
            {"new": new, "old": old},
        )
    _move_deep_target(_DEEP_BEFORE, _DEEP_AFTER)


def downgrade() -> None:
    bind = op.get_bind()
    _move_deep_target(_DEEP_AFTER, _DEEP_BEFORE)
    for column, old, new in _DEFAULT_MOVES:
        bind.execute(
            sa.text(f"UPDATE pipeline_config SET {column} = :old WHERE {column} = :new"),
            {"new": new, "old": old},
        )
    have = _config_columns()
    with op.batch_alter_table("pipeline_config", schema=None) as batch_op:
        for name, _type, _default in _NEW_COLUMNS:
            if name in have:
                batch_op.drop_column(name)
    tables = _tables()
    if "event_cluster_sources" in tables:
        op.drop_index("ix_event_cluster_sources_cluster", "event_cluster_sources")
        op.drop_table("event_cluster_sources")
    if "event_clusters" in tables:
        op.drop_table("event_clusters")
