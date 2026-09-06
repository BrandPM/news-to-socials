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
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

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
VISIBILITY_PREFIXES = (
    "run_started:",
    "run_finished:",
    "published:",
    # NTS_106 §2 — the daily contour-1 funnel. A pulse, not an incident: the
    # numbers are always "true", so there is nothing for the recovered
    # reconciliation to clear.
    "intake_heartbeat:",
)

# NTS_088 — backup-heartbeat alert. Key is "backup_stale:YYYY-MM-DD" so it
# sends at most once per calendar day (the date rolls the dedup key). Like
# the visibility pulses it is a one-shot ledger entry, never part of the
# "recovered" reconciliation.
BACKUP_STALE_PREFIX = "backup_stale:"

# NTS_100 §3.5 / NTS_106 §3 — the three production pulses added in S4. All
# one-shot: their dedup keys carry a date or a candidate id, so the key rolls
# on its own and there is nothing for the "recovered" pass to clear.
#
# ``thin_portfolio`` is the one that has to fire EARLY: NTS_100 §3.5 says the
# alert goes out three days before a slot, not on the morning of it. A calendar
# that tells you it is empty on the day it is empty is a calendar, not an alert.
THIN_PORTFOLIO_PREFIX = "thin_portfolio:"
CANDIDATE_FAILED_PREFIX = "candidate_failed:"
SPEND_CAP_PREFIX = "spend_cap:"
# NTS_123 S6 — the close-retry rate. NTS_122 measured one retry costing more
# than the draft it followed ($0.0143 against $0.0009) on fake text; the
# directive asks for the share on real ones, and for a prompt fix through a
# reseed migration if it passes 30%.
CLOSE_RETRY_PREFIX = "close_retry_rate:"
# NTS_106 §5 — the dead-man switch: no heartbeat in the chat by 09:00 in the
# brand's timezone. The silence itself is the alert, because a monitoring
# channel that has gone quiet is indistinguishable from a quiet morning.
DEAD_MAN_PREFIX = "dead_man:"
DEAD_MAN_HOUR = 9
PRODUCTION_PREFIXES = (
    THIN_PORTFOLIO_PREFIX,
    CANDIDATE_FAILED_PREFIX,
    SPEND_CAP_PREFIX,
    CLOSE_RETRY_PREFIX,
    DEAD_MAN_PREFIX,
)

# The share above which the close prompt itself is the problem, not the story.
CLOSE_RETRY_ALERT_SHARE = 0.30
# Below this many drafts the ratio is noise — three retries out of four drafts
# is not evidence of anything.
CLOSE_RETRY_MIN_DRAFTS = 10

# NTS_106 §1 — how long a failed delivery waits before it is tried again, and
# how many times. Ten minutes is the spec's number; five attempts is roughly
# an hour of a dead Telegram, after which retrying is not the problem to solve.
ALERT_RETRY_AFTER = timedelta(minutes=10)
ALERT_MAX_ATTEMPTS = 5

# NTS_100 §3.5 — how far ahead the thin-portfolio pulse looks.
THIN_PORTFOLIO_LEAD_DAYS = 3

_WEEKDAY_INDEX = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

# One-shot alert_sent keys that must NOT be treated as clearable incidents in
# the "recovered" reconciliation (visibility pulses + backup-stale pulses).
ONESHOT_PREFIXES = (
    *VISIBILITY_PREFIXES,
    BACKUP_STALE_PREFIX,
    *PRODUCTION_PREFIXES,
)

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


# --- Intake heartbeat (NTS_106 §2) -----------------------------------------

INTAKE_HEARTBEAT_PREFIX = "intake_heartbeat:"


def format_intake_heartbeat(
    *,
    run_id: int | None,
    finished_at: datetime,
    stats: dict,
) -> str:
    """The daily contour-1 summary, in ABSOLUTE numbers (NTS_106 §2).

    "Молчаливый сбой интейка неотличим от строгой рубрики: портфель пуст, все
    думают, что рубрика режет." So this renders counts, per ``input_kind``, and
    never a bare percentage: ``accepted: 0`` under ``fetched: 0`` is a dead
    parser, ``accepted: 0`` under ``fetched: 340`` is an editorial question, and
    a rate of 0% reads identically in both cases.

    ``prefilter_drop_rate`` is printed with a 🟡 marker outside [0.3, 0.95]
    (NTS_099 §1: below means the prefilter is not filtering, above means it is
    eating the feed) and ``guard_error_rate`` with one above 20% (NTS_106 §1).
    """
    funnel = stats.get("funnel", {}) or {}
    by_kind = stats.get("by_input_kind", {}) or {}
    drop_rate_value = float(stats.get("prefilter_drop_rate", 0.0) or 0.0)
    guard_error_rate = float(stats.get("guard_error_rate", 0.0) or 0.0)
    considered = int(funnel.get("after_dedup", 0) or 0)

    lines = [f"📥 <b>Интейк · Run #{run_id if run_id is not None else '—'}</b>"]
    lines.append(
        "fetched {fetched} → after_dedup {dedup} → after_prefilter {pre} "
        "→ guarded {guarded} → accepted {acc}".format(
            fetched=int(funnel.get("fetched", 0) or 0),
            dedup=considered,
            pre=int(funnel.get("after_prefilter", 0) or 0),
            guarded=int(funnel.get("guarded", 0) or 0),
            acc=int(funnel.get("accepted", 0) or 0),
        )
    )
    for kind in sorted(by_kind):
        k = by_kind[kind] or {}
        lines.append(
            "· {kind}: fetched {f} · dedup {d} · prefilter {p} · guarded {g} "
            "· accepted {a} · rejected {r} · errors {e} · deferred {df}".format(
                kind=html.escape(kind),
                f=int(k.get("fetched", 0) or 0),
                d=int(k.get("after_dedup", 0) or 0),
                p=int(k.get("after_prefilter", 0) or 0),
                g=int(k.get("guarded", 0) or 0),
                a=int(k.get("accepted", 0) or 0),
                r=int(k.get("rejected", 0) or 0),
                e=int(k.get("guard_errors", 0) or 0),
                df=int(k.get("deferred", 0) or 0),
            )
        )
    drop_flag = (
        " 🟡"
        if considered >= 10 and (drop_rate_value < 0.3 or drop_rate_value > 0.95)
        else ""
    )
    lines.append(f"prefilter_drop_rate: {drop_rate_value:.2f}{drop_flag}")
    if guard_error_rate > 0:
        err_flag = " 🟡" if guard_error_rate > 0.20 else ""
        lines.append(f"guard_error_rate: {guard_error_rate:.2f}{err_flag}")
    cap_overflow = int(stats.get("cap_overflow", 0) or 0)
    if cap_overflow:
        lines.append(f"⛔ суточный лимит: {cap_overflow} (можно повысить вручную)")
    if int(stats.get("superseded", 0) or 0):
        lines.append(f"🔄 superseded: {stats['superseded']}")
    if int(stats.get("embed_failures", 0) or 0):
        lines.append(f"⚠️ embed failures: {stats['embed_failures']}")
    if int(stats.get("source_errors", 0) or 0):
        lines.append(f"🔴 источников с ошибкой: {stats['source_errors']}")
    reason_codes = stats.get("reason_codes", {}) or {}
    if reason_codes:
        top = sorted(reason_codes.items(), key=lambda kv: -int(kv[1]))[:6]
        lines.append(
            "причины: "
            + ", ".join(f"{html.escape(str(k))} {v}" for k, v in top)
        )
    lines.append(f"🕓 {_fmt_hm(finished_at)} UTC")
    return "\n".join(lines)


# --- Production pulses (NTS_100 §3.5, NTS_106 §3) — S4 ---------------------


def format_thin_portfolio(
    *,
    slot_date: date,
    capacity: int,
    in_pipeline: int,
    brand_name: str | None = None,
) -> str:
    """🟡 the slot in three days has less coming than it can hold.

    NTS_100 §3.5 is explicit that an empty portfolio is a *valid* outcome of a
    production run and must not be reported as a failure — the alert belongs to
    the calendar, three days out, when there is still time to promote something
    by hand. Reported in absolute numbers for the same reason the intake
    heartbeat is (NTS_106 §2): "0 of 2" and "1 of 2" call for different actions.
    """
    who = f" · {html.escape(brand_name)}" if brand_name else ""
    return "\n".join(
        [
            f"🟡 <b>Тонкий портфель{who}</b>",
            f"Слот {slot_date.isoformat()}: ёмкость {capacity}, "
            f"в работе {in_pipeline}",
            "Продвинь кандидата из «Портфеля» или прими, что выйдет меньше.",
            _link(f"{ALERT_BASE_URL}/portfolio", "Открыть портфель"),
        ]
    )


def format_candidate_failed(
    *,
    candidate_id: int,
    title: str,
    attempts: int,
    last_error: str | None,
    brand_name: str | None = None,
) -> str:
    """🔴 a candidate hit ``max_attempts`` and is terminal (NTS_100 §4)."""
    who = f" · {html.escape(brand_name)}" if brand_name else ""
    lines = [
        f"🔴 <b>Кандидат провалил производство{who}</b>",
        f"#{candidate_id} · {html.escape(title[:120])}",
        f"Попыток: {attempts} — дальше только вручную, из «Портфеля».",
    ]
    if last_error:
        lines.append(f"<code>{html.escape(last_error[:300])}</code>")
    lines.append(_link(f"{ALERT_BASE_URL}/portfolio", "Открыть портфель"))
    return "\n".join(lines)


def format_spend_cap(
    *,
    spent_usd: float,
    cap_usd: float,
    stopped: bool,
    brand_name: str | None = None,
) -> str:
    """🟡 at 80% of the monthly cap, 🔴 at 100% (NTS_106 §3).

    The two are one message with two faces on purpose: they are the same fact
    at two thresholds, and the second one has to say what actually stopped —
    production, not intake, which keeps running at cents a day.
    """
    who = f" · {html.escape(brand_name)}" if brand_name else ""
    pct = (spent_usd / cap_usd * 100.0) if cap_usd else 0.0
    head = "🔴 <b>Месячный кап исчерпан" if stopped else "🟡 <b>Месячный кап на исходе"
    lines = [
        f"{head}{who}</b>",
        f"Потрачено ${spent_usd:.2f} из ${cap_usd:.2f} ({pct:.0f}%)",
    ]
    lines.append(
        "Производство не стартует; интейк продолжает работать."
        if stopped
        else "Производство ещё идёт. Подними кап или подожди начала месяца."
    )
    lines.append(_link(f"{ALERT_BASE_URL}/settings", "Настройки"))
    return "\n".join(lines)


def check_thin_portfolio(
    *,
    brand_id_fk: int,
    slots: Any,
    timezone_name: str | None,
    now: datetime,
    brand_name: str | None = None,
) -> tuple[str, str] | None:
    """A pulse for the nearest slot exactly ``lead`` days out, if it is thin.

    Returns ``(alert_sent key, message)`` or ``None``. Counted against every
    candidate that could still reach that slot — ``ready``, ``drafted``,
    ``returned`` and ``in_production`` — because an article being written right
    now is not a hole in the calendar.
    """
    from pipeline.selector.candidate_lifecycle import parse_slots
    from pipeline.selector.candidate_store import resolve_timezone

    parsed = parse_slots(slots)
    if not parsed:
        return None
    weekdays: dict[int, int] = {}
    for entry in parsed:
        index = _WEEKDAY_INDEX[entry["day"]]
        weekdays[index] = weekdays.get(index, 0) + int(entry["capacity"])

    target = (
        now.astimezone(resolve_timezone(timezone_name)).date()
        + timedelta(days=THIN_PORTFOLIO_LEAD_DAYS)
    )
    capacity = weekdays.get(target.weekday())
    if not capacity:
        return None

    from pipeline.admin.models import Candidate

    with session_scope() as session:
        in_pipeline = len(
            session.execute(
                select(Candidate.id).where(
                    Candidate.brand_id_fk == brand_id_fk,
                    Candidate.status.in_(
                        ("in_production", "drafted", "returned", "ready")
                    ),
                )
            ).all()
        )
    if in_pipeline >= capacity:
        return None
    return (
        f"{THIN_PORTFOLIO_PREFIX}{brand_id_fk}:{target.isoformat()}",
        format_thin_portfolio(
            slot_date=target,
            capacity=capacity,
            in_pipeline=in_pipeline,
            brand_name=brand_name,
        ),
    )


def close_retry_rate(
    brand_id_fk: int, *, days: int = 30, now: datetime | None = None
) -> tuple[int, int, float]:
    """``(drafts, retries, share)`` from ``cost_records`` over a window.

    Counted from the accounting table rather than from the log, because the
    accounting table is the one that survives a log rotation and is already the
    source for every other cost number the operator reads.
    """
    from sqlalchemy import func

    from pipeline.admin.models import CostRecord

    now = now or datetime.now(tz=UTC)
    since = (now - timedelta(days=days)).replace(tzinfo=None)
    with session_scope() as session:
        rows = session.execute(
            select(CostRecord.operation, func.count(CostRecord.id))
            .where(
                CostRecord.brand_id_fk == brand_id_fk,
                CostRecord.created_at >= since,
                CostRecord.operation.in_(("draft", "generic_close_retry")),
            )
            .group_by(CostRecord.operation)
        ).all()
    counts = {operation: int(count) for operation, count in rows}
    drafts = counts.get("draft", 0)
    retries = counts.get("generic_close_retry", 0)
    return drafts, retries, (retries / drafts if drafts else 0.0)


def format_close_retry_rate(
    *,
    drafts: int,
    retries: int,
    share: float,
    days: int,
    brand_name: str | None = None,
) -> str:
    """🟡 the close prompt is being retried more often than it should be."""
    who = f" · {html.escape(brand_name)}" if brand_name else ""
    return "\n".join(
        [
            f"🟡 <b>Концовки переписываются слишком часто{who}</b>",
            f"{retries} повторов на {drafts} черновиков за {days} дней "
            f"({share:.0%}, порог {CLOSE_RETRY_ALERT_SHARE:.0%})",
            "Повтор дороже черновика. Правим промпт close (NTS_067) "
            "миграцией пересева — это не единичный случай.",
            _link(f"{ALERT_BASE_URL}/editorial", "Редполитика"),
        ]
    )


def record_intent(notification_id: str, message: str) -> None:
    """Write the alert down BEFORE trying to send it (NTS_106 §1).

    The ordering is the whole point. Recording after a successful send — which
    is what this table did until S7 — means an alert raised while Telegram is
    down leaves no trace and is never retried: the row that would have said
    "this needs saying" is the row the failure prevented.
    """
    with session_scope() as session:
        row = session.get(AlertSent, notification_id)
        if row is None:
            session.add(
                AlertSent(
                    notification_id=notification_id,
                    sent_at=datetime.now(tz=UTC),
                    delivered=False,
                    attempts=0,
                    message=message,
                )
            )
        elif not row.delivered:
            row.message = message


def mark_delivery(notification_id: str, *, delivered: bool) -> None:
    """Record the outcome of one send attempt."""
    with session_scope() as session:
        row = session.get(AlertSent, notification_id)
        if row is None:
            return
        row.delivered = delivered
        row.attempts = int(row.attempts or 0) + 1
        row.last_attempt_at = datetime.now(tz=UTC)


def pending_deliveries(*, now: datetime | None = None) -> list[tuple[str, str]]:
    """Alerts that were recorded, never landed, and are due for another try.

    Ten minutes between attempts and five attempts in all (NTS_106 §1). Past
    that, retrying is not the problem to solve — the channel is down, and the
    dead-man switch is what covers a channel that stays down.
    """
    now = now or datetime.now(tz=UTC)
    out: list[tuple[str, str]] = []
    with session_scope() as session:
        rows = (
            session.execute(
                select(AlertSent).where(
                    AlertSent.delivered.is_(False),
                    AlertSent.attempts < ALERT_MAX_ATTEMPTS,
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            if not row.message:
                continue
            last = row.last_attempt_at
            if last is not None:
                if last.tzinfo is None:
                    last = last.replace(tzinfo=UTC)
                if now - last < ALERT_RETRY_AFTER:
                    continue
            out.append((row.notification_id, row.message))
    return out


def check_dead_man(
    *,
    brand_id_fk: int,
    timezone_name: str | None,
    now: datetime,
    brand_name: str | None = None,
) -> tuple[str, str] | None:
    """No heartbeat in the chat by 09:00 in the brand's timezone (NTS_106 §5).

    The silence is the alert. A monitoring channel that has gone quiet looks
    exactly like a quiet morning, and telling those two apart is the single
    thing this switch exists for — so it fires on the *absence* of a delivered
    pulse, not on any failure signal.
    """
    from pipeline.selector.candidate_store import resolve_timezone

    local = now.astimezone(resolve_timezone(timezone_name))
    if local.hour < DEAD_MAN_HOUR:
        return None
    day_start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = day_start_local.astimezone(UTC).replace(tzinfo=None)
    with session_scope() as session:
        delivered_today = session.execute(
            select(AlertSent.notification_id).where(
                AlertSent.delivered.is_(True),
                AlertSent.sent_at >= day_start,
            )
        ).first()
    if delivered_today is not None:
        return None
    key = f"{DEAD_MAN_PREFIX}{brand_id_fk}:{local.date().isoformat()}"
    who = f" · {html.escape(brand_name)}" if brand_name else ""
    message = "\n".join(
        [
            f"🔴 <b>Сводки сегодня не было{who}</b>",
            f"До {DEAD_MAN_HOUR:02d}:00 ({timezone_name or 'UTC'}) в канал не "
            "пришло ни одного сообщения.",
            "Возможно, таймеры не отработали, а не «всё тихо».",
            "<code>systemctl list-timers --all | grep nts-</code>",
        ]
    )
    return key, message


def _gather_production_events(
    already: set[str], *, now: datetime | None = None
) -> list[tuple[str, str]]:
    """Failed candidates, thin slots and the spend cap, for every active brand.

    Pull-based like every other gatherer here: the production run itself only
    writes rows and logs, and the monitoring pass decides what is worth a
    message. That keeps a Telegram outage from being able to fail a run, and it
    means an alert missed while the bot was down is re-detected on the next
    pass rather than lost (NTS_106 §1).
    """
    from pipeline.admin.models import Candidate, PipelineConfig
    from pipeline.selector.candidate_lifecycle import monthly_spend_usd

    now = now or datetime.now(tz=UTC)
    out: list[tuple[str, str]] = []
    with session_scope() as session:
        brands = (
            session.execute(select(Brand).where(Brand.active.is_(True)))
            .scalars()
            .all()
        )
        multi = len(brands) > 1
        rows = [
            (
                b.id,
                b.name if multi else None,
                session.get(PipelineConfig, b.id),
            )
            for b in brands
        ]
        configs = [
            (
                bid,
                name,
                getattr(cfg, "publication_slots", None),
                getattr(cfg, "brand_timezone", None),
                float(getattr(cfg, "monthly_spend_cap_usd", 0.0) or 0.0),
            )
            for bid, name, cfg in rows
        ]
        failures = (
            session.execute(
                select(
                    Candidate.id,
                    Candidate.brand_id_fk,
                    Candidate.source_title,
                    Candidate.attempts,
                    Candidate.last_error,
                ).where(
                    Candidate.status == "failed",
                    Candidate.failed_at.is_not(None),
                    Candidate.failed_at >= (now - VISIBILITY_WINDOW).replace(tzinfo=None),
                )
            )
        ).all()

    names = {bid: name for bid, name, *_ in configs}
    for cid, brand_id, title, attempts, last_error in failures:
        key = f"{CANDIDATE_FAILED_PREFIX}{cid}"
        if key in already:
            continue
        out.append(
            (
                key,
                format_candidate_failed(
                    candidate_id=int(cid),
                    title=title or "(untitled)",
                    attempts=int(attempts or 0),
                    last_error=last_error,
                    brand_name=names.get(brand_id),
                ),
            )
        )

    for brand_id, name, slots, tz_name, cap in configs:
        dead_man = check_dead_man(
            brand_id_fk=brand_id,
            timezone_name=tz_name,
            now=now,
            brand_name=name,
        )
        if dead_man is not None and dead_man[0] not in already:
            out.append(dead_man)
        pulse = check_thin_portfolio(
            brand_id_fk=brand_id,
            slots=slots,
            timezone_name=tz_name,
            now=now,
            brand_name=name,
        )
        if pulse is not None and pulse[0] not in already:
            out.append(pulse)
        if cap <= 0:
            continue
        drafts, retries, share = close_retry_rate(brand_id, now=now)
        if drafts >= CLOSE_RETRY_MIN_DRAFTS and share > CLOSE_RETRY_ALERT_SHARE:
            key = f"{CLOSE_RETRY_PREFIX}{brand_id}:{now.strftime('%Y-%W')}"
            if key not in already:
                out.append(
                    (
                        key,
                        format_close_retry_rate(
                            drafts=drafts,
                            retries=retries,
                            share=share,
                            days=30,
                            brand_name=name,
                        ),
                    )
                )
        spent = monthly_spend_usd(brand_id, now=now)
        # Two thresholds, one key per month per threshold: 80% is a warning
        # while there is still room to act, 100% is the kill-switch reporting
        # that it fired.
        for threshold, stopped in ((1.0, True), (0.8, False)):
            if spent < cap * threshold:
                continue
            key = (
                f"{SPEND_CAP_PREFIX}{brand_id}:{now.strftime('%Y-%m')}:"
                f"{int(threshold * 100)}"
            )
            if key not in already:
                out.append(
                    (
                        key,
                        format_spend_cap(
                            spent_usd=spent,
                            cap_usd=cap,
                            stopped=stopped,
                            brand_name=name,
                        ),
                    )
                )
            break
    return out


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
            stats = _parse_stats(r.stats)
            # An intake run gets the contour-1 heartbeat instead of the
            # generation pulse: "релевантных 0/340, черновиков 0" is true of
            # every intake run and says nothing, while the funnel is the whole
            # point of the shadow week (NTS_106 §2).
            if r.run_type == "intake":
                key = f"{INTAKE_HEARTBEAT_PREFIX}{r.id}"
                if key in already or r.finished_at is None:
                    continue
                out.append(
                    (
                        key,
                        format_intake_heartbeat(
                            run_id=r.id,
                            finished_at=r.finished_at,
                            stats=stats,
                        ),
                    )
                )
                continue
            key = f"run_finished:{r.id}"
            if key in already:
                continue
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

    async def deliver(notification_id: str, message: str) -> bool:
        """Record the intent, try to send, record the outcome (NTS_106 §1).

        The intent is written first, on purpose: an alert raised while Telegram
        is unreachable must leave a row saying it needs saying, or it is lost
        with no trace and nothing to retry. That was the state NTS_122 §8 found.
        """
        record_intent(notification_id, message)
        try:
            await publisher._send_message(chat_id, message)
        except Exception:
            log.exception("alerts.send_failed", notification_id=notification_id)
            mark_delivery(notification_id, delivered=False)
            return False
        mark_delivery(notification_id, delivered=True)
        return True

    # Send up to the cap individually; fold the overflow into one summary.
    head = new_ids[:MAX_INDIVIDUAL_ALERTS]
    overflow = new_ids[MAX_INDIVIDUAL_ALERTS:]
    for nid in head:
        item, brand_name = current[nid]
        if await deliver(nid, format_alert(item, brand_name=brand_name)):
            sent.append(nid)

    if overflow:
        summary_key = f"summary:{datetime.now(tz=UTC).strftime('%Y-%m-%dT%H:%M')}"
        if await deliver(summary_key, format_summary(len(overflow))):
            # The overflow ids are "handled" — record them so the next pass
            # doesn't re-summarize the same backlog.
            sent.extend(overflow)

    # NTS_075 — pipeline-visibility pulses. Own dedup keys, no cap (windowed
    # to 24h so volume is naturally bounded), gathered defensively.
    visibility: list[tuple[str, str]] = []
    try:
        visibility.extend(_gather_run_events(already))
        visibility.extend(await _gather_published_events(already))
    except Exception:  # noqa: BLE001 — unattended; never crash the timer
        log.exception("alerts.visibility_gather_failed")

    # NTS_100 §3.5 / NTS_106 §3 — production pulses. Gathered in their own try
    # so a schema that predates migration 026 costs these three alerts and not
    # the whole pass.
    try:
        visibility.extend(_gather_production_events(already))
    except Exception:  # unattended; never crash the timer
        log.exception("alerts.production_gather_failed")

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
    # NTS_106 §1 — anything that failed to land on an earlier pass is tried
    # again before anything new, so a recovered channel drains its backlog in
    # the order it happened rather than the order the next pass discovers.
    try:
        retries = pending_deliveries()
    except Exception:
        log.exception("alerts.retry_gather_failed")
        retries = []
    for key, message in retries:
        if await deliver(key, message):
            log.info("alerts.redelivered", key=key)

    for key, message in visibility:
        if await deliver(key, message):
            sent.append(key)

    # The ledger rows are written by ``deliver`` itself, before each send —
    # a merge here would overwrite ``attempts`` and ``delivered`` with the
    # defaults and undo exactly the bookkeeping this pass exists to keep.

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
