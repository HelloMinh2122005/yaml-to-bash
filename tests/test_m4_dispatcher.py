"""Tests for Milestone M4: Dispatcher and fake az integration."""

import os
import sys
from pathlib import Path
import pytest

from y2b.config import Job, Invoke, RunCommand
from y2b.dispatcher import (
    dispatch_vm,
    classify_error,
    parse_run_command_output,
    AuthCircuitBreaker,
    AuthAbortedError,
)
from y2b.inputcsv import VMRow
from y2b.vmlog import VMLogger

FAKE_AZ_DIR = Path(__file__).parent / "fake_az"


@pytest.fixture(autouse=True)
def setup_fake_az_env(monkeypatch, tmp_path):
    # Put fake_az first on PATH
    old_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", f"{FAKE_AZ_DIR}:{old_path}")
    log_file = tmp_path / "az_calls.log"
    monkeypatch.setenv("FAKE_AZ_LOG", str(log_file))
    return log_file


def make_test_job(tmp_path: Path, timeout: int = 1800) -> Job:
    return Job(
        yaml_path=tmp_path / "job.yaml",
        yaml_dir=tmp_path,
        raw_yaml={},
        job_hash="test-hash",
        input=tmp_path / "list.csv",
        output_log=tmp_path / "out.log",
        output_csv=tmp_path / "out.csv",
        run_timeout=timeout,
        invoke=Invoke(runcommand=RunCommand(res_file="/var/tmp/res", log_file="/var/tmp/log")),
    )


def make_test_row(name: str = "vm-01") -> VMRow:
    return VMRow(
        index=1,
        vm_name=name,
        resource_group="rg-test",
        subscription="sub-12345",
        os="rhel8",
        raw_data={"vm_name": name, "resource_group": "rg-test", "subscription": "sub-12345", "os": "rhel8"},
        env={"VM_NAME": name, "RESOURCE_GROUP": "rg-test", "SUBSCRIPTION": "sub-12345"},
    )


def test_classify_error():
    assert classify_error("429 TooManyRequests: throttled", 1) == "throttled"
    assert classify_error("Conflict: Operation in progress", 1) == "busy"
    assert classify_error("VMAgentStatusCommunicationError: agent not ready", 1) == "agent"
    assert classify_error("ResourceNotFound: VM not found", 1) == "missing"
    assert classify_error("AuthorizationFailed: az login required", 1) == "auth"
    assert classify_error("Some random unhandled exception", 1) == "unknown"


def test_dispatch_success(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AZ_RC_DEFAULT", "success")
    job = make_test_job(tmp_path)
    row = make_test_row()
    logger = VMLogger(row.vm_name, row.index)
    script_path = tmp_path / "remote.sh"
    script_path.write_text("#!/bin/bash\nexit 0\n")

    res = dispatch_vm(job, row, script_path, logger, delay_multiplier=0.001)

    assert res.error is None
    assert res.vars.get("HIP_VERSION") == "22102026"
    assert res.vars.get("KERNEL_COUNT") == "3"
    assert res.attempts == 1


def test_dispatch_throttled_retry_and_exhaustion(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AZ_RC_DEFAULT", "throttled")
    job = make_test_job(tmp_path)
    row = make_test_row()
    logger = VMLogger(row.vm_name, row.index)
    script_path = tmp_path / "remote.sh"
    script_path.write_text("#!/bin/bash\nexit 0\n")

    # Fast backoff in test
    res = dispatch_vm(job, row, script_path, logger, delay_multiplier=0.0001)

    assert res.error == "ERROR:throttled"
    # 1 initial + 5 backoff retries = 6 attempts
    assert res.attempts == 6


def test_dispatch_busy_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AZ_RC_DEFAULT", "busy")
    job = make_test_job(tmp_path)
    row = make_test_row()
    logger = VMLogger(row.vm_name, row.index)
    script_path = tmp_path / "remote.sh"
    script_path.write_text("#!/bin/bash\nexit 0\n")

    res = dispatch_vm(job, row, script_path, logger, delay_multiplier=0.0001)

    assert res.error == "ERROR:run_command_busy"
    # 1 initial + 3 retries = 4 attempts
    assert res.attempts == 4


def test_dispatch_agent_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AZ_RC_DEFAULT", "agent")
    job = make_test_job(tmp_path)
    row = make_test_row()
    logger = VMLogger(row.vm_name, row.index)
    script_path = tmp_path / "remote.sh"
    script_path.write_text("#!/bin/bash\nexit 0\n")

    res = dispatch_vm(job, row, script_path, logger)
    assert res.error == "ERROR:vm_agent_unavailable"
    assert res.attempts == 1


def test_dispatch_vm_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AZ_RC_DEFAULT", "missing")
    job = make_test_job(tmp_path)
    row = make_test_row()
    logger = VMLogger(row.vm_name, row.index)
    script_path = tmp_path / "remote.sh"
    script_path.write_text("#!/bin/bash\nexit 0\n")

    res = dispatch_vm(job, row, script_path, logger)
    assert res.error == "ERROR:vm_not_found"
    assert res.attempts == 1


def test_dispatch_auth_and_circuit_breaker(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AZ_RC_DEFAULT", "auth")
    job = make_test_job(tmp_path)
    script_path = tmp_path / "remote.sh"
    script_path.write_text("#!/bin/bash\nexit 0\n")

    cb = AuthCircuitBreaker(threshold=5)

    # First 4 auth errors don't raise AuthAbortedError, but return ERROR:auth
    for i in range(1, 5):
        row = make_test_row(f"vm-{i}")
        logger = VMLogger(row.vm_name, row.index)
        res = dispatch_vm(job, row, script_path, logger, circuit_breaker=cb)
        assert res.error == "ERROR:auth"
        assert not cb.is_tripped

    # 5th consecutive auth error trips the circuit breaker!
    row5 = make_test_row("vm-5")
    logger5 = VMLogger(row5.vm_name, row5.index)
    with pytest.raises(AuthAbortedError):
        dispatch_vm(job, row5, script_path, logger5, circuit_breaker=cb)

    assert cb.is_tripped


def test_dispatch_unknown_retry_once(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AZ_RC_DEFAULT", "unknown")
    job = make_test_job(tmp_path)
    row = make_test_row()
    logger = VMLogger(row.vm_name, row.index)
    script_path = tmp_path / "remote.sh"
    script_path.write_text("#!/bin/bash\nexit 0\n")

    res = dispatch_vm(job, row, script_path, logger, delay_multiplier=0.0001)

    assert res.error.startswith("ERROR:az:InternalServerError")
    # 1 initial + 1 retry = 2 attempts
    assert res.attempts == 2


def test_dispatch_truncated_markers(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AZ_RC_DEFAULT", "truncated")
    job = make_test_job(tmp_path)
    row = make_test_row()
    logger = VMLogger(row.vm_name, row.index)
    script_path = tmp_path / "remote.sh"
    script_path.write_text("#!/bin/bash\nexit 0\n")

    res = dispatch_vm(job, row, script_path, logger)
    assert res.error == "ERROR:result_truncated"


def test_dispatch_no_marker_block(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AZ_RC_DEFAULT", "no_marker")
    job = make_test_job(tmp_path)
    row = make_test_row()
    logger = VMLogger(row.vm_name, row.index)
    script_path = tmp_path / "remote.sh"
    script_path.write_text("#!/bin/bash\nexit 0\n")

    res = dispatch_vm(job, row, script_path, logger)
    assert res.error == "ERROR:no_result_block"


def test_dispatch_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AZ_RC_DEFAULT", "timeout")
    job = make_test_job(tmp_path, timeout=1)  # 1 second timeout
    row = make_test_row()
    logger = VMLogger(row.vm_name, row.index)
    script_path = tmp_path / "remote.sh"
    script_path.write_text("#!/bin/bash\nexit 0\n")

    res = dispatch_vm(job, row, script_path, logger)
    assert res.error == "ERROR:run_command_timeout"


def test_every_az_call_has_subscription(tmp_path, monkeypatch, setup_fake_az_env):
    log_file = setup_fake_az_env
    monkeypatch.setenv("FAKE_AZ_RC_DEFAULT", "success")
    job = make_test_job(tmp_path)
    row = make_test_row()
    logger = VMLogger(row.vm_name, row.index)
    script_path = tmp_path / "remote.sh"
    script_path.write_text("#!/bin/bash\nexit 0\n")

    dispatch_vm(job, row, script_path, logger)

    calls = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(calls) >= 1
    for call in calls:
        assert "--subscription" in call
        assert "sub-12345" in call

