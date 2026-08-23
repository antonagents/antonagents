"""Per-task model router: choose (provider, model) for a task.

Precedence:
  1. Explicit  — the task specifies provider and/or model.
  2. Rules     — keyword rules from routing.json, matched against the prompt.
  3. Default   — SUPERAGENT_DEFAULT_PROVIDER + that provider's default model.

routing.json (optional) is a list of rules, evaluated in order:
  [
    {"contains": ["migrate", "refactor", "architect"], "provider": "anthropic", "model": "claude-opus-4-8"},
    {"contains": ["summarize", "classify", "format"],   "provider": "zai",       "model": "glm-4.5-air"},
    {"provider": "zai", "model": "glm-5.2"}          # catch-all (no "contains")
  ]

This keeps routing transparent and cheap. An LLM-classifier strategy can be
added later by inserting a step between rules and default.
"""
import json
import os

from . import config, providers


def _rules() -> list[dict]:
    path = config.ROUTING_FILE
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _default() -> tuple[str, str]:
    prov = config.DEFAULT_PROVIDER
    p = providers.get(prov) or (providers.all()[0] if providers.all() else None)
    if p is None:
        raise ValueError("no providers configured")
    return p.name, p.default_model


def route(task: dict) -> tuple[str, str]:
    """Return (provider_name, model) for a task dict (may contain 'provider'/'model'/'prompt')."""
    prov = task.get("provider")
    model = task.get("model")

    # 1. explicit
    if prov and model:
        return prov, model
    if model:
        p = providers.find_by_model(model)
        return (p.name if p else config.DEFAULT_PROVIDER), model
    if prov:
        p = providers.get(prov)
        return prov, (p.default_model if p else _default()[1])

    # 2. keyword rules
    prompt = (task.get("prompt") or "").lower()
    for rule in _rules():
        needles = rule.get("contains")
        if needles and not any(n.lower() in prompt for n in needles):
            continue
        r_prov = rule.get("provider") or config.DEFAULT_PROVIDER
        p = providers.get(r_prov)
        r_model = rule.get("model") or (p.default_model if p else None)
        if r_prov and r_model:
            return r_prov, r_model

    # 3. default
    return _default()
