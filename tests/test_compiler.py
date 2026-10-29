"""Tests for the remote bash payload compiler (Milestone M1)."""

import base64
import os
import shutil
import subprocess
from pathlib import Path
import pytest

from y2shared.config import Job, Step, RunCommand, Invoke, load_job
from y2b.compiler import compile_job, compile_collect_script


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


def test_compiler_run_id_and_no_result_echo():
    job = load_job("examples/hip-compliance/job.yaml")
    compiled, errors = compile_job(job, run_id="20261029T100000Z-1234abcd")
    assert not errors
    assert "Y2B_RUN_ID=20261029T100000Z-1234abcd" in compiled
    assert "===BEGIN_RESULT_RES===" not in compiled
    assert "===END_RESULT_RES===" not in compiled
    assert "__Y2B_RUN__" in compiled
    assert "__Y2B_DONE__" in compiled

    # Test collect script compiler
    collect_script, c_errors = compile_collect_script(job)
    assert not c_errors
    assert "===BEGIN_RESULT_RES===" in collect_script
    assert "===END_RESULT_RES===" in collect_script
    assert "RES_FILE=/var/tmp/runcommand/result.res" in collect_script
    assert 'cat "$RES_FILE" 2>/dev/null' in collect_script


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
sleep_before_collect: 0
invoke:
  runcommand:
    res_file: "/var/tmp/runcommand/result.res"
    log_file: "/var/tmp/runcommand/result.log"
    steps:
      - name: "step-normal"
        script: |
          write_result NORMAL_VAL "simple_value"
          write_result "only-one-arg" || true
          write_result lower_case "ignored" || true
          write_result __Y2B_RESERVED "ignored" || true

      - name: "step-tricky-chars"
        script: |
          TRICKY='line1 with "quotes", $dollars, `backticks`
          line2 with spaces
          line3'
          write_result TRICKY_VAL "$TRICKY"

      - name: "step-crash"
        script: |
          echo "failing step"
          exit 1
          write_result CRASH_VAL "unreachable"

      - name: "step-timeout"
        timeout: 2
        script: |
          sleep 10
          write_result TIMEOUT_VAL "should_not_reach"

collect:
  - column: "dummy"
    source: "input.dummy"
  - column: "normal"
    var: "NORMAL_VAL"
  - column: "tricky"
    var: "TRICKY_VAL"
  - column: "crash"
    var: "CRASH_VAL"
  - column: "timeout"
    var: "TIMEOUT_VAL"
""",
        encoding="utf-8",
    )

    job = load_job(test_yaml)
    compiled, errors = compile_job(job, run_id="rocky9-test-run")
    assert not errors

    script_path = tmp_path / "remote.sh"
    script_path.write_text(compiled, encoding="utf-8")
    script_path.chmod(0o755)

    collect_script, c_errors = compile_collect_script(job)
    assert not c_errors
    collect_path = tmp_path / "collect.sh"
    collect_path.write_text(collect_script, encoding="utf-8")
    collect_path.chmod(0o755)

    container_cli = "podman" if shutil.which("podman") else "docker"

    # Run remote.sh in rockylinux:9
    cmd_remote = [
        container_cli,
        "run",
        "--name",
        "rocky9_test_container",
        "-v",
        f"{tmp_path}:/work:z",
        "docker.io/library/rockylinux:9",
        "bash",
        "/work/remote.sh",
    ]

    try:
        proc_remote = subprocess.run(cmd_remote, capture_output=True, text=True, timeout=30)
        assert proc_remote.returncode == 0, f"Remote execution failed: stderr={proc_remote.stderr}, stdout={proc_remote.stdout}"

        # Now run collect.sh in the same container state (using container commit or exec, or keep container running)
        # Since files in /var/tmp/runcommand/ are in the container filesystem, commit or exec:
        cmd_commit = [container_cli, "commit", "rocky9_test_container", "rocky9_test_img"]
        subprocess.run(cmd_commit, check=True)

        cmd_collect = [
            container_cli,
            "run",
            "--rm",
            "-v",
            f"{tmp_path}:/work:z",
            "rocky9_test_img",
            "bash",
            "/work/collect.sh",
        ]
        proc = subprocess.run(cmd_collect, capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, f"Collect execution failed: stderr={proc.stderr}, stdout={proc.stdout}"
    finally:
        subprocess.run([container_cli, "rm", "-f", "rocky9_test_container"], capture_output=True)
        subprocess.run([container_cli, "rmi", "-f", "rocky9_test_img"], capture_output=True)

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

    # Assert results including RUN/DONE markers
    assert parsed_vars.get("__Y2B_RUN__") == "rocky9-test-run"
    assert parsed_vars.get("__Y2B_DONE__") == "rocky9-test-run"
    assert parsed_vars.get("NORMAL_VAL") == "simple_value"
    expected_tricky = 'line1 with "quotes", $dollars, `backticks`\n  line2 with spaces\n  line3'
    assert parsed_vars.get("TRICKY_VAL") == expected_tricky
    assert "CRASH_VAL" not in parsed_vars
    assert "TIMEOUT_VAL" not in parsed_vars
    assert "only-one-arg" not in parsed_vars
    assert "lower_case" not in parsed_vars
    assert "__Y2B_RESERVED" not in parsed_vars
    assert parsed_vars.get("__Y2B_STEP_1__") == "ok"
    assert parsed_vars.get("__Y2B_STEP_2__") == "ok"
    assert parsed_vars.get("__Y2B_STEP_3__") == "rc=1"
    assert parsed_vars.get("__Y2B_STEP_4__") == "timeout"

    # Test collector resolution on this result
    from y2shared.collector import collect_for_vm
    from y2shared.dispatcher import ReadResult
    from y2shared.inputcsv import VMRow
    dummy_row = VMRow(index=1, vm_name="test-vm", resource_group="rg", subscription="sub", os="linux", env={}, raw_data={"dummy": "val"})
    vals = collect_for_vm(job, dummy_row, remote_result=ReadResult(status="OK", vars=parsed_vars))
    assert vals[0] == "val"
    assert vals[1] == "simple_value"
    assert vals[2] == expected_tricky
    assert vals[3] == "__ERROR__ rc=1"
    assert vals[4] == "__ERROR__ timeout"
