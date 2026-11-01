"""Precheck evaluation engine: host-side pre-flight checks, caching, actions."""

from __future__ import annotations

import shlex
import threading
from typing import Any, Callable

from y2shared.config import Job, Precheck
from y2shared.inputcsv import VMRow
from y2shared.shell import run_bash
from y2shared.vmlog import VMLogger


class PrecheckEngine:
    """Evaluates sequential host-side prechecks with subscription caching and VM start support."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], tuple[str, int, str]] = {}
        self._cache_locks: dict[tuple[str, str], threading.Lock] = {}
        self._global_lock = threading.Lock()

    def _get_lock_for_key(self, key: tuple[str, str]) -> threading.Lock:
        with self._global_lock:
            if key not in self._cache_locks:
                self._cache_locks[key] = threading.Lock()
            return self._cache_locks[key]

    def _execute_script(self, precheck: Precheck, row: VMRow, yaml_dir) -> tuple[str, int, str]:
        script_code = precheck.script or ""
        if precheck.script_path:
            with open(precheck.script_path, "r", encoding="utf-8") as sf:
                script_code = sf.read()

        res = run_bash(script_code, env=row.env, timeout=precheck.timeout, cwd=yaml_dir)
        return res.stdout_stripped, res.rc, res.stderr

    def run_prechecks_for_vm(
        self,
        job: Job,
        row: VMRow,
        logger: VMLogger,
        phase: str = "invoke",
        on_power: Callable[[str, bool], None] | None = None,
    ) -> tuple[bool, str | None]:
        """
        Run all prechecks matching phase for a single VM in YAML order.
        Returns:
            (proceed, outcome_marker)
            If proceed is True, outcome_marker is None.
            If proceed is False, outcome_marker is SKIPPED:..., ENDED:..., or ERROR:...
        """
        prechecks = job.pre_run if job.pre_run else (job.invoke.precheck if job.invoke else [])
        stop_after = False
        for precheck in prechecks:
            if phase not in precheck.phases:
                continue

            # Check subscription cache (A6)
            if precheck.cache == "subscription":
                cache_key = (precheck.name, row.subscription)
                lock = self._get_lock_for_key(cache_key)
                with lock:
                    if cache_key in self._cache:
                        stdout, rc, stderr = self._cache[cache_key]
                        logger.log(
                            f"Precheck '{precheck.name}' result retrieved from subscription cache for '{row.subscription}'"
                        )
                    else:
                        stdout, rc, stderr = self._execute_script(precheck, row, job.yaml_dir)
                        self._cache[cache_key] = (stdout, rc, stderr)
            else:
                stdout, rc, stderr = self._execute_script(precheck, row, job.yaml_dir)

            # Match against result keys (keys normalized to strings in config) (A4)
            res_spec = None
            if stdout in precheck.result:
                res_spec = precheck.result[stdout]
            elif "default" in precheck.result:
                res_spec = precheck.result["default"]
            else:
                logger.log_precheck(
                    precheck.name,
                    stdout,
                    "unmatched",
                    f"Output '{stdout}' matched no result key and no default was defined",
                )
                return False, f"ERROR:precheck_unmatched:{precheck.name}"

            action = res_spec.get("action", "")
            log_msg = res_spec.get("log", "")
            logger.log_precheck(precheck.name, stdout, action, log_msg)

            # Action semantics (A5). The power policy is the result's own action:
            #   continue | stop          -> proceed (stop: deallocate after the job)
            #   start | start-stop       -> az vm start, recheck, proceed (start-stop: restore prev state after the job)
            #   skip | end_check         -> stop processing this VM
            if action == "continue":
                continue
            elif action == "stop":
                stop_after = True
                continue
            elif action == "end_check":
                return False, f"ENDED:{precheck.name}"
            elif action == "skip":
                if phase == "collect":
                    return False, f"ERROR:not_running_at_collect:{precheck.name}"
                return False, f"SKIPPED:{precheck.name}"
            elif action in ("start", "start-stop"):
                if action == "start-stop" and on_power:
                    # Record the state to restore BEFORE starting (write-ahead)
                    from y2shared.dispatcher import get_power_state

                    prev_state, _ = get_power_state(row, logger=logger)
                    if not prev_state:
                        prev_state = "PowerState/deallocated"
                    on_power(prev_state, True)

                logger.log(
                    f"action '{action}' triggered for {row.vm_name} (RG: {row.resource_group}). Running az vm start..."
                )
                start_cmd = (
                    f"az vm start --subscription {shlex.quote(row.subscription)} "
                    f"-g {shlex.quote(row.resource_group)} -n {shlex.quote(row.vm_name)}"
                )
                start_res = run_bash(start_cmd, env=row.env, timeout=precheck.timeout, cwd=job.yaml_dir)
                if start_res.rc != 0:
                    logger.log_error(f"az vm start failed with rc={start_res.rc}: {start_res.stderr}")
                    return False, f"ERROR:start_failed:{precheck.name}"

                logger.log(
                    f"az vm start succeeded. Re-running precheck '{precheck.name}' once to confirm state..."
                )
                # Re-run the precheck once
                re_stdout, re_rc, re_stderr = self._execute_script(precheck, row, job.yaml_dir)
                re_spec = None
                if re_stdout in precheck.result:
                    re_spec = precheck.result[re_stdout]
                elif "default" in precheck.result:
                    re_spec = precheck.result["default"]
                else:
                    return False, f"ERROR:start_failed:{precheck.name}"

                re_action = re_spec.get("action", "")
                re_log = re_spec.get("log", "")
                logger.log_precheck(precheck.name, re_stdout, re_action, f"(after start) {re_log}")

                if re_action == "continue":
                    continue
                elif re_action == "stop":
                    stop_after = True
                    continue
                else:
                    return False, f"ERROR:start_failed:{precheck.name}"
            else:
                logger.log_error(f"Unknown action '{action}' for precheck '{precheck.name}'")
                return False, f"ERROR:precheck_unknown_action:{action}"

        # 'stop' only takes effect once every check let the VM through
        if stop_after and on_power:
            on_power("PowerState/running", False)

        return True, None

