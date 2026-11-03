"""TP-73: Scale guard tests for 500+ VMs and az call-count invariant.

Verifies:
1. explain scales linearly on 50, 250, and 500 VMs with single shared-cache listing.
2. Conformance evaluation over 250 and 500 VM traces is linear and executes in < 5s.
3. 20-VM --phase all run against fake_az preserves exact az call counts versus baseline.
"""

from __future__ import annotations

import json
import os
import resource
import time
from collections import Counter
from pathlib import Path
import pytest

from tests.helpers_ado import classify_argv, make_fleet, run_cli
from y2b.profile import PROFILE as B_PROFILE
from y2shared.azcmd import KIND_INVOKE, KIND_READER
from y2shared.aztrace import TraceRecorder
from y2shared.config import load_job
from y2shared.conformance import evaluate
from y2shared.explain import render_json, render_text
from y2shared.inputcsv import VMRow, read_input_csv, select_target_rows
from y2shared.plan import PlanContext, build_plan, build_vm_plan

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_explain_scale_linear_growth(tmp_path: Path):
    """Verify explain output growth is linear, finishes in <20s, and uses <300MB memory."""
    sizes_text: dict[int, int] = {}
    sizes_json: dict[int, int] = {}

    prechecks = [
        {
            "name": "check-sub",
            "cache": "subscription",
            "phase": ["invoke"],
            "script": "echo 0",
            "result": {
                "0": {"action": "continue", "log": "ok"},
                "default": {"action": "continue", "log": "default"},
            },
        }
    ]

    for n in [50, 250, 500]:
        fleet_dir = tmp_path / f"fleet_{n}"
        fleet = make_fleet(fleet_dir, n=n, os_type="linux", sub="name", prechecks=prechecks)

        job = load_job(fleet["job_yaml"])
        rows, _, _ = read_input_csv(job.input, job=job, os_family=B_PROFILE.os_family)
        target_rows = select_target_rows(rows)

        rusage_before = resource.getrusage(resource.RUSAGE_SELF)
        t0 = time.perf_counter()

        plan = build_plan(job, target_rows, B_PROFILE)
        out_text = render_text(plan)
        out_json = render_json(plan)

        dur = time.perf_counter() - t0
        rusage_after = resource.getrusage(resource.RUSAGE_SELF)

        # Linux ru_maxrss is in kilobytes; macOS is in bytes
        scale = 1024 if os.uname().sysname == "Linux" else (1024 * 1024)
        mem_mb = (rusage_after.ru_maxrss - rusage_before.ru_maxrss) / scale

        sizes_text[n] = len(out_text)
        sizes_json[n] = len(out_json)

        if n == 500:
            assert dur < 20.0, f"explain for 500 VMs took {dur:.2f}s, expected < 20s"
            assert mem_mb < 300.0, f"explain for 500 VMs consumed {mem_mb:.1f} MB RSS, expected < 300 MB"

        # Check that probe VM appears independent of fleet size (in cache share list once, and in own section)
        if n in (250, 500):
            probe_vm = "vm-0100"
            sizes_text[f"{n}_probe"] = out_text.count(probe_vm)

    assert sizes_text["250_probe"] == sizes_text["500_probe"], (
        f"Probe VM count changed with N: 250 had {sizes_text['250_probe']}, 500 had {sizes_text['500_probe']}"
    )

    # Growth ratio assertions: size(500)/size(250) <= 2.2, size(250)/size(50) <= 5.5
    ratio_500_250 = sizes_text[500] / sizes_text[250]
    ratio_250_50 = sizes_text[250] / sizes_text[50]
    assert ratio_500_250 <= 2.2, f"Text size ratio 500/250 was {ratio_500_250:.2f}, expected <= 2.2"
    assert ratio_250_50 <= 5.5, f"Text size ratio 250/50 was {ratio_250_50:.2f}, expected <= 5.5"

    ratio_json_500_250 = sizes_json[500] / sizes_json[250]
    ratio_json_250_50 = sizes_json[250] / sizes_json[50]
    assert ratio_json_500_250 <= 2.2, f"JSON size ratio 500/250 was {ratio_json_500_250:.2f}, expected <= 2.2"
    assert ratio_json_250_50 <= 5.5, f"JSON size ratio 250/50 was {ratio_json_250_50:.2f}, expected <= 5.5"


def test_conformance_scale_performance(tmp_path: Path):
    """Verify conformance evaluation over 250 and 500 synthetic VM traces is linear and <5s."""
    timings: dict[int, float] = {}

    for n in [250, 500]:
        fleet_dir = tmp_path / f"conf_fleet_{n}"
        fleet = make_fleet(fleet_dir, n=n, os_type="linux", sub="GUID", variant="a")
        job = load_job(fleet["job_yaml"])

        rows, _, _ = read_input_csv(job.input, job=job, os_family=B_PROFILE.os_family)
        work_rows = select_target_rows(rows)

        ctx = PlanContext.from_rows(job, work_rows, B_PROFILE)

        recorder = TraceRecorder()
        for row in work_rows:
            vm_plan = build_vm_plan(ctx, row)
            recorder.record_engine(
                row.index,
                KIND_INVOKE,
                1,
                0,
                "SUCCESS",
                0.1,
                vm_plan["commands"][KIND_INVOKE],
            )
            recorder.record_engine(
                row.index,
                KIND_READER,
                1,
                0,
                "SUCCESS",
                0.1,
                vm_plan["commands"][KIND_READER],
            )

        t0 = time.perf_counter()
        report = evaluate(ctx, work_rows, recorder, phases_run=["all"])
        dur = time.perf_counter() - t0

        timings[n] = dur
        assert report.matched == n, f"Expected {n} matches, got {report.matched} (mismatches: {report.mismatches})"
        assert len(report.mismatches) == 0

    assert timings[500] < 5.0, f"Conformance evaluation for 500 VMs took {timings[500]:.3f}s, expected < 5s"
    ratio = timings[500] / max(timings[250], 0.0001)
    assert ratio <= 3.0, f"Conformance time ratio 500/250 was {ratio:.2f}, expected <= 3.0"


def test_az_call_counts_match_baseline(tmp_path: Path):
    """Verify 20-VM fleet Azure CLI call counts match committed baseline exactly."""
    baseline_path = REPO_ROOT / "tests" / "golden" / "az_call_counts_baseline.json"
    assert baseline_path.exists(), "Baseline az_call_counts_baseline.json not found"

    with open(baseline_path, "r", encoding="utf-8") as f:
        baseline_data = json.load(f)
    expected_counts = baseline_data["fleet_20vm"]

    fleet_dir = tmp_path / "fleet_20"
    states_20 = {
        f"lin{i:02d}": ("PowerState/deallocated" if i % 2 == 0 else "PowerState/running")
        for i in range(1, 21)
    }

    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    for vm, st in states_20.items():
        (state_dir / f"{vm}_state.txt").write_text(st, encoding="utf-8")

    events_file = tmp_path / "events.jsonl"
    log_file = tmp_path / "az_calls.log"

    env = {
        "FAKE_AZ_STATE_DIR": str(state_dir),
        "FAKE_AZ_EVENTS": str(events_file),
        "FAKE_AZ_LOG": str(log_file),
        "FAKE_AZ_RC_DEFAULT": "success",
    }

    make_fleet(
        fleet_dir,
        n=20,
        os_type="linux",
        sub="GUID",
        states=states_20,
        sleep_before_collect=0.1,
        collect_retry_interval=0.1,
    )

    res = run_cli(
        "y2b",
        ["run", str(fleet_dir / "job.yaml"), "--phase", "all", "--parallel", "10", "-y"],
        env=env,
        cwd=fleet_dir,
    )
    assert res.returncode == 0, f"20-VM run failed:\nstdout={res.stdout}\nstderr={res.stderr}"

    calls = [
        line.strip().split()
        for line in log_file.read_text(encoding="utf-8").strip().splitlines()
        if line.strip()
    ]
    actual_counts = dict(Counter(classify_argv(c) for c in calls))

    assert actual_counts == expected_counts, (
        f"Azure call counts deviated from baseline!\nActual:   {actual_counts}\nExpected: {expected_counts}"
    )
