"""Integration tests for Fire-and-forget execution (Milestones M0-M4, Requirements R1-R4)."""

import argparse
import csv
import json
import os
import shutil
import time
from pathlib import Path
import pytest

from y2shared.collectlist import CollectList
from y2shared.config import load_job
from y2shared.parts import get_parts_dir, is_part_finished, is_part_still_running, get_run_info
from y2shared.runner import run_job

FAKE_AZ_DIR = Path(__file__).parent / "fake_az"


@pytest.fixture(autouse=True)
def setup_fire_and_forget_env(monkeypatch, tmp_path):
    old_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", f"{FAKE_AZ_DIR}:{old_path}")
    log_file = tmp_path / "az_calls.log"
    monkeypatch.setenv("FAKE_AZ_LOG", str(log_file))
    state_dir = tmp_path / "fake_az_state"
    monkeypatch.setenv("FAKE_AZ_STATE_DIR", str(state_dir))
    return log_file


def create_job_and_csv(
    tmp_path: Path,
    num_vms: int = 10,
    prefix: str = "vm",
    sleep_before_collect: str = "0",
    collect_retry_interval: str = "1s",
    collect_retries: int = 3,
) -> Path:
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
parallel: 5
sleep_before_collect: "{sleep_before_collect}"
collect_retry_interval: "{collect_retry_interval}"
collect_retries: {collect_retries}

invoke:
  precheck:
    - name: "check-vm-running"
      script: "echo 'PowerState/running'"
      result:
        "PowerState/running":
          log: "running"
          action: "continue"
        "PowerState/deallocated":
          log: "deallocated"
          action: "skip"
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
""",
        encoding="utf-8",
    )
    return yaml_file


# ==============================================================================
# R1: Fire and forget (fire all first, all runCommand POSTs before first reader)
# ==============================================================================

def test_r1_fire_and_forget_ordering(tmp_path, monkeypatch, setup_fire_and_forget_env):
    log_file = setup_fire_and_forget_env
    monkeypatch.setenv("FAKE_AZ_RUNTIME", "0.2")

    yaml_path = create_job_and_csv(
        tmp_path,
        num_vms=20,
        prefix="fleet",
        sleep_before_collect="0.1s",
    )

    args = argparse.Namespace(
        job_yaml=str(yaml_path),
        vm=[],
        limit=None,
        parallel=5,
        dry_run=False,
        resume=False,
        keep_parts=False,
        yes=True,
    )
    start_t = time.time()
    rc = run_job(args)
    duration = time.time() - start_t
    assert rc == 0

    # With 20 VMs and 5 workers: sequential would be 20/5 * 0.2s = 0.8s + session waits.
    # Check execution order in az log
    calls = log_file.read_text(encoding="utf-8").strip().splitlines()

    nowait_indices = [i for i, c in enumerate(calls) if c.startswith("rest ")]
    reader_indices = [i for i, c in enumerate(calls) if "-o json" in c and "run-command" in c]

    assert len(nowait_indices) == 20
    assert len(reader_indices) >= 20

    # Every runCommand POST must happen before the first reader call!
    assert max(nowait_indices) < min(reader_indices)


# ==============================================================================
# R2 & R3: Skipped / not-found VMs never collected; Collect list matches 1:1
# ==============================================================================

def test_r2_and_r3_skipped_and_not_found(tmp_path, monkeypatch, setup_fire_and_forget_env):
    log_file = setup_fire_and_forget_env

    # 3 VMs:
    # vm-0001: OK
    # vm-0002: skipped in precheck (deallocated)
    # vm-0003: not found in invoke
    yaml_path = create_job_and_csv(tmp_path, num_vms=3, prefix="mix")
    monkeypatch.setenv("FAKE_AZ_RC_mix-0003", "missing")

    # Counter file to verify prechecks run exactly once
    counter_file = tmp_path / "precheck_counter.txt"
    job = load_job(yaml_path)
    job.invoke.precheck[0].script = f"""
echo "$VM_NAME" >> "{counter_file}"
if [ "$VM_NAME" = "mix-0002" ]; then
  echo "PowerState/deallocated"
else
  echo "PowerState/running"
fi
"""
    import yaml
    raw = job.raw_yaml
    raw["invoke"]["precheck"][0]["script"] = job.invoke.precheck[0].script
    with open(yaml_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f)

    args = argparse.Namespace(
        job_yaml=str(yaml_path),
        vm=[],
        limit=None,
        parallel=3,
        dry_run=False,
        resume=False,
        keep_parts=True,  # keep parts to inspect collect_list
        yes=True,
    )
    rc = run_job(args)
    assert rc == 2  # Has skipped and error VMs

    # 1. Prechecks ran exactly once per VM
    precheck_counts = counter_file.read_text().strip().splitlines()
    assert sorted(precheck_counts) == ["mix-0001", "mix-0002", "mix-0003"]

    # 2. Inspect collect list
    parts_dir = get_parts_dir(job.output_csv)
    cl = CollectList(parts_dir)
    loaded = cl.load()

    # R3: After phase 1, collect list holds EXACTLY the fired VMs!
    # mix-0001 was fired.
    # mix-0002 was stopped by precheck (never on collect list).
    # mix-0003 failed invoke with missing (excluded from loaded collect list).
    assert list(loaded.keys()) == [1]
    assert loaded[1]["vm_name"] == "mix-0001"

    # R2: Reader calls in az log match collect list 1:1 (zero reader calls for skipped / not found)
    calls = log_file.read_text(encoding="utf-8").strip().splitlines()
    reader_calls = [c for c in calls if "-o json" in c and "run-command" in c]
    assert len(reader_calls) == 1
    assert "mix-0001" in reader_calls[0]
    assert not any("mix-0002" in c for c in reader_calls)
    assert not any("mix-0003" in c for c in reader_calls)

    # 3. CSV results check
    with open(job.output_csv, "r", encoding="utf-8") as f:
        rows = list(csv.reader(f))

    assert rows[1][0] == "mix-0001" and rows[1][2] == "22102026"
    assert rows[2][0] == "mix-0002" and "SKIPPED:" in rows[2][2]
    assert rows[3][0] == "mix-0003" and rows[3][2] == "ERROR:vm_not_found"


# ==============================================================================
# R4: Timing & Busy Retries
# ==============================================================================

def test_r4_timing_busy_retries(tmp_path, monkeypatch, setup_fire_and_forget_env):
    log_file = setup_fire_and_forget_env
    # Simulate VM busy for first 2 reader calls, then succeeds on 3rd call
    monkeypatch.setenv("FAKE_AZ_BUSY_COUNT_timing-0001", "2")

    yaml_path = create_job_and_csv(
        tmp_path,
        num_vms=1,
        prefix="timing",
        sleep_before_collect="0.1s",
        collect_retry_interval="0.1s",
        collect_retries=3,
    )

    args = argparse.Namespace(
        job_yaml=str(yaml_path),
        vm=[],
        limit=None,
        parallel=1,
        dry_run=False,
        resume=False,
        keep_parts=False,
        yes=True,
    )
    rc = run_job(args)
    assert rc == 0

    # Inspect reader calls: 1 initial attempt + 2 busy retries = 3 reader calls total
    calls = log_file.read_text(encoding="utf-8").strip().splitlines()
    reader_calls = [c for c in calls if "-o json" in c and "run-command" in c]
    assert len(reader_calls) == 3


# ==============================================================================
# Resume after Phase 1 and Phase 2 Interruption
# ==============================================================================

def test_resume_phase1_interruption(tmp_path, monkeypatch, setup_fire_and_forget_env):
    yaml_path = create_job_and_csv(tmp_path, num_vms=2, prefix="res-p1")
    job = load_job(yaml_path)
    parts_dir = get_parts_dir(job.output_csv)

    # Simulate an interrupted Phase 1 run:
    # VM 1 was fired and has confirmed=False in collect_list (crash right after az call or during it)
    from y2shared.parts import init_parts_dir
    import datetime
    run_id = init_parts_dir(parts_dir, job, total_rows=2, parallel=2)

    # State file in fake_az simulating that the remote invoke was recorded
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / "res-p1-0001_run.json", "w") as f:
        json.dump({"run_id": run_id, "started_at": time.time() - 60}, f)

    cl = CollectList(parts_dir)
    past_iso = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=60)).isoformat()
    cl.append({
        "index": 1,
        "vm_name": "res-p1-0001",
        "subscription": "sub-01",
        "resource_group": "rg-prod",
        "invoked_at": past_iso,
        "confirmed": False,
        "attempts": 1,
        "log": ["precheck ok"],
    })

    # Now resume
    args = argparse.Namespace(
        job_yaml=str(yaml_path),
        vm=[],
        limit=None,
        parallel=2,
        dry_run=False,
        resume=True,
        keep_parts=False,
        yes=True,
    )
    rc = run_job(args)
    assert rc == 0

    # In log_file: res-p1-0001 must NEVER have received a second runCommand POST!
    log_file = setup_fire_and_forget_env
    calls = log_file.read_text(encoding="utf-8").strip().splitlines()
    nowait_01 = [c for c in calls if "res-p1-0001" in c and c.startswith("rest ")]
    assert len(nowait_01) == 0  # Was already in collect_list, never re-fired!

    nowait_02 = [c for c in calls if "res-p1-0002" in c and c.startswith("rest ")]
    assert len(nowait_02) == 1  # VM 2 was fired


# ==============================================================================
# Collect Later: ERROR:still_running kept and read later with --resume
# ==============================================================================

def test_collect_later_still_running_and_resume(tmp_path, monkeypatch, setup_fire_and_forget_env):
    # VM takes longer than retry budget (e.g. 5 retries needed, but collect_retries=1)
    monkeypatch.setenv("FAKE_AZ_BUSY_COUNT_later-0001", "5")

    yaml_path = create_job_and_csv(
        tmp_path,
        num_vms=1,
        prefix="later",
        sleep_before_collect="0.05s",
        collect_retry_interval="0.05s",
        collect_retries=1,
    )

    args = argparse.Namespace(
        job_yaml=str(yaml_path),
        vm=[],
        limit=None,
        parallel=1,
        dry_run=False,
        resume=False,
        keep_parts=False,  # Should be kept automatically because of ERROR:still_running!
        yes=True,
    )
    rc = run_job(args)
    assert rc == 2  # Exit 2 due to still running error

    job = load_job(yaml_path)
    parts_dir = get_parts_dir(job.output_csv)
    # Parts folder MUST be kept even without --keep-parts
    assert parts_dir.exists()

    # result.csv has ERROR:still_running
    with open(job.output_csv, "r", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[1][2] == "ERROR:still_running"

    # Now simulate VM script finally finishes (clear busy count)
    monkeypatch.delenv("FAKE_AZ_BUSY_COUNT_later-0001", raising=False)
    log_file = setup_fire_and_forget_env
    with open(log_file, "w") as f:
        f.write("")

    # Resume to collect later
    args_resume = argparse.Namespace(
        job_yaml=str(yaml_path),
        vm=[],
        limit=None,
        parallel=1,
        dry_run=False,
        resume=True,
        keep_parts=False,
        yes=True,
    )
    rc_res = run_job(args_resume)
    assert rc_res == 0

    # Verify no second invoke call
    calls = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert not any(c.startswith("rest ") for c in calls)
    assert any("-o json" in c for c in calls)

    # result.csv updated with real values
    with open(job.output_csv, "r", encoding="utf-8") as f:
        rows_after = list(csv.reader(f))
    assert rows_after[1][2] == "22102026"


# ==============================================================================
# Auth Breaker in Phase 1 and Phase 2 -> Exit 3
# ==============================================================================

def test_auth_breaker_exit_3(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AZ_RC_DEFAULT", "auth")
    yaml_path = create_job_and_csv(tmp_path, num_vms=10, prefix="auth")

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
    assert rc == 3


def test_scheduler_unit_injected_clock(tmp_path):
    """
    Unit test for Phase 2 Scheduler with an injected clock:
    - First attempt waits sleep_before_collect from invoked_at.
    - VM busy on every attempt is read 1 + collect_retries times, collect_retry_interval apart, then ends ERROR:still_running.
    - VM busy once then done ends OK.
    """
    from y2shared.runner import run_collect_phase, RunState
    from y2shared.dispatcher import AuthCircuitBreaker, ReadResult
    from y2shared.inputcsv import VMRow
    from y2shared.parts import read_part_row, get_part_paths
    import threading

    job_path = create_job_and_csv(
        tmp_path,
        num_vms=2,
        prefix="sched",
        sleep_before_collect="10s",
        collect_retry_interval="5s",
        collect_retries=2,
    )
    job = load_job(job_path)
    parts_dir = get_parts_dir(job.output_csv)
    parts_dir.mkdir(parents=True, exist_ok=True)

    row1 = VMRow(1, "sched-0001", "rg", "sub", "rhel8", {"vm_name": "sched-0001", "subscription": "sub"}, {})
    row2 = VMRow(2, "sched-0002", "rg", "sub", "rhel8", {"vm_name": "sched-0002", "subscription": "sub"}, {})

    t0 = 1000.0
    current_time = t0
    clock_lock = threading.Lock()

    def mock_time() -> float:
        with clock_lock:
            return current_time

    def mock_sleep(seconds: float) -> None:
        nonlocal current_time
        with clock_lock:
            current_time += seconds
        time.sleep(0.001)  # small real yield so threads can run

    # Track attempts and timestamps per VM
    call_log: dict[str, list[float]] = {"sched-0001": [], "sched-0002": []}
    call_lock = threading.Lock()

    def mock_read(row, collect_script_path, run_id, logger, circuit_breaker=None):
        with call_lock:
            call_log[row.vm_name].append(mock_time())
            attempt = len(call_log[row.vm_name])

        if row.vm_name == "sched-0001":
            # Always busy
            return ReadResult(status="STILL_RUNNING")
        else:
            # Busy on attempt 1, OK on attempt 2
            if attempt == 1:
                return ReadResult(status="STILL_RUNNING")
            return ReadResult(status="OK", vars={"HIP_VERSION": "22102026", "KERNEL_COUNT": "3"})

    import datetime
    invoked_iso = datetime.datetime.fromtimestamp(t0).isoformat()
    work_set = [
        (row1, {"index": 1, "vm_name": "sched-0001", "invoked_at": invoked_iso, "log": []}),
        (row2, {"index": 2, "vm_name": "sched-0002", "invoked_at": invoked_iso, "log": []}),
    ]

    run_state = RunState(total_vms=2, start_time=t0)
    circuit_breaker = AuthCircuitBreaker()
    interrupted_event = threading.Event()

    run_collect_phase(
        job=job,
        work_set=work_set,
        parts_dir=parts_dir,
        collect_script_path=tmp_path / "collect.sh",
        run_id="preview",
        total_rows=2,
        parallel=2,
        circuit_breaker=circuit_breaker,
        run_state=run_state,
        interrupted_event=interrupted_event,
        time_fn=mock_time,
        sleep_fn=mock_sleep,
        read_fn=mock_read,
    )

    # 1. sched-0001 was read 1 initial + 2 retries = 3 times total
    assert len(call_log["sched-0001"]) == 3
    # First attempt waited exactly 10s (sleep_before_collect) from t0
    assert call_log["sched-0001"][0] >= 1010.0
    # Next attempts were 5s apart (collect_retry_interval)
    assert call_log["sched-0001"][1] >= call_log["sched-0001"][0] + 5.0
    assert call_log["sched-0001"][2] >= call_log["sched-0001"][1] + 5.0

    # 2. sched-0002 was read 2 times (attempt 1 busy, attempt 2 OK)
    assert len(call_log["sched-0002"]) == 2
    assert call_log["sched-0002"][0] >= 1010.0
    assert call_log["sched-0002"][1] >= call_log["sched-0002"][0] + 5.0

    # 3. Check part file results
    csv1, _ = get_part_paths(parts_dir, row1, 2)
    csv2, _ = get_part_paths(parts_dir, row2, 2)

    vals1 = read_part_row(csv1)
    vals2 = read_part_row(csv2)

    assert "ERROR:still_running" in vals1[2]
    assert "22102026" in vals2[2]

