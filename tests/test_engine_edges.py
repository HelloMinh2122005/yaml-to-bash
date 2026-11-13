"""Adversarial edge-case tests for the run engine.

Every test asserts one intended behavior of ``parts``, ``merger``,
``dispatcher``, ``post``, and ``runner`` helpers. Tests marked ``bug``
reproduce a confirmed defect.
"""

from __future__ import annotations

import base64
import csv
import json
from pathlib import Path

import pytest

from y2shared import dispatcher
from y2shared.config import Precheck, load_job
from y2shared.dispatcher import (
    AuthCircuitBreaker,
    classify_error,
    parse_run_command_output,
    read_results,
    reader_diagnostics,
)
from y2shared.inputcsv import read_input_csv
from y2shared.merger import merge_parts
from y2shared.parts import (
    get_part_paths,
    get_parts_dir,
    init_parts_dir,
    read_part_row,
    read_power,
    update_power,
    write_power_once,
)
from y2shared.post import get_vm_outcome, restore_action
from y2shared.runner import power_policy_banner
from y2shared.vmlog import VMLogger

JOB_YAML = Path("examples/linux/job.yaml")


def load_linux_job(tmp_path: Path):
    """Load the example job with outputs redirected into tmp_path."""
    job = load_job(JOB_YAML)
    job.output_csv = tmp_path / "result.csv"
    job.output_log = tmp_path / "result.log"
    rows, _, _ = read_input_csv(job.input, job=job)
    return job, rows


def prepare_parts(tmp_path: Path, total_rows: int = 3):
    job, rows = load_linux_job(tmp_path)
    parts_dir = get_parts_dir(job.output_csv)
    init_parts_dir(parts_dir, job, total_rows=total_rows, parallel=1)
    return job, rows, parts_dir


def read_merged_csv(path: Path) -> list[list[str]]:
    with open(path, "r", encoding="utf-8", newline="") as f:
        return list(csv.reader(f))


def result_message(stdout_text: str) -> str:
    return json.dumps({"value": [{"code": "StdOut", "message": stdout_text}]})


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


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
    stdout = result_message(
        "===BEGIN_RESULT_RES===\n"
        f"__Y2B_RUN__={b64('run-1')}\n"
        f"__Y2B_DONE__={b64('run-1')}\n"
        f"OS_NAME={b64('Linux')}\n"
        "===END_RESULT_RES===\n"
    )
    monkeypatch.setattr(
        dispatcher,
        "_az_run_command",
        lambda *a, **k: ("SUCCESS", 0, stdout, "", 1, 0.1),
    )
    job, rows = load_linux_job(tmp_path)
    logger = VMLogger(rows[0].vm_name, rows[0].index)

    result = read_results(rows[0], tmp_path / "collect.sh", "run-1", logger)

    assert result.status == "OK"
    assert result.vars["OS_NAME"] == "Linux"


def test_read_results_rejects_stale_run_id(tmp_path, monkeypatch):
    """branch: results from an older run are never merged into the current one."""
    stdout = result_message(
        "===BEGIN_RESULT_RES===\n"
        f"__Y2B_RUN__={b64('run-0')}\n"
        f"__Y2B_DONE__={b64('run-0')}\n"
        "===END_RESULT_RES===\n"
    )
    monkeypatch.setattr(
        dispatcher,
        "_az_run_command",
        lambda *a, **k: ("SUCCESS", 0, stdout, "", 1, 0.1),
    )
    job, rows = load_linux_job(tmp_path)
    logger = VMLogger(rows[0].vm_name, rows[0].index)

    result = read_results(rows[0], tmp_path / "collect.sh", "run-1", logger)

    assert result.status == "ERROR"
    assert result.error == "ERROR:stale_result"


def test_read_results_requires_done_marker_for_current_run(tmp_path, monkeypatch):
    """branch: __Y2B_RUN__ without a matching __Y2B_DONE__ is an incomplete remote run."""
    stdout = result_message(
        "===BEGIN_RESULT_RES===\n"
        f"__Y2B_RUN__={b64('run-1')}\n"
        "===END_RESULT_RES===\n"
    )
    monkeypatch.setattr(
        dispatcher,
        "_az_run_command",
        lambda *a, **k: ("SUCCESS", 0, stdout, "", 1, 0.1),
    )
    job, rows = load_linux_job(tmp_path)
    logger = VMLogger(rows[0].vm_name, rows[0].index)

    result = read_results(rows[0], tmp_path / "collect.sh", "run-1", logger)

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
