#!/usr/bin/env python3
"""Antigravity PreToolUse hook: Protect handoff protocol, claude files, lab guards, and superpowers docs."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

ALLOWED_HANDOFF = re.compile(
    r"^\.handoffs/[^/]+/(state\.json|11-research-findings\.md|30-questions\.md|40-delivery-report\.md|41-diff\.patch|42-evidence/.+|43-code-review-c[0-9]+\.md|44-debug-log\.md)$"
)

Y2_DEVELOPER_AGENTS = {
    "y2-engineer",
    "y2-test-engineer",
    "y2-azure-tester",
    "y2-reviewer",
    "y2-debugger",
    "y2-researcher",
}

APP_CODE_PATTERN = re.compile(
    r"^(y2b|y2p|y2shared|bin|examples|tests|pipelines)/"
)


def read_first_step(transcript_path: str) -> Optional[dict]:
    if not transcript_path or not os.path.exists(transcript_path):
        return None
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    return json.loads(line)
                except Exception:
                    return None
    except Exception:
        return None


def decide_write(rel_path: str, is_lead: bool = False) -> Optional[str]:
    """Check if write to rel_path is allowed. Returns None if allowed, or denial reason."""
    clean = rel_path.replace("\\", "/")
    if clean.startswith("./"):
        clean = clean[2:]

    if clean == ".handoffs/PROTOCOL.md" or clean.endswith("/.handoffs/PROTOCOL.md"):
        return "Denied: .handoffs/PROTOCOL.md is locked and owner-only."

    if clean.startswith(".claude/") or "/.claude/" in clean:
        return "Denied: .claude/ is owned by the Claude squad."

    if clean.startswith("docs/superpowers/") or "/docs/superpowers/" in clean:
        return "Denied: docs/superpowers/ is owned by the owner and Claude planning squad."

    if clean.startswith("lab/") or "/lab/" in clean:
        return "Denied: lab/ safety rails and scripts are protected from automated modifications."

    if (
        clean.startswith(".agents/plugins/y2/scripts/")
        or "/.agents/plugins/y2/scripts/" in clean
        or clean.startswith("scripts/")
        or "/scripts/" in clean
    ):
        return "Denied: Guard scripts cannot be modified."

    if (
        clean == ".agents/plugins/y2/hooks.json"
        or clean.endswith("/.agents/plugins/y2/hooks.json")
        or clean == "hooks.json"
        or clean.endswith("/hooks.json")
    ):
        return "Denied: Guard configuration hooks.json cannot be modified."

    if clean.startswith(".handoffs/") or "/.handoffs/" in clean:
        # Extract relative path from .handoffs/
        idx = clean.find(".handoffs/")
        sub_path = clean[idx:]
        if not ALLOWED_HANDOFF.match(sub_path):
            return f"Denied: Antigravity squad may not write to '{clean}'. See .handoffs/PROTOCOL.md §2."

    # Enforce lead delegation constraint
    if is_lead and APP_CODE_PATTERN.match(clean):
        return (
            f"Denied: y2-tech-lead is prohibited from authoring application code or tests ('{clean}'). "
            f"As lead, your role is to Think, Plan, Delegate, and Review. "
            f"Delegate this task via invoke_subagent: code -> y2-engineer, tests -> y2-test-engineer / y2-azure-tester, debugging -> y2-debugger."
        )

    return None


def main() -> None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            print(json.dumps({"decision": "deny", "reason": "empty hook input"}))
            return
        payload = json.loads(raw)
        tool_call = payload.get("toolCall", {})
        args = tool_call.get("args", {})

        # Resolve repository root from __file__ location
        cur = Path(__file__).resolve().parent
        repo_root = cur.parent.parent.parent.parent
        while repo_root.parent != repo_root and not ((repo_root / "CLAUDE.md").exists() or (repo_root / ".git").exists()):
            repo_root = repo_root.parent
        real_root = os.path.realpath(str(repo_root))

        # Check caller identity
        transcript_path = payload.get("transcriptPath", "")
        last_user_input = payload.get("lastUserInput", "")
        first_step = read_first_step(transcript_path)
        content = str(first_step.get("content", "")) if first_step else ""

        is_dev = any(f"[agent: {dev}]" in content or f"[agent: {dev}]" in last_user_input for dev in Y2_DEVELOPER_AGENTS)
        is_lead = not is_dev and ("y2-tech-lead" in last_user_input or "y2-tech-lead" in content or "[agent: y2-tech-lead]" in content)

        # Find path arguments (matching /(file|path)/i but not content)
        content_keys = {"codecontent", "content", "replacement", "target", "replacement_text", "new_content", "old_content"}
        paths_to_check: list[str] = []

        for k, v in args.items():
            if isinstance(v, str) and k.lower() not in content_keys:
                if re.search(r"(file|path)", k, re.IGNORECASE):
                    paths_to_check.append(v)

        for p in paths_to_check:
            # Normalise relative to repo_root
            if os.path.isabs(p):
                real_p = os.path.realpath(p)
            else:
                real_p = os.path.realpath(os.path.join(os.getcwd(), p))

            try:
                rel_p = os.path.relpath(real_p, real_root)
            except ValueError:
                rel_p = p

            refusal = decide_write(rel_p, is_lead=is_lead)
            if refusal is not None:
                print(json.dumps({"decision": "deny", "reason": refusal}))
                return

        print(json.dumps({"decision": "allow"}))
    except Exception as e:
        print(json.dumps({"decision": "deny", "reason": f"Hook exception: {e}"}))


if __name__ == "__main__":
    main()
