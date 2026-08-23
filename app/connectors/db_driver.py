"""Native database connectors (PostgreSQL, MySQL, MongoDB).

Unlike the SaaS connectors (OAuth via Composio), a DB connector is a stored
connection string. The DSN is encrypted at rest (Fernet, keyed off
config.SECRET_KEY) and only decrypted at run launch, then injected into the
task container as an env var so the agent can query with the CLI clients
(psql / mysql) or pymongo, all present in the task image.
"""
import base64
import hashlib
import re
from typing import Optional

from .. import config

ENGINES = {
    "postgres": "PostgreSQL",
    "mysql": "MySQL",
    "mongo": "MongoDB",
}


def engines() -> list[dict]:
    return [{"slug": k, "label": v} for k, v in ENGINES.items()]


def _fernet():
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(hashlib.sha256(config.SECRET_KEY.encode()).digest())
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode("ascii")


def decrypt(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode("ascii")).decode()


def env_var(name: str) -> str:
    """Env var the DSN is injected as inside the container (e.g. SUPERAGENT_DB_ORDERS)."""
    slug = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") or "DB"
    return f"SUPERAGENT_DB_{slug}"


def effective_dsn(engine: str, dsn: str, mode: str) -> str:
    """Best-effort read-only hardening at the DSN level, where the engine supports it."""
    if mode == "read_only" and engine == "postgres" and "default_transaction_read_only" not in dsn:
        sep = "&" if "?" in dsn else "?"
        return dsn + f"{sep}options=-c%20default_transaction_read_only%3Don"
    return dsn


def client_hint(engine: str) -> str:
    return {
        "postgres": "psql",
        "mysql": "mysql",
        "mongo": "python + pymongo",
    }.get(engine, engine)
