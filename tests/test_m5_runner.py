"""Tests for Milestone M5: Runner pool, concurrency, resume, and signals."""

import argparse
import csv
import os
import shutil
import sys
from pathlib import Path
import pytest

from y2shared.config import load_job
from y2shared.merger import merge_parts
from y2shared.parts import get_parts_dir, is_part_finished
from y2shared.runner import run_job

FAKE_AZ_DIR = Path(__file__).parent / "fake_az"


@pytest.fixture(autouse=True)
def setup_runner_env(monkeypatch, tmp_path):
    old_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", f"{FAKE_AZ_DIR}:{old_path}")
    monkeypatch.setenv("FAKE_AZ_RC_DEFAULT", "success")
    log_file = tmp_path / "az_calls.log"
    monkeypatch.setenv("FAKE_AZ_LOG", str(log_file))
    return log_file


def create_job_and_csv(tmp_path: Path, num_vms: int = 10, prefix: str = "vm") -> Path:
    csv_file = tmp_path / "list_vm.csv"
    with open(csv_file, "w", encoding="utf-8") as f:
        f.write("vm_name,resource_group,subscription,os\n")
        for i in range(1, num_vms + 1):
            f.write(f"{prefix}-{i:04d},rg-prod,sub-01,rhel8\n")

    yaml_file = tmp_path / "job.yaml"
    yaml_file.write_text(
        f"""
input: "./list_vm.csv"
output_log: "./result.log"
output_csv: "./result.csv"
parallel: 10
sleep_before_collect: 0
invoke:
  precheck:
    - name: "check-vm-running"
      script: "echo 'PowerState/running'"
      result:
        "PowerState/running":
          log: "running"
          action: "continue"
  runcommand:
    res_file: "/var/tmp/result.res"
    log_file: "/var/tmp/result.log"
    steps:
      - name: "get-hip"
        script: "write_result HIP_VERSION '22102026'"
      - name: "get-kernel"
        script: "write_result KERNEL_COUNT '3'"
collect:
  - column: "vm_name"
    source: "input.vm_name"
  - column: "subscription"
    source: "input.subscription"
  - column: "hip_version"
    var: "HIP_VERSION"
  - column: "kernel_count"
    var: "KERNEL_COUNT"
  - column: "recommendation"
    script: |
      if [ "$HIP_VERSION" = "22102026" ]; then
        echo "Compliant"
      else
        echo "NonCompliant"
      fi
""",
        encoding="utf-8",
    )
    return yaml_file


def test_dry_run_mode(tmp_path):
    yaml_path = create_job_and_csv(tmp_path, num_vms=3)
    args = argparse.Namespace(
        job_yaml=str(yaml_path),
        vm=[],
        limit=None,
        parallel=2,
        dry_run=True,
        resume=False,
        keep_parts=False,
        yes=True,
    )
    rc = run_job(args)
    # In dry-run, if prechecks pass, it succeeds with exit code 0
    assert rc == 0

    job = load_job(yaml_path)
    with open(job.output_csv, "r", encoding="utf-8") as f:
        reader = list(csv.reader(f))

    assert len(reader) == 4  # Header + 3 rows
    assert reader[1][0] == "vm-0001"
    assert reader[1][2] == "DRY_RUN:ok"


def test_vm_filter_and_limit(tmp_path):
    yaml_path = create_job_and_csv(tmp_path, num_vms=10)
    args = argparse.Namespace(
        job_yaml=str(yaml_path),
        vm=["vm-0002", "vm-0005"],
        limit=1,
        parallel=1,
        dry_run=False,
        resume=False,
        keep_parts=False,
        yes=True,
    )
    rc = run_job(args)
    assert rc == 0

    job = load_job(yaml_path)
    with open(job.output_csv, "r", encoding="utf-8") as f:
        reader = list(csv.reader(f))

    # vm-0002 was first matching, limited to 1
    assert len(reader) == 2  # Header + 1 row
    assert reader[1][0] == "vm-0002"
    assert reader[1][2] == "22102026"
    assert reader[1][4] == "Compliant"


def test_exit_codes(tmp_path, monkeypatch):
    # Test exit code 2 when some VMs end with SKIPPED
    yaml_path = create_job_and_csv(tmp_path, num_vms=2)
    # Cause vm-0002 to fail precheck
    monkeypatch.setenv("FAKE_AZ_STATE_vm-0002", "PowerState/deallocated")
    job = load_job(yaml_path)
    # Modify precheck to skip on deallocated
    job.invoke.precheck[0].script = """
if [ "$VM_NAME" = "vm-0002" ]; then
  echo "PowerState/deallocated"
else
  echo "PowerState/running"
fi
"""
    job.invoke.precheck[0].result["PowerState/deallocated"] = {"log": "stopped", "action": "skip"}
    # Re-save YAML
    import yaml
    raw = job.raw_yaml
    raw["invoke"]["precheck"][0]["script"] = job.invoke.precheck[0].script
    raw["invoke"]["precheck"][0]["result"]["PowerState/deallocated"] = {"log": "stopped", "action": "skip"}
    with open(yaml_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f)

    args = argparse.Namespace(
        job_yaml=str(yaml_path),
        vm=[],
        limit=None,
        parallel=2,
        dry_run=False,
        resume=False,
        keep_parts=False,
        yes=True,
    )
    rc = run_job(args)
    assert rc == 2


def test_concurrency_500_vms_parallel_50_vs_1(tmp_path):
    """
    Test 500 fake VMs at parallel 50 vs parallel 1.
    Output must have 500 rows in exact input order, and be identical!
    """
    dir_p1 = tmp_path / "run_p1"
    dir_p1.mkdir()
    yaml_p1 = create_job_and_csv(dir_p1, num_vms=500, prefix="fleet")

    args_p1 = argparse.Namespace(
        job_yaml=str(yaml_p1),
        vm=[],
        limit=None,
        parallel=1,
        dry_run=False,
        resume=False,
        keep_parts=False,
        yes=True,
    )
    rc1 = run_job(args_p1)
    assert rc1 == 0

    dir_p50 = tmp_path / "run_p50"
    dir_p50.mkdir()
    yaml_p50 = create_job_and_csv(dir_p50, num_vms=500, prefix="fleet")

    args_p50 = argparse.Namespace(
        job_yaml=str(yaml_p50),
        vm=[],
        limit=None,
        parallel=50,
        dry_run=False,
        resume=False,
        keep_parts=False,
        yes=True,
    )
    rc50 = run_job(args_p50)
    assert rc50 == 0

    csv_p1 = (dir_p1 / "result.csv").read_text(encoding="utf-8")
    csv_p50 = (dir_p50 / "result.csv").read_text(encoding="utf-8")

    assert csv_p1 == csv_p50
    lines = csv_p1.strip().splitlines()
    assert len(lines) == 501  # Header + 500 rows
    assert lines[1].startswith("fleet-0001")
    assert lines[500].startswith("fleet-0500")


def test_resume_after_partial_run(tmp_path, monkeypatch):
    """
    Simulate an interrupted run where parts folder is kept, then resumed.
    The finished parts should not be re-run, and merged result matches uninterrupted run.
    """
    run_dir = tmp_path / "resume_test"
    run_dir.mkdir()
    yaml_path = create_job_and_csv(run_dir, num_vms=10, prefix="resumable")

    # Step 1: Run with limit=5 and keep_parts=True
    args_partial = argparse.Namespace(
        job_yaml=str(yaml_path),
        vm=[],
        limit=5,
        parallel=5,
        dry_run=False,
        resume=False,
        keep_parts=True,
        yes=True,
    )
    rc1 = run_job(args_partial)
    assert rc1 == 0

    # Verify that first 5 parts exist
    job = load_job(yaml_path)
    parts_dir = get_parts_dir(job.output_csv)
    assert parts_dir.exists()

    # Step 2: Now run full 10 VMs with --resume
    log_file = tmp_path / "resume_az.log"
    monkeypatch.setenv("FAKE_AZ_LOG", str(log_file))

    args_full = argparse.Namespace(
        job_yaml=str(yaml_path),
        vm=[],
        limit=None,
        parallel=5,
        dry_run=False,
        resume=True,
        keep_parts=False,
        yes=True,
    )
    rc2 = run_job(args_full)
    assert rc2 == 0

    # In log_file, only VMs 6-10 should have been executed!
    calls = log_file.read_text(encoding="utf-8").strip().splitlines()
    # Check that resumable-0001 through 0005 were NOT in calls
    for call in calls:
        for i in range(1, 6):
            assert f"resumable-{i:04d}" not in call

    # The final merged result.csv must have all 10 VMs in order
    with open(job.output_csv, "r", encoding="utf-8") as f:
        reader = list(csv.reader(f))

    assert len(reader) == 11  # Header + 10 data rows
    for i in range(1, 11):
        assert reader[i][0] == f"resumable-{i:04d}"
        assert reader[i][2] == "22102026"
        assert reader[i][4] == "Compliant"
