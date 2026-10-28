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
class RemoteResult:
    vars: dict[str, str] = field(default_factory=dict)
    raw_message: str = ""
    attempts: int = 1
    duration_s: float = 0.0
    error: str | None = None


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


def dispatch_vm(
    job: Job,
    row: VMRow,
    remote_script_path: Path,
    logger: VMLogger,
    circuit_breaker: AuthCircuitBreaker | None = None,
    delay_multiplier: float = 1.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> RemoteResult:
    """
    Invoke remote script on VM using Azure CLI Run-Command with full error handling and retries.
    """
    if circuit_breaker and circuit_breaker.is_tripped:
        raise AuthAbortedError("Run aborted: 5 consecutive Azure authentication failures")

    # az vm run-command invoke command
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

    # Required Azure CLI environment
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
        logger.log_dispatch(f"Executing az vm run-command invoke (attempt {total_attempts})...")

        try:
            proc = subprocess.run(
                cmd,
                env=az_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=job.run_timeout,
            )
        except subprocess.TimeoutExpired as e:
            duration = time.time() - start_time
            logger.log_error(f"az vm run-command timed out after {job.run_timeout}s. Note: remote command may still be running on the VM!")
            return RemoteResult(
                error="ERROR:run_command_timeout",
                attempts=total_attempts,
                duration_s=duration,
            )
        except Exception as e:
            duration = time.time() - start_time
            logger.log_error(f"Failed to launch az CLI process: {e}")
            return RemoteResult(
                error=f"ERROR:internal:{type(e).__name__}",
                attempts=total_attempts,
                duration_s=duration,
            )

        duration = time.time() - start_time

        # Check returncode
        if proc.returncode == 0:
            if circuit_breaker:
                circuit_breaker.record_success()

            parsed_vars, err_marker, raw_msg = parse_run_command_output(proc.stdout, logger=logger)
            logger.log_dispatch(f"Run-command finished in {duration:.1f}s with {len(parsed_vars)} variables.")
            return RemoteResult(
                vars=parsed_vars,
                raw_message=raw_msg,
                attempts=total_attempts,
                duration_s=duration,
                error=err_marker,
            )

        # Non-zero return code: classify error
        err_class = classify_error(proc.stderr, proc.returncode)
        first_stderr_line = (proc.stderr.strip().splitlines() or ["unknown"])[0][:80]
        logger.log_dispatch(f"Command failed (rc={proc.returncode}, class={err_class}): {first_stderr_line}")

        if err_class == "auth":
            if circuit_breaker:
                circuit_breaker.record_auth_error()
                if circuit_breaker.is_tripped:
                    raise AuthAbortedError("Run aborted: 5 consecutive Azure authentication failures")
            return RemoteResult(
                error="ERROR:auth",
                raw_message=proc.stderr,
                attempts=total_attempts,
                duration_s=duration,
            )

        elif err_class == "agent":
            return RemoteResult(
                error="ERROR:vm_agent_unavailable",
                raw_message=proc.stderr,
                attempts=total_attempts,
                duration_s=duration,
            )

        elif err_class == "missing":
            return RemoteResult(
                error="ERROR:vm_not_found",
                raw_message=proc.stderr,
                attempts=total_attempts,
                duration_s=duration,
            )

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
                return RemoteResult(
                    error="ERROR:throttled",
                    raw_message=proc.stderr,
                    attempts=total_attempts,
                    duration_s=duration,
                )

        elif err_class == "busy":
            if busy_attempt < 3:
                wait_sec = 60 * delay_multiplier
                logger.log_dispatch(f"VM run-command busy (conflict). Waiting {wait_sec:.1f}s (retry {busy_attempt + 1}/3)...")
                busy_attempt += 1
                sleep_fn(wait_sec)
                continue
            else:
                return RemoteResult(
                    error="ERROR:run_command_busy",
                    raw_message=proc.stderr,
                    attempts=total_attempts,
                    duration_s=duration,
                )

        else:
            # Unknown error: retry once after 30s
            if unknown_attempt < 1:
                wait_sec = 30 * delay_multiplier
                logger.log_dispatch(f"Unknown Azure error. Retrying once after {wait_sec:.1f}s...")
                unknown_attempt += 1
                sleep_fn(wait_sec)
                continue
            else:
                return RemoteResult(
                    error=f"ERROR:az:{first_stderr_line}",
                    raw_message=proc.stderr,
                    attempts=total_attempts,
                    duration_s=duration,
                )
