#!/usr/bin/env python3
"""Antigravity PreToolUse hook: Protect handoff protocol, claude files, lab guards, and superpowers docs."""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Optional

ALLOWED_HANDOFF = re.compile(
    r"^\.handoffs/[^/]+/(state\.json|11-research-findings\.md|30-questions\.md|40-delivery-report\.md|41-diff\.patch|42-evidence/.+|43-code-review-c[0-9]+\.md|44-debug-log\.md)$"
)


def decide_write(rel_path: str) -> Optional[str]:
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

    if clean.startswith(".agents/plugins/y2/scripts/") or "/.agents/plugins/y2/scripts/" in clean:
        return "Denied: Guard scripts cannot be modified."

    if clean == ".agents/plugins/y2/hooks.json" or clean.endswith("/.agents/plugins/y2/hooks.json"):
        return "Denied: Guard configuration hooks.json cannot be modified."

    if clean.startswith(".handoffs/") or "/.handoffs/" in clean:
        # Extract relative path from .handoffs/
        idx = clean.find(".handoffs/")
        sub_path = clean[idx:]
        if not ALLOWED_HANDOFF.match(sub_path):
            return f"Denied: Antigravity squad may not write to '{clean}'. See .handoffs/PROTOCOL.md §2."

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

        repo_root = os.getcwd()

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
                real_root = os.path.realpath(repo_root)
                try:
                    rel_p = os.path.relpath(real_p, real_root)
                except ValueError:
                    rel_p = p
            else:
                rel_p = p

            refusal = decide_write(rel_p)
            if refusal is not None:
                print(json.dumps({"decision": "deny", "reason": refusal}))
                return

        print(json.dumps({"decision": "allow"}))
    except Exception as e:
        print(json.dumps({"decision": "deny", "reason": f"Hook exception: {e}"}))


if __name__ == "__main__":
    main()
