"""Tests for macOS / host environment hardening (hostenv.py, bash 3.2, nofile, caffeinate)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
import pytest

from y2shared.hostenv import (
    resolve_bash,
    check_host_bash,
    reset_host_bash_warning,
    raise_nofile,
    start_caffeinate,
    stop_caffeinate,
)
from y2shared.config import load_job
from y2b.compiler import compile_job


def test_resolve_bash_honours_y2_bash(tmp_path, monkeypatch):
    custom_bash = tmp_path / "custom_bash"
    custom_bash.write_text("#!/bin/sh\necho '5.2'\n")
    custom_bash.chmod(0o755)

    monkeypatch.setenv("Y2_BASH", str(custom_bash))
    bash_path, version = resolve_bash()
    assert bash_path == str(custom_bash)
    assert version == (5, 2)


def test_fake_bash_32_emits_warning(tmp_path, monkeypatch, capsys):
    fake_bash = tmp_path / "fake_bash_32"
    fake_bash.write_text("#!/bin/sh\necho '3.2'\n")
    fake_bash.chmod(0o755)

    monkeypatch.setenv("Y2_BASH", str(fake_bash))
    reset_host_bash_warning()

    warnings_caught = []
    check_host_bash(warn_fn=warnings_caught.append)
    assert len(warnings_caught) == 1
    assert "host bash 3.2" in warnings_caught[0]
    assert "brew install bash" in warnings_caught[0]

    # Verify second call emits nothing (at most once per run)
    check_host_bash(warn_fn=warnings_caught.append)
    assert len(warnings_caught) == 1


def test_bash_32_syntax_error_becomes_warning(tmp_path, monkeypatch, capsys):
    # Fake bash that returns 3.2 on versinfo, but fails -n syntax checks with rc=1
    fake_bash = tmp_path / "fake_bash_32"
    fake_bash.write_text("""#!/bin/sh
if [ "$1" = "-n" ]; then
    echo "bash: syntax error near unexpected token |&" >&2
    exit 1
fi
echo '3.2'
""")
    fake_bash.chmod(0o755)

    monkeypatch.setenv("Y2_BASH", str(fake_bash))
    job = load_job("examples/hip-compliance/job.yaml")
    compiled, errors = compile_job(job)

    # In bash 3.2, errors are converted to warnings and not treated as fatal compile errors
    assert len(errors) == 0
    captured = capsys.readouterr()
    assert "checked with bash 3.2" in captured.err


def test_raise_nofile():
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except Exception:
        pytest.skip("resource.RLIMIT_NOFILE not supported on this platform")

    if hard < 100:
        pytest.skip(f"Hard limit {hard} is too low to test raising soft limit")

    new_soft, new_hard = raise_nofile(required_fds=64)
    assert new_soft >= min(hard, 4096) or new_soft >= soft


def test_caffeinate_lifecycle():
    # If not on Darwin, returns None safely
    if sys.platform != "darwin":
        proc = start_caffeinate()
        assert proc is None
        stop_caffeinate(proc)
    else:
        proc = start_caffeinate()
        stop_caffeinate(proc)


def test_bin_y2b_runs_from_outside_repo(tmp_path):
    repo_root = Path(__file__).resolve().parent.parent
    bin_y2b = repo_root / "bin" / "y2b"

    proc = subprocess.run(
        [str(bin_y2b), "--help"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "yaml_to_bash" in proc.stdout
