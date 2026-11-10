"""Phase 3: post-run hooks and power action evaluation."""

from __future__ import annotations

import concurrent.futures
import datetime
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Set

from y2shared.config import Job
from y2shared.dispatcher import AuthCircuitBreaker, power_action_nowait
from y2shared.inputcsv import VMRow
from y2shared.parts import (
    append_part_log,
    get_part_paths,
    is_part_finished,
    is_part_still_running,
    power_path,
    read_part_row,
    read_power,
    update_power,
    write_power_once,
)
from y2shared.aztrace import RECORDER, format_script_line
from y2shared.shell import run_bash
from y2shared.vmlog import VMLogger, tee_lines


# pre_run result actions that leave a power action for Phase 3
POWER_AFTER_ACTIONS = {"stop", "start-stop"}


def restore_action(prev_state: str) -> str:
    """Power action that returns a VM to prev_state: stopped VMs are powered off, everything else deallocated."""
    if any(s in prev_state.lower() for s in ("stopped", "stopping")):
        return "powerOff"
    return "deallocate"


def get_vm_outcome(row_values: list[str]) -> str:
    """Determine outcome marker from a part CSV row values."""
    for val in row_values:
        val_str = str(val)
        if any(val_str.startswith(p) for p in ("ERROR:", "SKIPPED:", "ENDED:", "DRY_RUN:")):
            return val_str
    return "OK"


@dataclass
class PostPhaseResult:
    hooks_ok: int = 0
    hooks_failed: int = 0
    deallocated: int = 0
    powered_off: int = 0
    power_failed: int = 0
    orphan_indices: Set[int] = field(default_factory=set)


def run_post_phase(
    job: Job,
    target_rows: list[VMRow],
    collect_records: dict[int, dict[str, Any]],
    parts_dir: Path,
    run_id: str,
    parallel: int,
    circuit_breaker: AuthCircuitBreaker,
    dry_run: bool = False,
    restore_orphans: bool = False,
) -> PostPhaseResult:
    """
    Execute Phase 3: post_run hooks followed by power action for each finished VM.
    """
    total_rows = len(target_rows)
    work_set: list[VMRow] = []
    orphan_indices: set[int] = set()

    for r in target_rows:
        p_path = power_path(parts_dir, r, total_rows)
        p_data = read_power(p_path)

        finished = is_part_finished(parts_dir, r, total_rows)
        if not finished:
            # Check for orphan
            if (
                restore_orphans
                and p_data
                and p_data.get("started_by_run") is True
                and p_data.get("post") != "done"
                and r.index not in collect_records
                and not is_part_still_running(parts_dir, r, total_rows)
            ):
                work_set.append(r)
                orphan_indices.add(r.index)
            continue

        has_record = (r.index in collect_records) or p_path.exists()
        if not has_record:
            # VMs skipped or ended in Phase 1 never ran anything
            continue

        if p_data and p_data.get("post") == "done":
            continue

        work_set.append(r)

    result = PostPhaseResult(orphan_indices=set(orphan_indices))
    if not work_set:
        return result

    # Check if there's any work to do
    has_hooks = bool(job.post_run) and not dry_run
    has_power = bool(job.result_actions & POWER_AFTER_ACTIONS) or dry_run
    if not has_hooks and not has_power and not orphan_indices:
        return result

    lock = threading.Lock()
    completed = 0
    total_work = len(work_set)

    def post_worker(row: VMRow) -> None:
        nonlocal completed
        is_orphan = row.index in orphan_indices
        csv_path, log_path = get_part_paths(parts_dir, row, total_rows)
        if is_orphan:
            row_vals = []
            outcome = "no_result"
        else:
            row_vals = read_part_row(csv_path) if csv_path.exists() else []
            outcome = get_vm_outcome(row_vals)

        p_path = power_path(parts_dir, row, total_rows)
        p_data = read_power(p_path) or {}
        prev_state = p_data.get("prev_state", "")
        started_by_run = p_data.get("started_by_run", False)

        vm_lines: list[str] = [f"===== Phase 3: Post (Run ID: {run_id}) ====="]

        # Power action: a .power file exists only for VMs a 'stop' or 'start-stop' result marked.
        # Dry-run only undoes the starts this run made; it never deallocates a VM that was running.
        power_act = None
        if p_data and (started_by_run or not dry_run):
            power_act = restore_action(prev_state)

        # 1. Post-run hooks (skipped in dry-run and for orphans)
        v_hooks_ok = 0
        v_hooks_failed = 0

        if not is_orphan and not dry_run and job.post_run:
            hook_env = dict(row.env)
            hook_env["Y2_OUTCOME"] = outcome
            hook_env["Y2_RUN_ID"] = run_id
            hook_env["Y2_POWER_AFTER"] = power_act or ""
            hook_env["Y2_PREV_POWER_STATE"] = prev_state

            for hook in job.post_run:
                script_code = hook.script or ""
                if hook.script_path:
                    try:
                        with open(hook.script_path, "r", encoding="utf-8") as sf:
                            script_code = sf.read()
                    except Exception as e:
                        vm_lines.append(f"[post_run:{hook.name}] ERROR: failed to read script_path: {e}")
                        v_hooks_failed += 1
                        continue

                res = run_bash(
                    script_code,
                    env=hook_env,
                    timeout=hook.timeout,
                    cwd=job.yaml_dir,
                    trace_section="post_run",
                    trace_name=hook.name,
                )
                if res.az_calls:
                    for call in res.az_calls:
                        vm_lines.append(f"[az] {format_script_line(call.section, call.name, call.rc, call.argv)}")
                        RECORDER.record_script(row.index, call.section, call.name, call.rc, call.argv)
                stdout = res.stdout_stripped
                hook_ok = True

                if hook.result:
                    last_line = stdout.splitlines()[-1].strip() if stdout.splitlines() else ""
                    spec = hook.result.get(stdout) or hook.result.get(last_line) or hook.result.get("default")
                    if spec:
                        status_val = spec.get("status", "ok")
                        log_msg = spec.get("log", "")
                        vm_lines.append(f"[post_run:{hook.name}] stdout='{stdout}' -> {log_msg}")
                        if status_val == "error":
                            hook_ok = False
                    else:
                        vm_lines.append(f"[post_run:{hook.name}] ERROR: output '{stdout}' matched no result key and no default defined")
                        hook_ok = False
                else:
                    if res.rc != 0:
                        vm_lines.append(f"[post_run:{hook.name}] ERROR: rc={res.rc}, stderr={res.stderr}")
                        hook_ok = False
                    else:
                        vm_lines.append(f"[post_run:{hook.name}] ok (rc=0)")

                if hook_ok:
                    v_hooks_ok += 1
                else:
                    v_hooks_failed += 1

        # 2. Power action
        v_deallocated = 0
        v_powered_off = 0
        v_power_failed = 0
        p_marker = ""

        if power_act:
            p_logger = VMLogger.from_lines(row.vm_name, row.index, [], sink=False)
            p_logger.log(f"Applying power action '{power_act}' (prev_state='{prev_state}')...")
            success, p_marker = power_action_nowait(
                row=row,
                action=power_act,
                logger=p_logger,
                circuit_breaker=circuit_breaker,
            )
            vm_lines.extend(p_logger.lines)
            if success:
                RECORDER.expect(row.index, power_act)
                if power_act == "deallocate":
                    v_deallocated = 1
                elif power_act == "powerOff":
                    v_powered_off = 1
            else:
                v_power_failed = 1

        # 3. Update .power file
        if p_path.exists() or power_act is not None:
            if not p_path.exists():
                write_power_once(
                    p_path,
                    {
                        "prev_state": prev_state or "PowerState/running",
                        "started_by_run": started_by_run,
                        "post": "pending",
                        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    },
                )

            if v_power_failed > 0:
                update_power(p_path, post=f"failed:{p_marker}")
            else:
                update_power(p_path, post="done")

        append_part_log(log_path, vm_lines)
        tee_lines(row.index, row.vm_name, vm_lines)

        with lock:
            result.hooks_ok += v_hooks_ok
            result.hooks_failed += v_hooks_failed
            result.deallocated += v_deallocated
            result.powered_off += v_powered_off
            result.power_failed += v_power_failed
            completed += 1
            sys.stderr.write(f"\r[Phase 3: Post] [ {completed}/{total_work} ] deallocated {result.deallocated} | powered_off {result.powered_off}   ")
            sys.stderr.flush()

    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as executor:
        futures = [executor.submit(post_worker, r) for r in work_set]
        for f in concurrent.futures.as_completed(futures):
            pass

    sys.stderr.write("\n")
    return result

