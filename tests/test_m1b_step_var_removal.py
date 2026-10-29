"""Unit tests for Milestone M1b: Step var removal, two-argument write_result, and Collector rules 1-3."""

from pathlib import Path
import pytest
import yaml

from y2b.compiler import scan_written_vars, compile_job
from y2b.config import Job, Step, RunCommand, Invoke, Column, load_job
from y2b.collector import collect_for_vm
from y2b.dispatcher import ReadResult
from y2b.inputcsv import VMRow
from y2b.validate import validate_job_dict


def test_scan_written_vars(tmp_path):
    """Test static scanning of write_result calls in scripts and script files."""
    script_text = """
    write_result ALPHA "value1"
    write_result "BETA" "value2"
    write_result 'GAMMA' "value3"
    write_result "$DYNAMIC" "dyn"
    write_result lower_case "ignored"
    write_result __Y2B_RESERVED "reserved"
    """
    step_inline = Step(name="inline-step", script=script_text)
    assert scan_written_vars(step_inline) == {"ALPHA", "BETA", "GAMMA"}

    script_file = tmp_path / "test_script.sh"
    script_file.write_text("""#!/bin/bash\nwrite_result FILE_VAR "from_file"\n""", encoding="utf-8")
    step_file = Step(name="file-step", script_path=script_file)
    assert scan_written_vars(step_file) == {"FILE_VAR"}

    # Raw string and dict support
    assert scan_written_vars(script_text) == {"ALPHA", "BETA", "GAMMA"}
    assert scan_written_vars({"script": 'write_result DICT_VAR "val"'}) == {"DICT_VAR"}


def test_one_step_writes_two_values_fills_two_columns():
    """A single step can call write_result multiple times to populate multiple collect columns."""
    step = Step(
        name="multi-write-step",
        script="""
        write_result VAR_FIRST "val1"
        write_result VAR_SECOND "val2"
        """,
    )
    job = Job(
        yaml_path=Path("job.yaml"),
        yaml_dir=Path("."),
        raw_yaml={},
        job_hash="dummy",
        input=Path("list.csv"),
        output_log=Path("run.log"),
        output_csv=Path("result.csv"),
        actions="skip",
        parallel=1,
        sleep_before_collect=0,
        invoke=Invoke(runcommand=RunCommand(res_file="/res", log_file="/log", steps=[step])),
        collect=[
            Column(column="first", var="VAR_FIRST"),
            Column(column="second", var="VAR_SECOND"),
        ],
    )
    row = VMRow(index=1, vm_name="vm-01", resource_group="rg", subscription="sub", os="linux", env={}, raw_data={})
    res = ReadResult(
        status="OK",
        vars={
            "__Y2B_STEP_1__": "ok",
            "VAR_FIRST": "val1",
            "VAR_SECOND": "val2",
        },
    )
    vals = collect_for_vm(job, row, remote_result=res)
    assert vals == ["val1", "val2"]


def test_collector_rule_1_written_value_wins_over_failed_status():
    """Rule 1: A written value always wins, even if the step later exited non-zero."""
    step = Step(
        name="step-fails-after-write",
        script="""
        write_result WRITTEN_VAL "success_data"
        exit 1
        """,
    )
    job = Job(
        yaml_path=Path("job.yaml"),
        yaml_dir=Path("."),
        raw_yaml={},
        job_hash="dummy",
        input=Path("list.csv"),
        output_log=Path("run.log"),
        output_csv=Path("result.csv"),
        actions="skip",
        parallel=1,
        sleep_before_collect=0,
        invoke=Invoke(runcommand=RunCommand(res_file="/res", log_file="/log", steps=[step])),
        collect=[Column(column="col1", var="WRITTEN_VAL")],
    )
    row = VMRow(index=1, vm_name="vm-01", resource_group="rg", subscription="sub", os="linux", env={}, raw_data={})
    # Step status is rc=1, but WRITTEN_VAL was written into vars before exit:
    res = ReadResult(
        status="OK",
        vars={
            "__Y2B_STEP_1__": "rc=1",
            "WRITTEN_VAL": "success_data",
        },
    )
    vals = collect_for_vm(job, row, remote_result=res)
    assert vals == ["success_data"]


def test_collector_rule_2_missing_from_failed_step():
    """Rule 2: X not written, scan maps X to step with status != 'ok' -> __ERROR__ <status>."""
    step1 = Step(name="step-rc", script="exit 2\nwrite_result VAR_A 'foo'")
    step2 = Step(name="step-timeout", script="sleep 10\nwrite_result VAR_B 'bar'")
    job = Job(
        yaml_path=Path("job.yaml"),
        yaml_dir=Path("."),
        raw_yaml={},
        job_hash="dummy",
        input=Path("list.csv"),
        output_log=Path("run.log"),
        output_csv=Path("result.csv"),
        actions="skip",
        parallel=1,
        sleep_before_collect=0,
        invoke=Invoke(runcommand=RunCommand(res_file="/res", log_file="/log", steps=[step1, step2])),
        collect=[
            Column(column="col_a", var="VAR_A"),
            Column(column="col_b", var="VAR_B"),
        ],
    )
    row = VMRow(index=1, vm_name="vm-01", resource_group="rg", subscription="sub", os="linux", env={}, raw_data={})
    res = ReadResult(
        status="OK",
        vars={
            "__Y2B_STEP_1__": "rc=2",
            "__Y2B_STEP_2__": "timeout",
        },
    )
    vals = collect_for_vm(job, row, remote_result=res)
    assert vals == ["__ERROR__ rc=2", "__ERROR__ timeout"]


def test_collector_rule_3_missing_from_ok_step_or_unscanned():
    """Rule 3: Missing from an ok step, or unscanned name -> __MISSING__."""
    step1 = Step(name="step-ok", script="# Step ran ok but condition did not write VAR_A\n:")
    job = Job(
        yaml_path=Path("job.yaml"),
        yaml_dir=Path("."),
        raw_yaml={},
        job_hash="dummy",
        input=Path("list.csv"),
        output_log=Path("run.log"),
        output_csv=Path("result.csv"),
        actions="skip",
        parallel=1,
        sleep_before_collect=0,
        invoke=Invoke(runcommand=RunCommand(res_file="/res", log_file="/log", steps=[step1])),
        collect=[
            Column(column="col_a", var="VAR_A"),
            Column(column="col_dyn", var="VAR_DYNAMIC"),
        ],
    )
    row = VMRow(index=1, vm_name="vm-01", resource_group="rg", subscription="sub", os="linux", env={}, raw_data={})
    res = ReadResult(
        status="OK",
        vars={"__Y2B_STEP_1__": "ok"},
    )
    vals = collect_for_vm(job, row, remote_result=res)
    assert vals == ["__MISSING__", "__MISSING__"]


def test_compiler_run_step_emits_no_var_padding():
    """Compiler emits 'run_step y2b_step_N <timeout> N <quoted step name>' without var padding."""
    step1 = Step(name="short", script="write_result S 'val'", timeout=60)
    step2 = Step(name="very-long-name-step", script="write_result L 'val'", timeout=120)
    job = Job(
        yaml_path=Path("job.yaml"),
        yaml_dir=Path("."),
        raw_yaml={},
        job_hash="abcd",
        input=Path("list.csv"),
        output_log=Path("run.log"),
        output_csv=Path("result.csv"),
        actions="skip",
        parallel=1,
        sleep_before_collect=0,
        invoke=Invoke(runcommand=RunCommand(res_file="/res", log_file="/log", steps=[step1, step2])),
    )
    compiled, errors = compile_job(job)
    assert not errors
    assert "run_step y2b_step_1 60 1 short" in compiled
    assert "run_step y2b_step_2 120 2 very-long-name-step" in compiled

