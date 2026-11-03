"""Tests for export-ado command, YAML structure, script safety, and stability (TP-30, TP-31, TP-32, TP-70)."""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from y2shared.cli import main
from y2shared.config import load_yaml_raw, parse_job_from_dict
from y2shared.export_ado import generate_pipeline_files
from y2shared.profile import Profile
from y2b.profile import PROFILE as b_prof
from y2p.profile import PROFILE as p_prof


def init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(path), check=True)


def extract_inline_scripts(data: Any) -> List[str]:
    """Recursively extract all bash / inlineScript step contents from ADO YAML."""
    scripts: List[str] = []
    if isinstance(data, dict):
        for k, v in data.items():
            if k in ("bash", "script") and isinstance(v, str):
                scripts.append(v)
            elif k in ("inlineScript",) and isinstance(v, str):
                scripts.append(v)
            else:
                scripts.extend(extract_inline_scripts(v))
    elif isinstance(data, list):
        for item in data:
            scripts.extend(extract_inline_scripts(item))
    return scripts


# ==============================================================================
# TP-30: export-ado file set, paths, git boundary, unsafe values, --check
# ==============================================================================


def test_export_ado_six_files_and_byte_equality(tmp_path: Path):
    """Test exact 6 files written, relative paths, byte equality with compile/explain."""
    repo_root = Path(__file__).resolve().parent.parent
    git_dir = tmp_path / "repo"
    git_dir.mkdir()
    init_git_repo(git_dir)

    # Copy linux example
    src_dir = repo_root / "examples" / "linux"
    dst_dir = git_dir / "examples" / "linux"
    shutil.copytree(src_dir, dst_dir)

    job_yaml = dst_dir / "job.yaml"
    out_dir = git_dir / "pipelines" / "generated" / "linux"

    cmd = [
        "export-ado",
        str(job_yaml),
        "-o",
        str(out_dir),
        "--service-connection",
        "sc-azure-lab",
        "--environment",
        "y2-lab",
    ]
    rc = main(b_prof, cmd)
    assert rc == 0

    expected_files = {
        "azure-pipelines.yml",
        "templates/y2-fleet-run.yml",
        "templates/y2-setup.yml",
        "payload/remote.sh",
        "plan/explain.txt",
        "plan/explain.md",
    }
    actual_files = {
        p.relative_to(out_dir).as_posix()
        for p in out_dir.rglob("*")
        if p.is_file()
    }
    assert actual_files == expected_files

    # Compare payload with compile_remote
    raw_dict, fhash = load_yaml_raw(job_yaml)
    job = parse_job_from_dict(raw_dict, job_yaml, fhash)
    compiled, _ = b_prof.compile_remote(job, run_id="preview", check_syntax=False)
    assert (out_dir / "payload" / "remote.sh").read_text(encoding="utf-8") == compiled

    # No tmp_path substring in any generated file
    for rel in actual_files:
        content = (out_dir / rel).read_text(encoding="utf-8")
        assert str(tmp_path) not in content
        assert "tmp_path" not in content

    # Test --check mode: clean -> exits 0
    rc_check = main(b_prof, cmd + ["--check"])
    assert rc_check == 0

    # Test --check mode: modified -> exits 1
    (out_dir / "payload" / "remote.sh").write_text("modified", encoding="utf-8")
    rc_check_mod = main(b_prof, cmd + ["--check"])
    assert rc_check_mod == 1

    # Test --check mode: missing -> exits 1
    (out_dir / "payload" / "remote.sh").unlink()
    rc_check_miss = main(b_prof, cmd + ["--check"])
    assert rc_check_miss == 1


def test_export_ado_git_boundaries_and_unsafe_tokens(tmp_path: Path):
    """Test running outside git tree and unsafe tokens produce error."""
    # Outside git
    job_yaml = tmp_path / "job.yaml"
    job_yaml.write_text("input: list.csv\noutput_csv: out.csv\noutput_log: out.log\n", encoding="utf-8")
    (tmp_path / "list.csv").write_text("vm_name,resource_group,subscription\nvm1,rg,sub\n", encoding="utf-8")

    rc = main(b_prof, ["export-ado", str(job_yaml), "-o", str(tmp_path / "out"), "--service-connection", "sc", "--environment", "env"])
    assert rc == 1

    # In git repo, test unsafe values
    git_dir = tmp_path / "repo"
    git_dir.mkdir()
    init_git_repo(git_dir)
    repo_root = Path(__file__).resolve().parent.parent
    shutil.copytree(repo_root / "examples" / "linux", git_dir / "examples" / "linux")
    valid_job = git_dir / "examples" / "linux" / "job.yaml"

    unsafe_args = [
        ["--service-connection", "$(inject)"],
        ["--service-connection", "sc`whoami`"],
        ["--environment", "${{ parameters.x }}"],
        ["--vm-image", "ubuntu\nlatest"],
        ["--vm-image", "windows-latest"],  # Windows image rejected
    ]
    for bad in unsafe_args:
        cmd = ["export-ado", str(valid_job), "-o", str(git_dir / "out"), "--service-connection", "sc", "--environment", "env"]
        # Replace or add
        if bad[0] in cmd:
            idx = cmd.index(bad[0])
            cmd[idx + 1] = bad[1]
        else:
            cmd.extend(bad)
        rc_bad = main(b_prof, cmd)
        assert rc_bad == 1


# ==============================================================================
# TP-31: Pipeline Structure and Behavioral Checks
# ==============================================================================


@pytest.mark.parametrize("tool,job_subpath,os_family", [
    ("y2b", "examples/linux/job.yaml", "linux"),
    ("y2p", "examples/windows/job.yaml", "windows"),
])
def test_export_ado_pipeline_structure(tool: str, job_subpath: str, os_family: str, tmp_path: Path):
    """Test stage ordering, Delay@1 math, Collect condition, exit 2 mapping, artifacts."""
    repo_root = Path(__file__).resolve().parent.parent
    git_dir = tmp_path / "repo"
    git_dir.mkdir()
    init_git_repo(git_dir)

    shutil.copytree(repo_root / "examples", git_dir / "examples")
    job_yaml = git_dir / job_subpath
    out_dir = git_dir / "pipelines" / "generated" / os_family

    prof = b_prof if tool == "y2b" else p_prof
    rc = main(prof, [
        "export-ado",
        str(job_yaml),
        "-o",
        str(out_dir),
        "--service-connection",
        "sc-azure-lab",
        "--environment",
        "y2-lab",
        "--recollect-minutes",
        "30",
    ])
    assert rc == 0

    fleet_yml = (out_dir / "templates" / "y2-fleet-run.yml").read_text(encoding="utf-8")
    data = yaml.safe_load(fleet_yml)

    # 1. Check stages / jobs
    stages = data.get("stages", [])
    stage_names = [st.get("stage") for st in stages]
    assert stage_names == ["Validate", "Fleet"]

    jobs = []
    for st in stages:
        jobs.extend(st.get("jobs", []))

    job_names = [j.get("job") or j.get("deployment") for j in jobs]
    assert job_names == ["Validate", "Invoke", "Wait", "Collect", "Wait2", "Collect2"]

    # 2. Check Invoke job
    invoke_job = jobs[1]
    assert invoke_job.get("environment") == "y2-lab"
    invoke_steps = invoke_job["strategy"]["runOnce"]["deploy"]["steps"]
    step_display_names = [s.get("displayName", "") for s in invoke_steps]
    assert any("Phase 1 · invoke (run-command 1 of 2)" in dn for dn in step_display_names)
    assert any(s.get("name") == "fire" for s in invoke_steps)
    assert any(s.get("name") == "pack" for s in invoke_steps)

    # 3. Check Wait job
    wait_job = jobs[2]
    assert wait_job.get("pool") == "server"
    wait_delay = wait_job["steps"][0]["inputs"]["delayForMinutes"]
    # For linux (15s sleep) -> delay 1m. timeout = 1 + 15 = 16
    assert int(wait_delay) >= 1
    assert wait_job.get("timeoutInMinutes") == int(wait_delay) + 15
    assert "ne(dependencies.Invoke.outputs['Invoke.fire.fired'], '0')" in wait_job.get("condition", "")

    # 4. Check Collect job
    collect_job = jobs[3]
    collect_cond = collect_job.get("condition", "")
    assert "dependencies.Invoke.outputs['Invoke.pack.hasState']" in collect_cond
    assert "Succeeded" in collect_cond and "Skipped" in collect_cond
    collect_steps = collect_job.get("steps", [])
    collect_display_names = [s.get("displayName", "") for s in collect_steps]
    assert any("Phase 2 · collect (run-command 2 of 2) + Phase 3 post + merge" in dn for dn in collect_display_names)

    # Check exit code 2 mapping in collect step
    collect_script = [s.get("inputs", {}).get("inlineScript", "") for s in collect_steps if s.get("name") == "collect"][0]
    assert "exit 2" not in collect_script or "SucceededWithIssues" in collect_script

    # 5. Check Wait2 / Collect2
    wait2_job = jobs[4]
    assert wait2_job["steps"][0]["inputs"]["delayForMinutes"] == "30"
    assert wait2_job.get("timeoutInMinutes") == 45


# ==============================================================================
# TP-32 & TP-70: YAML Safety, Stability, bash -n on inline scripts
# ==============================================================================


def test_export_ado_safe_and_stable(tmp_path: Path):
    """Test yaml.safe_load, command substitution restrictions, committed equality."""
    repo_root = Path(__file__).resolve().parent.parent

    # Test committed pipelines equal fresh output (--check exits 0)
    rc_lin = main(b_prof, [
        "export-ado",
        str(repo_root / "examples" / "linux" / "job.yaml"),
        "-o",
        str(repo_root / "pipelines" / "generated" / "linux"),
        "--service-connection",
        "sc-azure-lab",
        "--environment",
        "y2-lab",
        "--check",
    ])
    assert rc_lin == 0

    rc_win = main(p_prof, [
        "export-ado",
        str(repo_root / "examples" / "windows" / "job.yaml"),
        "-o",
        str(repo_root / "pipelines" / "generated" / "windows"),
        "--service-connection",
        "sc-azure-lab",
        "--environment",
        "y2-lab",
        "--check",
    ])
    assert rc_win == 0

    # Check script safety: no backticks, no $((, no $( except ADO predefined vars
    allowed_macro_prefix = ("$(Build.", "$(Agent.", "$(Pipeline.", "$(System.")
    for p in (repo_root / "pipelines" / "generated").rglob("*.yml"):
        content = p.read_text(encoding="utf-8")
        data = yaml.safe_load(content)

        scripts = extract_inline_scripts(data)
        for s in scripts:
            assert "`" not in s, f"Backtick found in inline script in {p}"
            assert "$((" not in s, f"$(( found in inline script in {p}"

            # Find all $(...) occurrences
            macros = re.findall(r"\$\([^)]+\)", s)
            for m in macros:
                assert any(m.startswith(pre) for pre in allowed_macro_prefix), f"Disallowed macro {m} in {p}"


def test_inline_scripts_bash_n(tmp_path: Path):
    """TP-70: Extract every inline script and run bash -n."""
    repo_root = Path(__file__).resolve().parent.parent
    scripts_checked = 0

    for p in (repo_root / "pipelines" / "generated").rglob("*.yml"):
        content = p.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
        scripts = extract_inline_scripts(data)

        for script_text in scripts:
            # Replace ADO macros $(...) with mock variable
            sanitized = re.sub(r"\$\([^)]+\)", "__MOCK_VAR__", script_text)
            # Replace ADO expression ${{ ... }} with mock string
            sanitized = re.sub(r"\$\{\{[^}]+\}\}", "__MOCK_EXPR__", sanitized)

            # Run bash -n
            res = subprocess.run(
                ["bash", "-n"],
                input=sanitized,
                capture_output=True,
                text=True,
            )
            assert res.returncode == 0, f"bash -n failed on script in {p}:\n{sanitized}\nStderr: {res.stderr}"
            scripts_checked += 1

    assert scripts_checked >= 6, f"Expected at least 6 inline scripts checked, got {scripts_checked}"
