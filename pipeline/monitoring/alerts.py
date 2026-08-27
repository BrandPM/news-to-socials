"""Telegram push-alerts for pipeline failures + visibility (NTS_073/NTS_075).

Run on a 15-minute systemd timer (``nts-monitor.timer``). Each pass:

1. Sweeps every **active** brand and computes its notifications via the
   shared :func:`pipeline.admin.notifications_core.compute_notifications`
   — the same logic the ``/api/v1/notifications`` route serves.
2. Keeps the actionable ones — ``run_failed`` (danger) and
   ``source_unhealthy`` (warning).
3. Pushes the ones not already in the ``alert_sent`` dedup ledger to the
   monitoring chat via :meth:`TelegramPublisher._send_message`, then
   records them.
4. Optionally emits a single "recovered" message when previously-alerting
   notifications have cleared, and prunes their ledger rows.
5. NTS_075 — emits **pipeline-visibility** pulses so the manager sees the
   pipeline breathing in Telegram, not just failures:

   * ``run_started:{id}``    — a run entered ``status='running'``.
   * ``run_finished:{id}``   — a run reached ``success``/``cancelled``.
     ``failed`` is **deliberately excluded**: the NTS_073 ``run_failed``
     alert already owns failed runs, so a failed run gets exactly one
     message, never two.
   * ``published:{sanity_id}`` — a draft was published to Sanity.

   Each pulse has its own ``alert_sent`` key (so it sends once) and is
   windowed to the last 24h so the first tick after a deploy can't replay
   the whole backlog. These keys are never part of the "recovered"
   reconciliation — they are one-shot pulses, not clearable incidents.

6. NTS_088 — a **backup-heartbeat** check. If the daily admin.db backup's
   ``.last_ok`` heartbeat is missing or older than
   ``settings.backup_max_age_hours`` (26h), fire one ``backup_stale:DATE``
   alert. The dedup key carries today's date so at most one alert per
   calendar day. Also a one-shot pulse (never "recovered").

Safety contract (this runs unattended):

* If ``telegram_bot_token`` or ``telegram_monitoring_chat_id`` is empty it
  is a **no-op** + ``log.warning("alerts.telegram_not_configured")``.
* No exception escapes :func:`run_alerts`. A single send that fails is
  logged and skipped; its id is *not* recorded, so the next pass retries.
"""

from __future__ import annotations

import asyncio
import html
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from pipeline.admin.db import session_scope
from pipeline.admin.models import AlertSent, Brand, DraftApproval, Run
from pipeline.admin.notifications_core import compute_notifications
from pipeline.admin.schemas import NotificationItemOut
from pipeline.common.config import get_settings
from pipeline.common.logging import configure_logging, get_logger
from pipeline.publisher.telegram_bot import TelegramPublisher

log = get_logger(__name__)

# Public admin URL the deep-links point at. Severity → emoji prefix.
ALERT_BASE_URL = "https://iconpipe.com"
SEVERITY_EMOJI = {"danger": "🔴", "warning": "🟡", "resolved": "🟢"}

# Send at most this many individual alert messages per pass; the rest are
# folded into one "+N more" summary so a burst of failures can't spam the
# chat (Task 3: no "простыни").
MAX_INDIVIDUAL_ALERTS = 5

# Kinds the alerter pushes. draft_rejected (warning) is intentionally left
# to the in-app notifications list — it is not an ops incident.
ALERTABLE_KINDS = ("run_failed", "source_unhealthy")
_SEVERITY_RANK = {"danger": 0, "warning": 1}

# NTS_075 — visibility pulses. Their alert_sent keys carry these prefixes so
# we can tell them apart from the NTS_073 incident ids ("run-47", "source-3")
# and keep them OUT of the "recovered" reconciliation (one-shot pulses, not
# clearable incidents).
VISIBILITY_PREFIXES = ("run_started:", "run_finished:", "published:")

# NTS_088 — backup-heartbeat alert. Key is "backup_stale:YYYY-MM-DD" so it
# sends at most once per calendar day (the date rolls the dedup key). Like
# the visibility pulses it is a one-shot ledger entry, never part of the
# "recovered" reconciliation.
BACKUP_STALE_PREFIX = "backup_stale:"

# One-shot alert_sent keys that must NOT be treated as clearable incidents in
# the "recovered" reconciliation (visibility pulses + backup-stale pulses).
ONESHOT_PREFIXES = (*VISIBILITY_PREFIXES, BACKUP_STALE_PREFIX)

# Only look back this far when detecting visibility pulses — bounds the query
# and stops the first tick after a deploy from replaying the whole backlog.
VISIBILITY_WINDOW = timedelta(hours=24)

# Run.triggered_by → human label for the "Кто" line. Unknown values fall back
# to the escaped raw value.
_TRIGGER_LABELS = {
    "cron": "cron (по расписанию)",
    "manual": "менеджер (вручную)",
    "cli": "CLI",
}

# Terminal status → emoji for the run_finished pulse. 'failed' is rendered
# here for completeness/tests but is never enqueued (run_failed owns it).
_FINISHED_EMOJI = {"success": "✅", "cancelled": "⏹", "failed": "🔴"}


def _is_oneshot_key(notification_id: str) -> bool:
    """One-shot pulse keys (visibility + backup-stale) — excluded from the
    "recovered" reconciliation, which only clears real incidents."""
    return notification_id.startswith(ONESHOT_PREFIXES)


# --- Message rendering (pure, unit-tested) ---------------------------------


def _fmt_time(dt: datetime) -> str:
    """``YYYY-MM-DD HH:MM`` in UTC. Naive datetimes (SQLite) are treated as UTC."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M")


def _link(href: str, text: str) -> str:
    url = f"{ALERT_BASE_URL}{href}"
    # href is server-controlled ("/runs/47", "/sources"); text is too.
    return f'→ <a href="{html.escape(url, quote=True)}">{html.escape(text)}</a>'


def format_alert(item: NotificationItemOut, *, brand_name: str | None = None) -> str:
    """Render one notification as a short, phone-readable HTML message.

    parse_mode=HTML (never Markdown). All user-derived text (``title``,
    ``description``) is escaped via :func:`html.escape` so ``< > &`` in a
    log excerpt or source name can't break the markup.
    """
    emoji = SEVERITY_EMOJI.get(item.severity, "⚪️")
    title = html.escape(item.title)
    detail = html.escape(item.description)
    lines = [f"{emoji} <b>{title}</b>"]

    if item.kind == "source_unhealthy":
        # Brand folds into the detail line, matching the reference format.
        if brand_name:
            lines.append(f"Brand: {html.escape(brand_name)} · {detail}")
        else:
            lines.append(detail)
        lines.append(_link(item.href or "/sources", "Open sources"))
        return "\n".join(lines)

    # run_failed (and any other danger kind): brand on its own line, then
    # the one-line detail, then the event time, then the deep link.
    if brand_name:
        lines.append(f"Brand: {html.escape(brand_name)}")
    lines.append(detail)
    lines.append(f"🕓 {_fmt_time(item.created_at)} UTC")
    if item.kind == "run_failed" and item.href:
        run_no = item.href.rsplit("/", 1)[-1]
        lines.append(_link(item.href, f"Open run #{run_no}"))
    elif item.href:
        lines.append(_link(item.href, "Open"))
    return "\n".join(lines)


def format_summary(extra_count: int) -> str:
    """The "+N more" overflow message when a burst exceeds the per-pass cap."""
    return (
        f"{SEVERITY_EMOJI['danger']} <b>+{extra_count} more alert"
        f"{'s' if extra_count != 1 else ''}</b>\n"
        "More failures this pass — open the dashboard to review the rest.\n"
        + _link("/dashboard", "Open dashboard")
    )


def format_resolved(cleared_count: int) -> str:
    """Single consolidated "recovered" message when prior alerts have cleared."""
    return (
        f"{SEVERITY_EMOJI['resolved']} <b>Recovered</b>\n"
        f"{cleared_count} alert{'s' if cleared_count != 1 else ''} cleared "
        "since the last check."
    )


# --- Visibility pulses (NTS_075, pure renderers) ---------------------------


def _fmt_hm(dt: datetime) -> str:
    """``HH:MM`` in UTC. Naive datetimes (SQLite) are treated as UTC."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%H:%M")


def format_run_started(
    *, run_id: int, triggered_by: str, source_count: int, started_at: datetime
) -> str:
    """🚀 pulse when a run starts parsing."""
    who = _TRIGGER_LABELS.get(triggered_by, html.escape(triggered_by))
    return (
        "🚀 <b>Парсинг запущен</b>\n"
        f"Кто: {who}\n"
        f"Источников: {source_count}\n"
        f"🕓 {_fmt_hm(started_at)} UTC · Run #{run_id}"
    )


def format_run_finished(
    *,
    run_id: int,
    status: str,
    fetched: int,
    relevant: int,
    drafted: int,
    finished_at: datetime,
    deduped: int = 0,
    images_skipped: int = 0,
    thin: int = 0,
) -> str:
    """✅/🔴/⏹ pulse when a run reaches a terminal status.

    ``relevant`` is ``stats['scored']`` — the items that cleared the
    relevance threshold (``score >= min_score``) — out of ``fetched``.
    ``deduped`` (NTS_090) is ``stats['deduped']`` — near-duplicate topics
    skipped before generation; shown only when > 0.
    ``images_skipped`` (NTS_094) is ``stats['images_skipped']`` — covers the
    run deliberately did not generate because the brand runs images on
    demand. Reported so an on-demand run says something about images rather
    than silently saying nothing.
    ``thin`` (NTS_092) is ``stats['thin']`` — articles written WITHOUT a
    research fact pack, from the headline alone. Shown only when > 0, and
    deliberately loud: those articles came out at the pre-NTS_092 quality
    floor, and a run where every article is thin means research is broken,
    not that the news was quiet.
    """
    emoji = _FINISHED_EMOJI.get(status, "✅")
    lines = [
        f"{emoji} <b>Прогон завершён · Run #{run_id}</b>",
        f"Найдено релевантных: {relevant}/{fetched} · черновиков: {drafted}",
    ]
    if deduped > 0:
        lines.append(f"🔁 dedup: {deduped} skipped")
    if images_skipped > 0:
        lines.append(f"🖼 covers skipped: {images_skipped} (on demand)")
    if thin > 0:
        lines.append(f"⚠️ без ресёрча (thin): {thin}")
    lines.append(f"🕓 {_fmt_hm(finished_at)} UTC")
    return "\n".join(lines)


def format_published(
    *,
    title: str | None,
    language: str | None,
    live_url: str | None,
    published_at: datetime,
) -> str:
    """📤 pulse when a draft is published. ``title`` is user-derived → escaped."""
    safe_title = html.escape(title or "—")
    lang = html.escape((language or "—").upper())
    lines = [
        f'📤 <b>Опубликовано: "{safe_title}"</b>',
        f"Язык: {lang} · 🕓 {_fmt_hm(published_at)} UTC",
    ]
    if live_url:
        href = html.escape(live_url, quote=True)
        lines.append(f'→ <a href="{href}">{html.escape(live_url)}</a>')
    return "\n".join(lines)


# --- Backup heartbeat (NTS_088) --------------------------------------------


def format_backup_stale(*, last_ok: datetime | None, age_hours: float | None, max_age_hours: int) -> str:
    """🔴 alert when the daily admin.db backup heartbeat is missing/stale.

    ``last_ok`` is the timestamp parsed from the heartbeat file (``None`` if
    the file is absent or unparseable). ``age_hours`` is how old it is.
    """
    lines = ["🔴 <b>Бэкап admin.db не выполняется</b>"]
    if last_ok is None:
        lines.append("Последний успешный бэкап: <b>нет</b> (heartbeat отсутствует)")
    else:
        lines.append(f"Последний успешный бэкап: {_fmt_time(last_ok)} UTC")
        if age_hours is not None:
            lines.append(f"Возраст: {age_hours:.0f}ч (порог {max_age_hours}ч)")
    lines.append("Проверь nts-backup.timer на VPS: <code>systemctl status nts-backup.timer</code>")
    return "\n".join(lines)


def check_backup_heartbeat(
    *, now: datetime, heartbeat_path, max_age_hours: int
) -> tuple[str, str] | None:
    """Return a ``(alert_sent key, message)`` pulse if the backup is stale.

    Stale = heartbeat file missing/unreadable, OR its timestamp is older
    than ``max_age_hours``. The key is ``backup_stale:YYYY-MM-DD`` (today,
    UTC) so at most one alert fires per calendar day. Returns ``None`` when
    the backup is healthy. Never raises — a bad heartbeat file is treated
    as "stale" (fail loud), not swallowed.
    """
    from pathlib import Path  # noqa: PLC0415

    path = Path(heartbeat_path)
    key = f"{BACKUP_STALE_PREFIX}{now.strftime('%Y-%m-%d')}"
    last_ok: datetime | None = None
    age_hours: float | None = None

    try:
        raw = path.read_text().strip()
        last_ok = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if last_ok.tzinfo is None:
            last_ok = last_ok.replace(tzinfo=timezone.utc)
        age_hours = (now - last_ok).total_seconds() / 3600.0
    except (OSError, ValueError):
        # Missing or garbage heartbeat → stale by definition.
        return key, format_backup_stale(
            last_ok=None, age_hours=None, max_age_hours=max_age_hours
        )

    if age_hours > max_age_hours:
        return key, format_backup_stale(
            last_ok=last_ok, age_hours=age_hours, max_age_hours=max_age_hours
        )
    return None


# --- Visibility pulses (detection) -----------------------------------------


def _count_sources(source_ids_json: str | None) -> int:
    """``len()`` of the JSON-as-TEXT ``runs.source_ids`` list; 0 on garbage."""
    try:
        val = json.loads(source_ids_json or "[]")
    except (ValueError, TypeError):
        return 0
    return len(val) if isinstance(val, list) else 0


def _parse_stats(stats_json: str | None) -> dict:
    """``runs.stats`` is ``{fetched, scored, drafted, errors}`` JSON-as-TEXT."""
    if not stats_json:
        return {}
    try:
        val = json.loads(stats_json)
    except (ValueError, TypeError):
        return {}
    return val if isinstance(val, dict) else {}


def _gather_run_events(already: set[str]) -> list[tuple[str, str]]:
    """``run_started`` + ``run_finished`` pulses not yet in the ledger.

    ``failed`` runs are excluded from ``run_finished`` — the NTS_073
    ``run_failed`` alert owns them, so a failed run is never double-sent.
    """
    now = datetime.now(tz=timezone.utc)
    window_start = now - VISIBILITY_WINDOW
    out: list[tuple[str, str]] = []
    with session_scope() as session:
        running = (
            session.execute(
                select(Run)
                .where(Run.status == "running", Run.started_at >= window_start)
                .order_by(Run.started_at.desc())
                .limit(50)
            )
            .scalars()
            .all()
        )
        for r in running:
            key = f"run_started:{r.id}"
            if key in already:
                continue
            out.append(
                (
                    key,
                    format_run_started(
                        run_id=r.id,
                        triggered_by=r.triggered_by,
                        source_count=_count_sources(r.source_ids),
                        started_at=r.started_at,
                    ),
                )
            )

        finished = (
            session.execute(
                select(Run)
                .where(
                    Run.status.in_(("success", "cancelled")),
                    Run.finished_at.is_not(None),
                    Run.finished_at >= window_start,
                )
                .order_by(Run.finished_at.desc())
                .limit(50)
            )
            .scalars()
            .all()
        )
        for r in finished:
            key = f"run_finished:{r.id}"
            if key in already:
                continue
            stats = _parse_stats(r.stats)
            out.append(
                (
                    key,
                    format_run_finished(
                        run_id=r.id,
                        status=r.status,
                        fetched=int(stats.get("fetched", 0) or 0),
                        relevant=int(stats.get("scored", 0) or 0),
                        drafted=int(stats.get("drafted", 0) or 0),
                        deduped=int(stats.get("deduped", 0) or 0),
                        images_skipped=int(stats.get("images_skipped", 0) or 0),
                        thin=int(stats.get("thin", 0) or 0),
                        finished_at=r.finished_at,
                    ),
                )
            )
    return out


async def _fetch_published_meta(
    brand_id: int, sanity_published_id: str
) -> dict | None:
    """Resolve a published doc's ``title``/``language`` + public ``live_url``.

    The localised slug + title live only in Sanity, so this queries the
    brand's dataset and reuses ``_build_live_url`` for the canonical URL.
    Returns ``None`` (skip the pulse) if the brand has no creds or the
    query fails — never raises (the timer runs unattended).
    """
    # Lazy import: routes.drafts pulls in FastAPI; keep module import cheap.
    from pipeline.admin.routes.drafts import (  # noqa: PLC0415
        _build_live_url,
        _build_sanity_client_for_brand,
    )

    try:
        client, brand_slug = _build_sanity_client_for_brand(brand_id)
    except Exception:  # noqa: BLE001
        log.warning("alerts.published_no_client", brand_id=brand_id)
        return None
    groq = '*[_id == $id][0]{title, language, "slug": slug.current}'
    try:
        doc = await client.query(groq, {"id": sanity_published_id})  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        log.exception("alerts.published_query_failed", pub_id=sanity_published_id)
        return None
    if not isinstance(doc, dict):
        return None
    language = doc.get("language") or "en"
    return {
        "title": doc.get("title"),
        "language": language,
        "live_url": _build_live_url(brand_slug, language, doc.get("slug")),
    }


async def _gather_published_events(already: set[str]) -> list[tuple[str, str]]:
    """``published`` pulses for draft_approvals published in the last 24h.

    "Published" = ``published_at`` + ``sanity_published_id`` are set (the
    approve→publish step records both; ``status`` stays ``'approved'``).
    """
    now = datetime.now(tz=timezone.utc)
    window_start = now - VISIBILITY_WINDOW
    with session_scope() as session:
        rows = (
            session.execute(
                select(DraftApproval)
                .where(
                    DraftApproval.published_at.is_not(None),
                    DraftApproval.sanity_published_id.is_not(None),
                    DraftApproval.published_at >= window_start,
                )
                .order_by(DraftApproval.published_at.desc())
                .limit(50)
            )
            .scalars()
            .all()
        )
        # Detach the few primitives we need before the session closes.
        pending = [
            (a.sanity_published_id, a.brand_id_fk, a.published_at)
            for a in rows
            if f"published:{a.sanity_published_id}" not in already
        ]

    out: list[tuple[str, str]] = []
    for pub_id, brand_id, published_at in pending:
        meta = await _fetch_published_meta(brand_id, pub_id)
        if meta is None:
            continue
        out.append(
            (
                f"published:{pub_id}",
                format_published(
                    title=meta["title"],
                    language=meta["language"],
                    live_url=meta["live_url"],
                    published_at=published_at,
                ),
            )
        )
    return out


# --- Orchestration ---------------------------------------------------------


def _gather_alertable() -> list[tuple[NotificationItemOut, str | None]]:
    """Collect alertable notifications across all active brands.

    Returns ``(item, brand_name_or_None)`` tuples; ``brand_name`` is ``None``
    when only one brand is active (so single-brand setups don't see a noisy
    "Brand:" line), and the brand's name otherwise. Sorted danger-first,
    then newest-first — so the per-pass cap keeps the most urgent ones.
    """
    out: list[tuple[NotificationItemOut, str | None]] = []
    with session_scope() as session:
        brands = (
            session.execute(select(Brand).where(Brand.active.is_(True)))
            .scalars()
            .all()
        )
        multi = len(brands) > 1
        for brand in brands:
            for item in compute_notifications(session, brand.id):
                if item.kind in ALERTABLE_KINDS:
                    out.append((item, brand.name if multi else None))
    out.sort(
        key=lambda pair: (
            _SEVERITY_RANK.get(pair[0].severity, 9),
            -pair[0].created_at.timestamp(),
        )
    )
    return out


async def run_alerts(
    *,
    publisher: TelegramPublisher | None = None,
    send_resolved: bool = True,
) -> dict[str, list[str]]:
    """Compute, dedup, and push alerts. Never raises.

    Returns ``{"sent": [...], "resolved": [...], "skipped": bool}`` for
    tests/observability.
    """
    configure_logging()
    settings = get_settings()

    if not settings.telegram_bot_token or not settings.telegram_monitoring_chat_id:
        log.warning("alerts.telegram_not_configured")
        return {"sent": [], "resolved": [], "skipped": [True]}

    try:
        alertable = _gather_alertable()
    except Exception:  # noqa: BLE001 — unattended; never crash the timer
        log.exception("alerts.gather_failed")
        return {"sent": [], "resolved": [], "skipped": []}

    current = {item.id: (item, brand_name) for item, brand_name in alertable}

    with session_scope() as session:
        already = set(
            session.execute(select(AlertSent.notification_id)).scalars().all()
        )

    # Preserve the danger-first ordering from _gather_alertable.
    new_ids = [item.id for item, _ in alertable if item.id not in already]
    # Visibility pulses (run_started/finished/published) are one-shot — never
    # part of the "recovered" reconciliation, so exclude their keys here.
    gone_ids = [
        iid
        for iid in already
        if iid not in current and not _is_oneshot_key(iid)
    ]

    chat_id = settings.telegram_monitoring_chat_id
    publisher = publisher or TelegramPublisher()
    sent: list[str] = []

    # Send up to the cap individually; fold the overflow into one summary.
    head = new_ids[:MAX_INDIVIDUAL_ALERTS]
    overflow = new_ids[MAX_INDIVIDUAL_ALERTS:]
    for nid in head:
        item, brand_name = current[nid]
        try:
            await publisher._send_message(chat_id, format_alert(item, brand_name=brand_name))
        except Exception:  # noqa: BLE001
            log.exception("alerts.send_failed", notification_id=nid)
            continue
        sent.append(nid)

    if overflow:
        try:
            await publisher._send_message(chat_id, format_summary(len(overflow)))
            # The overflow ids are "handled" — record them so the next pass
            # doesn't re-summarize the same backlog.
            sent.extend(overflow)
        except Exception:  # noqa: BLE001
            log.exception("alerts.summary_send_failed", count=len(overflow))

    # NTS_075 — pipeline-visibility pulses. Own dedup keys, no cap (windowed
    # to 24h so volume is naturally bounded), gathered defensively.
    visibility: list[tuple[str, str]] = []
    try:
        visibility.extend(_gather_run_events(already))
        visibility.extend(await _gather_published_events(already))
    except Exception:  # noqa: BLE001 — unattended; never crash the timer
        log.exception("alerts.visibility_gather_failed")

    # NTS_088 — backup-heartbeat check. One-shot pulse (dedup key rolls daily),
    # gathered defensively so a filesystem hiccup can't crash the timer.
    try:
        pulse = check_backup_heartbeat(
            now=datetime.now(tz=timezone.utc),
            heartbeat_path=settings.backup_heartbeat_path,
            max_age_hours=settings.backup_max_age_hours,
        )
        if pulse is not None and pulse[0] not in already:
            visibility.append(pulse)
    except Exception:  # noqa: BLE001 — unattended; never crash the timer
        log.exception("alerts.backup_check_failed")
    for key, message in visibility:
        try:
            await publisher._send_message(chat_id, message)
        except Exception:  # noqa: BLE001
            log.exception("alerts.visibility_send_failed", key=key)
            continue
        sent.append(key)

    if sent:
        now = datetime.now(tz=timezone.utc)
        with session_scope() as session:
            for nid in sent:
                session.merge(AlertSent(notification_id=nid, sent_at=now))

    resolved: list[str] = []
    if send_resolved and gone_ids:
        try:
            await publisher._send_message(chat_id, format_resolved(len(gone_ids)))
        except Exception:  # noqa: BLE001
            log.exception("alerts.resolved_send_failed")
        else:
            resolved = list(gone_ids)
            with session_scope() as session:
                for nid in gone_ids:
                    obj = session.get(AlertSent, nid)
                    if obj is not None:
                        session.delete(obj)

    log.info("alerts.pass_complete", sent=len(sent), resolved=len(resolved))
    return {"sent": sent, "resolved": resolved, "skipped": []}


def main() -> None:
    asyncio.run(run_alerts())


if __name__ == "__main__":
    main()
