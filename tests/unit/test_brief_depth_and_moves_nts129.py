"""The ``brief`` band and the five moves (NTS_129 P2 Ф2, migrations 034/035).

Two defects, one cause. Regulator material came out at ~300 words against an
``article`` band of 600-900, on packs holding six to eight facts. Ruled out
over S6-S10: the target reaches the prompt, the polish pass does not compress,
no token ceiling binds.

What was left is that the model was choosing between padding and stopping and
choosing correctly — the target was wrong for the material (four facts never
supported 600-900 words), and the prompt asked for a genre rather than a
structure, so there was nothing to check completeness against.

So this file asserts both halves:

* ``brief`` exists and is *reached*, on the fact counts that produced the
  short articles.
* The prompt names the five moves, each of which has to rest on a fact, and
  the anti-padding rules that predate this change still hold — that last part
  matters most, because loosening "do not pad" to fix "too short" would trade
  a visible defect for an invisible one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from pipeline.generator.composition import (
    DEPTHS,
    Plan,
    compute_depth_final,
    depth_guidance,
)

TARGETS = {
    "note": (300, 450),
    "brief": (300, 400),
    "article": (600, 900),
    "deep": (1200, None),
}


@dataclass
class _Fact:
    text: str
    value: str = ""
    unit: str = ""
    comparable_group: str = ""


@dataclass
class _Pack:
    source_facts: list = field(default_factory=list)
    context: list = field(default_factory=list)


def _pack(n: int, *, pairs: int = 0) -> _Pack:
    """A pack of ``n`` countable facts, ``pairs`` of them comparable."""
    facts = [
        _Fact(text=f"the threshold is EUR {i}00,000 from 1 January 2027", value=f"{i}00000")
        for i in range(1, n + 1)
    ]
    for index in range(pairs * 2):
        facts[index].comparable_group = f"group{index // 2}"
        facts[index].unit = "EUR"
    return _Pack(source_facts=facts)


# --------------------------------------------------------------------------
# the band exists, and it is reached
# --------------------------------------------------------------------------


def test_brief_sits_between_note_and_article() -> None:
    assert DEPTHS == ("note", "brief", "article", "deep")


@pytest.mark.parametrize(
    ("n_facts", "expected"),
    [
        (0, "note"),
        (3, "note"),
        # The range that produced the 300-word "articles". Six to eight facts
        # is what a regulator press release plus a web-search pass typically
        # yields, and under the old floor of four every one of them asked for
        # 600-900 words.
        (4, "brief"),
        (6, "brief"),
        (8, "brief"),
        (9, "brief"),
        (10, "article"),
        (14, "article"),
    ],
)
def test_the_fact_count_picks_the_band(n_facts: int, expected: str) -> None:
    decision = compute_depth_final(
        _pack(n_facts), brief_min_facts=4, article_min_facts=10, deep_min_facts=10
    )
    assert decision.depth == expected, decision.reason


def test_deep_still_needs_its_pairs_and_demotes_to_article_not_brief() -> None:
    """The NTS_102 rule this must not disturb: ``deep`` promises a table, and
    a demotion for want of comparable numbers lands on ``article`` — the facts
    are there, only the table is not."""
    decision = compute_depth_final(
        _pack(12, pairs=0), brief_min_facts=4, article_min_facts=10, deep_min_facts=10
    )
    assert decision.depth == "article"
    decision = compute_depth_final(
        _pack(12, pairs=2), brief_min_facts=4, article_min_facts=10, deep_min_facts=10
    )
    assert decision.depth == "deep"


def test_the_thresholds_are_configurable_not_baked_in() -> None:
    """They are config columns so Andriy can move them from the Editorial
    screen once real output says where the boundary belongs — the numbers here
    are a starting point, not a finding."""
    assert (
        compute_depth_final(
            _pack(6), brief_min_facts=4, article_min_facts=5, deep_min_facts=20
        ).depth
        == "article"
    )
    assert (
        compute_depth_final(
            _pack(6), brief_min_facts=8, article_min_facts=20, deep_min_facts=40
        ).depth
        == "note"
    )


def test_the_brief_reason_names_the_gap_it_fell_short_of() -> None:
    """The reason string is what an editor reads on the traceability panel to
    understand why a piece is 350 words. "only 6 facts" is not an answer; "6
    facts, not the 10 for an article" is."""
    decision = compute_depth_final(
        _pack(6), brief_min_facts=4, article_min_facts=10, deep_min_facts=10
    )
    assert "6" in decision.reason and "10" in decision.reason


# --------------------------------------------------------------------------
# the guidance the prompt is rendered with
# --------------------------------------------------------------------------


def test_a_brief_asks_for_300_400_words_and_fewer_sections() -> None:
    guidance = depth_guidance("brief", TARGETS)
    assert "300-400 words" in guidance
    assert "1-2 H2 sections" in guidance
    # Still not a quota — the rule that outranks the band.
    assert "NOT a quota" in guidance


def test_a_brief_is_a_shape_to_aim_at_not_an_article_that_fell_short() -> None:
    """The whole point of the band. Both bands forbid padding; what changes is
    that 350 words is now the target rather than 40% of it, and an editor
    reviewing a brief is reading a finished piece."""
    brief = depth_guidance("brief", TARGETS)
    article = depth_guidance("article", TARGETS)
    assert "300-400" in brief and "600-900" in article
    assert "Padding to reach a word count is a failure" in brief


# --------------------------------------------------------------------------
# the plan carries the moves and the gaps
# --------------------------------------------------------------------------


def test_the_plan_renders_the_move_and_the_unknowns_for_the_writer() -> None:
    """Move 5 comes from the plan, not from the writer.

    A model asked at drafting time to say what it does not know will produce
    something plausible-sounding; the planner has the pack in front of it and
    can name what is actually absent.
    """
    plan = Plan(
        sections=[
            {"heading": "The EUR 5m threshold", "move": "affected", "facts": ["EUR 5m"]}
        ],
        lede="Holdings above EUR 5m must report from January.",
        close="The filing date is 31 March.",
        unknowns=["the guidance on valuation dates has not been issued"],
    )
    rendered = plan.render()
    assert "[move: affected]" in rendered
    assert "WHAT WE DON'T KNOW" in rendered
    assert "valuation dates" in rendered


def test_unknowns_survive_the_round_trip_through_storage() -> None:
    """``plan`` is stored as JSON on the fact pack and read back for the
    traceability panel; a key that serialises and does not deserialise would
    lose move 5 silently between the planner and the writer."""
    plan = Plan(sections=[{"heading": "x"}], unknowns=["a", "b"])
    assert Plan.from_dict(plan.as_dict()).unknowns == ["a", "b"]


# --------------------------------------------------------------------------
# the prompt
# --------------------------------------------------------------------------


def test_the_draft_prompt_names_all_five_moves() -> None:
    from pipeline.generator.comment_writer import _DRAFT_PROMPT

    for move in (
        "WHAT HAPPENED",
        "WHAT IT CHANGES",
        "WHO IS AFFECTED",
        "WHAT TO DO",
        "WHAT WE DON'T KNOW",
    ):
        assert move in _DRAFT_PROMPT, move


def test_every_move_must_rest_on_a_fact_and_an_ungrounded_one_goes_to_move_5() -> None:
    """The rule that makes the structure safe.

    Naming five moves without this would be an instruction to invent: a model
    told it must produce a "who is affected" section, on a pack with no
    threshold in it, will write one anyway. Routing the ungrounded move into
    "what we don't know" is what turns a missing fact into an honest sentence
    instead of a fabricated one.
    """
    from pipeline.generator.comment_writer import _DRAFT_PROMPT

    import re

    flat = re.sub(r"\s+", " ", _DRAFT_PROMPT)
    assert "must rest on at least one concrete fact" in flat
    assert "it goes to move 5 instead" in flat


def test_the_thresholds_and_deadlines_are_demanded_concretely() -> None:
    """Moves 3 and 4 are what the reader came for, and the failure mode is
    prose that gestures at them ("large holders", "in due course")."""
    from pipeline.generator.comment_writer import _DRAFT_PROMPT

    assert '"Large holders" is not a threshold' in _DRAFT_PROMPT
    assert "A step with no date" in _DRAFT_PROMPT


def test_the_planner_asks_for_the_moves_too() -> None:
    """The writer only writes what the plan assigns, so a plan that does not
    carry moves 3 and 4 produces an article without them however the draft
    prompt is worded."""
    import re

    from pipeline.generator.composition import _PLAN_INSTRUCTIONS

    flat = re.sub(r"\s+", " ", _PLAN_INSTRUCTIONS)
    assert "THE FIVE MOVES" in _PLAN_INSTRUCTIONS
    assert '"move": "<happened|changes|affected|todo|unknown>"' in _PLAN_INSTRUCTIONS
    assert "Leaving a threshold unassigned is the most expensive mistake" in flat


# --------------------------------------------------------------------------
# migration 034 — the candidates rebuild
# --------------------------------------------------------------------------


def test_034_widens_depth_final_without_losing_candidates(tmp_path) -> None:
    """SQLite cannot ALTER a CHECK, so admitting ``brief`` rebuilds
    ``candidates`` — and that table is the one with rows nobody can regenerate.

    What the rebuild has to survive, asserted here because each has a
    documented way of dying quietly in a SQLite table copy: every row, the
    indexes, the three inbound foreign keys, and the self-referencing
    ``supersedes_id`` (which a rebuild can silently re-point at the temporary
    table the copy went through).
    """
    import os
    import sqlite3
    import subprocess
    import sys
    from pathlib import Path

    project_root = Path(__file__).resolve().parents[2]
    test_db = tmp_path / "alembic-test.db"
    env = {
        **os.environ,
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
        "PYTHONPATH": str(project_root),
        "ADMIN_DB_PATH": str(test_db),
    }

    def alembic(*args: str) -> None:
        subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )

    alembic("upgrade", "033_prefilter_from_audit")
    with sqlite3.connect(test_db) as conn:
        brand_id = conn.execute("SELECT id FROM brands LIMIT 1").fetchone()[0]
        for depth in ("note", "article", "deep"):
            conn.execute(
                "INSERT INTO candidates (brand_id_fk, input_kind, source_title, "
                "verdict, reason_code, reason, status, depth_final, created_at) "
                "VALUES (?, 'document', ?, 'accept', 'ok', 'r', 'pending', ?, "
                "CURRENT_TIMESTAMP)",
                (brand_id, f"a {depth} candidate", depth),
            )
        # A self-reference, the FK most at risk in a rebuild.
        ids = [r[0] for r in conn.execute("SELECT id FROM candidates ORDER BY id")]
        conn.execute(
            "UPDATE candidates SET supersedes_id = ? WHERE id = ?", (ids[0], ids[-1])
        )
        conn.commit()
        before = [
            r
            for r in conn.execute(
                "SELECT id, source_title, depth_final, supersedes_id FROM candidates "
                "ORDER BY id"
            )
        ]
        indexes_before = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='candidates' AND name NOT LIKE 'sqlite_%'"
            )
        }

    alembic("upgrade", "head")

    with sqlite3.connect(test_db) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        after = [
            r
            for r in conn.execute(
                "SELECT id, source_title, depth_final, supersedes_id FROM candidates "
                "ORDER BY id"
            )
        ]
        assert after == before, "the rebuild changed rows"
        indexes_after = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='candidates' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert indexes_before <= indexes_after, sorted(indexes_before - indexes_after)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # The inbound references still point at ``candidates`` and not at the
        # rebuild's temporary table.
        inbound = {
            (table, fk[3])
            for (table,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            for fk in conn.execute(f"PRAGMA foreign_key_list('{table}')")
            if fk[2] == "candidates"
        }
        assert {
            ("review_decisions", "candidate_id_fk"),
            ("draft_approvals", "candidate_id_fk"),
            ("fact_packs", "candidate_id_fk"),
            ("candidates", "supersedes_id"),
        } <= inbound

        # The new value is now storable, and the guard's prior is NOT widened.
        conn.execute(
            "UPDATE candidates SET depth_final = 'brief' WHERE source_title = "
            "'a note candidate'"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE candidates SET depth_prior = 'brief' WHERE id = ?",
                         (before[0][0],))
        conn.commit()

    # …and the downgrade refuses rather than dropping a published brief.
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        alembic("downgrade", "033_prefilter_from_audit")
    assert "depth_final='brief'" in excinfo.value.stderr

    with sqlite3.connect(test_db) as conn:
        conn.execute("UPDATE candidates SET depth_final = NULL WHERE depth_final = 'brief'")
        conn.commit()
    alembic("downgrade", "033_prefilter_from_audit")
    alembic("upgrade", "head")
    with sqlite3.connect(test_db) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        assert conn.execute("SELECT count(*) FROM candidates").fetchone()[0] == len(before)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_035_reseeds_v2_relabels_a_stale_row_and_leaves_an_operator_edit_alone(
    tmp_path,
) -> None:
    """Three rows, three outcomes — the whole contract of a re-seed migration.

    * The shipped v2.0 text is replaced with v3.0.
    * A row whose CONTENT is already v3.0 but whose LABEL still says v2.0 is
      relabelled. This is not hypothetical: migration 028 re-seeds
      ``writer_draft`` from the same code constant, so on a database that was
      behind, the content arrives correct and only the version_name is stale.
      Skipping on content alone would leave "v2.0" on the Editorial screen
      next to v3.0 text, and the version_name is the operator's only signal
      about which prompt is live.
    * An operator's own text is left exactly as it was. Overwriting it would
      discard their work with no trace anywhere — NTS_071's rule, and the one
      failure a re-seed migration can cause that nobody would ever notice.
    """
    import os
    import sqlite3
    import subprocess
    import sys
    from pathlib import Path

    from pipeline.generator.comment_writer import _DRAFT_PROMPT

    project_root = Path(__file__).resolve().parents[2]
    test_db = tmp_path / "alembic-test.db"
    env = {
        **os.environ,
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
        "PYTHONPATH": str(project_root),
        "ADMIN_DB_PATH": str(test_db),
    }

    def alembic(*args: str) -> None:
        subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )

    alembic("upgrade", "034_brief_depth_band")

    shipped_v2 = (
        "You write an original expert commentary from the brand below on the "
        "news peg.\nThis is NOT a rewrite.\n{voice_profile_yaml}{title}{summary}"
        "{language_name}{banned_phrases}{fact_pack}{plan}{depth_guidance}"
        "{primary_document}"
    )
    operator_text = "My own prompt. {title} {fact_pack} {plan}"

    # One active row per brand — the partial unique index enforces that, so the
    # three cases go on three brands.
    with sqlite3.connect(test_db) as conn:
        brands = [r[0] for r in conn.execute("SELECT id FROM brands ORDER BY id")]
        assert len(brands) >= 3
        for brand_id, content, label in (
            (brands[0], shipped_v2, "v2.0 — plan, depth target, primary document"),
            (brands[1], _DRAFT_PROMPT, "v2.0 — plan, depth target, primary document"),
            (brands[2], operator_text, "andriy's edit"),
        ):
            conn.execute(
                "INSERT INTO prompts (prompt_type, version_name, content, "
                "is_active, brand_id_fk, created_by, created_at) VALUES "
                "('writer_draft', ?, ?, 1, ?, 'test', CURRENT_TIMESTAMP)",
                (label, content, brand_id),
            )
        conn.commit()

    alembic("upgrade", "head")

    with sqlite3.connect(test_db) as conn:
        rows = dict(
            (r[0], (r[1], r[2]))
            for r in conn.execute(
                "SELECT brand_id_fk, version_name, content FROM prompts "
                "WHERE prompt_type = 'writer_draft' AND is_active = 1"
            )
        )

    label, content = rows[brands[0]]
    assert content == _DRAFT_PROMPT, "the shipped v2.0 was not re-seeded"
    assert label.startswith("v3.0")

    label, content = rows[brands[1]]
    assert content == _DRAFT_PROMPT
    assert label.startswith("v3.0"), "content was already v3.0 but the label was left stale"

    label, content = rows[brands[2]]
    assert content == operator_text, "an operator's own prompt was overwritten"
    assert label == "andriy's edit"
