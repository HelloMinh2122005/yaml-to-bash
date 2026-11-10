"""Tests for root .gitattributes LF policy enforcement (AC-1, TP-01)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_gitattributes_file_content() -> None:
    """Verify .gitattributes exists at repo root and defines LF policy."""
    gitattributes_path = REPO_ROOT / ".gitattributes"
    assert gitattributes_path.is_file(), f"Expected {gitattributes_path} to exist"

    raw_bytes = gitattributes_path.read_bytes()
    assert b"\r" not in raw_bytes, ".gitattributes must have LF line endings (no CR)"

    content = raw_bytes.decode("utf-8")
    lines = [line.strip() for line in content.splitlines() if line.strip() and not line.strip().startswith("#")]
    assert "* text=auto eol=lf" in lines, f"Expected '* text=auto eol=lf' in .gitattributes, got: {lines}"


def test_git_check_attr_resolution() -> None:
    """Verify git check-attr resolves text=auto and eol=lf across repository files."""
    sample_files = [
        "README.md",
        "USER_GUIDE.md",
        "bin/y2b",
        "bin/y2p",
        "pipelines/linux.yml",
        "pipelines/windows.yml",
        "examples/linux/job.yaml",
        "examples/windows/job.yaml",
        "y2shared/runner.py",
        "tests/golden/hip_compliance_remote.sh",
        "tests/golden/windows_baseline_remote.ps1",
    ]

    for rel_path in sample_files:
        full_path = REPO_ROOT / rel_path
        if not full_path.exists():
            continue
        res = subprocess.run(
            ["git", "check-attr", "text", "eol", "--", rel_path],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
        output = res.stdout
        assert f"{rel_path}: text: auto" in output, f"Unexpected text attr for {rel_path}:\n{output}"
        assert f"{rel_path}: eol: lf" in output, f"Unexpected eol attr for {rel_path}:\n{output}"


def test_autocrlf_clone_checkout_and_renormalize() -> None:
    """TP-01: Verify clone with core.autocrlf=true produces LF line endings and renormalize stages nothing."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        clone_path = Path(tmp_dir) / "clone"

        # Clone repository with core.autocrlf=true
        subprocess.run(
            ["git", "clone", "-c", "core.autocrlf=true", str(REPO_ROOT), str(clone_path)],
            check=True,
            capture_output=True,
            text=True,
        )

        # If .gitattributes was not yet committed in source HEAD, ensure it is active in clone
        gitattributes_in_clone = clone_path / ".gitattributes"
        if not gitattributes_in_clone.exists():
            shutil.copy2(REPO_ROOT / ".gitattributes", gitattributes_in_clone)
            subprocess.run(["git", "-C", str(clone_path), "config", "user.name", "Test Runner"], check=True)
            subprocess.run(["git", "-C", str(clone_path), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(clone_path), "add", ".gitattributes"], check=True)
            subprocess.run(
                ["git", "-C", str(clone_path), "commit", "-m", "chore: add .gitattributes for test"],
                check=True,
                capture_output=True,
            )
            # Re-checkout files so git checks them out with .gitattributes active under core.autocrlf=true
            for item in clone_path.iterdir():
                if item.name == ".git":
                    continue
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
            subprocess.run(
                ["git", "-C", str(clone_path), "checkout", "HEAD", "--", "."],
                check=True,
                capture_output=True,
            )

        # Check git ls-files --eol in clone
        res_eol = subprocess.run(
            ["git", "-C", str(clone_path), "ls-files", "--eol"],
            check=True,
            capture_output=True,
            text=True,
        )

        eol_lines = res_eol.stdout.splitlines()
        assert eol_lines, "git ls-files --eol returned no output"

        crlf_files: list[str] = []
        mixed_files: list[str] = []
        for line in eol_lines:
            if "w/crlf" in line:
                crlf_files.append(line)
            if "w/mixed" in line:
                mixed_files.append(line)

        assert not crlf_files, f"Found files with CRLF working tree line endings in autocrlf clone:\n" + "\n".join(crlf_files)
        assert not mixed_files, f"Found files with mixed working tree line endings in autocrlf clone:\n" + "\n".join(mixed_files)

        # Run git add --renormalize . and assert diff --cached --quiet exits 0
        subprocess.run(
            ["git", "-C", str(clone_path), "add", "--renormalize", "."],
            check=True,
            capture_output=True,
            text=True,
        )

        res_diff = subprocess.run(
            ["git", "-C", str(clone_path), "diff", "--cached", "--quiet"],
            capture_output=True,
        )
        assert res_diff.returncode == 0, (
            f"git diff --cached --quiet exited {res_diff.returncode} after renormalize (staged changes present)"
        )
