"""What a run really cost — step 5 of docs/features/outcome-jobs.md.

The Agent SDK reports `total_cost_usd` at Anthropic list prices whatever model
actually served the run, so for GLM/OpenAI routes it is a guess. Every run now
records its provider, model and token usage; cost is usage × the provider's
price table (providers.json → "pricing", USD per million tokens) when one is
configured. Otherwise the SDK figure is kept and labelled an estimate.

  "pricing": {"glm-5.2": {"input": 0.6, "output": 2.2, "cache_read": 0.11},
              "*": {...}}            # "*" = any model of this provider
"""
from typing import Optional

from . import providers

METERED, ESTIMATE = "metered", "estimate"
_KEYS = {"input": "input_tokens", "output": "output_tokens",
         "cache_read": "cache_read_input_tokens", "cache_write": "cache_creation_input_tokens"}


def tokens(usage: Optional[dict]) -> int:
    if not isinstance(usage, dict):
        return 0
    return sum(int(usage.get(k) or 0) for k in _KEYS.values())


def price_for(provider: Optional[str], model: Optional[str]) -> Optional[dict]:
    p = providers.get(provider or "")
    table = getattr(p, "pricing", None) or {}
    return table.get(model or "") or table.get("*")


def cost(provider: Optional[str], model: Optional[str], usage: Optional[dict],
         sdk_cost: Optional[float]) -> tuple[Optional[float], str]:
    """(cost_usd, basis) for one run."""
    price = price_for(provider, model)
    if price and isinstance(usage, dict):
        usd = sum(float(price.get(k) or 0) * int(usage.get(f) or 0) for k, f in _KEYS.items()) / 1e6
        return round(usd, 6), METERED
    if provider == "anthropic" and sdk_cost is not None:
        return sdk_cost, METERED          # the SDK's prices are Anthropic's own
    return sdk_cost, ESTIMATE
