"""Thin entry-point: ``python -m scripts.run_pipeline``.

The systemd timer on the VPS invokes this module — keep the import path
stable. The real implementation lives in :mod:`pipeline.run`; this file
exists solely so ``-m scripts.run_pipeline`` keeps working without
duplicating the orchestrator.

Historical note: this file used to be a hand-maintained copy of
``pipeline/run.py`` and drifted out of sync (IT_PROJ_NTS_023 found it
missing the IT_PROJ_NTS_021 voice-profile hardening). Don't reintroduce
duplication — edit ``pipeline/run.py`` and let this delegate.
"""

from __future__ import annotations

from pipeline.run import app

if __name__ == "__main__":
    app()
