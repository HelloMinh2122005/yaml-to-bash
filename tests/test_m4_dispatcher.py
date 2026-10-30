"""Tests for Milestone M3: Dispatcher, invoke_nowait, and read_results."""

import json
import os
import sys
from pathlib import Path
import pytest

from y2shared.config import Job, Invoke, RunCommand
from y2shared import dispatcher
from y2shared.dispatcher import (
    invoke_nowait,
    read_results,
    classify_error,
    parse_run_command_output,
    AuthCircuitBreaker,
    AuthAbortedError,
)
from y2shared.inputcsv import VMRow
from y2shared.vmlog import VMLogger

FAKE_AZ_DIR = Path(__file__).parent / "fake_az"


@pytest.fixture(autouse=True)
def setup_fake_az_env(monkeypatch, tmp_path):
    old_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", f"{FAKE_AZ_DIR}:{old_path}")
    log_file = tmp_path / "az_calls.log"
    monkeypatch.setenv("FAKE_AZ_LOG", str(log_file))
    state_dir = tmp_path / "fake_az_state"
    monkeypatch.setenv("FAKE_AZ_STATE_DIR", str(state_dir))
    # subscription IDs are cached per process; start every test with an empty cache
    monkeypatch.setattr(dispatcher, "SUBSCRIPTIONS", dispatcher.SubscriptionResolver())
    return log_file


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


# ==============================================================================
# invoke_nowait Tests (Phase 1)
# ==============================================================================

def test_invoke_nowait_success(tmp_path, monkeypatch):
    row = make_test_row()
    logger = VMLogger(row.vm_name, row.index)
    script_path = tmp_path / "remote.sh"
    script_path.write_text("#!/bin/bash\nY2B_RUN_ID=run-100\nexit 0\n")

    res = invoke_nowait(row, script_path, logger, delay_multiplier=0.001)
    assert res.success is True
    assert res.error is None
    assert res.attempts == 1


def test_invoke_nowait_posts_run_command_via_az_rest(tmp_path, setup_fake_az_env):
    # Phase 1 must not depend on `--no-wait` (silently drops the request before azure-cli 2.85.0)
    row = make_test_row()
    logger = VMLogger(row.vm_name, row.index)
    script_path = tmp_path / "remote.sh"
    script_path.write_text("#!/bin/bash\nY2B_RUN_ID=run-100\necho 'a \"quoted\" line'\nexit 0\n")

    res = invoke_nowait(row, script_path, logger, command_id="RunShellScript")
    assert res.success is True

    calls = setup_fake_az_env.read_text(encoding="utf-8").splitlines()
    # subscription name from the CSV is resolved to its ID once
    assert "account show --subscription sub-12345 --query id -o tsv" in calls
    rest_calls = [c for c in calls if c.startswith("rest ")]
    assert rest_calls == [
        "rest --method post --url /subscriptions/11111111-2222-3333-4444-555555555555"
        "/resourceGroups/rg-test/providers/Microsoft.Compute/virtualMachines/vm-01"
        f"/runCommand?api-version=2024-07-01 --body @{script_path}.body.json"
    ]
    assert not any("--no-wait" in c for c in calls)

    body = json.loads((tmp_path / "remote.sh.body.json").read_text(encoding="utf-8"))
    assert body == {"commandId": "RunShellScript", "script": [script_path.read_text(encoding="utf-8")]}


def test_invoke_nowait_guid_subscription_needs_no_lookup(tmp_path, setup_fake_az_env):
    guid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    row = make_test_row()
    row.subscription = guid
    script_path = tmp_path / "remote.ps1"
    script_path.write_text("$Y2B_RUN_ID = 'run-200'\nexit 0\n")

    res = invoke_nowait(row, script_path, VMLogger(row.vm_name, row.index), command_id="RunPowerShellScript")
    assert res.success is True

    calls = setup_fake_az_env.read_text(encoding="utf-8").splitlines()
    assert not any(c.startswith("account show") for c in calls)
    assert [c for c in calls if c.startswith("rest ")][0].startswith(f"rest --method post --url /subscriptions/{guid}/")
    body = json.loads((tmp_path / "remote.ps1.body.json").read_text(encoding="utf-8"))
    assert body["commandId"] == "RunPowerShellScript"


def test_invoke_nowait_throttled_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AZ_RC_DEFAULT", "throttled")
    row = make_test_row()
    logger = VMLogger(row.vm_name, row.index)
    script_path = tmp_path / "remote.sh"
    script_path.write_text("#!/bin/bash\nexit 0\n")

    res = invoke_nowait(row, script_path, logger, delay_multiplier=0.0001)
    assert res.success is False
    assert res.error == "ERROR:throttled"
    assert res.attempts == 6  # 1 initial + 5 retries


def test_invoke_nowait_busy_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AZ_RC_DEFAULT", "busy")
    row = make_test_row()
    logger = VMLogger(row.vm_name, row.index)
    script_path = tmp_path / "remote.sh"
    script_path.write_text("#!/bin/bash\nexit 0\n")

    res = invoke_nowait(row, script_path, logger, delay_multiplier=0.0001)
    assert res.success is False
    assert res.error == "ERROR:run_command_busy"
    assert res.attempts == 4  # 1 initial + 3 retries


def test_invoke_nowait_agent_and_missing(tmp_path, monkeypatch):
    row = make_test_row()
    logger = VMLogger(row.vm_name, row.index)
    script_path = tmp_path / "remote.sh"
    script_path.write_text("#!/bin/bash\nexit 0\n")

    monkeypatch.setenv("FAKE_AZ_RC_DEFAULT", "agent")
    res_agent = invoke_nowait(row, script_path, logger)
    assert res_agent.success is False
    assert res_agent.error == "ERROR:vm_agent_unavailable"

    monkeypatch.setenv("FAKE_AZ_RC_DEFAULT", "missing")
    res_missing = invoke_nowait(row, script_path, logger)
    assert res_missing.success is False
    assert res_missing.error == "ERROR:vm_not_found"


# ==============================================================================
# read_results Tests (Phase 2 Reader Outcome Table §3.4)
# ==============================================================================

def test_read_results_success(tmp_path, monkeypatch):
    # Fired with run_id "my-run-123"
    row = make_test_row()
    logger = VMLogger(row.vm_name, row.index)
    remote_script = tmp_path / "remote.sh"
    remote_script.write_text("#!/bin/bash\nY2B_RUN_ID=my-run-123\nexit 0\n")
    invoke_res = invoke_nowait(row, remote_script, logger)
    assert invoke_res.success is True

    # Read results
    collect_script = tmp_path / "collect.sh"
    collect_script.write_text("#!/bin/bash\nexit 0\n")
    read_res = read_results(row, collect_script, run_id="my-run-123", logger=logger)

    assert read_res.status == "OK"
    assert read_res.error is None
    assert read_res.vars.get("HIP_VERSION") == "22102026"
    assert read_res.vars.get("KERNEL_COUNT") == "3"


def test_read_results_busy_returns_still_running(tmp_path, monkeypatch):
    row = make_test_row()
    logger = VMLogger(row.vm_name, row.index)
    # Simulate VM busy running script
    monkeypatch.setenv(f"FAKE_AZ_RUNTIME_{row.vm_name}", "100")
    remote_script = tmp_path / "remote.sh"
    remote_script.write_text("#!/bin/bash\nY2B_RUN_ID=run-busy\nexit 0\n")
    invoke_nowait(row, remote_script, logger)

    collect_script = tmp_path / "collect.sh"
    collect_script.write_text("#!/bin/bash\nexit 0\n")
    read_res = read_results(row, collect_script, run_id="run-busy", logger=logger)

    # Busy should return STILL_RUNNING immediately without using retries in dispatcher!
    assert read_res.status == "STILL_RUNNING"
    assert read_res.attempts == 1


def test_read_results_remote_incomplete(tmp_path, monkeypatch):
    row = make_test_row()
    logger = VMLogger(row.vm_name, row.index)
    remote_script = tmp_path / "remote.sh"
    remote_script.write_text("#!/bin/bash\nY2B_RUN_ID=test-run\nexit 0\n")
    invoke_nowait(row, remote_script, logger)

    monkeypatch.setenv(f"FAKE_AZ_ASYNC_{row.vm_name}", "killed")

    collect_script = tmp_path / "collect.sh"
    collect_script.write_text("#!/bin/bash\nexit 0\n")
    read_res = read_results(row, collect_script, run_id="test-run", logger=logger)

    assert read_res.status == "ERROR"
    assert read_res.error == "ERROR:remote_incomplete"


def test_read_results_stale_result(tmp_path, monkeypatch):
    row = make_test_row()
    logger = VMLogger(row.vm_name, row.index)
    monkeypatch.setenv(f"FAKE_AZ_ASYNC_{row.vm_name}", "stale")

    collect_script = tmp_path / "collect.sh"
    collect_script.write_text("#!/bin/bash\nexit 0\n")
    read_res = read_results(row, collect_script, run_id="current-run-id", logger=logger)

    assert read_res.status == "ERROR"
    assert read_res.error == "ERROR:stale_result"


def test_read_results_not_started(tmp_path, monkeypatch):
    row = make_test_row()
    logger = VMLogger(row.vm_name, row.index)
    monkeypatch.setenv(f"FAKE_AZ_ASYNC_{row.vm_name}", "never_started")

    collect_script = tmp_path / "collect.sh"
    collect_script.write_text("#!/bin/bash\nexit 0\n")
    read_res = read_results(row, collect_script, run_id="current-run-id", logger=logger)

    assert read_res.status == "ERROR"
    assert read_res.error == "ERROR:not_started"
    # The reader's status line explains why: here the res_file was never created
    assert any("res_file missing" in line and "boot 2026-10-30 09:58:00" in line for line in logger.lines)


def test_reader_diagnostics_absent():
    from y2shared.dispatcher import reader_diagnostics
    assert reader_diagnostics("[stdout]\n===BEGIN_RESULT_RES===\n===END_RESULT_RES===\n") == (
        "y2-reader: no diagnostics in reader output"
    )


def test_read_results_agent_and_missing(tmp_path, monkeypatch):
    row = make_test_row()
    logger = VMLogger(row.vm_name, row.index)
    collect_script = tmp_path / "collect.sh"
    collect_script.write_text("#!/bin/bash\nexit 0\n")

    monkeypatch.setenv(f"FAKE_AZ_ASYNC_{row.vm_name}", "agent")
    res_agent = read_results(row, collect_script, run_id="run-1", logger=logger)
    assert res_agent.status == "ERROR"
    assert res_agent.error == "ERROR:vm_agent_unavailable"

    monkeypatch.setenv("FAKE_AZ_RC_DEFAULT", "missing")
    monkeypatch.delenv(f"FAKE_AZ_ASYNC_{row.vm_name}", raising=False)
    res_missing = read_results(row, collect_script, run_id="run-1", logger=logger)
    assert res_missing.status == "ERROR"
    assert res_missing.error == "ERROR:vm_not_found"


def test_read_results_auth_circuit_breaker(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AZ_RC_DEFAULT", "auth")
    collect_script = tmp_path / "collect.sh"
    collect_script.write_text("#!/bin/bash\nexit 0\n")

    cb = AuthCircuitBreaker(threshold=5)

    # First 4 auth errors return ERROR:auth
    for i in range(1, 5):
        row = make_test_row(f"vm-{i}")
        logger = VMLogger(row.vm_name, row.index)
        res = read_results(row, collect_script, run_id="run-1", logger=logger, circuit_breaker=cb)
        assert res.status == "ERROR"
        assert res.error == "ERROR:auth"
        assert not cb.is_tripped

    # 5th error trips circuit breaker
    row5 = make_test_row("vm-5")
    logger5 = VMLogger(row5.vm_name, row5.index)
    with pytest.raises(AuthAbortedError):
        read_results(row5, collect_script, run_id="run-1", logger=logger5, circuit_breaker=cb)

    assert cb.is_tripped


def test_read_results_truncated_and_timeout(tmp_path, monkeypatch):
    row = make_test_row()
    logger = VMLogger(row.vm_name, row.index)
    collect_script = tmp_path / "collect.sh"
    collect_script.write_text("#!/bin/bash\nexit 0\n")

    monkeypatch.setenv("FAKE_AZ_RC_DEFAULT", "truncated")
    res_trunc = read_results(row, collect_script, run_id="run-1", logger=logger)
    assert res_trunc.status == "ERROR"
    assert res_trunc.error == "ERROR:result_truncated"

    monkeypatch.setenv("FAKE_AZ_RC_DEFAULT", "timeout")
    res_to = read_results(row, collect_script, run_id="run-1", logger=logger, timeout=1)
    assert res_to.status == "ERROR"
    assert res_to.error == "ERROR:run_command_timeout"


def test_every_az_call_has_subscription(tmp_path, monkeypatch, setup_fake_az_env):
    log_file = setup_fake_az_env
    row = make_test_row()
    logger = VMLogger(row.vm_name, row.index)
    script_path = tmp_path / "remote.sh"
    script_path.write_text("#!/bin/bash\nexit 0\n")

    invoke_nowait(row, script_path, logger)
    read_results(row, script_path, run_id="preview", logger=logger)

    # name -> ID lookup, runCommand POST (subscription in its URL), reader
    calls = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(calls) == 3
    for call in calls:
        if call.startswith("rest "):
            assert " --url /subscriptions/11111111-2222-3333-4444-555555555555/" in call
        else:
            assert "--subscription sub-12345" in call
