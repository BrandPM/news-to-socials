"""Pricing constants for paid services (OpenAI, Replicate).

These constants drive the ``cost_records`` write in every paid call
site. Update them when the providers change their published rates.

Sources (verify when prices change — provider URLs):

* OpenAI: https://openai.com/api/pricing/  /  https://developers.openai.com/api/docs/pricing
  - gpt-5.5          : $5.00 / 1M input, $30.00 / 1M output ($0.50 cached in)
                       reasoning tokens are billed within output tokens.
  - gpt-4o          : $2.50 / 1M input, $10.00 / 1M output
  - gpt-4o-mini     : $0.15 / 1M input, $0.60 / 1M output
  - text-embedding-3-small : $0.02 / 1M input

* Replicate: https://replicate.com/black-forest-labs/flux-1.1-pro
  - flux-1.1-pro    : approx $0.04 per image (billed per-second under
    the hood). We record both ``duration_seconds`` and a fixed
    per-image dollar so the trend graph is comparable across runs even
    if Replicate's per-second rate drifts.

Last verified: 2026-06-18.
"""

from __future__ import annotations


# OpenAI: USD per token (= per-1M-tokens / 1e6).
OPENAI_PRICING_PER_1M: dict[str, dict[str, float]] = {
    # input/output cost in USD per 1M tokens
    # GPT-5.5 reasoning model (NTS_064 — prompt_analysis). Reasoning tokens
    # are part of the output total, so the output rate already covers them.
    "gpt-5.5": {"input": 5.00, "output": 30.00},
    "gpt-5.5-2026-04-23": {"input": 5.00, "output": 30.00},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-2024-11-20": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o-mini-2024-07-18": {"input": 0.15, "output": 0.60},
    # Embeddings are input-only — completion side stays 0.
    "text-embedding-3-small": {"input": 0.02, "output": 0.0},
    "text-embedding-3-large": {"input": 0.13, "output": 0.0},
}


# Replicate: estimated per-image cost for Flux 1.1 Pro. Replicate bills
# per-second, but for the dashboard a fixed per-image figure is more
# legible. Adjust whenever the published rate changes.
REPLICATE_PER_IMAGE_USD: dict[str, float] = {
    "black-forest-labs/flux-1.1-pro": 0.04,
    "black-forest-labs/flux-pro": 0.055,
    "black-forest-labs/flux-1.1-pro-ultra": 0.06,
}


def openai_cost(
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> float:
    """Compute the USD cost of an OpenAI call from its usage payload.

    Falls back to 0.0 when ``model`` is unknown or tokens are missing.
    The unknown-model fallback is intentional: pricing entries can drift
    behind reality, but a missing cost row is worse than an inaccurate
    one — so we record what we can.
    """
    if prompt_tokens is None and completion_tokens is None:
        return 0.0
    rates = OPENAI_PRICING_PER_1M.get(model)
    if rates is None:
        return 0.0
    in_rate = rates.get("input", 0.0) / 1_000_000.0
    out_rate = rates.get("output", 0.0) / 1_000_000.0
    cost = (prompt_tokens or 0) * in_rate + (completion_tokens or 0) * out_rate
    return round(cost, 6)


def replicate_image_cost(model: str) -> float:
    """Return the USD cost per image for a Replicate model. 0.0 if unknown."""
    return REPLICATE_PER_IMAGE_USD.get(model, 0.0)
