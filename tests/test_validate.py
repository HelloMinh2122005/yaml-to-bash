"""Unit tests for validate.py covering all validation rules with YAML paths."""

from pathlib import Path
import pytest
import yaml

from y2b.validate import validate_job_dict, validate_job_file


def get_valid_raw() -> dict:
    with open("examples/hip-compliance/job.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_valid_example():
    raw = get_valid_raw()
    res = validate_job_dict(raw, "examples/hip-compliance/job.yaml")
    assert res.is_valid
    assert len(res.errors) == 0


def test_missing_root_fields():
    raw = get_valid_raw()
    del raw["input"]
    del raw["output_csv"]
    del raw["output_log"]
    res = validate_job_dict(raw, "examples/hip-compliance/job.yaml")
    assert not res.is_valid
    error_paths = [e.path for e in res.errors]
    assert "input" in error_paths
    assert "output_csv" in error_paths
    assert "output_log" in error_paths


def test_invalid_actions_and_parallel():
    raw = get_valid_raw()
    raw["actions"] = "invalid_action"
    raw["parallel"] = 0
    res = validate_job_dict(raw, "examples/hip-compliance/job.yaml")
    assert not res.is_valid
    error_paths = [e.path for e in res.errors]
    assert "actions" in error_paths
    assert "parallel" in error_paths

    raw["parallel"] = 101
    res = validate_job_dict(raw, "examples/hip-compliance/job.yaml")
    assert any(e.path == "parallel" for e in res.errors)


def test_precheck_duplicate_name_and_missing_script():
    raw = get_valid_raw()
    raw["invoke"]["precheck"].append({
        "name": "check-if-vm-is-running",  # Duplicate name
        "result": {"0": {"log": "ok", "action": "continue"}}
        # Missing both script and script_path
    })
    res = validate_job_dict(raw, "examples/hip-compliance/job.yaml")
    assert not res.is_valid
    errors_str = [str(e) for e in res.errors]
    assert any("invoke.precheck[2].name: Duplicate precheck name" in s for s in errors_str)
    assert any("invoke.precheck[2]: Precheck must define exactly one of 'script' or 'script_path'" in s for s in errors_str)


def test_precheck_invalid_action():
    raw = get_valid_raw()
    # In raw YAML, 0 is parsed as integer 0
    raw["invoke"]["precheck"][0]["result"][0]["action"] = "restart_vm"
    res = validate_job_dict(raw, "examples/hip-compliance/job.yaml")
    assert not res.is_valid
    assert any("invoke.precheck[0].result[0].action" in e.path for e in res.errors)


def test_runcommand_relative_paths():
    raw = get_valid_raw()
    raw["invoke"]["runcommand"]["res_file"] = "relative/result.res"
    raw["invoke"]["runcommand"]["log_file"] = "./relative/result.log"
    res = validate_job_dict(raw, "examples/hip-compliance/job.yaml")
    assert not res.is_valid
    error_paths = [e.path for e in res.errors]
    assert "invoke.runcommand.res_file" in error_paths
    assert "invoke.runcommand.log_file" in error_paths


def test_step_duplicate_name_and_var():
    raw = get_valid_raw()
    raw["invoke"]["runcommand"]["steps"].append({
        "name": "check-hip-version",  # Duplicate step name
        "var": "HIP_VERSION",         # Duplicate step var
        "script": "echo 1",
    })
    res = validate_job_dict(raw, "examples/hip-compliance/job.yaml")
    assert not res.is_valid
    errors_str = [str(e) for e in res.errors]
    assert any("invoke.runcommand.steps[3].name: Duplicate step name" in s for s in errors_str)
    assert any("invoke.runcommand.steps[3].var: Duplicate variable name" in s for s in errors_str)


def test_step_invalid_var_regex():
    raw = get_valid_raw()
    raw["invoke"]["runcommand"]["steps"][0]["var"] = "hip-version-lower"
    res = validate_job_dict(raw, "examples/hip-compliance/job.yaml")
    assert not res.is_valid
    assert any("invoke.runcommand.steps[0].var" in e.path for e in res.errors)


def test_step_missing_script_file():
    raw = get_valid_raw()
    raw["invoke"]["runcommand"]["steps"][2]["script_path"] = "./scripts/non_existent.sh"
    res = validate_job_dict(raw, "examples/hip-compliance/job.yaml")
    assert not res.is_valid
    assert any("invoke.runcommand.steps[2].script_path" in e.path for e in res.errors)


def test_collect_unknown_var_and_duplicate_col():
    raw = get_valid_raw()
    raw["collect"].append({
        "column": "vm_name",  # Duplicate column name
        "var": "UNDEFINED_VAR",
    })
    res = validate_job_dict(raw, "examples/hip-compliance/job.yaml")
    assert not res.is_valid
    errors_str = [str(e) for e in res.errors]
    assert any("collect[8].column: Duplicate column name" in s for s in errors_str)
    assert any("collect[8].var: Referenced variable 'UNDEFINED_VAR' is not declared" in s for s in errors_str)


def test_collect_invalid_source_format():
    raw = get_valid_raw()
    raw["collect"][0]["source"] = "csv.vm_name"  # Must start with input.
    res = validate_job_dict(raw, "examples/hip-compliance/job.yaml")
    assert not res.is_valid
    assert any("collect[0].source" in e.path for e in res.errors)
