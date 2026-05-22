"""brands table + brand_id_fk on sources/prompts/pipeline_config/runs

Revision ID: 002_brands_fk
Revises: bfef7aabd7b4
Create Date: 2026-05-22

Multi-brand refactor per IT_PROJ_NTS_025.

* Adds ``brands`` table — first-class brand entity with encrypted
  Sanity/Telegram/Meta credentials.
* Adds nullable ``brand_id_fk`` INTEGER on the four existing tables.
* Seeds 5 brands: Icon (active, real Sanity creds from .env) + 4
  placeholder drafts (Neovox, Creolix, Vilatrix, Nexora).
* Backfills ``brand_id_fk`` on existing rows by matching the old string
  ``brand_id`` to ``brands.slug``.
* GUARD: aborts with row counts if any backfilled row is still NULL —
  catches a botched data migration before the schema lock-in.
* Drops the old string ``brand_id`` column, alters ``brand_id_fk`` to
  NOT NULL.

Downgrade re-creates the string ``brand_id`` column populated from a
join through ``brands.slug``, then drops the FK and ``brands`` table.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op


revision: str = "002_brands_fk"
down_revision: str | None = "bfef7aabd7b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Brand seed defaults (Icon active, 4 placeholders draft). Sanity creds
# for Icon are pulled live from settings + encrypted at migration time.
_PLACEHOLDER_BRANDS = (
    ("neovox", "Neovox", "en", "Europe/Madrid"),
    ("creolix", "Creolix", "en", "Europe/Madrid"),
    ("vilatrix", "Vilatrix", "en", "Europe/Madrid"),
    ("nexora", "Nexora", "en", "Europe/Madrid"),
)


def _encrypt_or_none(value: str | None) -> str | None:
    """Encrypt ``value`` using the configured master key, or None if empty.

    Imported lazily so the migration module is importable without the
    cryptography package being installed (e.g. for autogenerate diffs).
    """
    if not value:
        return None
    from pipeline.admin.encryption import get_encryption  # noqa: PLC0415

    return get_encryption().encrypt(value)


def _affected_existing_tables() -> tuple[str, ...]:
    return ("sources", "prompts", "pipeline_config", "runs")


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(tz=timezone.utc)

    # SQLite enforces FK constraints when our app engine attaches them
    # via the ``connect`` event listener (db.py). batch_alter_table
    # operations recreate tables, which transiently violates FKs. Turn
    # them off for the duration of this migration; re-enabled at the
    # end so a subsequent ``alembic upgrade head`` on the same process
    # sees the proper enforcement.
    op.execute(sa.text("PRAGMA foreign_keys=OFF"))

    # 1. brands table -------------------------------------------------------
    op.create_table(
        "brands",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("language", sa.String(), nullable=False),
        sa.Column("timezone", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("sanity_project_id", sa.String(), nullable=True),
        sa.Column("sanity_dataset", sa.String(), nullable=True),
        sa.Column("sanity_api_version", sa.String(), nullable=True),
        sa.Column("sanity_api_token_enc", sa.Text(), nullable=True),
        sa.Column("sanity_studio_url", sa.String(), nullable=True),
        sa.Column("telegram_bot_token_enc", sa.Text(), nullable=True),
        sa.Column("telegram_channel_id", sa.String(), nullable=True),
        sa.Column("meta_app_id", sa.String(), nullable=True),
        sa.Column("meta_app_secret_enc", sa.Text(), nullable=True),
        sa.Column("meta_access_token_enc", sa.Text(), nullable=True),
        sa.Column("meta_page_id", sa.String(), nullable=True),
        sa.Column("meta_ig_business_id", sa.String(), nullable=True),
        sa.Column("voice_profile_yaml", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'paused', 'archived')",
            name="ck_brands_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_brands_slug"),
    )

    # 2. Seed brands --------------------------------------------------------
    # Icon — active with live credentials from .env (encrypted on the fly).
    from pipeline.common.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    icon_token_enc = _encrypt_or_none(settings.sanity_api_token)
    icon_studio_url = (
        f"https://{settings.sanity_project_id}.sanity.studio/"
        if settings.sanity_project_id
        else None
    )

    op.bulk_insert(
        sa.table(
            "brands",
            sa.column("slug", sa.String()),
            sa.column("name", sa.String()),
            sa.column("language", sa.String()),
            sa.column("timezone", sa.String()),
            sa.column("status", sa.String()),
            sa.column("active", sa.Boolean()),
            sa.column("sanity_project_id", sa.String()),
            sa.column("sanity_dataset", sa.String()),
            sa.column("sanity_api_version", sa.String()),
            sa.column("sanity_api_token_enc", sa.Text()),
            sa.column("sanity_studio_url", sa.String()),
            sa.column("created_at", sa.DateTime()),
            sa.column("updated_at", sa.DateTime()),
        ),
        [
            {
                "slug": "icon",
                "name": "Icon Finance",
                "language": "en",
                "timezone": "Europe/Madrid",
                "status": "active",
                "active": bool(settings.sanity_api_token),
                "sanity_project_id": settings.sanity_project_id or None,
                "sanity_dataset": settings.sanity_dataset or None,
                "sanity_api_version": settings.sanity_api_version or "2024-01-01",
                "sanity_api_token_enc": icon_token_enc,
                "sanity_studio_url": icon_studio_url,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )

    # Placeholders — status='draft', no credentials.
    op.bulk_insert(
        sa.table(
            "brands",
            sa.column("slug", sa.String()),
            sa.column("name", sa.String()),
            sa.column("language", sa.String()),
            sa.column("timezone", sa.String()),
            sa.column("status", sa.String()),
            sa.column("active", sa.Boolean()),
            sa.column("created_at", sa.DateTime()),
            sa.column("updated_at", sa.DateTime()),
        ),
        [
            {
                "slug": slug,
                "name": name,
                "language": lang,
                "timezone": tz,
                "status": "draft",
                "active": False,
                "created_at": now,
                "updated_at": now,
            }
            for slug, name, lang, tz in _PLACEHOLDER_BRANDS
        ],
    )

    # 3. Add nullable brand_id_fk on each existing table -------------------
    for tbl in _affected_existing_tables():
        op.add_column(
            tbl,
            sa.Column("brand_id_fk", sa.Integer(), nullable=True),
        )
        # Backfill via subquery: brand_id_fk = (SELECT id FROM brands WHERE slug = old.brand_id)
        op.execute(
            sa.text(
                f"UPDATE {tbl} SET brand_id_fk = "
                f"(SELECT id FROM brands WHERE brands.slug = {tbl}.brand_id)"
            )
        )

    # 4. GUARD — abort with row counts if any FK is still NULL -------------
    null_counts: dict[str, int] = {}
    for tbl in _affected_existing_tables():
        result = bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {tbl} WHERE brand_id_fk IS NULL")
        )
        n = int(result.scalar() or 0)
        if n > 0:
            null_counts[tbl] = n
    if null_counts:
        raise RuntimeError(
            "Migration 002_brands_fk aborted: brand_id_fk backfill incomplete. "
            "Rows with NULL brand_id_fk per table: "
            + ", ".join(f"{t}={n}" for t, n in null_counts.items())
            + ". Likely cause: existing rows had brand_id values that don't "
            "match any seeded brand slug. Inspect admin.db before re-running."
        )

    # 5. Lock in: NOT NULL + FK constraint + drop old brand_id (string) ----

    # ---- sources ---------------------------------------------------------
    with op.batch_alter_table("sources", schema=None) as batch_op:
        batch_op.drop_index("ix_sources_brand_active")
        batch_op.drop_index(batch_op.f("ix_sources_brand_id"))
        batch_op.drop_column("brand_id")
        batch_op.alter_column(
            "brand_id_fk",
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch_op.create_foreign_key(
            "fk_sources_brand_id_fk_brands",
            "brands",
            ["brand_id_fk"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index(
            "ix_sources_brand_active", ["brand_id_fk", "active"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_sources_brand_id_fk"), ["brand_id_fk"], unique=False
        )

    # ---- prompts ---------------------------------------------------------
    with op.batch_alter_table("prompts", schema=None) as batch_op:
        batch_op.drop_index(
            "idx_active_prompt", sqlite_where=sa.text("is_active = 1")
        )
        batch_op.drop_index(batch_op.f("ix_prompts_brand_id"))
        batch_op.drop_column("brand_id")
        batch_op.alter_column(
            "brand_id_fk",
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch_op.create_foreign_key(
            "fk_prompts_brand_id_fk_brands",
            "brands",
            ["brand_id_fk"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index(
            "idx_active_prompt",
            ["brand_id_fk", "prompt_type"],
            unique=True,
            sqlite_where=sa.text("is_active = 1"),
        )
        batch_op.create_index(
            batch_op.f("ix_prompts_brand_id_fk"), ["brand_id_fk"], unique=False
        )

    # ---- pipeline_config -------------------------------------------------
    # pipeline_config had brand_id as PRIMARY KEY. Switch to brand_id_fk.
    with op.batch_alter_table("pipeline_config", schema=None) as batch_op:
        batch_op.drop_column("brand_id")
        batch_op.alter_column(
            "brand_id_fk",
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch_op.create_primary_key(
            "pk_pipeline_config", ["brand_id_fk"]
        )
        batch_op.create_foreign_key(
            "fk_pipeline_config_brand_id_fk_brands",
            "brands",
            ["brand_id_fk"],
            ["id"],
            ondelete="RESTRICT",
        )

    # ---- runs ------------------------------------------------------------
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_runs_brand_id"))
        batch_op.drop_column("brand_id")
        batch_op.alter_column(
            "brand_id_fk",
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch_op.create_foreign_key(
            "fk_runs_brand_id_fk_brands",
            "brands",
            ["brand_id_fk"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index(
            batch_op.f("ix_runs_brand_id_fk"), ["brand_id_fk"], unique=False
        )
        # Extend status CHECK to allow 'dry_run' (used by smoke tests).
        batch_op.drop_constraint("ck_runs_status", type_="check")
        batch_op.create_check_constraint(
            "ck_runs_status",
            "status IN ('running', 'success', 'failed', 'dry_run')",
        )

    op.execute(sa.text("PRAGMA foreign_keys=ON"))


def downgrade() -> None:
    """Reverse the multi-brand refactor.

    Recreates the string ``brand_id`` columns on each table, populates
    them from a join through ``brands.slug``, drops the FK + brands table.
    """
    op.execute(sa.text("PRAGMA foreign_keys=OFF"))

    # 1. Re-add string brand_id columns (nullable for backfill window) -----
    for tbl in _affected_existing_tables():
        op.add_column(tbl, sa.Column("brand_id", sa.String(), nullable=True))
        op.execute(
            sa.text(
                f"UPDATE {tbl} SET brand_id = "
                f"(SELECT slug FROM brands WHERE brands.id = {tbl}.brand_id_fk)"
            )
        )

    # 2. Reverse the table changes per table -------------------------------
    with op.batch_alter_table("sources", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sources_brand_id_fk"))
        batch_op.drop_index("ix_sources_brand_active")
        batch_op.drop_constraint(
            "fk_sources_brand_id_fk_brands", type_="foreignkey"
        )
        batch_op.drop_column("brand_id_fk")
        batch_op.alter_column(
            "brand_id", existing_type=sa.String(), nullable=False
        )
        batch_op.create_index(
            "ix_sources_brand_active", ["brand_id", "active"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_sources_brand_id"), ["brand_id"], unique=False
        )

    with op.batch_alter_table("prompts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_prompts_brand_id_fk"))
        batch_op.drop_index(
            "idx_active_prompt", sqlite_where=sa.text("is_active = 1")
        )
        batch_op.drop_constraint(
            "fk_prompts_brand_id_fk_brands", type_="foreignkey"
        )
        batch_op.drop_column("brand_id_fk")
        batch_op.alter_column(
            "brand_id", existing_type=sa.String(), nullable=False
        )
        batch_op.create_index(
            "idx_active_prompt",
            ["brand_id", "prompt_type"],
            unique=True,
            sqlite_where=sa.text("is_active = 1"),
        )
        batch_op.create_index(
            batch_op.f("ix_prompts_brand_id"), ["brand_id"], unique=False
        )

    with op.batch_alter_table("pipeline_config", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_pipeline_config_brand_id_fk_brands", type_="foreignkey"
        )
        batch_op.drop_constraint("pk_pipeline_config", type_="primary")
        batch_op.drop_column("brand_id_fk")
        batch_op.alter_column(
            "brand_id", existing_type=sa.String(), nullable=False
        )
        batch_op.create_primary_key("pk_pipeline_config", ["brand_id"])

    # The 'dry_run' status was introduced in this migration's upgrade.
    # Coerce any existing dry_run rows to 'success' before reinstating
    # the narrower CHECK constraint — otherwise SQLite refuses the
    # CREATE-TABLE step inside batch_alter_table.
    op.execute(sa.text("UPDATE runs SET status='success' WHERE status='dry_run'"))

    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_runs_brand_id_fk"))
        batch_op.drop_constraint(
            "fk_runs_brand_id_fk_brands", type_="foreignkey"
        )
        batch_op.drop_column("brand_id_fk")
        batch_op.alter_column(
            "brand_id", existing_type=sa.String(), nullable=False
        )
        batch_op.create_index(
            batch_op.f("ix_runs_brand_id"), ["brand_id"], unique=False
        )
        batch_op.drop_constraint("ck_runs_status", type_="check")
        batch_op.create_check_constraint(
            "ck_runs_status",
            "status IN ('running', 'success', 'failed')",
        )

    # 3. Finally, drop brands ----------------------------------------------
    op.drop_table("brands")

    op.execute(sa.text("PRAGMA foreign_keys=ON"))
