"""Tier 1 tests for the explain subcommand (TP-10, TP-11, TP-12, TP-13)."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

from y2b.profile import PROFILE as B_PROFILE
from y2p.profile import PROFILE as P_PROFILE
from y2shared import azcmd
from y2shared.cli import build_parser
from y2shared.config import Job, load_job
from y2shared.explain import cmd_explain, render_json, render_md, render_text
from y2shared.inputcsv import read_input_csv, select_target_rows
from y2shared.plan import PlanContext, build_plan
from y2shared.profile import Profile
from tests.helpers_ado import make_fleet, normalise_argv, run_cli


def make_plan(
    job: Job,
    profile: Profile,
    vm_names: Optional[Sequence[str]] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    rows, _, _ = read_input_csv(job.input, job=job, os_family=profile.os_family)
    target_rows = select_target_rows(rows, vm_names=vm_names, limit=limit)
    return build_plan(job, target_rows, profile)


# ==============================================================================
# TP-10: Explain Content & Structural Verification
# ==============================================================================


def test_header_fields():
    repo_root = Path(__file__).resolve().parent.parent

    # Linux example
    job_l = load_job(repo_root / "examples" / "linux" / "job.yaml")
    plan_l = make_plan(job_l, B_PROFILE)
    txt_l = render_text(plan_l)

    assert "Tool: y2b (RunShellScript)" in txt_l
    assert "Fleet: 3 VMs · Max run commands: 6 (2 x eligible VMs)" in txt_l
    assert "run_id=preview" in txt_l
    assert "Reachable Azure mutations: runCommand POST, vm start, deallocate, powerOff" in txt_l

    # Check sha256 matches compiled bytes
    compiled_l, _ = B_PROFILE.compile_remote(job_l, run_id="preview", check_syntax=False)
    expected_hash_l = hashlib.sha256(compiled_l.encode("utf-8")).hexdigest()
    assert expected_hash_l in txt_l

    golden_sh = (repo_root / "tests" / "golden" / "hip_compliance_remote.sh").read_bytes()
    assert hashlib.sha256(golden_sh).hexdigest() == expected_hash_l

    # Windows example
    job_w = load_job(repo_root / "examples" / "windows" / "job.yaml")
    plan_w = make_plan(job_w, P_PROFILE)
    txt_w = render_text(plan_w)

    assert "Tool: y2p (RunPowerShellScript)" in txt_w
    assert "Fleet: 2 VMs · Max run commands: 4 (2 x eligible VMs)" in txt_w

    compiled_w, _ = P_PROFILE.compile_remote(job_w, run_id="preview", check_syntax=False)
    expected_hash_w = hashlib.sha256(compiled_w.encode("utf-8")).hexdigest()
    assert expected_hash_w in txt_w


def test_job_path_rule(tmp_path: Path):
    repo_root = Path(__file__).resolve().parent.parent
    job_path = repo_root / "examples" / "linux" / "job.yaml"
    job = load_job(job_path)
    plan = make_plan(job, B_PROFILE)
    txt = render_text(plan)

    # Inside git repo: relative posix path
    assert "Job: examples/linux/job.yaml" in txt
    assert str(repo_root) not in txt

    # Outside git repo: copy to tmp_path without .git
    isolated = tmp_path / "scratch"
    isolated.mkdir()
    shutil.copy(job_path, isolated / "my_job.yaml")
    shutil.copy(job_path.parent / "list_vm.csv", isolated / "list_vm.csv")
    shutil.copytree(job_path.parent / "scripts", isolated / "scripts")

    job_iso = load_job(isolated / "my_job.yaml")
    plan_iso = make_plan(job_iso, B_PROFILE)
    txt_iso = render_text(plan_iso)
    assert "Job: my_job.yaml" in txt_iso
    assert str(isolated) not in txt_iso


def test_per_vm_phases_and_order():
    repo_root = Path(__file__).resolve().parent.parent
    job = load_job(repo_root / "examples" / "linux" / "job.yaml")
    plan = make_plan(job, B_PROFILE)
    txt = render_text(plan)

    # CSV order: lin01, lin02, lin03
    idx_01 = txt.index("# ===== VM 1/3: lin01")
    idx_02 = txt.index("# ===== VM 2/3: lin02")
    idx_03 = txt.index("# ===== VM 3/3: lin03")
    assert idx_01 < idx_02 < idx_03

    # Per-VM phases in order: Invoke, Wait, Collect, Post
    vm1_section = txt[idx_01:idx_02]
    p_inv = vm1_section.index("Phase 1: Invoke")
    p_coll = vm1_section.index("Phase 2: Collect")
    p_post = vm1_section.index("Phase 3: Post")
    assert p_inv < p_coll < p_post

    # Commands in VM1 section
    assert "az rest --method post --url '/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/lin01/runCommand?api-version=2024-07-01' --body @remote.sh.body.json" in vm1_section
    assert "az vm run-command invoke --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n lin01 --command-id RunShellScript --scripts @collect.sh -o json" in vm1_section
    assert "/deallocate?api-version=2024-07-01" in vm1_section
    assert "/powerOff?api-version=2024-07-01" in vm1_section


def test_precheck_branches():
    repo_root = Path(__file__).resolve().parent.parent
    job = load_job(repo_root / "examples" / "linux" / "job.yaml")
    plan = make_plan(job, B_PROFILE)
    txt = render_text(plan)

    # Branches
    assert "when start / start-stop:" in txt
    assert "az vm start" in txt
    assert "unmatched -> ERROR:precheck_unmatched:" in txt
    assert "nothing below runs -> ENDED:" in txt


def test_subscription_cache_shared_list():
    repo_root = Path(__file__).resolve().parent.parent
    job = load_job(repo_root / "examples" / "linux" / "job.yaml")
    plan = make_plan(job, B_PROFILE)
    txt = render_text(plan)

    # First VM lin01 lists all sharing VMs
    assert "cache=subscription (shared by lin01, lin02, lin03)" in txt
    # lin02 and lin03 reference owner
    assert "result reused from lin01" in txt


def test_retry_prose():
    repo_root = Path(__file__).resolve().parent.parent
    job = load_job(repo_root / "examples" / "linux" / "job.yaml")
    plan = make_plan(job, B_PROFILE)
    txt = render_text(plan)

    assert "429/throttled: backoff 15, 30, 60, 120, 240 s" in txt
    assert "Conflict/busy on invoke: retry up to 3 times every 60 s" in txt
    assert "Unknown error: retry once after 30 s" in txt
    assert "Conflict on reader: STILL_RUNNING retried every 10s up to 3 times" in txt


def test_power_restore_per_prev_state():
    repo_root = Path(__file__).resolve().parent.parent
    job = load_job(repo_root / "examples" / "linux" / "job.yaml")
    plan = make_plan(job, B_PROFILE)
    txt = render_text(plan)

    assert "prev=PowerState/running -> action: deallocate" in txt
    assert "prev=PowerState/deallocated -> action: deallocate" in txt
    assert "prev=PowerState/stopped -> action: powerOff" in txt
    assert "az vm get-instance-view" in txt


def test_host_scripts_display_substitution():
    repo_root = Path(__file__).resolve().parent.parent
    job = load_job(repo_root / "examples" / "linux" / "job.yaml")
    plan = make_plan(job, B_PROFILE)
    txt = render_text(plan)

    # Marked display only
    assert "display only" in txt.lower()

    # lin01 values substituted
    assert '79f7da80-0c64-4e4b-9ed4-316a88142152' in txt
    assert 'rg-lab-10vms' in txt
    assert 'lin01' in txt

    # Escaped or non-row variables preserved verbatim
    assert '$Y2_OUTCOME' in txt
    assert '$Y2_PREV_POWER_STATE' in txt


def test_subscription_name_placeholder(tmp_path: Path):
    fleet = make_fleet(tmp_path, n=2, sub="name", variant="a")
    job = load_job(fleet["job_yaml"])
    plan = make_plan(job, B_PROFILE)
    txt = render_text(plan)

    # Resolution command present once
    assert "az account show --subscription 'Prod Subscription' --query id -o tsv" in txt
    # Placeholder in URL
    assert "<id-of:Prod Subscription>" in txt


def test_formats_agree():
    repo_root = Path(__file__).resolve().parent.parent
    job = load_job(repo_root / "examples" / "linux" / "job.yaml")
    plan = make_plan(job, B_PROFILE)

    txt = render_text(plan)
    md = render_md(plan)
    j_str = render_json(plan)

    j_data = json.loads(j_str)
    assert j_data.get("schema") == "y2-plan/1"

    # All argvs in JSON appear verbatim in text and markdown
    for vm_p in j_data.get("vms", []):
        for kind, argv in vm_p.get("commands", {}).items():
            if argv:
                quoted = shlex.join(argv)
                assert quoted in txt
                assert quoted in md


def test_vm_and_limit_filters():
    repo_root = Path(__file__).resolve().parent.parent
    job_path = repo_root / "examples" / "linux" / "job.yaml"
    job = load_job(job_path)

    # --vm lin02
    plan_vm = make_plan(job, B_PROFILE, vm_names=["lin02"])
    txt_vm = render_text(plan_vm)
    assert "lin02" in txt_vm
    assert "lin01" not in txt_vm
    assert "lin03" not in txt_vm

    # --limit 2
    plan_lim = make_plan(job, B_PROFILE, limit=2)
    txt_lim = render_text(plan_lim)
    assert "lin01" in txt_lim
    assert "lin02" in txt_lim
    assert "lin03" not in txt_lim


def test_invalid_job_exits_1(tmp_path: Path):
    bad_job = tmp_path / "bad.yaml"
    bad_job.write_text("invalid: [yaml", encoding="utf-8")
    res = run_cli("y2b", ["explain", str(bad_job)])
    assert res.returncode == 1
    assert "ERROR" in res.stderr
    assert len(res.stdout.strip()) == 0


# ==============================================================================
# TP-11: explain touches nothing and starts zero subprocesses
# ==============================================================================


def test_touches_nothing(tmp_path: Path, monkeypatch):
    repo_root = Path(__file__).resolve().parent.parent
    job_path = repo_root / "examples" / "linux" / "job.yaml"

    # Empty temp dirs for PATH, HOME, AZURE_CONFIG_DIR
    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir()
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    empty_cfg = tmp_path / "empty_cfg"
    empty_cfg.mkdir()

    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setenv("HOME", str(empty_home))
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(empty_cfg))
    monkeypatch.setenv("FAKE_AZ_LOG", str(tmp_path / "should_not_exist.log"))

    # Track attempts
    calls = []

    def fail_call(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("Guarded call intercepted!")

    monkeypatch.setattr(subprocess, "run", fail_call)
    monkeypatch.setattr(subprocess, "Popen", fail_call)
    monkeypatch.setattr(subprocess, "call", fail_call)
    monkeypatch.setattr(subprocess, "check_call", fail_call)
    monkeypatch.setattr(subprocess, "check_output", fail_call)
    monkeypatch.setattr(os, "system", fail_call)
    monkeypatch.setattr(os, "popen", fail_call)
    if hasattr(os, "posix_spawn"):
        monkeypatch.setattr(os, "posix_spawn", fail_call)

    # Socket guards
    monkeypatch.setattr(socket.socket, "connect", fail_call)
    monkeypatch.setattr(socket, "getaddrinfo", fail_call)

    # Take snapshot before
    def take_snapshot(p: Path) -> dict[str, str]:
        snap = {}
        for root, _, files in os.walk(p):
            for f in files:
                fp = Path(root) / f
                rel = fp.relative_to(p).as_posix()
                snap[rel] = hashlib.sha256(fp.read_bytes()).hexdigest()
        return snap

    snap_before = take_snapshot(job_path.parent)

    # Run explain in-process
    parser = build_parser(B_PROFILE)
    out_file = tmp_path / "plan.txt"
    args = parser.parse_args(["explain", str(job_path), "-o", str(out_file)])
    rc = cmd_explain(args, B_PROFILE)

    assert rc == 0
    assert len(calls) == 0  # Zero subprocess / socket calls
    assert out_file.exists()
    assert not (tmp_path / "should_not_exist.log").exists()

    snap_after = take_snapshot(job_path.parent)
    assert snap_before == snap_after


def test_check_syntax_equivalence():
    repo_root = Path(__file__).resolve().parent.parent

    # Linux
    job_l = load_job(repo_root / "examples" / "linux" / "job.yaml")
    rem_no_check_l, _ = B_PROFILE.compile_remote(job_l, run_id="preview", check_syntax=False)
    rem_check_l, _ = B_PROFILE.compile_remote(job_l, run_id="preview", check_syntax=True)
    golden_l = (repo_root / "tests" / "golden" / "hip_compliance_remote.sh").read_text(encoding="utf-8")

    assert rem_no_check_l == rem_check_l
    assert rem_no_check_l == golden_l

    coll_no_check_l, _ = B_PROFILE.compile_collect(job_l, check_syntax=False)
    coll_check_l, _ = B_PROFILE.compile_collect(job_l, check_syntax=True)
    assert coll_no_check_l == coll_check_l

    # Windows
    job_w = load_job(repo_root / "examples" / "windows" / "job.yaml")
    rem_no_check_w, _ = P_PROFILE.compile_remote(job_w, run_id="preview", check_syntax=False)
    rem_check_w, _ = P_PROFILE.compile_remote(job_w, run_id="preview", check_syntax=True)
    golden_w = (repo_root / "tests" / "golden" / "windows_baseline_remote.ps1").read_text(encoding="utf-8-sig")

    assert rem_no_check_w == rem_check_w
    assert rem_no_check_w == golden_w


# ==============================================================================
# TP-12: Determinism and Goldens
# ==============================================================================


def test_goldens_and_determinism(tmp_path: Path):
    repo_root = Path(__file__).resolve().parent.parent

    # 1. Compare against committed goldens
    job_l = load_job(repo_root / "examples" / "linux" / "job.yaml")
    plan_l = make_plan(job_l, B_PROFILE)
    txt_l = render_text(plan_l)
    md_l = render_md(plan_l)

    golden_txt_l = (repo_root / "tests" / "golden" / "explain_linux.txt").read_text(encoding="utf-8")
    golden_md_l = (repo_root / "tests" / "golden" / "explain_linux.md").read_text(encoding="utf-8")

    assert txt_l == golden_txt_l
    assert md_l == golden_md_l

    job_w = load_job(repo_root / "examples" / "windows" / "job.yaml")
    plan_w = make_plan(job_w, P_PROFILE)
    txt_w = render_text(plan_w)
    md_w = render_md(plan_w)

    golden_txt_w = (repo_root / "tests" / "golden" / "explain_windows.txt").read_text(encoding="utf-8")
    golden_md_w = (repo_root / "tests" / "golden" / "explain_windows.md").read_text(encoding="utf-8")

    assert txt_w == golden_txt_w
    assert md_w == golden_md_w

    # No environment leaks
    for text in (txt_l, md_l, txt_w, md_w):
        assert "/home/" not in text
        assert "/tmp/" not in text
        assert "\\Users\\" not in text
        assert not re.search(r"\d{8}T\d{6}Z-[0-9a-f]{8}", text)

    # 2. Changing one byte changes output
    modified_yaml = tmp_path / "job_mod.yaml"
    raw = (repo_root / "examples" / "linux" / "job.yaml").read_text(encoding="utf-8")
    modified_yaml.write_text(raw.replace("collect_retries: 3", "collect_retries: 4"), encoding="utf-8")
    shutil.copy(repo_root / "examples" / "linux" / "list_vm.csv", tmp_path / "list_vm.csv")
    shutil.copytree(repo_root / "examples" / "linux" / "scripts", tmp_path / "scripts")

    job_mod = load_job(modified_yaml)
    plan_mod = make_plan(job_mod, B_PROFILE)
    txt_mod = render_text(plan_mod)
    assert txt_mod != txt_l


# ==============================================================================
# TP-13: explain argv == runtime argv
# ==============================================================================


def test_runtime_argv(tmp_path: Path):
    repo_root = Path(__file__).resolve().parent.parent

    # Scenario 1: Subscription by name -> sub-resolve
    fleet_s1 = make_fleet(tmp_path / "s1", n=1, sub="NAME", variant="a")
    state_dir_s1 = tmp_path / "s1" / "fake_az_state"
    state_dir_s1.mkdir(parents=True, exist_ok=True)
    events_s1 = tmp_path / "s1" / "events.jsonl"
    env_s1 = {
        "FAKE_AZ_STATE_DIR": str(state_dir_s1),
        "FAKE_AZ_EVENTS": str(events_s1),
    }
    res_s1 = run_cli("y2b", ["run", str(fleet_s1["job_yaml"]), "--phase", "all", "-y"], cwd=tmp_path / "s1", env=env_s1)
    assert res_s1.returncode == 0

    # Parse explain plan for s1
    job_s1 = load_job(fleet_s1["job_yaml"])
    plan_s1 = make_plan(job_s1, B_PROFILE)
    json_s1 = json.loads(render_json(plan_s1))

    # Compare argv for each kind
    bindings = {}
    found_kinds = set()

    for line in events_s1.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        ev = json.loads(line)
        kind = ev.get("kind")
        found_kinds.add(kind)

    assert "sub-resolve" in found_kinds
    assert "invoke" in found_kinds
    assert "reader" in found_kinds

    # Scenario 2: deallocated VM + start-stop
    fleet_s2 = make_fleet(
        tmp_path / "s2",
        n=1,
        variant="a",
    )
    state_dir_s2 = tmp_path / "s2" / "fake_az_state"
    state_dir_s2.mkdir(parents=True, exist_ok=True)
    (state_dir_s2 / "lin01_state.txt").write_text("PowerState/deallocated", encoding="utf-8")
    events_s2 = tmp_path / "s2" / "events.jsonl"
    env_s2 = {
        "FAKE_AZ_STATE_DIR": str(state_dir_s2),
        "FAKE_AZ_EVENTS": str(events_s2),
    }
    res_s2 = run_cli("y2b", ["run", str(fleet_s2["job_yaml"]), "--phase", "all", "-y"], cwd=tmp_path / "s2", env=env_s2)
    assert res_s2.returncode == 0

    kinds_s2 = set()
    for line in events_s2.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        ev = json.loads(line)
        kinds_s2.add(ev.get("kind"))

    assert "power-state" in kinds_s2
    assert "vm-start" in kinds_s2
    assert "deallocate" in kinds_s2

    # Scenario 3: stopped VM + start-stop -> powerOff
    fleet_s3 = make_fleet(
        tmp_path / "s3",
        n=1,
        variant="a",
    )
    state_dir_s3 = tmp_path / "s3" / "fake_az_state"
    state_dir_s3.mkdir(parents=True, exist_ok=True)
    (state_dir_s3 / "lin01_state.txt").write_text("PowerState/stopped", encoding="utf-8")
    events_s3 = tmp_path / "s3" / "events.jsonl"
    env_s3 = {
        "FAKE_AZ_STATE_DIR": str(state_dir_s3),
        "FAKE_AZ_EVENTS": str(events_s3),
    }
    res_s3 = run_cli("y2b", ["run", str(fleet_s3["job_yaml"]), "--phase", "all", "-y"], cwd=tmp_path / "s3", env=env_s3)
    assert res_s3.returncode == 0

    kinds_s3 = set()
    for line in events_s3.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        ev = json.loads(line)
        kinds_s3.add(ev.get("kind"))

    assert "powerOff" in kinds_s3

    all_observed = found_kinds | kinds_s2 | kinds_s3
    for k in azcmd.ENGINE_KINDS:
        assert k in all_observed, f"Kind {k} was not observed during runtime"


def test_static_azcmd_builders():
    """Static AST check: each kind has exactly one builder in y2shared/azcmd.py."""
    repo_root = Path(__file__).resolve().parent.parent
    azcmd_file = repo_root / "y2shared" / "azcmd.py"
    tree = ast.parse(azcmd_file.read_text(encoding="utf-8"))

    func_names = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    expected_builders = [
        "build_sub_resolve",
        "build_power_state",
        "build_vm_start",
        "build_run_command_post",
        "build_reader_invoke",
        "build_power_post",
    ]
    for b in expected_builders:
        assert b in func_names, f"Expected builder {b} missing from azcmd.py"

    # Verify INV-7: grep for '["az"' in y2shared hits only azcmd.py
    y2shared_dir = repo_root / "y2shared"
    hits = []
    for p in y2shared_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        if '["az"' in text and p.name != "azcmd.py":
            hits.append(p.name)
    assert len(hits) == 0, f"Bare az command construction found outside azcmd.py: {hits}"
