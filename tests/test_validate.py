"""Unit tests for validate.py covering all validation rules with YAML paths."""

from pathlib import Path
import pytest
import yaml

from y2b.validate import validate_job_dict, validate_job_file


def get_valid_raw() -> dict:
    with open("examples/linux/job.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_valid_example():
    raw = get_valid_raw()
    res = validate_job_dict(raw, "examples/linux/job.yaml")
    assert res.is_valid
    assert len(res.errors) == 0


def test_missing_root_fields():
    raw = get_valid_raw()
    del raw["input"]
    del raw["output_csv"]
    del raw["output_log"]
    res = validate_job_dict(raw, "examples/linux/job.yaml")
    assert not res.is_valid
    error_paths = [e.path for e in res.errors]
    assert "input" in error_paths
    assert "output_csv" in error_paths
    assert "output_log" in error_paths


def test_removed_root_actions_and_invalid_parallel():
    raw = get_valid_raw()
    raw["actions"] = "skip"  # root 'actions' was removed: any value is an error
    raw["parallel"] = 0
    res = validate_job_dict(raw, "examples/linux/job.yaml")
    assert not res.is_valid
    error_paths = [e.path for e in res.errors]
    assert "actions" in error_paths
    assert "parallel" in error_paths

    raw["parallel"] = 101
    res = validate_job_dict(raw, "examples/linux/job.yaml")
    assert any(e.path == "parallel" for e in res.errors)


def test_precheck_duplicate_name_and_missing_script():
    raw = get_valid_raw()
    pre_list = raw.get("pre_run") if "pre_run" in raw else raw["invoke"]["precheck"]
    pre_path = "pre_run" if "pre_run" in raw else "invoke.precheck"
    pre_list.append({
        "name": "check-if-vm-is-running",  # Duplicate name
        "result": {"0": {"log": "ok", "action": "continue"}}
        # Missing both script and script_path
    })
    res = validate_job_dict(raw, "examples/linux/job.yaml")
    assert not res.is_valid
    errors_str = [str(e) for e in res.errors]
    assert any(f"{pre_path}[2].name: Duplicate precheck name" in s for s in errors_str)
    assert any(f"{pre_path}[2]: Precheck must define exactly one of 'script' or 'script_path'" in s for s in errors_str)


def test_precheck_invalid_action():
    raw = get_valid_raw()
    pre_list = raw.get("pre_run") if "pre_run" in raw else raw["invoke"]["precheck"]
    pre_path = "pre_run" if "pre_run" in raw else "invoke.precheck"
    # In raw YAML, 0 is parsed as integer 0
    pre_list[0]["result"][0]["action"] = "restart_vm"
    res = validate_job_dict(raw, "examples/linux/job.yaml")
    assert not res.is_valid
    assert any(f"{pre_path}[0].result[0].action" in e.path for e in res.errors)


def test_runcommand_relative_paths():
    raw = get_valid_raw()
    raw["invoke"]["runcommand"]["res_file"] = "relative/result.res"
    raw["invoke"]["runcommand"]["log_file"] = "./relative/result.log"
    res = validate_job_dict(raw, "examples/linux/job.yaml")
    assert not res.is_valid
    error_paths = [e.path for e in res.errors]
    assert "invoke.runcommand.res_file" in error_paths
    assert "invoke.runcommand.log_file" in error_paths


def test_step_duplicate_name():
    raw = get_valid_raw()
    raw["invoke"]["runcommand"]["steps"].append({
        "name": "check-os-release",  # Duplicate step name
        "script": "echo 1",
    })
    res = validate_job_dict(raw, "examples/linux/job.yaml")
    assert not res.is_valid
    errors_str = [str(e) for e in res.errors]
    assert any("Duplicate step name" in s for s in errors_str)


def test_step_with_var_rejected_with_migration_hint():
    raw = get_valid_raw()
    raw["invoke"]["runcommand"]["steps"][0]["var"] = "OS_NAME"
    res = validate_job_dict(raw, "examples/linux/job.yaml")
    assert not res.is_valid
    errors_str = [str(e) for e in res.errors]
    assert any("remove 'var'; use 'write_result NAME VALUE'" in s for s in errors_str)


def test_step_duplicate_written_var_emits_warning():
    raw = get_valid_raw()
    raw["invoke"]["runcommand"]["steps"].append({
        "name": "extra-step",
        "script": 'write_result OS_NAME "2.0"',
    })
    res = validate_job_dict(raw, "examples/linux/job.yaml")
    assert res.is_valid  # Still valid, only warning
    assert any("written by multiple steps" in w.message for w in res.warnings)


def test_step_missing_script_file():
    raw = get_valid_raw()
    raw["invoke"]["runcommand"]["steps"][4]["script_path"] = "./scripts/non_existent.sh"
    res = validate_job_dict(raw, "examples/linux/job.yaml")
    assert not res.is_valid
    assert any("invoke.runcommand.steps[4].script_path" in e.path for e in res.errors)


def test_collect_unknown_var_and_duplicate_col():
    raw = get_valid_raw()
    raw["collect"].append({
        "column": "vm_name",  # Duplicate column name
        "var": "UNDEFINED_VAR",
    })
    res = validate_job_dict(raw, "examples/linux/job.yaml")
    assert not res.is_valid
    errors_str = [str(e) for e in res.errors]
    assert any("Duplicate column name" in s for s in errors_str)
    # UNDEFINED_VAR is a warning, not an error
    warnings_str = [str(w) for w in res.warnings]
    assert any("UNDEFINED_VAR" in s and "not written by any runcommand step" in s for s in warnings_str)


def test_collect_invalid_source_format():
    raw = get_valid_raw()
    raw["collect"][0]["source"] = "csv.vm_name"  # Must start with input.
    res = validate_job_dict(raw, "examples/linux/job.yaml")
    assert not res.is_valid
    assert any("collect[0].source" in e.path for e in res.errors)


def test_sleep_before_collect_required():
    raw = get_valid_raw()
    del raw["sleep_before_collect"]
    res = validate_job_dict(raw, "examples/linux/job.yaml")
    assert not res.is_valid
    assert any("sleep_before_collect" in e.path for e in res.errors)

    raw["sleep_before_collect"] = 0
    res0 = validate_job_dict(raw, "examples/linux/job.yaml")
    assert not any("sleep_before_collect" in e.path for e in res0.errors)


def test_reserved_var_rejected():
    raw = get_valid_raw()
    raw["collect"].append({
        "column": "y2b_reserved",
        "var": "__Y2B_CUSTOM",
    })
    res = validate_job_dict(raw, "examples/linux/job.yaml")
    assert not res.is_valid
    assert any("is reserved" in str(e) for e in res.errors)


def test_validation_warnings():
    raw = get_valid_raw()
    raw["run_timeout"] = 300
    res = validate_job_dict(raw, "examples/linux/job.yaml")
    assert any("run_timeout" in w.path for w in res.warnings)

    # Readiness warning
    raw["sleep_before_collect"] = "10s"
    raw["collect_retries"] = 1
    raw["collect_retry_interval"] = "10s"
    res_ready = validate_job_dict(raw, "examples/linux/job.yaml")
    assert any("exceeds total collect wait time" in w.message for w in res_ready.warnings)

    # 5400s cap warning
    raw["invoke"]["runcommand"]["steps"][0]["timeout"] = 6000
    res_cap = validate_job_dict(raw, "examples/linux/job.yaml")
    assert any("5400s" in w.message for w in res_cap.warnings)
