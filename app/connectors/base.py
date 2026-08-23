"""Vendor-neutral connector interface.

A connector manages a user's authenticated links to SaaS apps (Slack, Linear,
Gmail, ...) and, at task-run time, provisions the tools the agent uses to act on
the user's behalf. All vendor SDK calls live in a concrete driver
(e.g. composio_driver.py) behind this interface, so swapping the vendor
(Composio -> Nango later) never touches the runner, API, or UI.

Methods are synchronous (the vendor SDKs are sync); async callers wrap them with
`asyncio.to_thread` so they don't block the event loop. Drivers are DB-agnostic —
persistence of connection state lives in app/db.py, driven by the API layer.
"""
from typing import Optional


class Connector:
    name = "none"

    def providers(self) -> list[dict]:
        """Apps this driver can connect, as [{'slug','label'}]."""
        return []

    def initiate(self, user_id: int, provider: str) -> dict:
        """Begin a user's OAuth connection. Returns {'redirect_url', 'ref'}."""
        raise NotImplementedError

    def status(self, user_id: int, provider: str) -> str:
        """Current connection status: 'active' | 'pending' | 'none'."""
        return "none"

    def disconnect(self, user_id: int, provider: str) -> None:
        """Revoke/remove the user's connection to a provider."""
        return None

    def agent_provisioning(self, user_id: int, providers: list[str]) -> Optional[dict]:
        """Tools to inject into the agent container for this user.

        Given the user's currently-active providers, return a normalized dict the
        runner knows how to inject, e.g. {"mcp_servers": {...}}. Return None when
        there is nothing to provision.
        """
        return None


class NullConnector(Connector):
    """Used when no connector is configured; the app runs with no integrations."""

    def initiate(self, user_id: int, provider: str) -> dict:
        raise RuntimeError("connectors are not configured")
