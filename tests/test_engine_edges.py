"""Adversarial edge-case tests for the run engine.

Every test asserts one intended behavior of ``parts``, ``merger``,
``dispatcher``, ``post``, and ``runner`` helpers. Tests marked ``bug``
reproduce a confirmed defect.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import threading
from pathlib import Path

import pytest
import yaml

from tests.helpers_edges import load_linux_job, make_vm_row
from y2b.profile import PROFILE as B_PROFILE
from y2shared import collector, dispatcher, post, precheck, runner
from y2shared.collectlist import CollectList
from y2shared.config import Column, Job, PostRun, Precheck
from y2shared.dispatcher import (
    AuthAbortedError,
    AuthCircuitBreaker,
    ReadResult,
    RemoteResult,
    classify_error,
    parse_run_command_output,
    read_results,
    reader_diagnostics,
)
from y2shared.inputcsv import VMRow
from y2shared.merger import merge_parts
from y2shared.parts import (
    get_part_paths,
    get_parts_dir,
    init_parts_dir,
    power_path,
    read_part_row,
    read_power,
    update_power,
    write_part_files,
    write_power_once,
)
from y2shared.post import get_vm_outcome, restore_action, run_post_phase
from y2shared.precheck import PrecheckEngine
from y2shared.runner import RunState, power_policy_banner, run_collect_phase, run_job
from y2shared.shell import ShellResult
from y2shared.vmlog import VMLogger


def prepare_parts(tmp_path: Path, total_rows: int = 3) -> tuple[Job, list[VMRow], Path]:
    """Load the Linux example job and initialize its parts directory under tmp_path."""
    job, rows = load_linux_job(tmp_path)
    parts_dir = get_parts_dir(job.output_csv)
    init_parts_dir(parts_dir, job, total_rows=total_rows, parallel=1)
    return job, rows, parts_dir


def read_merged_csv(path: Path) -> list[list[str]]:
    """Read all rows from a merged CSV output file."""
    with open(path, "r", encoding="utf-8", newline="") as f:
        return list(csv.reader(f))


def result_message(stdout_text: str) -> str:
    """Wrap stdout_text in the Azure Run Command JSON StdOut envelope."""
    return json.dumps({"value": [{"code": "StdOut", "message": stdout_text}]})


def b64(text: str) -> str:
    """Base64-encode UTF-8 text as an ASCII string."""
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _run_read_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout_body: str,
) -> ReadResult:
    """Execute read_results with a mocked Azure reader returning stdout_body."""
    stdout = result_message(stdout_body)
    monkeypatch.setattr(
        dispatcher,
        "_az_run_command",
        lambda *a, **k: ("SUCCESS", 0, stdout, "", 1, 0.1),
    )
    _, rows = load_linux_job(tmp_path)
    logger = VMLogger(rows[0].vm_name, rows[0].index)
    return read_results(rows[0], tmp_path / "collect.sh", "run-1", logger)


# ---------------------------------------------------------------------------
# parts.py
# ---------------------------------------------------------------------------


def test_init_parts_dir_refuses_existing_directory_without_resume(tmp_path):
    """branch: a fresh run must not silently reuse a previous parts folder."""
    job, rows = load_linux_job(tmp_path)
    parts_dir = get_parts_dir(job.output_csv)
    init_parts_dir(parts_dir, job, total_rows=len(rows), parallel=1)
    with pytest.raises(FileExistsError):
        init_parts_dir(parts_dir, job, total_rows=len(rows), parallel=1)


@pytest.mark.bug
def test_resume_without_stored_run_id_fails_instead_of_minting_one(tmp_path):
    """branch: run.json missing run_id must not invent an id the remote scripts never saw."""
    job, rows = load_linux_job(tmp_path)
    parts_dir = get_parts_dir(job.output_csv)
    init_parts_dir(parts_dir, job, total_rows=len(rows), parallel=1, run_id="run-1")
    info = json.loads((parts_dir / "run.json").read_text(encoding="utf-8"))
    info.pop("run_id")
    (parts_dir / "run.json").write_text(json.dumps(info), encoding="utf-8")

    with pytest.raises((RuntimeError, ValueError)):
        init_parts_dir(parts_dir, job, total_rows=len(rows), parallel=1, resume=True)


def test_write_power_once_is_create_if_absent(tmp_path):
    """branch: O_EXCL semantics; the second write must not overwrite the recorded state."""
    power_file = tmp_path / "0001-vm.power"
    assert write_power_once(power_file, {"prev_state": "PowerState/running"}) is True
    assert write_power_once(power_file, {"prev_state": "PowerState/stopped"}) is False
    assert read_power(power_file) == {"prev_state": "PowerState/running"}


def test_read_power_returns_none_for_corrupt_json(tmp_path):
    """branch: an unreadable power file is treated as absent, not as a crash."""
    power_file = tmp_path / "0001-vm.power"
    power_file.write_text('{"prev_state": ', encoding="utf-8")
    assert read_power(power_file) is None


@pytest.mark.bug
def test_update_power_tolerates_non_object_json(tmp_path):
    """EP: a JSON array is not a valid power record and must not crash the post phase."""
    power_file = tmp_path / "0001-vm.power"
    power_file.write_text('["PowerState/stopped"]', encoding="utf-8")
    data = update_power(power_file, post="done")
    assert data["post"] == "done"


def test_read_part_row_returns_first_non_empty_row(tmp_path):
    """BVA: a leading blank line in a part file must be skipped, not returned."""
    part = tmp_path / "part.csv"
    part.write_text("\nlin01,rg,sub\n", encoding="utf-8")
    assert read_part_row(part) == ["lin01", "rg", "sub"]


# ---------------------------------------------------------------------------
# merger.py
# ---------------------------------------------------------------------------


@pytest.mark.bug
def test_merge_classifies_outcome_by_prefix_not_substring(tmp_path):
    """branch: a passthrough value containing 'SKIPPED:' is data, not an outcome."""
    job, rows, parts_dir = prepare_parts(tmp_path)
    csv_part, _ = get_part_paths(parts_dir, rows[0], 3)
    csv_part.write_text(
        "lin01,rg-lab-10vms,sub,note SKIPPED: retried,rhel9,9.4,5.14.0,1200,0.05,15%,PASS,HEALTHY\n",
        encoding="utf-8",
    )

    counts = merge_parts(job, rows, parts_dir, keep_parts=True)

    assert counts["SKIPPED"] == 0
    assert counts["OK"] == 1


@pytest.mark.bug
def test_merge_synthesizes_no_result_for_blank_part_row(tmp_path):
    """BVA: a zero-column part row cannot be represented and must become ERROR:no_result."""
    job, rows, parts_dir = prepare_parts(tmp_path)
    csv_part, _ = get_part_paths(parts_dir, rows[0], 3)
    csv_part.write_text("\n", encoding="utf-8")

    merge_parts(job, rows, parts_dir, keep_parts=True)

    merged = read_merged_csv(job.output_csv)
    header, data_rows = merged[0], merged[1:]
    assert len(data_rows) == 3
    for data_row in data_rows:
        assert len(data_row) == len(header)


def test_merge_removes_parts_dir_unless_keep_parts(tmp_path):
    """branch: a successful merge cleans up the parts folder by default."""
    job, rows, parts_dir = prepare_parts(tmp_path)
    merge_parts(job, rows, parts_dir, keep_parts=False)
    assert not parts_dir.exists()


# ---------------------------------------------------------------------------
# dispatcher.py
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stderr,expected",
    [
        ("TooManyRequests", "throttled"),
        ("OperationNotAllowed: conflict", "busy"),
        ("VMAgentStatusCommunicationError", "agent"),
        ("ResourceNotFound", "missing"),
        ("AuthorizationFailed", "auth"),
        ("something unexpected", "unknown"),
    ],
)
def test_classify_error_maps_each_documented_pattern(stderr, expected):
    """EP: each error family maps to its documented class."""
    assert classify_error(stderr, rc=1) == expected


def test_parse_output_end_marker_without_begin_reports_truncation():
    """branch: a cut-off BEGIN marker is reported as a truncated result block."""
    message = result_message("partial output\n===END_RESULT_RES===\nA=MQ==\n")
    vars_dict, error, _ = parse_run_command_output(message)
    assert vars_dict == {}
    assert error == "ERROR:result_truncated"


@pytest.mark.bug
def test_parse_output_begin_marker_without_end_reports_truncation():
    """branch: a cut-off END marker leaves a partial block and must be reported."""
    message = result_message("===BEGIN_RESULT_RES===\nA=MQ==\n")
    _, error, _ = parse_run_command_output(message)
    assert error == "ERROR:result_truncated"


def test_read_results_accepts_matching_run_and_done_markers(tmp_path, monkeypatch):
    """branch: matching __Y2B_RUN__/__Y2B_DONE__ markers yield OK and decoded vars."""
    result = _run_read_results(
        tmp_path,
        monkeypatch,
        "===BEGIN_RESULT_RES===\n"
        f"__Y2B_RUN__={b64('run-1')}\n"
        f"__Y2B_DONE__={b64('run-1')}\n"
        f"OS_NAME={b64('Linux')}\n"
        "===END_RESULT_RES===\n",
    )
    assert result.status == "OK"
    assert result.vars["OS_NAME"] == "Linux"


def test_read_results_rejects_stale_run_id(tmp_path, monkeypatch):
    """branch: results from an older run are never merged into the current one."""
    result = _run_read_results(
        tmp_path,
        monkeypatch,
        "===BEGIN_RESULT_RES===\n"
        f"__Y2B_RUN__={b64('run-0')}\n"
        f"__Y2B_DONE__={b64('run-0')}\n"
        "===END_RESULT_RES===\n",
    )
    assert result.status == "ERROR"
    assert result.error == "ERROR:stale_result"


def test_read_results_requires_done_marker_for_current_run(tmp_path, monkeypatch):
    """branch: __Y2B_RUN__ without a matching __Y2B_DONE__ is an incomplete remote run."""
    result = _run_read_results(
        tmp_path,
        monkeypatch,
        "===BEGIN_RESULT_RES===\n"
        f"__Y2B_RUN__={b64('run-1')}\n"
        "===END_RESULT_RES===\n",
    )
    assert result.status == "ERROR"
    assert result.error == "ERROR:remote_incomplete"


def test_reader_diagnostics_keeps_only_reader_lines():
    """EP: unrelated console noise is excluded from the diagnostics suffix."""
    message = "y2-reader: res_file size=0\nnoise line\ny2-reader: boot=2026-01-01"
    assert reader_diagnostics(message) == "y2-reader: res_file size=0; y2-reader: boot=2026-01-01"


def test_reader_diagnostics_placeholder_when_absent():
    """BVA: no reader lines produces the documented placeholder."""
    assert reader_diagnostics("plain output") == "y2-reader: no diagnostics in reader output"


def test_auth_circuit_breaker_trips_at_threshold_and_resets_on_success():
    """BVA: the breaker trips at the threshold and any success clears the count."""
    breaker = AuthCircuitBreaker(threshold=3)
    breaker.record_auth_error()
    breaker.record_auth_error()
    assert breaker.is_tripped is False

    breaker.record_success()
    breaker.record_auth_error()
    breaker.record_auth_error()
    assert breaker.is_tripped is False

    breaker.record_auth_error()
    assert breaker.is_tripped is True


# ---------------------------------------------------------------------------
# post.py / runner.py
# ---------------------------------------------------------------------------


def test_get_vm_outcome_uses_prefix_not_substring():
    """branch: only a leading marker denotes an outcome."""
    assert get_vm_outcome(["note ERROR: inner", "OK"]) == "OK"
    assert get_vm_outcome(["fine", "ERROR:boom"]) == "ERROR:boom"


def test_restore_action_powers_off_stopped_vms_only():
    """EP: stopped/stopping states map to powerOff; every other state is deallocated."""
    assert restore_action("PowerState/stopped") == "powerOff"
    assert restore_action("VM stopping") == "powerOff"
    assert restore_action("PowerState/running") == "deallocate"
    assert restore_action("PowerState/deallocated") == "deallocate"


def test_power_policy_banner_prefers_stop_over_start_stop(tmp_path):
    """branch: 'stop' is the dominant policy and must be reported first."""
    job, _ = load_linux_job(tmp_path)
    job.pre_run = [
        Precheck(
            name="power",
            result={
                "a": {"action": "stop"},
                "b": {"action": "start-stop"},
            },
        )
    ]
    banner = power_policy_banner(job, total_vms=4)
    assert banner is not None
    assert "'stop'" in banner


def test_power_policy_banner_none_without_power_actions(tmp_path):
    """BVA: a job whose pre_run actions never touch power prints no banner."""
    job, _ = load_linux_job(tmp_path)
    job.pre_run = [Precheck(name="gate", result={"a": {"action": "continue"}})]
    assert power_policy_banner(job, total_vms=4) is None


# ---------------------------------------------------------------------------
# collector.py
# ---------------------------------------------------------------------------


def test_collect_for_vm_outcome_marker_skips_scripts_and_fills_passthrough(tmp_path):
    """branch: outcome_marker fills input.* columns and propagates the marker without scripts."""
    job, rows = load_linux_job(tmp_path)
    job.collect = [
        Column(column="vm_name", source="input.vm_name"),
        Column(column="calc", script="exit 1"),
    ]
    assert collect_for_vm(job, rows[0], outcome_marker="SKIPPED:pre") == [
        rows[0].vm_name,
        "SKIPPED:pre",
    ]


def test_collect_for_vm_failed_step_var_reports_step_error(tmp_path):
    """branch: missing var from a non-ok step resolves to '__ERROR__ <status>'."""
    job, rows = load_linux_job(tmp_path)
    job.collect = [Column(column="val", var="OS_NAME")]
    remote = RemoteResult(vars={"__Y2B_STEP_1__": "rc=2"})
    assert collect_for_vm(job, rows[0], remote_result=remote, var_to_step={"OS_NAME": 1}) == [
        "__ERROR__ rc=2"
    ]


def test_collect_for_vm_unwritten_var_without_failed_step_reports_missing(tmp_path):
    """branch: missing var from an ok step resolves to '__MISSING__'."""
    job, rows = load_linux_job(tmp_path)
    job.collect = [Column(column="val", var="OS_NAME")]
    remote = RemoteResult(vars={"__Y2B_STEP_1__": "ok"})
    assert collect_for_vm(job, rows[0], remote_result=remote, var_to_step={"OS_NAME": 1}) == [
        "__MISSING__"
    ]


def test_collect_for_vm_computed_column_exports_env_to_next_column(tmp_path, monkeypatch):
    """branch: computed columns export COL_<NAME> to subsequent computed columns."""
    job, rows = load_linux_job(tmp_path)
    job.collect = [
        Column(column="first", script="echo one"),
        Column(column="second", script="echo $COL_FIRST"),
    ]
    seen_envs: list[dict[str, str]] = []

    def fake_run_bash(script: str, env: dict[str, str] | None = None, **kwargs: object) -> ShellResult:
        seen_envs.append(dict(env or {}))
        return ShellResult(rc=0, stdout="alpha\n" if len(seen_envs) == 1 else "beta\n", stderr="")

    monkeypatch.setattr(collector, "run_bash", fake_run_bash)
    assert collect_for_vm(job, rows[0]) == ["alpha", "beta"]
    assert seen_envs[1]["COL_FIRST"] == "alpha"


def test_collect_for_vm_computed_column_timeout_reports_error(tmp_path, monkeypatch):
    """branch: a timed-out computed column yields 'ERROR:column:timeout'."""
    job, rows = load_linux_job(tmp_path)
    job.collect = [Column(column="slow", script="sleep 5", timeout=1)]
    monkeypatch.setattr(
        collector,
        "run_bash",
        lambda *a, **k: ShellResult(rc=124, stdout="", stderr="[TIMEOUT]", timed_out=True),
    )
    assert collect_for_vm(job, rows[0]) == ["ERROR:column:timeout"]


# ---------------------------------------------------------------------------
# precheck.py
# ---------------------------------------------------------------------------


def test_precheck_subscription_cache_reuses_first_vm_result(tmp_path, monkeypatch):
    """branch: cache='subscription' executes the precheck once per subscription."""
    job, _ = load_linux_job(tmp_path)
    job.pre_run = [
        Precheck(
            name="sub-gate",
            script="echo ok",
            cache="subscription",
            result={"ok": {"action": "continue", "log": "ok"}},
        )
    ]
    calls: list[str] = []

    def fake_run_bash(script: str, env: dict[str, str] | None = None, **kwargs: object) -> ShellResult:
        calls.append((env or {}).get("VM_NAME", ""))
        return ShellResult(rc=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(precheck, "run_bash", fake_run_bash)
    engine = PrecheckEngine()
    vm1 = make_vm_row(1, "vm-1", subscription="Sub One")
    vm2 = make_vm_row(2, "vm-2", subscription="Sub One")

    assert engine.run_prechecks_for_vm(job, vm1, VMLogger(vm1.vm_name, vm1.index)) == (True, None)
    assert engine.run_prechecks_for_vm(job, vm2, VMLogger(vm2.vm_name, vm2.index)) == (True, None)
    assert calls == ["vm-1"]


def test_precheck_unmatched_without_default_returns_error(tmp_path, monkeypatch):
    """branch: unmatched stdout with no default key returns ERROR:precheck_unmatched."""
    job, rows = load_linux_job(tmp_path)
    job.pre_run = [
        Precheck(
            name="gate",
            script="echo unexpected",
            result={"ok": {"action": "continue", "log": "ok"}},
        )
    ]
    monkeypatch.setattr(
        precheck,
        "run_bash",
        lambda *a, **k: ShellResult(rc=0, stdout="unexpected\n", stderr=""),
    )
    engine = PrecheckEngine()
    assert engine.run_prechecks_for_vm(job, rows[0], VMLogger(rows[0].vm_name, rows[0].index)) == (
        False,
        "ERROR:precheck_unmatched:gate",
    )


def test_precheck_collect_phase_skip_returns_error_marker(tmp_path, monkeypatch):
    """branch: action='skip' in collect phase returns ERROR:not_running_at_collect."""
    job, rows = load_linux_job(tmp_path)
    job.pre_run = [
        Precheck(
            name="state",
            phases=["collect"],
            script="echo stopped",
            result={"stopped": {"action": "skip", "log": "stopped"}},
        )
    ]
    monkeypatch.setattr(
        precheck,
        "run_bash",
        lambda *a, **k: ShellResult(rc=0, stdout="stopped\n", stderr=""),
    )
    engine = PrecheckEngine()
    assert engine.run_prechecks_for_vm(
        job, rows[0], VMLogger(rows[0].vm_name, rows[0].index), phase="collect"
    ) == (False, "ERROR:not_running_at_collect:state")


@pytest.mark.bug
def test_precheck_timed_out_script_returns_error(tmp_path, monkeypatch):
    """branch: a timed-out precheck must fail even when partial output or default exists."""
    job, rows = load_linux_job(tmp_path)
    job.pre_run = [
        Precheck(
            name="slow-check",
            script="sleep 5",
            timeout=1,
            result={"default": {"action": "continue", "log": "fallback"}},
        )
    ]
    monkeypatch.setattr(
        precheck,
        "run_bash",
        lambda *a, **k: ShellResult(rc=124, stdout="", stderr="[TIMEOUT]", timed_out=True),
    )
    engine = PrecheckEngine()
    proceed, marker = engine.run_prechecks_for_vm(
        job, rows[0], VMLogger(rows[0].vm_name, rows[0].index)
    )
    assert proceed is False
    assert marker is not None and marker.startswith("ERROR:")


# ---------------------------------------------------------------------------
# runner.py / post.py adversarial resume, auth-abort, power, and timeout paths
# ---------------------------------------------------------------------------


@pytest.mark.bug
def test_resume_retries_error_part_instead_of_skipping(tmp_path, monkeypatch):
    """branch: --resume must retry recoverable ERROR:* parts per USER_GUIDE advice."""
    csv_path = tmp_path / "list.csv"
    csv_path.write_text("vm_name,resource_group,subscription,os\nlin01,rg-1,Sub One,linux\n", encoding="utf-8")
    job_yaml = tmp_path / "job.yaml"
    job_yaml.write_text(
        yaml.safe_dump(
            {
                "input": str(csv_path),
                "output_csv": str(tmp_path / "result.csv"),
                "output_log": str(tmp_path / "result.log"),
                "sleep_before_collect": "0s",
                "invoke": {
                    "runcommand": {
                        "res_file": "/var/tmp/result.res",
                        "log_file": "/var/tmp/result.log",
                        "steps": [{"name": "step-1", "script": "write_result A 1\n"}],
                    }
                },
                "collect": [{"column": "vm_name", "source": "input.vm_name"}, {"column": "a", "var": "A"}],
            }
        ),
        encoding="utf-8",
    )
    target_row = make_vm_row(1, "lin01")
    from y2shared.config import load_job

    temp_job = load_job(job_yaml)
    parts_dir = get_parts_dir(temp_job.output_csv)
    init_parts_dir(parts_dir, temp_job, total_rows=1, parallel=1, run_id="run-1")
    CollectList(parts_dir).ensure_exists()
    part_csv, part_log = get_part_paths(parts_dir, target_row, 1)
    part_csv.write_text("lin01,ERROR:run_command_busy\n", encoding="utf-8")
    part_log.write_text("ERROR:run_command_busy\n", encoding="utf-8")

    invoked: list[str] = []
    monkeypatch.setattr(runner, "check_host_bash", lambda: None)
    monkeypatch.setattr(runner, "start_caffeinate", lambda: None)
    monkeypatch.setattr(runner, "stop_caffeinate", lambda p: None)
    monkeypatch.setattr(runner, "raise_nofile", lambda required_fds: (1024, 1024))
    monkeypatch.setattr(
        runner,
        "invoke_nowait",
        lambda row, **kw: invoked.append(row.vm_name) or dispatcher.InvokeResult(success=True),
    )
    monkeypatch.setattr(
        runner,
        "read_results",
        lambda *a, **kw: ReadResult(status="OK", vars={"A": "1"}),
    )

    args = argparse.Namespace(
        job_yaml=str(job_yaml),
        vm=[],
        limit=None,
        parallel=1,
        dry_run=False,
        resume=True,
        keep_parts=True,
        phase="all",
        yes=True,
    )
    run_job(args, profile=B_PROFILE)
    assert invoked == ["lin01"]


@pytest.mark.bug
def test_post_phase_corrupt_power_is_not_overwritten_as_done(tmp_path, monkeypatch):
    """branch: corrupt .power must not silently skip restoration and mark post='done'."""
    job, rows, parts_dir = prepare_parts(tmp_path, total_rows=1)
    target = rows[0]
    job.pre_run = [Precheck(name="p", result={"x": {"action": "start-stop"}})]
    part_csv, part_log = get_part_paths(parts_dir, target, 1)
    part_csv.write_text(f"{target.vm_name},rg,sub,ok\n", encoding="utf-8")
    part_log.write_text("ok\n", encoding="utf-8")
    p_file = power_path(parts_dir, target, 1)
    p_file.write_text("{corrupt", encoding="utf-8")

    monkeypatch.setattr(post, "power_action_nowait", lambda **kw: (True, ""))
    run_post_phase(
        job=job,
        target_rows=[target],
        collect_records={target.index: {"index": target.index, "vm_name": target.vm_name}},
        parts_dir=parts_dir,
        run_id="run-1",
        parallel=1,
        circuit_breaker=AuthCircuitBreaker(threshold=5),
    )
    stored = read_power(p_file)
    assert stored is None or stored.get("post") != "done"


@pytest.mark.bug
def test_collect_phase_auth_abort_preserves_already_completed_future(tmp_path):
    """branch: auth abort in one future must still persist sibling completed results."""
    job, rows, parts_dir = prepare_parts(tmp_path, total_rows=2)
    job.sleep_before_collect = 0.0
    targets = rows[:2]
    work_set = [
        (r, {"index": r.index, "vm_name": r.vm_name, "invoked_at": "2026-01-01T00:00:00+00:00", "log": []})
        for r in targets
    ]
    interrupted_event = threading.Event()
    both_started = threading.Barrier(2, timeout=2.0)

    def fake_read_fn(row: VMRow, **kw: object) -> ReadResult:
        both_started.wait()
        if row.index == targets[0].index:
            raise AuthAbortedError("auth failed")
        interrupted_event.wait(timeout=2.0)
        return ReadResult(status="OK", vars={"KERNEL_COUNT": "3"})

    run_state = RunState(total_vms=2)
    run_collect_phase(
        job=job,
        work_set=work_set,
        parts_dir=parts_dir,
        collect_script_path=tmp_path / "collect.sh",
        run_id="run-1",
        total_rows=2,
        parallel=2,
        circuit_breaker=AuthCircuitBreaker(threshold=1),
        run_state=run_state,
        interrupted_event=interrupted_event,
        read_fn=fake_read_fn,
    )
    csv_part_2, _ = get_part_paths(parts_dir, targets[1], 2)
    assert csv_part_2.is_file()


@pytest.mark.bug
def test_post_phase_runs_hooks_for_invoke_failed_vm_without_power_file(tmp_path, monkeypatch):
    """branch: an invoke-failed VM with an ERROR part row still runs post_run hooks."""
    job, rows, parts_dir = prepare_parts(tmp_path, total_rows=1)
    target = rows[0]
    job.post_run = [PostRun(name="audit", script="echo ok")]
    part_csv, part_log = get_part_paths(parts_dir, target, 1)
    part_csv.write_text(f"{target.vm_name},ERROR:vm_not_found\n", encoding="utf-8")
    part_log.write_text("ERROR:vm_not_found\n", encoding="utf-8")

    monkeypatch.setattr(
        post,
        "run_bash",
        lambda *a, **k: ShellResult(rc=0, stdout="ok\n", stderr=""),
    )
    res = run_post_phase(
        job=job,
        target_rows=[target],
        collect_records={},
        parts_dir=parts_dir,
        run_id="run-1",
        parallel=1,
        circuit_breaker=AuthCircuitBreaker(threshold=5),
    )
    assert res.hooks_ok == 1


@pytest.mark.bug
def test_post_phase_marks_timed_out_hook_with_default_as_failed(tmp_path, monkeypatch):
    """branch: a timed-out post_run hook must fail instead of matching 'default'."""
    job, rows, parts_dir = prepare_parts(tmp_path, total_rows=1)
    target = rows[0]
    job.post_run = [
        PostRun(
            name="audit",
            script="sleep 5",
            timeout=1,
            result={"default": {"log": "fallback", "status": "ok"}},
        )
    ]
    part_csv, part_log = get_part_paths(parts_dir, target, 1)
    part_csv.write_text(f"{target.vm_name},ok\n", encoding="utf-8")
    part_log.write_text("ok\n", encoding="utf-8")

    monkeypatch.setattr(
        post,
        "run_bash",
        lambda *a, **k: ShellResult(rc=124, stdout="", stderr="[TIMEOUT]", timed_out=True),
    )
    res = run_post_phase(
        job=job,
        target_rows=[target],
        collect_records={target.index: {"index": target.index, "vm_name": target.vm_name}},
        parts_dir=parts_dir,
        run_id="run-1",
        parallel=1,
        circuit_breaker=AuthCircuitBreaker(threshold=5),
    )
    assert res.hooks_failed == 1
