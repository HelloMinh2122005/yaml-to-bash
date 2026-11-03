"""Baseline capture script for FX-5.

Captures console stdout and az call count baselines from base commit af5d690
using a clean git worktree.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

BASE_COMMIT = "af5d69049f4d8daa02ee08bc22227a5118cc6755"
REPO_ROOT = Path(__file__).resolve().parent.parent


def normalise_console_output(stdout: str) -> str:
    """Normalise dynamic tokens (timestamps, run-ids, paths) in console stdout."""
    lines = [line for line in stdout.splitlines() if not line.startswith("conformance:")]
    text = "\n".join(lines)
    # Normalise timestamps
    text = re.sub(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?",
        "<TIMESTAMP>",
        text,
    )
    # Normalise run-ids
    text = re.sub(r"\d{8}T\d{6}Z-[0-9a-f]{8}", "<RUN_ID>", text)
    # Normalise paths to output files
    text = re.sub(r"(Output (?:CSV|Log):\s+)\S+/(result\.(?:csv|log))", r"\1<DIR>/\2", text)
    return text.strip() + "\n"


def run_capture() -> None:
    golden_dir = REPO_ROOT / "tests" / "golden"
    golden_dir.mkdir(parents=True, exist_ok=True)

    worktree_dir = Path(tempfile.mkdtemp(prefix="y2_worktree_"))
    try:
        # Create worktree
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree_dir), BASE_COMMIT],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
        )

        fake_dir = worktree_dir / "tests" / "fake_az"
        sub_env = dict(os.environ)
        sub_env["PATH"] = f"{fake_dir}:{sub_env.get('PATH', '')}"
        sub_env["FAKE_AZ_RC_DEFAULT"] = "success"
        sub_env["PYTHONPATH"] = str(worktree_dir)

        tmp_run_dir = Path(tempfile.mkdtemp(prefix="y2_baseline_run_"))
        log_file = tmp_run_dir / "az_calls.log"
        state_dir = tmp_run_dir / "fake_az_state"
        state_dir.mkdir(parents=True, exist_ok=True)
        sub_env["FAKE_AZ_LOG"] = str(log_file)
        sub_env["FAKE_AZ_STATE_DIR"] = str(state_dir)
        os.environ["FAKE_AZ_STATE_DIR"] = str(state_dir)

        # Import classify_argv and make_fleet from the current repo helpers
        sys.path.insert(0, str(REPO_ROOT))
        from tests.helpers_ado import classify_argv, make_fleet

        # 1. Linux example (3 VMs)
        log_file.unlink(missing_ok=True)
        res_lin = subprocess.run(
            [sys.executable, "-m", "y2b", "run", "job.yaml", "-y"],
            cwd=str(worktree_dir / "examples" / "linux"),
            env=sub_env,
            capture_output=True,
            text=True,
        )
        norm_lin = normalise_console_output(res_lin.stdout)
        lin_calls = [
            line.strip().split()
            for line in log_file.read_text(encoding="utf-8").strip().splitlines()
        ]
        lin_counts = dict(Counter(classify_argv(c) for c in lin_calls))

        # 2. Windows example (2 VMs)
        log_file.unlink(missing_ok=True)
        res_win = subprocess.run(
            [sys.executable, "-m", "y2p", "run", "job.yaml", "-y"],
            cwd=str(worktree_dir / "examples" / "windows"),
            env=sub_env,
            capture_output=True,
            text=True,
        )
        norm_win = normalise_console_output(res_win.stdout)
        win_calls = [
            line.strip().split()
            for line in log_file.read_text(encoding="utf-8").strip().splitlines()
        ]
        win_counts = dict(Counter(classify_argv(c) for c in win_calls))

        # 3. 20-VM TP-73 fleet (mixed running / deallocated)
        fleet_20_dir = tmp_run_dir / "fleet_20"
        states_20 = {
            f"lin{i:02d}": ("PowerState/deallocated" if i % 2 == 0 else "PowerState/running")
            for i in range(1, 21)
        }
        make_fleet(
            fleet_20_dir,
            n=20,
            os_type="linux",
            sub="GUID",
            states=states_20,
            sleep_before_collect=0.1,
            collect_retry_interval=0.1,
        )
        log_file.unlink(missing_ok=True)
        res_20 = subprocess.run(
            [sys.executable, "-m", "y2b", "run", "job.yaml", "--parallel", "10", "-y"],
            cwd=str(fleet_20_dir),
            env=sub_env,
            capture_output=True,
            text=True,
        )
        if res_20.returncode != 0:
            raise RuntimeError(f"20-VM fleet run failed (rc={res_20.returncode}):\n{res_20.stderr}")
        fleet_20_calls = [
            line.strip().split()
            for line in log_file.read_text(encoding="utf-8").strip().splitlines()
        ]
        fleet_20_counts = dict(Counter(classify_argv(c) for c in fleet_20_calls))

        # Write golden files
        (golden_dir / "run_console_baseline_linux.txt").write_text(norm_lin, encoding="utf-8")
        (golden_dir / "run_console_baseline_windows.txt").write_text(norm_win, encoding="utf-8")

        summary_json = {
            "commit": BASE_COMMIT,
            "fleet_20vm": fleet_20_counts,
            "linux_example": lin_counts,
            "windows_example": win_counts,
        }
        (golden_dir / "az_call_counts_baseline.json").write_text(
            json.dumps(summary_json, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print("Captured baselines successfully.")
    finally:
        # Cleanup worktree
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_dir)],
            cwd=str(REPO_ROOT),
            check=False,
            capture_output=True,
        )
        if worktree_dir.exists():
            shutil.rmtree(worktree_dir, ignore_errors=True)
        # Cleanup scratch worktree if present from earlier test
        if Path("/tmp/base_af5d690").exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", "/tmp/base_af5d690"],
                cwd=str(REPO_ROOT),
                check=False,
                capture_output=True,
            )


if __name__ == "__main__":
    run_capture()

