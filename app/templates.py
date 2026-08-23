"""Prebuilt agent templates.

A template is a fully-configured agent a brand-new org can instantiate and run on
day one — no connectors or credentials required — so the first run produces a real
result and populates the impact view. Each template carries an instruction prompt,
a baseline (minutes of manual work it replaces), and any skills to attach.

Templates rely only on built-in tools (web + shell). `suggested_connectors` are
hints surfaced in the UI, never a hard requirement to run.
"""

TEMPLATES = [
    {
        "key": "news-digest",
        "name": "Daily news digest",
        "description": "Researches the latest developments on a topic and writes a "
                       "tight, sourced bullet digest. Runnable immediately; set a "
                       "daily routine once you like the output.",
        "prompt": ("Research the most important developments from the last 24-48 hours "
                   "on the topic of \"AI agents and enterprise automation\". Produce a "
                   "digest of 5-7 bullets. Each bullet: one sentence of what happened "
                   "plus why it matters, and a source link. End with a one-line "
                   "\"watch next\" note."),
        "system_prompt": "You are a concise research analyst. Prefer primary sources. "
                         "Never pad; every bullet must carry new information.",
        "baseline_minutes": 30,
        "schedule_kind": "once",
        "skills": [{
            "name": "concise-digest",
            "description": "Format research into a scannable, sourced bullet digest.",
            "instructions": ("When asked to summarize or digest information, output a "
                             "short title line, then 5-7 bullets. Each bullet is a "
                             "single sentence with the key fact and its significance, "
                             "followed by a source URL in parentheses. No preamble, no "
                             "conclusion paragraph. Keep the whole digest under 200 words."),
        }],
        "suggested_connectors": ["slack"],
    },
    {
        "key": "site-watcher",
        "name": "Website change watcher",
        "description": "Checks a web page and reports whether it is up and what changed "
                       "since the notes in its workspace. Great with an on-failure or "
                       "result-contains alert.",
        "prompt": ("Fetch https://news.ycombinator.com and report: (1) whether the page "
                   "loaded successfully, and (2) the top 5 story titles right now. If a "
                   "file notes/last_check.md exists in your workspace, compare against it "
                   "and call out what changed. Then overwrite notes/last_check.md with "
                   "today's top 5 titles for next time."),
        "system_prompt": "You are a reliable monitoring agent. Be factual and terse. "
                         "State clearly whether the check passed or failed.",
        "baseline_minutes": 15,
        "schedule_kind": "once",
        "skills": [],
        "suggested_connectors": [],
    },
]

_BY_KEY = {t["key"]: t for t in TEMPLATES}


def list_templates() -> list[dict]:
    """Public catalog shape for the UI (omits internal prompt/system_prompt detail)."""
    return [{
        "key": t["key"],
        "name": t["name"],
        "description": t["description"],
        "baseline_minutes": t.get("baseline_minutes"),
        "schedule_kind": t.get("schedule_kind", "once"),
        "skills": [s["name"] for s in t.get("skills", [])],
        "suggested_connectors": t.get("suggested_connectors", []),
    } for t in TEMPLATES]


def get(key: str) -> dict | None:
    return _BY_KEY.get(key)
