"""The primary document: version cache, fetch budgets, doc-search retries.

Revision ID: 027_document_cache
Revises: 026_production_run
Create Date: 2026-09-06

S5 (NTS_101 §2-7). Until now ``candidates.doc_match`` / ``doc_version_id`` /
``doc_sections_used`` were columns nothing wrote (NTS_121 §3) — the fetcher did
not exist, so "каждое число прослеживается до документа" was undeliverable for
the whole ``news`` branch. This migration adds what the fetcher needs:

1. ``document_versions`` — the cache of NTS_101 §5, keyed by URL and versioned
   by ``content_hash``. **Old versions are never deleted.** An article has to
   be able to cite the version it was written from, and a cache that
   overwrites in place makes ``as_of`` (NTS_108 §2-3) a lie the moment the
   regulator republishes.
2. Five fetch budgets in ``pipeline_config`` (NTS_101 §4): ``doc_timeout_s``,
   ``doc_max_mb``, ``doc_max_tokens_for_composition``, ``doc_retries``,
   ``doc_match_model``.
3. ``candidates.doc_attempts`` / ``doc_last_search_at`` — the 48-hour retry of
   NTS_101 §7. Regulators routinely publish the document a day or two after
   the announcement, so a first miss is normal and must not be terminal.
4. ``sources.cache_ttl_days`` back-filled by ``source_class`` with the numbers
   NTS_101 §1 names (legislation 30, jurisdiction_list 7, regulator 14,
   filings 365, professional_alert 90). The column shipped in 020 and is NULL
   on all 41 production rows; leaving that would make every document fetch a
   cache miss.
5. The two feeds parked inactive in 022 — FATF (``html_list``) and SEC EDGAR
   full-text search (``edgar_fts``) — are activated, because their fetchers
   arrive with this session. Only rows still carrying the URL 022 inserted are
   touched, so an operator who edited or paused one keeps their edit.

Additive; the intake timer does not need to stop. ``downgrade`` reverses all
five, and deactivates the two feeds again so a rollback does not leave the
intake calling a fetcher that no longer exists.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "027_document_cache"
down_revision: str | None = "026_production_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# NTS_101 §1 — cache TTL by source class, in days.
CACHE_TTL_BY_CLASS: dict[str, int] = {
    "legislation": 30,
    "jurisdiction_list": 7,
    "regulator": 14,
    "tax_authority": 14,
    "filings": 365,
    "professional_alert": 90,
    "court": 365,
    "corporate_pr": 90,
    "news": 7,
}

# The two rows 022 inserted inactive, identified by the URL it wrote.
_FEEDS_TO_ACTIVATE = (
    "https://www.fatf-gafi.org/en/publications.html",
    "https://efts.sec.gov/LATEST/search-index?forms=8-K,S-4,SC%2013D",
)

_CONFIG_COLUMNS: tuple[tuple[str, sa.types.TypeEngine, str], ...] = (
    ("doc_timeout_s", sa.Integer(), "60"),
    ("doc_max_mb", sa.Integer(), "25"),
    ("doc_max_tokens_for_composition", sa.Integer(), "12000"),
    ("doc_retries", sa.Integer(), "2"),
    ("doc_match_model", sa.Text(), "gpt-4o-mini"),
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
    bind = op.get_bind()

    # 1. the version cache ------------------------------------------------
    if "document_versions" not in _tables():
        op.create_table(
            "document_versions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("content_hash", sa.String(), nullable=False),
            sa.Column("fetched_at", sa.DateTime(), nullable=False),
            # The extracted text, not the source PDF (NTS_096 §Риски): storing
            # the binary would multiply the database by the size of every
            # directive we ever read, and nothing downstream reads bytes.
            sa.Column("extracted_text", sa.Text(), nullable=False),
            sa.Column("doc_language", sa.String(), nullable=True),
            sa.Column("byte_size", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("content_type", sa.String(), nullable=True),
            # Which extractor produced the text. A change of tool changes the
            # text, and an article's provenance has to survive the upgrade.
            sa.Column("tool_version", sa.String(), nullable=False),
            sa.Column("title", sa.Text(), nullable=True),
            sa.Column("published_at", sa.DateTime(), nullable=True),
            sa.Column("source_class", sa.String(), nullable=True),
            sa.Column("section_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("http_status", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "url", "content_hash", name="uq_document_versions_url_hash"
            ),
        )
        op.create_index("ix_document_versions_url", "document_versions", ["url"])
        op.create_index(
            "ix_document_versions_fetched", "document_versions", ["fetched_at"]
        )

    # 2. fetch budgets ----------------------------------------------------
    present = _columns("pipeline_config")
    for name, coltype, default in _CONFIG_COLUMNS:
        if "pipeline_config" in _tables() and name not in present:
            op.add_column(
                "pipeline_config",
                sa.Column(name, coltype, nullable=False, server_default=default),
            )

    # 3. the 48-hour document retry --------------------------------------
    candidate_columns = _columns("candidates")
    if "candidates" in _tables() and "doc_attempts" not in candidate_columns:
        op.add_column(
            "candidates",
            sa.Column(
                "doc_attempts", sa.Integer(), nullable=False, server_default="0"
            ),
        )
    if "candidates" in _tables() and "doc_last_search_at" not in candidate_columns:
        op.add_column(
            "candidates", sa.Column("doc_last_search_at", sa.DateTime(), nullable=True)
        )

    # 4. back-fill sources.cache_ttl_days --------------------------------
    if "cache_ttl_days" in _columns("sources"):
        for source_class, days in CACHE_TTL_BY_CLASS.items():
            bind.execute(
                sa.text(
                    "UPDATE sources SET cache_ttl_days = :d "
                    "WHERE source_class = :c AND cache_ttl_days IS NULL"
                ),
                {"d": days, "c": source_class},
            )

    # 5. activate the two feeds whose fetchers arrive with S5 -------------
    if "fetch_method" in _columns("sources"):
        for url in _FEEDS_TO_ACTIVATE:
            bind.execute(
                sa.text("UPDATE sources SET active = 1 WHERE url = :u AND active = 0"),
                {"u": url},
            )

    if "ix_candidates_doc_search" not in _indexes("candidates"):
        op.create_index(
            "ix_candidates_doc_search",
            "candidates",
            ["status", "doc_last_search_at"],
        )


# --- downgrade -------------------------------------------------------------


def downgrade() -> None:
    bind = op.get_bind()

    if "fetch_method" in _columns("sources"):
        for url in _FEEDS_TO_ACTIVATE:
            # Back to inactive: without the S5 fetchers the intake would raise
            # NotImplementedError on them every morning.
            bind.execute(
                sa.text("UPDATE sources SET active = 0 WHERE url = :u"), {"u": url}
            )

    if "ix_candidates_doc_search" in _indexes("candidates"):
        op.drop_index("ix_candidates_doc_search", table_name="candidates")
    dropping = [
        c
        for c in ("doc_attempts", "doc_last_search_at")
        if c in _columns("candidates")
    ]
    if dropping:
        with op.batch_alter_table("candidates") as batch:
            for column in dropping:
                batch.drop_column(column)

    present = _columns("pipeline_config")
    dropping = [name for name, _t, _d in _CONFIG_COLUMNS if name in present]
    if dropping:
        with op.batch_alter_table("pipeline_config") as batch:
            for column in dropping:
                batch.drop_column(column)

    if "document_versions" in _tables():
        op.drop_table("document_versions")

    # ``sources.cache_ttl_days`` is deliberately NOT reset to NULL: the values
    # are the spec's, an operator may have edited them, and a downgrade that
    # erased a hand-tuned TTL would be destroying data to undo a deploy.
