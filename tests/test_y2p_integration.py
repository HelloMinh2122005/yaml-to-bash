"""Integration tests for y2p (PowerShell frontend): 20 VMs, fake_az, busy retry, resume, auth breaker."""

from __future__ import annotations

import csv
import os
import subprocess
import sys
from pathlib import Path
import pytest
import yaml

from y2shared import cli
from y2b.profile import PROFILE as Y2B_PROFILE
from y2p.profile import PROFILE as Y2P_PROFILE

FAKE_AZ_DIR = Path(__file__).parent / "fake_az"


@pytest.fixture(autouse=True)
def setup_fake_az_env(monkeypatch, tmp_path):
    old_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", f"{FAKE_AZ_DIR}:{old_path}")
    log_file = tmp_path / "az_calls.log"
    monkeypatch.setenv("FAKE_AZ_LOG", str(log_file))
    state_dir = tmp_path / "fake_az_state"
    monkeypatch.setenv("FAKE_AZ_STATE_DIR", str(state_dir))
    return log_file


def create_windows_job(tmp_path: Path, num_vms: int = 20) -> Path:
    job_dir = tmp_path / "job_win"
    job_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir = job_dir / "scripts"
    scripts_dir.mkdir(exist_ok=True)

    tls_script = scripts_dir / "check_tls.ps1"
    tls_script.write_text("write_result TLS12_ENABLED '1'\n", encoding="utf-8")

    csv_path = job_dir / "list_vm.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["vm_name", "resource_group", "subscription", "os"])
        for i in range(1, num_vms + 1):
            writer.writerow([f"win-vm-{i:02d}", "rg-win-prod", "sub-12345", "windows2022"])

    job_content = f"""
input: "./list_vm.csv"
output_log: "./result.log"
output_csv: "./result.csv"
actions: "skip"
parallel: 5

sleep_before_collect: "1s"
collect_retry_interval: "1s"
collect_retries: 2

invoke:
  precheck:
    - name: "check-sub"
      script: |
        echo 0
      result:
        0:
          log: "OK"
          action: "continue"

  runcommand:
    res_file: 'C:\\ProgramData\\y2p\\result.res'
    log_file: 'C:\\ProgramData\\y2p\\result.log'
    steps:
      - name: "check-defender"
        timeout: 60
        script: |
          write_result DEFENDER_ENABLED "True"
          write_result SIGNATURE_AGE_DAYS "0"
      - name: "tls-settings"
        timeout: 60
        script_path: "./scripts/check_tls.ps1"

collect:
  - column: "vm_name"
    source: "input.vm_name"
  - column: "defender_enabled"
    var: "DEFENDER_ENABLED"
  - column: "signature_age"
    var: "SIGNATURE_AGE_DAYS"
  - column: "tls12_enabled"
    var: "TLS12_ENABLED"
  - column: "verdict"
    script: |
      if [ "$DEFENDER_ENABLED" = "True" ]; then
        echo "OK"
      else
        echo "FIX"
      fi
"""
    job_file = job_dir / "job.yaml"
    job_file.write_text(job_content, encoding="utf-8")
    return job_file


def test_y2p_run_20_windows_vms(tmp_path, setup_fake_az_env, monkeypatch):
    log_file = setup_fake_az_env
    monkeypatch.setenv("FAKE_AZ_EXPECT_CMD", "RunPowerShellScript")

    job_file = create_windows_job(tmp_path, num_vms=20)
    rc = cli.main(Y2P_PROFILE, argv=["run", str(job_file), "--parallel", "5", "-y"])
    assert rc == 0

    # Verify az log calls
    calls = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(calls) > 0
    for call in calls:
        if "run-command" in call:
            assert "--command-id RunPowerShellScript" in call
            assert "@" in call
            assert ("remote.ps1" in call or "collect.ps1" in call)

    # Verify output CSV
    out_csv = job_file.parent / "result.csv"
    assert out_csv.exists()
    with open(out_csv, "r", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
    assert len(reader) == 20
    for row in reader:
        assert row["defender_enabled"] == "True"
        assert row["signature_age"] == "0"
        assert row["tls12_enabled"] == "1"
        assert row["verdict"] == "OK"


def test_y2p_busy_retry_and_resume(tmp_path, setup_fake_az_env, monkeypatch):
    monkeypatch.setenv("FAKE_AZ_EXPECT_CMD", "RunPowerShellScript")
    job_file = create_windows_job(tmp_path, num_vms=2)

    # Set VM 1 to be perpetually busy
    monkeypatch.setenv("FAKE_AZ_BUSY_COUNT_win-vm-01", "99")

    rc = cli.main(Y2P_PROFILE, argv=["run", str(job_file), "-y"])
    # Returncode is 2 because VM 1 ended with ERROR:still_running
    assert rc == 2

    parts_dir = job_file.parent / ".result.parts"
    assert parts_dir.exists()

    # Clear busy count for VM 1 so resume will succeed
    monkeypatch.delenv("FAKE_AZ_BUSY_COUNT_win-vm-01")

    # Resume run
    rc_resume = cli.main(Y2P_PROFILE, argv=["run", str(job_file), "--resume", "-y"])
    assert rc_resume == 0


def test_y2p_auth_breaker_exit_3(tmp_path, setup_fake_az_env, monkeypatch):
    monkeypatch.setenv("FAKE_AZ_EXPECT_CMD", "RunPowerShellScript")
    job_file = create_windows_job(tmp_path, num_vms=5)

    # Set auth failure default
    monkeypatch.setenv("FAKE_AZ_RC_DEFAULT", "auth")

    rc = cli.main(Y2P_PROFILE, argv=["run", str(job_file), "-y"])
    assert rc == 3


def test_command_id_isolation(tmp_path, setup_fake_az_env, monkeypatch):
    # Running y2b expecting RunShellScript -> succeeds
    job_b = tmp_path / "job_b.yaml"
    job_b.write_text("""
input: "./list_vm.csv"
output_log: "./result.log"
output_csv: "./result.csv"
sleep_before_collect: "1s"
invoke:
  runcommand:
    res_file: "/var/tmp/result.res"
    log_file: "/var/tmp/result.log"
    steps:
      - name: "step1"
        script: "write_result A 1"
collect:
  - column: "vm_name"
    source: "input.vm_name"
""", encoding="utf-8")
    csv_b = tmp_path / "list_vm.csv"
    csv_b.write_text("vm_name,resource_group,subscription,os\nvm-b,rg,sub,rhel8\n", encoding="utf-8")

    monkeypatch.setenv("FAKE_AZ_EXPECT_CMD", "RunShellScript")
    rc_b = cli.main(Y2B_PROFILE, argv=["run", str(job_b), "-y"])
    assert rc_b == 0

    # Running y2p expecting RunPowerShellScript -> succeeds
    job_p = create_windows_job(tmp_path / "p_dir", num_vms=1)
    monkeypatch.setenv("FAKE_AZ_EXPECT_CMD", "RunPowerShellScript")
    rc_p = cli.main(Y2P_PROFILE, argv=["run", str(job_p), "-y"])
    assert rc_p == 0

    # If y2p runs with FAKE_AZ_EXPECT_CMD=RunShellScript -> az call fails loudly
    monkeypatch.setenv("FAKE_AZ_EXPECT_CMD", "RunShellScript")
    rc_mismatch = cli.main(Y2P_PROFILE, argv=["run", str(job_p), "-y"])
    assert rc_mismatch != 0


def test_bin_y2p_runs_from_outside_repo(tmp_path):
    repo_root = Path(__file__).resolve().parent.parent
    bin_y2p = repo_root / "bin" / "y2p"

    proc = subprocess.run(
        [str(bin_y2p), "--help"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "yaml_to_powershell" in proc.stdout

