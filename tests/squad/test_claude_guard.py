from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
import pytest


@pytest.mark.parametrize(
    "role,path,ok",
    [
        ("y2-lead", ".handoffs/20261101-x/20-spec.md", True),
        ("y2-lead", ".handoffs/20261101-x/state.json", True),
        ("y2-lead", ".claude/squad-learnings.md", True),
        ("y2-lead", ".handoffs/PROTOCOL.md", False),
        ("y2-lead", ".handoffs/20261101-x/40-delivery-report.md", False),
        ("y2-lead", "y2shared/runner.py", False),
        ("y2-planner", ".handoffs/20261101-x/22-tasks.md", True),
        ("y2-planner", ".handoffs/20261101-x/20-spec.md", False),
        ("y2-test-planner", ".handoffs/20261101-x/23-test-plan.md", True),
        ("y2-test-planner", "tests/test_x.py", False),
        ("y2-research-planner", ".handoffs/20261101-x/10-research-plan.md", True),
        (None, "y2shared/runner.py", True),
    ],
)
def test_guard_table(role, path, ok, repo_root: Path):
    guard_script = repo_root / ".claude" / "hooks" / "guard_y2_writes.py"
    payload = {
        "tool_input": {"file_path": path},
    }
    if role:
        payload["agent_type"] = role

    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(repo_root)

    res = subprocess.run(
        [sys.executable, str(guard_script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        cwd=str(repo_root),
    )
    expected_rc = 0 if ok else 2
    assert res.returncode == expected_rc, f"Failed for role={role}, path={path}. stderr: {res.stderr}"


def test_traversal_denied(repo_root: Path):
    guard_script = repo_root / ".claude" / "hooks" / "guard_y2_writes.py"
    payload = {
        "agent_type": "y2-lead",
        "tool_input": {"file_path": ".handoffs/x/../../y2shared/a.py"},
    }
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(repo_root)

    res = subprocess.run(
        [sys.executable, str(guard_script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        cwd=str(repo_root),
    )
    assert res.returncode == 2


def test_role_from_argv(repo_root: Path):
    guard_script = repo_root / ".claude" / "hooks" / "guard_y2_writes.py"
    payload = {
        "tool_input": {"file_path": "y2shared/a.py"},
    }
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(repo_root)

    res = subprocess.run(
        [sys.executable, str(guard_script), "y2-lead"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        cwd=str(repo_root),
    )
    assert res.returncode == 2


def test_malformed_input_denied(repo_root: Path):
    guard_script = repo_root / ".claude" / "hooks" / "guard_y2_writes.py"
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(repo_root)

    res = subprocess.run(
        [sys.executable, str(guard_script)],
        input="not json",
        text=True,
        capture_output=True,
        env=env,
        cwd=str(repo_root),
    )
    assert res.returncode == 2


def test_settings_json_valid(repo_root: Path):
    settings_file = repo_root / ".claude" / "settings.json"
    assert settings_file.is_file()
    data = json.loads(settings_file.read_text())
    deny = data.get("permissions", {}).get("deny", [])
    assert "Bash(az:*)" in deny
    assert "Read(./lab/terraform.tfvars)" in deny

