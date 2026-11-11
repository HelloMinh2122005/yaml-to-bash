# tests/test_showcase_jobs.py - Test suite for showcase Linux and Windows demo jobs
# Pure ASCII, LF line endings, BOM-less UTF-8, Python 3.9 compatible (stdlib + PyYAML + pytest).
# Covers: T1-1 through T1-8 from .handoffs/20261111-showcase-jobs/20-lite-plan.md.

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
from pathlib import Path
from typing import List
import urllib.error
import urllib.request

import pytest

from tests.test_ado_ps51_compat import find_ps7_syntax_violations
from y2b.compiler import compile_job as compile_job_linux
from y2b.profile import PROFILE as Y2B_PROFILE
from y2b.validate import validate_job_file as validate_job_file_linux
from y2p.compiler import compile_job as compile_job_windows
from y2p.profile import PROFILE as Y2P_PROFILE
from y2p.validate import validate_job_file as validate_job_file_windows
from y2shared.config import load_job
from y2shared.export_ado import generate_pipeline_files
from y2shared.runner import run_job

REPO_ROOT = Path(__file__).resolve().parent.parent
LINUX_SHOWCASE_DIR = REPO_ROOT / "examples" / "showcase" / "linux"
WINDOWS_SHOWCASE_DIR = REPO_ROOT / "examples" / "showcase" / "windows"
LINUX_JOB_PATH = LINUX_SHOWCASE_DIR / "job.yaml"
WINDOWS_JOB_PATH = WINDOWS_SHOWCASE_DIR / "job.yaml"
FAKE_AZ_DIR = REPO_ROOT / "tests" / "fake_az"

PINNED_URL = (
    "https://files.pythonhosted.org/packages/d9/5a/e7c31adbe875f2abbb91bd84cf2dc52d792b5a01506781dbcf25c91daf11/"
    "six-1.16.0-py2.py3-none-any.whl"
)
PINNED_SHA256 = "8abb2f1d86890a2dfb989f9a77cfcfd3e47c2a354b01111771326f8aa26e0254"
PINNED_SHA256_12 = "8abb2f1d8689"

EXPECTED_STEPS = [
    "say-hello",
    "do-math",
    "get-system-info",
    "get-updates",
    "download-package",
    "dedupe-packages",
    "cleanup",
]

EXPECTED_COLUMNS = [
    "vm_name",
    "os_target",
    "hello",
    "shell_version",
    "math_sum",
    "math_fact10",
    "math_cpu_x1024",
    "os_name",
    "os_version",
    "kernel",
    "cpu_count",
    "mem_mb",
    "uptime_secs",
    "vm_size",
    "location",
    "upd_count",
    "upd_latest",
    "upd_latest_date",
    "kernel_count",
    "dl_status",
    "dl_bytes",
    "dl_sha256_12",
    "dup_found",
    "dup_deleted",
    "files_left",
    "dedup_source",
    "cleanup",
    "math_check",
    "verdict",
]


# ==============================================================================
# T1-1: Job Validation (0 errors, 0 warnings, exactly 7 steps)
# ==============================================================================

def test_t1_1_linux_job_validation():
    """T1-1: y2b validate on Linux showcase job produces 0 errors and 0 warnings."""
    assert LINUX_JOB_PATH.is_file(), f"Missing Linux job file: {LINUX_JOB_PATH}"
    job, res = validate_job_file_linux(LINUX_JOB_PATH, profile=Y2B_PROFILE)

    error_msgs = [f"{e.path}: {e.message}" for e in res.errors]
    warning_msgs = [f"{w.path}: {w.message}" for w in res.warnings]

    assert res.is_valid, f"Linux job validation errors: {error_msgs}"
    assert len(warning_msgs) == 0, f"Linux job validation warnings: {warning_msgs}"
    assert job is not None

    steps = [s.name for s in job.invoke.runcommand.steps]
    assert steps == EXPECTED_STEPS, f"Expected 7 steps {EXPECTED_STEPS}, got {steps}"


def test_t1_1_windows_job_validation():
    """T1-1: y2p validate on Windows showcase job produces 0 errors and 0 warnings."""
    assert WINDOWS_JOB_PATH.is_file(), f"Missing Windows job file: {WINDOWS_JOB_PATH}"
    job, res = validate_job_file_windows(WINDOWS_JOB_PATH, profile=Y2P_PROFILE)

    error_msgs = [f"{e.path}: {e.message}" for e in res.errors]
    warning_msgs = [f"{w.path}: {w.message}" for w in res.warnings]

    assert res.is_valid, f"Windows job validation errors: {error_msgs}"
    assert len(warning_msgs) == 0, f"Windows job validation warnings: {warning_msgs}"
    assert job is not None

    steps = [s.name for s in job.invoke.runcommand.steps]
    assert steps == EXPECTED_STEPS, f"Expected 7 steps {EXPECTED_STEPS}, got {steps}"


# ==============================================================================
# T1-2: Compile Reproducibility (byte-identical outputs)
# ==============================================================================

def test_t1_2_linux_compile_reproducibility():
    """T1-2: y2b compile twice produces byte-identical outputs."""
    job = load_job(LINUX_JOB_PATH)
    payload1, errs1 = compile_job_linux(job, run_id="showcase-preview-lin")
    payload2, errs2 = compile_job_linux(job, run_id="showcase-preview-lin")

    assert not errs1, f"Compile errors in run 1: {errs1}"
    assert not errs2, f"Compile errors in run 2: {errs2}"
    assert payload1 == payload2, "Linux compiled payloads are not byte-identical"


def test_t1_2_windows_compile_reproducibility():
    """T1-2: y2p compile twice produces byte-identical outputs."""
    job = load_job(WINDOWS_JOB_PATH)
    payload1, errs1 = compile_job_windows(job, run_id="showcase-preview-win")
    payload2, errs2 = compile_job_windows(job, run_id="showcase-preview-win")

    assert not errs1, f"Compile errors in run 1: {errs1}"
    assert not errs2, f"Compile errors in run 2: {errs2}"
    assert payload1 == payload2, "Windows compiled payloads are not byte-identical"


# ==============================================================================
# T1-3: Static Hygiene of All Showcase Files (Pure ASCII, LF only, chmod +x)
# ==============================================================================

def test_t1_3_new_files_hygiene():
    """T1-3: All showcase example files and test files are pure ASCII, LF-only, scripts executable."""
    target_files: List[Path] = [
        LINUX_JOB_PATH,
        LINUX_SHOWCASE_DIR / "list_vm.csv",
        WINDOWS_JOB_PATH,
        WINDOWS_SHOWCASE_DIR / "list_vm.csv",
        Path(__file__).resolve(),
    ]

    # Collect any non-inlined script files
    for base in (LINUX_SHOWCASE_DIR, WINDOWS_SHOWCASE_DIR):
        scripts_dir = base / "scripts"
        if scripts_dir.is_dir():
            target_files.extend([p for p in scripts_dir.rglob("*") if p.is_file()])

    assert target_files, "Target file list must not be empty"

    non_ascii_files: List[str] = []
    crlf_files: List[str] = []
    non_executable_scripts: List[str] = []

    for fpath in target_files:
        assert fpath.is_file(), f"Expected file does not exist: {fpath}"
        raw = fpath.read_bytes()

        # Check BOM
        assert not raw.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")), f"BOM detected: {fpath}"

        # Check CRLF
        if b"\r" in raw:
            crlf_files.append(str(fpath))

        # Check ASCII
        if not raw.isascii():
            non_ascii_files.append(str(fpath))

        # If file is under scripts/, verify executable permission
        if "scripts" in fpath.parts and not os.access(fpath, os.X_OK):
            non_executable_scripts.append(str(fpath))

    assert not non_ascii_files, f"Non-ASCII characters detected in: {non_ascii_files}"
    assert not crlf_files, f"Non-LF line endings detected in: {crlf_files}"
    assert not non_executable_scripts, f"Scripts missing execute permissions: {non_executable_scripts}"


# ==============================================================================
# T1-4: Windows Payload Static PS 5.1 Compatibility
# ==============================================================================

def test_t1_4_windows_payload_ps51_compatibility():
    """T1-4: Windows compiled payload and scripts contain no PS 7+ syntax and enforce PS 5.1 rules."""
    job = load_job(WINDOWS_JOB_PATH)
    payload, errs = compile_job_windows(job, run_id="ps51-compat-test")
    assert not errs, f"Compilation errors: {errs}"

    violations = find_ps7_syntax_violations(payload, "showcase_windows_remote.ps1")
    assert not violations, f"PS 7+ syntax violations in compiled payload: {violations}"

    # Also check any individual script files if present
    scripts_dir = WINDOWS_SHOWCASE_DIR / "scripts"
    if scripts_dir.is_dir():
        for sp in scripts_dir.rglob("*.ps1"):
            script_violations = find_ps7_syntax_violations(sp.read_text(encoding="ascii"), sp.name)
            assert not script_violations, f"PS 7+ syntax violations in {sp}: {script_violations}"

    # Verify TLS 1.2 and $ProgressPreference settings are present in payload
    assert "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12" in payload, (
        "Windows payload must explicitly configure TLS 1.2 for download compatibility"
    )
    assert "$ProgressPreference = 'SilentlyContinue'" in payload or '$ProgressPreference = "SilentlyContinue"' in payload, (
        "Windows payload must set $ProgressPreference='SilentlyContinue'"
    )
    assert "-UseBasicParsing" in payload, (
        "Invoke-WebRequest must use -UseBasicParsing for PowerShell 5.1 compatibility on Server 2016"
    )


# ==============================================================================
# T1-5: Export ADO Pipelines Verification (--check exits 0)
# ==============================================================================

def test_t1_5_export_ado_linux_check():
    """T1-5: y2b export-ado pipelines/generated/showcase-linux --check exits 0."""
    out_dir = REPO_ROOT / "pipelines" / "generated" / "showcase-linux"
    assert out_dir.is_dir(), f"Expected directory does not exist: {out_dir}"

    job = load_job(LINUX_JOB_PATH)
    files = generate_pipeline_files(
        job=job,
        profile=Y2B_PROFILE,
        out_dir=out_dir,
        service_connection="sc-azure-lab",
        environment="y2-lab",
    )

    missing = []
    differs = []
    for relpath, expected_content in files.items():
        target_path = out_dir / relpath
        if not target_path.is_file():
            missing.append(relpath)
        else:
            existing_bytes = target_path.read_bytes()
            expected_bytes = expected_content.encode("utf-8")
            if existing_bytes != expected_bytes:
                differs.append(relpath)

    assert not missing, f"Missing files in generated showcase-linux pipeline: {missing}"
    assert not differs, f"Differing files in generated showcase-linux pipeline: {differs}"


def test_t1_5_export_ado_windows_check():
    """T1-5: y2p export-ado pipelines/generated/showcase-windows --check exits 0."""
    out_dir = REPO_ROOT / "pipelines" / "generated" / "showcase-windows"
    assert out_dir.is_dir(), f"Expected directory does not exist: {out_dir}"

    job = load_job(WINDOWS_JOB_PATH)
    files = generate_pipeline_files(
        job=job,
        profile=Y2P_PROFILE,
        out_dir=out_dir,
        service_connection="sc-azure-lab",
        environment="y2-lab",
    )

    missing = []
    differs = []
    for relpath, expected_content in files.items():
        target_path = out_dir / relpath
        if not target_path.is_file():
            missing.append(relpath)
        else:
            existing_bytes = target_path.read_bytes()
            expected_bytes = expected_content.encode("utf-8")
            if existing_bytes != expected_bytes:
                differs.append(relpath)

    assert not missing, f"Missing files in generated showcase-windows pipeline: {missing}"
    assert not differs, f"Differing files in generated showcase-windows pipeline: {differs}"


# ==============================================================================
# T1-6: Run Against fake_az (exit 0, merge order = CSV order)
# ==============================================================================

def test_t1_6_fake_az_linux_run(tmp_path, monkeypatch):
    """T1-6: y2b run against fake_az succeeds with exit 0 and preserves CSV merge order."""
    old_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", f"{FAKE_AZ_DIR}:{old_path}")
    monkeypatch.setenv("FAKE_AZ_RC_DEFAULT", "success")
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FAKE_AZ_STATE_DIR", str(state_dir))

    # Prepare temporary copy with accelerated collection timings for test execution
    job_content = LINUX_JOB_PATH.read_text(encoding="ascii")
    fast_content = re.sub(r'sleep_before_collect:\s*"[^"]+"', 'sleep_before_collect: "0.1s"', job_content)
    fast_content = re.sub(r'collect_retry_interval:\s*"[^"]+"', 'collect_retry_interval: "0.1s"', fast_content)
    (tmp_path / "job.yaml").write_text(fast_content, encoding="ascii")
    (tmp_path / "list_vm.csv").write_text((LINUX_SHOWCASE_DIR / "list_vm.csv").read_text(encoding="ascii"), encoding="ascii")

    args = argparse.Namespace(
        job_yaml=str(tmp_path / "job.yaml"),
        vm=[],
        limit=None,
        parallel=5,
        dry_run=False,
        resume=False,
        keep_parts=False,
        yes=True,
    )
    rc = run_job(args, profile=Y2B_PROFILE)
    assert rc == 0, f"y2b run failed with return code {rc}"

    out_csv = tmp_path / "result.csv"
    assert out_csv.is_file(), f"Output CSV missing: {out_csv}"

    with open(out_csv, "r", encoding="utf-8") as f:
        rows = list(csv.reader(f))

    assert len(rows) == 4, f"Expected 4 rows (1 header + 3 VMs), got {len(rows)}"
    header = rows[0]
    assert header == EXPECTED_COLUMNS, f"Header mismatch: {header}"

    vm_names = [r[0] for r in rows[1:]]
    assert vm_names == ["lin01", "lin02", "lin03"], f"Merge order mismatch: {vm_names}"


def test_t1_6_fake_az_windows_run(tmp_path, monkeypatch):
    """T1-6: y2p run against fake_az succeeds with exit 0 and preserves CSV merge order."""
    old_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", f"{FAKE_AZ_DIR}:{old_path}")
    monkeypatch.setenv("FAKE_AZ_RC_DEFAULT", "success")
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FAKE_AZ_STATE_DIR", str(state_dir))

    # Prepare temporary copy with accelerated collection timings for test execution
    job_content = WINDOWS_JOB_PATH.read_text(encoding="ascii")
    fast_content = re.sub(r'sleep_before_collect:\s*"[^"]+"', 'sleep_before_collect: "0.1s"', job_content)
    fast_content = re.sub(r'collect_retry_interval:\s*"[^"]+"', 'collect_retry_interval: "0.1s"', fast_content)
    (tmp_path / "job.yaml").write_text(fast_content, encoding="ascii")
    (tmp_path / "list_vm.csv").write_text((WINDOWS_SHOWCASE_DIR / "list_vm.csv").read_text(encoding="ascii"), encoding="ascii")

    args = argparse.Namespace(
        job_yaml=str(tmp_path / "job.yaml"),
        vm=[],
        limit=None,
        parallel=5,
        dry_run=False,
        resume=False,
        keep_parts=False,
        yes=True,
    )
    rc = run_job(args, profile=Y2P_PROFILE)
    assert rc == 0, f"y2p run failed with return code {rc}"

    out_csv = tmp_path / "result.csv"
    assert out_csv.is_file(), f"Output CSV missing: {out_csv}"

    with open(out_csv, "r", encoding="utf-8") as f:
        rows = list(csv.reader(f))

    assert len(rows) == 3, f"Expected 3 rows (1 header + 2 VMs), got {len(rows)}"
    header = rows[0]
    assert header == EXPECTED_COLUMNS, f"Header mismatch: {header}"

    vm_names = [r[0] for r in rows[1:]]
    assert vm_names == ["win01", "win02"], f"Merge order mismatch: {vm_names}"


# ==============================================================================
# T1-7: Safety Rules, Step Structure & Variable Consistency
# ==============================================================================

def test_t1_7_step_definitions_and_safety_rules():
    """T1-7: Verify safety boundaries, stage-proof paths, and column parity across jobs."""
    job_lin = load_job(LINUX_JOB_PATH)
    job_win = load_job(WINDOWS_JOB_PATH)

    # 1. Exactly 7 steps in both jobs
    assert len(job_lin.invoke.runcommand.steps) == 7
    assert len(job_win.invoke.runcommand.steps) == 7
    assert [s.name for s in job_lin.invoke.runcommand.steps] == EXPECTED_STEPS
    assert [s.name for s in job_win.invoke.runcommand.steps] == EXPECTED_STEPS

    # 2. Timing rules: sum of step timeouts <= collect wait window
    wait_time_lin = job_lin.sleep_before_collect + (job_lin.collect_retries * job_lin.collect_retry_interval)
    sum_timeouts_lin = sum(s.timeout for s in job_lin.invoke.runcommand.steps)
    assert sum_timeouts_lin <= wait_time_lin, f"Linux step timeouts {sum_timeouts_lin}s exceed wait time {wait_time_lin}s"

    wait_time_win = job_win.sleep_before_collect + (job_win.collect_retries * job_win.collect_retry_interval)
    sum_timeouts_win = sum(s.timeout for s in job_win.invoke.runcommand.steps)
    assert sum_timeouts_win <= wait_time_win, f"Windows step timeouts {sum_timeouts_win}s exceed wait time {wait_time_win}s"

    # 3. Collect columns parity
    cols_lin = [c.column for c in job_lin.collect]
    cols_win = [c.column for c in job_win.collect]
    assert cols_lin == EXPECTED_COLUMNS
    assert cols_win == EXPECTED_COLUMNS

    # 4. Safety checks in script contents
    lin_scripts = " ".join(s.script or "" for s in job_lin.invoke.runcommand.steps)
    win_scripts = " ".join(s.script or "" for s in job_win.invoke.runcommand.steps)

    forbidden_linux = ["dnf", "yum", "rpm -e", "package-cleanup"]
    for cmd in forbidden_linux:
        assert cmd not in lin_scripts, f"Forbidden command '{cmd}' found in Linux showcase job"

    forbidden_windows = ["Install-Package", "Uninstall-Package", "Install-Module", "Uninstall-Module"]
    for cmd in forbidden_windows:
        assert cmd not in win_scripts, f"Forbidden command '{cmd}' found in Windows showcase job"

    # Writes restricted to /var/tmp/y2demo and C:\y2demo
    assert "/var/tmp/y2demo" in lin_scripts
    assert "C:\\y2demo" in win_scripts


# ==============================================================================
# T1-8: Pinned URL & Immutable SHA256 Verification
# ==============================================================================

def test_t1_8_pinned_url_download():
    """T1-8: Verify pinned download package can be fetched and matches the pinned SHA256."""
    try:
        req = urllib.request.Request(
            PINNED_URL,
            headers={"User-Agent": "y2-test-verifier/1.0"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        pytest.skip(f"Network unavailable for pinned download verification: {exc}")

    actual_sha256 = hashlib.sha256(data).hexdigest()
    assert actual_sha256 == PINNED_SHA256, (
        f"SHA256 mismatch for pinned package: expected {PINNED_SHA256}, got {actual_sha256}"
    )
    assert actual_sha256[:12] == PINNED_SHA256_12, (
        f"First 12 chars mismatch: expected {PINNED_SHA256_12}, got {actual_sha256[:12]}"
    )
    assert len(data) <= 1024 * 1024, f"Pinned package exceeds 1MB limit: {len(data)} bytes"
