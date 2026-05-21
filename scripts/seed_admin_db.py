"""Seed admin.db with the canonical Icon sources, prompts, and config.

Idempotent: re-running won't create duplicates. The script reports what
it INSERTED, what it SKIPPED (already present), and exits 0 on success.

Usage:
    python -m scripts.seed_admin_db [--brand icon] [--dry-run]

The script uses whatever DB path ``settings.admin_db_path`` resolves to
(default ``./admin.db``). Make sure the schema is migrated first:

    alembic upgrade head
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.admin import seed_data
from pipeline.admin.db import get_session_factory
from pipeline.admin.models import PipelineConfig, Prompt, Source


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


def _seed_sources(session: Session, brand_id: str) -> tuple[list[str], list[str]]:
    inserted: list[str] = []
    skipped: list[str] = []
    for s in seed_data.ICON_SEED_SOURCES:
        # Idempotency key: (brand_id, url). URL uniquely identifies a feed.
        existing = session.execute(
            select(Source).where(
                Source.brand_id == brand_id, Source.url == s.url
            )
        ).scalar_one_or_none()
        if existing is not None:
            skipped.append(f"source {s.name!r} ({s.url})")
            continue
        session.add(
            Source(
                brand_id=brand_id,
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


def _seed_prompts(session: Session, brand_id: str) -> tuple[list[str], list[str]]:
    inserted: list[str] = []
    skipped: list[str] = []
    targets = [
        ("writer_polish", seed_data.get_active_polish_prompt()),
        ("writer_draft", seed_data.get_active_draft_prompt()),
    ]
    for ptype, (version_name, content) in targets:
        # Idempotency: skip if a prompt with this (brand, type, version_name)
        # already exists.
        existing = session.execute(
            select(Prompt).where(
                Prompt.brand_id == brand_id,
                Prompt.prompt_type == ptype,
                Prompt.version_name == version_name,
            )
        ).scalar_one_or_none()
        if existing is not None:
            skipped.append(f"prompt {ptype}/{version_name!r}")
            continue

        # If no active prompt of this type exists yet, the new row becomes
        # the active one. Otherwise the operator activates it later in the
        # UI — we don't want to silently flip something they've curated.
        has_active = (
            session.execute(
                select(Prompt.id).where(
                    Prompt.brand_id == brand_id,
                    Prompt.prompt_type == ptype,
                    Prompt.is_active.is_(True),
                )
            ).first()
            is not None
        )
        session.add(
            Prompt(
                brand_id=brand_id,
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


def _seed_config(session: Session, brand_id: str) -> tuple[list[str], list[str]]:
    inserted: list[str] = []
    skipped: list[str] = []
    existing = session.get(PipelineConfig, brand_id)
    if existing is not None:
        skipped.append(f"pipeline_config[{brand_id}]")
        return inserted, skipped

    # Pull voice_profile YAML + banned_phrases from the live BrandConfig so
    # the seed always matches whatever shipped most recently.
    from pipeline.run import icon_brand_config  # noqa: PLC0415

    brand = icon_brand_config()
    # banned_phrases JSON is parsed out of the YAML for ease of editing
    # via the future Settings UI banned-phrases tag input.
    from pipeline.generator.comment_writer import parse_voice_guardrails  # noqa: PLC0415

    banned, _examples = parse_voice_guardrails(brand.voice_profile_yaml)

    session.add(
        PipelineConfig(
            brand_id=brand_id,
            scoring_threshold=seed_data.ICON_SEED_THRESHOLD,
            topics_per_run=seed_data.ICON_SEED_TOPICS_PER_RUN,
            banned_phrases=json.dumps(banned, ensure_ascii=False),
            voice_profile=brand.voice_profile_yaml,
        )
    )
    inserted.append(f"pipeline_config[{brand_id}]")
    return inserted, skipped


def seed(brand_id: str = "icon", *, dry_run: bool = False) -> SeedReport:
    """Seed admin.db. Returns what was inserted / skipped. Idempotent."""
    factory = get_session_factory()
    inserted: list[str] = []
    skipped: list[str] = []
    with factory() as session:
        a, b = _seed_sources(session, brand_id)
        inserted.extend(a)
        skipped.extend(b)
        a, b = _seed_prompts(session, brand_id)
        inserted.extend(a)
        skipped.extend(b)
        a, b = _seed_config(session, brand_id)
        inserted.extend(a)
        skipped.extend(b)
        if dry_run:
            session.rollback()
        else:
            session.commit()
    return SeedReport(inserted=inserted, skipped=skipped)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brand", default="icon", help="Brand slug (default: icon)")
    parser.add_argument(
        "--dry-run", action="store_true", help="Report without committing"
    )
    args = parser.parse_args(argv)

    report = seed(brand_id=args.brand, dry_run=args.dry_run)
    report.print_to(sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
