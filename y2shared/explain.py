"""Explain subcommand and plan renderers (AC-7, AC-8, AC-9, AC-10)."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence

from y2shared.inputcsv import read_input_csv, select_target_rows
from y2shared.plan import build_plan
from y2shared.profile import Profile
from y2shared.validate import validate_job_file


def render_text(plan: Dict[str, Any]) -> str:
    """Render structured plan as plain text with runnable az commands."""
    lines: List[str] = []
    fleet = plan["fleet"]
    tool = plan["tool"]
    cmd_id = plan["command_id"]
    api_ver = plan["api_version"]

    lines.append("=" * 80)
    lines.append(f"# {tool} Azure Execution Plan")
    lines.append(f"# Tool: {tool} ({cmd_id}) · API Version: {api_ver}")
    lines.append(f"# Job: {plan['job']}")
    lines.append(
        f"# Fleet: {fleet['vm_count']} VMs · Max run commands: {fleet['max_run_commands']} (2 x eligible VMs)"
    )
    lines.append("# Subscriptions:")
    for sub_info in fleet["subscriptions"]:
        guid_str = "GUID" if sub_info["is_guid"] else "name"
        lines.append(f"#   - {sub_info['value']} ({sub_info['vm_count']} VMs, {guid_str})")

    lines.append(
        f"# Payload: {fleet['payload']['file']} (sha256: {fleet['payload']['sha256']}, run_id={fleet['payload']['run_id']})"
    )
    mutations_str = ", ".join(fleet["mutations"])
    lines.append(f"# Reachable Azure mutations: {mutations_str}")

    timing = fleet["timing"]
    lines.append(
        f"# Timing: sleep_before_collect={timing['sleep_before_collect_s']}s, "
        f"collect_retry_interval={timing['collect_retry_interval_s']}s, "
        f"collect_retries={timing['collect_retries']}"
    )

    lines.append("# Retry rules:")
    for rule in fleet["retry_rules"]:
        lines.append(f"#   - {rule}")

    if fleet.get("resolutions"):
        lines.append("# Name subscription resolutions:")
        for res in fleet["resolutions"]:
            argv_str = shlex.join(res["argv"])
            lines.append(f"#   - {res['subscription']} -> {res['placeholder']}")
            lines.append(argv_str)

    lines.append("# Notes:")
    for note in fleet["notes"]:
        lines.append(f"#   - {note}")
    lines.append("=" * 80)
    lines.append("")

    total_vms = len(plan["vms"])
    for vm_plan in plan["vms"]:
        idx = vm_plan["index"]
        vname = vm_plan["vm_name"]
        rg = vm_plan["resource_group"]
        sub = vm_plan["subscription"]

        lines.append(
            f"# ===== VM {idx}/{total_vms}: {vname} · {rg} · sub {sub} · {tool} ({cmd_id})"
        )

        for phase_block in vm_plan["phases"]:
            phase = phase_block["phase"]
            title = phase_block["title"]
            lines.append(f"# ---------- {title} ----------")

            for step in phase_block["steps"]:
                stype = step["type"]
                if stype == "command":
                    when_str = f" [{step['when']}]" if step.get("when") else ""
                    shared_str = " (shared)" if step.get("shared") else ""
                    lines.append(f"# [command:{step['kind']}]{when_str}{shared_str}")
                    lines.append(shlex.join(step["argv"]))

                elif stype == "precheck":
                    cache_desc = ""
                    if step.get("cache") == "subscription":
                        if step.get("cache_role") == "owner":
                            shared_list = ", ".join(step.get("shared_with") or [])
                            cache_desc = f" · cache=subscription (shared by {vname}"
                            if shared_list:
                                cache_desc += f", {shared_list})"
                            else:
                                cache_desc += ")"
                        elif step.get("cache_role") == "reuse":
                            cache_desc = f" · cache=subscription (result reused from {step.get('cached_from')})"

                    lines.append(f"# [precheck] {step['name']} · host bash{cache_desc}")
                    lines.append("#   script (display only; at runtime values come from env):")
                    for scr_line in step["script"]["text"].splitlines():
                        lines.append(f"#     {scr_line}")

                    lines.append("#   branches:")
                    start_steps = []
                    for br in step["branches"]:
                        extra = ""
                        if br.get("outcome"):
                            extra = f" (nothing below runs -> {br['outcome']})"
                        elif br.get("after_job"):
                            extra = f" (after the job: {br['after_job']})"
                        lines.append(f"#     {br['key']} -> {br['log']}{extra}")
                        if br.get("steps") and not start_steps:
                            start_steps = br["steps"]

                    if step.get("unmatched"):
                        lines.append(f"#     unmatched -> {step['unmatched']}")

                    if start_steps:
                        lines.append("#   when start / start-stop:")
                        for cmd in start_steps:
                            lines.append(shlex.join(cmd["argv"]))

                elif stype == "host_script":
                    sec = step["section"]
                    name = step["name"]
                    lines.append(f"# [{sec}] {name} · host bash")
                    lines.append("#   script (display only; at runtime values come from env):")
                    for scr_line in step["script"]["text"].splitlines():
                        lines.append(f"#     {scr_line}")

                elif stype == "restore":
                    lines.append("# [power] restore branches per previous state:")
                    for br in step["branches"]:
                        lines.append(f"#   prev={br['prev_state']} -> action: {br['action']}")
                        for cmd in br["steps"]:
                            lines.append(shlex.join(cmd["argv"]))

                elif stype == "note":
                    lines.append(f"# {step['text']}")

            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_md(plan: Dict[str, Any]) -> str:
    """Render structured plan as Markdown with runnable az commands in code blocks."""
    lines: List[str] = []
    fleet = plan["fleet"]
    tool = plan["tool"]
    cmd_id = plan["command_id"]
    api_ver = plan["api_version"]

    lines.append(f"# {tool} Azure Execution Plan")
    lines.append("")
    lines.append(f"- **Tool:** `{tool}` (`{cmd_id}`) · **API Version:** `{api_ver}`")
    lines.append(f"- **Job:** `{plan['job']}`")
    lines.append(
        f"- **Fleet:** {fleet['vm_count']} VMs · **Max run commands:** {fleet['max_run_commands']} (2 x eligible VMs)"
    )
    lines.append("- **Subscriptions:**")
    for sub_info in fleet["subscriptions"]:
        guid_str = "GUID" if sub_info["is_guid"] else "name"
        lines.append(f"  - `{sub_info['value']}` ({sub_info['vm_count']} VMs, {guid_str})")

    lines.append(
        f"- **Payload:** `{fleet['payload']['file']}` (sha256: `{fleet['payload']['sha256']}`, `run_id={fleet['payload']['run_id']}`)"
    )
    mutations_str = ", ".join(f"`{m}`" for m in fleet["mutations"])
    lines.append(f"- **Reachable Azure mutations:** {mutations_str}")

    timing = fleet["timing"]
    lines.append(
        f"- **Timing:** `sleep_before_collect={timing['sleep_before_collect_s']}s`, "
        f"`collect_retry_interval={timing['collect_retry_interval_s']}s`, "
        f"`collect_retries={timing['collect_retries']}`"
    )

    lines.append("- **Retry rules:**")
    for rule in fleet["retry_rules"]:
        lines.append(f"  - {rule}")

    if fleet.get("resolutions"):
        lines.append("- **Name subscription resolutions:**")
        for res in fleet["resolutions"]:
            lines.append(f"  - `{res['subscription']}` -> `{res['placeholder']}`")
            lines.append("```bash")
            lines.append(shlex.join(res["argv"]))
            lines.append("```")

    lines.append("- **Notes:**")
    for note in fleet["notes"]:
        lines.append(f"  - {note}")
    lines.append("")

    total_vms = len(plan["vms"])
    for vm_plan in plan["vms"]:
        idx = vm_plan["index"]
        vname = vm_plan["vm_name"]
        rg = vm_plan["resource_group"]
        sub = vm_plan["subscription"]

        lines.append(f"## VM {idx}/{total_vms}: {vname} (`{rg}`, `{sub}`)")
        lines.append("")

        for phase_block in vm_plan["phases"]:
            title = phase_block["title"]
            lines.append(f"### {title}")
            lines.append("")

            for step in phase_block["steps"]:
                stype = step["type"]
                if stype == "command":
                    when_str = f" ({step['when']})" if step.get("when") else ""
                    shared_str = " (shared)" if step.get("shared") else ""
                    lines.append(f"**Command:** `{step['kind']}`{when_str}{shared_str}")
                    lines.append("```bash")
                    lines.append(shlex.join(step["argv"]))
                    lines.append("```")

                elif stype == "precheck":
                    cache_desc = ""
                    if step.get("cache") == "subscription":
                        if step.get("cache_role") == "owner":
                            shared_list = ", ".join(step.get("shared_with") or [])
                            cache_desc = f" (cache=subscription, shared by {vname}"
                            if shared_list:
                                cache_desc += f", {shared_list})"
                            else:
                                cache_desc += ")"
                        elif step.get("cache_role") == "reuse":
                            cache_desc = f" (cache=subscription, reused from {step.get('cached_from')})"

                    lines.append(f"**Precheck:** `{step['name']}` · host bash{cache_desc}")
                    lines.append("*Script (display only; at runtime values come from env):*")
                    lines.append("```bash")
                    lines.append(step["script"]["text"].rstrip())
                    lines.append("```")

                    lines.append("Branches:")
                    start_steps = []
                    for br in step["branches"]:
                        extra = ""
                        if br.get("outcome"):
                            extra = f" (nothing below runs -> `{br['outcome']}`)"
                        elif br.get("after_job"):
                            extra = f" (after the job: `{br['after_job']}`)"
                        lines.append(f"- `{br['key']}` -> {br['log']}{extra}")
                        if br.get("steps") and not start_steps:
                            start_steps = br["steps"]

                    if step.get("unmatched"):
                        lines.append(f"- unmatched -> `{step['unmatched']}`")

                    if start_steps:
                        lines.append("")
                        lines.append("*When start / start-stop:*")
                        lines.append("```bash")
                        for cmd in start_steps:
                            lines.append(shlex.join(cmd["argv"]))
                        lines.append("```")

                elif stype == "host_script":
                    sec = step["section"]
                    name = step["name"]
                    lines.append(f"**Host script ({sec}):** `{name}` · host bash")
                    lines.append("*Script (display only; at runtime values come from env):*")
                    lines.append("```bash")
                    lines.append(step["script"]["text"].rstrip())
                    lines.append("```")

                elif stype == "restore":
                    lines.append("**Power restore branches per previous state:**")
                    for br in step["branches"]:
                        lines.append(f"- `prev={br['prev_state']}` -> `{br['action']}`")
                        lines.append("```bash")
                        for cmd in br["steps"]:
                            lines.append(shlex.join(cmd["argv"]))
                        lines.append("```")

                elif stype == "note":
                    lines.append(f"> {step['text']}")

            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_json(plan: Dict[str, Any]) -> str:
    """Render structured plan as formatted JSON."""
    return json.dumps(plan, indent=2, ensure_ascii=False) + "\n"


def cmd_explain(args: argparse.Namespace, profile: Profile) -> int:
    """Execute explain subcommand: pure static plan inspection (AC-7, AC-8, AC-9)."""
    yaml_path = Path(args.job_yaml).resolve()

    # Pure validation: no subprocess, no check_host_bash
    job, result = validate_job_file(yaml_path, profile=profile)
    for w in result.warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    for e in result.errors:
        print(f"ERROR: {e}", file=sys.stderr)

    if not result.is_valid or job is None:
        return 1

    # Read and validate input CSV
    rows, _, csv_result = read_input_csv(job.input, job=job, os_family=profile.os_family)
    for w in csv_result.warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    for e in csv_result.errors:
        print(f"ERROR: {e}", file=sys.stderr)

    if not csv_result.is_valid:
        return 1

    # Filter target rows
    target_rows = select_target_rows(rows, vm_names=getattr(args, "vm", None), limit=getattr(args, "limit", None))

    plan = build_plan(job, target_rows, profile)

    fmt = getattr(args, "format", "text") or "text"
    if fmt == "json":
        rendered = render_json(plan)
    elif fmt == "md":
        rendered = render_md(plan)
    else:
        rendered = render_text(plan)

    out_file = getattr(args, "output", None)
    if out_file:
        p_out = Path(out_file)
        p_out.parent.mkdir(parents=True, exist_ok=True)
        p_out.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")

    return 0


render_markdown = render_md
