from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
import pytest


def _run_hook(script_path: Path, payload: dict | str, repo_root: Path) -> dict:
    stdin_data = payload if isinstance(payload, str) else json.dumps(payload)
    res = subprocess.run(
        [sys.executable, str(script_path)],
        input=stdin_data,
        text=True,
        capture_output=True,
        cwd=str(repo_root),
    )
    assert res.returncode == 0, f"Hook crashed with code {res.returncode}. Stderr: {res.stderr}"
    return json.loads(res.stdout)


@pytest.mark.parametrize(
    "path",
    [
        ".handoffs/20261101-x/40-delivery-report.md",
        ".handoffs/20261101-x/42-evidence/t3/post-state.txt",
        "y2shared/runner.py",
        "tests/test_x.py",
        "README.md",
    ],
)
def test_protect_allows_ag_files(path, repo_root: Path):
    script = repo_root / ".agents" / "plugins" / "y2" / "scripts" / "protect_handoff.py"
    payload = {"toolCall": {"name": "write_to_file", "args": {"TargetFile": path}}}
    out = _run_hook(script, payload, repo_root)
    assert out.get("decision") == "allow", f"Failed for {path}: {out}"


@pytest.mark.parametrize(
    "path",
    [
        ".handoffs/20261101-x/20-spec.md",
        ".handoffs/20261101-x/23-test-plan.md",
        ".handoffs/PROTOCOL.md",
        ".claude/agents/y2-lead.md",
        "docs/superpowers/specs/a.md",
    ],
)
def test_protect_denies_claude_files(path, repo_root: Path):
    script = repo_root / ".agents" / "plugins" / "y2" / "scripts" / "protect_handoff.py"
    payload = {"toolCall": {"name": "write_to_file", "args": {"TargetFile": path}}}
    out = _run_hook(script, payload, repo_root)
    assert out.get("decision") == "deny", f"Should have denied {path}: {out}"


@pytest.mark.parametrize(
    "path",
    [
        "lab/az-shim/az",
        "lab/run-lab.sh",
        "lab/lab.env",
        ".agents/plugins/y2/scripts/lab_guard.py",
        ".agents/plugins/y2/hooks.json",
    ],
)
def test_protect_denies_guard_files(path, repo_root: Path):
    script = repo_root / ".agents" / "plugins" / "y2" / "scripts" / "protect_handoff.py"
    payload = {"toolCall": {"name": "write_to_file", "args": {"TargetFile": path}}}
    out = _run_hook(script, payload, repo_root)
    assert out.get("decision") == "deny", f"Should have denied guard file {path}: {out}"


def test_protect_ignores_content_args(repo_root: Path):
    script = repo_root / ".agents" / "plugins" / "y2" / "scripts" / "protect_handoff.py"
    payload = {
        "toolCall": {
            "name": "write_to_file",
            "args": {
                "TargetFile": "y2shared/a.py",
                "CodeContent": "see /.claude/x or .handoffs/PROTOCOL.md or lab/run-lab.sh",
            },
        }
    }
    out = _run_hook(script, payload, repo_root)
    assert out.get("decision") == "allow"


@pytest.mark.parametrize(
    "cmd",
    [
        "terraform plan",
        "cd lab && terraform apply",
    ],
)
def test_lab_guard_denies_terraform(cmd, repo_root: Path):
    script = repo_root / ".agents" / "plugins" / "y2" / "scripts" / "lab_guard.py"
    payload = {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}}
    out = _run_hook(script, payload, repo_root)
    assert out.get("decision") == "deny"


@pytest.mark.parametrize(
    "cmd",
    [
        "az vm list",
        "bash -c 'az group list'",
    ],
)
def test_lab_guard_denies_bare_az(cmd, repo_root: Path):
    script = repo_root / ".agents" / "plugins" / "y2" / "scripts" / "lab_guard.py"
    payload = {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}}
    out = _run_hook(script, payload, repo_root)
    assert out.get("decision") == "deny"


def test_lab_guard_allows_runner(repo_root: Path):
    script = repo_root / ".agents" / "plugins" / "y2" / "scripts" / "lab_guard.py"
    payload = {
        "toolCall": {
            "name": "run_command",
            "args": {"CommandLine": "lab/run-lab.sh -- y2b run examples/hip-compliance/job.yaml -y"},
        }
    }
    out = _run_hook(script, payload, repo_root)
    assert out.get("decision") == "allow"


@pytest.mark.parametrize(
    "cmd",
    [
        "y2b run job.yaml",
        "bin/y2p run x.yaml --dry-run",
    ],
)
def test_lab_guard_denies_direct_y2_run(cmd, repo_root: Path):
    script = repo_root / ".agents" / "plugins" / "y2" / "scripts" / "lab_guard.py"
    payload = {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}}
    out = _run_hook(script, payload, repo_root)
    assert out.get("decision") == "deny"


@pytest.mark.parametrize(
    "cmd",
    [
        "python3 -m pytest tests/ -q",
        "y2b validate examples/hip-compliance/job.yaml",
    ],
)
def test_lab_guard_allows_local_work(cmd, repo_root: Path):
    script = repo_root / ".agents" / "plugins" / "y2" / "scripts" / "lab_guard.py"
    payload = {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}}
    out = _run_hook(script, payload, repo_root)
    assert out.get("decision") == "allow"


@pytest.mark.parametrize(
    "tool,args",
    [
        ("view_file", {"AbsolutePath": str(Path.home() / ".config/y2-lab/sp.env")}),
        ("view_file", {"TargetFile": "lab/terraform.tfvars"}),
        ("run_command", {"CommandLine": "cat lab/terraform.tfvars"}),
    ],
)
def test_lab_guard_denies_secret_reads(tool, args, repo_root: Path):
    script = repo_root / ".agents" / "plugins" / "y2" / "scripts" / "lab_guard.py"
    payload = {"toolCall": {"name": tool, "args": args}}
    out = _run_hook(script, payload, repo_root)
    assert out.get("decision") == "deny"


def test_malformed_payload_denies(repo_root: Path):
    for script_name in ["protect_handoff.py", "lab_guard.py"]:
        script = repo_root / ".agents" / "plugins" / "y2" / "scripts" / script_name
        res = subprocess.run(
            [sys.executable, str(script)],
            input="{",
            text=True,
            capture_output=True,
            cwd=str(repo_root),
        )
        assert res.returncode == 0
        out = json.loads(res.stdout)
        assert out.get("decision") == "deny"


def test_hooks_json_valid(repo_root: Path):
    hooks_file = repo_root / ".agents" / "plugins" / "y2" / "hooks.json"
    assert hooks_file.is_file()
    data = json.loads(hooks_file.read_text())
    assert "y2-guards" in data
    # Verify script files referenced exist
    scripts_dir = repo_root / ".agents" / "plugins" / "y2" / "scripts"
    assert (scripts_dir / "protect_handoff.py").is_file()
    assert (scripts_dir / "lab_guard.py").is_file()
