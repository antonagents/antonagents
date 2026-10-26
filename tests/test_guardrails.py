"""Guardrails: how connector actions are classified and what a routine may do."""
import pytest

from app import guardrails as g


@pytest.mark.parametrize("slug,cat", [
    ("GMAIL_SEND_EMAIL", "message"),
    ("GMAIL_GET_MESSAGE", "read"),            # reading about a message is a read
    ("GMAIL_LIST_MESSAGES", "read"),
    ("GMAIL_CREATE_EMAIL_DRAFT", "write"),    # a draft sends nothing
    ("SLACK_CHAT_POST_MESSAGE", "message"),
    ("GITHUB_CREATE_AN_ISSUE", "write"),
    ("GITHUB_CREATE_AN_ISSUE_COMMENT", "message"),
    ("GITHUB_DELETE_A_REPOSITORY", "destructive"),
    ("GITHUB_ADD_A_REPOSITORY_COLLABORATOR", "admin"),
    ("GITHUB_GET_A_WEBHOOK", "read"),
    ("GITHUB_CREATE_A_REPOSITORY_WEBHOOK", "admin"),
    ("STRIPE_CREATE_REFUND", "money"),
    ("STRIPE_LIST_CHARGES", "read"),
    ("SHOPIFY_CANCEL_ORDER", "destructive"),
    ("GOOGLEDRIVE_ADD_FILE_SHARING_PREF", "message"),
    ("COMPOSIO_REMOTE_WORKBENCH", "code"),
    ("COMPOSIO_MANAGE_CONNECTIONS", "admin"),
    ("NOTION_SOMETHING_NEW", "write"),        # unknown verbs are never treated as reads
])
def test_classify(slug, cat):
    assert g.classify(slug) == cat


def test_routine_defaults_hold_messages_deletes_and_money():
    p = g.policy_for({"kind": "routine"})
    assert g.decide("GITHUB_LIST_REPOSITORY_ISSUES", p)[0] == "allow"
    assert g.decide("CLICKUP_CREATE_TASK", p)[0] == "allow"
    for slug in ("SLACK_SEND_MESSAGE", "GITHUB_DELETE_A_REPOSITORY", "STRIPE_CREATE_REFUND"):
        assert g.decide(slug, p)[0] == "approve"
    assert g.decide("COMPOSIO_REMOTE_WORKBENCH", p)[0] == "block"


def test_chats_allow_everything():
    p = g.policy_for({"kind": "chat"})
    assert all(v == "allow" for v in p["categories"].values())


def test_overrides_and_tool_rules():
    agent = {"kind": "routine", "action_policy": g.normalize(
        {"categories": {"destructive": "allow", "write": "block"},
         "tools": {"github_create_an_issue": "approve"}})}
    p = g.policy_for(agent)
    assert g.decide("GITHUB_DELETE_A_REPOSITORY", p)[0] == "allow"
    assert g.decide("CLICKUP_CREATE_TASK", p)[0] == "block"
    assert g.decide("GITHUB_CREATE_AN_ISSUE", p)[0] == "approve"   # tool rule beats category


@pytest.mark.parametrize("bad", [
    {"categories": {"nuke": "allow"}},
    {"categories": {"read": "maybe"}},
    {"tools": {"not a slug!": "allow"}},
])
def test_normalize_rejects_bad_policies(bad):
    with pytest.raises(ValueError):
        g.normalize(bad)
