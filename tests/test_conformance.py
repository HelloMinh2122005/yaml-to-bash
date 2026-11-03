"""Tests for plan conformance checking and TF_BUILD warnings (TP-22, AC-13)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from tests.helpers_ado import fast_backoff, make_fleet, run_cli
from y2shared.azcmd import KIND_POWER_STATE
from y2shared.config import load_yaml_raw
from y2shared.parts import get_parts_dir
from y2shared.profile import Profile
import y2shared.dispatcher


def test_conformance_normal_all_examples(tmp_path: Path):
    """Test that normal runs on examples/linux and examples/windows report n/n."""
    import shutil
    repo_root = Path(__file__).resolve().parent.parent
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env = {"FAKE_AZ_STATE_DIR": str(state_dir)}

    def cleanup_example(name: str):
        p = repo_root / "examples" / name / ".result.parts"
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
        for f in (repo_root / "examples" / name).glob("result.*"):
            try:
                f.unlink()
            except OSError:
                pass

    # Linux example (3 VMs)
    cleanup_example("linux")
    try:
        res_lin = run_cli("y2b", ["run", str(repo_root / "examples" / "linux" / "job.yaml"), "--phase", "all", "-y"], cwd=tmp_path, env=env)
        assert res_lin.returncode in (0, 2)
        assert "conformance: 3/3 VMs matched plan" in res_lin.stdout
        # Last stdout line in --phase all is conformance
        lines = [ln.strip() for ln in res_lin.stdout.strip().splitlines() if ln.strip()]
        assert lines[-1] == "conformance: 3/3 VMs matched plan"
    finally:
        cleanup_example("linux")

    # Windows example (2 VMs)
    cleanup_example("windows")
    try:
        res_win = run_cli("y2p", ["run", str(repo_root / "examples" / "windows" / "job.yaml"), "--phase", "all", "-y"], cwd=tmp_path, env=env)
        assert res_win.returncode in (0, 2)
        assert "conformance: 2/2 VMs matched plan" in res_win.stdout
        lines_win = [ln.strip() for ln in res_win.stdout.strip().splitlines() if ln.strip()]
        assert lines_win[-1] == "conformance: 2/2 VMs matched plan"
    finally:
        cleanup_example("windows")


def test_conformance_phase_invoke_and_collect_scoping(tmp_path: Path):
    """Test invoke, collect, and resume conformance counting and ordering."""
    fleet = make_fleet(tmp_path, n=3, variant="a")
    job_yaml = fleet["job_yaml"]
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env = {"FAKE_AZ_STATE_DIR": str(state_dir)}

    # 1. Phase invoke
    res_inv = run_cli("y2b", ["run", str(job_yaml), "--phase", "invoke", "-y"], cwd=tmp_path, env=env)
    assert res_inv.returncode in (0, 2)
    assert "conformance: 3/3 VMs matched plan" in res_inv.stdout
    # In invoke: conformance line comes before fired=, and fired= is last
    inv_lines = [ln.strip() for ln in res_inv.stdout.strip().splitlines() if ln.strip()]
    assert inv_lines[-1] == "fired=3"
    assert inv_lines[-2] == "conformance: 3/3 VMs matched plan"

    # 2. Phase collect
    res_col = run_cli("y2b", ["run", str(job_yaml), "--phase", "collect", "-y"], cwd=tmp_path, env=env)
    assert res_col.returncode in (0, 2)
    assert "conformance: 3/3 VMs matched plan" in res_col.stdout
    col_lines = [ln.strip() for ln in res_col.stdout.strip().splitlines() if ln.strip()]
    assert col_lines[-1] == "conformance: 3/3 VMs matched plan"


def test_conformance_early_stop_and_retries_collapsed(tmp_path: Path):
    """Test that early stop prefix matches, and retries are collapsed without mismatch."""
    fleet = make_fleet(tmp_path, n=2, variant="a")
    job_yaml = fleet["job_yaml"]
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env = {
        "FAKE_AZ_STATE_DIR": str(state_dir),
        # VM 1 hits throttle twice on POST, should collapse and match
        f"FAKE_AZ_POST_THROTTLE_COUNT_{fleet['vm_names'][0]}": "2",
        # VM 2 hits busy twice on reader, should collapse and match
        f"FAKE_AZ_BUSY_COUNT_{fleet['vm_names'][1]}": "2",
    }
    res = run_cli("y2b", ["run", str(job_yaml), "--phase", "all", "-y"], cwd=tmp_path, env=env)
    assert res.returncode in (0, 2)
    assert "conformance: 2/2 VMs matched plan" in res_conformance_check(res.stdout)


def res_conformance_check(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("conformance:"):
            return line
    return ""


def test_conformance_injected_extra_command(tmp_path: Path):
    """Test call-site patch injecting extra command causes mismatch without changing exit code."""
    fleet = make_fleet(tmp_path, n=3, variant="a")
    job_yaml = fleet["job_yaml"]
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    bad_vm = fleet["vm_names"][1]
    (state_dir / f"{bad_vm}_state.txt").write_text("PowerState/deallocated\n")

    # Pre-inject script in python prelude to patch dispatcher.get_power_state to call real function twice for bad_vm
    patch_code = f"""
import y2shared.dispatcher as disp
orig_gps = disp.get_power_state
def bad_gps(row, *args, **kwargs):
    res = orig_gps(row, *args, **kwargs)
    if row.vm_name == "{bad_vm}":
        orig_gps(row, *args, **kwargs)
    return res
disp.get_power_state = bad_gps
"""
    env = {"FAKE_AZ_STATE_DIR": str(state_dir)}
    res = run_cli_with_prelude("y2b", ["run", str(job_yaml), "--phase", "all", "-y"], prelude=patch_code, cwd=tmp_path, env=env)
    assert res.returncode in (0, 2)
    assert "conformance: 2/3 VMs matched plan" in res.stdout
    assert f"conformance: {bad_vm}: unexpected power-state" in res.stderr

    # Check result.log for bad_vm has note
    result_log = (tmp_path / "result.log").read_text(encoding="utf-8")
    assert f"conformance: {bad_vm}: unexpected power-state" in result_log


def test_conformance_injected_missing_command(tmp_path: Path):
    """Test read_results returning without az call causes missing command mismatch."""
    fleet = make_fleet(tmp_path, n=3, variant="a")
    job_yaml = fleet["job_yaml"]
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    bad_vm = fleet["vm_names"][1]

    patch_code = f"""
import y2shared.dispatcher as disp
orig_read = disp.read_results
def bad_read(row, *args, **kwargs):
    if row.vm_name == "{bad_vm}":
        from y2shared.dispatcher import ReadResult
        return ReadResult(status="OK", vars={{"status": "0"}})
    return orig_read(row, *args, **kwargs)
disp.read_results = bad_read
"""
    env = {"FAKE_AZ_STATE_DIR": str(state_dir)}
    res = run_cli_with_prelude("y2b", ["run", str(job_yaml), "--phase", "all", "-y"], prelude=patch_code, cwd=tmp_path, env=env)
    assert res.returncode in (0, 2)
    assert "conformance: 2/3 VMs matched plan" in res.stdout
    assert f"conformance: {bad_vm}: missing reader" in res.stderr


def test_conformance_duplicate_post_mismatch(tmp_path: Path):
    """Test duplicate POST causes conformance mismatch."""
    fleet = make_fleet(tmp_path, n=3, variant="a")
    job_yaml = fleet["job_yaml"]
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    bad_vm = fleet["vm_names"][1]

    patch_code = f"""
import y2shared.dispatcher as disp
orig_inv_nw = disp.invoke_nowait
def bad_inv_nw(row, *args, **kwargs):
    res = orig_inv_nw(row, *args, **kwargs)
    if row.vm_name == "{bad_vm}":
        orig_inv_nw(row, *args, **kwargs)
    return res
disp.invoke_nowait = bad_inv_nw
"""
    env = {"FAKE_AZ_STATE_DIR": str(state_dir)}
    res = run_cli_with_prelude("y2b", ["run", str(job_yaml), "--phase", "all", "-y"], prelude=patch_code, cwd=tmp_path, env=env)
    assert res.returncode in (0, 2)
    assert "conformance: 2/3 VMs matched plan" in res.stdout
    assert f"conformance: {bad_vm}: unexpected invoke" in res.stderr


def test_conformance_tf_build_warning_on_mismatch(tmp_path: Path):
    """Test TF_BUILD=True produces ##vso[task.logissue type=warning] on mismatch and zero when unset."""
    fleet = make_fleet(tmp_path, n=2, variant="a")
    job_yaml = fleet["job_yaml"]
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    bad_vm = fleet["vm_names"][0]
    (state_dir / f"{bad_vm}_state.txt").write_text("PowerState/deallocated\n")

    patch_code = f"""
import y2shared.dispatcher as disp
orig_gps = disp.get_power_state
def bad_gps(row, *args, **kwargs):
    res = orig_gps(row, *args, **kwargs)
    if row.vm_name == "{bad_vm}":
        orig_gps(row, *args, **kwargs)
    return res
disp.get_power_state = bad_gps
"""
    # 1. With TF_BUILD=True
    env_tf = {"FAKE_AZ_STATE_DIR": str(state_dir), "TF_BUILD": "True"}
    res_tf = run_cli_with_prelude("y2b", ["run", str(job_yaml), "--phase", "all", "-y"], prelude=patch_code, cwd=tmp_path, env=env_tf)
    assert res_tf.returncode in (0, 2)
    assert f"##vso[task.logissue type=warning]conformance: {bad_vm}:" in res_tf.stdout

    # 2. Without TF_BUILD
    env_no_tf = {"FAKE_AZ_STATE_DIR": str(state_dir)}
    res_no_tf = run_cli_with_prelude("y2b", ["run", str(job_yaml), "--phase", "all", "-y"], prelude=patch_code, cwd=tmp_path, env=env_no_tf)
    assert res_no_tf.returncode in (0, 2)
    assert "##vso" not in res_no_tf.stdout
    assert "##vso" not in res_no_tf.stderr
    result_log = (tmp_path / "result.log").read_text(encoding="utf-8")
    assert "##vso" not in result_log


def test_conformance_host_script_commands_not_matched(tmp_path: Path):
    """Test that extra az calls inside host scripts do not cause conformance mismatch."""
    fleet = make_fleet(tmp_path, n=2, variant="b")  # variant b has az in precheck and column
    job_yaml = fleet["job_yaml"]
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env = {"FAKE_AZ_STATE_DIR": str(state_dir)}

    res = run_cli("y2b", ["run", str(job_yaml), "--phase", "all", "-y"], cwd=tmp_path, env=env)
    assert res.returncode in (0, 2)
    assert "conformance: 2/2 VMs matched plan" in res.stdout


def test_conformance_resume_counts_only_non_ledgered_vms(tmp_path: Path):
    """Test that --resume counts only non-ledgered VMs in total n."""
    fleet = make_fleet(tmp_path, n=5, variant="a")
    job_yaml = fleet["job_yaml"]
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env = {"FAKE_AZ_STATE_DIR": str(state_dir)}

    # Create ledger for vm_0000 and vm_0001 (index 1 and 2 in CSV)
    parts_dir = get_parts_dir(tmp_path / "result.csv")
    parts_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = parts_dir / "collect_list.jsonl"
    r1 = {
        "index": 1, "vm_name": fleet["vm_names"][0], "resource_group": "rg", "subscription": "sub",
        "run_command_name": "RunShellScript", "start_time": "2026-11-01T12:00:00Z", "confirmed": True,
        "log": "line1", "previous_power_state": "running"
    }
    r2 = {
        "index": 2, "vm_name": fleet["vm_names"][1], "resource_group": "rg", "subscription": "sub",
        "run_command_name": "RunShellScript", "start_time": "2026-11-01T12:00:00Z", "confirmed": True,
        "log": "line1", "previous_power_state": "running"
    }
    ledger_path.write_text(json.dumps(r1) + "\n" + json.dumps(r2) + "\n", encoding="utf-8")

    _, job_hash = load_yaml_raw(job_yaml)
    run_json = parts_dir / "run.json"
    run_meta = {
        "job_hash": job_hash,
        "phase": "invoke",
        "run_id": "test_run",
    }
    run_json.write_text(json.dumps(run_meta), encoding="utf-8")

    # Run invoke --resume: out of 5 VMs, 2 are in ledger, so 3 remain
    res = run_cli("y2b", ["run", str(job_yaml), "--phase", "invoke", "--resume", "-y"], cwd=tmp_path, env=env)
    assert res.returncode in (0, 2)
    assert "conformance: 3/3 VMs matched plan" in res.stdout


def run_cli_with_prelude(
    tool: str,
    argv: List[str],
    prelude: str,
    env: Dict[str, str],
    cwd: Path,
) -> Any:
    """Helper to run CLI with a python prelude modifying dispatcher."""
    repo_root = Path(__file__).resolve().parent.parent
    fake_az_dir = repo_root / "tests" / "fake_az"
    cmd_env = dict(os.environ)
    cmd_env.update(env)
    cmd_env["PYTHONPATH"] = str(repo_root)
    cmd_env["PATH"] = f"{fake_az_dir}:{cmd_env.get('PATH', '')}"

    prelude_script = f"""
import sys
import y2shared.dispatcher as d
orig_az = d._az_run_command
d._az_run_command = lambda cmd, timeout=30, **kw: orig_az(cmd, timeout=timeout, **dict(kw, delay_multiplier=0.0, sleep_fn=lambda s: None))
{prelude}
import y2shared.cli as c
from y2b.profile import PROFILE as b_prof
from y2p.profile import PROFILE as p_prof

prof = b_prof if "{tool}" == "y2b" else p_prof
sys.exit(c.main(prof, sys.argv[2:]))
"""
    return subprocess.run(
        [sys.executable, "-c", prelude_script, tool] + argv,
        capture_output=True,
        text=True,
        env=cmd_env,
        cwd=str(cwd),
    )
