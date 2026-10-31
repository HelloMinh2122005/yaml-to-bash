"""Policy checker for az CLI invocations in the lab environment."""

from __future__ import annotations

import urllib.parse
from typing import Optional
from labenv import LabEnv, is_lab_subscription

ALLOWED_POST_ACTIONS = {"runCommand", "start", "deallocate", "powerOff"}


def _parse_az_args(argv: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """Parse argv into positional words and option mappings."""
    positionals: list[str] = []
    options: dict[str, list[str]] = {}

    i = 0
    n = len(argv)
    while i < n:
        tok = argv[i]
        if tok.startswith("-"):
            if "=" in tok:
                key, val = tok.split("=", 1)
                options.setdefault(key, []).append(val)
                i += 1
            elif tok == "--ids":
                vals: list[str] = []
                i += 1
                while i < n and not argv[i].startswith("-"):
                    vals.append(argv[i])
                    i += 1
                options["--ids"] = vals
            else:
                flag = tok
                if i + 1 < n and not argv[i + 1].startswith("-"):
                    options.setdefault(flag, []).append(argv[i + 1])
                    i += 2
                else:
                    options.setdefault(flag, []).append("")
                    i += 1
        else:
            positionals.append(tok)
            i += 1

    return positionals, options


def _get_opt(options: dict[str, list[str]], *flags: str) -> Optional[str]:
    for flag in flags:
        if flag in options and options[flag]:
            return options[flag][0]
    return None


def check_az_args(argv: list[str], lab: LabEnv) -> Optional[str]:
    """Check if az arguments are allowed under lab guard policy.
    
    Returns None if allowed, or a refusal reason string if denied.
    """
    for token in argv:
        if "rg-tfstate" in token.lower():
            return "forbidden resource group 'rg-tfstate' mentioned in arguments"

    positionals, options = _parse_az_args(argv)
    if not positionals:
        return "empty command"

    # 1. account show
    if positionals[:2] == ["account", "show"]:
        return None

    # 2. account set --subscription X
    if positionals[:2] == ["account", "set"]:
        sub = _get_opt(options, "-s", "--subscription")
        if sub and is_lab_subscription(sub, lab):
            return None
        return f"account set refused: subscription '{sub}' is not the lab subscription"

    # 3. vm commands
    if positionals[0] == "vm":
        if positionals == ["vm", "list"]:
            cmd = "vm list"
        elif len(positionals) >= 2 and positionals[1] in ("show", "get-instance-view", "start", "deallocate"):
            cmd = f"vm {positionals[1]}"
        elif len(positionals) >= 3 and positionals[1] == "run-command" and positionals[2] in ("invoke", "show", "list"):
            cmd = f"vm run-command {positionals[2]}"
        else:
            return f"vm subcommand '{' '.join(positionals)}' is not allowed"

        sub = _get_opt(options, "-s", "--subscription")
        if sub and not is_lab_subscription(sub, lab):
            return f"subscription '{sub}' is not the lab subscription"

        ids = options.get("--ids")
        if ids is not None:
            if not ids:
                return "--ids flag provided without arguments"
            for vm_id in ids:
                parts = vm_id.strip().split("/")
                if len(parts) != 9:
                    return f"invalid VM id format '{vm_id}'"
                if parts[1].lower() != "subscriptions" or not is_lab_subscription(parts[2], lab):
                    return f"VM id '{vm_id}' does not target the lab subscription"
                if parts[3].lower() != "resourcegroups" or parts[4].lower() != lab.resource_group.lower():
                    return f"VM id '{vm_id}' does not target the lab resource group '{lab.resource_group}'"
                if parts[5].lower() != "providers" or parts[6].lower() != "microsoft.compute" or parts[7].lower() != "virtualmachines":
                    return f"VM id '{vm_id}' has invalid resource type"
                if parts[8].lower() not in lab.vms:
                    return f"VM id '{vm_id}' targets unknown VM '{parts[8]}'"
            return None

        rg = _get_opt(options, "-g", "--resource-group")
        if not rg:
            return f"{cmd} requires -g/--resource-group"
        if rg.lower() != lab.resource_group.lower():
            return f"resource group '{rg}' is not the lab resource group '{lab.resource_group}'"

        if cmd == "vm list":
            return None

        name = _get_opt(options, "-n", "--name")
        if not name:
            return f"{cmd} requires -n/--name"
        if name.lower() not in lab.vms:
            return f"VM '{name}' is not in the lab VM list {lab.vms}"
        return None

    # 4. rest
    if positionals[0] == "rest":
        method = (_get_opt(options, "-m", "--method") or "get").lower()
        url = _get_opt(options, "-u", "--url", "--uri")
        if not url:
            return "rest command requires --url"

        prefix = "https://management.azure.com"
        if url.lower().startswith(prefix):
            url = url[len(prefix):]
        if "?" in url:
            url = url.split("?", 1)[0]
        path = urllib.parse.unquote(url)

        expected_sub = lab.subscription_id.lower()
        expected_rg = lab.resource_group.lower()
        expected_prefix = f"/subscriptions/{expected_sub}/resourcegroups/{expected_rg}/"

        if method == "get":
            if path.lower().startswith(expected_prefix):
                return None
            return f"rest GET url '{path}' does not start with '{expected_prefix}'"
        elif method == "post":
            parts = path.strip().split("/")
            if len(parts) != 10:
                return f"rest POST url '{path}' does not match expected VM action path"
            if parts[1].lower() != "subscriptions" or not is_lab_subscription(parts[2], lab):
                return f"rest POST url '{path}' subscription mismatch"
            if parts[3].lower() != "resourcegroups" or parts[4].lower() != expected_rg:
                return f"rest POST url '{path}' resource group mismatch"
            if parts[5].lower() != "providers" or parts[6].lower() != "microsoft.compute" or parts[7].lower() != "virtualmachines":
                return f"rest POST url '{path}' provider mismatch"
            if parts[8].lower() not in lab.vms:
                return f"rest POST url '{path}' unknown VM '{parts[8]}'"
            action = parts[9]
            if action not in ALLOWED_POST_ACTIONS:
                return f"rest POST action '{action}' is not in allowed actions: {ALLOWED_POST_ACTIONS}"
            return None
        else:
            return f"rest method '{method}' is not allowed"

    return f"command '{' '.join(positionals)}' is not permitted by lab guard policy"
