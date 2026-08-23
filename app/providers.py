"""LLM provider registry.

The Claude Agent SDK / claude CLI selects a backend via environment variables:
an Anthropic-compatible base URL + auth token + model. Any provider that speaks
the Anthropic protocol can therefore be plugged in — native Anthropic, or GLM
via Z.ai's Anthropic-compatible endpoint, or a local LiteLLM/gateway fronting
other models.

A Provider says: where its endpoint is, which *server* env var holds its secret,
which env var that secret should be exposed as *inside the task container*, and
which models it offers. Secrets live only in the server environment — this
registry stores the env var NAME, never the value.

Built-ins (Anthropic + Z.ai/GLM) can be fully overridden by a providers.json
file next to the project root.
"""
import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from . import config


@dataclass
class Provider:
    name: str
    label: str
    base_url: Optional[str]      # None => native Anthropic default endpoint
    auth_env: str                # server env var that holds the secret
    token_target: str            # env var to expose the secret as in the container
    models: list[str]
    default_model: str
    extra_env: dict[str, str] = field(default_factory=dict)  # passthrough into container
    # If true and configured, the live model list is fetched from the provider's
    # gateway (LiteLLM /v1/models) so the UI reflects exactly what's routable —
    # `models` above is the fallback when the gateway is unreachable.
    dynamic_models: bool = False


_BUILTINS: list[Provider] = [
    Provider(
        name="anthropic", label="Anthropic (Claude)",
        base_url=None, auth_env="ANTHROPIC_API_KEY", token_target="ANTHROPIC_API_KEY",
        models=["claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5"],
        default_model="claude-opus-4-8",
    ),
    Provider(
        name="zai", label="Z.ai (GLM)",
        base_url="https://api.z.ai/api/anthropic",
        auth_env="ZAI_API_KEY", token_target="ANTHROPIC_AUTH_TOKEN",
        models=["glm-5.2", "glm-4.7", "glm-4.5-air"],
        default_model="glm-5.2",
        extra_env={"API_TIMEOUT_MS": "3000000"},
    ),
]

_registry: dict[str, Provider] = {}


def load() -> None:
    """Load built-ins, then apply providers.json overrides if present."""
    global _registry
    provs = list(_BUILTINS)
    path = config.PROVIDERS_FILE
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        provs = [Provider(**p) for p in data]  # file fully defines the set
    _registry = {p.name: p for p in provs}


def all() -> list[Provider]:
    return list(_registry.values())


def get(name: str) -> Optional[Provider]:
    return _registry.get(name)


def find_by_model(model: str) -> Optional[Provider]:
    for p in _registry.values():
        if model in p.models:
            return p
    return None


def is_configured(p: Provider) -> bool:
    return bool(os.environ.get(p.auth_env))


def container_env(provider: str, model: str) -> dict[str, str]:
    """Env vars to inject into the task container for this provider+model.

    Raises ValueError if the provider is unknown or its key isn't set.
    Returns exactly one auth var (setting both ANTHROPIC_API_KEY and
    ANTHROPIC_AUTH_TOKEN makes the API reject the request).
    """
    p = get(provider)
    if p is None:
        raise ValueError(f"unknown provider '{provider}'")
    token = os.environ.get(p.auth_env)
    if not token:
        raise ValueError(f"provider '{provider}' needs env {p.auth_env} to be set")
    env = {p.token_target: token}
    if p.base_url:
        env["ANTHROPIC_BASE_URL"] = p.base_url
    env.update(p.extra_env)
    return env


def to_public() -> list[dict]:
    """Provider info safe to expose over the API (no secrets)."""
    return [
        {
            "name": p.name, "label": p.label,
            "models": p.models, "default_model": p.default_model,
            "configured": is_configured(p),
        }
        for p in _registry.values()
    ]


# Live model lists fetched from gateway providers, cached briefly so /api/providers
# stays fast (and a down gateway isn't re-polled every call).
_models_cache: dict[str, tuple[float, Optional[list[str]]]] = {}
_MODELS_TTL = 60.0


async def gateway_models(p: Provider) -> Optional[list[str]]:
    """For a gateway-backed provider (e.g. OpenAI via LiteLLM), fetch the models it
    actually serves from `{base_url}/v1/models`. Returns None if not applicable or
    the gateway is unreachable (caller falls back to the static list)."""
    if not (p.dynamic_models and p.base_url and is_configured(p)):
        return None
    now = time.monotonic()
    cached = _models_cache.get(p.name)
    if cached and (now - cached[0]) < _MODELS_TTL:
        return cached[1]
    result: Optional[list[str]] = None
    try:
        import httpx
        token = os.environ.get(p.auth_env)
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(p.base_url.rstrip("/") + "/v1/models",
                                  headers={"Authorization": f"Bearer {token}"})
            r.raise_for_status()
            ids = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
            if ids:
                result = ids
    except Exception:  # noqa: BLE001 — unreachable/misconfigured gateway → fall back
        result = None
    _models_cache[p.name] = (now, result)
    return result


async def to_public_dynamic() -> list[dict]:
    """Like to_public(), but replaces a gateway provider's model list with what the
    gateway actually serves (when reachable)."""
    out = to_public()
    by_name = {p.name: p for p in _registry.values()}
    for d in out:
        p = by_name[d["name"]]
        live = await gateway_models(p)
        if live:
            d["models"] = live
            d["models_source"] = "gateway"
            if d["default_model"] not in live:
                d["default_model"] = live[0]
    return out
