from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
import pytest

from labenv import LabEnv


@pytest.fixture
def lab_cfg(tmp_path: Path, stub_az, repo_root: Path):
    bin_dir, calls_file = stub_az
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    sp_file = config_dir / "sp.env"
    sp_file.write_text(
        'AZURE_CLIENT_ID="sp-client-id-12345"\n'
        'AZURE_TENANT_ID="sp-tenant-id-67890"\n'
        'AZURE_CLIENT_SECRET="secret-shh-do-not-leak-9999"\n'
    )

    env = dict(os.environ)
    env["Y2_REAL_AZ"] = str(bin_dir / "az")
    env["Y2_LAB_CONFIG_DIR"] = str(config_dir)
    env["Y2_LAB_ENV"] = str(repo_root / "lab" / "lab.env")
    # Provide stub stdout for az vm list so toogle_vm.sh finds a VM to stop
    vm_id = "/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/lin01"
    env["STUB_AZ_STDOUT"] = vm_id

    return {
        "config_dir": config_dir,
        "calls_file": calls_file,
        "env": env,
        "secret": "secret-shh-do-not-leak-9999",
    }


def test_success_deallocates(lab_cfg, repo_root: Path, tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    res = subprocess.run(
        [
            "bash",
            str(repo_root / "lab" / "run-lab.sh"),
            "--evidence",
            str(evidence_dir),
            "--",
            "true",
        ],
        capture_output=True,
        text=True,
        env=lab_cfg["env"],
    )
    assert res.returncode == 0
    calls = lab_cfg["calls_file"].read_text()
    assert "login --service-principal" in calls
    assert "account set" in calls
    assert "vm deallocate" in calls
    assert (evidence_dir / "post-state.txt").exists()
    assert not (lab_cfg["config_dir"] / "run.lock").exists()


def test_failure_still_deallocates(lab_cfg, repo_root: Path, tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    res = subprocess.run(
        [
            "bash",
            str(repo_root / "lab" / "run-lab.sh"),
            "--evidence",
            str(evidence_dir),
            "--",
            "false",
        ],
        capture_output=True,
        text=True,
        env=lab_cfg["env"],
    )
    assert res.returncode == 1
    calls = lab_cfg["calls_file"].read_text()
    assert "vm deallocate" in calls
    assert (evidence_dir / "post-state.txt").exists()


def test_sigint_deallocates(lab_cfg, repo_root: Path, tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    proc = subprocess.Popen(
        [
            "bash",
            str(repo_root / "lab" / "run-lab.sh"),
            "--evidence",
            str(evidence_dir),
            "--",
            "sleep",
            "30",
        ],
        env=lab_cfg["env"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Wait until pre-state.txt exists, meaning trap is active and command started
    pre_state = evidence_dir / "pre-state.txt"
    for _ in range(50):
        if pre_state.exists():
            break
        time.sleep(0.1)

    proc.send_signal(signal.SIGINT)
    proc.wait(timeout=10)
    assert proc.returncode != 0
    calls = lab_cfg["calls_file"].read_text()
    assert "vm deallocate" in calls


def test_budget_exceeded(lab_cfg, repo_root: Path, tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    env = dict(lab_cfg["env"])
    env["Y2_LAB_BUDGET_SECONDS"] = "1"
    res = subprocess.run(
        [
            "bash",
            str(repo_root / "lab" / "run-lab.sh"),
            "--evidence",
            str(evidence_dir),
            "--",
            "sleep",
            "10",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 124
    calls = lab_cfg["calls_file"].read_text()
    assert "vm deallocate" in calls


def test_missing_sp_env(lab_cfg, repo_root: Path, tmp_path: Path):
    (lab_cfg["config_dir"] / "sp.env").unlink()
    res = subprocess.run(
        [
            "bash",
            str(repo_root / "lab" / "run-lab.sh"),
            "--",
            "true",
        ],
        capture_output=True,
        text=True,
        env=lab_cfg["env"],
    )
    assert res.returncode == 78
    calls = lab_cfg["calls_file"].read_text()
    assert "vm " not in calls


def test_non_lab_csv_refused(lab_cfg, repo_root: Path, tmp_path: Path):
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text(
        "vm_name,resource_group,subscription,os\n"
        "lin01,rg-tfstate,79f7da80-0c64-4e4b-9ed4-316a88142152,linux\n"
    )
    marker = tmp_path / "marker.txt"
    res = subprocess.run(
        [
            "bash",
            str(repo_root / "lab" / "run-lab.sh"),
            "--csv",
            str(bad_csv),
            "--",
            "touch",
            str(marker),
        ],
        capture_output=True,
        text=True,
        env=lab_cfg["env"],
    )
    assert res.returncode == 65
    assert not marker.exists()
    calls = lab_cfg["calls_file"].read_text()
    assert "vm deallocate" not in calls


def test_live_lock_busy(lab_cfg, repo_root: Path):
    lock_dir = lab_cfg["config_dir"] / "run.lock"
    lock_dir.mkdir(parents=True, exist_ok=True)
    (lock_dir / "pid").write_text(str(os.getpid()))

    res = subprocess.run(
        [
            "bash",
            str(repo_root / "lab" / "run-lab.sh"),
            "--",
            "true",
        ],
        capture_output=True,
        text=True,
        env=lab_cfg["env"],
    )
    assert res.returncode == 75


def test_stale_lock_reclaimed(lab_cfg, repo_root: Path, tmp_path: Path):
    lock_dir = lab_cfg["config_dir"] / "run.lock"
    lock_dir.mkdir(parents=True, exist_ok=True)
    (lock_dir / "pid").write_text("999999")  # Non-existent PID

    evidence_dir = tmp_path / "evidence"
    res = subprocess.run(
        [
            "bash",
            str(repo_root / "lab" / "run-lab.sh"),
            "--evidence",
            str(evidence_dir),
            "--",
            "true",
        ],
        capture_output=True,
        text=True,
        env=lab_cfg["env"],
    )
    assert res.returncode == 0


def test_command_sees_shim(lab_cfg, repo_root: Path, tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    res = subprocess.run(
        [
            "bash",
            str(repo_root / "lab" / "run-lab.sh"),
            "--evidence",
            str(evidence_dir),
            "--",
            "az",
            "group",
            "list",
        ],
        capture_output=True,
        text=True,
        env=lab_cfg["env"],
    )
    assert res.returncode == 3
    calls = lab_cfg["calls_file"].read_text()
    assert "group list" not in calls


def test_secret_not_in_evidence(lab_cfg, repo_root: Path, tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    res = subprocess.run(
        [
            "bash",
            str(repo_root / "lab" / "run-lab.sh"),
            "--evidence",
            str(evidence_dir),
            "--",
            "true",
        ],
        capture_output=True,
        text=True,
        env=lab_cfg["env"],
    )
    assert res.returncode == 0
    secret = lab_cfg["secret"]
    for path in evidence_dir.glob("*"):
        if path.is_file():
            content = path.read_text(errors="ignore")
            assert secret not in content, f"Secret leaked in {path.name}"
