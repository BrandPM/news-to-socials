"""End-to-end smoke test: one peg → all brands × languages × channels.

For Stage 5. Pushes a synthetic Topic through the full pipeline in
``--dry-run`` mode so we can verify that the multi-brand parametrisation
works without spamming production channels.

Output: a markdown matrix in ``docs/validate-multibrand-<date>.md`` showing
which (brand, language, channel) combinations produced a Post.
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from pipeline.common.config import get_settings
from pipeline.common.logging import configure_logging, get_logger

log = get_logger(__name__)


async def main() -> int:
    configure_logging()
    settings = get_settings()
    settings.dry_run = True  # type: ignore[misc]

    # TODO(stage-5): wire this once the pipeline orchestrator exists.
    # The shape of the run is:
    #   1. pick the synthetic Topic (Visa launches X)
    #   2. for each brand in Directus.brands.active=true:
    #        for each language in brand.languages:
    #            generate Draft
    #            for each Channel routing to that brand+language:
    #                format Post via adapter
    #                "dispatch" (dry-run prints destination)
    #   3. summarise → matrix
    log.info("validate.placeholder", note="see TODO; wire after Stage 4 completes")

    matrix: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    out = Path(f"docs/validate-multibrand-{datetime.utcnow():%Y%m%d}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        f.write("# Multibrand validation\n\n(placeholder; populate post-Stage-5)\n")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
