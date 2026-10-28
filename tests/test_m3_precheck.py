"""Tests for Milestone M3: Precheck engine."""

import concurrent.futures
import os
import threading
import time
from pathlib import Path
import pytest

from y2b.config import Job, Precheck, Invoke
from y2b.inputcsv import VMRow
from y2b.precheck import PrecheckEngine
from y2b.vmlog import VMLogger


def create_test_vm(index: int, name: str, sub: str = "sub-1", rg: str = "rg-1") -> VMRow:
    data = {
        "vm_name": name,
        "resource_group": rg,
        "subscription": sub,
        "os": "rhel8",
    }
    env = {
        "VM_NAME": name,
        "RESOURCE_GROUP": rg,
        "SUBSCRIPTION": sub,
        "OS": "rhel8",
    }
    return VMRow(
        index=index,
        vm_name=name,
        resource_group=rg,
        subscription=sub,
        os="rhel8",
        raw_data=data,
        env=env,
    )


@pytest.mark.parametrize(
    "echo_val,expected_proceed,expected_marker",
    [
        ("0", True, None),
        ("1", False, "ENDED:test-check"),
        ("deallocated", False, "SKIPPED:test-check"),
        ("unexpected_val", False, "SKIPPED:test-check"),  # Caught by default -> skip
    ],
)
def test_precheck_actions_and_default(echo_val, expected_proceed, expected_marker, tmp_path):
    precheck = Precheck(
        name="test-check",
        script=f"echo '{echo_val}'",
        result={
            "0": {"log": "all good", "action": "continue"},
            "1": {"log": "terminating", "action": "end_check"},
            "deallocated": {"log": "stopped", "action": "skip"},
            "default": {"log": "unrecognized state fallback", "action": "skip"},
        },
    )
    job = Job(
        yaml_path=tmp_path / "job.yaml",
        yaml_dir=tmp_path,
        raw_yaml={},
        job_hash="test",
        input=tmp_path / "list.csv",
        output_log=tmp_path / "out.log",
        output_csv=tmp_path / "out.csv",
        actions="skip",
        invoke=Invoke(precheck=[precheck]),
    )
    row = create_test_vm(1, "vm-01")
    logger = VMLogger("vm-01", 1)

    engine = PrecheckEngine()
    proceed, marker = engine.run_prechecks_for_vm(job, row, logger)

    assert proceed is expected_proceed
    assert marker == expected_marker


def test_precheck_unmatched_without_default(tmp_path):
    precheck = Precheck(
        name="strict-check",
        script="echo 'unknown_state'",
        result={
            "0": {"log": "ok", "action": "continue"},
        },
    )
    job = Job(
        yaml_path=tmp_path / "job.yaml",
        yaml_dir=tmp_path,
        raw_yaml={},
        job_hash="test",
        input=tmp_path / "list.csv",
        output_log=tmp_path / "out.log",
        output_csv=tmp_path / "out.csv",
        actions="skip",
        invoke=Invoke(precheck=[precheck]),
    )
    row = create_test_vm(1, "vm-01")
    logger = VMLogger("vm-01", 1)

    engine = PrecheckEngine()
    proceed, marker = engine.run_prechecks_for_vm(job, row, logger)

    assert proceed is False
    assert marker == "ERROR:precheck_unmatched:strict-check"


def test_actions_start_policy_success(tmp_path):
    """
    When actions: start is set, if precheck returns skip, it executes 'az vm start'
    and re-runs the precheck.
    We simulate this using a state file in tmp_path.
    """
    state_file = tmp_path / "vm_state.txt"
    state_file.write_text("PowerState/deallocated", encoding="utf-8")

    # Script returns current state from state_file
    check_script = f"cat {state_file}"

    # Fake az vm start command in PATH
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_az = bin_dir / "az"
    fake_az.write_text(
        f"""#!/usr/bin/env bash
if [ "$1" = "vm" ] && [ "$2" = "start" ]; then
  echo "PowerState/running" > {state_file}
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    fake_az.chmod(0o755)

    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}:{old_path}"

    try:
        precheck = Precheck(
            name="check-vm-state",
            script=check_script,
            result={
                "PowerState/running": {"log": "running", "action": "continue"},
                "PowerState/deallocated": {"log": "deallocated", "action": "skip"},
            },
        )
        job = Job(
            yaml_path=tmp_path / "job.yaml",
            yaml_dir=tmp_path,
            raw_yaml={},
            job_hash="test",
            input=tmp_path / "list.csv",
            output_log=tmp_path / "out.log",
            output_csv=tmp_path / "out.csv",
            actions="start",  # Triggers az vm start
            invoke=Invoke(precheck=[precheck]),
        )
        row = create_test_vm(1, "vm-01")
        logger = VMLogger("vm-01", 1)

        engine = PrecheckEngine()
        proceed, marker = engine.run_prechecks_for_vm(job, row, logger)

        assert proceed is True
        assert marker is None
        assert state_file.read_text().strip() == "PowerState/running"
    finally:
        os.environ["PATH"] = old_path


def test_subscription_cache_under_concurrency(tmp_path):
    """
    20 threads run a subscription-cached precheck for the same subscription.
    The script counter must be incremented exactly once.
    """
    counter_file = tmp_path / "counter.txt"
    counter_file.write_text("0", encoding="utf-8")

    # Increment counter and return 0
    script = f"""
count=$(cat {counter_file})
count=$((count + 1))
echo "$count" > {counter_file}
# simulate brief query delay
sleep 0.05
echo "0"
"""
    precheck = Precheck(
        name="cached-sub-check",
        script=script,
        cache="subscription",
        result={
            "0": {"log": "verified", "action": "continue"},
        },
    )
    job = Job(
        yaml_path=tmp_path / "job.yaml",
        yaml_dir=tmp_path,
        raw_yaml={},
        job_hash="test",
        input=tmp_path / "list.csv",
        output_log=tmp_path / "out.log",
        output_csv=tmp_path / "out.csv",
        actions="skip",
        invoke=Invoke(precheck=[precheck]),
    )

    engine = PrecheckEngine()

    def worker(i: int):
        row = create_test_vm(i, f"vm-{i}", sub="common-subscription-id")
        logger = VMLogger(row.vm_name, row.index)
        proceed, marker = engine.run_prechecks_for_vm(job, row, logger)
        return proceed, marker

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(worker, i) for i in range(1, 21)]
        results = [f.result() for f in futures]

    assert all(proceed is True for proceed, _ in results)
    assert all(marker is None for _, marker in results)

    # Counter should be exactly 1!
    final_count = int(counter_file.read_text().strip())
    assert final_count == 1

