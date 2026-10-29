"""Unit tests for y2p (PowerShell frontend): os_family, validation, compiler, scanner, parser."""

from __future__ import annotations

import base64
import json
from pathlib import Path
import pytest
import yaml

from y2shared.config import load_job
from y2shared.dispatcher import parse_run_command_output
from y2shared.inputcsv import os_family, read_input_csv
from y2b.validate import validate_job_dict as y2b_validate_job_dict
from y2p.compiler import compile_job, compile_collect_script, ps_quote, scan_written_vars
from y2p.profile import PROFILE as Y2P_PROFILE
from y2p.validate import validate_job_dict as y2p_validate_job_dict


def test_os_family_detection():
    # windows indicators
    assert os_family("windows2022") == "windows"
    assert os_family("Win2019") == "windows"
    assert os_family("WindowsServer") == "windows"

    # linux indicators
    assert os_family("rhel8") == "linux"
    assert os_family("ubuntu") == "linux"
    assert os_family("") == "linux"


def test_cross_tool_csv_rejection(tmp_path):
    job = load_job("examples/windows-baseline/job.yaml")

    # CSV with Linux OS given to y2p
    csv_linux = tmp_path / "linux.csv"
    csv_linux.write_text("vm_name,resource_group,subscription,os\nvm-01,rg,sub,rhel8\n", encoding="utf-8")
    _, _, res_p = read_input_csv(csv_linux, job=job, os_family="windows")
    assert not res_p.is_valid
    assert any("y2p only targets Windows VMs (use y2b)" in str(e) for e in res_p.errors)

    # CSV with Windows OS given to y2b
    csv_win = tmp_path / "win.csv"
    csv_win.write_text("vm_name,resource_group,subscription,os\nwin-01,rg,sub,windows2022\n", encoding="utf-8")
    _, _, res_b = read_input_csv(csv_win, job=job, os_family="linux")
    assert not res_b.is_valid
    assert any("y2b only targets Linux VMs (use y2p)" in str(e) for e in res_b.errors)


def test_y2p_validation_res_file_rules():
    with open("examples/windows-baseline/job.yaml", "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    # Error: POSIX path
    raw["invoke"]["runcommand"]["res_file"] = "/var/tmp/x"
    res = y2p_validate_job_dict(raw, "examples/windows-baseline/job.yaml")
    assert not res.is_valid
    assert any("invoke.runcommand.res_file" in e.path for e in res.errors)

    # Accepted: C:\path and C:/path
    raw["invoke"]["runcommand"]["res_file"] = r"C:\ProgramData\y2p\res.res"
    raw["invoke"]["runcommand"]["log_file"] = "C:/ProgramData/y2p/log.log"
    res_ok = y2p_validate_job_dict(raw, "examples/windows-baseline/job.yaml")
    assert res_ok.is_valid

    # Warning: Control characters (e.g. \t from tab escape)
    raw["invoke"]["runcommand"]["res_file"] = "C:\temp\res.res"  # \t is tab char
    res_ctrl = y2p_validate_job_dict(raw, "examples/windows-baseline/job.yaml")
    assert any("control character" in w.message for w in res_ctrl.warnings)


def test_y2b_validation_rejects_windows_path():
    with open("examples/hip-compliance/job.yaml", "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw["invoke"]["runcommand"]["res_file"] = r"C:\ProgramData\result.res"
    res = y2b_validate_job_dict(raw, "examples/hip-compliance/job.yaml")
    assert not res.is_valid
    assert any("invoke.runcommand.res_file" in e.path for e in res.errors)


def test_y2p_validation_step_lints():
    with open("examples/windows-baseline/job.yaml", "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    # Error: line starts with '@
    raw["invoke"]["runcommand"]["steps"][0]["script"] = "echo 1\n'@\necho 2"
    res = y2p_validate_job_dict(raw, "examples/windows-baseline/job.yaml")
    assert not res.is_valid
    assert any("starts with '@" in e.message for e in res.errors)

    # Reset
    raw["invoke"]["runcommand"]["steps"][0]["script"] = "echo 1"

    # Warning: PowerShell 7 constructs
    raw["invoke"]["runcommand"]["steps"][0]["script"] = "$a = $b ?? $c"
    res_ps7 = y2p_validate_job_dict(raw, "examples/windows-baseline/job.yaml")
    assert any("PowerShell 7-only syntax" in w.message for w in res_ps7.warnings)

    # Warning: non-ASCII characters
    raw["invoke"]["runcommand"]["steps"][0]["script"] = "Write-Output 'é日本'"
    res_ascii = y2p_validate_job_dict(raw, "examples/windows-baseline/job.yaml")
    assert any("non-ASCII characters" in w.message for w in res_ascii.warnings)

    # Warning: script_path without .ps1
    raw["invoke"]["runcommand"]["steps"][3]["script_path"] = "./scripts/check_tls.sh"
    res_ext = y2p_validate_job_dict(raw, "examples/windows-baseline/job.yaml")
    assert any(".ps1 extension" in w.message for w in res_ext.warnings)


def test_ps_quote_and_scan():
    assert ps_quote("it's") == "'it''s'"
    assert ps_quote("hello world") == "'hello world'"

    # write_result case insensitivity
    text = """
    write_result FOO "1"
    Write_Result BAR "2"
    WRITE_RESULT BAZ "3"
    write_result lower "ignored"
    """
    found = scan_written_vars(text)
    assert "FOO" in found
    assert "BAR" in found
    assert "BAZ" in found
    assert "lower" not in found


def test_parser_windows_json_shape():
    b64_val = base64.b64encode(b"my_value").decode("ascii")
    raw_json = json.dumps({
        "value": [
            {
                "code": "ComponentStatus/StdOut/succeeded",
                "message": f"===BEGIN_RESULT_RES===\nMY_VAR={b64_val}\n===END_RESULT_RES===\n",
            },
            {
                "code": "ComponentStatus/StdErr/succeeded",
                "message": "",
            },
        ]
    })
    vars_dict, err, msg = parse_run_command_output(raw_json)
    assert err is None
    assert vars_dict.get("MY_VAR") == "my_value"


def test_parser_windows_truncated():
    raw_json = json.dumps({
        "value": [
            {
                "code": "ComponentStatus/StdOut/succeeded",
                "message": "some prefix... MY_VAR=123\n===END_RESULT_RES===\n",
            },
            {
                "code": "ComponentStatus/StdErr/succeeded",
                "message": "",
            },
        ]
    })
    vars_dict, err, msg = parse_run_command_output(raw_json)
    assert err == "ERROR:result_truncated"


def test_y2p_compiler_golden_and_properties():
    job = load_job("examples/windows-baseline/job.yaml")
    compiled, errors = compile_job(job)
    assert not errors

    golden_path = Path("tests/golden/windows_baseline_remote.ps1")
    assert golden_path.exists()
    expected = golden_path.read_text(encoding="utf-8")
    assert compiled == expected

    # Verify PowerShell constructs and absence of bash constructs
    assert "$Steps = @(" in compiled
    assert "@'" in compiled
    assert "'@" in compiled
    assert "#!/bin/bash" not in compiled
    assert "export -f" not in compiled
    assert "run_step" not in compiled

