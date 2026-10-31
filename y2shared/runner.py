"""Execution runner: worker thread pool, signal handling, progress reporting, exit codes."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import functools
import heapq
import itertools
import os
import signal
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from y2shared.collectlist import CollectList
from y2shared.collector import collect_for_vm
from y2shared.config import Job, load_job
from y2shared.dispatcher import (
    AuthCircuitBreaker,
    AuthAbortedError,
    invoke_nowait,
    read_results,
    ReadResult,
)
from y2shared.hostenv import (
    check_host_bash,
    raise_nofile,
    start_caffeinate,
    stop_caffeinate,
)
from y2shared.inputcsv import VMRow, read_input_csv
from y2shared.merger import merge_parts, MergeError
from y2shared.parts import (
    get_parts_dir,
    init_parts_dir,
    get_run_info,
    update_run_phase,
    is_part_finished,
    is_part_still_running,
    write_part_files,
    power_path,
    write_power_once,
    read_power,
)
from y2shared.post import POWER_AFTER_ACTIONS, run_post_phase, PostPhaseResult
from y2shared.precheck import PrecheckEngine
from y2shared.profile import Profile
from y2shared.scan import build_var_to_step_map
from y2shared.validate import validate_job_file
from y2shared.vmlog import VMLogger


@dataclass
class RunState:
    total_vms: int
    completed: int = 0
    ok: int = 0
    skipped: int = 0
    ended: int = 0
    error: int = 0
    start_time: float = 0.0
    stop_requested: bool = False
    lock: threading.Lock = threading.Lock()

    def update(self, outcome_type: str) -> None:
        with self.lock:
            self.completed += 1
            if outcome_type == "OK":
                self.ok += 1
            elif outcome_type == "SKIPPED":
                self.skipped += 1
            elif outcome_type == "ENDED":
                self.ended += 1
            else:
                self.error += 1

    def format_progress(self, phase_name: str, active_workers: int) -> str:
        with self.lock:
            elapsed = time.time() - self.start_time
            if self.completed > 0:
                rate = self.completed / max(elapsed, 0.001)
                remaining = self.total_vms - self.completed
                eta_s = int(remaining / rate)
                if eta_s >= 60:
                    eta_str = f"{eta_s // 60}m"
                else:
                    eta_str = f"{eta_s}s"
            else:
                eta_str = "--"

            return (
                f"[{phase_name}] [ {self.completed}/{self.total_vms} ] "
                f"ok {self.ok} · skipped {self.skipped} · ended {self.ended} · error {self.error} · "
                f"running {active_workers} · eta {eta_str}"
            )


def run_collect_phase(
    job: Job,
    work_set: list[tuple[VMRow, dict[str, Any]]],
    parts_dir: Path,
    collect_script_path: Path,
    run_id: str,
    total_rows: int,
    parallel: int,
    circuit_breaker: AuthCircuitBreaker,
    run_state: RunState,
    interrupted_event: threading.Event,
    time_fn: Callable[[], float] = time.time,
    sleep_fn: Callable[[float], None] = time.sleep,
    read_fn: Callable[..., ReadResult] | None = None,
    var_to_step: dict[str, int] | None = None,
    precheck_engine: PrecheckEngine | None = None,
) -> None:
    """
    Phase 2 Collect scheduler running on min-heap.
    Waiting occurs in the heap, never holding a worker thread with time.sleep.
    """
    if not work_set:
        return

    actual_read_fn = read_fn or read_results
    seq_gen = itertools.count()
    heap: list[tuple[float, int, VMRow, dict[str, Any], int]] = []
    now = time_fn()

    for r, rec in work_set:
        if is_part_still_running(parts_dir, r, total_rows):
            first_ready_at = now
        else:
            try:
                invoked_dt = datetime.datetime.fromisoformat(rec["invoked_at"])
                invoked_ts = invoked_dt.timestamp()
            except Exception:
                invoked_ts = now
            first_ready_at = invoked_ts + job.sleep_before_collect

        heapq.heappush(heap, (first_ready_at, next(seq_gen), r, rec, 0))

    def collect_worker_task(
        r: VMRow,
        rec: dict[str, Any],
    ) -> tuple[VMRow, ReadResult, VMLogger]:
        logger = VMLogger.from_lines(r.vm_name, r.index, rec.get("log", []))
        if precheck_engine:
            def on_power(prev_state: str, started: bool) -> None:
                p_path = power_path(parts_dir, r, total_rows)
                now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                data = {
                    "prev_state": prev_state,
                    "started_by_run": started,
                    "post": "pending",
                    "at": now_iso,
                }
                write_power_once(p_path, data)

            proceed, marker = precheck_engine.run_prechecks_for_vm(
                job, r, logger, phase="collect", on_power=on_power
            )
            if not proceed:
                return r, ReadResult(status="ERROR", error=marker), logger

        res = actual_read_fn(
            row=r,
            collect_script_path=collect_script_path,
            run_id=run_id,
            logger=logger,
            circuit_breaker=circuit_breaker,
        )
        return r, res, logger

    in_flight: dict[concurrent.futures.Future, tuple[VMRow, dict[str, Any], int]] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as executor:
        while (heap or in_flight) and not run_state.stop_requested:
            now = time_fn()
            # Submit ready tasks up to concurrency limit
            while len(in_flight) < parallel and heap and heap[0][0] <= now:
                ready_at, _, r, rec, retries = heapq.heappop(heap)
                fut = executor.submit(collect_worker_task, r, rec)
                in_flight[fut] = (r, rec, retries)

            # Wait for at least one in-flight task to finish
            if in_flight:
                if len(in_flight) < parallel and heap:
                    wait_timeout = max(0.0, heap[0][0] - time_fn())
                else:
                    wait_timeout = None

                done, _ = concurrent.futures.wait(
                    in_flight.keys(),
                    timeout=wait_timeout,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )

                for fut in done:
                    r, rec, retries = in_flight.pop(fut)
                    try:
                        r, res, logger = fut.result()
                    except AuthAbortedError:
                        run_state.stop_requested = True
                        interrupted_event.set()
                        break
                    except Exception as e:
                        err_type = type(e).__name__
                        tb = traceback.format_exc()
                        logger = VMLogger.from_lines(r.vm_name, r.index, rec.get("log", []))
                        logger.log_error(f"Uncaught exception in collect: {e}\n{tb}")
                        marker = f"ERROR:internal:{err_type}"
                        logger.finalize(marker)
                        row_vals = collect_for_vm(job, r, outcome_marker=marker, logger=logger, var_to_step=var_to_step)
                        write_part_files(parts_dir, r, total_rows, row_vals, logger)
                        run_state.update("ERROR")
                        continue

                    if res.status == "STILL_RUNNING":
                        if retries < job.collect_retries:
                            next_ready = time_fn() + job.collect_retry_interval
                            rec["log"] = list(logger.lines)
                            heapq.heappush(heap, (next_ready, next(seq_gen), r, rec, retries + 1))
                        else:
                            # Retries exhausted -> run collect-phase checks once more before ERROR:still_running (§4.5)
                            marker = "ERROR:still_running"
                            if precheck_engine:
                                def on_power(prev_state: str, started: bool) -> None:
                                    p_path = power_path(parts_dir, r, total_rows)
                                    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                                    data = {
                                        "prev_state": prev_state,
                                        "started_by_run": started,
                                        "post": "pending",
                                        "at": now_iso,
                                    }
                                    write_power_once(p_path, data)

                                proceed, c_marker = precheck_engine.run_prechecks_for_vm(
                                    job, r, logger, phase="collect", on_power=on_power
                                )
                                if not proceed and c_marker:
                                    marker = c_marker

                            logger.finalize(marker)
                            row_vals = collect_for_vm(job, r, outcome_marker=marker, logger=logger, var_to_step=var_to_step)
                            write_part_files(parts_dir, r, total_rows, row_vals, logger)
                            run_state.update("ERROR")
                    else:
                        # Completed OK or with error marker
                        if res.status == "OK":
                            row_vals = collect_for_vm(job, r, remote_result=res, logger=logger, var_to_step=var_to_step)
                            logger.finalize("OK")
                            run_state.update("OK")
                        else:
                            marker = res.error or "ERROR:unknown"
                            row_vals = collect_for_vm(job, r, outcome_marker=marker, logger=logger, var_to_step=var_to_step)
                            logger.finalize(marker)
                            run_state.update("ERROR")

                        write_part_files(parts_dir, r, total_rows, row_vals, logger)

                    sys.stderr.write("\r" + run_state.format_progress("Phase 2: Collect", len(in_flight)) + "   ")
                    sys.stderr.flush()

            elif heap:
                wait_time = max(0.0, heap[0][0] - time_fn())
                if wait_time > 0:
                    sleep_fn(min(wait_time, 0.5))

        if run_state.stop_requested and in_flight:
            concurrent.futures.wait(in_flight.keys())

    sys.stderr.write("\n")


def power_policy_banner(job: Job, total_vms: int) -> str | None:
    """Describe what the pre_run power actions will do after the job, or None if they do nothing."""
    actions = job.result_actions
    if "stop" in actions:
        return (
            f"pre_run uses action 'stop' — after the job, deallocates up to {total_vms} VMs, "
            f"INCLUDING VMs that are running now"
        )
    if "start-stop" in actions:
        return "pre_run uses action 'start-stop' — VMs this run starts are returned to their previous power state"
    return None


def run_job(args: argparse.Namespace, profile: Profile | None = None) -> int:
    """Main execution function invoked by `<tool> run`."""
    check_host_bash()
    if profile is None:
        from y2b.profile import PROFILE
        profile = PROFILE

    yaml_path = Path(args.job_yaml).resolve()

    # 1. Validation
    job, val_res = validate_job_file(yaml_path, profile=profile)
    if not val_res.is_valid or job is None:
        for e in val_res.errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1

    for w in val_res.warnings:
        print(f"WARNING: {w}", file=sys.stderr)

    # 2. Read Input CSV
    rows, headers, csv_res = read_input_csv(job.input, job=job, os_family=profile.os_family)
    if not csv_res.is_valid:
        for e in csv_res.errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1

    for w in csv_res.warnings:
        print(f"WARNING: {w}", file=sys.stderr)

    # Precompute var-to-step map once per run
    var_to_step = build_var_to_step_map(job, profile.write_result_re)

    # 3. Filter VMs (--vm and --limit)
    target_rows = rows
    if args.vm:
        vm_set = set(args.vm)
        target_rows = [r for r in target_rows if r.vm_name in vm_set]

    if args.limit is not None and args.limit > 0:
        target_rows = target_rows[: args.limit]

    if not target_rows:
        print("No target VMs selected to run.", file=sys.stderr)
        return 0

    parallel = args.parallel if args.parallel is not None else job.parallel
    raise_nofile(required_fds=parallel * 4 + 32)
    caffeinate_proc = start_caffeinate()

    # 4. Initialize Parts Directory & Get run_id
    parts_dir = get_parts_dir(job.output_csv)
    try:
        run_id = init_parts_dir(
            parts_dir=parts_dir,
            job=job,
            total_rows=len(target_rows),
            parallel=parallel,
            resume=args.resume,
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # 5. Compile Remote & Collect Scripts
    compiled_remote, compile_errors = profile.compile_remote(job, run_id=run_id)
    if compile_errors:
        for e in compile_errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1

    compiled_collect, collect_errors = profile.compile_collect(job)
    if collect_errors:
        for e in collect_errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1

    remote_script_path = parts_dir / profile.remote_script_name
    collect_script_path = parts_dir / profile.collect_script_name
    try:
        with open(remote_script_path, "w", encoding="utf-8") as f:
            f.write(compiled_remote)
        remote_script_path.chmod(0o755)

        with open(collect_script_path, "w", encoding="utf-8") as f:
            f.write(compiled_collect)
        collect_script_path.chmod(0o755)
    except Exception as e:
        print(f"ERROR: Failed to write scripts to {parts_dir}: {e}", file=sys.stderr)
        return 1

    # 6. Check Collect List for Resume
    collect_list = CollectList(parts_dir)
    stored_phase = "invoke"
    existing_records: dict[int, dict[str, Any]] = {}

    if args.resume:
        run_info = get_run_info(parts_dir)
        stored_phase = run_info.get("phase", "invoke")
        if stored_phase in ("collect", "merged") and not collect_list.path.exists():
            print(f"ERROR: Cannot resume: {collect_list.path} is missing while run was in phase '{stored_phase}'.", file=sys.stderr)
            return 1
        try:
            existing_records = collect_list.load(target_rows)
        except Exception as e:
            print(f"ERROR: Failed to load collect list during resume: {e}", file=sys.stderr)
            return 1

    # 7. Confirmation Prompt
    print(f"\n==================================================")
    print(f"{profile.title} Run Plan (v2 Fire-and-Forget)")
    print(f"  Job file:               {job.yaml_path.name}")
    print(f"  Target VMs:             {len(target_rows)} (from {job.input.name})")
    print(f"  Parallel:               {parallel}")
    print(f"  Dry run:                {args.dry_run}")
    print(f"  Resume:                 {args.resume} (Phase: {stored_phase})")
    print(f"  Sleep before collect:   {job.sleep_before_collect}s")
    print(f"  Collect retry interval: {job.collect_retry_interval}s")
    print(f"  Collect retries:        {job.collect_retries}")
    print(f"  Output CSV:             {job.output_csv}")
    print(f"  Output Log:             {job.output_log}")
    power_banner = power_policy_banner(job, len(target_rows))
    if power_banner:
        print(f"  Power policy:           {power_banner}")
    print(f"==================================================\n")

    if power_banner:
        print(f"Power policy: {power_banner}\n")

    if not args.yes and sys.stdin.isatty():
        try:
            resp = input("Proceed with execution? [y/N]: ").strip().lower()
            if resp not in ("y", "yes"):
                print("Run cancelled by user.")
                return 0
        except (KeyboardInterrupt, EOFError):
            print("\nRun cancelled.")
            return 0

    # 8. Setup Signal Handlers & Shared State
    run_state = RunState(total_vms=len(target_rows), start_time=time.time())
    circuit_breaker = AuthCircuitBreaker(threshold=5)
    precheck_engine = PrecheckEngine()
    post_result: PostPhaseResult | None = None

    ctrl_c_count = 0
    interrupted_event = threading.Event()

    def sigint_handler(signum, frame):
        nonlocal ctrl_c_count
        ctrl_c_count += 1
        if ctrl_c_count == 1:
            print("\n[!] Ctrl-C detected. Stopping submission of new VMs and waiting for active tasks to finish...", file=sys.stderr)
            print("[!] Note: Already-fired scripts keep running on the VMs.", file=sys.stderr)
            print("[!] Press Ctrl-C again to abort immediately.", file=sys.stderr)
            run_state.stop_requested = True
            interrupted_event.set()
        else:
            print("\n[!] Second Ctrl-C received. Aborting immediately.", file=sys.stderr)
            os._exit(3)

    original_sigint = signal.signal(signal.SIGINT, sigint_handler)

    active_workers_count = 0
    workers_lock = threading.Lock()

    # ==============================================================================
    # 9. Phase 1 — Invoke
    # ==============================================================================
    try:
        # Determine VMs for phase 1: VMs with no finished part file AND not in collect list (§3.7)
        phase1_rows: list[VMRow] = []
        for r in target_rows:
            if args.resume and is_part_finished(parts_dir, r, len(target_rows)):
                run_state.update("OK")
                continue
            if r.index in existing_records:
                # Already fired or attempted in previous run
                continue
            phase1_rows.append(r)

        if phase1_rows:
            update_run_phase(parts_dir, "invoke")

            def invoke_worker(row: VMRow) -> None:
                nonlocal active_workers_count
                if run_state.stop_requested:
                    return

                with workers_lock:
                    active_workers_count += 1

                logger = VMLogger(row.vm_name, row.index)

                try:
                    # 1. Precheck
                    def on_power(prev_state: str, started: bool) -> None:
                        p_path = power_path(parts_dir, row, len(target_rows))
                        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                        data = {
                            "prev_state": prev_state,
                            "started_by_run": started,
                            "post": "pending",
                            "at": now_iso,
                        }
                        write_power_once(p_path, data)

                    proceed, precheck_marker = precheck_engine.run_prechecks_for_vm(
                        job, row, logger, phase="invoke", on_power=on_power
                    )
                    if not proceed:
                        row_vals = collect_for_vm(job, row, outcome_marker=precheck_marker, logger=logger, var_to_step=var_to_step)
                        logger.finalize(precheck_marker or "SKIPPED")
                        write_part_files(parts_dir, row, len(target_rows), row_vals, logger)
                        outcome_type = "SKIPPED" if "SKIPPED:" in (precheck_marker or "") else "ENDED"
                        run_state.update(outcome_type)
                        return

                    # 2. Dry run
                    if args.dry_run:
                        logger.log("[dry-run] Prechecks passed; skipping run-command execution.")
                        row_vals = collect_for_vm(job, row, outcome_marker="DRY_RUN:ok", logger=logger, var_to_step=var_to_step)
                        logger.finalize("DRY_RUN:ok")
                        write_part_files(parts_dir, row, len(target_rows), row_vals, logger)
                        run_state.update("OK")
                        return

                    # 3. Fire script (--no-wait)
                    # Append record BEFORE az call (R3 / §3.2)
                    invoked_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    rec = {
                        "index": row.index,
                        "vm_name": row.vm_name,
                        "subscription": row.subscription,
                        "resource_group": row.resource_group,
                        "invoked_at": invoked_at,
                        "confirmed": False,
                        "attempts": 1,
                        "log": list(logger.lines),
                    }
                    collect_list.append(rec)

                    invoke_res = invoke_nowait(
                        row=row,
                        remote_script_path=remote_script_path,
                        logger=logger,
                        circuit_breaker=circuit_breaker,
                        command_id=profile.command_id,
                    )

                    if invoke_res.success:
                        rec["confirmed"] = True
                        rec["attempts"] = invoke_res.attempts
                        rec["log"] = list(logger.lines)
                        collect_list.append(rec)
                    else:
                        rec["confirmed"] = False
                        rec["failed"] = True
                        rec["attempts"] = invoke_res.attempts
                        rec["log"] = list(logger.lines)
                        collect_list.append(rec)

                        # Write part file now with error marker
                        marker = invoke_res.error or "ERROR:unknown"
                        row_vals = collect_for_vm(job, row, outcome_marker=marker, logger=logger, var_to_step=var_to_step)
                        logger.finalize(marker)
                        write_part_files(parts_dir, row, len(target_rows), row_vals, logger)
                        run_state.update("ERROR")

                except AuthAbortedError as e:
                    logger.log_error(str(e))
                    logger.finalize("ERROR:auth")
                    write_part_files(
                        parts_dir=parts_dir,
                        row=row,
                        total_rows=len(target_rows),
                        row_values=collect_for_vm(job, row, outcome_marker="ERROR:auth", logger=logger, var_to_step=var_to_step),
                        logger=logger,
                    )
                    run_state.update("ERROR")
                    run_state.stop_requested = True
                    interrupted_event.set()

                except Exception as e:
                    err_type = type(e).__name__
                    tb = traceback.format_exc()
                    logger.log_error(f"Uncaught exception in invoke phase: {e}\n{tb}")
                    marker = f"ERROR:internal:{err_type}"
                    logger.finalize(marker)
                    write_part_files(
                        parts_dir=parts_dir,
                        row=row,
                        total_rows=len(target_rows),
                        row_values=collect_for_vm(job, row, outcome_marker=marker, logger=logger, var_to_step=var_to_step),
                        logger=logger,
                    )
                    run_state.update("ERROR")

                finally:
                    with workers_lock:
                        active_workers_count -= 1
                    sys.stderr.write("\r" + run_state.format_progress("Phase 1: Invoke", active_workers_count) + "   ")
                    sys.stderr.flush()

            if phase1_rows:
                with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as executor:
                    futures = [executor.submit(invoke_worker, r) for r in phase1_rows]
                    for f in concurrent.futures.as_completed(futures):
                        if run_state.stop_requested:
                            break

            sys.stderr.write("\n")

        # Check circuit breaker & Ctrl-C after Phase 1
        if circuit_breaker.is_tripped:
            print("\nFATAL: Run aborted due to 5 consecutive Azure authentication failures in Phase 1.", file=sys.stderr)
            print(f"Part files preserved in: {parts_dir}", file=sys.stderr)
            print(f"Rerun with: {profile.tool} run {job.yaml_path} --resume", file=sys.stderr)
            return 3

        if ctrl_c_count > 0 or run_state.stop_requested:
            print(f"\nRun interrupted by user. Note: already-fired scripts keep running on the VMs.", file=sys.stderr)
            print(f"Part files preserved in: {parts_dir}", file=sys.stderr)
            print(f"Rerun with: {profile.tool} run {job.yaml_path} --resume", file=sys.stderr)
            return 3

        # ==============================================================================
        # 10. Phase 2 — Collect (R2, R3, R4)
        # ==============================================================================
        if not args.dry_run:
            update_run_phase(parts_dir, "collect")

            # Phase 2 work set = VMs that have a record in collect list AND no finished part file
            all_collect_records = collect_list.load(target_rows)
            row_by_index = {r.index: r for r in target_rows}

            work_set: list[tuple[VMRow, dict[str, Any]]] = []
            for idx, rec in all_collect_records.items():
                if idx in row_by_index:
                    r = row_by_index[idx]
                    if not is_part_finished(parts_dir, r, len(target_rows)):
                        work_set.append((r, rec))

            if work_set:
                bound_read_results = functools.partial(read_results, command_id=profile.command_id)
                run_collect_phase(
                    job=job,
                    work_set=work_set,
                    parts_dir=parts_dir,
                    collect_script_path=collect_script_path,
                    run_id=run_id,
                    total_rows=len(target_rows),
                    parallel=parallel,
                    circuit_breaker=circuit_breaker,
                    run_state=run_state,
                    interrupted_event=interrupted_event,
                    read_fn=bound_read_results,
                    var_to_step=var_to_step,
                    precheck_engine=precheck_engine,
                )

        # Check circuit breaker & Ctrl-C after Phase 2
        if circuit_breaker.is_tripped:
            print("\nFATAL: Run aborted due to 5 consecutive Azure authentication failures in Phase 2.", file=sys.stderr)
            print(f"Part files preserved in: {parts_dir}", file=sys.stderr)
            print(f"Rerun with: {profile.tool} run {job.yaml_path} --resume", file=sys.stderr)
            return 3

        if ctrl_c_count > 0 or run_state.stop_requested:
            print(f"\nRun interrupted by user. Note: already-fired scripts keep running on the VMs.", file=sys.stderr)
            print(f"Part files preserved in: {parts_dir}", file=sys.stderr)
            print(f"Rerun with: {profile.tool} run {job.yaml_path} --resume", file=sys.stderr)
            return 3

        # ==============================================================================
        # 10.5 Phase 3 — Post (R2, R3, R4)
        # ==============================================================================
        update_run_phase(parts_dir, "post")
        all_collect_records = collect_list.load(target_rows)
        post_result = run_post_phase(
            job=job,
            target_rows=target_rows,
            collect_records=all_collect_records,
            parts_dir=parts_dir,
            run_id=run_id,
            parallel=parallel,
            circuit_breaker=circuit_breaker,
            dry_run=args.dry_run,
        )

    finally:
        stop_caffeinate(caffeinate_proc)
        signal.signal(signal.SIGINT, original_sigint)

    # ==============================================================================
    # 11. Merge Part Files (Collect Later / Still Running handling)
    # ==============================================================================
    has_still_running = any(
        is_part_still_running(parts_dir, r, len(target_rows))
        for r in target_rows
    )
    has_pending_post = any(
        (p := read_power(power_path(parts_dir, r, len(target_rows)))) is not None
        and p.get("post") != "done"
        for r in target_rows
    )

    keep_parts = args.keep_parts or has_still_running or has_pending_post

    print("Merging VM part files...")
    try:
        counts = merge_parts(
            job=job,
            all_rows=target_rows,
            parts_dir=parts_dir,
            keep_parts=keep_parts,
        )
    except MergeError as me:
        print(f"\nFATAL: {me}", file=sys.stderr)
        return 3

    if parts_dir.exists():
        update_run_phase(parts_dir, "merged")

    print("\n==================================================")
    print("Run Completed")
    print(f"  Total VMs:   {len(target_rows)}")
    print(f"  OK:          {counts.get('OK', 0)}")
    print(f"  Skipped:     {counts.get('SKIPPED', 0)}")
    print(f"  Ended:       {counts.get('ENDED', 0)}")
    print(f"  Error:       {counts.get('ERROR', 0)}")
    if post_result and (post_result.hooks_ok > 0 or post_result.hooks_failed > 0 or job.post_run):
        print(f"  Post hooks:  ok {post_result.hooks_ok} · failed {post_result.hooks_failed}")
    if post_result and (post_result.deallocated > 0 or post_result.powered_off > 0 or job.result_actions & POWER_AFTER_ACTIONS):
        print(f"  Deallocated: {post_result.deallocated}")
        print(f"  Powered off: {post_result.powered_off}")

    still_running_vms = [r for r in target_rows if is_part_still_running(parts_dir, r, len(target_rows))]
    if still_running_vms:
        names = ", ".join(r.vm_name for r in still_running_vms)
        print(f"  Left running: {len(still_running_vms)} ({names}, rerun with {profile.tool} run {job.yaml_path} --resume)")

    print(f"  Output CSV:  {job.output_csv}")
    print(f"  Output Log:  {job.output_log}")

    if still_running_vms:
        print(f"\n[!] Notice: Some VMs were still running remotely when collection timed out.")
        print(f"    Part files preserved in: {parts_dir}")
        print(f"    Rerun later to read remaining VMs: {profile.tool} run {job.yaml_path} --resume")

    print("==================================================")

    if (
        counts.get("SKIPPED", 0) > 0
        or counts.get("ENDED", 0) > 0
        or counts.get("ERROR", 0) > 0
        or (post_result and post_result.hooks_failed > 0)
        or (post_result and post_result.power_failed > 0)
        or bool(still_running_vms)
    ):
        return 2

    return 0
