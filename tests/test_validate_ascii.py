"""T1 tests for static non-ASCII scan in job configuration, CSV, and script files (T-05 / TP-09..TP-12)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from tests.helpers_ado import make_fleet, run_cli
from y2shared.asciiguard import scan_non_ascii


REPO_ROOT = Path(__file__).resolve().parent.parent


# ==============================================================================
# TP-09: Validate scan per input type (job comment, value, CSV cell, script_path)
# ==============================================================================


@pytest.mark.parametrize("tool", ["y2b", "y2p"])
@pytest.mark.parametrize(
    "input_type",
    ["job_comment", "job_value", "csv_cell", "host_script", "remote_script", "collect_script", "post_run_script"],
)
def test_validate_ascii_input_type(tmp_path: Path, tool: str, input_type: str):
    """TP-09: One non-ASCII char in each input type causes validate to exit 1 with <path>:<line>:<col>: non-ASCII character U+1EC5."""
    os_type = "linux" if tool == "y2b" else "windows"
    fleet = make_fleet(tmp_path, n=1, os_type=os_type)
    job_yaml = fleet["job_yaml"]
    scripts_dir = fleet["scripts_dir"]
    offender = "ễ"  # U+1EC5
    offending_file: Path

    if input_type == "job_comment":
        offending_file = job_yaml
        content = job_yaml.read_text(encoding="utf-8")
        job_yaml.write_text(f"# comment with {offender}\n" + content, encoding="utf-8")
        expected_line = 1
        expected_col = 16
    elif input_type == "job_value":
        offending_file = job_yaml
        content = job_yaml.read_text(encoding="utf-8")
        job_yaml.write_text(f"description: test_{offender}\n" + content, encoding="utf-8")
        expected_line = 1
        expected_col = 19
    elif input_type == "csv_cell":
        csv_path = tmp_path / "list_vm.csv"
        offending_file = csv_path
        lines = csv_path.read_text(encoding="utf-8").splitlines()
        # Header is line 1, data is line 2
        lines[1] = lines[1] + f",{offender}"
        csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        expected_line = 2
        expected_col = len(lines[1])
    elif input_type == "host_script":
        # check_state.sh is the precheck host script
        script_path = scripts_dir / "check_state.sh"
        offending_file = script_path
        content = script_path.read_text(encoding="utf-8")
        script_path.write_text(f"# host script comment {offender}\n" + content, encoding="utf-8")
        expected_line = 1
        expected_col = 23
    elif input_type == "remote_script":
        # step1.sh (linux) or step1.ps1 (windows) is already the remote script
        ext = "sh" if tool == "y2b" else "ps1"
        step_script = scripts_dir / f"step1.{ext}"
        offending_file = step_script
        content = step_script.read_text(encoding="utf-8")
        step_script.write_text(f"# remote step {offender}\n" + content, encoding="utf-8")
        expected_line = 1
        expected_col = 15
    elif input_type == "collect_script":
        col_script = scripts_dir / "custom_collect.sh"
        offending_file = col_script
        col_script.write_text(f"# collect {offender}\necho 1\n", encoding="utf-8")
        content = job_yaml.read_text(encoding="utf-8")
        content = content.replace(
            "- column: status\n  var: STEP_STATUS",
            "- column: status\n  script_path: ./scripts/custom_collect.sh",
        )
        job_yaml.write_text(content, encoding="utf-8")
        expected_line = 1
        expected_col = 11
    elif input_type == "post_run_script":
        pr_script = scripts_dir / "custom_post_run.sh"
        offending_file = pr_script
        pr_script.write_text(f"# post_run {offender}\nexit 0\n", encoding="utf-8")
        content = job_yaml.read_text(encoding="utf-8")
        content += f"\npost_run:\n- name: pr1\n  timeout: 30\n  script_path: ./scripts/custom_post_run.sh\n"
        job_yaml.write_text(content, encoding="utf-8")
        expected_line = 1
        expected_col = 12
    else:
        raise ValueError(f"Unknown input_type: {input_type}")

    # Run tool validate
    res = run_cli(tool, ["validate", str(job_yaml)], cwd=tmp_path)
    assert res.returncode == 1, f"Expected validate to exit 1, got {res.returncode}. Stderr: {res.stderr}"

    expected_marker = f"{offending_file.resolve()}:{expected_line}:{expected_col}: non-ASCII character U+1EC5"
    assert (
        expected_marker in res.stderr
    ), f"Expected marker '{expected_marker}' in stderr, got:\n{res.stderr}"


# ==============================================================================
# TP-10: Cap, count and bad-byte cases
# ==============================================================================


def test_validate_ascii_cap_and_count(tmp_path: Path):
    """TP-10: File with 25 offending lines produces exactly 20 lines plus count line."""
    f = tmp_path / "twenty_five_offenders.txt"
    # 25 lines, each having one non-ASCII character
    lines = [f"line {i:02d} with \u1ec5" for i in range(1, 26)]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")

    errors = scan_non_ascii(f)
    # Total error items should be 20 line reports + 1 count line
    assert len(errors) == 21

    # First 20 items are line:col reports
    for idx, err in enumerate(errors[:20], start=1):
        assert f"{f}:{idx}:14: non-ASCII character U+1EC5" == err

    # 21st item is the count line
    assert errors[-1] == f"{f}: 5 more non-ASCII characters"


def test_validate_ascii_invalid_utf8_bytes(tmp_path: Path):
    """TP-10: Invalid UTF-8 bytes produce invalid UTF-8 report without traceback."""
    f = tmp_path / "bad_bytes.yaml"
    # Write invalid UTF-8 byte sequence \xff\xfe at byte offset 6
    f.write_bytes(b"hello \xff\xfe world\n")

    errors = scan_non_ascii(f)
    assert len(errors) == 1
    assert errors[0] == f"{f}: invalid UTF-8 at byte 6"

    # Also test via CLI validate
    res = run_cli("y2b", ["validate", str(f)], cwd=tmp_path)
    assert res.returncode == 1
    assert f"{f}: invalid UTF-8 at byte 6" in res.stderr
    assert "Traceback" not in res.stderr


def test_validate_ascii_utf8_bom_bytes(tmp_path: Path):
    """TP-10: UTF-8 BOM is reported as non-ASCII character U+FEFF at line 1 column 1."""
    f = tmp_path / "bom.yaml"
    f.write_bytes(b"\xef\xbb\xbfinput: ./list_vm.csv\n")

    errors = scan_non_ascii(f)
    assert len(errors) >= 1
    assert errors[0] == f"{f}:1:1: non-ASCII character U+FEFF"


# ==============================================================================
# TP-11: Validate, compile, and run all exit 1 before Azure work
# ==============================================================================


@pytest.mark.parametrize("tool", ["y2b", "y2p"])
@pytest.mark.parametrize(
    "subcmd, extra_args",
    [
        ("validate", []),
        ("compile", ["-o", "out_payload"]),
        ("run", ["--dry-run", "-y"]),
        ("run", ["--phase", "invoke", "-y"]),
        ("run", ["--phase", "collect", "-y"]),
        ("run", ["--phase", "all", "-y"]),
        ("run", ["--resume", "-y"]),
    ],
)
def test_validate_ascii_commands_exit_1(tmp_path: Path, tool: str, subcmd: str, extra_args: list[str]):
    """TP-11: Bad job causes validate, compile, run (all phases, dry-run, resume) to exit 1 before any Azure work."""
    os_type = "linux" if tool == "y2b" else "windows"
    fleet = make_fleet(tmp_path, n=1, os_type=os_type)
    job_yaml = fleet["job_yaml"]

    # Inject non-ASCII into job.yaml
    content = job_yaml.read_text(encoding="utf-8")
    job_yaml.write_text("# non-ascii \u1ec5\n" + content, encoding="utf-8")

    log_file = tmp_path / "fake_az_calls.log"
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env = {
        "FAKE_AZ_LOG": str(log_file),
        "FAKE_AZ_STATE_DIR": str(state_dir),
    }

    argv = [subcmd, str(job_yaml)] + extra_args
    res = run_cli(tool, argv, cwd=tmp_path, env=env)
    assert res.returncode == 1, f"Expected command {tool} {argv} to exit 1, got {res.returncode}.\nStderr: {res.stderr}\nStdout: {res.stdout}"

    # Fake az log must not exist or be empty (0 Azure calls)
    if log_file.exists():
        calls = [ln for ln in log_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(calls) == 0, f"Expected 0 az calls, found: {calls}"

    # Part files dir must not exist
    parts_dir = tmp_path / ".result.parts"
    assert not parts_dir.exists(), f"Part files dir should not be created before validation failure: {parts_dir}"


# ==============================================================================
# TP-12: Both examples pass
# ==============================================================================


def test_validate_ascii_examples_pass():
    """TP-12: examples/linux/job.yaml and examples/windows/job.yaml pass validation with exit 0."""
    linux_job = REPO_ROOT / "examples" / "linux" / "job.yaml"
    res_lin = run_cli("y2b", ["validate", str(linux_job)], cwd=linux_job.parent)
    assert res_lin.returncode == 0, f"Linux example validate failed (rc={res_lin.returncode}):\n{res_lin.stderr}"

    windows_job = REPO_ROOT / "examples" / "windows" / "job.yaml"
    res_win = run_cli("y2p", ["validate", str(windows_job)], cwd=windows_job.parent)
    assert res_win.returncode == 0, f"Windows example validate failed (rc={res_win.returncode}):\n{res_win.stderr}"
