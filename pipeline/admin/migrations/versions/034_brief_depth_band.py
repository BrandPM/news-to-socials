"""A fourth depth band for material that is real but thin (NTS_129 P2 Ф2).

Revision ID: 034_brief_depth_band
Revises: 033_prefilter_from_audit
Create Date: 2026-09-06

The article band asked for 600-900 words on a floor of four facts. Four facts
do not support 600-900 words, and the observed output says so: regulator
material came out at ~300 words against that band, reading as if the piece had
been cut off. Ruled out over S6-S10 — the target reaches the prompt, the polish
pass does not compress, no token ceiling binds. What was left is that the model
was choosing between padding and stopping, and choosing correctly. The target
was wrong for the material.

So ``brief`` is added underneath ``article``:

    note     < 4 facts        300-450   a paragraph or two
    brief    4-9 facts        300-400   the honest shape for thin material
    article  >= 10 facts      600-900
    deep     >= 10 + 2 pairs  1200+

and ``depth_article_min_facts`` moves from 4 to 10 so the article floor means
what it says. The difference between ``note`` and ``brief`` is not length — it
is that a brief still carries the five moves, in one or two sections instead of
four. A 300-word brief is a *shape the writer aims at*, where a 300-word
article is a target it fell short of, and only one of those can be reviewed.

Three changes, and one of them is a table rebuild:

**1. ``depth_brief_min_facts``** — a new config column, default 4 (the value
the article floor used to hold, so the boundary itself does not move).

**2. Defaults, edits preserved.** ``depth_article_min_facts`` is raised only
where it still reads 4, and ``depth_length_targets`` is *extended* with the
``brief`` key rather than replaced. An operator who tuned either from the
Editorial screen keeps their value: NTS_071's rule, applied to config rather
than to a prompt, where nothing else would catch the loss.

**3. ``ck_candidates_depth_final``** has to admit the new value, and SQLite
cannot ALTER a CHECK — so ``candidates`` is rebuilt, the same operation
migration 021 performed on ``prompts``. What that rebuild must not lose:

* the seven plain indexes (batch mode reflects these; asserted by test anyway);
* the self-referencing ``supersedes_id`` FK and the three inbound FKs from
  ``review_decisions``, ``draft_approvals`` and ``fact_packs`` — the classic
  casualty, because a rebuild renames the old table and an inbound reference
  can follow the rename;
* every row. On the production database that is 1 532 candidates carrying the
  whole recall history, and they are not reproducible.

``depth_prior`` is deliberately NOT widened. It is the guard's estimate from a
headline, and the guard cannot count facts — a prior of "brief" would be a
value nothing can compute. The two enums differ, and that difference is the
point (``CANDIDATE_DEPTHS`` vs ``CANDIDATE_DEPTHS_FINAL``).
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "034_brief_depth_band"
down_revision: str | None = "033_prefilter_from_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_candidates_depth_final"
_DEPTHS_BEFORE = ("note", "article", "deep")
_DEPTHS_AFTER = ("note", "brief", "article", "deep")

_OLD_ARTICLE_FLOOR = 4
_NEW_ARTICLE_FLOOR = 10
_BRIEF_BAND = [300, 400]


def _check(values: Sequence[str]) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    return f"depth_final IN ({joined})"


def _accepts(value: str) -> bool:
    """True when the live CHECK already admits ``value``.

    Read from ``sqlite_master`` rather than tracked in a flag, so the migration
    is re-runnable after a half-applied deploy — 021's rule, and the reason
    this rebuild is safe to retry.
    """
    ddl = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='candidates'"
            )
        )
        .scalar()
    )
    return bool(ddl) and f"'{value}'" in str(ddl)


def _rebuild(values: Sequence[str]) -> None:
    with op.batch_alter_table("candidates", schema=None) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(_CONSTRAINT, _check(values))


def _has_column(name: str) -> bool:
    bind = op.get_bind()
    return name in {c["name"] for c in sa.inspect(bind).get_columns("pipeline_config")}


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_column("depth_brief_min_facts"):
        op.add_column(
            "pipeline_config",
            sa.Column(
                "depth_brief_min_facts",
                sa.Integer(),
                nullable=False,
                server_default="4",
            ),
        )

    # Raise the article floor only where it still holds the shipped default.
    bind.execute(
        sa.text(
            "UPDATE pipeline_config SET depth_article_min_facts = :new "
            "WHERE depth_article_min_facts = :old"
        ),
        {"new": _NEW_ARTICLE_FLOOR, "old": _OLD_ARTICLE_FLOOR},
    )

    for brand_id, raw in bind.execute(
        sa.text("SELECT brand_id_fk, depth_length_targets FROM pipeline_config")
    ).fetchall():
        try:
            targets = dict(json.loads(raw)) if raw else {}
        except (TypeError, ValueError):
            # An unreadable value is an operator typo; replacing it here would
            # hide it, and the reader already falls back to the documented
            # default.
            continue
        if "brief" in targets:
            continue
        targets["brief"] = _BRIEF_BAND
        bind.execute(
            sa.text(
                "UPDATE pipeline_config SET depth_length_targets = :v "
                "WHERE brand_id_fk = :b"
            ),
            {"v": json.dumps(targets), "b": brand_id},
        )

    if not _accepts("brief"):
        _rebuild(_DEPTHS_AFTER)


def downgrade() -> None:
    bind = op.get_bind()

    if _accepts("brief"):
        # Rows holding the value being removed would violate the narrowed
        # CHECK on the copy-back. Refuse loudly rather than rebuild into a
        # table the data cannot satisfy — 021's rule. The operator decides
        # what a published brief becomes.
        remaining = bind.execute(
            sa.text("SELECT count(*) FROM candidates WHERE depth_final = 'brief'")
        ).scalar()
        if remaining:
            raise RuntimeError(
                f"cannot downgrade 034: {remaining} candidate(s) hold "
                "depth_final='brief'. Retype or delete them first — narrowing "
                "the CHECK would either drop them silently or fail mid-rebuild."
            )
        _rebuild(_DEPTHS_BEFORE)

    for brand_id, raw in bind.execute(
        sa.text("SELECT brand_id_fk, depth_length_targets FROM pipeline_config")
    ).fetchall():
        try:
            targets = dict(json.loads(raw)) if raw else {}
        except (TypeError, ValueError):
            continue
        if targets.pop("brief", None) is None:
            continue
        bind.execute(
            sa.text(
                "UPDATE pipeline_config SET depth_length_targets = :v "
                "WHERE brand_id_fk = :b"
            ),
            {"v": json.dumps(targets), "b": brand_id},
        )

    bind.execute(
        sa.text(
            "UPDATE pipeline_config SET depth_article_min_facts = :old "
            "WHERE depth_article_min_facts = :new"
        ),
        {"new": _NEW_ARTICLE_FLOOR, "old": _OLD_ARTICLE_FLOOR},
    )

    if _has_column("depth_brief_min_facts"):
        op.drop_column("pipeline_config", "depth_brief_min_facts")
