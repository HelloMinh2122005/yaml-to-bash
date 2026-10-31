"""Tests for Milestone M7: root-level pre_run / post_run and power policies start-stop / stop."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from pathlib import Path
import pytest

from y2shared import cli
from y2shared.config import Job, Precheck, PostRun, Invoke, load_job
from y2shared.inputcsv import VMRow, read_input_csv
from y2shared.parts import get_parts_dir, init_parts_dir, is_part_finished, power_path, read_power, read_part_row
from y2shared.runner import run_job
from y2shared.validate import validate_job_dict, validate_job_file
from y2b.profile import PROFILE as Y2B_PROFILE
from y2p.profile import PROFILE as Y2P_PROFILE

FAKE_AZ_DIR = Path(__file__).parent / "fake_az"


@pytest.fixture(autouse=True)
def setup_fake_az_env(monkeypatch, tmp_path):
    old_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", f"{FAKE_AZ_DIR}:{old_path}")
    monkeypatch.setenv("FAKE_AZ_RC_DEFAULT", "success")
    log_file = tmp_path / "az_calls.log"
    monkeypatch.setenv("FAKE_AZ_LOG", str(log_file))
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FAKE_AZ_STATE_DIR", str(state_dir))
    return log_file, state_dir


def read_counter(state_dir: Path, vm_name: str, counter_name: str) -> int:
    counter_file = state_dir / f"{vm_name}_{counter_name}.txt"
    if not counter_file.exists():
        return 0
    return int(counter_file.read_text(encoding="utf-8").strip())


def read_vm_state(state_dir: Path, vm_name: str) -> str:
    state_file = state_dir / f"{vm_name}_state.txt"
    if not state_file.exists():
        return ""
    return state_file.read_text(encoding="utf-8").strip()


# Fleet scenarios expressed as per-result pre_run actions: (running, deallocated, stopped)
POLICY_RESULT_ACTIONS = {
    "skip": ("continue", "skip", "skip"),
    "start": ("continue", "start", "start"),
    "start-stop": ("stop", "start-stop", "start-stop"),
    "stop": ("stop", "skip", "skip"),
}


def create_fleet_job(
    tmp_path: Path,
    vms: list[tuple[str, str]],  # (vm_name, initial_power_state)
    policy: str = "skip",  # key of POLICY_RESULT_ACTIONS
    include_collect_precheck: bool = True,
    post_run_yaml: str = "",
    sleep_before_collect: str = "0s",
    res_file: str = "/var/tmp/runcommand/result.res",
    os_name: str = "rhel8",
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    csv_file = tmp_path / "list_vm.csv"
    with open(csv_file, "w", encoding="utf-8") as f:
        f.write("vm_name,resource_group,subscription,os\n")
        for vm_name, _ in vms:
            f.write(f"{vm_name},rg-prod,sub-01,{os_name}\n")

    state_dir = Path(os.environ.get("FAKE_AZ_STATE_DIR", "/tmp/fake_az_state"))
    for vm_name, initial_state in vms:
        state_file = state_dir / f"{vm_name}_state.txt"
        state_file.write_text(initial_state, encoding="utf-8")

    default_log_file = r"C:\ProgramData\y2p\result.log" if os_name.startswith("win") else "/var/tmp/runcommand/result.log"
    collect_phase_str = '["invoke", "collect"]' if include_collect_precheck else '["invoke"]'

    post_block = f"\npost_run:\n{post_run_yaml}" if post_run_yaml else ""
    running_action, deallocated_action, stopped_action = POLICY_RESULT_ACTIONS[policy]

    yaml_file = tmp_path / "job.yaml"
    yaml_file.write_text(
        f"""
input: "./list_vm.csv"
output_log: "./result.log"
output_csv: "./result.csv"
parallel: 5
sleep_before_collect: "{sleep_before_collect}"
collect_retry_interval: "0.1s"
collect_retries: 2

pre_run:
  - name: "check-if-subscription-in-tenant"
    phase: ["invoke"]
    cache: "subscription"
    script: "echo 0"
    result:
      0: {{ log: "Sub OK", action: "continue" }}
      1: {{ log: "Sub missing", action: "end_check" }}

  - name: "check-if-vm-is-running"
    phase: {collect_phase_str}
    script: |
      az vm get-instance-view -g "$RESOURCE_GROUP" -n "$VM_NAME" --query "instanceView.statuses[?starts_with(code, 'PowerState/')].code" -o tsv
    result:
      "PowerState/running": {{ log: "running", action: "{running_action}" }}
      "PowerState/deallocated": {{ log: "deallocated", action: "{deallocated_action}" }}
      "PowerState/stopped": {{ log: "stopped", action: "{stopped_action}" }}

invoke:
  runcommand:
    res_file: '{res_file}'
    log_file: '{default_log_file}'
    steps:
      - name: "get-hip"
        script: "write_result HIP_VERSION '22102026'"

collect:
  - column: "vm_name"
    source: "input.vm_name"
  - column: "subscription"
    source: "input.subscription"
  - column: "hip_version"
    var: "HIP_VERSION"
{post_block}
""",
        encoding="utf-8",
    )
    return yaml_file


# ------------------------------------------------------------------------------
# R7: Legacy invoke.precheck compatibility & mutual exclusion
# ------------------------------------------------------------------------------
def test_r7_legacy_precheck_and_mutual_exclusion(tmp_path):
    # 1. Job with invoke.precheck and no pre_run loads with deprecation warning
    legacy_yaml = tmp_path / "legacy.yaml"
    legacy_yaml.write_text(
        """
input: "./list_vm.csv"
output_log: "./result.log"
output_csv: "./result.csv"
sleep_before_collect: "0s"
invoke:
  precheck:
    - name: "check-legacy"
      script: "echo 0"
      result:
        0: { log: "ok", action: "continue" }
  runcommand:
    res_file: "/var/tmp/result.res"
    log_file: "/var/tmp/result.log"
    steps:
      - name: "step1"
        script: "write_result TEST_VAR 'val'"
collect:
  - column: "test"
    var: "TEST_VAR"
""",
        encoding="utf-8",
    )
    job, val_res = validate_job_file(legacy_yaml)
    assert val_res.is_valid
    assert any("invoke.precheck is deprecated, use root pre_run" in str(w) for w in val_res.warnings)
    assert len(job.pre_run) == 1
    assert job.pre_run[0].name == "check-legacy"
    assert job.pre_run[0].phases == ["invoke"]
    assert job.invoke.precheck == job.pre_run

    # 2. Both pre_run and invoke.precheck defined -> validation error
    both_yaml = tmp_path / "both.yaml"
    both_yaml.write_text(
        """
input: "./list_vm.csv"
output_log: "./result.log"
output_csv: "./result.csv"
sleep_before_collect: "0s"
pre_run:
  - name: "check-root"
    script: "echo 0"
    result:
      0: { log: "ok", action: "continue" }
invoke:
  precheck:
    - name: "check-invoke"
      script: "echo 0"
      result:
        0: { log: "ok", action: "continue" }
  runcommand:
    res_file: "/var/tmp/result.res"
    log_file: "/var/tmp/result.log"
    steps:
      - name: "step1"
        script: "write_result TEST_VAR 'val'"
collect:
  - column: "test"
    var: "TEST_VAR"
""",
        encoding="utf-8",
    )
    job2, val_res2 = validate_job_file(both_yaml)
    assert not val_res2.is_valid
    assert any("Cannot define both root 'pre_run' and 'invoke.precheck'" in str(e) for e in val_res2.errors)


# ------------------------------------------------------------------------------
# R1: Phase execution counts and subscription caching across phases
# ------------------------------------------------------------------------------
def test_r1_phases_execution_and_subscription_cache(tmp_path, setup_fake_az_env):
    log_file, state_dir = setup_fake_az_env
    cnt_invoke = tmp_path / "cnt_invoke.txt"
    cnt_invoke.write_text("", encoding="utf-8")

    cnt_both = tmp_path / "cnt_both.txt"
    cnt_both.write_text("", encoding="utf-8")

    cnt_cached = tmp_path / "cnt_cached.txt"
    cnt_cached.write_text("", encoding="utf-8")

    csv_file = tmp_path / "list_vm.csv"
    csv_file.write_text(
        "vm_name,resource_group,subscription,os\nvm-01,rg-1,sub-1,rhel8\nvm-02,rg-1,sub-1,rhel8\n",
        encoding="utf-8",
    )

    yaml_file = tmp_path / "job.yaml"
    yaml_file.write_text(
        f"""
input: "./list_vm.csv"
output_log: "./result.log"
output_csv: "./result.csv"
parallel: 2
sleep_before_collect: "0s"

pre_run:
  - name: "invoke-only-check"
    phase: ["invoke"]
    script: |
      echo 1 >> {cnt_invoke}
      echo 0
    result:
      0: {{ log: "ok", action: "continue" }}

  - name: "both-phases-check"
    phase: ["invoke", "collect"]
    script: |
      echo 1 >> {cnt_both}
      echo 0
    result:
      0: {{ log: "ok", action: "continue" }}

  - name: "cached-both-phases"
    phase: ["invoke", "collect"]
    cache: "subscription"
    script: |
      echo 1 >> {cnt_cached}
      echo 0
    result:
      0: {{ log: "ok", action: "continue" }}

invoke:
  runcommand:
    res_file: "/var/tmp/result.res"
    log_file: "/var/tmp/result.log"
    steps:
      - name: "step"
        script: "write_result TEST 'ok'"

collect:
  - column: "vm_name"
    source: "input.vm_name"
  - column: "val"
    var: "TEST"
""",
        encoding="utf-8",
    )
    args = argparse.Namespace(
        job_yaml=str(yaml_file),
        vm=[],
        limit=None,
        parallel=2,
        dry_run=False,
        resume=False,
        keep_parts=False,
        yes=True,
    )
    rc = run_job(args)
    assert rc == 0

    # 2 VMs:
    # invoke-only runs 1x per VM = 2
    assert len(cnt_invoke.read_text().split()) == 2
    # both-phases runs 1x in invoke + 1x in collect per VM = 4
    assert len(cnt_both.read_text().split()) == 4
    # cached-both-phases: both VMs in sub-1; runs once in invoke, cached for VM 2 and cached in collect!
    assert len(cnt_cached.read_text().split()) == 1


# ------------------------------------------------------------------------------
# R3, Power Matrix: 2 running, 2 deallocated, 1 stopped under skip/start/start-stop/stop
# ------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "policy,expected_counts,expected_end_states,expected_deallocs,expected_poweroffs",
    [
        (
            "skip",
            {"OK": 2, "SKIPPED": 3, "ENDED": 0, "ERROR": 0},
            {
                "vm-01": "PowerState/running",
                "vm-02": "PowerState/running",
                "vm-03": "PowerState/deallocated",
                "vm-04": "PowerState/deallocated",
                "vm-05": "PowerState/stopped",
            },
            0,
            0,
        ),
        (
            "start",
            {"OK": 5, "SKIPPED": 0, "ENDED": 0, "ERROR": 0},
            {
                "vm-01": "PowerState/running",
                "vm-02": "PowerState/running",
                "vm-03": "PowerState/running",
                "vm-04": "PowerState/running",
                "vm-05": "PowerState/running",
            },
            0,
            0,
        ),
        (
            "start-stop",
            {"OK": 5, "SKIPPED": 0, "ENDED": 0, "ERROR": 0},
            {
                "vm-01": "PowerState/deallocated",
                "vm-02": "PowerState/deallocated",
                "vm-03": "PowerState/deallocated",
                "vm-04": "PowerState/deallocated",
                "vm-05": "PowerState/stopped",
            },
            4,
            1,
        ),
        (
            "stop",
            {"OK": 2, "SKIPPED": 3, "ENDED": 0, "ERROR": 0},
            {
                "vm-01": "PowerState/deallocated",
                "vm-02": "PowerState/deallocated",
                "vm-03": "PowerState/deallocated",
                "vm-04": "PowerState/deallocated",
                "vm-05": "PowerState/stopped",
            },
            2,
            0,
        ),
    ],
)
def test_power_matrix(
    policy,
    expected_counts,
    expected_end_states,
    expected_deallocs,
    expected_poweroffs,
    tmp_path,
    setup_fake_az_env,
):
    log_file, state_dir = setup_fake_az_env
    fleet = [
        ("vm-01", "PowerState/running"),
        ("vm-02", "PowerState/running"),
        ("vm-03", "PowerState/deallocated"),
        ("vm-04", "PowerState/deallocated"),
        ("vm-05", "PowerState/stopped"),
    ]
    job_file = create_fleet_job(tmp_path, fleet, policy=policy)
    args = argparse.Namespace(
        job_yaml=str(job_file),
        vm=[],
        limit=None,
        parallel=5,
        dry_run=False,
        resume=False,
        keep_parts=False,
        yes=True,
    )
    rc = run_job(args)
    expected_rc = 0 if expected_counts["SKIPPED"] == 0 and expected_counts["ERROR"] == 0 else 2
    assert rc == expected_rc

    # Verify end states
    for vm_name, exp_state in expected_end_states.items():
        actual_state = read_vm_state(state_dir, vm_name)
        assert actual_state == exp_state, f"{vm_name} expected {exp_state}, got {actual_state}"

    # Verify call counts
    total_deallocs = sum(read_counter(state_dir, vm, "deallocate") for vm, _ in fleet)
    total_poweroffs = sum(read_counter(state_dir, vm, "poweroff") for vm, _ in fleet)
    assert total_deallocs == expected_deallocs
    assert total_poweroffs == expected_poweroffs


# ------------------------------------------------------------------------------
# R3: Crash after .power before az vm start completes; resume restores VM
# ------------------------------------------------------------------------------
def test_r3_crash_after_power_file_resumed(tmp_path, setup_fake_az_env):
    log_file, state_dir = setup_fake_az_env
    fleet = [("vm-01", "PowerState/running")]
    job_file = create_fleet_job(tmp_path, fleet, policy="start-stop")
    job = load_job(job_file)
    parts_dir = get_parts_dir(job.output_csv)
    init_parts_dir(parts_dir, job, total_rows=1, parallel=1)

    # Pre-write a finished part for vm-01 and a pending .power file simulating interrupted run
    p_file = parts_dir / "0001-vm-01.power"
    p_data = {
        "prev_state": "PowerState/deallocated",
        "started_by_run": True,
        "post": "pending",
        "at": "2026-10-31T09:00:00Z",
    }
    p_file.write_text(json.dumps(p_data), encoding="utf-8")

    # Part CSV & log
    csv_part = parts_dir / "0001-vm-01.csv"
    csv_part.write_text("vm-01,sub-01,22102026\n", encoding="utf-8")
    log_part = parts_dir / "0001-vm-01.log"
    log_part.write_text("vm log\n", encoding="utf-8")

    # Set VM state to running (as if it was started)
    (state_dir / "vm-01_state.txt").write_text("PowerState/running", encoding="utf-8")

    # Run with --resume
    args = argparse.Namespace(
        job_yaml=str(job_file),
        vm=[],
        limit=None,
        parallel=1,
        dry_run=False,
        resume=True,
        keep_parts=True,
        yes=True,
    )
    rc = run_job(args)
    assert rc == 0

    # Verify VM was returned to PowerState/deallocated according to prev_state
    assert read_vm_state(state_dir, "vm-01") == "PowerState/deallocated"
    # Verify .power prev_state was NOT overwritten
    updated_power = json.loads(p_file.read_text(encoding="utf-8"))
    assert updated_power["prev_state"] == "PowerState/deallocated"
    assert updated_power["post"] == "done"


# ------------------------------------------------------------------------------
# R4: still_running VM gets no hooks and no power action until --resume
# ------------------------------------------------------------------------------
def test_r4_still_running_held_until_resume(tmp_path, setup_fake_az_env, monkeypatch):
    log_file, state_dir = setup_fake_az_env
    # vm-01 will be busy on reader calls -> STILL_RUNNING
    monkeypatch.setenv("FAKE_AZ_BUSY_COUNT_vm-01", "10")

    fleet = [("vm-01", "PowerState/running")]
    post_hook = """
  - name: "notify-audit"
    script: "echo ok"
    result:
      ok: { log: "hook ran" }
"""
    job_file = create_fleet_job(tmp_path, fleet, policy="start-stop", post_run_yaml=post_hook)
    args = argparse.Namespace(
        job_yaml=str(job_file),
        vm=[],
        limit=None,
        parallel=1,
        dry_run=False,
        resume=False,
        keep_parts=False,
        yes=True,
    )
    rc1 = run_job(args)
    assert rc1 == 2  # Held/error

    # Parts must be kept
    job = load_job(job_file)
    parts_dir = get_parts_dir(job.output_csv)
    assert parts_dir.exists()

    # VM should NOT have been deallocated yet because it was held
    assert read_counter(state_dir, "vm-01", "deallocate") == 0
    p_data = read_power(parts_dir / "0001-vm-01.power")
    assert p_data["post"] == "pending"

    # Now clear busy count so reader succeeds on resume
    monkeypatch.delenv("FAKE_AZ_BUSY_COUNT_vm-01", raising=False)
    (state_dir / "vm-01_busy.txt").write_text("0", encoding="utf-8")

    args_resume = argparse.Namespace(
        job_yaml=str(job_file),
        vm=[],
        limit=None,
        parallel=1,
        dry_run=False,
        resume=True,
        keep_parts=True,
        yes=True,
    )
    rc2 = run_job(args_resume)
    assert rc2 == 0

    # Now hooks ran and VM deallocated!
    assert read_counter(state_dir, "vm-01", "deallocate") == 1
    p_data_after = read_power(parts_dir / "0001-vm-01.power")
    assert p_data_after["post"] == "done"


# ------------------------------------------------------------------------------
# R6: Drift handling (running at invoke, not running at collect)
# ------------------------------------------------------------------------------
def test_r6_drift_skip_stop_errors_and_start_restarts(tmp_path, setup_fake_az_env, monkeypatch):
    log_file, state_dir = setup_fake_az_env
    # Cause vm-01 to drift to deallocated after invoke
    monkeypatch.setenv("FAKE_AZ_DRIFT_vm-01", "PowerState/deallocated")

    # Case 1: skip policy with collect check -> ERROR:not_running_at_collect
    fleet1 = [("vm-01", "PowerState/running")]
    job_file1 = create_fleet_job(tmp_path / "run_skip", fleet1, policy="skip", include_collect_precheck=True)
    args1 = argparse.Namespace(
        job_yaml=str(job_file1),
        vm=[],
        limit=None,
        parallel=1,
        dry_run=False,
        resume=False,
        keep_parts=False,
        yes=True,
    )
    rc1 = run_job(args1)
    assert rc1 == 2
    job1 = load_job(job_file1)
    with open(job1.output_csv, "r", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert any("ERROR:not_running_at_collect:check-if-vm-is-running" in col for col in rows[1])

    # Case 2: start-stop policy with collect check -> restarts VM and reads OK, ends deallocated
    fleet2 = [("vm-01", "PowerState/running")]
    job_file2 = create_fleet_job(tmp_path / "run_ss", fleet2, policy="start-stop", include_collect_precheck=True)
    args2 = argparse.Namespace(
        job_yaml=str(job_file2),
        vm=[],
        limit=None,
        parallel=1,
        dry_run=False,
        resume=False,
        keep_parts=False,
        yes=True,
    )
    rc2 = run_job(args2)
    assert rc2 == 0
    job2 = load_job(job_file2)
    with open(job2.output_csv, "r", encoding="utf-8") as f:
        rows2 = list(csv.reader(f))
    assert rows2[1][2] == "22102026"
    assert read_vm_state(state_dir, "vm-01") == "PowerState/deallocated"


# ------------------------------------------------------------------------------
# post_run: environment variables and failure tolerance
# ------------------------------------------------------------------------------
def test_post_run_environment_and_failure_tolerance(tmp_path, setup_fake_az_env):
    log_file, state_dir = setup_fake_az_env
    env_dump = tmp_path / "env_dump.txt"

    post_yaml = f"""
  - name: "failing-hook"
    script: "echo 'error outcome'"
    result:
      ok: {{ log: "ok" }}
      default: {{ log: "failed as expected", status: "error" }}

  - name: "dump-env-hook"
    script: |
      echo "OUTCOME=$Y2_OUTCOME POWER=$Y2_POWER_AFTER VM=$VM_NAME" > {env_dump}
      echo ok
    result:
      ok: {{ log: "dumped env" }}
"""
    fleet = [("vm-01", "PowerState/running")]
    job_file = create_fleet_job(tmp_path, fleet, policy="start-stop", post_run_yaml=post_yaml)
    args = argparse.Namespace(
        job_yaml=str(job_file),
        vm=[],
        limit=None,
        parallel=1,
        dry_run=False,
        resume=False,
        keep_parts=False,
        yes=True,
    )
    rc = run_job(args)
    # Even though CSV row was OK, a failing post_run hook causes exit code 2
    assert rc == 2

    # Second hook still ran and dumped env!
    dump_text = env_dump.read_text(encoding="utf-8").strip()
    assert "OUTCOME=OK" in dump_text
    assert "POWER=deallocate" in dump_text
    assert "VM=vm-01" in dump_text

    # Power action still ran even though first hook failed!
    assert read_vm_state(state_dir, "vm-01") == "PowerState/deallocated"


# ------------------------------------------------------------------------------
# Dry-run with stop and start-stop
# ------------------------------------------------------------------------------
def test_dry_run_power_rules(tmp_path, setup_fake_az_env):
    log_file, state_dir = setup_fake_az_env

    # 1. Dry run with stop: running VMs must NOT be deallocated!
    fleet_stop = [("vm-01", "PowerState/running")]
    job_stop = create_fleet_job(tmp_path / "dry_stop", fleet_stop, policy="stop")
    args_stop = argparse.Namespace(
        job_yaml=str(job_stop),
        vm=[],
        limit=None,
        parallel=1,
        dry_run=True,
        resume=False,
        keep_parts=False,
        yes=True,
    )
    rc_stop = run_job(args_stop)
    assert rc_stop == 0
    assert read_counter(state_dir, "vm-01", "deallocate") == 0
    assert read_vm_state(state_dir, "vm-01") == "PowerState/running"

    # 2. Dry run with start-stop: a VM that was deallocated and started during precheck
    # must be restored to deallocated!
    fleet_ss = [("vm-02", "PowerState/deallocated")]
    job_ss = create_fleet_job(tmp_path / "dry_ss", fleet_ss, policy="start-stop")
    args_ss = argparse.Namespace(
        job_yaml=str(job_ss),
        vm=[],
        limit=None,
        parallel=1,
        dry_run=True,
        resume=False,
        keep_parts=False,
        yes=True,
    )
    rc_ss = run_job(args_ss)
    assert rc_ss == 0
    # vm-02 was started, then restored to deallocated
    assert read_counter(state_dir, "vm-02", "start") == 1
    assert read_counter(state_dir, "vm-02", "deallocate") == 1
    assert read_vm_state(state_dir, "vm-02") == "PowerState/deallocated"


# ------------------------------------------------------------------------------
# Banner warning for stop and start-stop
# ------------------------------------------------------------------------------
def test_banner_power_policy_warning(tmp_path, setup_fake_az_env, capsys):
    fleet = [("vm-01", "PowerState/running")]
    job_file = create_fleet_job(tmp_path, fleet, policy="start-stop")
    args = argparse.Namespace(
        job_yaml=str(job_file),
        vm=[],
        limit=None,
        parallel=1,
        dry_run=True,
        resume=False,
        keep_parts=False,
        yes=True,
    )
    run_job(args)
    captured = capsys.readouterr().out
    assert (
        "Power policy: pre_run uses action 'stop' — after the job, deallocates up to 1 VMs, "
        "INCLUDING VMs that are running now"
    ) in captured


def test_banner_start_stop_only(tmp_path, setup_fake_az_env, capsys):
    fleet = [("vm-01", "PowerState/running")]
    job_file = create_fleet_job(tmp_path, fleet, policy="start")
    job_file.write_text(job_file.read_text(encoding="utf-8").replace('action: "start"', 'action: "start-stop"'), encoding="utf-8")
    args = argparse.Namespace(
        job_yaml=str(job_file),
        vm=[],
        limit=None,
        parallel=1,
        dry_run=True,
        resume=False,
        keep_parts=False,
        yes=True,
    )
    run_job(args)
    captured = capsys.readouterr().out
    assert "VMs this run starts are returned to their previous power state" in captured
    assert "INCLUDING VMs that are running now" not in captured


# ------------------------------------------------------------------------------
# Validator warning on res_file under /tmp
# ------------------------------------------------------------------------------
def test_tmp_res_file_validator_warning(tmp_path):
    fleet = [("vm-01", "PowerState/running")]
    job_file = create_fleet_job(tmp_path, fleet, policy="skip", res_file="/tmp/result.res")
    job, val_res = validate_job_file(job_file)
    assert val_res.is_valid
    assert any("res_file under /tmp may not survive a reboot or VM restart" in str(w) for w in val_res.warnings)


# ------------------------------------------------------------------------------
# y2p integration: start-stop and post_run
# ------------------------------------------------------------------------------
def test_y2p_start_stop_and_post_run(tmp_path, setup_fake_az_env, monkeypatch):
    log_file, state_dir = setup_fake_az_env
    monkeypatch.setenv("FAKE_AZ_EXPECT_CMD", "RunPowerShellScript")

    cmdb_dump = tmp_path / "cmdb.txt"
    post_hook = f"""
  - name: "notify-cmdb"
    script: |
      echo "VM=$VM_NAME OUTCOME=$Y2_OUTCOME" > {cmdb_dump}
      echo ok
    result:
      ok: {{ log: "cmdb notified" }}
"""
    fleet = [("win-01", "PowerState/running")]
    job_file = create_fleet_job(
        tmp_path,
        fleet,
        policy="start-stop",
        post_run_yaml=post_hook,
        res_file='C:\\ProgramData\\y2p\\result.res',
        os_name="windows2022",
    )

    rc = cli.main(Y2P_PROFILE, argv=["run", str(job_file), "-y"])
    assert rc == 0

    # Check post_run hook output
    assert "VM=win-01 OUTCOME=OK" in cmdb_dump.read_text(encoding="utf-8")
    # Check VM was deallocated under start-stop
    assert read_vm_state(state_dir, "win-01") == "PowerState/deallocated"
    assert read_counter(state_dir, "win-01", "deallocate") == 1
