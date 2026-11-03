"""Pure builders for Azure CLI command lines and comparison contract (ADR-002)."""

from __future__ import annotations

import os
import re
import urllib.parse
from pathlib import Path
from typing import Dict, List, Sequence, Union

KIND_SUB_RESOLVE = "sub-resolve"
KIND_POWER_STATE = "power-state"
KIND_VM_START = "vm-start"
KIND_INVOKE = "invoke"
KIND_READER = "reader"
KIND_DEALLOCATE = "deallocate"
KIND_POWEROFF = "powerOff"

ENGINE_KINDS = (
    KIND_SUB_RESOLVE,
    KIND_POWER_STATE,
    KIND_VM_START,
    KIND_INVOKE,
    KIND_READER,
    KIND_DEALLOCATE,
    KIND_POWEROFF,
)

COMPUTE_API_VERSION = "2024-07-01"
RUN_COMMAND_API_VERSION = COMPUTE_API_VERSION

THROTTLE_BACKOFFS_S = (15, 30, 60, 120, 240)
BUSY_RETRIES = 3
BUSY_WAIT_S = 60
UNKNOWN_RETRY_WAIT_S = 30

_SECRET_FLAG_RE = re.compile(r"^--[a-zA-Z0-9_-]*(?:password|secret|token|key)[a-zA-Z0-9_-]*$", re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(r"<id-of:([^>]+)>")


def _redact_argv_local(argv: Sequence[str]) -> List[str]:
    """Redact secret flags in an argv list."""
    out: List[str] = []
    skip_next = False
    for i, token in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if token.startswith("--") and "=" in token:
            k, v = token.split("=", 1)
            if _SECRET_FLAG_RE.match(k):
                out.append(f"{k}=***")
                continue
        elif token.startswith("--") and _SECRET_FLAG_RE.match(token):
            out.append(token)
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                out.append("***")
                skip_next = True
            continue
        out.append(token)
    return out


def _basename(path_str: str) -> str:
    """Extract basename handling both Unix and Windows separators."""
    clean = path_str.replace("\\", "/")
    return clean.split("/")[-1]


def build_sub_resolve(subscription: str) -> List[str]:
    return ["az", "account", "show", "--subscription", subscription, "--query", "id", "-o", "tsv"]


def build_power_state(subscription: str, rg: str, vm: str) -> List[str]:
    return [
        "az",
        "vm",
        "get-instance-view",
        "--subscription",
        subscription,
        "-g",
        rg,
        "-n",
        vm,
        "--query",
        "instanceView.statuses[?starts_with(code, 'PowerState/')].code",
        "-o",
        "tsv",
    ]


def build_vm_start(subscription: str, rg: str, vm: str) -> List[str]:
    return [
        "az",
        "vm",
        "start",
        "--subscription",
        subscription,
        "-g",
        rg,
        "-n",
        vm,
    ]


def build_run_command_post(
    subscription_id: str,
    rg: str,
    vm: str,
    body_path: Union[Path, str],
) -> List[str]:
    url = (
        f"/subscriptions/{subscription_id}"
        f"/resourceGroups/{urllib.parse.quote(rg, safe='')}"
        f"/providers/Microsoft.Compute/virtualMachines/{urllib.parse.quote(vm, safe='')}"
        f"/runCommand?api-version={RUN_COMMAND_API_VERSION}"
    )
    return ["az", "rest", "--method", "post", "--url", url, "--body", f"@{body_path}"]


def build_reader_invoke(
    subscription: str,
    rg: str,
    vm: str,
    command_id: str,
    collect_script_path: Union[Path, str],
) -> List[str]:
    return [
        "az",
        "vm",
        "run-command",
        "invoke",
        "--subscription",
        subscription,
        "-g",
        rg,
        "-n",
        vm,
        "--command-id",
        command_id,
        "--scripts",
        f"@{collect_script_path}",
        "-o",
        "json",
    ]


def build_power_post(
    subscription_id: str,
    rg: str,
    vm: str,
    action: str,
) -> List[str]:
    url = (
        f"/subscriptions/{subscription_id}"
        f"/resourceGroups/{urllib.parse.quote(rg, safe='')}"
        f"/providers/Microsoft.Compute/virtualMachines/{urllib.parse.quote(vm, safe='')}"
        f"/{action}?api-version={COMPUTE_API_VERSION}"
    )
    return ["az", "rest", "--method", "post", "--url", url]


def argv_matches(
    plan_argv: Sequence[str],
    runtime_argv: Sequence[str],
    bindings: Dict[str, str],
) -> bool:
    """Compare a planned az argv against an executed runtime argv per N1-N4 rules."""
    p_redacted = _redact_argv_local(plan_argv)
    r_redacted = _redact_argv_local(runtime_argv)

    if len(p_redacted) != len(r_redacted):
        return False

    for p, r in zip(p_redacted, r_redacted):
        if p.startswith("@") and r.startswith("@"):
            if f"@{_basename(p[1:])}" != f"@{_basename(r[1:])}":
                return False
            continue

        if "<id-of:" in p:
            # Extract placeholder name
            matches = list(_PLACEHOLDER_RE.finditer(p))
            if not matches:
                if p != r:
                    return False
                continue

            # Build regex replacing <id-of:NAME> with ([0-9a-fA-F-]{36})
            pattern = "^"
            last_end = 0
            names = []
            for m in matches:
                pattern += re.escape(p[last_end:m.start()])
                pattern += r"([0-9a-fA-F-]{36})"
                names.append(m.group(1))
                last_end = m.end()
            pattern += re.escape(p[last_end:]) + "$"

            m_r = re.match(pattern, r)
            if not m_r:
                return False

            for idx, name in enumerate(names, start=1):
                guid = m_r.group(idx)
                if name in bindings and bindings[name] != guid:
                    return False
                bindings[name] = guid
            continue

        if p != r:
            return False

    return True

