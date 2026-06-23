"""Telegram push-alerts for pipeline failures (IT_PROJ_NTS_073).

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

Safety contract (this runs unattended):

* If ``telegram_bot_token`` or ``telegram_monitoring_chat_id`` is empty it
  is a **no-op** + ``log.warning("alerts.telegram_not_configured")``.
* No exception escapes :func:`run_alerts`. A single send that fails is
  logged and skipped; its id is *not* recorded, so the next pass retries.

This module deliberately does **not** touch the legacy Directus
``daily_summary.py``.
"""

from __future__ import annotations

import asyncio
import html
from datetime import datetime, timezone

from sqlalchemy import select

from pipeline.admin.db import session_scope
from pipeline.admin.models import AlertSent, Brand
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
    gone_ids = [iid for iid in already if iid not in current]

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
