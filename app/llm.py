"""One host-side model call (used by the job planner).

Every configured provider speaks the Anthropic Messages API (that is how the
task harness reaches GLM via Ollama, Z.ai, DeepInfra …), so the server can make
a single completion through the same providers: the routed default first, then
any other configured provider as a fallback.
"""
import json
import logging
import os
import re
from typing import Optional

import httpx

from . import config, providers

log = logging.getLogger("antonagents.llm")


class LLMError(Exception):
    pass


def _candidates(prefer: Optional[str] = None) -> list:
    first = providers.get(prefer or config.DEFAULT_PROVIDER) or providers.get(config.DEFAULT_PROVIDER)
    rest = [p for p in providers.all() if p is not first]
    return [p for p in ([first] if first else []) + rest if p and providers.is_configured(p)]


async def complete(system: str, user: str, *, max_tokens: int = 4000,
                   timeout: float = 120, provider: Optional[str] = None,
                   model: Optional[str] = None) -> str:
    """Return the text of one completion, trying providers in order. `provider`
    / `model` pick the first choice (e.g. a separate judge model)."""
    errors = []
    for p in _candidates(provider):
        base = (p.base_url or "https://api.anthropic.com").rstrip("/")
        tok = os.environ.get(p.auth_env, "")
        headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
        headers.update(providers.extra_headers(p))
        if p.token_target == "ANTHROPIC_API_KEY":
            headers["x-api-key"] = tok
        else:
            headers["authorization"] = f"Bearer {tok}"
        use = model if (model and provider == p.name) else p.default_model
        body = {"model": use, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": user}]}
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post(f"{base}/v1/messages", headers=headers, json=body)
            if r.status_code >= 300:
                errors.append(f"{p.name}: HTTP {r.status_code} {r.text[:160]}")
                continue
            data = r.json()
            text = "".join(b.get("text", "") for b in data.get("content", [])
                           if b.get("type") == "text").strip()
            if text:
                return text
            errors.append(f"{p.name}: empty answer")
        except (httpx.HTTPError, ValueError) as e:
            errors.append(f"{p.name}: {type(e).__name__}: {e}")
    raise LLMError("; ".join(errors) or "no model provider is configured")


def extract_json(text: str) -> Optional[dict]:
    """Pull the first JSON object out of a model answer (tolerates ```json fences)."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    cand = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    try:
        v = json.loads(cand)
        return v if isinstance(v, dict) else None
    except ValueError:
        return None
