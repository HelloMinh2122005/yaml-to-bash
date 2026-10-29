"""Container execution test for y2p remote.ps1 payload using pwsh in Linux container."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from pathlib import Path
import pytest

from y2shared.config import Job, Step, RunCommand, Invoke
from y2p.compiler import compile_job, compile_collect_script

CONTAINER_IMAGE = "mcr.microsoft.com/powershell:7.4-ubuntu-22.04"


def test_container_execution_pwsh(tmp_path):
    container_cli = None
    if shutil.which("podman"):
        container_cli = "podman"
    elif shutil.which("docker"):
        container_cli = "docker"

    if not container_cli:
        pytest.skip("Neither podman nor docker is installed")

    # Verify image presence or pull
    check_img = subprocess.run([container_cli, "image", "exists", CONTAINER_IMAGE], capture_output=True)
    if check_img.returncode != 0:
        pull_img = subprocess.run([container_cli, "pull", CONTAINER_IMAGE], capture_output=True)
        if pull_img.returncode != 0:
            pytest.skip(f"Could not pull {CONTAINER_IMAGE}")

    # Build a test job targeting forward-slash paths for container filesystem
    job = Job(
        raw_yaml="",
        yaml_path=tmp_path / "job.yaml",
        yaml_dir=tmp_path,
        input=tmp_path / "list_vm.csv",
        output_csv=tmp_path / "result.csv",
        output_log=tmp_path / "result.log",
        sleep_before_collect=1,
        collect_retry_interval=1,
        collect_retries=1,
        job_hash="test-pwsh-hash-1234",
        invoke=Invoke(
            precheck=[],
            runcommand=RunCommand(
                res_file="/tmp/y2p_container_test/result.res",
                log_file="/tmp/y2p_container_test/result.log",
                steps=[
                    Step(
                        name="step1-values",
                        timeout=10,
                        script=(
                            '$val = "line1 with \'quote\' and `"double`"`nline2 with 日本"\n'
                            'write_result TRICKY_VAL $val\n'
                            'write_result only-one-arg\n'
                            'write_result lower x\n'
                        ),
                    ),
                    Step(
                        name="step2-failing-native",
                        timeout=10,
                        script=(
                            "ls /non_existent_dir_12345 2>$null\n"
                            'write_result NATIVE_STATUS "ok"\n'
                        ),
                    ),
                    Step(
                        name="step3-exit3",
                        timeout=10,
                        script="exit 3",
                    ),
                    Step(
                        name="step4-throw",
                        timeout=10,
                        script='throw "Terminating error in step 4"',
                    ),
                    Step(
                        name="step5-timeout",
                        timeout=2,
                        script="Start-Sleep -Seconds 30",
                    ),
                ],
            ),
        ),
        collect=[],
    )

    compiled_remote, r_errors = compile_job(job, run_id="pwsh-test-run")
    assert not r_errors

    compiled_collect, c_errors = compile_collect_script(job)
    assert not c_errors

    remote_file = tmp_path / "remote.ps1"
    remote_file.write_text(compiled_remote, encoding="utf-8")

    collect_file = tmp_path / "collect.ps1"
    collect_file.write_text(compiled_collect, encoding="utf-8")

    container_name = "pwsh_test_container"
    img_name = "pwsh_test_img"

    cmd_remote = [
        container_cli,
        "run",
        "--name",
        container_name,
        "-v",
        f"{tmp_path}:/work:z",
        CONTAINER_IMAGE,
        "pwsh",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        "/work/remote.ps1",
    ]

    try:
        proc_remote = subprocess.run(cmd_remote, capture_output=True, text=True, timeout=40)
        assert proc_remote.returncode == 0, f"remote.ps1 execution failed: {proc_remote.stderr}"

        # Commit container state so collect.ps1 reads the generated res_file
        subprocess.run([container_cli, "commit", container_name, img_name], check=True, capture_output=True)

        cmd_collect = [
            container_cli,
            "run",
            "--rm",
            "-v",
            f"{tmp_path}:/work:z",
            img_name,
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            "/work/collect.ps1",
        ]
        proc_collect = subprocess.run(cmd_collect, capture_output=True, text=True, timeout=30)
        assert proc_collect.returncode == 0, f"collect.ps1 execution failed: {proc_collect.stderr}"
    finally:
        subprocess.run([container_cli, "rm", "-f", container_name], capture_output=True)
        subprocess.run([container_cli, "rmi", "-f", img_name], capture_output=True)

    stdout = proc_collect.stdout
    assert "===BEGIN_RESULT_RES===" in stdout
    assert "===END_RESULT_RES===" in stdout

    block = stdout.split("===BEGIN_RESULT_RES===")[1].split("===END_RESULT_RES===")[0].strip()
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]

    parsed_vars: dict[str, str] = {}
    for ln in lines:
        if "=" in ln:
            k, v = ln.split("=", 1)
            parsed_vars[k] = base64.b64decode(v).decode("utf-8")

    assert parsed_vars.get("__Y2B_RUN__") == "pwsh-test-run"
    assert parsed_vars.get("__Y2B_DONE__") == "pwsh-test-run"
    assert parsed_vars.get("TRICKY_VAL") == "line1 with 'quote' and \"double\"\nline2 with 日本"
    assert "only-one-arg" not in parsed_vars
    assert "lower" not in parsed_vars
    assert parsed_vars.get("NATIVE_STATUS") == "ok"

    # Step statuses
    assert parsed_vars.get("__Y2B_STEP_1__") == "ok"
    assert parsed_vars.get("__Y2B_STEP_2__") == "ok"
    assert parsed_vars.get("__Y2B_STEP_3__") == "rc=3"
    assert parsed_vars.get("__Y2B_STEP_4__") == "rc=1"
    assert parsed_vars.get("__Y2B_STEP_5__") == "timeout"
