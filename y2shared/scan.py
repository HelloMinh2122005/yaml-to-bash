"""Static scanner for variables written by steps."""

from __future__ import annotations

import re
from typing import Any


def scan_written_vars(step: Any, pattern: re.Pattern) -> set[str]:
    """
    Statically scan a step body or script file for written variable names.
    pattern: regex pattern supplied by Profile (captures variable name in group 1).
    Returns set of variable names (excluding reserved __Y2B_*).
    """
    if isinstance(step, str):
        body = step
    elif isinstance(step, dict):
        sp = step.get("script_path")
        if sp:
            try:
                with open(sp, "r", encoding="utf-8") as f:
                    body = f.read()
            except Exception:
                body = ""
        else:
            body = step.get("script") or ""
    else:
        # Step dataclass or similar
        sp = getattr(step, "script_path", None)
        if sp:
            try:
                with open(sp, "r", encoding="utf-8") as f:
                    body = f.read()
            except Exception:
                body = ""
        else:
            body = getattr(step, "script", None) or ""

    matches = pattern.findall(body)
    return {m for m in matches if not m.startswith("__Y2B_")}


def build_var_to_step_map(job: Any, pattern: re.Pattern) -> dict[str, int]:
    """
    Map written variables to 1-based step index (later step overwrites earlier).
    """
    var_to_step: dict[str, int] = {}
    rc = getattr(getattr(job, "invoke", None), "runcommand", None)
    if rc and getattr(rc, "steps", None):
        for i, step in enumerate(rc.steps, start=1):
            for v in scan_written_vars(step, pattern):
                var_to_step[v] = i
    return var_to_step

