"""Seed admin.db with brands + the canonical Icon sources/prompts/config.

Idempotent: re-running won't create duplicates and NEVER overwrites
credentials of an existing brand row (operator may have edited them via
the UI). The script reports what it INSERTED, what it SKIPPED, and
exits 0 on success.

Usage:
    python -m scripts.seed_admin_db [--brand-slug icon] [--dry-run]

Make sure the schema is migrated first:

    alembic upgrade head

If ``BRANDS_ENCRYPTION_KEY`` is set in .env and the Sanity credentials
for Icon are present, they will be encrypted-on-insert. If the brand
row already exists, the Sanity credentials are NOT touched — see
``_seed_brands`` below.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.admin import seed_data
from pipeline.admin.db import get_session_factory
from pipeline.admin.models import Brand, PipelineConfig, Prompt, Source


@dataclass
class SeedReport:
    inserted: list[str]
    skipped: list[str]

    def print_to(self, stream) -> None:  # noqa: ANN001
        for x in self.inserted:
            stream.write(f"INSERT  {x}\n")
        for x in self.skipped:
            stream.write(f"SKIP    {x}\n")
        stream.write(
            f"\nDone. {len(self.inserted)} inserted, {len(self.skipped)} skipped.\n"
        )


# ---------------------------------------------------------------------------
# Brands
# ---------------------------------------------------------------------------


def _seed_brands(session: Session) -> tuple[list[str], list[str]]:
    """Seed Icon + 4 placeholders. Skip existing brands by slug — NEVER
    overwrite credentials of a brand that already exists."""
    inserted: list[str] = []
    skipped: list[str] = []
    now = datetime.now(tz=timezone.utc)

    # Icon — active, with encrypted Sanity creds pulled from .env at seed time.
    existing_icon = session.execute(
        select(Brand).where(Brand.slug == seed_data.ICON_BRAND_SLUG)
    ).scalar_one_or_none()
    if existing_icon is None:
        from pipeline.admin.encryption import get_encryption  # noqa: PLC0415
        from pipeline.common.config import get_settings  # noqa: PLC0415

        settings = get_settings()
        token_enc: str | None = None
        if settings.sanity_api_token:
            token_enc = get_encryption().encrypt(settings.sanity_api_token)
        has_creds = bool(token_enc and settings.sanity_project_id)
        session.add(
            Brand(
                slug=seed_data.ICON_BRAND_SLUG,
                name=seed_data.ICON_BRAND_NAME,
                language=seed_data.ICON_BRAND_LANGUAGE,
                timezone=seed_data.ICON_BRAND_TIMEZONE,
                status="active" if has_creds else "draft",
                active=has_creds,
                sanity_project_id=settings.sanity_project_id or None,
                sanity_dataset=settings.sanity_dataset or None,
                sanity_api_version=settings.sanity_api_version or "2024-01-01",
                sanity_api_token_enc=token_enc,
                sanity_studio_url=(
                    f"https://{settings.sanity_project_id}.sanity.studio/"
                    if settings.sanity_project_id
                    else None
                ),
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        inserted.append(f"brand {seed_data.ICON_BRAND_SLUG!r} (active, Sanity creds)")
    else:
        skipped.append(f"brand {seed_data.ICON_BRAND_SLUG!r}")

    # Placeholders — status='draft', no creds.
    for placeholder in seed_data.PLACEHOLDER_BRAND_SEEDS:
        existing = session.execute(
            select(Brand).where(Brand.slug == placeholder.slug)
        ).scalar_one_or_none()
        if existing is not None:
            skipped.append(f"brand {placeholder.slug!r}")
            continue
        session.add(
            Brand(
                slug=placeholder.slug,
                name=placeholder.name,
                language=placeholder.language,
                timezone=placeholder.timezone,
                status="draft",
                active=False,
                created_at=now,
                updated_at=now,
            )
        )
        inserted.append(f"brand {placeholder.slug!r} (draft placeholder)")
    session.flush()
    return inserted, skipped


# ---------------------------------------------------------------------------
# Sources / prompts / config — bound to the Icon brand row
# ---------------------------------------------------------------------------


def _get_brand_id(session: Session, slug: str) -> int:
    row = session.execute(select(Brand).where(Brand.slug == slug)).scalar_one()
    return row.id


def _seed_sources(session: Session, brand_id_fk: int) -> tuple[list[str], list[str]]:
    inserted: list[str] = []
    skipped: list[str] = []
    for s in seed_data.ICON_SEED_SOURCES:
        existing = session.execute(
            select(Source).where(
                Source.brand_id_fk == brand_id_fk, Source.url == s.url
            )
        ).scalar_one_or_none()
        if existing is not None:
            skipped.append(f"source {s.name!r} ({s.url})")
            continue
        session.add(
            Source(
                brand_id_fk=brand_id_fk,
                name=s.name,
                source_type=s.source_type,
                url=s.url,
                primary_category=s.primary_category,
                active=s.active,
                paywall=False,
                polling_minutes=s.polling_minutes,
            )
        )
        inserted.append(f"source {s.name!r} ({s.url})")
    return inserted, skipped


def _seed_prompts(session: Session, brand_id_fk: int) -> tuple[list[str], list[str]]:
    inserted: list[str] = []
    skipped: list[str] = []
    targets = [
        ("writer_polish", seed_data.get_active_polish_prompt()),
        ("writer_draft", seed_data.get_active_draft_prompt()),
        ("writer_translate", seed_data.get_active_translate_prompt()),
    ]
    for ptype, (version_name, content) in targets:
        existing = session.execute(
            select(Prompt).where(
                Prompt.brand_id_fk == brand_id_fk,
                Prompt.prompt_type == ptype,
                Prompt.version_name == version_name,
            )
        ).scalar_one_or_none()
        if existing is not None:
            skipped.append(f"prompt {ptype}/{version_name!r}")
            continue
        has_active = (
            session.execute(
                select(Prompt.id).where(
                    Prompt.brand_id_fk == brand_id_fk,
                    Prompt.prompt_type == ptype,
                    Prompt.is_active.is_(True),
                )
            ).first()
            is not None
        )
        session.add(
            Prompt(
                brand_id_fk=brand_id_fk,
                prompt_type=ptype,
                version_name=version_name,
                content=content,
                notes=(
                    "Seeded from pipeline/generator/comment_writer.py "
                    "(IT_PROJ_NTS_023). Edit through the admin UI."
                ),
                is_active=not has_active,
                created_by="seed",
            )
        )
        inserted.append(
            f"prompt {ptype}/{version_name!r}"
            + (" [active]" if not has_active else "")
        )
    return inserted, skipped


def _seed_config(session: Session, brand_id_fk: int) -> tuple[list[str], list[str]]:
    inserted: list[str] = []
    skipped: list[str] = []
    existing = session.get(PipelineConfig, brand_id_fk)
    if existing is not None:
        skipped.append(f"pipeline_config[{brand_id_fk}]")
        return inserted, skipped

    from pipeline.generator.comment_writer import parse_voice_guardrails  # noqa: PLC0415
    from pipeline.run import icon_brand_config  # noqa: PLC0415

    brand = icon_brand_config()
    banned, _examples = parse_voice_guardrails(brand.voice_profile_yaml)

    session.add(
        PipelineConfig(
            brand_id_fk=brand_id_fk,
            scoring_threshold=seed_data.ICON_SEED_THRESHOLD,
            topics_per_run=seed_data.ICON_SEED_TOPICS_PER_RUN,
            banned_phrases=json.dumps(banned, ensure_ascii=False),
            voice_profile=brand.voice_profile_yaml,
        )
    )
    inserted.append(f"pipeline_config[{brand_id_fk}]")
    return inserted, skipped


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def seed(brand_slug: str = "icon", *, dry_run: bool = False) -> SeedReport:
    """Seed admin.db (brands + Icon's sources/prompts/config). Idempotent.

    ``brand_slug`` selects which brand owns the seeded sources/prompts/
    config rows — for now we only seed Icon's data. Other brands ship
    with their own data once Andriy creates them via the UI.
    """
    factory = get_session_factory()
    inserted: list[str] = []
    skipped: list[str] = []
    with factory() as session:
        a, b = _seed_brands(session)
        inserted.extend(a)
        skipped.extend(b)

        brand_id_fk = _get_brand_id(session, brand_slug)

        a, b = _seed_sources(session, brand_id_fk)
        inserted.extend(a)
        skipped.extend(b)
        a, b = _seed_prompts(session, brand_id_fk)
        inserted.extend(a)
        skipped.extend(b)
        a, b = _seed_config(session, brand_id_fk)
        inserted.extend(a)
        skipped.extend(b)

        if dry_run:
            session.rollback()
        else:
            session.commit()
    return SeedReport(inserted=inserted, skipped=skipped)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--brand-slug",
        default="icon",
        help="Brand slug whose sources/prompts/config get seeded (default: icon)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report without committing"
    )
    args = parser.parse_args(argv)

    report = seed(brand_slug=args.brand_slug, dry_run=args.dry_run)
    report.print_to(sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
