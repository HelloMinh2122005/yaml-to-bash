"""Adversarial edge-case tests for input parsing and validation.

Every test asserts one intended behavior of ``config``, ``inputcsv``,
``validate``, ``collectlist``, ``scan``, and the ``y2b``/``y2p`` frontend
validators. Tests marked ``bug`` reproduce a confirmed defect.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from y2b.compiler import WRITE_RESULT_PATTERN
from y2b.validate import validate_job_dict as validate_bash_job
from y2p.validate import validate_job_dict as validate_ps_job
from y2shared.collectlist import CollectList
from y2shared.config import load_job, parse_duration
from y2shared.inputcsv import VMRow, read_input_csv, select_target_rows
from y2shared.scan import scan_written_vars

CSV_HEADER = "vm_name,resource_group,subscription,os\n"
CSV_ROW = "vm-1,rg-1,Sub One,linux\n"
CSV_ROW_2 = "vm-2,rg-2,Sub One,linux\n"


def job_dict(tool: str = "y2b") -> dict:
    """Return a minimal job mapping that satisfies the shared schema."""
    if tool == "y2b":
        res_file = "/var/tmp/y2/result.res"
        log_file = "/var/tmp/y2/result.log"
    else:
        res_file = r"C:\y2\result.res"
        log_file = r"C:\y2\result.log"
    return {
        "input": "list.csv",
        "output_csv": "result.csv",
        "output_log": "result.log",
        "sleep_before_collect": "1s",
        "invoke": {
            "runcommand": {
                "res_file": res_file,
                "log_file": log_file,
                "steps": [{"name": "step-1", "script": "write_result A 1\n"}],
            }
        },
        "collect": [
            {"column": "vm_name", "source": "input.vm_name"},
            {"column": "a", "var": "A"},
        ],
    }


def write_csv(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def make_row(index: int, vm_name: str = "vm-1") -> VMRow:
    return VMRow(
        index=index,
        vm_name=vm_name,
        resource_group="rg-1",
        subscription="Sub One",
        os="linux",
        raw_data={"vm_name": vm_name},
        env={"VM_NAME": vm_name},
    )


# ---------------------------------------------------------------------------
# parse_duration — equivalence partitioning and boundaries
# ---------------------------------------------------------------------------


def test_parse_duration_compound_units_sum_correctly():
    """BVA: each unit boundary contributes the documented number of seconds."""
    assert parse_duration("1h30m15s") == 5415.0


def test_parse_duration_negative_number_rejected():
    """EP: negative durations are outside the accepted domain."""
    with pytest.raises(ValueError):
        parse_duration(-0.5)


def test_parse_duration_bool_rejected():
    """EP: booleans are not durations even though bool is an int subclass."""
    with pytest.raises(ValueError):
        parse_duration(False)


@pytest.mark.bug
def test_parse_duration_inf_string_rejected():
    """EP: 'inf' must not become an infinite collect timeout."""
    with pytest.raises(ValueError):
        parse_duration("inf")


@pytest.mark.bug
def test_parse_duration_nan_string_rejected():
    """EP: 'nan' is not a usable duration."""
    with pytest.raises(ValueError):
        parse_duration("nan")


@pytest.mark.bug
def test_parse_duration_overflow_literal_rejected():
    """BVA: a decimal literal that overflows to inf is invalid."""
    with pytest.raises(ValueError):
        parse_duration("1e309")


# ---------------------------------------------------------------------------
# Job schema validation branches
# ---------------------------------------------------------------------------


def test_validate_missing_invoke_reports_error(tmp_path):
    """branch: a job without 'invoke' is invalid."""
    raw = job_dict()
    del raw["invoke"]
    result = validate_bash_job(raw, tmp_path / "job.yaml")
    assert any(e.path == "invoke" for e in result.errors)


def test_validate_pre_run_mapping_instead_of_list_reports_error(tmp_path):
    """branch: pre_run must be a list, not a mapping."""
    raw = job_dict()
    raw["pre_run"] = {"name": "p1"}
    result = validate_bash_job(raw, tmp_path / "job.yaml")
    assert any(e.path == "pre_run" for e in result.errors)


def test_validate_empty_steps_reports_error(tmp_path):
    """branch: runcommand.steps must be a non-empty list."""
    raw = job_dict()
    raw["invoke"]["runcommand"]["steps"] = []
    result = validate_bash_job(raw, tmp_path / "job.yaml")
    assert any("steps" in e.path for e in result.errors)


def test_validate_parallel_boundaries_one_and_hundred_accepted(tmp_path):
    """BVA: 1 and 100 are the inclusive parallel bounds."""
    raw = job_dict()
    for value in (1, 100):
        raw["parallel"] = value
        assert validate_bash_job(raw, tmp_path / "job.yaml").is_valid


def test_validate_parallel_zero_and_101_rejected(tmp_path):
    """BVA: 0 and 101 fall outside the parallel bounds."""
    raw = job_dict()
    for value in (0, 101):
        raw["parallel"] = value
        result = validate_bash_job(raw, tmp_path / "job.yaml")
        assert any(e.path == "parallel" for e in result.errors)


def test_validate_pre_run_without_default_is_allowed(tmp_path):
    """branch: 'default' is optional; an unmatched result fails only at runtime."""
    raw = job_dict()
    raw["pre_run"] = [
        {
            "name": "p1",
            "script": "echo ok",
            "result": {"ok": {"log": "ok", "action": "continue"}},
        }
    ]
    assert validate_bash_job(raw, tmp_path / "job.yaml").is_valid


@pytest.mark.bug
def test_validate_step_script_must_be_string(tmp_path):
    """EP: a non-string step body cannot be compiled."""
    raw = job_dict()
    raw["invoke"]["runcommand"]["steps"][0]["script"] = 123
    result = validate_bash_job(raw, tmp_path / "job.yaml")
    assert any("script" in e.path for e in result.errors)


@pytest.mark.bug
def test_load_job_ignores_non_numeric_deprecated_run_timeout(tmp_path):
    """branch: deprecated run_timeout is ignored, so a bad value must not crash loading."""
    raw = job_dict()
    raw["run_timeout"] = "abc"
    path = tmp_path / "job.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    job = load_job(path)
    assert job.run_timeout == 1800


# ---------------------------------------------------------------------------
# Input CSV parsing
# ---------------------------------------------------------------------------


def test_read_input_csv_missing_file_reports_error(tmp_path):
    """branch: a missing inventory file is an error, not a crash."""
    rows, headers, result = read_input_csv(tmp_path / "absent.csv")
    assert rows == [] and headers == []
    assert not result.is_valid


def test_read_input_csv_empty_file_reports_error(tmp_path):
    """BVA: an empty CSV has no header row and is invalid."""
    path = write_csv(tmp_path / "list.csv", "")
    rows, _, result = read_input_csv(path)
    assert rows == []
    assert not result.is_valid


def test_read_input_csv_row_width_mismatch_reports_error(tmp_path):
    """branch: a row with the wrong column count is rejected."""
    path = write_csv(tmp_path / "list.csv", CSV_HEADER + "vm-1,rg-1\n")
    rows, _, result = read_input_csv(path)
    assert rows == []
    assert any("column count" in e.message for e in result.errors)


def test_read_input_csv_bom_header_is_stripped(tmp_path):
    """EP: a UTF-8 BOM must not corrupt the first header name."""
    path = tmp_path / "list.csv"
    path.write_bytes(("\ufeff" + CSV_HEADER + CSV_ROW).encode("utf-8"))
    rows, headers, result = read_input_csv(path)
    assert result.is_valid
    assert headers[0] == "vm_name"
    assert rows[0].vm_name == "vm-1"


@pytest.mark.bug
def test_read_input_csv_blank_os_rejected_for_windows(tmp_path):
    """EP: 'os' is a required column, so a blank value cannot be accepted."""
    path = write_csv(tmp_path / "list.csv", CSV_HEADER + "vm-1,rg-1,Sub One,\n")
    rows, _, result = read_input_csv(path, os_family="windows")
    assert not result.is_valid
    assert any("os" in e.path for e in result.errors)


@pytest.mark.bug
def test_read_input_csv_duplicate_headers_rejected(tmp_path):
    """EP: duplicate header names silently collapse in a dict and must be rejected."""
    path = write_csv(
        tmp_path / "list.csv",
        "vm_name,vm_name,resource_group,subscription,os\nvm-1,vm-2,rg-1,Sub One,linux\n",
    )
    _, headers, result = read_input_csv(path)
    assert not result.is_valid
    assert headers.count("vm_name") == 1


# ---------------------------------------------------------------------------
# Target selection boundaries
# ---------------------------------------------------------------------------


def test_select_target_rows_positive_limit_truncates_in_order(tmp_path):
    """EP: a positive limit keeps the first N rows in input order."""
    path = write_csv(tmp_path / "list.csv", CSV_HEADER + CSV_ROW + CSV_ROW_2)
    rows, _, _ = read_input_csv(path)
    assert [r.vm_name for r in select_target_rows(rows, limit=1)] == ["vm-1"]


@pytest.mark.bug
def test_select_target_rows_zero_limit_selects_nothing():
    """BVA: --limit 0 requests zero VMs, not the whole fleet."""
    rows = [make_row(1, "vm-1"), make_row(2, "vm-2")]
    assert select_target_rows(rows, limit=0) == []


@pytest.mark.bug
def test_select_target_rows_negative_limit_selects_nothing():
    """BVA: a negative --limit is invalid input and must not select the whole fleet."""
    rows = [make_row(1, "vm-1"), make_row(2, "vm-2")]
    assert select_target_rows(rows, limit=-1) == []


def test_select_target_rows_vm_filter_exact_match():
    """EP: --vm matches VM names exactly."""
    rows = [make_row(1, "vm-1"), make_row(2, "vm-2")]
    assert [r.vm_name for r in select_target_rows(rows, vm_names=["vm-2"])] == ["vm-2"]


# ---------------------------------------------------------------------------
# Collect list parsing
# ---------------------------------------------------------------------------


def test_collect_list_malformed_last_line_is_ignored(tmp_path):
    """branch: a truncated final JSON line is a crash artefact and is skipped."""
    cl = CollectList(tmp_path)
    cl.append({"index": 1, "vm_name": "vm-1"})
    with open(cl.path, "a", encoding="utf-8") as f:
        f.write('{"index": 2, "vm_name": "vm-')
    with pytest.warns(UserWarning):
        assert list(cl.load()) == [1]


def test_collect_list_malformed_non_last_line_raises(tmp_path):
    """branch: corruption in the middle of the file is not ignored."""
    cl = CollectList(tmp_path)
    with open(cl.path, "w", encoding="utf-8") as f:
        f.write("not json\n")
        f.write(json.dumps({"index": 1, "vm_name": "vm-1"}) + "\n")
    with pytest.raises(ValueError):
        cl.load()


@pytest.mark.bug
def test_collect_list_non_integer_last_index_is_ignored(tmp_path):
    """EP: a non-integer index on the final line is malformed and must be skipped."""
    cl = CollectList(tmp_path)
    with open(cl.path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"index": "abc", "vm_name": "vm-1"}) + "\n")
    with pytest.warns(UserWarning):
        assert cl.load() == {}


@pytest.mark.bug
def test_collect_list_float_index_is_rejected(tmp_path):
    """EP: a fractional index must not be truncated into a different VM's slot."""
    cl = CollectList(tmp_path)
    with open(cl.path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"index": 2.9, "vm_name": "vm-2"}) + "\n")
    with pytest.raises(ValueError):
        cl.load()


def test_collect_list_failed_record_removes_earlier_entry(tmp_path):
    """branch: failed=true supersedes an earlier record for the same index."""
    cl = CollectList(tmp_path)
    cl.append({"index": 1, "vm_name": "vm-1"})
    cl.append({"index": 1, "vm_name": "vm-1", "failed": True})
    assert cl.load() == {}


@pytest.mark.bug
def test_collect_list_subset_load_ignores_records_outside_targets(tmp_path):
    """branch: resuming a narrowed --vm/--limit subset must ignore other records."""
    cl = CollectList(tmp_path)
    cl.append({"index": 1, "vm_name": "vm-1"})
    cl.append({"index": 2, "vm_name": "vm-2"})
    assert list(cl.load([make_row(2, "vm-2")])) == [2]


# ---------------------------------------------------------------------------
# write_result scanning
# ---------------------------------------------------------------------------


def test_scan_written_vars_finds_active_call():
    """EP: a plain write_result call is recognised."""
    assert scan_written_vars('write_result OS_NAME "Linux"', WRITE_RESULT_PATTERN) == {"OS_NAME"}


@pytest.mark.bug
def test_scan_written_vars_ignores_commented_call():
    """branch: a commented-out write_result does not write anything."""
    body = '# write_result OS_NAME "Linux"\n'
    assert scan_written_vars(body, WRITE_RESULT_PATTERN) == set()


@pytest.mark.bug
def test_scan_written_vars_ignores_quoted_call():
    """branch: write_result inside a string literal is not an executed call."""
    body = 'echo "write_result OS_NAME Linux"\n'
    assert scan_written_vars(body, WRITE_RESULT_PATTERN) == set()


# ---------------------------------------------------------------------------
# Frontend path rules
# ---------------------------------------------------------------------------


def test_bash_validator_rejects_windows_drive_res_file(tmp_path):
    """EP: y2b runcommand paths are POSIX; a drive-letter path is invalid."""
    raw = job_dict("y2b")
    raw["invoke"]["runcommand"]["res_file"] = r"C:\tmp\result.res"
    result = validate_bash_job(raw, tmp_path / "job.yaml")
    assert any("res_file" in e.path for e in result.errors)


def test_powershell_validator_rejects_posix_res_file(tmp_path):
    """EP: y2p runcommand paths need a Windows drive letter."""
    raw = job_dict("y2p")
    raw["invoke"]["runcommand"]["res_file"] = "/var/tmp/result.res"
    result = validate_ps_job(raw, tmp_path / "job.yaml")
    assert any("res_file" in e.path for e in result.errors)


def test_powershell_validator_rejects_here_string_terminator(tmp_path):
    """branch: a step line starting with '@ would close the generated here-string."""
    raw = job_dict("y2p")
    raw["invoke"]["runcommand"]["steps"][0]["script"] = "Get-Date\n'@\nWrite-Output x\n"
    result = validate_ps_job(raw, tmp_path / "job.yaml")
    assert any("here-string" in e.message for e in result.errors)
