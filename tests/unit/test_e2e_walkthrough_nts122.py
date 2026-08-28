"""The e2e walkthrough is reproducible and spends nothing (NTS_122).

A walkthrough that only runs when someone remembers to run it is a document,
not a check. These tests keep three properties true:

1. **It runs from one command, on a fresh fixture.** When a stage's real
   function changes signature, this fails here rather than the next time
   somebody wants a status report.
2. **It cannot spend money.** ``httpx`` is severed at import time; asserted
   rather than trusted to the comment that says so.
3. **Missing stages stay marked missing.** The five ``NOT IMPLEMENTED`` stages
   are the report's whole value, and the temptation when a stage is half-built
   is to promote it to ``ok``. The count is pinned, so a promotion is a
   deliberate edit with a failing test to answer for.

**Run as a subprocess, deliberately.** The script severs ``httpx`` and swaps
``openai.AsyncOpenAI`` process-wide, which is correct for a command and fatal
for a test session that imports it — the first version of this file took 360
other tests down with it. Running it the way an operator runs it is both
faithful to "воспроизводимый одной командой" and the only clean isolation.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "scripts.e2e_walkthrough", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, (
        f"walkthrough exited {result.returncode}\n"
        f"--- stdout ---\n{result.stdout[-4000:]}\n"
        f"--- stderr ---\n{result.stderr[-4000:]}"
    )
    return result.stdout


@pytest.fixture(scope="module")
def walkthrough_output(tmp_path_factory) -> str:
    db = tmp_path_factory.mktemp("e2e") / "walkthrough.db"
    return _run("--db", str(db), "--markdown")


def test_the_chain_completes_through_the_slot(walkthrough_output: str):
    # The whole point of the NTS_121 link work: the candidate reaches
    # ``published``, and reaches it *through* a slot rather than around one.
    assert "status=published" in walkthrough_output
    assert "slot=2026-08-31" in walkthrough_output
    assert "link_candidate_to_draft → True" in walkthrough_output


def test_every_stage_is_reported_and_five_are_still_unbuilt(
    walkthrough_output: str,
):
    assert "17 total" in walkthrough_output
    assert "5 not implemented" in walkthrough_output
    for owner in ("S4", "S5", "S6"):
        assert f"[{owner}]" in walkthrough_output, f"nothing waits on {owner}"
    # Each unbuilt stage says so in words, not by omission.
    assert walkthrough_output.count("nothing —") >= 4


def test_spend_is_accounted_but_never_charged(walkthrough_output: str):
    """The cost column must be non-zero — a walkthrough reporting $0.00 for a
    research call measures nothing — while every call is a fake."""
    total_line = next(
        line
        for line in walkthrough_output.splitlines()
        if line.strip().startswith("TOTAL")
    )
    total = float(total_line.split("$")[1])
    # Research plus composition on one article is cents. If this band moves,
    # either the pricing table or the fakes' token counts changed.
    assert 0.01 < total < 1.0, total
    assert "guard:document" in walkthrough_output


def test_the_network_is_severed_before_any_stage_runs():
    """A live HTTP call from the walkthrough's process must be impossible."""
    probe = (
        "import scripts.e2e_walkthrough as w, httpx\n"
        "try:\n"
        "    httpx.Client().get('https://api.openai.com/v1/models')\n"
        "except w.NetworkBlockedError:\n"
        "    print('BLOCKED')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "BLOCKED" in result.stdout, result.stderr[-2000:]
