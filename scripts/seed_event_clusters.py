"""Seed event clusters from sourced material (NTS_131 P3 Ф3).

The feeds carried two press releases and one document in a week; the
directive says not to wait for them. So the material is read off the
regulators' sites directly, one JSON block per event, and this script turns
each block into:

* one **candidate** — ``input_kind='document'``, the primary document as
  ``primary_doc_url``, ``doc_match='manual'`` (a person chose this document,
  which is what ``manual`` means in NTS_123), accepted, ``pending``;
* one **event cluster** anchored on it, with every other material as a
  cluster source (``origin='sourcing'``).

It writes only to the database ``--db`` names and refuses the production path,
so it cannot be pointed at the live admin.db by accident.

Input::

    [{"title": "...", "summary": "...", "date": "2026-09-29",
      "regulator": "FINMA", "source_name": "FINMA News EN",
      "service": "wealth", "jurisdictions": ["CH"], "stage": "ruling",
      "primary": "https://...", "primary_hint": "...",
      "sources": [{"url": "...", "publisher": "...", "role": "secondary",
                   "title": "..."}]}]

Prints ``{"title": ..., "candidate_id": ...}`` per event.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import typer

app = typer.Typer(add_completion=False)

_FORBIDDEN = ("/opt/news-to-socials",)


@app.command()
def main(
    events_file: Path = typer.Argument(..., exists=True, readable=True),
    db: Path = typer.Option(..., "--db", help="admin.db to seed (never prod)"),
    brand: str = typer.Option("icon", "--brand"),
) -> None:
    if any(str(db.resolve()).startswith(p) for p in _FORBIDDEN):
        typer.echo("refusing to seed the production database", err=True)
        raise typer.Exit(2)
    os.environ["ADMIN_DB_PATH"] = str(db.resolve())

    from pipeline.admin import db as admin_db
    from pipeline.admin.models import Brand, Candidate, Source
    from pipeline.common import config as config_module
    from pipeline.selector.candidate_store import CandidateInput, create_candidate
    from pipeline.selector.event_cluster import add_source, ensure_cluster

    config_module._settings = None
    admin_db.reset_for_tests()

    events = json.loads(events_file.read_text())
    with admin_db.get_session_factory()() as session:
        brand_row = session.query(Brand).filter(Brand.slug == brand).one()
        brand_id = int(brand_row.id)
        sources = {
            s.name: (int(s.id), s.source_class)
            for s in session.query(Source).filter(Source.brand_id_fk == brand_id)
        }

    out = []
    for event in events:
        name = event.get("source_name", "")
        if name and name not in sources and event.get("source_site"):
            # A regulator with no feed row yet. Registered as an INACTIVE
            # primary site: it gives the candidate its licence class (an
            # official act may be quoted with attribution) without making
            # intake start polling a URL nobody verified as a feed.
            sources[name] = _register_site(brand_id, name, event["source_site"])
        source_id, source_class = sources.get(name, (None, None))
        published = datetime.fromisoformat(event["date"]).replace(tzinfo=UTC)
        cid = create_candidate(
            CandidateInput(
                brand_id_fk=brand_id,
                input_kind="document",
                source_id_fk=source_id,
                source_title=event["title"],
                source_summary=event.get("summary"),
                source_url=event["primary"],
                source_published_at=published,
                source_language=event.get("language", "en"),
                source_name=event.get("regulator"),
                source_class=source_class or "regulator",
                topic_embedding_ref=None,
                verdict="accept",
                reason_code="ok",
                reason=f"NTS_131 P3 sourcing: {event.get('why', event['title'])}"[:200],
                confidence=0.9,
                service_category=event.get("service"),
                jurisdictions=tuple(event.get("jurisdictions", ())),
                event_stage=event.get("stage", "other"),
                depth_prior="article",
                primary_doc_hint=event.get("primary_hint"),
                primary_doc_url=event["primary"],
                doc_language_expected=event.get("language", "en"),
            ),
            now=datetime.now(tz=UTC),
        )
        if cid is None:
            typer.echo(f"not created: {event['title']}", err=True)
            continue
        with admin_db.get_session_factory()() as session:
            row = session.get(Candidate, cid)
            assert row is not None
            row.doc_match = "manual"
            session.commit()
        ensure_cluster(anchor_candidate_id=cid)
        added = 0
        for src in event.get("sources", []):
            added += bool(
                add_source(
                    anchor_candidate_id=cid,
                    url=src["url"],
                    title=src.get("title", ""),
                    publisher=src.get("publisher", ""),
                    role=src.get("role", "secondary"),
                    origin="sourcing",
                    max_sources=None,
                )
            )
        out.append({"title": event["title"], "candidate_id": cid, "sources": added})
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def _register_site(brand_id: int, name: str, url: str) -> tuple[int, str]:
    from pipeline.admin import db as admin_db
    from pipeline.admin.models import Source

    now = datetime.now(tz=UTC).replace(tzinfo=None)
    with admin_db.get_session_factory()() as session:
        row = Source(
            name=name,
            source_type="web",
            url=url,
            primary_category="regulation",
            active=False,
            paywall=False,
            polling_minutes=1440,
            created_at=now,
            updated_at=now,
            brand_id_fk=brand_id,
            source_role="primary_site",
            source_class="regulator",
            license_class="public_official",
        )
        session.add(row)
        session.commit()
        return int(row.id), "regulator"


if __name__ == "__main__":  # pragma: no cover
    app()
