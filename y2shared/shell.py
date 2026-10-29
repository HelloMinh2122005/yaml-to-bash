"""Bash command execution helper for prechecks and computed columns."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from y2shared.hostenv import resolve_bash


@dataclass
class ShellResult:
    rc: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def stdout_stripped(self) -> str:
        return self.stdout.strip()


def run_bash(
    script: str,
    env: dict[str, str] | None = None,
    timeout: int = 120,
    cwd: Path | None = None,
) -> ShellResult:
    """Run a bash snippet using 'bash -c' with the given environment and timeout."""
    merged_env = os.environ.copy()
    if env:
        for k, v in env.items():
            merged_env[str(k)] = str(v)

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
        return ShellResult(
            rc=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            timed_out=False,
        )
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout.decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = e.stderr.decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        return ShellResult(
            rc=124,
            stdout=stdout,
            stderr=f"{stderr}\n[TIMEOUT] command timed out after {timeout} seconds",
            timed_out=True,
        )
    except Exception as e:
        return ShellResult(
            rc=1,
            stdout="",
            stderr=f"[ERROR] failed to run bash command: {e}",
            timed_out=False,
        )

