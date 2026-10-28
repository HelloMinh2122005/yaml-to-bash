"""Execution runner: worker thread pool, signal handling, progress reporting, exit codes."""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import signal
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from y2b.collector import collect_for_vm
from y2b.compiler import compile_job
from y2b.config import Job, load_job
from y2b.dispatcher import AuthCircuitBreaker, AuthAbortedError, dispatch_vm
from y2b.inputcsv import VMRow, read_input_csv
from y2b.merger import merge_parts, MergeError
from y2b.parts import (
    get_parts_dir,
    init_parts_dir,
    is_part_finished,
    write_part_files,
)
from y2b.precheck import PrecheckEngine
from y2b.validate import validate_job_file
from y2b.vmlog import VMLogger


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

    def format_progress(self, active_workers: int) -> str:
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
                f"[ {self.completed}/{self.total_vms} ] "
                f"ok {self.ok} · skipped {self.skipped} · ended {self.ended} · error {self.error} · "
                f"running {active_workers} · eta {eta_str}"
            )


def run_job(args: argparse.Namespace) -> int:
    """Main execution function invoked by `y2b run`."""
    yaml_path = Path(args.job_yaml).resolve()

    # 1. Validation
    job, val_res = validate_job_file(yaml_path)
    if not val_res.is_valid or job is None:
        for e in val_res.errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1

    for w in val_res.warnings:
        print(f"WARNING: {w}", file=sys.stderr)

    # 2. Read Input CSV
    rows, headers, csv_res = read_input_csv(job.input, job=job)
    if not csv_res.is_valid:
        for e in csv_res.errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1

    for w in csv_res.warnings:
        print(f"WARNING: {w}", file=sys.stderr)

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

    # 4. Compile Remote Script
    compiled_script, compile_errors = compile_job(job)
    if compile_errors:
        for e in compile_errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # 5. Initialize Parts Directory
    parts_dir = get_parts_dir(job.output_csv)
    try:
        init_parts_dir(
            parts_dir=parts_dir,
            job=job,
            total_rows=len(target_rows),
            parallel=parallel,
            resume=args.resume,
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # Write compiled script into parts folder
    remote_script_path = parts_dir / "remote.sh"
    try:
        with open(remote_script_path, "w", encoding="utf-8") as f:
            f.write(compiled_script)
        remote_script_path.chmod(0o755)
    except Exception as e:
        print(f"ERROR: Failed to write {remote_script_path}: {e}", file=sys.stderr)
        return 1

    # 6. Confirmation Prompt
    print(f"\n==================================================")
    print(f"yaml_to_bash Run Plan")
    print(f"  Job file:        {job.yaml_path.name}")
    print(f"  Target VMs:      {len(target_rows)} (from {job.input.name})")
    print(f"  Parallel:        {parallel}")
    print(f"  Dry run:         {args.dry_run}")
    print(f"  Resume:          {args.resume}")
    print(f"  Output CSV:      {job.output_csv}")
    print(f"  Output Log:      {job.output_log}")
    print(f"==================================================\n")

    if not args.yes and sys.stdin.isatty():
        try:
            resp = input("Proceed with execution? [y/N]: ").strip().lower()
            if resp not in ("y", "yes"):
                print("Run cancelled by user.")
                return 0
        except (KeyboardInterrupt, EOFError):
            print("\nRun cancelled.")
            return 0

    # 7. Setup Execution State & Signal Handlers
    run_state = RunState(total_vms=len(target_rows), start_time=time.time())
    circuit_breaker = AuthCircuitBreaker(threshold=5)
    precheck_engine = PrecheckEngine()

    ctrl_c_count = 0
    interrupted_event = threading.Event()

    def sigint_handler(signum, frame):
        nonlocal ctrl_c_count
        ctrl_c_count += 1
        if ctrl_c_count == 1:
            print("\n[!] Ctrl-C detected. Stopping submission of new VMs and waiting for active tasks to finish...", file=sys.stderr)
            print("[!] Press Ctrl-C again to abort immediately.", file=sys.stderr)
            run_state.stop_requested = True
            interrupted_event.set()
        else:
            print("\n[!] Second Ctrl-C received. Aborting immediately.", file=sys.stderr)
            os._exit(3)

    original_sigint = signal.signal(signal.SIGINT, sigint_handler)

    # 8. Worker Function
    active_workers_count = 0
    workers_lock = threading.Lock()

    def process_vm(row: VMRow) -> None:
        nonlocal active_workers_count

        if run_state.stop_requested:
            return

        # Skip if already finished (when resuming)
        if args.resume and is_part_finished(parts_dir, row, len(target_rows)):
            run_state.update("OK")
            return

        with workers_lock:
            active_workers_count += 1

        logger = VMLogger(row.vm_name, row.index)
        outcome_marker: str | None = None
        remote_res = None

        try:
            # Phase 1: Precheck
            proceed, precheck_marker = precheck_engine.run_prechecks_for_vm(job, row, logger)
            if not proceed:
                outcome_marker = precheck_marker
            else:
                # Phase 2: Dispatch
                if args.dry_run:
                    logger.log("[dry-run] Prechecks passed; skipping run-command execution.")
                    outcome_marker = "DRY_RUN:ok"
                else:
                    remote_res = dispatch_vm(
                        job=job,
                        row=row,
                        remote_script_path=remote_script_path,
                        logger=logger,
                        circuit_breaker=circuit_breaker,
                    )
                    if remote_res.error:
                        outcome_marker = remote_res.error

            # Phase 3: Collect
            row_values = collect_for_vm(
                job=job,
                row=row,
                remote_result=remote_res,
                outcome_marker=outcome_marker,
                logger=logger,
            )

            # Determine outcome type for progress reporting
            row_str = " ".join(row_values)
            if "ERROR:" in row_str:
                outcome_type = "ERROR"
            elif "SKIPPED:" in row_str:
                outcome_type = "SKIPPED"
            elif "ENDED:" in row_str:
                outcome_type = "ENDED"
            else:
                outcome_type = "OK"

            logger.finalize(outcome_marker or outcome_type)

            # Phase 4: Write part files
            write_part_files(
                parts_dir=parts_dir,
                row=row,
                total_rows=len(target_rows),
                row_values=row_values,
                logger=logger,
            )

            run_state.update(outcome_type)

        except AuthAbortedError as e:
            logger.log_error(str(e))
            logger.finalize("ERROR:auth")
            write_part_files(
                parts_dir=parts_dir,
                row=row,
                total_rows=len(target_rows),
                row_values=collect_for_vm(job, row, outcome_marker="ERROR:auth", logger=logger),
                logger=logger,
            )
            run_state.update("ERROR")
            run_state.stop_requested = True
            interrupted_event.set()

        except Exception as e:
            err_type = type(e).__name__
            tb = traceback.format_exc()
            logger.log_error(f"Uncaught exception: {e}\n{tb}")
            marker = f"ERROR:internal:{err_type}"
            logger.finalize(marker)
            write_part_files(
                parts_dir=parts_dir,
                row=row,
                total_rows=len(target_rows),
                row_values=collect_for_vm(job, row, outcome_marker=marker, logger=logger),
                logger=logger,
            )
            run_state.update("ERROR")

        finally:
            with workers_lock:
                active_workers_count -= 1
            # Print progress update
            sys.stderr.write("\r" + run_state.format_progress(active_workers_count) + "   ")
            sys.stderr.flush()

    # 9. Execute Pool
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = []
            for row in target_rows:
                if run_state.stop_requested:
                    break
                futures.append(executor.submit(process_vm, row))

            for f in concurrent.futures.as_completed(futures):
                if run_state.stop_requested:
                    break
    finally:
        signal.signal(signal.SIGINT, original_sigint)

    sys.stderr.write("\n")

    # 10. Check if interrupted or auth circuit breaker tripped
    if circuit_breaker.is_tripped:
        print("\nFATAL: Run aborted due to 5 consecutive Azure authentication failures.", file=sys.stderr)
        print(f"Part files preserved in: {parts_dir}", file=sys.stderr)
        print(f"Rerun with: y2b run {job.yaml_path} --resume", file=sys.stderr)
        return 3

    if ctrl_c_count > 0:
        print(f"\nRun interrupted by user. Part files preserved in: {parts_dir}", file=sys.stderr)
        print(f"Rerun with: y2b run {job.yaml_path} --resume", file=sys.stderr)
        return 3

    # 11. Merge Part Files
    print("Merging VM part files...")
    try:
        counts = merge_parts(
            job=job,
            all_rows=target_rows,
            parts_dir=parts_dir,
            keep_parts=args.keep_parts,
        )
    except MergeError as me:
        print(f"\nFATAL: {me}", file=sys.stderr)
        return 3

    print("\n==================================================")
    print("Run Completed Successfully")
    print(f"  Total VMs:   {len(target_rows)}")
    print(f"  OK:          {counts.get('OK', 0)}")
    print(f"  Skipped:     {counts.get('SKIPPED', 0)}")
    print(f"  Ended:       {counts.get('ENDED', 0)}")
    print(f"  Error:       {counts.get('ERROR', 0)}")
    print(f"  Output CSV:  {job.output_csv}")
    print(f"  Output Log:  {job.output_log}")
    print("==================================================")

    # Exit code determination:
    # 0 if all VMs OK
    # 2 if completed but some VMs SKIPPED, ENDED, or ERROR
    if counts.get("SKIPPED", 0) > 0 or counts.get("ENDED", 0) > 0 or counts.get("ERROR", 0) > 0:
        return 2

    return 0

