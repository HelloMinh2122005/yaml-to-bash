#!/usr/bin/env python3
"""Antigravity PreToolUse hook: Guard real Azure commands and secrets."""

from __future__ import annotations

import json
import re
import sys
from typing import Optional

FORBIDDEN_SECRET_SUBSTRINGS = (
    "terraform.tfvars",
    ".tfstate",
    ".config/y2-lab",
    "sp.env",
)


def decide_command(cmd: str) -> Optional[str]:
    """Check command line string. Returns None if allowed, or refusal string."""
    stripped = cmd.strip()

    if re.search(r"\bterraform\b", cmd, re.IGNORECASE):
        return "Denied: terraform invocations are strictly forbidden for all agents."

    cmd_lower = cmd.lower()
    for secret in FORBIDDEN_SECRET_SUBSTRINGS:
        if secret in cmd_lower:
            return f"Denied: command mentions sensitive lab file/directory '{secret}'."

    # Check for direct az or direct y2b/y2p run
    has_az = bool(re.search(r"\baz\b", cmd))
    has_y2_run = bool(re.search(r"\by2[bp]\s+run\b", cmd))

    if has_az or has_y2_run:
        # Must start with lab/run-lab.sh or ./lab/run-lab.sh
        if not (stripped.startswith("lab/run-lab.sh") or stripped.startswith("./lab/run-lab.sh")):
            return "Denied: Azure interactions and fleet runs must go through 'lab/run-lab.sh'."

    return None


def decide_read(path: str) -> Optional[str]:
    """Check if viewing path is allowed. Returns None if allowed, or refusal string."""
    clean = path.replace("\\", "/")
    clean_lower = clean.lower()

    if "terraform.tfvars" in clean_lower:
        return "Denied: access to terraform.tfvars is forbidden."

    if ".tfstate" in clean_lower:
        return "Denied: access to terraform state is forbidden."

    if "/lab/.terraform/" in clean or clean.startswith("lab/.terraform/") or "/.terraform/" in clean:
        return "Denied: access to .terraform internal files is forbidden."

    if "/.config/y2-lab/" in clean or clean.startswith(".config/y2-lab") or "/.config/y2-lab" in clean:
        return "Denied: access to ~/.config/y2-lab secrets is forbidden."

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

        # Check commands
        for k, v in args.items():
            if isinstance(v, str) and re.search(r"command", k, re.IGNORECASE):
                refusal = decide_command(v)
                if refusal is not None:
                    print(json.dumps({"decision": "deny", "reason": refusal}))
                    return

        # Check paths
        content_keys = {"codecontent", "content", "replacement", "target", "replacement_text"}
        for k, v in args.items():
            if isinstance(v, str) and k.lower() not in content_keys:
                if re.search(r"(file|path)", k, re.IGNORECASE):
                    refusal = decide_read(v)
                    if refusal is not None:
                        print(json.dumps({"decision": "deny", "reason": refusal}))
                        return

        print(json.dumps({"decision": "allow"}))
    except Exception as e:
        print(json.dumps({"decision": "deny", "reason": f"Hook exception: {e}"}))


if __name__ == "__main__":
    main()
