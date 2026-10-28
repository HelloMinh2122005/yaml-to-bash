"""Tests for the remote bash payload compiler (Milestone M1)."""

import base64
import os
import shutil
import subprocess
from pathlib import Path
import pytest

from y2b.config import Job, Step, RunCommand, Invoke, load_job
from y2b.compiler import compile_job


def test_golden_file_matches():
    job_path = Path("examples/hip-compliance/job.yaml")
    job = load_job(job_path)
    compiled, errors = compile_job(job)
    assert not errors

    golden_path = Path("tests/golden/hip_compliance_remote.sh")
    expected = golden_path.read_text(encoding="utf-8")
    assert compiled == expected


def test_bash_n_syntax_check():
    job_path = Path("examples/hip-compliance/job.yaml")
    job = load_job(job_path)
    compiled, errors = compile_job(job)
    assert not errors

    proc = subprocess.run(["bash", "-n"], input=compiled, text=True, capture_output=True)
    assert proc.returncode == 0, f"bash -n failed: {proc.stderr}"


def test_syntax_error_reported():
    job = load_job("examples/hip-compliance/job.yaml")
    # Introduce syntax error in step
    job.invoke.runcommand.steps[0].script = "if [ 1 -eq 1 ]; then echo missing fi"
    compiled, errors = compile_job(job)
    assert errors
    assert any("Syntax error" in e for e in errors)


def test_empty_step_produces_valid_bash():
    job = load_job("examples/hip-compliance/job.yaml")
    job.invoke.runcommand.steps[0].script = "# Only comments\n\n# More comments"
    compiled, errors = compile_job(job)
    assert not errors
    assert "y2b_step_1() {            # check-hip-version\n  # Only comments\n\n  # More comments\n  :\n}" in compiled


@pytest.mark.skipif(shutil.which("podman") is None and shutil.which("docker") is None, reason="No container runtime found")
def test_container_execution_rocky9(tmp_path):
    """
    Run compiled payload inside rockylinux:9 container:
    - fake rpm and dnf
    - step that exit 1s
    - step that hangs (timeout)
    - tricky values containing quotes, dollars, backticks, newlines
    """
    # Create test job with various edge cases
    test_yaml = tmp_path / "test_job.yaml"
    test_yaml.write_text(
        """
input: "./dummy.csv"
output_log: "./result.log"
output_csv: "./result.csv"
actions: "skip"
parallel: 1
invoke:
  runcommand:
    res_file: "/var/tmp/runcommand/result.res"
    log_file: "/var/tmp/runcommand/result.log"
    steps:
      - name: "step-normal"
        var: "NORMAL_VAL"
        script: |
          write_result "simple_value"

      - name: "step-tricky-chars"
        var: "TRICKY_VAL"
        script: |
          TRICKY='line1 with "quotes", $dollars, `backticks`
          line2 with spaces
          line3'
          write_result "$TRICKY"

      - name: "step-crash"
        var: "CRASH_VAL"
        script: |
          echo "failing step"
          exit 1

      - name: "step-timeout"
        var: "TIMEOUT_VAL"
        timeout: 2
        script: |
          sleep 10
          write_result "should_not_reach"

collect:
  - column: "dummy"
    source: "input.dummy"
""",
        encoding="utf-8",
    )

    job = load_job(test_yaml)
    compiled, errors = compile_job(job)
    assert not errors

    script_path = tmp_path / "remote.sh"
    script_path.write_text(compiled, encoding="utf-8")
    script_path.chmod(0o755)

    container_cli = "podman" if shutil.which("podman") else "docker"

    # Run in rockylinux:9
    cmd = [
        container_cli,
        "run",
        "--rm",
        "-v",
        f"{tmp_path}:/work:z",
        "docker.io/library/rockylinux:9",
        "bash",
        "/work/remote.sh",
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"Container execution failed: stderr={proc.stderr}, stdout={proc.stdout}"

    stdout = proc.stdout
    assert "===BEGIN_RESULT_RES===" in stdout
    assert "===END_RESULT_RES===" in stdout

    block = stdout.split("===BEGIN_RESULT_RES===")[1].split("===END_RESULT_RES===")[0].strip()
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]

    parsed_vars = {}
    for ln in lines:
        var_name, b64_val = ln.split("=", 1)
        decoded = base64.b64decode(b64_val).decode("utf-8")
        parsed_vars[var_name] = decoded

    # Assert results
    assert parsed_vars.get("NORMAL_VAL") == "simple_value"
    expected_tricky = 'line1 with "quotes", $dollars, `backticks`\n  line2 with spaces\n  line3'
    assert parsed_vars.get("TRICKY_VAL") == expected_tricky
    assert parsed_vars.get("CRASH_VAL") == "__ERROR__ rc=1"
    assert parsed_vars.get("TIMEOUT_VAL") == "__ERROR__ timeout"
