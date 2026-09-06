"""Composition: the plan, the attribution verdicts, depth targets, data blocks.

Revision ID: 028_composition
Revises: 027_document_cache
Create Date: 2026-09-06

S6 (NTS_102 v2, NTS_096 parts A/C, NTS_095, NTS_108 §1). What it adds:

1. ``fact_packs.plan`` and ``fact_packs.attribution`` — the plan the article was
   written from and the per-claim verdicts it was checked against. Both belong
   on the pack rather than on the candidate because both are *about the
   material*: NTS_102 v2 §3 says the plan is what the editor edits on a
   ``scope=plan`` return, and NTS_096 §C says the verdicts are what opens the
   review card when something reads ``distorted``.
2. ``candidates.needs_attention`` — set when the fix cycle did not clear every
   ``distorted`` claim (NTS_102 v2 §2). The draft is still created: the check
   advises, it does not block (NTS_096 §C), and a check that blocked before its
   false-positive rate was known would stop the pipeline for its own bugs.
3. ``depth_length_targets`` — the word bands per depth. ``depth_final`` and its
   fact thresholds already exist (020); what was missing is the length target,
   which NTS_102 puts in the brand config rather than in the prompt, because "a
   number in a prompt does not know what the article is about".
4. ``data_blocks_enabled`` — OFF until the Sanity schema PR of S8 is merged
   (NTS_095: schema → render → pipeline, in that order). The generator is
   written and tested now; it writes nothing into a draft until the flag flips.
5. ``max_quote_words`` — the per-licence quote ceiling of NTS_108 §1
   (``professional_commentary`` 15 words, ``corporate_pr`` 25), which the
   attribution check enforces as ``quote_too_long``.
6. ``attribution_model`` — a cheap model, its own config key and its own
   ``cost_records`` operation, because NTS_096 says to measure this rather than
   estimate it.
7. **Reseed of ``writer_draft``** (NTS_067/071/009/011/019). The draft prompt
   gains ``{plan}``, ``{depth_guidance}`` and ``{primary_document}``, which
   changes its placeholder set — and a live row whose placeholders no longer
   match is silently rejected in favour of the in-code constant
   (``CommentWriter._resolve_template``). Without this reseed every brand would
   quietly fall back and the S6 prompt would never run in production. Active
   rows are overwritten with the code constant, as 009, 011 and 019 did — the
   convention NTS_071 sets for the writer prompts.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "028_composition"
down_revision: str | None = "027_document_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# NTS_102 §"Длину задаёт depth_estimate" + v2 §1b: the deep band has no upper
# bound, which is why its second element is null rather than a big number.
DEFAULT_DEPTH_TARGETS = json.dumps(
    {"note": [300, 450], "article": [600, 900], "deep": [1200, None]},
    separators=(", ", ": "),
)

# NTS_108 §1. Absent classes have no quote ceiling of their own — official acts
# may be quoted at length with attribution.
DEFAULT_MAX_QUOTE_WORDS = json.dumps(
    {"professional_commentary": 15, "corporate_pr": 25, "news_paywalled": 0},
    separators=(", ", ": "),
)

_VERSION_NAME = "v2.0 — plan, depth target, primary document (NTS_102 v2)"
_NOTES = (
    "Re-synced to the comment_writer constant (NTS_102 v2 / S6). Generation "
    "reads this active row; edits here affect the next run. Keep the "
    "{placeholders} — writer_draft now also requires {plan}, "
    "{depth_guidance} and {primary_document}. Removing any of them drops the "
    "row back to the code constant and your edits stop reaching production."
)

_CONFIG_COLUMNS: tuple[tuple[str, sa.types.TypeEngine, str], ...] = (
    ("data_blocks_enabled", sa.Boolean(), "0"),
    ("depth_length_targets", sa.Text(), DEFAULT_DEPTH_TARGETS),
    ("max_quote_words", sa.Text(), DEFAULT_MAX_QUOTE_WORDS),
    ("attribution_model", sa.Text(), "gpt-4o-mini"),
)


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in set(inspector.get_table_names()):
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


# --- upgrade ---------------------------------------------------------------


def upgrade() -> None:
    if "fact_packs" in _tables():
        present = _columns("fact_packs")
        if "plan" not in present:
            op.add_column("fact_packs", sa.Column("plan", sa.Text(), nullable=True))
        if "attribution" not in present:
            op.add_column(
                "fact_packs", sa.Column("attribution", sa.Text(), nullable=True)
            )

    if "candidates" in _tables() and "needs_attention" not in _columns("candidates"):
        op.add_column(
            "candidates",
            sa.Column(
                "needs_attention",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )

    present = _columns("pipeline_config")
    for name, coltype, default in _CONFIG_COLUMNS:
        if "pipeline_config" in _tables() and name not in present:
            server_default = (
                sa.text(default) if isinstance(coltype, sa.Boolean) else default
            )
            op.add_column(
                "pipeline_config",
                sa.Column(name, coltype, nullable=False, server_default=server_default),
            )

    _reseed_writer_draft()


def _reseed_writer_draft() -> None:
    """Re-sync the active ``writer_draft`` rows to the current code constant.

    The same move migrations 009, 011 and 019 made, for the same reason: the
    draft prompt gains ``{plan}``, ``{depth_guidance}`` and
    ``{primary_document}``, which changes its placeholder set, and
    ``CommentWriter._resolve_template`` silently rejects a live row whose set no
    longer matches. The visible symptom is not an error — it is that the
    operator's edits stop reaching production and only
    ``comment_writer.db_prompt_rejected`` says so.

    Idempotent: a row already equal to the constant is skipped.
    """
    from datetime import UTC, datetime

    from pipeline.generator.comment_writer import _DRAFT_PROMPT

    if "prompts" not in _tables():
        return
    bind = op.get_bind()
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    rows = bind.execute(
        sa.text(
            "SELECT id, content FROM prompts "
            "WHERE prompt_type = 'writer_draft' AND is_active = 1"
        )
    ).fetchall()
    for row_id, current in rows:
        if current == _DRAFT_PROMPT:
            continue
        bind.execute(
            sa.text(
                "UPDATE prompts SET content = :c, version_name = :v, "
                "notes = :n, created_at = :ts WHERE id = :i"
            ),
            {
                "c": _DRAFT_PROMPT,
                "v": _VERSION_NAME,
                "n": _NOTES,
                "ts": now,
                "i": row_id,
            },
        )


# --- downgrade -------------------------------------------------------------


def downgrade() -> None:
    # The prompt re-sync is not meaningfully reversible — the superseded text
    # has no value to restore (same reasoning as 019). Only the schema is.
    present = _columns("pipeline_config")
    dropping = [name for name, _t, _d in _CONFIG_COLUMNS if name in present]
    if dropping:
        with op.batch_alter_table("pipeline_config") as batch:
            for column in dropping:
                batch.drop_column(column)

    if "needs_attention" in _columns("candidates"):
        with op.batch_alter_table("candidates") as batch:
            batch.drop_column("needs_attention")

    dropping = [c for c in ("plan", "attribution") if c in _columns("fact_packs")]
    if dropping:
        with op.batch_alter_table("fact_packs") as batch:
            for column in dropping:
                batch.drop_column(column)
