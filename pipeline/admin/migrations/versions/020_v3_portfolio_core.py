"""v3 contour-1 core schema: candidates, review_decisions, brand_taxonomy (NTS_098/099/101/107/109).

Revision ID: 020_v3_portfolio_core
Revises: 019_resync_nts092
Create Date: 2026-08-28

Session S1 of IT_PROJ_NTS_114. This is the whole data floor v3 stands on;
nothing here is switched on by itself. It creates the three new tables, widens
``sources`` with the primary-feed registry fields, links ``draft_approvals``
back to the candidate that produced it, tags ``runs`` with a ``run_type``, and
lands 25 new ``pipeline_config`` keys.

Design notes worth keeping:

* **Nothing runs yet.** No pipeline code reads ``candidates`` after this
  migration. S2 fills it from the intake run, S4 selects out of it. Applying
  this on prod changes zero behaviour — by construction, not by a flag.
* **``runs.run_type`` is nullable on purpose.** NTS_106 §5 defines the enum as
  ``{intake, production, publish, ttl}``; the ~72 historical rows are none of
  those. NULL means "pre-v3 run" and the CHECK passes on NULL, so no value had
  to be invented for old data.
* **``sources.license_class`` is NOT NULL, defaulting to the most restrictive
  class.** NTS_108 §1 lets ``news_paywalled`` supply a headline as a lead and
  nothing more. Existing feeds are a mix and none of them are classified yet,
  so they start at the class that cannot cause a licensing problem; the
  Sources screen (S3) reclassifies them upward. A nullable column would have
  risked a downstream reader treating "unknown" as "unrestricted".
* **``brand_timezone`` overlaps ``brands.timezone``.** NTS_098 §4 puts it in
  the config surface, so that is where it goes, and each brand's existing
  ``brands.timezone`` is copied in as its starting value so the two cannot
  disagree on day one. From S4 on, slot arithmetic must read the config key
  as the single authority.
* **Icon's taxonomy and jurisdiction tiers are seeded** from the S0 inputs
  (IT_PROJ_NTS_115 artefacts 3-4) — that document exists precisely so the
  guard's ``{services}`` / ``{jurisdiction_tiers}`` placeholders have real
  values to resolve against in S2. Seeding is insert-when-absent, so an
  operator edit made before the next deploy is never overwritten.

Every step is guarded by an existence check, so a half-applied deploy can be
re-run without hand-editing ``alembic_version``. ``downgrade`` reverses all of
it: drops the three tables, the added columns, and leaves neighbouring columns
alone (asserted by test).
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "020_v3_portfolio_core"
down_revision: str | None = "019_resync_nts092"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --- enums (duplicated here on purpose) -----------------------------------
# A migration must not import the ORM: models drift with the head revision,
# a migration must keep describing the schema as it was at ITS revision.

_INPUT_KINDS = ("document", "news")
_VERDICTS = ("accept", "reject")
_REASON_CODES = (
    "ok",
    "personnel",
    "forecast",
    "award_pr",
    "no_document",
    "no_consequence",
    "out_of_jurisdiction",
    "out_of_scope",
    "duplicate_stage",
    "retail_crypto",
    "daily_cap",
    "guard_error",
)
_EVENT_STAGES = (
    "consultation",
    "adopted",
    "in_force",
    "ruling",
    "deal_announced",
    "deal_closed",
    "list_update",
    "other",
)
_DEPTHS = ("note", "article", "deep")
_CANDIDATE_STATUSES = (
    "pending",
    "selected",
    "in_production",
    "drafted",
    "returned",
    "ready",
    "published",
    "doc_missing",
    "expired",
    "failed",
    "superseded",
    "rejected",
)
_MANUAL_ACTIONS = ("promoted", "held", "rejected")
_REVIEW_ACTIONS = (
    "approve",
    "return",
    "reject",
    "hold",
    "promote",
    "disagree_guard",
)
_SOURCE_ROLES = ("news", "primary_feed", "primary_site")
_SOURCE_CLASSES = (
    "regulator",
    "tax_authority",
    "legislation",
    "jurisdiction_list",
    "filings",
    "court",
    "professional_alert",
    "corporate_pr",
    "news",
)
_LICENSE_CLASSES = (
    "public_official",
    "public_domain",
    "corporate_pr",
    "professional_commentary",
    "news_paywalled",
)
_FETCH_METHODS = ("rss", "atom", "html_list", "edgar_fts")
_RUN_TYPES = ("intake", "production", "publish", "ttl")


def _in(column: str, values: Sequence[str]) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"


# --- pipeline_config keys (NTS_098 §4, NTS_099 §1) ------------------------
# (name, type, server_default). Defaults are Icon's starting values from the
# spec; every one of them is editable from Settings without a deploy, and
# every one is sentinel-tested end to end (migration → ORM → ConfigRecord →
# API) by tests/unit/test_v3_config_sentinels_nts098.py.

_CONFIG_COLUMNS: tuple[tuple[str, sa.types.TypeEngine, str], ...] = (
    # Rhythm — NTS_098 §4/§5
    (
        "publication_slots",
        sa.Text(),
        json.dumps(
            [{"day": "mon", "capacity": 2}, {"day": "thu", "capacity": 2}]
        ),
    ),
    ("weekly_draft_budget", sa.Integer(), "6"),
    ("portfolio_daily_cap_document", sa.Integer(), "2"),
    ("portfolio_daily_cap_news", sa.Integer(), "1"),
    (
        "candidate_ttl_days",
        sa.Text(),
        json.dumps(
            {
                "deal_announced": 7,
                "deal_closed": 7,
                "consultation": 21,
                "default": 14,
            }
        ),
    ),
    ("production_timeout_min", sa.Integer(), "60"),
    ("max_attempts", sa.Integer(), "2"),
    ("brand_timezone", sa.Text(), "Europe/Madrid"),
    ("retention_days_rejected", sa.Integer(), "30"),
    # Dedup windows — NTS_098 §3. Deliberately NOT reusing the NTS_090 keys
    # (dedup_threshold / dedup_window_days): those govern v2 draft dedup and
    # keep working untouched until v2 generation is switched off in S2.
    ("dedup_threshold_live", sa.Float(), "0.90"),
    ("dedup_threshold_rejected", sa.Float(), "0.92"),
    ("dedup_window_rejected_days", sa.Integer(), "14"),
    ("dedup_threshold_published", sa.Float(), "0.88"),
    ("dedup_window_published_days", sa.Integer(), "60"),
    # Guard axes — NTS_099 §2, values from NTS_115 artefact 4
    (
        "jurisdiction_tiers",
        sa.Text(),
        json.dumps(
            {
                "tier1": ["CH", "CY", "MT", "AE", "UK", "PL", "UA", "LI", "EU"],
                "tier2": [
                    "US",
                    "SG",
                    "HK",
                    "LU",
                    "MC",
                    "PT",
                    "ES",
                    "IT",
                    "DE",
                    "AT",
                    "IL",
                    "KZ",
                    "TR",
                ],
            }
        ),
    ),
    # Depth thresholds — NTS_102 v2, consumed from S6
    ("depth_article_min_facts", sa.Integer(), "4"),
    ("depth_deep_min_facts", sa.Integer(), "10"),
    # Spend kill-switch — NTS_106 §3
    ("monthly_spend_cap_usd", sa.Float(), "150"),
    ("max_cost_per_candidate_usd", sa.Float(), "5"),
    # Prefilter — NTS_099 §1
    (
        "prefilter_deny_title_patterns",
        sa.Text(),
        json.dumps(
            [
                "appoints",
                "hires",
                "joins",
                "named as",
                "wins award",
                "ranked",
                "opens office",
                "rebrand",
                "outlook",
                "forecast",
                "analysts expect",
            ]
        ),
    ),
    ("prefilter_require_summary", sa.Boolean(), "1"),
    ("prefilter_max_age_hours_news", sa.Integer(), "72"),
    ("prefilter_max_age_hours_primary", sa.Integer(), "240"),
    (
        "prefilter_languages",
        sa.Text(),
        json.dumps(["en", "de", "fr", "it", "pl", "uk", "ru", "el"]),
    ),
    ("prefilter_min_summary_chars", sa.Integer(), "80"),
)


# --- sources registry fields (NTS_101 §1) ---------------------------------

_SOURCE_COLUMNS: tuple[tuple[str, sa.types.TypeEngine, str | None, bool], ...] = (
    # (name, type, server_default, nullable)
    ("source_role", sa.String(), "news", False),
    ("source_class", sa.String(), "news", False),
    ("license_class", sa.String(), "news_paywalled", False),
    ("doc_language", sa.String(), None, True),
    ("fetch_method", sa.String(), None, True),
    ("reliability", sa.Float(), None, True),
    ("cache_ttl_days", sa.Integer(), None, True),
)

# NTS_115 artefact 4 — Icon's five services, seeded so the guard's {services}
# placeholder resolves against real rows from S2 on.
_ICON_TAXONOMY: tuple[tuple[str, str, str, str], ...] = (
    (
        "wealth",
        "Wealth Management",
        "Управление частным капиталом: банки, управляющие, условия доступа "
        "к private banking, регулирование управляющих и трасти",
        "/services/wealth-management",
    ),
    (
        "family",
        "Family Office",
        "Семейные структуры: фонды, трасты, наследование, брачные режимы, "
        "управление активами семьи через поколения",
        "/services/family-office",
    ),
    (
        "structuring",
        "Structuring & Tax",
        "Налоговое и корпоративное структурирование: резидентность, режимы, "
        "CRS/DAC/CARF, UBO, списки юрисдикций, DTT",
        "/services/structuring-tax",
    ),
    (
        "ma",
        "M&A Consulting",
        "Сделки среднего рынка: покупка/продажа бизнеса, структура сделки, "
        "earn-out, SPA, раскрытые условия",
        "/services/ma-consulting",
    ),
    (
        "special",
        "Special Solutions",
        "Нестандартные ситуации: санкции и комплаенс-барьеры, программы "
        "резидентности и гражданства, защита активов, кризисные структуры",
        "/services/special-solutions",
    ),
)


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in set(inspector.get_table_names()):
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


# --- upgrade ---------------------------------------------------------------


def _create_candidates() -> None:
    op.create_table(
        "candidates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "brand_id_fk",
            sa.Integer(),
            sa.ForeignKey(
                "brands.id", ondelete="RESTRICT", name="fk_candidates_brand"
            ),
            nullable=False,
        ),
        sa.Column("input_kind", sa.String(), nullable=False),
        # --- source snapshot: copied, never joined. NTS_098 DoD "кандидат
        # переживает исчезновение RSS-элемента" — the feed item can vanish
        # and the candidate still renders in full.
        sa.Column(
            "source_id_fk",
            sa.Integer(),
            sa.ForeignKey(
                "sources.id", ondelete="RESTRICT", name="fk_candidates_source"
            ),
            nullable=True,
        ),
        sa.Column("source_title", sa.Text(), nullable=False),
        sa.Column("source_summary", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_published_at", sa.DateTime(), nullable=True),
        sa.Column("source_language", sa.String(), nullable=True),
        sa.Column("source_name", sa.String(), nullable=True),
        sa.Column("source_class", sa.String(), nullable=True),
        # --- dedup (NTS_079)
        sa.Column("topic_embedding_ref", sa.String(), nullable=True),
        # --- guard verdict (NTS_099 §3)
        sa.Column("verdict", sa.String(), nullable=False),
        sa.Column("reason_code", sa.String(), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("service_category", sa.String(), nullable=True),
        sa.Column("jurisdictions", sa.Text(), nullable=True),
        sa.Column("event_stage", sa.String(), nullable=True),
        sa.Column("depth_prior", sa.String(), nullable=True),
        # --- set after research (NTS_102 v2)
        sa.Column("depth_final", sa.String(), nullable=True),
        # --- primary document (NTS_101 v2)
        sa.Column("primary_doc_hint", sa.Text(), nullable=True),
        sa.Column("primary_doc_url", sa.Text(), nullable=True),
        sa.Column("doc_version_id", sa.String(), nullable=True),
        sa.Column("doc_match", sa.String(), nullable=True),
        sa.Column("doc_language_expected", sa.String(), nullable=True),
        # --- lifecycle (NTS_098 §2)
        sa.Column(
            "status", sa.String(), nullable=False, server_default="pending"
        ),
        sa.Column(
            "attempts", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("manual_action", sa.String(), nullable=True),
        sa.Column("manual_by", sa.String(), nullable=True),
        sa.Column("manual_at", sa.DateTime(), nullable=True),
        sa.Column(
            "cap_overflow", sa.Boolean(), nullable=False, server_default="0"
        ),
        sa.Column("sanity_draft_id", sa.String(), nullable=True),
        sa.Column("publication_slot", sa.Date(), nullable=True),
        sa.Column(
            "canon_dirty", sa.Boolean(), nullable=False, server_default="0"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("selected_at", sa.DateTime(), nullable=True),
        sa.Column("drafted_at", sa.DateTime(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("failed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "supersedes_id",
            sa.Integer(),
            sa.ForeignKey(
                "candidates.id",
                ondelete="SET NULL",
                name="fk_candidates_supersedes",
            ),
            nullable=True,
        ),
        sa.CheckConstraint(
            _in("input_kind", _INPUT_KINDS), name="ck_candidates_input_kind"
        ),
        sa.CheckConstraint(
            _in("verdict", _VERDICTS), name="ck_candidates_verdict"
        ),
        sa.CheckConstraint(
            _in("reason_code", _REASON_CODES), name="ck_candidates_reason_code"
        ),
        sa.CheckConstraint(
            _in("event_stage", _EVENT_STAGES), name="ck_candidates_event_stage"
        ),
        sa.CheckConstraint(
            _in("depth_prior", _DEPTHS), name="ck_candidates_depth_prior"
        ),
        sa.CheckConstraint(
            _in("depth_final", _DEPTHS), name="ck_candidates_depth_final"
        ),
        sa.CheckConstraint(
            _in("status", _CANDIDATE_STATUSES), name="ck_candidates_status"
        ),
        sa.CheckConstraint(
            _in("manual_action", _MANUAL_ACTIONS),
            name="ck_candidates_manual_action",
        ),
        sa.CheckConstraint(
            _in("source_class", _SOURCE_CLASSES),
            name="ck_candidates_source_class",
        ),
    )
    op.create_index(
        "ix_candidates_brand_status", "candidates", ["brand_id_fk", "status"]
    )
    op.create_index(
        "ix_candidates_brand_created",
        "candidates",
        ["brand_id_fk", "created_at"],
    )
    op.create_index(
        "ix_candidates_sanity_draft", "candidates", ["sanity_draft_id"]
    )
    # TTL sweep (NTS_098 §2) scans expiring rows by status.
    op.create_index(
        "ix_candidates_status_expires", "candidates", ["status", "expires_at"]
    )
    # One document, one candidate (NTS_098 §3) — the lookup that enforces it.
    op.create_index(
        "ix_candidates_primary_doc", "candidates", ["primary_doc_url"]
    )


def _create_review_decisions() -> None:
    """Every editor action, with the timer reading (NTS_107 §5).

    RESTRICT on the candidate: the decision log is the only free signal for
    tuning the guard rubric and the rank weights (NTS_113), so a candidate
    cannot be deleted out from under its own history.
    """
    op.create_table(
        "review_decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "brand_id_fk",
            sa.Integer(),
            sa.ForeignKey(
                "brands.id", ondelete="RESTRICT", name="fk_review_brand"
            ),
            nullable=False,
        ),
        sa.Column(
            "candidate_id_fk",
            sa.Integer(),
            sa.ForeignKey(
                "candidates.id",
                ondelete="RESTRICT",
                name="fk_review_candidate",
            ),
            nullable=False,
        ),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "reviewer", sa.String(), nullable=False, server_default="admin"
        ),
        sa.Column("time_spent_s", sa.Integer(), nullable=True),
        sa.Column(
            "at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            _in("action", _REVIEW_ACTIONS), name="ck_review_decisions_action"
        ),
    )
    op.create_index(
        "ix_review_decisions_brand_at", "review_decisions", ["brand_id_fk", "at"]
    )
    op.create_index(
        "ix_review_decisions_candidate", "review_decisions", ["candidate_id_fk"]
    )


def _create_brand_taxonomy() -> None:
    """Per-brand service list (NTS_109). Replaces the hardcoded five-service
    enum so a second brand needs rows, not a code change."""
    op.create_table(
        "brand_taxonomy",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "brand_id_fk",
            sa.Integer(),
            sa.ForeignKey(
                "brands.id", ondelete="RESTRICT", name="fk_taxonomy_brand"
            ),
            nullable=False,
        ),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("description_for_guard", sa.Text(), nullable=False),
        sa.Column("service_url_path", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "brand_id_fk", "key", name="uq_brand_taxonomy_brand_key"
        ),
    )


def _seed_icon_taxonomy() -> None:
    """Insert-when-absent, so an operator edit survives the next deploy."""
    bind = op.get_bind()
    icon_id = bind.execute(
        sa.text("SELECT id FROM brands WHERE slug = 'icon'")
    ).scalar()
    if icon_id is None:
        return
    existing = {
        r[0]
        for r in bind.execute(
            sa.text("SELECT key FROM brand_taxonomy WHERE brand_id_fk = :b"),
            {"b": icon_id},
        )
    }
    for key, label, description, url_path in _ICON_TAXONOMY:
        if key in existing:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO brand_taxonomy "
                "(brand_id_fk, key, label, description_for_guard, service_url_path) "
                "VALUES (:b, :k, :l, :d, :u)"
            ),
            {"b": icon_id, "k": key, "l": label, "d": description, "u": url_path},
        )


def _backfill_sources() -> None:
    """Existing feeds are all news RSS — say so explicitly rather than leaving
    the registry fields at a default nobody chose."""
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE sources SET fetch_method = 'rss' "
            "WHERE fetch_method IS NULL AND source_type = 'rss'"
        )
    )


def _backfill_brand_timezone() -> None:
    """Copy each brand's existing timezone in, so the new config key and
    ``brands.timezone`` cannot disagree on day one."""
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE pipeline_config SET brand_timezone = ("
            "  SELECT timezone FROM brands WHERE brands.id = pipeline_config.brand_id_fk"
            ") WHERE EXISTS ("
            "  SELECT 1 FROM brands WHERE brands.id = pipeline_config.brand_id_fk"
            "    AND brands.timezone IS NOT NULL AND brands.timezone != ''"
            ")"
        )
    )


def upgrade() -> None:
    tables = _tables()

    if "candidates" not in tables:
        _create_candidates()
    if "review_decisions" not in tables:
        _create_review_decisions()
    if "brand_taxonomy" not in tables:
        _create_brand_taxonomy()

    # --- sources: primary-feed registry fields (NTS_101 §1)
    present = _columns("sources")
    added_source_cols = False
    with op.batch_alter_table("sources", schema=None) as batch_op:
        for name, coltype, default, nullable in _SOURCE_COLUMNS:
            if name in present:
                continue
            added_source_cols = True
            batch_op.add_column(
                sa.Column(
                    name,
                    coltype,
                    nullable=nullable,
                    server_default=default,
                )
            )
    if added_source_cols:
        # CHECKs are added in a second pass: batch add_column takes the fast
        # ALTER path, and mixing a constraint into it forces a table rebuild
        # for every column instead of one.
        with op.batch_alter_table("sources", schema=None) as batch_op:
            batch_op.create_check_constraint(
                "ck_sources_source_role", _in("source_role", _SOURCE_ROLES)
            )
            batch_op.create_check_constraint(
                "ck_sources_source_class", _in("source_class", _SOURCE_CLASSES)
            )
            batch_op.create_check_constraint(
                "ck_sources_license_class",
                _in("license_class", _LICENSE_CLASSES),
            )
            batch_op.create_check_constraint(
                "ck_sources_fetch_method", _in("fetch_method", _FETCH_METHODS)
            )
        _backfill_sources()

    # --- draft_approvals: link back to the candidate (nullable for v2 rows)
    if "candidate_id_fk" not in _columns("draft_approvals"):
        with op.batch_alter_table("draft_approvals", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "candidate_id_fk",
                    sa.Integer(),
                    sa.ForeignKey(
                        "candidates.id",
                        ondelete="SET NULL",
                        name="fk_draft_approvals_candidate",
                    ),
                    nullable=True,
                )
            )
        op.create_index(
            "ix_draft_approvals_candidate", "draft_approvals", ["candidate_id_fk"]
        )

    # --- runs: which contour produced this run (NTS_106 §5)
    if "run_type" not in _columns("runs"):
        with op.batch_alter_table("runs", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("run_type", sa.String(), nullable=True)
            )
        with op.batch_alter_table("runs", schema=None) as batch_op:
            batch_op.create_check_constraint(
                "ck_runs_run_type", _in("run_type", _RUN_TYPES)
            )
        op.create_index("ix_runs_run_type", "runs", ["run_type"])

    # --- pipeline_config: 25 new keys (NTS_098 §4, NTS_099 §1)
    present = _columns("pipeline_config")
    added_config = False
    with op.batch_alter_table("pipeline_config", schema=None) as batch_op:
        for name, coltype, default in _CONFIG_COLUMNS:
            if name in present:
                continue
            added_config = True
            batch_op.add_column(
                sa.Column(
                    name,
                    coltype,
                    nullable=False,
                    server_default=default,
                )
            )
    if added_config:
        _backfill_brand_timezone()

    if "brand_taxonomy" in _tables():
        _seed_icon_taxonomy()


# --- downgrade -------------------------------------------------------------


def downgrade() -> None:
    present = _columns("pipeline_config")
    with op.batch_alter_table("pipeline_config", schema=None) as batch_op:
        for name, _coltype, _default in _CONFIG_COLUMNS:
            if name in present:
                batch_op.drop_column(name)

    if "run_type" in _columns("runs"):
        op.drop_index("ix_runs_run_type", table_name="runs")
        with op.batch_alter_table("runs", schema=None) as batch_op:
            batch_op.drop_constraint("ck_runs_run_type", type_="check")
            batch_op.drop_column("run_type")

    if "candidate_id_fk" in _columns("draft_approvals"):
        op.drop_index(
            "ix_draft_approvals_candidate", table_name="draft_approvals"
        )
        with op.batch_alter_table("draft_approvals", schema=None) as batch_op:
            batch_op.drop_constraint(
                "fk_draft_approvals_candidate", type_="foreignkey"
            )
            batch_op.drop_column("candidate_id_fk")

    present = _columns("sources")
    if any(name in present for name, _t, _d, _n in _SOURCE_COLUMNS):
        with op.batch_alter_table("sources", schema=None) as batch_op:
            for cname in (
                "ck_sources_source_role",
                "ck_sources_source_class",
                "ck_sources_license_class",
                "ck_sources_fetch_method",
            ):
                batch_op.drop_constraint(cname, type_="check")
            for src_name, _src_type, _src_default, _src_nullable in _SOURCE_COLUMNS:
                if src_name in present:
                    batch_op.drop_column(src_name)

    tables = _tables()
    # review_decisions before candidates: it holds the FK.
    if "review_decisions" in tables:
        op.drop_table("review_decisions")
    if "brand_taxonomy" in tables:
        op.drop_table("brand_taxonomy")
    if "candidates" in tables:
        op.drop_table("candidates")
