"""T1 tests for engine trace, host-script shim, and secret redaction (TP-20, TP-21, TP-23)."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

from tests.helpers_ado import (
    classify_argv,
    fast_backoff,
    make_fleet,
    normalise_argv,
    normalise_console_output,
    parse_engine_trace_line,
    parse_script_trace_line,
    run_cli,
)
from y2shared.azcmd import (
    KIND_DEALLOCATE,
    KIND_INVOKE,
    KIND_POWEROFF,
    KIND_POWER_STATE,
    KIND_READER,
    KIND_SUB_RESOLVE,
    KIND_VM_START,
)
from y2shared.aztrace import (
    ensure_shim_dir,
    format_engine_line,
    format_script_line,
    quote_argv,
    redact_argv,
    redact_text,
)
from y2shared.shell import run_bash


# ==============================================================================
# TP-23: Secret Redaction Tests
# ==============================================================================


@pytest.mark.parametrize(
    "argv_in, expected_out",
    [
        # Long flags with space or =
        (["az", "login", "--password", "hunter2"], ["az", "login", "--password", "***"]),
        (["az", "login", "--client-secret=s3cr3t"], ["az", "login", "--client-secret=***"]),
        (["az", "login", "--account-key", "key123"], ["az", "login", "--account-key", "***"]),
        (["az", "storage", "--sas-token", "sig=xyz"], ["az", "storage", "--sas-token", "***"]),
        (["az", "vm", "create", "--admin-password", "$PW"], ["az", "vm", "create", "--admin-password", "***"]),
        # Case insensitivity
        (["az", "login", "--Password", "p"], ["az", "login", "--Password", "***"]),
        (["az", "login", "--CLIENT-SECRET=cs"], ["az", "login", "--CLIENT-SECRET=***"]),
        (["az", "login", "--Sas-Token", "tok"], ["az", "login", "--Sas-Token", "***"]),
        (["az", "login", "--TOKEN", "tok"], ["az", "login", "--TOKEN", "***"]),
        (["az", "login", "--storage-account-key", "sak"], ["az", "login", "--storage-account-key", "***"]),
        # Flag containing "key"
        (["az", "keyvault", "secret", "show", "--key-name", "mykey"], ["az", "keyvault", "secret", "show", "--key-name", "***"]),
        (["az", "show", "--keyvault", "kv"], ["az", "show", "--keyvault", "***"]),
        # Non-secret flags (untouched)
        (["az", "vm", "show", "--subscription", "sub1"], ["az", "vm", "show", "--subscription", "sub1"]),
        (["az", "vm", "show", "--resource-group", "rg1"], ["az", "vm", "show", "--resource-group", "rg1"]),
        (["az", "vm", "show", "-g", "rg1", "-n", "vm1"], ["az", "vm", "show", "-g", "rg1", "-n", "vm1"]),
        (["az", "vm", "show", "--query", "id", "-o", "tsv"], ["az", "vm", "show", "--query", "id", "-o", "tsv"]),
        (["az", "vm", "run-command", "invoke", "--command-id", "RunShellScript"], ["az", "vm", "run-command", "invoke", "--command-id", "RunShellScript"]),
        (["az", "vm", "run-command", "invoke", "--scripts", "@script.sh"], ["az", "vm", "run-command", "invoke", "--scripts", "@script.sh"]),
        # Positional arguments containing secret words
        (["az", "vm", "echo", "mypassword"], ["az", "vm", "echo", "mypassword"]),
        # Short flag -p is NOT redacted per AC-14
        (["az", "login", "-u", "user", "-p", "hunter2"], ["az", "login", "-u", "user", "-p", "hunter2"]),
        # Edge cases
        (["az", "login", "--password"], ["az", "login", "--password"]),
        (["az", "login", "--password="], ["az", "login", "--password=***"]),
        (["az", "login", "--password", "--other"], ["az", "login", "--password", "--other"]),
        (["az", "login", "--password", "s1", "--token", "s2"], ["az", "login", "--password", "***", "--token", "***"]),
        (["az", "login", "--password", "repeat", "--name", "repeat"], ["az", "login", "--password", "***", "--name", "repeat"]),
    ],
)
def test_redact_argv_table_driven(argv_in: List[str], expected_out: List[str]):
    assert redact_argv(argv_in) == expected_out


def test_redact_text_table_driven():
    text_in = 'az login --password "hunter2" --client-secret=s3cr3t --account-key \'mykey\' --other val'
    expected = 'az login --password *** --client-secret=*** --account-key *** --other val'
    assert redact_text(text_in) == expected


def test_redact_end_to_end(tmp_path: Path):
    fleet = make_fleet(tmp_path, n=1, variant="b")
    job_yaml = fleet["job_yaml"]
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)

    import yaml
    with open(job_yaml, "r", encoding="utf-8") as f:
        job_data = yaml.safe_load(f)
    job_data["pre_run"] = [
        {
            "name": "check-secret",
            "phases": ["invoke"],
            "script": 'az login --password hunter2 --client-secret=s3cr3t >/dev/null 2>&1\necho 0\n',
            "result": {
                "0": {"action": "continue", "log": "secret ok"},
                "default": {"action": "continue", "log": "default"},
            },
        }
    ]
    with open(job_yaml, "w", encoding="utf-8") as f:
        yaml.safe_dump(job_data, f)

    env = {"FAKE_AZ_STATE_DIR": str(state_dir)}
    res = run_cli("y2b", ["run", str(job_yaml), "--phase", "all", "-y"], cwd=tmp_path, env=env)
    assert res.returncode == 0

    combined_output = res.stdout + res.stderr
    assert "hunter2" not in combined_output
    assert "s3cr3t" not in combined_output

    result_log = (tmp_path / "result.log").read_text(encoding="utf-8")
    assert "hunter2" not in result_log
    assert "s3cr3t" not in result_log
    assert ("--password '***'" in result_log) or ("--password ***" in result_log)


# ==============================================================================
# TP-20: Engine Trace Line Format Tests
# ==============================================================================


def test_engine_trace_normal(tmp_path: Path):
    fleet = make_fleet(tmp_path, n=1, variant="a")
    job_yaml = fleet["job_yaml"]
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)

    env = {"FAKE_AZ_STATE_DIR": str(state_dir)}
    res = run_cli("y2b", ["run", str(job_yaml), "--phase", "all", "-y"], cwd=tmp_path, env=env)
    assert res.returncode == 0

    result_log = (tmp_path / "result.log").read_text(encoding="utf-8")
    engine_lines = [
        ln for ln in result_log.splitlines()
        if "[az] engine:" in ln
    ]
    assert len(engine_lines) >= 2  # at least invoke and reader

    for ln in engine_lines:
        parsed = parse_engine_trace_line(ln)
        assert parsed is not None, f"Failed to parse engine line: {ln}"
        assert parsed["kind"] in (
            KIND_SUB_RESOLVE,
            KIND_POWER_STATE,
            KIND_VM_START,
            KIND_INVOKE,
            KIND_READER,
            KIND_DEALLOCATE,
            KIND_POWEROFF,
        )
        assert parsed["n"] >= 1
        assert parsed["dur"] >= 0.0
        assert parsed["status"] in ("SUCCESS", "STILL_RUNNING") or parsed["status"].startswith("retry:") or parsed["status"].startswith("ERROR:")
        # Round trip
        assert parsed["argv_list"] == shlex.split(parsed["argv"])


def test_engine_trace_429_retry_and_success(tmp_path: Path):
    fleet = make_fleet(tmp_path, n=1, variant="a")
    job_yaml = fleet["job_yaml"]
    vm_name = fleet["vm_names"][0]
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)

    env = {
        "FAKE_AZ_STATE_DIR": str(state_dir),
        f"FAKE_AZ_POST_THROTTLE_COUNT_{vm_name}": "1",
    }
    res = run_cli("y2b", ["run", str(job_yaml), "--phase", "all", "-y"], cwd=tmp_path, env=env)
    assert res.returncode == 0

    result_log = (tmp_path / "result.log").read_text(encoding="utf-8")
    invoke_lines = [
        parse_engine_trace_line(ln)
        for ln in result_log.splitlines()
        if f"[az] engine:{KIND_INVOKE} " in ln
    ]
    assert len(invoke_lines) == 2
    assert invoke_lines[0]["n"] == 1
    assert invoke_lines[0]["status"] == "retry:throttled"
    assert invoke_lines[0]["rc"] == 1

    assert invoke_lines[1]["n"] == 2
    assert invoke_lines[1]["status"] == "SUCCESS"
    assert invoke_lines[1]["rc"] == 0


def test_engine_trace_429_exhaustion(tmp_path: Path):
    fleet = make_fleet(tmp_path, n=1, variant="a")
    job_yaml = fleet["job_yaml"]
    vm_name = fleet["vm_names"][0]
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)

    env = {
        "FAKE_AZ_STATE_DIR": str(state_dir),
        f"FAKE_AZ_RC_{vm_name}": "throttled",
    }
    res = run_cli("y2b", ["run", str(job_yaml), "--phase", "all", "-y"], cwd=tmp_path, env=env)
    assert res.returncode == 2

    result_log = (tmp_path / "result.log").read_text(encoding="utf-8")
    invoke_lines = [
        parse_engine_trace_line(ln)
        for ln in result_log.splitlines()
        if f"[az] engine:{KIND_INVOKE} " in ln
    ]
    assert len(invoke_lines) == 6
    for i in range(5):
        assert invoke_lines[i]["n"] == i + 1
        assert invoke_lines[i]["status"] == "retry:throttled"
    assert invoke_lines[5]["n"] == 6
    assert invoke_lines[5]["status"] == "ERROR:throttled"


def test_engine_trace_conflict_on_invoke(tmp_path: Path):
    fleet = make_fleet(tmp_path, n=1, variant="a")
    job_yaml = fleet["job_yaml"]
    vm_name = fleet["vm_names"][0]
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)

    env = {
        "FAKE_AZ_STATE_DIR": str(state_dir),
        f"FAKE_AZ_POST_BUSY_COUNT_{vm_name}": "1",
    }
    res = run_cli("y2b", ["run", str(job_yaml), "--phase", "all", "-y"], cwd=tmp_path, env=env)
    assert res.returncode == 0

    result_log = (tmp_path / "result.log").read_text(encoding="utf-8")
    invoke_lines = [
        parse_engine_trace_line(ln)
        for ln in result_log.splitlines()
        if f"[az] engine:{KIND_INVOKE} " in ln
    ]
    assert len(invoke_lines) == 2
    assert invoke_lines[0]["status"] == "retry:busy"
    assert invoke_lines[1]["status"] == "SUCCESS"


def test_engine_trace_conflict_exhaustion_on_invoke(tmp_path: Path):
    fleet = make_fleet(tmp_path, n=1, variant="a")
    job_yaml = fleet["job_yaml"]
    vm_name = fleet["vm_names"][0]
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)

    env = {
        "FAKE_AZ_STATE_DIR": str(state_dir),
        f"FAKE_AZ_POST_BUSY_COUNT_{vm_name}": "4",
    }
    res = run_cli("y2b", ["run", str(job_yaml), "--phase", "all", "-y"], cwd=tmp_path, env=env)
    assert res.returncode == 2

    result_log = (tmp_path / "result.log").read_text(encoding="utf-8")
    invoke_lines = [
        parse_engine_trace_line(ln)
        for ln in result_log.splitlines()
        if f"[az] engine:{KIND_INVOKE} " in ln
    ]
    assert len(invoke_lines) == 4
    for i in range(3):
        assert invoke_lines[i]["status"] == "retry:busy"
    assert invoke_lines[3]["status"] == "ERROR:run_command_busy"


def test_engine_trace_reader_conflict_is_terminal_per_call(tmp_path: Path):
    fleet = make_fleet(tmp_path, n=1, variant="a", collect_retries=3)
    job_yaml = fleet["job_yaml"]
    vm_name = fleet["vm_names"][0]
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)

    env = {
        "FAKE_AZ_STATE_DIR": str(state_dir),
        f"FAKE_AZ_BUSY_COUNT_{vm_name}": "2",
    }
    res = run_cli("y2b", ["run", str(job_yaml), "--phase", "all", "-y"], cwd=tmp_path, env=env)
    assert res.returncode == 0

    result_log = (tmp_path / "result.log").read_text(encoding="utf-8")
    reader_lines = [
        parse_engine_trace_line(ln)
        for ln in result_log.splitlines()
        if f"[az] engine:{KIND_READER} " in ln
    ]
    assert len(reader_lines) == 3
    assert reader_lines[0]["n"] == 1
    assert reader_lines[0]["status"] == "STILL_RUNNING"
    assert reader_lines[1]["n"] == 1
    assert reader_lines[1]["status"] == "STILL_RUNNING"
    assert reader_lines[2]["n"] == 1
    assert reader_lines[2]["status"] == "SUCCESS"


def test_engine_trace_auth_terminal(tmp_path: Path):
    fleet = make_fleet(tmp_path, n=1, variant="a")
    job_yaml = fleet["job_yaml"]
    vm_name = fleet["vm_names"][0]
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)

    env = {
        "FAKE_AZ_STATE_DIR": str(state_dir),
        f"FAKE_AZ_RC_{vm_name}": "auth",
    }
    res = run_cli("y2b", ["run", str(job_yaml), "--phase", "all", "-y"], cwd=tmp_path, env=env)
    assert res.returncode in (2, 3)

    result_log = (tmp_path / "result.log").read_text(encoding="utf-8")
    invoke_lines = [
        parse_engine_trace_line(ln)
        for ln in result_log.splitlines()
        if f"[az] engine:{KIND_INVOKE} " in ln
    ]
    assert len(invoke_lines) == 1
    assert invoke_lines[0]["n"] == 1
    assert invoke_lines[0]["status"] == "ERROR:auth"


def test_engine_trace_timeout(tmp_path: Path):
    fleet = make_fleet(tmp_path, n=1, variant="a")
    job_yaml = fleet["job_yaml"]
    vm_name = fleet["vm_names"][0]
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)

    env = {
        "FAKE_AZ_STATE_DIR": str(state_dir),
        f"FAKE_AZ_RC_{vm_name}": "timeout",
    }
    from y2b.profile import PROFILE
    from y2shared.cli import build_parser
    from y2shared.runner import run_job

    args = build_parser(PROFILE).parse_args(["run", str(job_yaml), "--phase", "all", "-y"])
    repo_root = Path(__file__).resolve().parent.parent
    fake_az_dir = repo_root / "tests" / "fake_az"
    cur_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{fake_az_dir}:{cur_path}"
    os.environ["FAKE_AZ_STATE_DIR"] = str(state_dir)
    os.environ[f"FAKE_AZ_RC_{vm_name}"] = "timeout"

    try:
        with fast_backoff(timeout=0.2):
            rc = run_job(args, profile=PROFILE)
        assert rc == 2
    finally:
        os.environ["PATH"] = cur_path
        os.environ.pop(f"FAKE_AZ_RC_{vm_name}", None)

    result_log = (tmp_path / "result.log").read_text(encoding="utf-8")
    invoke_lines = [
        parse_engine_trace_line(ln)
        for ln in result_log.splitlines()
        if f"[az] engine:{KIND_INVOKE} " in ln
    ]
    assert len(invoke_lines) == 1
    assert invoke_lines[0]["rc"] == "timeout"
    assert invoke_lines[0]["status"] == "ERROR:run_command_timeout"


def test_engine_trace_vm_start_and_cached_precheck(tmp_path: Path):
    fleet = make_fleet(tmp_path, n=2, variant="b")
    job_yaml = fleet["job_yaml"]
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)

    import yaml
    with open(job_yaml, "r", encoding="utf-8") as f:
        job_data = yaml.safe_load(f)

    # Precheck with start action that succeeds on re-check
    start_script = (
        f'FLAG="{state_dir}/precheck_flag_${{VM_NAME}}.txt"\n'
        'if [ -f "$FLAG" ]; then\n'
        '  echo running\n'
        'else\n'
        '  touch "$FLAG"\n'
        '  echo start\n'
        'fi\n'
    )

    job_data["pre_run"] = [
        {
            "name": "check-sub",
            "phases": ["invoke"],
            "cache": "subscription",
            "script": 'az account show --subscription "$SUBSCRIPTION" >/dev/null 2>&1\necho 0\n',
            "result": {
                "0": {"action": "continue", "log": "sub ok"},
                "default": {"action": "continue", "log": "sub default"},
            },
        },
        {
            "name": "check-start",
            "phases": ["invoke"],
            "script": start_script,
            "result": {
                "running": {"action": "continue", "log": "vm confirmed running"},
                "start": {"action": "start", "log": "trigger start"},
                "default": {"action": "continue", "log": "start default"},
            },
        },
    ]
    with open(job_yaml, "w", encoding="utf-8") as f:
        yaml.safe_dump(job_data, f)

    env = {"FAKE_AZ_STATE_DIR": str(state_dir)}
    res = run_cli("y2b", ["run", str(job_yaml), "--phase", "all", "-y"], cwd=tmp_path, env=env)
    assert res.returncode == 0

    result_log = (tmp_path / "result.log").read_text(encoding="utf-8")

    # az vm start is logged as engine:vm-start attempt=1 and NOT as script:
    vm_start_lines = [
        parse_engine_trace_line(ln)
        for ln in result_log.splitlines()
        if f"[az] engine:{KIND_VM_START} " in ln
    ]
    assert len(vm_start_lines) == 2
    for vl in vm_start_lines:
        assert vl["n"] == 1
        assert vl["status"] == "SUCCESS"

    assert not any("script:precheck:check-start" in ln and "start" in ln for ln in result_log.splitlines())

    # Cached precheck check
    vm1, vm2 = fleet["vm_names"][0], fleet["vm_names"][1]
    assert f"[az] script:precheck:check-sub cached from {vm1}" in result_log

    # vm1 has exactly one script:precheck:check-sub line; vm2 has cached line and no direct execution
    lines_vm1 = result_log.split(f"VM: {vm2}")[0]
    lines_vm2 = result_log.split(f"VM: {vm2}")[1]
    assert "script:precheck:check-sub" in lines_vm1
    assert "script:precheck:check-sub rc=" not in lines_vm2
    assert f"script:precheck:check-sub cached from {vm1}" in lines_vm2


# ==============================================================================
# TP-21: Host-Script az Trace Tests
# ==============================================================================


def test_script_trace_sections(tmp_path: Path):
    fleet = make_fleet(tmp_path, n=1, variant="b")
    job_yaml = fleet["job_yaml"]
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)

    env = {"FAKE_AZ_STATE_DIR": str(state_dir)}
    res = run_cli("y2b", ["run", str(job_yaml), "--phase", "all", "-y"], cwd=tmp_path, env=env)
    assert res.returncode == 0

    result_log = (tmp_path / "result.log").read_text(encoding="utf-8")
    script_lines = [
        parse_script_trace_line(ln)
        for ln in result_log.splitlines()
        if "[az] script:" in ln and "cached from" not in ln
    ]
    assert len(script_lines) >= 3

    sections_found = {sl["section"] for sl in script_lines}
    assert "precheck" in sections_found
    assert "column" in sections_found
    assert "post_run" in sections_found

    for sl in script_lines:
        assert sl["rc"] == 0
        assert sl["argv_list"] == shlex.split(sl["argv"])


def test_script_trace_pass_through_and_direct_bytes(tmp_path: Path):
    repo_root = Path(__file__).resolve().parent.parent
    shim_bin = repo_root / "y2shared" / "azshim" / "az"
    fake_az_dir = Path(__file__).resolve().parent / "fake_az"

    trace_file = tmp_path / "trace.out"
    env = dict(os.environ)
    env["PATH"] = f"{fake_az_dir}:{env['PATH']}"
    env["Y2_AZ_SHIM_DIR"] = str(shim_bin.parent)
    env["Y2_AZ_TRACE_FILE"] = str(trace_file)
    env["Y2_AZ_TRACE_CTX"] = "precheck:test"

    # Invoke shim directly with stdin containing arbitrary bytes
    res = subprocess.run(
        ["/bin/bash", str(shim_bin), "account", "show", "--subscription", "sub1"],
        input=b"hello\x00world\xff",
        capture_output=True,
        env=env,
    )
    assert res.returncode == 0
    assert trace_file.exists()
    content = trace_file.read_bytes()
    assert b"precheck:test\x000\x00" in content


def test_script_trace_absolute_path_untraced(tmp_path: Path):
    fake_az_bin = Path(__file__).resolve().parent / "fake_az" / "az"
    script = (
        f'"{fake_az_bin}" account show --subscription sub1 >/dev/null 2>&1\n'
        'exit 0\n'
    )
    res = run_bash(script, trace_section="precheck", trace_name="test_abs")
    assert res.rc == 0
    assert len(res.az_calls) == 0  # Absolute path is untraced!


def test_script_trace_missing_az_on_path(tmp_path: Path):
    script = 'az account show --subscription sub1\n'
    # Create isolated bin dir with only bash symlinked
    empty_bin = tmp_path / "isolated_bin"
    empty_bin.mkdir()
    bash_path = subprocess.run(["which", "bash"], capture_output=True, text=True).stdout.strip()
    (empty_bin / "bash").symlink_to(bash_path)

    env = {"PATH": str(empty_bin)}
    res = run_bash(script, env=env, trace_section="precheck", trace_name="missing_az")
    assert res.rc == 127
    assert len(res.az_calls) == 1
    assert res.az_calls[0].rc == 127
    assert "command not found" in res.stderr.lower() or "not found" in res.stderr.lower()


def test_script_trace_parallel_attribution(tmp_path: Path):
    fleet = make_fleet(tmp_path, n=20, variant="b")
    job_yaml = fleet["job_yaml"]
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)

    env = {"FAKE_AZ_STATE_DIR": str(state_dir)}
    res = run_cli("y2b", ["run", str(job_yaml), "--phase", "all", "-y", "--parallel", "5"], cwd=tmp_path, env=env)
    assert res.returncode == 0

    result_log = (tmp_path / "result.log").read_text(encoding="utf-8")

    # Verify per-VM attribution
    for vm_name in fleet["vm_names"]:
        pattern = rf"VM: {vm_name} .*?\n(.*?)(?:\n==================================================\nVM: |\Z)"
        match = re.search(pattern, result_log, re.DOTALL)
        assert match is not None, f"Could not find section for {vm_name}"
        section_text = match.group(1)

        # In this section, every script line containing -n should reference vm_name
        for ln in section_text.splitlines():
            if "[az] script:" in ln and "cached from" not in ln:
                parsed = parse_script_trace_line(ln)
                assert parsed is not None
                if "-n" in parsed["argv_list"]:
                    idx = parsed["argv_list"].index("-n")
                    assert parsed["argv_list"][idx + 1] == vm_name


# ==============================================================================
# TP-24: ADO Log Groups and Unchanged Console Tests
# ==============================================================================


def test_ado_groups_tf_build_all_invoke_collect(tmp_path: Path):
    """Test TF_BUILD=True produces balanced groups and proper footer ordering."""
    fleet = make_fleet(tmp_path, n=2, variant="a")
    job_yaml = fleet["job_yaml"]
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)

    # 1. --phase all under TF_BUILD=True
    env_tf = {"FAKE_AZ_STATE_DIR": str(state_dir), "TF_BUILD": "True"}
    res_all = run_cli("y2b", ["run", str(job_yaml), "--phase", "all", "-y"], cwd=tmp_path, env=env_tf)
    assert res_all.returncode in (0, 2)

    # Check balanced groups
    group_open = re.findall(r"^##\[group\](\S+) \| (\S+)", res_all.stdout, re.MULTILINE)
    group_close = re.findall(r"^##\[endgroup\]", res_all.stdout, re.MULTILINE)
    assert len(group_open) == 2
    assert len(group_close) == 2
    assert group_open[0][0] == fleet["vm_names"][0]
    assert group_open[1][0] == fleet["vm_names"][1]

    # Verify footer ordering: groups come before conformance line
    lines_all = [ln.strip() for ln in res_all.stdout.strip().splitlines() if ln.strip()]
    assert lines_all[-1].startswith("conformance:")
    last_endgroup_idx = max(i for i, ln in enumerate(lines_all) if ln == "##[endgroup]")
    conformance_idx = max(i for i, ln in enumerate(lines_all) if ln.startswith("conformance:"))
    assert last_endgroup_idx < conformance_idx

    # 2. --phase invoke under TF_BUILD=true (case-insensitive)
    fleet2 = make_fleet(tmp_path / "f2", n=2, variant="a")
    env_tf_lower = {"FAKE_AZ_STATE_DIR": str(state_dir), "TF_BUILD": "true"}
    res_inv = run_cli("y2b", ["run", str(fleet2["job_yaml"]), "--phase", "invoke", "-y"], cwd=fleet2["dir"], env=env_tf_lower)
    assert res_inv.returncode in (0, 2)

    # In invoke: footer order is groups -> conformance -> fired=<n>
    lines_inv = [ln.strip() for ln in res_inv.stdout.strip().splitlines() if ln.strip()]
    assert lines_inv[-1] == "fired=2"
    assert lines_inv[-2].startswith("conformance:")
    inv_endgroup_idx = max(i for i, ln in enumerate(lines_inv) if ln == "##[endgroup]")
    assert inv_endgroup_idx < len(lines_inv) - 2

    # 3. --phase collect does not replay Phase 1 lines
    res_col = run_cli("y2b", ["run", str(fleet2["job_yaml"]), "--phase", "collect", "-y"], cwd=fleet2["dir"], env=env_tf_lower)
    assert res_col.returncode in (0, 2)
    # Check that inside collect groups, there are no Phase 1 [dispatch] runCommand POST lines
    collect_groups = re.findall(r"##\[group\].*?##\[endgroup\]", res_col.stdout, re.DOTALL)
    assert len(collect_groups) == 2
    for cg in collect_groups:
        assert "runCommand POST" not in cg


def test_ado_groups_neutralisation(tmp_path: Path):
    """Test VM output containing ##vso and ##[ is neutralised."""
    fleet = make_fleet(tmp_path, n=1, variant="a")
    job_yaml = fleet["job_yaml"]
    scripts_dir = fleet["scripts_dir"]
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)

    # Inject ##vso and ##[ into precheck script output
    (scripts_dir / "check_state.sh").write_text(
        '#!/bin/bash\n'
        'echo "##vso[task.complete result=Failed;]"\n'
        'echo "##[error] fake error"\n',
        encoding="utf-8",
    )

    env_tf = {"FAKE_AZ_STATE_DIR": str(state_dir), "TF_BUILD": "True"}
    res = run_cli("y2b", ["run", str(job_yaml), "--phase", "all", "-y"], cwd=tmp_path, env=env_tf)
    assert res.returncode in (0, 2)

    # In groups output, the injected lines must be neutralised
    groups_text = "".join(re.findall(r"##\[group\].*?##\[endgroup\]", res.stdout, re.DOTALL))
    assert "#vso[task.complete result=Failed;]" in groups_text
    assert "#[error] fake error" in groups_text
    # And there should be no unneutralised ones inside groups (other than the group markers themselves)
    inner_lines = [
        ln for ln in groups_text.splitlines()
        if not ln.startswith("##[group]") and not ln.startswith("##[endgroup]")
    ]
    for ln in inner_lines:
        assert "##vso" not in ln
        assert "##[" not in ln


def test_console_unchanged_without_tf_build(tmp_path: Path):
    """Test console output without TF_BUILD has no ## markers and equals baseline."""
    import shutil
    repo_root = Path(__file__).resolve().parent.parent
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)

    def cleanup_linux():
        p = repo_root / "examples" / "linux" / ".result.parts"
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
        for f in (repo_root / "examples" / "linux").glob("result.*"):
            try:
                f.unlink()
            except OSError:
                pass

    cleanup_linux()
    try:
        # 1. Unset TF_BUILD
        env_no_tf = {"FAKE_AZ_STATE_DIR": str(state_dir)}
        res_lin = run_cli("y2b", ["run", str(repo_root / "examples" / "linux" / "job.yaml"), "--phase", "all", "-y"], cwd=repo_root / "examples" / "linux", env=env_no_tf)
        assert res_lin.returncode in (0, 2)
        assert "##[" not in res_lin.stdout
        assert "##vso" not in res_lin.stdout
        assert "##[" not in res_lin.stderr
        assert "##vso" not in res_lin.stderr

        # Check baseline match
        norm_lin = normalise_console_output(res_lin.stdout)
        golden_lin = (repo_root / "tests" / "golden" / "run_console_baseline_linux.txt").read_text(encoding="utf-8")
        assert norm_lin.strip() == golden_lin.strip()

        # 2. TF_BUILD=1 and TF_BUILD=false do NOT enable groups
        for val in ("1", "false"):
            cleanup_linux()
            env_val = {"FAKE_AZ_STATE_DIR": str(state_dir), "TF_BUILD": val}
            res = run_cli("y2b", ["run", str(repo_root / "examples" / "linux" / "job.yaml"), "--phase", "all", "-y"], cwd=repo_root / "examples" / "linux", env=env_val)
            assert "##[" not in res.stdout
            assert "##vso" not in res.stdout

        # 3. Check banner line: Run phase appears only for invoke|collect, never for all
        assert "Run phase:" not in res_lin.stdout
        fleet = make_fleet(tmp_path / "f_banner", n=1, variant="a")
        res_inv = run_cli("y2b", ["run", str(fleet["job_yaml"]), "--phase", "invoke", "-y"], cwd=fleet["dir"], env=env_no_tf)
        assert "Run phase:              invoke" in res_inv.stdout
    finally:
        cleanup_linux()


def test_ado_log_label_outcome_ok_not_no_result(tmp_path: Path):
    """Regression test for Owner Addition (a): with TF_BUILD=True, a run with OK=3 prints ##[group]lin01 | OK."""
    fleet = make_fleet(tmp_path, n=3, os_type="linux")
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env_tf = {"FAKE_AZ_STATE_DIR": str(state_dir), "TF_BUILD": "True"}

    res = run_cli("y2b", ["run", str(fleet["job_yaml"]), "--phase", "all", "-y"], cwd=tmp_path, env=env_tf)
    assert res.returncode == 0
    assert "OK:          3" in res.stdout
    assert "##[group]lin01 | OK" in res.stdout
    assert "##[group]lin02 | OK" in res.stdout
    assert "##[group]lin03 | OK" in res.stdout
    assert "no_result" not in res.stdout


