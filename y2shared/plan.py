"""Structured plan builder for yaml_to_bash (AC-7, AC-8, AC-10, ADR-002)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

from y2shared.azcmd import (
    BUSY_RETRIES,
    BUSY_WAIT_S,
    KIND_DEALLOCATE,
    KIND_INVOKE,
    KIND_POWEROFF,
    KIND_POWER_STATE,
    KIND_READER,
    KIND_SUB_RESOLVE,
    KIND_VM_START,
    THROTTLE_BACKOFFS_S,
    UNKNOWN_RETRY_WAIT_S,
    build_power_post,
    build_power_state,
    build_reader_invoke,
    build_run_command_post,
    build_sub_resolve,
    build_vm_start,
)
from y2shared.aztrace import redact_text
from y2shared.config import Job, Precheck
from y2shared.inputcsv import VMRow
from y2shared.profile import Profile
from y2shared.repo import find_repo_root, rel_posix

GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_VAR_SUBST_RE = re.compile(
    r"(?<!\\)\$(?:\{([A-Za-z0-9_]+)\}|([A-Za-z_][A-Za-z0-9_]*))"
)


def format_script_display(script: str, env: Dict[str, str]) -> str:
    """
    Substitute $NAME / ${NAME} only when NAME is in env.
    Preserves escaped \\$X and non-env variables ($Y2_OUTCOME, etc.).
    Boundary-correct for identifiers. Redacts secret tokens.
    """
    if not script:
        return ""

    def _repl(m: re.Match) -> str:
        var_name = m.group(1) or m.group(2)
        if var_name in env:
            return str(env[var_name])
        return m.group(0)

    substituted = _VAR_SUBST_RE.sub(_repl, script)
    return redact_text(substituted)


@dataclass
class PlanContext:
    job: Job
    rows: Sequence[VMRow]
    profile: Profile
    payload_sha256: str
    job_label: str
    subscriptions: List[Dict[str, Any]]
    sub_owners: Dict[str, str]
    sub_shared: Dict[str, List[str]]

    @classmethod
    def from_rows(
        cls,
        job: Job,
        rows: Sequence[VMRow],
        profile: Profile,
        payload_sha256: Optional[str] = None,
        job_label: Optional[str] = None,
    ) -> PlanContext:
        if job_label is None:
            repo_root = find_repo_root(job.yaml_path)
            if repo_root is not None:
                job_label = rel_posix(job.yaml_path, repo_root)
            else:
                job_label = job.yaml_path.name

        if payload_sha256 is None:
            compiled, _ = profile.compile_remote(job, run_id="preview", check_syntax=False)
            payload_sha256 = hashlib.sha256(compiled.encode("utf-8")).hexdigest()

        # Track subscriptions in first-appearance order
        seen_subs: Dict[str, int] = {}
        sub_owners: Dict[str, str] = {}
        sub_shared: Dict[str, List[str]] = {}

        for row in rows:
            sub = row.subscription
            if sub not in seen_subs:
                seen_subs[sub] = 1
                sub_owners[sub] = row.vm_name
                sub_shared[sub] = []
            else:
                seen_subs[sub] += 1
                sub_shared[sub].append(row.vm_name)

        subscriptions = [
            {
                "value": sub,
                "is_guid": bool(GUID_RE.match(sub)),
                "vm_count": count,
            }
            for sub, count in seen_subs.items()
        ]

        return cls(
            job=job,
            rows=rows,
            profile=profile,
            payload_sha256=payload_sha256,
            job_label=job_label,
            subscriptions=subscriptions,
            sub_owners=sub_owners,
            sub_shared=sub_shared,
        )


def _read_script_content(item: Any) -> tuple[str, str]:
    """Return (source, text) where source is 'file' or 'inline'."""
    script_path = getattr(item, "script_path", None)
    if script_path:
        try:
            with open(script_path, "r", encoding="utf-8") as f:
                return "file", f.read()
        except Exception:
            return "file", ":"
    return "inline", getattr(item, "script", None) or ":"


def build_vm_plan(ctx: PlanContext, row: VMRow) -> Dict[str, Any]:
    """Build the plan dictionary for a single VM row."""
    job = ctx.job
    profile = ctx.profile

    is_guid = bool(GUID_RE.match(row.subscription))
    sub_id_placeholder = row.subscription if is_guid else f"<id-of:{row.subscription}>"
    is_sub_owner = ctx.sub_owners.get(row.subscription) == row.vm_name

    body_name = "remote.sh.body.json" if profile.tool == "y2b" else "remote.ps1.body.json"
    collect_name = profile.collect_script_name

    # 1. Phase: invoke
    invoke_steps: List[Dict[str, Any]] = []

    if not is_guid and is_sub_owner:
        invoke_steps.append({
            "type": "command",
            "kind": KIND_SUB_RESOLVE,
            "argv": build_sub_resolve(row.subscription),
            "when": "Phase 1 (precheck resolution)",
            "optional": False,
            "shared": True,
        })

    for p in job.pre_run:
        if "invoke" not in p.phases:
            continue

        cache_role = None
        shared_with = None
        cached_from = None

        if p.cache == "subscription":
            if is_sub_owner:
                cache_role = "owner"
                shared_with = ctx.sub_shared.get(row.subscription, [])
            else:
                cache_role = "reuse"
                cached_from = ctx.sub_owners.get(row.subscription)

        source, raw_script = _read_script_content(p)
        display_text = format_script_display(raw_script, row.env)

        branches = []
        for key, spec in p.result.items():
            is_default = (key == "default")
            action = spec.get("action", "")
            log = spec.get("log", "")
            after_job = None
            outcome = None
            branch_steps = []

            if action == "continue":
                if not log:
                    log = "continue"
            elif action == "stop":
                after_job = "deallocate"
                if not log:
                    log = "stop; after the job: deallocate"
            elif action == "end_check":
                outcome = f"ENDED:{p.name}"
            elif action == "skip":
                outcome = f"SKIPPED:{p.name}"
            elif action == "start":
                branch_steps.append({
                    "kind": KIND_VM_START,
                    "argv": build_vm_start(row.subscription, row.resource_group, row.vm_name),
                })
                if not log:
                    log = "start: record state, start, recheck"
            elif action == "start-stop":
                branch_steps.append({
                    "kind": KIND_POWER_STATE,
                    "argv": build_power_state(row.subscription, row.resource_group, row.vm_name),
                })
                branch_steps.append({
                    "kind": KIND_VM_START,
                    "argv": build_vm_start(row.subscription, row.resource_group, row.vm_name),
                })
                after_job = "restore"
                if not log:
                    log = "start-stop: record state, start, recheck; after the job: restore"

            branches.append({
                "key": key,
                "is_default": is_default,
                "action": action,
                "log": log,
                "outcome": outcome,
                "after_job": after_job,
                "steps": branch_steps,
            })

        unmatched = f"ERROR:precheck_unmatched:{p.name}" if "default" not in p.result else None

        invoke_steps.append({
            "type": "precheck",
            "name": p.name,
            "phases": p.phases,
            "cache": p.cache,
            "cache_role": cache_role,
            "shared_with": shared_with,
            "cached_from": cached_from,
            "timeout": p.timeout,
            "script": {
                "source": source,
                "text": display_text,
                "display_only": True,
            },
            "branches": branches,
            "unmatched": unmatched,
        })

    # Invoke run command
    invoke_argv = build_run_command_post(
        sub_id_placeholder,
        row.resource_group,
        row.vm_name,
        Path(body_name),
    )
    invoke_steps.append({
        "type": "command",
        "kind": KIND_INVOKE,
        "argv": invoke_argv,
        "when": "Phase 1: invoke (fire-and-forget)",
        "optional": False,
        "shared": False,
    })
    invoke_steps.append({
        "type": "note",
        "text": f"payload sha256: {ctx.payload_sha256}",
    })

    # 2. Phase: wait
    wait_steps = [
        {
            "type": "note",
            "text": f"wait sleep_before_collect={job.sleep_before_collect}s",
        }
    ]

    # 3. Phase: collect
    collect_steps: List[Dict[str, Any]] = []
    for p in job.pre_run:
        if "collect" not in p.phases:
            continue

        cache_role = None
        shared_with = None
        cached_from = None

        if p.cache == "subscription":
            if is_sub_owner:
                cache_role = "owner"
                shared_with = ctx.sub_shared.get(row.subscription, [])
            else:
                cache_role = "reuse"
                cached_from = ctx.sub_owners.get(row.subscription)

        source, raw_script = _read_script_content(p)
        display_text = format_script_display(raw_script, row.env)

        branches = []
        for key, spec in p.result.items():
            is_default = (key == "default")
            action = spec.get("action", "")
            log = spec.get("log", "")
            after_job = None
            outcome = None
            branch_steps = []

            if action == "continue":
                if not log:
                    log = "continue"
            elif action == "stop":
                after_job = "deallocate"
                if not log:
                    log = "stop; after the job: deallocate"
            elif action == "end_check":
                outcome = f"ENDED:{p.name}"
            elif action == "skip":
                outcome = f"ERROR:not_running_at_collect:{p.name}"
            elif action == "start":
                branch_steps.append({
                    "kind": KIND_VM_START,
                    "argv": build_vm_start(row.subscription, row.resource_group, row.vm_name),
                })
                if not log:
                    log = "start: record state, start, recheck"
            elif action == "start-stop":
                branch_steps.append({
                    "kind": KIND_POWER_STATE,
                    "argv": build_power_state(row.subscription, row.resource_group, row.vm_name),
                })
                branch_steps.append({
                    "kind": KIND_VM_START,
                    "argv": build_vm_start(row.subscription, row.resource_group, row.vm_name),
                })
                after_job = "restore"
                if not log:
                    log = "start-stop: record state, start, recheck; after the job: restore"

            branches.append({
                "key": key,
                "is_default": is_default,
                "action": action,
                "log": log,
                "outcome": outcome,
                "after_job": after_job,
                "steps": branch_steps,
            })

        unmatched = f"ERROR:precheck_unmatched:{p.name}" if "default" not in p.result else None

        collect_steps.append({
            "type": "precheck",
            "name": p.name,
            "phases": p.phases,
            "cache": p.cache,
            "cache_role": cache_role,
            "shared_with": shared_with,
            "cached_from": cached_from,
            "timeout": p.timeout,
            "script": {
                "source": source,
                "text": display_text,
                "display_only": True,
            },
            "branches": branches,
            "unmatched": unmatched,
        })

    # Reader step
    reader_argv = build_reader_invoke(
        row.subscription,
        row.resource_group,
        row.vm_name,
        profile.command_id,
        Path(collect_name),
    )
    collect_steps.append({
        "type": "command",
        "kind": KIND_READER,
        "argv": reader_argv,
        "when": "Phase 2: collect (reader)",
        "optional": False,
        "shared": False,
    })

    # Computed columns
    for col in job.collect:
        if col.script or col.script_path:
            source, raw_script = _read_script_content(col)
            display_text = format_script_display(raw_script, row.env)
            collect_steps.append({
                "type": "host_script",
                "section": "column",
                "name": col.column,
                "script": {
                    "source": source,
                    "text": display_text,
                    "display_only": True,
                },
            })

    # 4. Phase: post
    post_steps: List[Dict[str, Any]] = []
    for hook in job.post_run:
        source, raw_script = _read_script_content(hook)
        display_text = format_script_display(raw_script, row.env)
        post_steps.append({
            "type": "host_script",
            "section": "post_run",
            "name": hook.name,
            "script": {
                "source": source,
                "text": display_text,
                "display_only": True,
            },
        })

    # Restore node
    dealloc_step = {
        "kind": KIND_DEALLOCATE,
        "argv": build_power_post(sub_id_placeholder, row.resource_group, row.vm_name, "deallocate"),
    }
    pstate_step = {
        "kind": KIND_POWER_STATE,
        "argv": build_power_state(row.subscription, row.resource_group, row.vm_name),
    }
    poweroff_step = {
        "kind": KIND_POWEROFF,
        "argv": build_power_post(sub_id_placeholder, row.resource_group, row.vm_name, "powerOff"),
    }

    restore_branches = [
        {
            "prev_state": "PowerState/running",
            "action": "deallocate",
            "steps": [dealloc_step],
        },
        {
            "prev_state": "PowerState/deallocated",
            "action": "deallocate",
            "steps": [dealloc_step],
        },
        {
            "prev_state": "PowerState/stopped",
            "action": "powerOff",
            "steps": [pstate_step, poweroff_step],
        },
        {
            "prev_state": "PowerState/stopping",
            "action": "powerOff",
            "steps": [pstate_step, poweroff_step],
        },
    ]
    post_steps.append({
        "type": "restore",
        "branches": restore_branches,
    })

    # Flat commands index
    commands: Dict[str, List[str]] = {}
    if not is_guid:
        commands[KIND_SUB_RESOLVE] = build_sub_resolve(row.subscription)

    actions = job.result_actions
    if actions & {"start", "start-stop"}:
        commands[KIND_VM_START] = build_vm_start(row.subscription, row.resource_group, row.vm_name)
    if actions & {"start-stop"}:
        commands[KIND_POWER_STATE] = build_power_state(row.subscription, row.resource_group, row.vm_name)

    commands[KIND_INVOKE] = invoke_argv
    commands[KIND_READER] = reader_argv

    if actions & {"stop", "start-stop"}:
        commands[KIND_DEALLOCATE] = dealloc_step["argv"]
    if actions & {"start-stop"}:
        commands[KIND_POWEROFF] = poweroff_step["argv"]

    # Budget calculation
    R = int(job.collect_retries)
    n_start_inv = sum(
        1 for p in job.pre_run
        if "invoke" in p.phases and any(s.get("action") in {"start", "start-stop"} for s in p.result.values())
    )
    n_ss_inv = sum(
        1 for p in job.pre_run
        if "invoke" in p.phases and any(s.get("action") == "start-stop" for s in p.result.values())
    )
    n_start_col = sum(
        1 for p in job.pre_run
        if "collect" in p.phases and any(s.get("action") in {"start", "start-stop"} for s in p.result.values())
    )
    n_ss_col = sum(
        1 for p in job.pre_run
        if "collect" in p.phases and any(s.get("action") == "start-stop" for s in p.result.values())
    )

    has_start_stop = "start-stop" in actions
    has_stop_or_ss = bool(actions & {"stop", "start-stop"})

    invoke_allow = {
        "sub-resolve": 1 if not is_guid else 0,
        "power-state": n_ss_inv,
        "vm-start": n_start_inv,
        "invoke": 1,
    }
    collect_allow = {
        "power-state": n_ss_col * (R + 2),
        "vm-start": n_start_col * (R + 2),
        "reader": R + 1,
    }
    post_allow = {
        "sub-resolve": 1 if not is_guid else 0,
        "power-state": 1 if has_start_stop else 0,
        "deallocate": 1 if has_stop_or_ss else 0,
        "powerOff": 1 if has_start_stop else 0,
    }

    budget = [
        {"stage": "invoke", "allow": invoke_allow},
        {"stage": "collect", "allow": collect_allow},
        {"stage": "post", "allow": post_allow, "at_most_one_of": ["deallocate", "powerOff"]},
    ]

    return {
        "index": row.index,
        "vm_name": row.vm_name,
        "resource_group": row.resource_group,
        "subscription": row.subscription,
        "subscription_id": sub_id_placeholder,
        "phases": [
            {"phase": "invoke", "title": "Phase 1: Invoke", "steps": invoke_steps},
            {"phase": "wait", "title": "Wait", "steps": wait_steps},
            {"phase": "collect", "title": "Phase 2: Collect", "steps": collect_steps},
            {"phase": "post", "title": "Phase 3: Post", "steps": post_steps},
        ],
        "commands": commands,
        "budget": budget,
    }


def vm_commands(vm_plan: Dict[str, Any]) -> Dict[str, List[str]]:
    """Return flat kind -> argv index for the VM plan."""
    return vm_plan.get("commands", {})


def vm_budget(
    vm_plan: Dict[str, Any], phases: Optional[Sequence[str]] = None
) -> List[Dict[str, Any]]:
    """Return budget stages for the VM plan, optionally filtered by phase/stage."""
    budgets = vm_plan.get("budget", [])
    if phases is None:
        return list(budgets)
    allowed_phases = set(phases)
    return [b for b in budgets if b.get("stage") in allowed_phases]


def build_plan(
    job: Job,
    rows: Sequence[VMRow],
    profile: Profile,
    payload_sha256: Optional[str] = None,
    job_label: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Construct the full structured plan object (schema y2-plan/1).
    Pure calculation: no subprocess, no file writes, no env reads (INV-6).
    """
    ctx = PlanContext.from_rows(
        job=job,
        rows=rows,
        profile=profile,
        payload_sha256=payload_sha256,
        job_label=job_label,
    )

    actions = job.result_actions
    mutations = ["runCommand POST"]
    if actions & {"start", "start-stop"}:
        mutations.append("vm start")
    if actions & {"stop", "start-stop"}:
        mutations.append("deallocate")
    if actions & {"start-stop"}:
        mutations.append("powerOff")

    backoffs_str = ", ".join(str(s) for s in THROTTLE_BACKOFFS_S)
    interval_val = (
        int(job.collect_retry_interval)
        if float(job.collect_retry_interval).is_integer()
        else job.collect_retry_interval
    )
    retry_rules = [
        f"429/throttled: backoff {backoffs_str} s (x0.8..1.2 jitter), then ERROR:throttled",
        f"Conflict/busy on invoke: retry up to {BUSY_RETRIES} times every {BUSY_WAIT_S} s, then ERROR:run_command_busy",
        f"Unknown error: retry once after {UNKNOWN_RETRY_WAIT_S} s, then ERROR:az:<message>",
        f"Conflict on reader: STILL_RUNNING retried every {interval_val}s up to {job.collect_retries} times",
    ]

    resolutions = [
        {
            "subscription": item["value"],
            "placeholder": f"<id-of:{item['value']}>",
            "argv": build_sub_resolve(item["value"]),
        }
        for item in ctx.subscriptions
        if not item["is_guid"]
    ]

    notes = [
        "host-script az calls made through an absolute path or `command -p az` are not traced"
    ]

    fleet = {
        "vm_count": len(rows),
        "max_run_commands": 2 * len(rows),
        "subscriptions": ctx.subscriptions,
        "payload": {
            "file": profile.remote_script_name,
            "run_id": "preview",
            "sha256": ctx.payload_sha256,
        },
        "mutations": mutations,
        "timing": {
            "sleep_before_collect_s": float(job.sleep_before_collect),
            "collect_retry_interval_s": float(job.collect_retry_interval),
            "collect_retries": int(job.collect_retries),
        },
        "retry_rules": retry_rules,
        "resolutions": resolutions,
        "notes": notes,
    }

    vms = [build_vm_plan(ctx, row) for row in rows]

    return {
        "schema": "y2-plan/1",
        "tool": profile.tool,
        "command_id": profile.command_id,
        "job": ctx.job_label,
        "api_version": "2024-07-01",
        "fleet": fleet,
        "vms": vms,
    }

