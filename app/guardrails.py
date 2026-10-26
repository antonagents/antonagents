"""Guardrails on what an agent may do through its connectors.

Every connector call goes through the action gateway (app/gateway.py), so the
policy here is enforced outside the agent's container — the agent can't talk
its way around it, and it never holds the connector key.

Each action is classified from its tool slug, then the routine's policy decides:
  allow    run it now (always recorded)
  approve  hold it; a person approves, then the platform runs it exactly once
  block    refuse it
"""
import re
from typing import Optional

CATEGORIES = {
    "read":        "Read data (get, list, search, fetch)",
    "write":       "Create or change things (create, update, upload)",
    "message":     "Message people (send, post, reply, invite, share)",
    "destructive": "Delete, remove or cancel things",
    "money":       "Move money (pay, charge, refund, transfer)",
    "code":        "Run code in the connector's sandbox (can't be inspected)",
    "admin":       "Change connections or permissions",
}
DECISIONS = ("allow", "approve", "block")

# unattended routines: messages, deletes and payments wait for a person;
# opaque code execution and connection changes are refused
ROUTINE_DEFAULT = {"read": "allow", "write": "allow", "message": "approve",
                   "destructive": "approve", "money": "approve", "code": "block",
                   "admin": "block"}
# a chat has a person watching: everything runs, everything is recorded
CHAT_DEFAULT = {k: "allow" for k in CATEGORIES}

# meta-tools of the Composio tool router
DISCOVERY_TOOLS = {"COMPOSIO_SEARCH_TOOLS", "COMPOSIO_GET_TOOL_SCHEMAS"}
EXECUTE_TOOL = "COMPOSIO_MULTI_EXECUTE_TOOL"
CODE_TOOLS = {"COMPOSIO_REMOTE_WORKBENCH", "COMPOSIO_REMOTE_BASH_TOOL"}
ADMIN_TOOLS = {"COMPOSIO_MANAGE_CONNECTIONS"}

_VERBS = [   # first match wins, most dangerous first
    ("money", r"PAY|PAYMENT|PAYOUT|CHARGE|REFUND|TRANSFER_FUNDS|PURCHASE|CAPTURE|INVOICE_SEND|SEND_MONEY|WITHDRAW"),
    ("destructive", r"DELETE|REMOVE|DESTROY|PURGE|ERASE|TRASH|ARCHIVE|CANCEL|REVOKE|BAN|BLOCK|KICK|"
                    r"UNSUBSCRIBE|DISABLE|DEACTIVATE|RESET|DROP|CLOSE|TRANSFER|WIPE|EMPTY"),
    ("admin", r"PERMISSION|COLLABORATOR|ADMIN|ROLE|ACCESS|AUTH|TOKEN|SECRET|WEBHOOK|DEPLOY_KEY|MEMBER"),
    ("message", r"SEND|POST|REPLY|FORWARD|PUBLISH|INVITE|SHARE|SHARING|NOTIFY|COMMENT|MENTION|BROADCAST|TWEET|"
                r"DM|MESSAGE|EMAIL|SMS|CALL|REACT"),
    ("read", r"GET|LIST|SEARCH|FETCH|FIND|READ|RETRIEVE|QUERY|DESCRIBE|DOWNLOAD|EXPORT|CHECK|VIEW|COUNT|LOOKUP"),
    ("write", r"CREATE|UPDATE|ADD|SET|EDIT|PATCH|PUT|UPLOAD|MOVE|RENAME|INSERT|UPSERT|MERGE|ASSIGN|STAR|"
              r"LABEL|TAG|COPY|IMPORT|SYNC|RUN|TRIGGER|START|STOP|ENABLE"),
]
_VERB_RX = [(cat, re.compile(rf"(?:^|_)(?:{pat})(?:_|$)")) for cat, pat in _VERBS]


def classify(slug: str) -> str:
    """Category of one connector action, from its slug (e.g. GMAIL_SEND_EMAIL).

    1. a leading read verb (GET_MESSAGE, LIST_WEBHOOKS) is a read — reading about a
       dangerous thing isn't dangerous;
    2. otherwise money / destructive / permission words anywhere win (CREATE_REFUND,
       ADD_A_REPOSITORY_COLLABORATOR);
    3. then talking to people (SEND_EMAIL, CREATE_AN_ISSUE_COMMENT) — drafts excepted;
    4. then the leading verb; unknown verbs count as writes, never as reads."""
    s = (slug or "").upper()
    if s in CODE_TOOLS:
        return "code"
    if s in ADMIN_TOOLS:
        return "admin"
    body = s.split("_", 1)[1] if "_" in s else s      # past the toolkit prefix
    first = body.split("_", 1)[0]
    rx = dict(_VERB_RX)
    if rx["read"].fullmatch(first) or rx["read"].search(f"_{first}_"):
        return "read"
    for cat in ("money", "destructive", "admin"):
        if rx[cat].search(body):
            return cat
    if rx["message"].search(body) and "DRAFT" not in body.split("_"):
        return "message"
    if rx["write"].search(f"_{first}_"):
        return "write"
    return "write"


def policy_for(agent: dict) -> dict:
    """The routine's effective policy: defaults by kind, then its own overrides."""
    base = dict(CHAT_DEFAULT if (agent.get("kind") or "routine") == "chat" else ROUTINE_DEFAULT)
    custom = agent.get("action_policy") or {}
    for k, v in (custom.get("categories") or {}).items():
        if k in CATEGORIES and v in DECISIONS:
            base[k] = v
    return {"categories": base,
            "tools": {k.upper(): v for k, v in (custom.get("tools") or {}).items() if v in DECISIONS}}


def decide(slug: str, policy: dict) -> tuple[str, str]:
    """(decision, category) for one action. A per-tool rule beats its category."""
    cat = classify(slug)
    rule = policy["tools"].get((slug or "").upper())
    return (rule or policy["categories"].get(cat, "approve")), cat


def normalize(body: Optional[dict]) -> dict:
    """Validate a policy from the API. Raises ValueError."""
    body = body or {}
    cats = {}
    for k, v in (body.get("categories") or {}).items():
        if k not in CATEGORIES:
            raise ValueError(f"unknown category '{k}'")
        if v not in DECISIONS:
            raise ValueError(f"'{k}' must be one of {', '.join(DECISIONS)}")
        cats[k] = v
    tools = {}
    for k, v in (body.get("tools") or {}).items():
        slug = str(k).strip().upper()
        if not re.fullmatch(r"[A-Z0-9_]{3,120}", slug):
            raise ValueError(f"'{k}' isn't a tool slug (e.g. SLACK_SEND_MESSAGE)")
        if v not in DECISIONS:
            raise ValueError(f"'{slug}' must be one of {', '.join(DECISIONS)}")
        tools[slug] = v
    return {"categories": cats, "tools": tools}


def prompt_note(policy: dict) -> str:
    """Told to the agent up front, so it doesn't waste attempts on refused actions."""
    held = [CATEGORIES[k].split(" (")[0].lower() for k, v in policy["categories"].items() if v == "approve"]
    blocked = [CATEGORIES[k].split(" (")[0].lower() for k, v in policy["categories"].items() if v == "block"]
    if not held and not blocked:
        return ""
    parts = ["CONNECTOR GUARDRAILS: every connector action goes through a gateway that records it."]
    if held:
        parts.append("Actions that " + "; ".join(held) + " are HELD for a person's approval — the "
                     "gateway queues them and runs them exactly as you requested once approved. "
                     "Request each such action once, then carry on; never look for another way "
                     "to do it.")
    if blocked:
        parts.append("Not allowed here: " + "; ".join(blocked) + ".")
    return " ".join(parts)
