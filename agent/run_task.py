#!/usr/bin/env python3
"""In-container task runner.

Executed as the entrypoint INSIDE each ephemeral task container. Reads a task
spec as JSON from stdin, drives the Claude Agent SDK to completion, and emits
one JSON event per line on stdout. The host-side runner (app/runner.py) parses
those lines, persists them, and streams them to clients.

The container is the security sandbox: it runs as a non-root user with no host
mounts, resource limits, and only the env vars the task was given. Inside that
boundary the agent runs autonomously (permission_mode=bypassPermissions) so it
can actually do the work without a human approving each tool call.

Spec (stdin JSON):
    {
      "prompt": "<the task>",              # required
      "system_prompt": "<override>",       # optional
      "model": "claude-opus-4-8",          # optional
      "permission_mode": "bypassPermissions",  # optional
      "allowed_tools": [...],              # optional; omit for full builtin set
      "max_turns": 60,                     # optional
      "cwd": "/workspace",                 # optional
      "resume": "<session_id>",            # optional; continue a prior session
      "fork_session": false,               # optional; fork instead of continue
      "mcp_servers": {...}                 # optional; remote MCP servers (connectors)
    }
"""
import json
import os
import sys
import traceback

import anyio

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    UserMessage,
    SystemMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    ToolResultBlock,
)

DEFAULT_SYSTEM = (
    "You are an autonomous problem-solving agent running inside an isolated "
    "sandbox container. You are given a task and must carry it out end to end "
    "using the tools available (shell, files, web, and any provided access). "
    "You are running without a human watching in real time, so do not ask for "
    "confirmation — for reversible actions that follow from the task, proceed. "
    "Work until the task is genuinely complete or you are blocked on something "
    "only the requester can provide. When you finish, end with a clear summary "
    "of what you did, what you found, and any output locations. Report outcomes "
    "faithfully: if something failed, say so with the evidence."
)


def emit(event: dict) -> None:
    """Write one JSON event line to stdout and flush immediately."""
    sys.stdout.write(json.dumps(event, default=str) + "\n")
    sys.stdout.flush()


def _materialize_skills(skills) -> None:
    """Write attached skills to the CLI's skills dir so the Agent SDK loads them.

    Each skill becomes `<CLAUDE_CONFIG_DIR>/skills/<slug>/SKILL.md` with the
    standard name/description frontmatter plus the instructions as the body.
    """
    import os
    import pathlib
    import re

    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    skills_dir = pathlib.Path(base) / "skills"
    for sk in skills or []:
        name = (sk.get("name") or "skill").strip()
        slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "skill"
        desc = (sk.get("description") or name).replace("\n", " ").strip()
        body = sk.get("instructions") or ""
        d = skills_dir / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {slug}\ndescription: {desc}\n---\n\n{body}\n"
        )


def _blocks(content) -> list:
    """Normalize a message .content into a list of blocks."""
    if content is None:
        return []
    if isinstance(content, list):
        return content
    return [content]


async def run(spec: dict) -> int:
    prompt = spec.get("prompt")
    if not prompt:
        emit({"type": "error", "message": "spec.prompt is required"})
        return 2

    opt_kwargs = dict(
        system_prompt=spec.get("system_prompt") or DEFAULT_SYSTEM,
        permission_mode=spec.get("permission_mode") or "bypassPermissions",
        cwd=spec.get("cwd") or "/workspace",
    )
    if spec.get("model"):
        opt_kwargs["model"] = spec["model"]
    if spec.get("allowed_tools"):
        opt_kwargs["allowed_tools"] = spec["allowed_tools"]
    if spec.get("max_turns"):
        opt_kwargs["max_turns"] = int(spec["max_turns"])
    # follow-up: resume the prior conversation by session id. The transcript
    # lives on the persistent /workspace volume (CLAUDE_CONFIG_DIR), so the CLI
    # can replay it in this fresh container.
    if spec.get("resume"):
        opt_kwargs["resume"] = spec["resume"]
    if spec.get("fork_session"):
        opt_kwargs["fork_session"] = bool(spec["fork_session"])
    # connector tools (e.g. Composio per-user MCP server): a remote MCP server
    # config injected by the host runner. strict_mcp_config keeps it deterministic.
    if spec.get("mcp_servers"):
        opt_kwargs["mcp_servers"] = spec["mcp_servers"]
        opt_kwargs["strict_mcp_config"] = True

    _materialize_skills(spec.get("skills"))

    options = ClaudeAgentOptions(**opt_kwargs)

    emit({"type": "started", "prompt": prompt, "model": spec.get("model")})

    exit_code = 0
    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for b in _blocks(msg.content):
                    if isinstance(b, TextBlock):
                        emit({"type": "assistant_text", "text": b.text})
                    elif isinstance(b, ThinkingBlock):
                        emit({"type": "thinking", "text": getattr(b, "thinking", "")})
                    elif isinstance(b, ToolUseBlock):
                        emit({
                            "type": "tool_use",
                            "name": b.name,
                            "input": b.input,
                        })
            elif isinstance(msg, UserMessage):
                for b in _blocks(msg.content):
                    if isinstance(b, ToolResultBlock):
                        emit({
                            "type": "tool_result",
                            "is_error": bool(getattr(b, "is_error", False)),
                            "content": _stringify(getattr(b, "content", "")),
                        })
            elif isinstance(msg, SystemMessage):
                # The SDK emits SystemMessage frames for session init and assorted
                # runtime notices (reminders, compaction, etc.). None carry
                # user-facing value and they flood the run log as repeated,
                # content-less "system" lines — so we don't surface them. The run's
                # start is already marked by the "started" event above, and the
                # session id is captured from the ResultMessage below.
                continue
            elif isinstance(msg, ResultMessage):
                is_error = bool(getattr(msg, "is_error", False))
                emit({
                    "type": "result",
                    "subtype": getattr(msg, "subtype", None),
                    "is_error": is_error,
                    "num_turns": getattr(msg, "num_turns", None),
                    "cost_usd": getattr(msg, "total_cost_usd", None),
                    "session_id": getattr(msg, "session_id", None),
                    "result": getattr(msg, "result", None),
                })
                if is_error:
                    exit_code = 1
    except Exception as e:  # surface any SDK/runtime failure as an event
        emit({
            "type": "error",
            "message": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
        })
        exit_code = 1

    emit({"type": "done", "exit_code": exit_code})
    return exit_code


def _stringify(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict):
                out.append(part.get("text") or json.dumps(part, default=str))
            else:
                out.append(str(getattr(part, "text", part)))
        return "\n".join(out)
    return str(content)


def main() -> int:
    raw = sys.stdin.read()
    try:
        spec = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        emit({"type": "error", "message": f"invalid spec JSON: {e}"})
        return 2
    return anyio.run(run, spec)


if __name__ == "__main__":
    sys.exit(main())
