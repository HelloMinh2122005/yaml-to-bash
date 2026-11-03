"""Tests for Tier 3 Azure lab evidence verification (TP-60, TP-61, TP-62, TP-63).

These tests run offline against captured evidence directories under
.handoffs/20261101-ado-export-explain/42-evidence/t3/.
They make ZERO Azure calls and require Y2_T3_EVIDENCE environment variable.
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import pytest

from tests.helpers_ado import parse_az_trace_lines, classify_argv, normalise_argv
from y2shared.azcmd import argv_matches


def get_evidence_dir() -> Path | None:
    env_val = os.environ.get("Y2_T3_EVIDENCE")
    if not env_val:
        return None
    p = Path(env_val).resolve()
    return p if p.is_dir() else None


@pytest.fixture
def evidence_dir() -> Path:
    ed = get_evidence_dir()
    if ed is None:
        pytest.skip("Y2_T3_EVIDENCE is not set or not a directory")
    return ed


def assert_all_vms_deallocated(state_text: str):
    """Assert all 5 lab VMs are in deallocated state."""
    lines = [line.strip() for line in state_text.splitlines() if line.strip()]
    vm_names = {"lin01", "lin02", "lin03", "win01", "win02"}
    found_vms = set()
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        vm = parts[0]
        if vm in vm_names:
            found_vms.add(vm)
            assert "deallocated" in line.lower(), f"VM {vm} not deallocated in: {line}"
    assert found_vms == vm_names, f"Missing VMs in state check: {vm_names - found_vms}"


def test_tp60_linux_evidence(evidence_dir: Path):
    """Verify TP-60 evidence: y2b split run on lin01..lin03."""
    tp_dir = evidence_dir if evidence_dir.name == "TP-60" else evidence_dir / "TP-60"
    if not tp_dir.is_dir():
        pytest.skip(f"TP-60 evidence dir not found at {tp_dir}")

    # Check files exist
    assert (tp_dir / "run.log").exists()
    assert (tp_dir / "pre-state.txt").exists()
    assert (tp_dir / "post-state.txt").exists()
    assert (tp_dir / "explain.txt").exists()
    assert (tp_dir / "explain.md").exists()
    assert (tp_dir / "explain.json").exists()
    assert (tp_dir / "invoke.stdout").exists()
    assert (tp_dir / "invoke.stderr").exists()
    assert (tp_dir / "invoke.rc").exists()
    assert (tp_dir / "parts.tar").exists()
    assert (tp_dir / "parts.tar.sha256").exists()
    assert (tp_dir / "result.csv").exists()
    assert (tp_dir / "result.log").exists()
    assert (tp_dir / "run.before.json").exists()
    assert (tp_dir / "run.after.json").exists()
    assert (tp_dir / "collect_list.jsonl").exists()

    # Pre/post states
    assert_all_vms_deallocated((tp_dir / "pre-state.txt").read_text(encoding="utf-8"))
    assert_all_vms_deallocated((tp_dir / "post-state.txt").read_text(encoding="utf-8"))

    # Invoke phase checks
    invoke_rc = int((tp_dir / "invoke.rc").read_text().strip())
    assert invoke_rc == 0
    invoke_stdout = (tp_dir / "invoke.stdout").read_text(encoding="utf-8")
    assert "conformance: 3/3 VMs matched plan" in invoke_stdout
    invoke_lines = [line.strip() for line in invoke_stdout.splitlines() if line.strip()]
    assert invoke_lines[-1] == "fired=3"

    # run.before.json
    run_before = json.loads((tp_dir / "run.before.json").read_text(encoding="utf-8"))
    assert run_before.get("phase") == "collect"

    # collect_list.jsonl has 3 confirmed records
    cl_lines = [json.loads(line) for line in (tp_dir / "collect_list.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    confirmed_records = [r for r in cl_lines if r.get("confirmed")]
    assert len(confirmed_records) == 3
    assert {r["vm_name"] for r in confirmed_records} == {"lin01", "lin02", "lin03"}

    # Collect phase: result.csv
    csv_rows = list(csv.DictReader((tp_dir / "result.csv").read_text(encoding="utf-8").splitlines()))
    assert len(csv_rows) == 3
    expected_vms = ["lin01", "lin02", "lin03"]
    actual_vms = [r["vm_name"] for r in csv_rows]
    assert actual_vms == expected_vms

    for r in csv_rows:
        assert r["os_name"]
        assert r["kernel_version"]
        assert r["uptime_secs"]
        assert r["verdict"] in ("HEALTHY", "ATTENTION")
        for val in r.values():
            assert not str(val).startswith("ERROR:")
            assert not str(val).startswith("SKIPPED:")

    # result.log analysis and trace-summary
    log_text = (tp_dir / "result.log").read_text(encoding="utf-8")
    parsed_traces = parse_az_trace_lines(log_text)

    summary_lines = ["TP-60 Trace Summary:"]
    vm_traces: dict[str, list] = {vm: [] for vm in expected_vms}
    for tr in parsed_traces:
        vm = tr.get("vm")
        if vm in vm_traces:
            vm_traces[vm].append(tr)

    for vm in expected_vms:
        trs = vm_traces[vm]
        kinds = [classify_argv(t["argv"]) for t in trs if t["kind"] == "engine"]
        summary_lines.append(f"  {vm}: {len(trs)} total traces, engine kinds: {sorted(set(kinds))}")

        # Assert engine kinds
        assert kinds.count("power-state") >= 1
        assert kinds.count("vm-start") == 1
        assert kinds.count("invoke") == 1
        reader_success = [t for t in trs if classify_argv(t["argv"]) == "reader" and t.get("rc") == 0]
        assert len(reader_success) == 1
        assert kinds.count("deallocate") == 1

    (tp_dir / "trace-summary.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")


def test_tp61_windows_evidence(evidence_dir: Path):
    """Verify TP-61 evidence: y2p split run on win01, win02."""
    tp_dir = evidence_dir if evidence_dir.name == "TP-61" else evidence_dir / "TP-61"
    if not tp_dir.is_dir():
        pytest.skip(f"TP-61 evidence dir not found at {tp_dir}")

    # Check files exist
    assert (tp_dir / "run.log").exists()
    assert (tp_dir / "pre-state.txt").exists()
    assert (tp_dir / "post-state.txt").exists()
    assert (tp_dir / "explain.txt").exists()
    assert (tp_dir / "explain.md").exists()
    assert (tp_dir / "explain.json").exists()
    assert (tp_dir / "invoke.stdout").exists()
    assert (tp_dir / "invoke.stderr").exists()
    assert (tp_dir / "invoke.rc").exists()
    assert (tp_dir / "parts.tar").exists()
    assert (tp_dir / "parts.tar.sha256").exists()
    assert (tp_dir / "result.csv").exists()
    assert (tp_dir / "result.log").exists()
    assert (tp_dir / "run.before.json").exists()
    assert (tp_dir / "run.after.json").exists()
    assert (tp_dir / "collect_list.jsonl").exists()

    cl_lines = [json.loads(line) for line in (tp_dir / "collect_list.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    confirmed_records = [r for r in cl_lines if r.get("confirmed")]
    assert len(confirmed_records) == 2
    assert {r["vm_name"] for r in confirmed_records} == {"win01", "win02"}

    assert_all_vms_deallocated((tp_dir / "pre-state.txt").read_text(encoding="utf-8"))
    assert_all_vms_deallocated((tp_dir / "post-state.txt").read_text(encoding="utf-8"))

    invoke_rc = int((tp_dir / "invoke.rc").read_text().strip())
    assert invoke_rc == 0
    invoke_stdout = (tp_dir / "invoke.stdout").read_text(encoding="utf-8")
    assert "conformance: 2/2 VMs matched plan" in invoke_stdout
    invoke_lines = [line.strip() for line in invoke_stdout.splitlines() if line.strip()]
    assert invoke_lines[-1] == "fired=2"

    csv_rows = list(csv.DictReader((tp_dir / "result.csv").read_text(encoding="utf-8").splitlines()))
    assert len(csv_rows) == 2
    expected_vms = ["win01", "win02"]
    actual_vms = [r["vm_name"] for r in csv_rows]
    assert actual_vms == expected_vms

    for r in csv_rows:
        assert r["os_name"]
        assert r["kernel_version"]
        assert r["verdict"] in ("COMPLIANT", "NON_COMPLIANT")
        for k, val in r.items():
            assert not str(val).startswith("ERROR:not_started")
            assert not str(val).startswith("remote_incomplete")
            assert not str(val).startswith("no_result")
            assert not str(val).startswith("stale_result")

    # result.log analysis
    log_text = (tp_dir / "result.log").read_text(encoding="utf-8")
    parsed_traces = parse_az_trace_lines(log_text)

    summary_lines = ["TP-61 Trace Summary:"]
    for vm in expected_vms:
        trs = [t for t in parsed_traces if t.get("vm") == vm]
        kinds = [classify_argv(t["argv"]) for t in trs if t["kind"] == "engine"]
        summary_lines.append(f"  {vm}: {len(trs)} total traces, engine kinds: {sorted(set(kinds))}")

        assert kinds.count("power-state") >= 1
        assert kinds.count("vm-start") == 1
        assert kinds.count("invoke") == 1
        reader_success = [t for t in trs if classify_argv(t["argv"]) == "reader" and t.get("rc") == 0]
        assert len(reader_success) == 1
        assert kinds.count("deallocate") == 1

        # Check RunPowerShellScript
        for t in trs:
            if classify_argv(t["argv"]) == "reader":
                assert "--command-id" in t["argv"]
                assert "RunPowerShellScript" in t["argv"]

    (tp_dir / "trace-summary.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")


def test_tp62_abort_restore_evidence(evidence_dir: Path):
    """Verify TP-62 evidence: abort after first started VM, collect restores power."""
    tp_dir = evidence_dir if evidence_dir.name == "TP-62" else evidence_dir / "TP-62"
    if not tp_dir.is_dir():
        pytest.skip(f"TP-62 evidence dir not found at {tp_dir}")

    assert (tp_dir / "run.log").exists()
    assert (tp_dir / "pre-state.txt").exists()
    assert (tp_dir / "post-state.txt").exists()
    assert (tp_dir / "invoke.stdout").exists()
    assert (tp_dir / "invoke.stderr").exists()
    assert (tp_dir / "invoke.rc").exists()
    assert (tp_dir / "collect.stdout").exists()
    assert (tp_dir / "collect.stderr").exists()
    assert (tp_dir / "collect.rc").exists()
    assert (tp_dir / "run.after-invoke.json").exists()
    assert (tp_dir / "collect_list.after-invoke.jsonl").exists()
    assert (tp_dir / "power.lin01.after-invoke.json").exists()
    assert (tp_dir / "result.csv").exists()
    assert (tp_dir / "result.log").exists()

    assert_all_vms_deallocated((tp_dir / "pre-state.txt").read_text(encoding="utf-8"))
    assert_all_vms_deallocated((tp_dir / "post-state.txt").read_text(encoding="utf-8"))

    invoke_rc = int((tp_dir / "invoke.rc").read_text().strip())
    assert invoke_rc == 3
    invoke_stdout = (tp_dir / "invoke.stdout").read_text(encoding="utf-8")
    invoke_lines = [l.strip() for l in invoke_stdout.splitlines() if l.strip()]
    assert invoke_lines[-1] == "fired=1"

    run_after_invoke = json.loads((tp_dir / "run.after-invoke.json").read_text(encoding="utf-8"))
    assert run_after_invoke.get("phase") == "invoke"

    cl_lines = [json.loads(l) for l in (tp_dir / "collect_list.after-invoke.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    confirmed_records = [r for r in cl_lines if r.get("confirmed")]
    assert len(confirmed_records) == 1
    assert confirmed_records[0].get("vm_name") == "lin01"

    power_lin01 = json.loads((tp_dir / "power.lin01.after-invoke.json").read_text(encoding="utf-8"))
    assert power_lin01.get("prev_state") == "PowerState/deallocated"
    assert power_lin01.get("started_by_run") is True
    assert power_lin01.get("post") == "pending"

    collect_rc = int((tp_dir / "collect.rc").read_text().strip())
    assert collect_rc == 2

    csv_rows = list(csv.DictReader((tp_dir / "result.csv").read_text(encoding="utf-8").splitlines()))
    assert len(csv_rows) == 3
    assert csv_rows[0]["vm_name"] == "lin01"
    assert not csv_rows[0]["os_name"].startswith("ERROR:")
    assert csv_rows[1]["vm_name"] == "lin02"
    assert csv_rows[1]["os_name"] == "ERROR:no_result"
    assert csv_rows[2]["vm_name"] == "lin03"
    assert csv_rows[2]["os_name"] == "ERROR:no_result"

    log_text = (tp_dir / "result.log").read_text(encoding="utf-8")
    parsed_traces = parse_az_trace_lines(log_text)

    # lin02 and lin03 must have no start/invoke/deallocate
    for tr in parsed_traces:
        vm = tr.get("vm")
        if vm in ("lin02", "lin03"):
            kind = classify_argv(tr["argv"])
            assert kind not in ("vm-start", "invoke", "deallocate")

    # lin01 has invoke, reader SUCCESS, deallocate
    lin01_trs = [t for t in parsed_traces if t.get("vm") == "lin01"]
    lin01_kinds = [classify_argv(t["argv"]) for t in lin01_trs if t["kind"] == "engine"]
    assert "invoke" in lin01_kinds
    assert any(classify_argv(t["argv"]) == "reader" and t.get("rc") == 0 for t in lin01_trs)
    assert "deallocate" in lin01_kinds

    (tp_dir / "trace-summary.txt").write_text(f"TP-62 Trace Summary: lin01 traces={len(lin01_trs)}, lin02/03 untouched\n", encoding="utf-8")


def test_tp63_explain_vs_trace(evidence_dir: Path):
    """Verify TP-63: compare explain listing vs real engine-issued commands."""
    base_t3 = evidence_dir if (evidence_dir / "TP-60").exists() else evidence_dir.parent
    tp60_dir = base_t3 / "TP-60"
    tp61_dir = base_t3 / "TP-61"

    if not tp60_dir.is_dir() or not tp61_dir.is_dir():
        pytest.skip("TP-60 and TP-61 evidence dirs required for TP-63")

    report_lines = ["TP-63: Explain vs Real Engine-Issued Trace Report", "=" * 60]

    # Check Linux VMs (lin01..lin03)
    explain_json_lin = json.loads((tp60_dir / "explain.json").read_text(encoding="utf-8"))
    log_text_lin = (tp60_dir / "result.log").read_text(encoding="utf-8")
    traces_lin = parse_az_trace_lines(log_text_lin)

    plan_by_vm_lin = {vm["vm_name"]: vm for vm in explain_json_lin.get("vms", [])}

    for vm in ["lin01", "lin02", "lin03"]:
        assert vm in plan_by_vm_lin, f"VM {vm} missing from explain.json"
        vm_plan = plan_by_vm_lin[vm]
        commands_dict = vm_plan.get("commands", {})
        plan_commands = [{"kind": k, "argv": v} for k, v in commands_dict.items()]

        # Get real engine traces for this VM
        real_engine_traces = [t for t in traces_lin if t.get("vm") == vm and t.get("kind") == "engine"]
        report_lines.append(f"\nVM: {vm} (Linux)")
        report_lines.append(f"  Plan has {len(plan_commands)} commands, Real trace has {len(real_engine_traces)} engine commands")

        for r_tr in real_engine_traces:
            r_argv = r_tr["argv"]
            matched = False
            for p_cmd in plan_commands:
                p_argv = p_cmd.get("argv", [])
                bindings: dict[str, str] = {}
                if argv_matches(p_argv, r_argv, bindings):
                    matched = True
                    report_lines.append(f"  [MATCH] real '{' '.join(r_argv[:4])}...' matches plan kind={p_cmd.get('kind')}")
                    break
            assert matched, f"Real argv for {vm} not matched in plan: {r_argv}"

    # Check Windows VMs (win01..win02)
    explain_json_win = json.loads((tp61_dir / "explain.json").read_text(encoding="utf-8")) if (tp61_dir / "explain.json").exists() else None
    log_text_win = (tp61_dir / "result.log").read_text(encoding="utf-8")
    traces_win = parse_az_trace_lines(log_text_win)

    if explain_json_win:
        plan_by_vm_win = {vm["vm_name"]: vm for vm in explain_json_win.get("vms", [])}
        for vm in ["win01", "win02"]:
            assert vm in plan_by_vm_win, f"VM {vm} missing from explain.json"
            vm_plan = plan_by_vm_win[vm]
            commands_dict = vm_plan.get("commands", {})
            plan_commands = [{"kind": k, "argv": v} for k, v in commands_dict.items()]

            real_engine_traces = [t for t in traces_win if t.get("vm") == vm and t.get("kind") == "engine"]
            report_lines.append(f"\nVM: {vm} (Windows)")
            report_lines.append(f"  Plan has {len(plan_commands)} commands, Real trace has {len(real_engine_traces)} engine commands")

            for r_tr in real_engine_traces:
                r_argv = r_tr["argv"]
                matched = False
                for p_cmd in plan_commands:
                    p_argv = p_cmd.get("argv", [])
                    bindings: dict[str, str] = {}
                    if argv_matches(p_argv, r_argv, bindings):
                        matched = True
                        report_lines.append(f"  [MATCH] real '{' '.join(r_argv[:4])}...' matches plan kind={p_cmd.get('kind')}")
                        break
                assert matched, f"Real argv for {vm} not matched in plan: {r_argv}"

    report_lines.append("\nTP-63 Verdict: ALL REAL AZURE TRACES MATCHED EXPLAIN PLAN.")
    out_dir = base_t3 / "TP-63"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_file = out_dir / "explain-vs-trace.txt"
    report_file.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    assert report_file.exists()

