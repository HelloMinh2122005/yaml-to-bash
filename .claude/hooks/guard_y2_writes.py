#!/usr/bin/env python3
"""Claude write guard hook for y2 squad roles."""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Optional

PATTERNS = {
    "y2-lead": [
        re.compile(
            r"^\.handoffs/[^/]+/(state\.json|00-brief\.md|10-research-plan\.md|2[0-3]-[^/]+\.md|31-answers\.md|50-acceptance-c[0-9]+\.md|90-retro\.md|adr/ADR-[0-9]{3}-[^/]+\.md)$"
        ),
        re.compile(r"^\.claude/squad-learnings\.md$"),
    ],
    "y2-planner": [
        re.compile(r"^\.handoffs/[^/]+/(21-plan|22-tasks)\.md$"),
    ],
    "y2-research-planner": [
        re.compile(r"^\.handoffs/[^/]+/10-research-plan\.md$"),
    ],
    "y2-test-planner": [
        re.compile(r"^\.handoffs/[^/]+/23-test-plan\.md$"),
    ],
}


def decide(role: Optional[str], rel_path: str) -> Optional[str]:
    """Decide if role is allowed to write to rel_path. Returns None if allowed."""
    clean_path = rel_path.replace("\\", "/")
    if not role or role not in PATTERNS:
        return None

    patterns = PATTERNS[role]
    for pat in patterns:
        if pat.match(clean_path):
            return None

    return f"Role '{role}' is not allowed to write to '{clean_path}'"


def main() -> None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            sys.stderr.write("guard_y2_writes: empty input on stdin\n")
            sys.exit(2)
        payload = json.loads(raw)
    except Exception as e:
        sys.stderr.write(f"guard_y2_writes: unparseable stdin JSON: {e}\n")
        sys.exit(2)

    # Determine role
    role = payload.get("agent_type")
    if role and ":" in role:
        role = role.split(":")[-1]
    if not role and len(sys.argv) > 1:
        role = sys.argv[1]

    tool_input = payload.get("tool_input", {})
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not file_path:
        # No file target identified, allow
        sys.exit(0)

    project_root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    real_root = os.path.realpath(project_root)

    if os.path.isabs(file_path):
        real_file = os.path.realpath(file_path)
    else:
        real_file = os.path.realpath(os.path.join(real_root, file_path))

    try:
        rel_path = os.path.relpath(real_file, real_root)
    except ValueError:
        sys.stderr.write(f"guard_y2_writes: file outside project root: {file_path}\n")
        sys.exit(2)

    if rel_path.startswith("..") or os.path.isabs(rel_path):
        sys.stderr.write(f"guard_y2_writes: path traversal outside project root: {file_path}\n")
        sys.exit(2)

    refusal = decide(role, rel_path)
    if refusal is not None:
        sys.stderr.write(f"guard_y2_writes: denied: {refusal}\n")
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()

