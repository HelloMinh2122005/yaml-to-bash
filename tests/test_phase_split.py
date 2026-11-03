"""Tier 1 tests for run --phase invoke | collect | all (TP-01, TP-02, TP-03, TP-06)."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from tests.helpers_ado import (
    classify_argv,
    make_fleet,
    parse_events,
    run_cli,
)
from y2b.profile import PROFILE as PROFILE_B
from y2p.profile import PROFILE as PROFILE_P
from y2shared.cli import build_parser, main
from y2shared.config import load_yaml_raw, parse_job_from_dict
from y2shared.parts import get_parts_dir, update_power, write_power_once
from y2shared.runner import run_job


# ==============================================================================
# TP-01: invoke_only
# ==============================================================================


@pytest.mark.parametrize("tool", ["y2b", "y2p"])
def test_invoke_only_basic(tmp_path: Path, tool: str) -> None:
    os_type = "linux" if tool == "y2b" else "windows"
    fleet = make_fleet(tmp_path / "fleet", n=5, os_type=os_type, variant="a")

    job_yaml = fleet["job_yaml"]
    raw_job = yaml.safe_load(job_yaml.read_text(encoding="utf-8"))
    raw_job["parallel"] = 1
    # Set precheck on vm-0002 to skip
    scripts_dir = fleet["scripts_dir"]
    chk_state = scripts_dir / "check_state.sh"
    chk_state.write_text(
        '#!/bin/bash\n'
        'if [ "$VM_NAME" = "vm-0002" ] || [ "$VM_NAME" = "lin02" ] || [ "$VM_NAME" = "win02" ]; then\n'
        '  echo "skip_me"\n'
        'else\n'
        '  echo "PowerState/running"\n'
        'fi\n',
        encoding="utf-8",
    )
    raw_job["pre_run"][0]["result"]["skip_me"] = {"log": "skip", "action": "skip"}
    job_yaml.write_text(yaml.dump(raw_job), encoding="utf-8")

    events_file = tmp_path / "events.jsonl"
    fake_az_log = tmp_path / "fake_az.log"
    env = {
        "FAKE_AZ_EVENTS": str(events_file),
        "FAKE_AZ_LOG": str(fake_az_log),
        "FAKE_AZ_RC_vm-0003": "missing",
        "FAKE_AZ_RC_lin03": "missing",
        "FAKE_AZ_RC_win03": "missing",
    }

    res = run_cli(tool, ["run", str(job_yaml), "--phase", "invoke", "-y"], env=env, cwd=fleet["dir"])
    assert res.returncode == 0, f"stdout: {res.stdout}\nstderr: {res.stderr}"

    stdout_lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    assert stdout_lines[-1] == "fired=3", f"Expected fired=3 as last stdout line, got: {stdout_lines[-1]}"

    # result.csv and result.log do NOT exist
    assert not (fleet["dir"] / "result.csv").exists()
    assert not (fleet["dir"] / "result.log").exists()

    # Parts folder exists
    parts_dir = get_parts_dir(fleet["dir"] / "result.csv")
    assert parts_dir.exists()
    assert (parts_dir / "run.json").exists()
    run_meta = json.loads((parts_dir / "run.json").read_text(encoding="utf-8"))
    assert run_meta.get("phase") == "collect"

    ledger_path = parts_dir / "collect_list.jsonl"
    assert ledger_path.exists()

    # Events assert: 3 invoke ok events, 0 reader, 0 deallocate, 0 powerOff
    events = parse_events(events_file)
    invoke_events = [e for e in events if e.get("kind") == "invoke" and e.get("outcome") == "ok"]
    reader_events = [e for e in events if e.get("kind") == "reader"]
    power_events = [e for e in events if e.get("kind") in ("deallocate", "powerOff")]
    assert len(invoke_events) == 3
    assert len(reader_events) == 0
    assert len(power_events) == 0


def test_invoke_only_all_skipped(tmp_path: Path) -> None:
    fleet = make_fleet(tmp_path / "fleet", n=3, os_type="linux", variant="a")
    job_yaml = fleet["job_yaml"]
    raw_job = yaml.safe_load(job_yaml.read_text(encoding="utf-8"))

    scripts_dir = fleet["scripts_dir"]
    chk_state = scripts_dir / "check_state.sh"
    chk_state.write_text('#!/bin/bash\necho "skip_all"\n', encoding="utf-8")
    raw_job["pre_run"][0]["result"]["skip_all"] = {"log": "skip", "action": "skip"}
    job_yaml.write_text(yaml.dump(raw_job), encoding="utf-8")

    events_file = tmp_path / "events.jsonl"
    env = {"FAKE_AZ_EVENTS": str(events_file)}

    res = run_cli("y2b", ["run", str(job_yaml), "--phase", "invoke", "-y"], env=env, cwd=fleet["dir"])
    assert res.returncode == 0

    stdout_lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    assert stdout_lines[-1] == "fired=0"

    parts_dir = get_parts_dir(fleet["dir"] / "result.csv")
    assert parts_dir.exists()
    ledger_path = parts_dir / "collect_list.jsonl"
    assert ledger_path.exists()
    assert ledger_path.stat().st_size == 0

    run_meta = json.loads((parts_dir / "run.json").read_text(encoding="utf-8"))
    assert run_meta.get("phase") == "collect"

    events = parse_events(events_file)
    invoke_events = [e for e in events if e.get("kind") == "invoke"]
    assert len(invoke_events) == 0


def test_invoke_only_abort_exit_3(tmp_path: Path) -> None:
    fleet = make_fleet(tmp_path / "fleet", n=6, os_type="linux", variant="a")
    job_yaml = fleet["job_yaml"]
    raw_job = yaml.safe_load(job_yaml.read_text(encoding="utf-8"))
    raw_job["parallel"] = 1
    job_yaml.write_text(yaml.dump(raw_job), encoding="utf-8")

    events_file = tmp_path / "events.jsonl"
    env = {
        "FAKE_AZ_EVENTS": str(events_file),
        "FAKE_AZ_RC_DEFAULT": "auth",
    }

    res = run_cli("y2b", ["run", str(job_yaml), "--phase", "invoke", "-y"], env=env, cwd=fleet["dir"])
    assert res.returncode == 3

    stdout_lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    assert stdout_lines[-1].startswith("fired=")

    parts_dir = get_parts_dir(fleet["dir"] / "result.csv")
    assert parts_dir.exists()
    ledger_path = parts_dir / "collect_list.jsonl"
    assert ledger_path.exists()

    run_meta = json.loads((parts_dir / "run.json").read_text(encoding="utf-8"))
    assert run_meta.get("phase") == "invoke"
    assert not (fleet["dir"] / "result.csv").exists()


def test_invoke_only_exit_1(tmp_path: Path) -> None:
    fleet = make_fleet(tmp_path / "fleet", n=2, os_type="linux", variant="a")
    job_yaml = fleet["job_yaml"]

    # First invoke
    res1 = run_cli("y2b", ["run", str(job_yaml), "--phase", "invoke", "-y"], cwd=fleet["dir"])
    assert res1.returncode == 0

    # Second invoke without --resume -> exit 1
    res2 = run_cli("y2b", ["run", str(job_yaml), "--phase", "invoke", "-y"], cwd=fleet["dir"])
    assert res2.returncode == 1
    assert "already exists" in res2.stderr


def test_invoke_only_resume_does_not_refire(tmp_path: Path) -> None:
    fleet = make_fleet(tmp_path / "fleet", n=2, os_type="linux", variant="a")
    job_yaml = fleet["job_yaml"]

    # Pre-populate parts dir with vm-0001 confirmed
    parts_dir = get_parts_dir(fleet["dir"] / "result.csv")
    parts_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = parts_dir / "collect_list.jsonl"
    vm1_name = "lin01"
    ledger_record = {
        "index": 1,
        "vm_name": vm1_name,
        "run_command_name": "RunShellScript",
        "start_time": "2026-11-01T12:00:00Z",
        "confirmed": True,
        "log": f"vm {vm1_name} confirmed",
    }
    ledger_path.write_text(json.dumps(ledger_record) + "\n", encoding="utf-8")

    _, job_hash = load_yaml_raw(job_yaml)
    run_json = parts_dir / "run.json"
    run_meta = {
        "job_hash": job_hash,
        "phase": "invoke",
        "run_id": "test_run",
    }
    run_json.write_text(json.dumps(run_meta), encoding="utf-8")

    events_file = tmp_path / "events.jsonl"
    env = {"FAKE_AZ_EVENTS": str(events_file)}

    res = run_cli(
        "y2b",
        ["run", str(job_yaml), "--phase", "invoke", "--resume", "-y"],
        env=env,
        cwd=fleet["dir"],
    )
    assert res.returncode == 0
    stdout_lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    assert stdout_lines[-1] == "fired=2"

    events = parse_events(events_file)
    invoke_vms = [e.get("vm") for e in events if e.get("kind") == "invoke"]
    assert vm1_name not in invoke_vms
    assert len(invoke_vms) == 1


# ==============================================================================
# TP-02: collect_only
# ==============================================================================


def test_collect_only_d1(tmp_path: Path) -> None:
    sentinel = tmp_path / "sentinel.txt"
    fleet = make_fleet(tmp_path / "fleet", n=3, os_type="linux", variant="d1", sentinel_file=sentinel)
    job_yaml = fleet["job_yaml"]

    # Run invoke phase
    res_inv = run_cli("y2b", ["run", str(job_yaml), "--phase", "invoke", "-y"], cwd=fleet["dir"])
    assert res_inv.returncode == 0

    sentinel_inv_lines = sentinel.read_text(encoding="utf-8").splitlines() if sentinel.exists() else []

    events_file = tmp_path / "events_collect.jsonl"
    env = {"FAKE_AZ_EVENTS": str(events_file)}

    # Run collect phase
    res_col = run_cli("y2b", ["run", str(job_yaml), "--phase", "collect", "-y"], env=env, cwd=fleet["dir"])
    assert res_col.returncode == 0

    # Assert no new invoke: lines in sentinel
    sentinel_all_lines = sentinel.read_text(encoding="utf-8").splitlines() if sentinel.exists() else []
    new_sentinel = sentinel_all_lines[len(sentinel_inv_lines):]
    assert not any(ln.startswith("invoke:") for ln in new_sentinel)

    # Forbidden Phase 1 work: 0 events of kind invoke
    events = parse_events(events_file)
    assert not any(e.get("kind") == "invoke" for e in events)
    assert not any(e.get("kind") == "vm-start" for e in events)

    # Readers == 3
    readers = [e for e in events if e.get("kind") == "reader"]
    assert len(readers) == 3

    # result.csv exists with 3 rows
    res_csv = fleet["dir"] / "result.csv"
    assert res_csv.exists()
    csv_lines = [ln.strip() for ln in res_csv.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(csv_lines) == 4  # header + 3 data rows


def test_collect_only_d2_start_stop(tmp_path: Path) -> None:
    sentinel = tmp_path / "sentinel.txt"
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)

    fleet = make_fleet(
        tmp_path / "fleet",
        n=2,
        os_type="linux",
        variant="d2",
        sentinel_file=sentinel,
    )
    job_yaml = fleet["job_yaml"]

    env_inv = {"FAKE_AZ_STATE_DIR": str(state_dir)}
    res_inv = run_cli("y2b", ["run", str(job_yaml), "--phase", "invoke", "-y"], env=env_inv, cwd=fleet["dir"])
    assert res_inv.returncode == 0

    # Set VMs state to deallocated before collect
    for i in range(1, 3):
        (state_dir / f"lin{i:02d}_state.txt").write_text("PowerState/deallocated\n", encoding="utf-8")

    events_file = tmp_path / "events_col.jsonl"
    env = {
        "FAKE_AZ_EVENTS": str(events_file),
        "FAKE_AZ_STATE_DIR": str(state_dir),
    }

    res_col = run_cli("y2b", ["run", str(job_yaml), "--phase", "collect", "-y"], env=env, cwd=fleet["dir"])
    assert res_col.returncode == 0

    events = parse_events(events_file)
    # 0 POSTs (invoke)
    assert not any(e.get("kind") == "invoke" for e in events)
    # 2 vm-starts from Phase 2 collect-phase precheck
    vm_starts = [e for e in events if e.get("kind") == "vm-start"]
    assert len(vm_starts) == 2


def test_collect_only_row_absent_from_ledger(tmp_path: Path) -> None:
    sentinel = tmp_path / "sentinel.txt"
    fleet = make_fleet(tmp_path / "fleet", n=3, os_type="linux", variant="d1", sentinel_file=sentinel)
    job_yaml = fleet["job_yaml"]

    res_inv = run_cli("y2b", ["run", str(job_yaml), "--phase", "invoke", "-y"], cwd=fleet["dir"])
    assert res_inv.returncode == 0

    # Remove lin03 from collect_list.jsonl
    parts_dir = get_parts_dir(fleet["dir"] / "result.csv")
    ledger_file = parts_dir / "collect_list.jsonl"
    lines = ledger_file.read_text(encoding="utf-8").splitlines()
    filtered = [ln for ln in lines if "lin03" not in ln]
    ledger_file.write_text("\n".join(filtered) + "\n", encoding="utf-8")

    events_file = tmp_path / "events_col.jsonl"
    env = {"FAKE_AZ_EVENTS": str(events_file)}

    res_col = run_cli("y2b", ["run", str(job_yaml), "--phase", "collect", "-y"], env=env, cwd=fleet["dir"])
    assert res_col.returncode == 2

    events = parse_events(events_file)
    # Zero az calls for lin03
    lin03_events = [e for e in events if e.get("vm") == "lin03"]
    assert len(lin03_events) == 0

    # result.csv has ERROR:no_result for lin03
    csv_text = (fleet["dir"] / "result.csv").read_text(encoding="utf-8")
    assert "ERROR:no_result" in csv_text


def test_collect_only_missing_files_and_hash_mismatch(tmp_path: Path) -> None:
    # 1. Missing run.json
    fleet1 = make_fleet(tmp_path / "f1", n=2, os_type="linux", variant="a")
    res_inv1 = run_cli("y2b", ["run", str(fleet1["job_yaml"]), "--phase", "invoke", "-y"], cwd=fleet1["dir"])
    assert res_inv1.returncode == 0
    parts1 = get_parts_dir(fleet1["dir"] / "result.csv")
    (parts1 / "run.json").unlink()
    res1 = run_cli("y2b", ["run", str(fleet1["job_yaml"]), "--phase", "collect", "-y"], cwd=fleet1["dir"])
    assert res1.returncode == 1
    assert "run.json" in res1.stderr

    # 2. Missing collect_list.jsonl
    fleet2 = make_fleet(tmp_path / "f2", n=2, os_type="linux", variant="a")
    res_inv2 = run_cli("y2b", ["run", str(fleet2["job_yaml"]), "--phase", "invoke", "-y"], cwd=fleet2["dir"])
    assert res_inv2.returncode == 0
    parts2 = get_parts_dir(fleet2["dir"] / "result.csv")
    (parts2 / "collect_list.jsonl").unlink()
    res2 = run_cli("y2b", ["run", str(fleet2["job_yaml"]), "--phase", "collect", "-y"], cwd=fleet2["dir"])
    assert res2.returncode == 1
    assert "collect_list.jsonl" in res2.stderr

    # 3. Hash mismatch
    fleet3 = make_fleet(tmp_path / "f3", n=2, os_type="linux", variant="a")
    res_inv3 = run_cli("y2b", ["run", str(fleet3["job_yaml"]), "--phase", "invoke", "-y"], cwd=fleet3["dir"])
    assert res_inv3.returncode == 0
    # Modify job.yaml
    with open(fleet3["job_yaml"], "a", encoding="utf-8") as f:
        f.write("# comment\n")
    res3 = run_cli("y2b", ["run", str(fleet3["job_yaml"]), "--phase", "collect", "-y"], cwd=fleet3["dir"])
    assert res3.returncode == 1
    assert "job hash" in res3.stderr.lower() or "hash" in res3.stderr.lower()


def test_collect_only_stored_phase_invoke(tmp_path: Path) -> None:
    fleet = make_fleet(tmp_path / "fleet", n=2, os_type="linux", variant="a")
    res_inv = run_cli("y2b", ["run", str(fleet["job_yaml"]), "--phase", "invoke", "-y"], cwd=fleet["dir"])
    assert res_inv.returncode == 0

    parts_dir = get_parts_dir(fleet["dir"] / "result.csv")
    run_json = parts_dir / "run.json"
    meta = json.loads(run_json.read_text(encoding="utf-8"))
    meta["phase"] = "invoke"
    run_json.write_text(json.dumps(meta), encoding="utf-8")

    res_col = run_cli("y2b", ["run", str(fleet["job_yaml"]), "--phase", "collect", "-y"], cwd=fleet["dir"])
    assert res_col.returncode in (0, 2)
    assert "phase=invoke" in res_col.stderr or "invoke" in res_col.stderr


# ==============================================================================
# TP-03: arg_rules
# ==============================================================================


def test_arg_rules_default_all() -> None:
    parser = build_parser(PROFILE_B)
    args = parser.parse_args(["run", "job.yaml"])
    assert args.phase == "all"


def test_arg_rules_bogus_phase() -> None:
    parser = build_parser(PROFILE_B)
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["run", "job.yaml", "--phase", "bogus"])
    assert exc.value.code == 2


def test_arg_rules_dry_run_with_phase(tmp_path: Path) -> None:
    fleet = make_fleet(tmp_path / "fleet", n=1, os_type="linux", variant="a")
    job_yaml = str(fleet["job_yaml"])

    res_inv = run_cli("y2b", ["run", job_yaml, "--phase", "invoke", "--dry-run"], cwd=fleet["dir"])
    assert res_inv.returncode == 1
    assert "cannot combine" in res_inv.stderr.lower() or "--dry-run" in res_inv.stderr

    res_col = run_cli("y2b", ["run", job_yaml, "--phase", "collect", "--dry-run"], cwd=fleet["dir"])
    assert res_col.returncode == 1
    assert "cannot combine" in res_col.stderr.lower() or "--dry-run" in res_col.stderr


def test_arg_rules_subcommands_registered() -> None:
    for tool in ["y2b", "y2p"]:
        res = run_cli(tool, ["--help"])
        assert res.returncode == 0
        assert "explain" in res.stdout
        assert "export-ado" in res.stdout


# ==============================================================================
# TP-06: aborted_invoke_restores_power
# ==============================================================================


def test_aborted_invoke_restores_power(tmp_path: Path) -> None:
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)

    fleet = make_fleet(
        tmp_path / "fleet",
        n=6,
        os_type="linux",
        variant="a",
        states={"lin01": "PowerState/stopped"},
    )
    job_yaml = fleet["job_yaml"]
    raw_job = yaml.safe_load(job_yaml.read_text(encoding="utf-8"))
    raw_job["parallel"] = 1
    job_yaml.write_text(yaml.dump(raw_job), encoding="utf-8")

    env_inv = {
        "FAKE_AZ_DEFAULT_STATE": "PowerState/deallocated",
        "FAKE_AZ_STATE_lin01": "PowerState/stopped",
        "FAKE_AZ_STATE_DIR": str(state_dir),
        "FAKE_AZ_RC_DEFAULT": "auth",
    }

    # Run invoke phase which will abort
    res_inv = run_cli(
        "y2b",
        ["run", str(job_yaml), "--phase", "invoke", "-y"],
        env=env_inv,
        cwd=fleet["dir"],
    )
    assert res_inv.returncode == 3

    # Run collect phase without FAKE_AZ_RC_DEFAULT
    env_col = {
        "FAKE_AZ_DEFAULT_STATE": "PowerState/deallocated",
        "FAKE_AZ_STATE_DIR": str(state_dir),
    }
    res_col = run_cli(
        "y2b",
        ["run", str(job_yaml), "--phase", "collect", "-y"],
        env=env_col,
        cwd=fleet["dir"],
    )
    assert res_col.returncode == 2

    # Verify result.csv has 6 rows
    res_csv = fleet["dir"] / "result.csv"
    assert res_csv.exists()
    lines = [ln.strip() for ln in res_csv.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 7  # header + 6 data rows


# ==============================================================================
# TP-04 & TP-05: split_equals_single (y2b & y2p)
# ==============================================================================


def _normalise_csv_text(text: str) -> str:
    # Normalize ISO timestamps and run_id tokens
    t = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?", "<TIMESTAMP>", text)
    t = re.sub(r"\d{8}T\d{6}Z-[0-9a-f]{8}", "<RUN_ID>", t)
    return t


def _run_split_equals_single(tmp_path: Path, tool: str) -> None:
    os_type = "linux" if tool == "y2b" else "windows"
    prefix = "lin" if os_type == "linux" else "win"
    vms = [f"{prefix}{i:02d}" for i in range(1, 7)]
    states = {
        vms[0]: "PowerState/running",
        vms[1]: "PowerState/running",
        vms[2]: "PowerState/running",
        vms[3]: "PowerState/deallocated",
        vms[4]: "PowerState/deallocated",
        vms[5]: "PowerState/deallocated",
    }

    # Setup directories
    dir_a = tmp_path / "A"
    dir_b1 = tmp_path / "B1"
    dir_b2 = tmp_path / "B2"
    state_dir_a = tmp_path / "state_a"
    state_dir_b = tmp_path / "state_b"
    state_dir_a.mkdir(parents=True)
    state_dir_b.mkdir(parents=True)

    for vm, st in states.items():
        (state_dir_a / f"{vm}_state.txt").write_text(st, encoding="utf-8")
        (state_dir_b / f"{vm}_state.txt").write_text(st, encoding="utf-8")

    fleet_a = make_fleet(dir_a, n=6, os_type=os_type, sub="name", variant="a", collect_retries=3)
    fleet_b1 = make_fleet(dir_b1, n=6, os_type=os_type, sub="name", variant="a", collect_retries=3)

    # Injections
    vm2, vm3, vm4 = vms[1], vms[2], vms[3]
    injections = {
        f"FAKE_AZ_POST_THROTTLE_COUNT_{vm2}": "1",
        f"FAKE_AZ_BUSY_COUNT_{vm3}": "2",
        f"FAKE_AZ_POST_BUSY_COUNT_{vm4}": "1",
    }
    if tool == "y2p":
        injections["FAKE_AZ_EXPECT_CMD"] = "RunPowerShellScript"

    # Scenario A: --phase all
    events_a = tmp_path / "events_a.jsonl"
    env_a = dict(injections)
    env_a["FAKE_AZ_STATE_DIR"] = str(state_dir_a)
    env_a["FAKE_AZ_EVENTS"] = str(events_a)

    res_a = run_cli(tool, ["run", str(fleet_a["job_yaml"]), "--phase", "all", "-y"], env=env_a, cwd=dir_a)
    assert res_a.returncode == 0, f"Scenario A failed:\nstdout={res_a.stdout}\nstderr={res_a.stderr}"

    # Scenario B: B1 invoke
    events_b = tmp_path / "events_b.jsonl"
    env_b = dict(injections)
    env_b["FAKE_AZ_STATE_DIR"] = str(state_dir_b)
    env_b["FAKE_AZ_EVENTS"] = str(events_b)

    res_b1 = run_cli(tool, ["run", str(fleet_b1["job_yaml"]), "--phase", "invoke", "-y"], env=env_b, cwd=dir_b1)
    assert res_b1.returncode == 0, f"Scenario B1 failed:\nstdout={res_b1.stdout}\nstderr={res_b1.stderr}"
    assert not (dir_b1 / "result.csv").exists()

    # Copy fleet_b1 to B2 (without .result.parts)
    shutil.copytree(dir_b1, dir_b2, ignore=shutil.ignore_patterns(".*"))

    # Tar .result.parts in B1 and extract into B2
    tar_path = tmp_path / "parts.tar"
    subprocess.run(["tar", "-C", str(dir_b1), "-cf", str(tar_path), ".result.parts"], check=True)
    subprocess.run(["tar", "-C", str(dir_b2), "-xf", str(tar_path)], check=True)

    if tool == "y2p":
        orig_ps1 = dir_b1 / ".result.parts" / "remote.ps1"
        restored_ps1 = dir_b2 / ".result.parts" / "remote.ps1"
        assert restored_ps1.read_bytes() == orig_ps1.read_bytes()
        if orig_ps1.read_bytes().startswith(b"\xef\xbb\xbf"):
            assert restored_ps1.read_bytes().startswith(b"\xef\xbb\xbf")

    events_before_b2 = len(parse_events(events_b))

    # Scenario B: B2 collect
    res_b2 = run_cli(tool, ["run", str(dir_b2 / "job.yaml"), "--phase", "collect", "-y"], env=env_b, cwd=dir_b2)
    assert res_b2.returncode == 0, f"Scenario B2 failed:\nstdout={res_b2.stdout}\nstderr={res_b2.stderr}"
    assert (dir_b2 / "result.csv").exists()
    assert (dir_b2 / "result.log").exists()

    # Verify B2 produced 0 invoke events and 0 start events
    events_b_all = parse_events(events_b)
    events_b2_only = events_b_all[events_before_b2:]
    assert not any(e.get("kind") == "invoke" for e in events_b2_only)
    assert not any(e.get("kind") == "start" for e in events_b2_only)

    # Compare result.csv ignoring timestamps/run-ids
    csv_a = _normalise_csv_text((dir_a / "result.csv").read_text(encoding="utf-8"))
    csv_b2 = _normalise_csv_text((dir_b2 / "result.csv").read_text(encoding="utf-8"))
    assert csv_a == csv_b2

    # Verify FX-1 events per VM
    events_a_all = parse_events(events_a)
    for ev_list in [events_a_all, events_b_all]:
        for vm in vms:
            vm_evs = [e for e in ev_list if e.get("vm") == vm]
            ok_invokes = [e for e in vm_evs if e.get("kind") == "invoke" and e.get("outcome") == "ok"]
            ok_readers = [e for e in vm_evs if e.get("kind") == "reader" and e.get("outcome") == "ok"]
            assert len(ok_invokes) == 1, f"Expected 1 ok invoke for {vm}, got {len(ok_invokes)}"
            assert len(ok_readers) == 1, f"Expected 1 ok reader for {vm}, got {len(ok_readers)}"

        # vm2 has 1 throttled invoke
        vm2_throttled = [e for e in ev_list if e.get("vm") == vm2 and e.get("kind") == "invoke" and e.get("outcome") == "throttled"]
        assert len(vm2_throttled) == 1

        # vm4 has 1 conflict invoke
        vm4_conflict = [e for e in ev_list if e.get("vm") == vm4 and e.get("kind") == "invoke" and e.get("outcome") == "conflict"]
        assert len(vm4_conflict) == 1

        # vm3 has 2 conflict readers
        vm3_conflict = [e for e in ev_list if e.get("vm") == vm3 and e.get("kind") == "reader" and e.get("outcome") == "conflict"]
        assert len(vm3_conflict) == 2

    # Power state and counters equal
    for vm in vms:
        state_file_a = state_dir_a / f"{vm}_state.txt"
        state_file_b = state_dir_b / f"{vm}_state.txt"
        assert state_file_a.read_text(encoding="utf-8") == state_file_b.read_text(encoding="utf-8")

        for action in ["start", "deallocate", "poweroff"]:
            cnt_a = (state_dir_a / f"{vm}_{action}.txt").read_text(encoding="utf-8").strip() if (state_dir_a / f"{vm}_{action}.txt").exists() else "0"
            cnt_b = (state_dir_b / f"{vm}_{action}.txt").read_text(encoding="utf-8").strip() if (state_dir_b / f"{vm}_{action}.txt").exists() else "0"
            assert cnt_a == cnt_b, f"Mismatch for {vm}_{action}: A={cnt_a}, B={cnt_b}"


def test_split_equals_single_y2b(tmp_path: Path) -> None:
    """TP-04: split run equals single run (y2b, RunShellScript)."""
    _run_split_equals_single(tmp_path, "y2b")


def test_split_equals_single_y2p(tmp_path: Path) -> None:
    """TP-05: split run equals single run (y2p, RunPowerShellScript)."""
    _run_split_equals_single(tmp_path, "y2p")

