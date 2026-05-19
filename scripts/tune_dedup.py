"""Sweep dedup thresholds over a labelled fixture.

Usage:
    python scripts/tune_dedup.py path/to/labeled.jsonl

Where each line is:
    {"text_a": "...", "text_b": "...", "is_duplicate": true|false}

Output: a markdown table of FP/FN rates at different (cosine, entity_overlap)
threshold combinations, written to ``docs/dedup-tuning-<date>.md``.

Embeddings: by default uses sentence-transformers all-MiniLM-L6-v2 (small,
local, no API key). For a multilingual run, set ``MODEL=intfloat/multilingual-e5-small``
and reuse the same pattern meridian uses.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np

from pipeline.selector.dedup import cosine, extract_entities


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 64

    fixture_path = Path(sys.argv[1])
    if not fixture_path.is_file():
        print(f"Not found: {fixture_path}", file=sys.stderr)
        return 66

    # Lazy import so this file is still importable without ML extras.
    from sentence_transformers import SentenceTransformer

    model_name = os.environ.get("MODEL", "all-MiniLM-L6-v2")
    print(f"Loading {model_name} …")
    model = SentenceTransformer(model_name)

    pairs: list[tuple[str, str, bool]] = []
    with fixture_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            pairs.append((row["text_a"], row["text_b"], bool(row["is_duplicate"])))

    if not pairs:
        print("Empty fixture", file=sys.stderr)
        return 65

    embs_a = model.encode([p[0] for p in pairs], normalize_embeddings=True)
    embs_b = model.encode([p[1] for p in pairs], normalize_embeddings=True)

    cosine_grid = [0.75, 0.80, 0.85, 0.90, 0.95]
    overlap_grid = [0.0, 0.5, 0.7, 0.9]

    results: list[tuple[float, float, int, int, int]] = []
    for ct in cosine_grid:
        for ot in overlap_grid:
            counts: Counter[str] = Counter()
            for (a, b, label), va, vb in zip(pairs, embs_a, embs_b, strict=True):
                sim = cosine(np.asarray(va), np.asarray(vb))
                if sim < ct:
                    pred = False
                else:
                    ea = extract_entities(a)
                    eb = extract_entities(b)
                    if not ea and not eb:
                        overlap = 1.0
                    elif not ea or not eb:
                        overlap = 0.0
                    else:
                        overlap = len(ea & eb) / min(len(ea), len(eb))
                    pred = overlap >= ot
                key = ("TP" if (label and pred) else
                       "FP" if (not label and pred) else
                       "FN" if (label and not pred) else "TN")
                counts[key] += 1
            results.append((ct, ot, counts["FP"], counts["FN"], counts["TP"] + counts["TN"]))

    out_path = Path(f"docs/dedup-tuning-{datetime.utcnow():%Y%m%d}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as out:
        out.write(f"# Dedup tuning ({fixture_path.name}, model={model_name})\n\n")
        out.write("| cosine | entity_overlap | FP | FN | Correct |\n")
        out.write("|---:|---:|---:|---:|---:|\n")
        for ct, ot, fp, fn, correct in results:
            out.write(f"| {ct:.2f} | {ot:.2f} | {fp} | {fn} | {correct} |\n")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
