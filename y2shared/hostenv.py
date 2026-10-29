"""Host environment detection, bash resolution, and resource limits for macOS and Linux."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

try:
    import resource
except ImportError:
    resource = None


def resolve_bash() -> tuple[str, tuple[int, int]]:
    """
    Resolve host bash binary path and its version (major, minor).
    Checks $Y2_BASH first, then searches PATH for 'bash', defaulting to '/bin/bash'.
    """
    custom = os.environ.get("Y2_BASH")
    if custom:
        bash_path = custom
    else:
        found = shutil.which("bash")
        bash_path = found if found else "/bin/bash"

    major, minor = 0, 0
    try:
        proc = subprocess.run(
            [bash_path, "-c", 'echo "${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"'],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            out = proc.stdout.strip()
            parts = out.split(".", 1)
            major = int(parts[0])
            minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    except Exception:
        pass

    return bash_path, (major, minor)


_bash_warning_emitted = False


def reset_host_bash_warning() -> None:
    """Reset the one-time warning flag (useful in tests)."""
    global _bash_warning_emitted
    _bash_warning_emitted = False


def check_host_bash(warn_fn=None) -> None:
    """
    Emit warning if host bash is < 4.
    Emits at most once per process (unless reset).
    """
    global _bash_warning_emitted
    if _bash_warning_emitted:
        return
    bash_path, (major, minor) = resolve_bash()
    if major > 0 and major < 4:
        _bash_warning_emitted = True
        msg = (
            f"host bash {major}.{minor}: prechecks and computed columns must be "
            f"3.2-compatible; `brew install bash` recommended"
        )
        if warn_fn:
            warn_fn(msg)
        else:
            print(f"WARNING: {msg}", file=sys.stderr)


def raise_nofile(required_fds: int | None = None) -> tuple[int, int]:
    """
    Raise RLIMIT_NOFILE soft limit up to min(hard, 4096).
    If soft limit remains below required_fds, prints/logs a warning.
    Returns (soft, hard) limits.
    """
    if resource is None:
        return (0, 0)

    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except Exception:
        return (0, 0)

    effective_hard = hard
    if effective_hard <= 0 or effective_hard > 1_000_000:
        target = 4096
    else:
        target = min(effective_hard, 4096)

    if target > soft:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            soft = target
        except Exception:
            pass

    if required_fds is not None and soft < required_fds:
        print(
            f"WARNING: File descriptor limit ({soft}) is below recommended {required_fds} (parallel * 4 + 32). "
            f"Increase with 'ulimit -n {required_fds}'",
            file=sys.stderr,
        )

    return soft, hard


def start_caffeinate() -> subprocess.Popen | None:
    """
    On Darwin (macOS), spawn caffeinate -i -w <pid> to prevent sleep during run.
    """
    if sys.platform != "darwin":
        return None
    if not shutil.which("caffeinate"):
        return None
    try:
        proc = subprocess.Popen(
            ["caffeinate", "-i", "-w", str(os.getpid())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc
    except Exception:
        return None


def stop_caffeinate(proc: subprocess.Popen | None) -> None:
    """Stop caffeinate background process."""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        pass
