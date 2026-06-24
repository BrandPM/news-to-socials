"""Tests for the Telegram push-alerter (IT_PROJ_NTS_073).

Covers:
* ``format_alert`` rendering + HTML escaping of user-derived text.
* ``run_alerts`` dedup — a second pass does not re-send the same alert.
* the no-op + warning when Telegram is not configured.
* the consolidated "recovered" message when an alert clears.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from pipeline.admin import db as admin_db
from pipeline.admin.models import AlertSent, DraftApproval, Run
from pipeline.admin.schemas import NotificationItemOut
from pipeline.common import config as config_module
from pipeline.monitoring import alerts
from tests.unit.conftest import seed_brand, seed_icon_brand


# --- format_alert ----------------------------------------------------------


def _run_item(**kw) -> NotificationItemOut:
    defaults = dict(
        id="run-47",
        kind="run_failed",
        severity="danger",
        title="Run #47 failed",
        description="RBC News — request timeout after 20s",
        href="/runs/47",
        created_at=datetime(2026, 6, 23, 12, 45, tzinfo=timezone.utc),
    )
    defaults.update(kw)
    return NotificationItemOut(**defaults)


def test_format_run_failed_matches_reference() -> None:
    msg = alerts.format_alert(_run_item(), brand_name="Icon")
    assert msg.startswith("🔴 <b>Run #47 failed</b>")
    assert "Brand: Icon" in msg
    assert "RBC News — request timeout after 20s" in msg
    assert "🕓 2026-06-23 12:45 UTC" in msg
    assert '<a href="https://iconpipe.com/runs/47">Open run #47</a>' in msg


def test_format_run_failed_single_brand_hides_brand_line() -> None:
    msg = alerts.format_alert(_run_item(), brand_name=None)
    assert "Brand:" not in msg


def test_format_source_unhealthy() -> None:
    item = NotificationItemOut(
        id="source-3",
        kind="source_unhealthy",
        severity="warning",
        title='Source “RBC News” unhealthy',
        description="6/8 fetches failed in the last 7 days (25% success).",
        href="/sources",
        created_at=datetime(2026, 6, 23, 12, 45, tzinfo=timezone.utc),
    )
    msg = alerts.format_alert(item, brand_name="Icon")
    assert msg.startswith("🟡 <b>")
    assert "Brand: Icon · 6/8 fetches failed" in msg
    assert '<a href="https://iconpipe.com/sources">Open sources</a>' in msg


def test_format_alert_escapes_html_special_chars() -> None:
    """A log excerpt with < > & " must not break the HTML markup."""
    item = _run_item(
        description='timeout <script>alert("x")</script> & co',
        title="Run #1 <broke>",
    )
    msg = alerts.format_alert(item, brand_name='A & B "Co"')
    # No raw angle brackets from user text leak through.
    assert "<script>" not in msg
    assert "&lt;script&gt;" in msg
    assert "&amp;" in msg
    assert "Run #1 &lt;broke&gt;" in msg
    assert "Brand: A &amp; B" in msg
    # Our own structural tags survive.
    assert msg.startswith("🔴 <b>")
    assert "</b>" in msg


def test_format_resolved_and_summary() -> None:
    assert alerts.format_resolved(1).startswith("🟢 <b>Recovered</b>")
    assert "1 alert cleared" in alerts.format_resolved(1)
    assert "3 alerts cleared" in alerts.format_resolved(3)
    assert "+2 more alerts" in alerts.format_summary(2)


# --- run_alerts (dedup / config / resolved) --------------------------------


class FakePublisher:
    """Stand-in for TelegramPublisher — records messages instead of sending."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def _send_message(self, chat_id: str, html: str) -> str:
        self.sent.append((chat_id, html))
        return str(len(self.sent))


@pytest.fixture
def alert_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-token")
    monkeypatch.setenv("TELEGRAM_MONITORING_CHAT_ID", "-100123")
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    factory = admin_db.get_session_factory()
    with factory() as session:
        brand_id = seed_icon_brand(session)
        session.commit()
    yield brand_id
    admin_db.reset_for_tests()


def _add_failed_run(brand_id: int) -> int:
    factory = admin_db.get_session_factory()
    with factory() as session:
        run = Run(
            brand_id_fk=brand_id,
            triggered_by="manual",
            source_ids="[]",
            started_at=datetime.now(tz=timezone.utc),
            status="failed",
            log_excerpt="connection reset by peer",
        )
        session.add(run)
        session.commit()
        return run.id


def test_dedup_does_not_resend(alert_env) -> None:
    brand_id = alert_env
    run_id = _add_failed_run(brand_id)

    fake1 = FakePublisher()
    res1 = asyncio.run(alerts.run_alerts(publisher=fake1))
    assert res1["sent"] == [f"run-{run_id}"]
    assert len(fake1.sent) == 1
    assert fake1.sent[0][0] == "-100123"

    # Ledger recorded.
    factory = admin_db.get_session_factory()
    with factory() as session:
        ids = set(session.execute(select(AlertSent.notification_id)).scalars().all())
    assert ids == {f"run-{run_id}"}

    # Second pass: the same failed run is still present → no re-send.
    fake2 = FakePublisher()
    res2 = asyncio.run(alerts.run_alerts(publisher=fake2))
    assert res2["sent"] == []
    assert fake2.sent == []


def test_not_configured_is_noop(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_MONITORING_CHAT_ID", "")
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    fake = FakePublisher()
    res = asyncio.run(alerts.run_alerts(publisher=fake))
    assert res["skipped"]
    assert fake.sent == []
    admin_db.reset_for_tests()


def test_resolved_message_when_alert_clears(alert_env) -> None:
    brand_id = alert_env
    run_id = _add_failed_run(brand_id)

    # First pass alerts and records.
    asyncio.run(alerts.run_alerts(publisher=FakePublisher()))

    # The run is fixed (status flips to success) → notification disappears.
    factory = admin_db.get_session_factory()
    with factory() as session:
        run = session.get(Run, run_id)
        run.status = "success"
        run.finished_at = datetime.now(tz=timezone.utc)
        session.commit()

    fake = FakePublisher()
    res = asyncio.run(alerts.run_alerts(publisher=fake))
    assert res["resolved"] == [f"run-{run_id}"]
    assert any("Recovered" in html for _, html in fake.sent)

    # Ledger row pruned so a recurrence re-alerts.
    with factory() as session:
        ids = set(session.execute(select(AlertSent.notification_id)).scalars().all())
    assert f"run-{run_id}" not in ids


def test_overflow_folds_into_summary(alert_env) -> None:
    brand_id = alert_env
    # One more than the per-pass cap → 1 summary message replaces the tail.
    for _ in range(alerts.MAX_INDIVIDUAL_ALERTS + 2):
        _add_failed_run(brand_id)

    fake = FakePublisher()
    res = asyncio.run(alerts.run_alerts(publisher=fake))
    # All ids recorded as handled (head + overflow).
    assert len(res["sent"]) == alerts.MAX_INDIVIDUAL_ALERTS + 2
    # Messages = cap individual + 1 summary.
    assert len(fake.sent) == alerts.MAX_INDIVIDUAL_ALERTS + 1
    assert any("more alert" in html for _, html in fake.sent)


def test_multi_brand_shows_brand_name(alert_env, monkeypatch) -> None:
    brand_id = alert_env
    factory = admin_db.get_session_factory()
    with factory() as session:
        seed_brand(session, slug="second", name="Second Brand")
        session.commit()
    _add_failed_run(brand_id)

    fake = FakePublisher()
    asyncio.run(alerts.run_alerts(publisher=fake))
    assert any("Brand: Icon Finance" in html for _, html in fake.sent)


# ---------------------------------------------------------------------------
# NTS_075 — pipeline-visibility pulses (run_started / run_finished / published)
# ---------------------------------------------------------------------------


def test_format_run_started_matches_reference() -> None:
    msg = alerts.format_run_started(
        run_id=42,
        triggered_by="manual",
        source_count=3,
        started_at=datetime(2026, 6, 24, 14, 5, tzinfo=timezone.utc),
    )
    assert msg.startswith("🚀 <b>Парсинг запущен</b>")
    assert "Кто: менеджер (вручную)" in msg
    assert "Источников: 3" in msg
    assert "🕓 14:05 UTC · Run #42" in msg


def test_format_run_started_cron_label() -> None:
    msg = alerts.format_run_started(
        run_id=1,
        triggered_by="cron",
        source_count=0,
        started_at=datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc),
    )
    assert "Кто: cron (по расписанию)" in msg


def test_format_run_finished_success() -> None:
    msg = alerts.format_run_finished(
        run_id=42,
        status="success",
        fetched=20,
        relevant=5,
        drafted=3,
        finished_at=datetime(2026, 6, 24, 14, 30, tzinfo=timezone.utc),
    )
    assert msg.startswith("✅ <b>Прогон завершён · Run #42</b>")
    assert "Найдено релевантных: 5/20 · черновиков: 3" in msg
    assert "🕓 14:30 UTC" in msg


def test_format_run_finished_emoji_per_status() -> None:
    common = dict(
        run_id=1, fetched=1, relevant=1, drafted=0,
        finished_at=datetime(2026, 6, 24, 9, 0, tzinfo=timezone.utc),
    )
    assert alerts.format_run_finished(status="failed", **common).startswith("🔴")
    assert alerts.format_run_finished(status="cancelled", **common).startswith("⏹")


def test_format_published_with_live_url_escapes_title() -> None:
    msg = alerts.format_published(
        title='Q1 & "outlook" <2026>',
        language="pl",
        live_url="https://www.iconfinance.io/pl/insights/q1-outlook",
        published_at=datetime(2026, 6, 24, 16, 12, tzinfo=timezone.utc),
    )
    assert msg.startswith("📤 <b>Опубликовано:")
    # User-derived title is escaped — no raw angle brackets leak.
    assert "&amp;" in msg and "&lt;2026&gt;" in msg
    assert "<2026>" not in msg
    assert "Язык: PL · 🕓 16:12 UTC" in msg
    assert (
        '<a href="https://www.iconfinance.io/pl/insights/q1-outlook">'
        in msg
    )


def test_format_published_without_live_url_omits_link() -> None:
    msg = alerts.format_published(
        title="No link brand",
        language="en",
        live_url=None,
        published_at=datetime(2026, 6, 24, 16, 12, tzinfo=timezone.utc),
    )
    assert "→" not in msg
    assert "<a href" not in msg


def _add_run(
    brand_id: int,
    *,
    status: str,
    triggered_by: str = "cron",
    source_ids: str = "[]",
    stats: str | None = None,
    finished: bool = False,
) -> int:
    factory = admin_db.get_session_factory()
    now = datetime.now(tz=timezone.utc)
    with factory() as session:
        run = Run(
            brand_id_fk=brand_id,
            triggered_by=triggered_by,
            source_ids=source_ids,
            started_at=now,
            finished_at=now if finished else None,
            status=status,
            stats=stats,
        )
        session.add(run)
        session.commit()
        return run.id


def test_run_started_pulse_sent_once(alert_env) -> None:
    brand_id = alert_env
    run_id = _add_run(brand_id, status="running", triggered_by="manual",
                      source_ids="[1, 2, 3]")

    fake1 = FakePublisher()
    res1 = asyncio.run(alerts.run_alerts(publisher=fake1))
    assert f"run_started:{run_id}" in res1["sent"]
    assert any("🚀 <b>Парсинг запущен</b>" in html for _, html in fake1.sent)
    assert any("Источников: 3" in html for _, html in fake1.sent)

    # Second pass: still running → dedup, no re-send.
    fake2 = FakePublisher()
    res2 = asyncio.run(alerts.run_alerts(publisher=fake2))
    assert f"run_started:{run_id}" not in res2["sent"]
    assert fake2.sent == []


def test_run_finished_for_success_and_cancelled_only(alert_env) -> None:
    """run_finished fires for success/cancelled. A failed run gets the
    NTS_073 run_failed alert and NO run_finished — no double-send."""
    brand_id = alert_env
    ok_id = _add_run(
        brand_id, status="success", finished=True,
        stats='{"fetched": 20, "scored": 5, "drafted": 3, "errors": 0}',
    )
    failed_id = _add_run(brand_id, status="failed", finished=True,
                         stats='{"fetched": 9, "scored": 0, "drafted": 0}')

    fake = FakePublisher()
    res = asyncio.run(alerts.run_alerts(publisher=fake))

    # success → run_finished pulse with the relevant/fetched/drafted line.
    assert f"run_finished:{ok_id}" in res["sent"]
    assert any(
        f"✅ <b>Прогон завершён · Run #{ok_id}</b>" in html
        for _, html in fake.sent
    )
    assert any(
        "Найдено релевантных: 5/20 · черновиков: 3" in html
        for _, html in fake.sent
    )

    # failed → NTS_073 run_failed, but NEVER run_finished (no conflict).
    assert f"run_finished:{failed_id}" not in res["sent"]
    assert f"run-{failed_id}" in res["sent"]
    assert not any(
        f"Прогон завершён · Run #{failed_id}" in html for _, html in fake.sent
    )
    # Exactly one message mentions the failed run (the run_failed alert).
    failed_msgs = [h for _, h in fake.sent if f"#{failed_id}" in h or
                   f"run #{failed_id}" in h.lower()]
    assert len(failed_msgs) == 1
    assert failed_msgs[0].startswith("🔴 <b>Run")

    # Dedup: a second pass re-sends nothing.
    fake2 = FakePublisher()
    res2 = asyncio.run(alerts.run_alerts(publisher=fake2))
    assert res2["sent"] == []
    assert fake2.sent == []


class _FakeSanityClient:
    def __init__(self, doc: dict) -> None:
        self._doc = doc

    async def query(self, groq: str, params: dict | None = None) -> dict:
        return self._doc


def test_published_pulse_sent_once(alert_env, monkeypatch) -> None:
    brand_id = alert_env
    factory = admin_db.get_session_factory()
    with factory() as session:
        session.add(
            DraftApproval(
                sanity_draft_id="drafts.post-pl-1",
                brand_id_fk=brand_id,
                status="approved",
                decided_by="andriy",
                published_at=datetime.now(tz=timezone.utc),
                sanity_published_id="post-pl-1",
            )
        )
        session.commit()

    fake_doc = {"title": "Nowy fundusz", "language": "pl", "slug": "nowy-fundusz"}
    from pipeline.admin.routes import drafts as drafts_mod

    monkeypatch.setattr(
        drafts_mod,
        "_build_sanity_client_for_brand",
        lambda bid: (_FakeSanityClient(fake_doc), "icon"),
    )

    fake = FakePublisher()
    res = asyncio.run(alerts.run_alerts(publisher=fake))
    assert "published:post-pl-1" in res["sent"]
    pub_msgs = [h for _, h in fake.sent if "📤" in h]
    assert len(pub_msgs) == 1
    assert 'Опубликовано: "Nowy fundusz"' in pub_msgs[0]
    assert "Язык: PL" in pub_msgs[0]
    assert (
        "https://www.iconfinance.io/pl/insights/nowy-fundusz" in pub_msgs[0]
    )

    # Dedup: second pass sends nothing.
    fake2 = FakePublisher()
    res2 = asyncio.run(alerts.run_alerts(publisher=fake2))
    assert "published:post-pl-1" not in res2["sent"]
    assert fake2.sent == []


def test_visibility_keys_not_treated_as_recovered(alert_env, monkeypatch) -> None:
    """A run_finished pulse recorded in alert_sent must never trigger a
    'Recovered' message on a later pass (one-shot pulse, not an incident)."""
    brand_id = alert_env
    _add_run(brand_id, status="success", finished=True,
             stats='{"fetched": 1, "scored": 1, "drafted": 1}')

    asyncio.run(alerts.run_alerts(publisher=FakePublisher()))
    fake = FakePublisher()
    res = asyncio.run(alerts.run_alerts(publisher=fake))
    assert res["resolved"] == []
    assert not any("Recovered" in html for _, html in fake.sent)
