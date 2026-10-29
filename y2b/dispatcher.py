"""Azure Run-Command dispatcher, retry logic, error classification, and output parsing."""

from __future__ import annotations

import base64
import json
import os
import random
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from y2b.config import Job
from y2b.inputcsv import VMRow
from y2b.vmlog import VMLogger

INVOKE_CALL_TIMEOUT = 300
READER_CALL_TIMEOUT = 300


class AuthCircuitBreaker:
    """Tracks consecutive auth errors across all worker threads."""

    def __init__(self, threshold: int = 5) -> None:
        self.threshold = threshold
        self.consecutive_auth_errors = 0
        self.aborted = False

    def record_success(self) -> None:
        self.consecutive_auth_errors = 0

    def record_auth_error(self) -> None:
        self.consecutive_auth_errors += 1
        if self.consecutive_auth_errors >= self.threshold:
            self.aborted = True

    @property
    def is_tripped(self) -> bool:
        return self.aborted


class AuthAbortedError(Exception):
    """Raised when the auth circuit breaker trips after 5 consecutive auth errors."""
    pass


@dataclass
class InvokeResult:
    success: bool
    error: str | None = None
    attempts: int = 1
    duration_s: float = 0.0
    raw_message: str = ""


@dataclass
class ReadResult:
    status: str = "OK"  # "OK", "STILL_RUNNING", "ERROR"
    vars: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    attempts: int = 1
    duration_s: float = 0.0
    raw_message: str = ""


# Backward-compatible alias
RemoteResult = ReadResult


# Error classification patterns
PATTERNS_THROTTLED = ("toomanyrequests", "429", "throttl")
PATTERNS_BUSY = ("conflict", "in progress", "operationnotallowed")
PATTERNS_AGENT = ("vmagentstatuscommunicationerror", "agent not ready", "agent is not ready")
PATTERNS_NOT_FOUND = ("resourcenotfound",)
PATTERNS_AUTH = ("authorizationfailed", "az login", "aadsts")


def classify_error(stderr: str, rc: int) -> str:
    """Classify Azure CLI error based on exit code and stderr."""
    err_lower = stderr.lower()
    for p in PATTERNS_THROTTLED:
        if p in err_lower:
            return "throttled"
    for p in PATTERNS_BUSY:
        if p in err_lower:
            return "busy"
    for p in PATTERNS_AGENT:
        if p in err_lower:
            return "agent"
    for p in PATTERNS_NOT_FOUND:
        if p in err_lower:
            return "missing"
    for p in PATTERNS_AUTH:
        if p in err_lower:
            return "auth"
    return "unknown"


def parse_run_command_output(
    raw_json_str: str,
    logger: VMLogger | None = None,
) -> tuple[dict[str, str], str | None, str]:
    """
    Parse JSON from az vm run-command invoke.
    Returns:
        (vars_dict, error_marker, raw_message)
    """
    try:
        data = json.loads(raw_json_str)
    except Exception as e:
        if logger:
            logger.log_error(f"Failed to parse az JSON response: {e}\nRaw output: {raw_json_str[:500]}")
        return {}, "ERROR:az:invalid_json_response", raw_json_str

    # Extract message from value[0].message
    message = ""
    if isinstance(data, dict):
        val = data.get("value")
        if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
            message = val[0].get("message", "")
        elif "message" in data:
            message = str(data["message"])
    elif isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
        message = data[0].get("message", "")

    # Extract stdout section: text between [stdout] and [stderr]
    stdout_text = message
    if "[stdout]" in message:
        after_stdout = message.split("[stdout]", 1)[1]
        if "[stderr]" in after_stdout:
            stdout_text = after_stdout.split("[stderr]", 1)[0]
        else:
            stdout_text = after_stdout

    has_begin = "===BEGIN_RESULT_RES===" in stdout_text
    has_end = "===END_RESULT_RES===" in stdout_text

    if has_end and not has_begin:
        # 4 KB output truncation (A1/3.6)
        if logger:
            logger.log_error("Result block truncated: ===END_RESULT_RES=== found but ===BEGIN_RESULT_RES=== was cut off by 4KB limit")
        return {}, "ERROR:result_truncated", message

    if not has_begin and not has_end:
        if logger:
            logger.log_error(f"No result block found in run-command message: {message[:500]}")
        return {}, "ERROR:no_result_block", message

    # Both markers present
    block = stdout_text.split("===BEGIN_RESULT_RES===")[1].split("===END_RESULT_RES===")[0]
    parsed_vars: dict[str, str] = {}

    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            if logger:
                logger.log(f"Warning: malformed result line ignored: {line}")
            continue

        var_name, b64_val = line.split("=", 1)
        var_name = var_name.strip()
        b64_val = b64_val.strip()

        try:
            decoded_val = base64.b64decode(b64_val.encode("ascii")).decode("utf-8", errors="replace")
            parsed_vars[var_name] = decoded_val
        except Exception as e:
            if logger:
                logger.log(f"Warning: failed to base64-decode value for '{var_name}': {e}")
            continue

    return parsed_vars, None, message


def _az_run_command(
    cmd: list[str],
    timeout: int,
    busy_policy: str,  # "retry" or "return_busy"
    logger: VMLogger,
    circuit_breaker: AuthCircuitBreaker | None = None,
    delay_multiplier: float = 1.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[str, int | None, str, str, int, float]:
    """
    Execute an Azure CLI command with standard retry and backoff loop.
    Returns:
        (status_or_marker, returncode, stdout, stderr, total_attempts, duration_s)
    """
    if circuit_breaker and circuit_breaker.is_tripped:
        raise AuthAbortedError("Run aborted: 5 consecutive Azure authentication failures")

    az_env = os.environ.copy()
    az_env["AZURE_CORE_COLLECT_TELEMETRY"] = "no"
    az_env["AZURE_CORE_NO_COLOR"] = "1"
    az_env["AZURE_CORE_ONLY_SHOW_ERRORS"] = "1"

    throttled_backoffs = [15, 30, 60, 120, 240]
    throttled_attempt = 0
    busy_attempt = 0
    unknown_attempt = 0
    total_attempts = 0

    start_time = time.time()

    while True:
        total_attempts += 1
        logger.log_dispatch(f"Executing az command (attempt {total_attempts}): {' '.join(cmd[:6])}...")

        try:
            proc = subprocess.run(
                cmd,
                env=az_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            duration = time.time() - start_time
            logger.log_error(f"az command timed out after {timeout}s.")
            return "ERROR:run_command_timeout", None, "", (e.stderr or ""), total_attempts, duration
        except Exception as e:
            duration = time.time() - start_time
            logger.log_error(f"Failed to launch az CLI process: {e}")
            return f"ERROR:internal:{type(e).__name__}", None, "", str(e), total_attempts, duration

        duration = time.time() - start_time

        if proc.returncode == 0:
            if circuit_breaker:
                circuit_breaker.record_success()
            return "SUCCESS", 0, proc.stdout, proc.stderr, total_attempts, duration

        # Non-zero return code: classify error
        err_class = classify_error(proc.stderr, proc.returncode)
        first_stderr_line = (proc.stderr.strip().splitlines() or ["unknown"])[0][:80]
        logger.log_dispatch(f"Command failed (rc={proc.returncode}, class={err_class}): {first_stderr_line}")

        if err_class == "auth":
            if circuit_breaker:
                circuit_breaker.record_auth_error()
                if circuit_breaker.is_tripped:
                    raise AuthAbortedError("Run aborted: 5 consecutive Azure authentication failures")
            return "ERROR:auth", proc.returncode, proc.stdout, proc.stderr, total_attempts, duration

        elif err_class == "agent":
            return "ERROR:vm_agent_unavailable", proc.returncode, proc.stdout, proc.stderr, total_attempts, duration

        elif err_class == "missing":
            return "ERROR:vm_not_found", proc.returncode, proc.stdout, proc.stderr, total_attempts, duration

        elif err_class == "throttled":
            if throttled_attempt < len(throttled_backoffs):
                base_delay = throttled_backoffs[throttled_attempt]
                jitter = random.uniform(0.8, 1.2)
                wait_sec = base_delay * jitter * delay_multiplier
                logger.log_dispatch(f"Throttled by Azure. Backing off for {wait_sec:.1f}s (retry {throttled_attempt + 1}/{len(throttled_backoffs)})...")
                throttled_attempt += 1
                sleep_fn(wait_sec)
                continue
            else:
                return "ERROR:throttled", proc.returncode, proc.stdout, proc.stderr, total_attempts, duration

        elif err_class == "busy":
            if busy_policy == "return_busy":
                logger.log_dispatch("VM run-command busy (conflict); returning STILL_RUNNING.")
                return "STILL_RUNNING", proc.returncode, proc.stdout, proc.stderr, total_attempts, duration
            else:
                if busy_attempt < 3:
                    wait_sec = 60 * delay_multiplier
                    logger.log_dispatch(f"VM run-command busy (conflict). Waiting {wait_sec:.1f}s (retry {busy_attempt + 1}/3)...")
                    busy_attempt += 1
                    sleep_fn(wait_sec)
                    continue
                else:
                    return "ERROR:run_command_busy", proc.returncode, proc.stdout, proc.stderr, total_attempts, duration

        else:
            # Unknown error: retry once after 30s
            if unknown_attempt < 1:
                wait_sec = 30 * delay_multiplier
                logger.log_dispatch(f"Unknown Azure error. Retrying once after {wait_sec:.1f}s...")
                unknown_attempt += 1
                sleep_fn(wait_sec)
                continue
            else:
                return f"ERROR:az:{first_stderr_line}", proc.returncode, proc.stdout, proc.stderr, total_attempts, duration


def invoke_nowait(
    row: VMRow,
    remote_script_path: Path,
    logger: VMLogger,
    circuit_breaker: AuthCircuitBreaker | None = None,
    delay_multiplier: float = 1.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    timeout: int = INVOKE_CALL_TIMEOUT,
) -> InvokeResult:
    """
    Fire the compiled script on the VM with --no-wait.
    Returns:
        InvokeResult(success=True) on rc 0, or InvokeResult(success=False, error=marker).
    """
    cmd = [
        "az",
        "vm",
        "run-command",
        "invoke",
        "--subscription",
        row.subscription,
        "-g",
        row.resource_group,
        "-n",
        row.vm_name,
        "--command-id",
        "RunShellScript",
        "--scripts",
        f"@{remote_script_path}",
        "--no-wait",
    ]

    status, rc, stdout, stderr, attempts, duration = _az_run_command(
        cmd=cmd,
        timeout=timeout,
        busy_policy="retry",
        logger=logger,
        circuit_breaker=circuit_breaker,
        delay_multiplier=delay_multiplier,
        sleep_fn=sleep_fn,
    )

    if status == "SUCCESS":
        logger.log_dispatch(f"invoke --no-wait accepted in {duration:.1f}s.")
        return InvokeResult(
            success=True,
            error=None,
            attempts=attempts,
            duration_s=duration,
            raw_message=stdout,
        )
    else:
        return InvokeResult(
            success=False,
            error=status,
            attempts=attempts,
            duration_s=duration,
            raw_message=stderr,
        )


def read_results(
    row: VMRow,
    collect_script_path: Path,
    run_id: str,
    logger: VMLogger,
    circuit_breaker: AuthCircuitBreaker | None = None,
    delay_multiplier: float = 1.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    timeout: int = READER_CALL_TIMEOUT,
) -> ReadResult:
    """
    Read results from VM by executing collect.sh synchronously.
    Handles STILL_RUNNING on busy conflict, checks RUN_ID and DONE markers.
    """
    cmd = [
        "az",
        "vm",
        "run-command",
        "invoke",
        "--subscription",
        row.subscription,
        "-g",
        row.resource_group,
        "-n",
        row.vm_name,
        "--command-id",
        "RunShellScript",
        "--scripts",
        f"@{collect_script_path}",
        "-o",
        "json",
    ]

    status, rc, stdout, stderr, attempts, duration = _az_run_command(
        cmd=cmd,
        timeout=timeout,
        busy_policy="return_busy",
        logger=logger,
        circuit_breaker=circuit_breaker,
        delay_multiplier=delay_multiplier,
        sleep_fn=sleep_fn,
    )

    if status == "STILL_RUNNING":
        return ReadResult(
            status="STILL_RUNNING",
            attempts=attempts,
            duration_s=duration,
            raw_message=stderr,
        )

    if status != "SUCCESS":
        return ReadResult(
            status="ERROR",
            error=status,
            attempts=attempts,
            duration_s=duration,
            raw_message=stderr,
        )

    parsed_vars, err_marker, raw_msg = parse_run_command_output(stdout, logger=logger)
    if err_marker:
        return ReadResult(
            status="ERROR",
            error=err_marker,
            attempts=attempts,
            duration_s=duration,
            raw_message=raw_msg,
        )

    if "__Y2B_RUN__" not in parsed_vars:
        logger.log_error("No __Y2B_RUN__ marker in res_file (script never started)")
        return ReadResult(
            status="ERROR",
            error="ERROR:not_started",
            attempts=attempts,
            duration_s=duration,
            raw_message=raw_msg,
        )

    res_run_id = parsed_vars.get("__Y2B_RUN__")
    if res_run_id != run_id:
        logger.log_error(f"Stale result: res_file __Y2B_RUN__ is '{res_run_id}', expected current run_id '{run_id}'")
        return ReadResult(
            status="ERROR",
            error="ERROR:stale_result",
            attempts=attempts,
            duration_s=duration,
            raw_message=raw_msg,
        )

    if "__Y2B_DONE__" not in parsed_vars or parsed_vars.get("__Y2B_DONE__") != run_id:
        logger.log_error(f"Incomplete remote execution: __Y2B_RUN__ present for '{run_id}', but no valid __Y2B_DONE__ marker")
        return ReadResult(
            status="ERROR",
            error="ERROR:remote_incomplete",
            attempts=attempts,
            duration_s=duration,
            raw_message=raw_msg,
        )

    # Valid run and done markers match run_id
    logger.log_dispatch(f"Reader finished in {duration:.1f}s with {len(parsed_vars)} variables.")
    return ReadResult(
        status="OK",
        vars=parsed_vars,
        attempts=attempts,
        duration_s=duration,
        raw_message=raw_msg,
    )


def dispatch_vm(
    job: Job,
    row: VMRow,
    remote_script_path: Path,
    logger: VMLogger,
    circuit_breaker: AuthCircuitBreaker | None = None,
    delay_multiplier: float = 1.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> ReadResult:
    """Synchronous dispatch function (legacy wrapper)."""
    cmd = [
        "az",
        "vm",
        "run-command",
        "invoke",
        "--subscription",
        row.subscription,
        "-g",
        row.resource_group,
        "-n",
        row.vm_name,
        "--command-id",
        "RunShellScript",
        "--scripts",
        f"@{remote_script_path}",
        "-o",
        "json",
    ]
    status, rc, stdout, stderr, attempts, duration = _az_run_command(
        cmd=cmd,
        timeout=job.run_timeout,
        busy_policy="retry",
        logger=logger,
        circuit_breaker=circuit_breaker,
        delay_multiplier=delay_multiplier,
        sleep_fn=sleep_fn,
    )
    if status == "SUCCESS":
        parsed_vars, err_marker, raw_msg = parse_run_command_output(stdout, logger=logger)
        return ReadResult(
            status="OK" if not err_marker else "ERROR",
            vars=parsed_vars,
            error=err_marker,
            attempts=attempts,
            duration_s=duration,
            raw_message=raw_msg,
        )
    return ReadResult(
        status="ERROR",
        error=status,
        attempts=attempts,
        duration_s=duration,
        raw_message=stderr,
    )
