"""Connector factory: selects the active driver from config (cached singleton)."""
from .. import config
from .base import Connector, NullConnector

_instance: Connector | None = None


def get_connector() -> Connector:
    global _instance
    if _instance is not None:
        return _instance
    if config.CONNECTORS_ENABLED and config.CONNECTORS_PROVIDER == "composio":
        from .composio_driver import ComposioConnector
        _instance = ComposioConnector()
    else:
        _instance = NullConnector()
    return _instance
