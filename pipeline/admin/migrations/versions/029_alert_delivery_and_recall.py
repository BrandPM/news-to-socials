"""Alert delivery bookkeeping and the recall test's seed topics (S7).

Revision ID: 029_alert_delivery_and_recall
Revises: 028_composition
Create Date: 2026-09-06

Two things S7 needs and nothing before it could have:

1. **``alert_sent.delivered`` and its retry columns.** NTS_106 §1 asks for
   "алерты копятся с delivered=0, повтор через 10 мин"; NTS_122 §8 found the
   column does not exist and the retry does not happen, so an alert raised
   while Telegram is unreachable is lost silently. The table has been a *dedup
   ledger* — a row meant "we already said this" — and the change makes it a
   *delivery ledger*: a row means "we intended to say this", and ``delivered``
   says whether it landed. Existing rows are back-filled ``delivered=1``,
   because every one of them was written after a successful send.

2. **``seed_topics``.** The 20 recall-test topics of NTS_115 lived only in the
   vault, which is why the recall test was a manual exercise that never
   happened (NTS_117, gate journal 2026-09-06). Andriy's directive moved it
   into code as an auto-recall over the accumulated ``candidates``; a table is
   what lets the numbers be recomputed as the portfolio grows rather than
   pasted into a document once.

Both additive. ``downgrade`` drops the two columns (batch mode: the table is
228 rows, so the rebuild is cheap) and the table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "029_alert_delivery_and_recall"
down_revision: str | None = "028_composition"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in set(inspector.get_table_names()):
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    present = _columns("alert_sent")
    if "alert_sent" in _tables() and "delivered" not in present:
        op.add_column(
            "alert_sent",
            sa.Column(
                "delivered", sa.Boolean(), nullable=False, server_default=sa.text("0")
            ),
        )
        # Every existing row was written *after* a successful send — that was
        # the whole contract of the old table. Back-filling them to 1 keeps
        # that history true instead of scheduling 228 retries of alerts that
        # were delivered weeks ago.
        bind.execute(sa.text("UPDATE alert_sent SET delivered = 1"))
    if "alert_sent" in _tables() and "attempts" not in _columns("alert_sent"):
        op.add_column(
            "alert_sent",
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        )
    if "alert_sent" in _tables() and "last_attempt_at" not in _columns("alert_sent"):
        op.add_column(
            "alert_sent", sa.Column("last_attempt_at", sa.DateTime(), nullable=True)
        )
    if "alert_sent" in _tables() and "message" not in _columns("alert_sent"):
        # The rendered text, kept so a retry sends what was meant rather than
        # re-deriving it from a world that has since moved on.
        op.add_column("alert_sent", sa.Column("message", sa.Text(), nullable=True))

    if "seed_topics" not in _tables():
        op.create_table(
            "seed_topics",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "brand_id_fk",
                sa.Integer(),
                sa.ForeignKey(
                    "brands.id", ondelete="RESTRICT", name="fk_seed_topics_brand"
                ),
                nullable=False,
            ),
            sa.Column("topic", sa.Text(), nullable=False),
            # Comma-free keyword list, matched case-insensitively against a
            # candidate's title and summary. Deliberately simple: the recall
            # test asks "did this subject reach the funnel at all", and an
            # embedding search would answer a different, softer question.
            sa.Column("keywords", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("jurisdiction", sa.String(), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("brand_id_fk", "topic", name="uq_seed_topic_brand"),
        )
        _seed_icon_topics()


def _seed_icon_topics() -> None:
    """The 20 topics of NTS_115 artefact 2, for the Icon brand.

    Seeded here rather than left to the operator because a recall test with no
    topics reports 100% and means nothing — the empty case has to be impossible
    rather than merely discouraged.
    """
    import json
    from datetime import UTC, datetime

    bind = op.get_bind()
    icon_id = bind.execute(
        sa.text("SELECT id FROM brands WHERE slug = 'icon'")
    ).scalar()
    if icon_id is None:
        return
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    for topic, keywords, jurisdiction in SEED_TOPICS:
        bind.execute(
            sa.text(
                "INSERT OR IGNORE INTO seed_topics "
                "(brand_id_fk, topic, keywords, jurisdiction, created_at) "
                "VALUES (:b, :t, :k, :j, :ts)"
            ),
            {
                "b": icon_id,
                "t": topic,
                "k": json.dumps(keywords),
                "j": jurisdiction,
                "ts": now,
            },
        )


# NTS_115 artefact 2. The keyword lists are deliberately narrow: a topic that
# matches on "tax" would report recall for the whole feed.
SEED_TOPICS: tuple[tuple[str, list[str], str | None], ...] = (
    ("CARF / crypto-asset reporting framework", ["carf", "crypto-asset reporting"], "OECD"),
    ("DAC8 implementation", ["dac8", "dac 8"], "EU"),
    ("EU AML package / AMLA", ["amla", "anti-money laundering authority", "aml package"], "EU"),
    ("MiCA / CASP authorisation", ["mica", "crypto-asset service provider", "casp"], "EU"),
    ("CRS amendments", ["common reporting standard", "crs amendment"], "OECD"),
    ("UBO register access", ["beneficial ownership register", "ubo register"], "EU"),
    ("Cyprus IP box / tax rulings", ["cyprus tax", "cyprus ip box"], "CY"),
    ("Malta residence programmes", ["malta residence", "malta permanent residence"], "MT"),
    ("UAE corporate tax / free zone", ["uae corporate tax", "free zone person"], "AE"),
    ("Swiss FINMA circulars", ["finma circular", "finma rundschreiben"], "CH"),
    ("UK non-dom regime", ["non-dom", "non-domiciled"], "UK"),
    ("Pillar Two / global minimum tax", ["pillar two", "global minimum tax", "globe rules"], "OECD"),
    ("Trust reporting obligations", ["trust register", "trust reporting"], "EU"),
    ("EU blacklist of non-cooperative jurisdictions", ["non-cooperative jurisdictions", "eu blacklist"], "EU"),
    ("Golden visa closures", ["golden visa", "citizenship by investment"], "EU"),
    ("Family office regulation", ["family office", "single family office"], None),
    ("Private banking M&A", ["private bank acquisition", "wealth manager acquisition"], None),
    ("Sanctions and asset freezes", ["asset freeze", "sanctions package"], "EU"),
    ("Poland holding company regime", ["polish holding", "poland holding regime"], "PL"),
    ("Ukraine tax residency", ["ukraine tax residen", "ukrainian tax residen"], "UA"),
)


def downgrade() -> None:
    if "seed_topics" in _tables():
        op.drop_table("seed_topics")

    dropping = [
        c
        for c in ("delivered", "attempts", "last_attempt_at", "message")
        if c in _columns("alert_sent")
    ]
    if dropping:
        with op.batch_alter_table("alert_sent") as batch:
            for column in dropping:
                batch.drop_column(column)
