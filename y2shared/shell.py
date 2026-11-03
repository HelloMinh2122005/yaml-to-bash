"""Bash command execution helper for prechecks and computed columns."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List

from y2shared.hostenv import resolve_bash


@dataclass
class ShellResult:
    rc: int
    stdout: str
    stderr: str
    timed_out: bool = False
    az_calls: List[Any] = field(default_factory=list)

    @property
    def stdout_stripped(self) -> str:
        return self.stdout.strip()


def run_bash(
    script: str,
    env: dict[str, str] | None = None,
    timeout: int = 120,
    cwd: Path | None = None,
    trace_section: str | None = None,
    trace_name: str = "",
) -> ShellResult:
    """Run a bash snippet using 'bash -c' with the given environment and timeout."""
    merged_env = os.environ.copy()
    if env:
        for k, v in env.items():
            merged_env[str(k)] = str(v)

    trace = None
    if trace_section is not None:
        from y2shared.aztrace import prepare_script_trace
        trace = prepare_script_trace(trace_section, trace_name)
        shim_dir = trace.env["Y2_AZ_SHIM_DIR"]
        old_path = merged_env.get("PATH", "")
        merged_env["PATH"] = f"{shim_dir}:{old_path}" if old_path else shim_dir
        merged_env["Y2_AZ_SHIM_DIR"] = shim_dir
        merged_env["Y2_AZ_TRACE_FILE"] = trace.env["Y2_AZ_TRACE_FILE"]
        merged_env["Y2_AZ_TRACE_CTX"] = trace.env["Y2_AZ_TRACE_CTX"]

    bash_path, _ = resolve_bash()
    try:
        proc = subprocess.run(
            [bash_path, "-c", script],
            env=merged_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
        )
        az_calls = trace.collect() if trace is not None else []
        return ShellResult(
            rc=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            timed_out=False,
            az_calls=az_calls,
        )
    except subprocess.TimeoutExpired as e:
        az_calls = trace.collect() if trace is not None else []
        stdout = e.stdout.decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = e.stderr.decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        return ShellResult(
            rc=124,
            stdout=stdout,
            stderr=f"{stderr}\n[TIMEOUT] command timed out after {timeout} seconds",
            timed_out=True,
            az_calls=az_calls,
        )
    except Exception as e:
        az_calls = trace.collect() if trace is not None else []
        return ShellResult(
            rc=1,
            stdout="",
            stderr=f"[ERROR] failed to run bash command: {e}",
            timed_out=False,
            az_calls=az_calls,
        )

