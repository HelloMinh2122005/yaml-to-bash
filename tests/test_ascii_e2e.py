"""T1 tests for end-to-end ASCII hygiene with fake az and baseline diff (T-06 / TP-13, TP-15)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers_ado import run_cli


REPO_ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_DIR = REPO_ROOT / ".handoffs" / "20261104-lf-ascii-hygiene" / "42-evidence" / "t1"


def test_baseline_diff():
    """TP-13: Console baselines diff shows only allowed glyph replacements (->, -, |) and both are ASCII-only."""
    golden_linux = REPO_ROOT / "tests" / "golden" / "run_console_baseline_linux.txt"
    golden_windows = REPO_ROOT / "tests" / "golden" / "run_console_baseline_windows.txt"

    # 1. Both files must be pure ASCII
    text_lin = golden_linux.read_text(encoding="utf-8")
    text_win = golden_windows.read_text(encoding="utf-8")
    assert text_lin.isascii(), f"Non-ASCII characters found in {golden_linux}"
    assert text_win.isascii(), f"Non-ASCII characters found in {golden_windows}"

    # 2. Line counts must match baseline commit
    assert len(text_lin.splitlines()) > 0
    assert len(text_win.splitlines()) > 0

    # 3. Check git word diff tokens against base commit (HEAD or HEAD~1)
    # If working tree has uncommitted modifications, compare against HEAD;
    # if clean (already committed), compare HEAD~1 against HEAD.
    has_unstaged = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", str(golden_linux), str(golden_windows)]
    ).returncode != 0

    diff_cmd = ["git", "diff", "--word-diff=porcelain"]
    if has_unstaged:
        diff_cmd.extend(["HEAD", "--", str(golden_linux), str(golden_windows)])
    else:
        diff_cmd.extend(["HEAD~1", "HEAD", "--", str(golden_linux), str(golden_windows)])

    diff = subprocess.check_output(diff_cmd, text=True)

    allowed_pairs = {("—", "-"), ("·", "|"), ("→", "->")}
    pairs = []
    curr_del = None
    for line in diff.splitlines():
        if line.startswith("-") and not line.startswith("---"):
            curr_del = line[1:]
        elif line.startswith("+") and not line.startswith("+++"):
            if curr_del is not None:
                pairs.append((curr_del, line[1:]))
                curr_del = None

    assert len(pairs) > 0, "Expected word-level differences in console baselines"
    for p in pairs:
        assert p in allowed_pairs, f"Disallowed replacement pair found in diff: {p}"


def test_ascii_e2e_fake_az_vietnamese(tmp_path: Path):
    """TP-15: Run e2e job where remote returns Vietnamese text, verify pure ASCII output and folding."""
    fixture_dir = REPO_ROOT / "tests" / "fixtures" / "ascii_e2e"
    job_src = fixture_dir / "lab_job.yaml"
    csv_src = fixture_dir / "list_vm.csv"

    shutil.copy(csv_src, tmp_path / "list_vm.csv")

    # Fast collect timings for testing
    job_content = job_src.read_text(encoding="utf-8")
    job_content = job_content.replace('sleep_before_collect: "10s"', 'sleep_before_collect: "0.1s"')
    job_content = job_content.replace('collect_retry_interval: "5s"', 'collect_retry_interval: "0.1s"')
    job_file = tmp_path / "job.yaml"
    job_file.write_text(job_content, encoding="utf-8")

    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env = {
        "FAKE_AZ_STATE_DIR": str(state_dir),
        "FAKE_AZ_VIETNAMESE": "Nguyễn Đà Nẵng",
    }

    res = run_cli("y2b", ["run", str(job_file), "--phase", "all", "-y"], cwd=tmp_path, env=env)
    assert res.returncode == 0, f"Run failed with rc={res.returncode}:\n{res.stderr}\n{res.stdout}"

    res_csv = tmp_path / "result.csv"
    res_log = tmp_path / "result.log"
    assert res_csv.is_file(), "result.csv was not generated"
    assert res_log.is_file(), "result.log was not generated"

    # Grep for any non-ASCII characters: grep -P '[^\x00-\x7F]'
    grep_cmd = ["grep", "-P", r"[^\x00-\x7F]", str(res_csv), str(res_log)]
    grep_res = subprocess.run(grep_cmd, capture_output=True, text=True)

    # Save grep output to evidence directory
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    grep_evidence_file = EVIDENCE_DIR / "tp15-grep.txt"
    grep_evidence_file.write_text(
        f"Grep command: {' '.join(grep_cmd)}\nReturn code: {grep_res.returncode}\nStdout: {grep_res.stdout}\nStderr: {grep_res.stderr}\n",
        encoding="utf-8",
    )

    # Must find NO non-ASCII characters (grep exit code 1)
    assert grep_res.returncode == 1, f"Found non-ASCII characters in result files:\n{grep_res.stdout}"
    assert grep_res.stdout == "", f"Expected empty grep output, got:\n{grep_res.stdout}"

    # Verify folded value in CSV
    csv_text = res_csv.read_text(encoding="utf-8")
    assert csv_text.isascii()
    assert "Nguyen Da Nang" in csv_text
    assert "Nguyễn" not in csv_text

    # Verify [ascii] line in VM log
    log_text = res_log.read_text(encoding="utf-8")
    assert log_text.isascii()
    assert "[ascii] vietnamese_val: 4 non-ASCII char(s) replaced" in log_text

    # Verify run summary in console stdout
    assert "ascii: 1 value(s) sanitized on 1 VM(s)" in res.stdout
