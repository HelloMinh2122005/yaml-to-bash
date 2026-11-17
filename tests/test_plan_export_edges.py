"""Adversarial edge-case tests for plan rendering, export, and compilers.

Every test asserts one intended behavior of ``plan``, ``explain``,
``conformance``, ``export_ado``, and the ``y2b``/``y2p`` compilers. Tests
marked ``bug`` reproduce a confirmed defect.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.helpers_edges import make_vm_row
from y2b.compiler import compile_job as compile_bash
from y2b.profile import PROFILE as B_PROFILE
from y2p.compiler import compile_job as compile_ps
from y2p.compiler import ps_quote
from y2shared.aztrace import TraceRecorder
from y2shared.config import Job, load_job
from y2shared.conformance import ConformanceReport, evaluate, format_summary
from y2shared.explain import render_json, render_md, render_text
from y2shared.export_ado import (
    cmd_export_ado,
    format_header_comment,
    generate_pipeline_files,
    sanitize_one_line,
    validate_safe_token,
)
from y2shared.inputcsv import VMRow, read_input_csv
from y2shared.plan import PlanContext, build_plan, format_script_display

PAYLOAD_SHA = "0" * 64
REPO_ROOT = Path(__file__).resolve().parent.parent


def load_linux_plan(tmp_path: Path) -> tuple[Job, list[VMRow], dict[str, Any]]:
    """Load the Linux example job, redirect outputs to tmp_path, and build its plan."""
    job = load_job("examples/linux/job.yaml")
    job.output_csv = tmp_path / "result.csv"
    job.output_log = tmp_path / "result.log"
    rows, _, _ = read_input_csv(job.input, job=job)
    return job, rows, build_plan(job, rows, B_PROFILE, payload_sha256=PAYLOAD_SHA)


def _init_git_repo_with_linux_job(tmp_path: Path) -> tuple[Path, Path]:
    """Create a temporary git repo containing a copy of examples/linux and return (repo_dir, job_yaml)."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo_dir), check=True)
    shutil.copytree(REPO_ROOT / "examples" / "linux", repo_dir / "examples" / "linux")
    return repo_dir, repo_dir / "examples" / "linux" / "job.yaml"


# ---------------------------------------------------------------------------
# plan.py
# ---------------------------------------------------------------------------


def test_plan_context_groups_duplicate_subscriptions():
    """branch: the first VM owns the subscription; later VMs are listed as shared."""
    job = load_job("examples/linux/job.yaml")
    rows = [
        make_vm_row(1, "vm-a", subscription="Sub One"),
        make_vm_row(2, "vm-b", subscription="Sub One"),
        make_vm_row(3, "vm-c", subscription="Sub Two"),
    ]
    ctx = PlanContext.from_rows(job, rows, B_PROFILE, payload_sha256=PAYLOAD_SHA)
    assert ctx.subscriptions == [
        {"value": "Sub One", "is_guid": False, "vm_count": 2},
        {"value": "Sub Two", "is_guid": False, "vm_count": 1},
    ]
    assert ctx.sub_owners == {"Sub One": "vm-a", "Sub Two": "vm-c"}
    assert ctx.sub_shared == {"Sub One": ["vm-b"], "Sub Two": []}


def test_format_script_display_substitutes_known_env_only():
    """EP: only names present in env are substituted; unknown names survive verbatim."""
    assert format_script_display("hostname $VM_NAME", {"VM_NAME": "vm-a"}) == "hostname vm-a"
    assert format_script_display("echo $UNKNOWN", {}) == "echo $UNKNOWN"


def test_plan_exposes_schema_and_api_version(tmp_path):
    """EP: the structured plan declares the pinned schema and Compute API version."""
    _, _, plan = load_linux_plan(tmp_path)
    assert plan["schema"] == "y2-plan/1"
    assert plan["api_version"] == "2024-07-01"


def test_plan_exposes_supplied_payload_sha256(tmp_path):
    """EP: the structured plan records the supplied payload SHA-256 digest."""
    _, _, plan = load_linux_plan(tmp_path)
    assert plan["fleet"]["payload"]["sha256"] == PAYLOAD_SHA


def test_plan_caps_max_run_commands_at_two_per_vm(tmp_path):
    """BVA: max_run_commands equals 2 * vm_count."""
    _, _, plan = load_linux_plan(tmp_path)
    assert plan["fleet"]["max_run_commands"] == 2 * plan["fleet"]["vm_count"]


def test_render_text_includes_payload_sha256(tmp_path):
    """branch: the text renderer reports the payload hash."""
    _, _, plan = load_linux_plan(tmp_path)
    assert f"payload sha256: {PAYLOAD_SHA}" in render_text(plan)


def test_render_json_round_trips_the_plan(tmp_path):
    """EP: the JSON renderer is lossless."""
    _, _, plan = load_linux_plan(tmp_path)
    assert json.loads(render_json(plan)) == plan


def test_render_md_has_one_heading_per_vm(tmp_path):
    """BVA: the number of VM headings equals the number of planned VMs."""
    _, _, plan = load_linux_plan(tmp_path)
    md = render_md(plan)
    assert md.count("## VM ") == len(plan["vms"])


@pytest.mark.bug
def test_render_md_does_not_inject_new_headings_from_vm_name(tmp_path):
    """EP: a newline in a VM name must not create additional Markdown structure."""
    _, _, plan = load_linux_plan(tmp_path)
    plan["vms"][0]["vm_name"] = "vm-1\n## Injected"
    md = render_md(plan)
    assert not any(line.startswith("## Injected") for line in md.splitlines())


# ---------------------------------------------------------------------------
# conformance.py
# ---------------------------------------------------------------------------


def test_format_summary_reports_matched_and_total():
    """EP: the conformance footer uses the documented 'matched/total' shape."""
    report = ConformanceReport(matched=2, total=3, mismatches={"vm-3": "missing reader"})
    assert format_summary(report) == "conformance: 2/3 VMs matched plan"


# ---------------------------------------------------------------------------
# export_ado.py
# ---------------------------------------------------------------------------


def test_sanitize_one_line_flattens_control_characters():
    """EP: CR, LF, TAB, and NUL never survive into a generated YAML comment."""
    out = sanitize_one_line("a\r\nb\tc\x00d")
    assert not any(ch in out for ch in "\r\n\t\x00")
    assert out.startswith("a") and out.endswith("d")


@pytest.mark.parametrize("value", ["", "bad\nname", "bad name", 'bad"name'])
def test_validate_safe_token_rejects_unsafe_values(value):
    """EP: empty, newline, space, and shell/YAML metacharacters are rejected."""
    with pytest.raises(ValueError):
        validate_safe_token(value, "token")


def test_validate_safe_token_allows_spaces_when_requested():
    """EP: allow_spaces=True permits space-separated names."""
    validate_safe_token("a b", "name", allow_spaces=True)


def test_validate_safe_token_rejects_spaces_by_default():
    """EP: allow_spaces=False rejects spaces."""
    with pytest.raises(ValueError):
        validate_safe_token("a b", "name", allow_spaces=False)


def test_validate_safe_token_accepts_colon_without_space():
    """EP: bare ':' without spaces is accepted by token validation."""
    validate_safe_token("bad:name", "token")


def test_format_header_comment_sanitizes_step_names():
    """EP: a step name carrying a newline cannot escape the comment block."""
    job = load_job("examples/linux/job.yaml")
    job.invoke.runcommand.steps[0].name = "step-1\n# Injected"
    header = format_header_comment(job, B_PROFILE, "examples/linux/job.yaml")
    assert all(line.startswith("#") for line in header.splitlines())
    assert "\n# Injected" not in header


# ---------------------------------------------------------------------------
# y2b/compiler.py
# ---------------------------------------------------------------------------


def test_compile_bash_strips_shebang_from_external_step_script(tmp_path):
    """branch: external step scripts lose their leading shebang inside the step function."""
    script_file = tmp_path / "step.sh"
    script_file.write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    job = load_job("examples/linux/job.yaml")
    job.invoke.runcommand.steps[0].script = None
    job.invoke.runcommand.steps[0].script_path = script_file
    compiled, errors = compile_bash(job, check_syntax=False)
    assert errors == []
    func_body = compiled.split("y2b_step_1() {", 1)[1].split("\n}", 1)[0]
    assert "#!/bin/bash" not in func_body
    assert "  echo hi" in func_body


def test_compile_bash_appends_noop_colon_for_comment_only_step():
    """BVA: a comment-only step body receives a ':' no-op so bash stays valid."""
    job = load_job("examples/linux/job.yaml")
    job.invoke.runcommand.steps[0].script = "# only a comment"
    compiled, errors = compile_bash(job, check_syntax=False)
    assert errors == []
    func_body = compiled.split("y2b_step_1() {", 1)[1].split("\n}", 1)[0]
    assert "  :" in func_body


def test_compile_bash_replaces_every_template_token():
    """EP: no template placeholder survives compilation."""
    job = load_job("examples/linux/job.yaml")
    compiled, errors = compile_bash(job, check_syntax=False)
    assert errors == []
    for token in (
        "__JOB_NAME__",
        "__JOB_HASH__",
        "__RUN_ID__",
        "__RES_FILE__",
        "__LOG_FILE__",
        "__STEP_FUNCTIONS__",
        "__EXPORT_FUNCTIONS__",
        "__RUN_STEPS__",
    ):
        assert token not in compiled


@pytest.mark.bug
def test_compile_bash_preserves_literal_run_steps_in_script_body():
    """EP: step content containing '__RUN_STEPS__' is data, not a template slot."""
    job = load_job("examples/linux/job.yaml")
    job.invoke.runcommand.steps[0].script = 'echo "__RUN_STEPS__"'
    compiled, _ = compile_bash(job, check_syntax=False)
    assert 'echo "__RUN_STEPS__"' in compiled


@pytest.mark.bug
def test_compile_bash_does_not_execute_newline_in_step_name():
    """EP: a newline in a step name must not inject an executable line."""
    job = load_job("examples/linux/job.yaml")
    job.invoke.runcommand.steps[0].name = "step-1\n  echo INJECTED"
    compiled, _ = compile_bash(job, check_syntax=False)
    assert "\n  echo INJECTED" not in compiled


# ---------------------------------------------------------------------------
# y2p/compiler.py
# ---------------------------------------------------------------------------


def test_ps_quote_doubles_single_quotes():
    """EP: PowerShell single-quoted literals escape a quote by doubling it."""
    assert ps_quote("O'Brien") == "'O''Brien'"
    assert ps_quote("plain") == "'plain'"


def test_compile_ps_replaces_every_template_token():
    """EP: the PowerShell payload has no unreplaced placeholder and quotes names."""
    job = load_job("examples/windows/job.yaml")
    compiled, errors = compile_ps(job, check_syntax=False)
    assert errors == []
    for token in (
        "__JOB_NAME__",
        "__JOB_HASH__",
        "__RUN_ID__",
        "__RES_FILE__",
        "__LOG_FILE__",
        "__STEP_TABLE__",
    ):
        assert token not in compiled
    assert "Name = 'check-defender'" in compiled


# ---------------------------------------------------------------------------
# Additional plan, conformance, export_ado, and bash syntax checks
# ---------------------------------------------------------------------------


def test_generate_pipeline_files_emits_expected_six_paths(tmp_path):
    """EP: generate_pipeline_files returns the documented six relative pipeline artifacts."""
    repo_dir, job_yaml = _init_git_repo_with_linux_job(tmp_path)
    files = generate_pipeline_files(
        load_job(job_yaml),
        B_PROFILE,
        repo_dir / "pipelines" / "out",
        service_connection="sc-azure",
        environment="y2-lab",
    )
    assert set(files.keys()) == {
        "azure-pipelines.yml",
        "templates/y2-fleet-run.yml",
        "templates/y2-setup.yml",
        "payload/remote.sh",
        "plan/explain.txt",
        "plan/explain.md",
    }


def test_cmd_export_ado_check_detects_stale_file(tmp_path):
    """branch: export-ado --check returns 1 when a generated file is modified."""
    repo_dir, job_yaml = _init_git_repo_with_linux_job(tmp_path)
    out_dir = repo_dir / "pipelines" / "out"
    base_args = {
        "job_yaml": str(job_yaml),
        "output": str(out_dir),
        "service_connection": "sc-azure",
        "environment": "y2-lab",
        "vm_image": None,
        "pool": None,
        "recollect_minutes": 0,
    }
    assert cmd_export_ado(argparse.Namespace(**base_args, check=False), B_PROFILE) == 0
    (out_dir / "plan" / "explain.txt").write_text("stale\n", encoding="utf-8")
    assert cmd_export_ado(argparse.Namespace(**base_args, check=True), B_PROFILE) == 1


def test_compile_bash_output_passes_bash_syntax_check():
    """EP: compiled Linux remote payload parses cleanly under bash -n."""
    compiled, errors = compile_bash(load_job("examples/linux/job.yaml"), check_syntax=False)
    assert errors == []
    proc = subprocess.run(["bash", "-n"], input=compiled, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.bug
def test_generate_pipeline_files_colon_with_space_produces_valid_yaml(tmp_path):
    """EP: token validation must not admit ': ' that breaks generated YAML."""
    repo_dir, job_yaml = _init_git_repo_with_linux_job(tmp_path)
    files = generate_pipeline_files(
        load_job(job_yaml),
        B_PROFILE,
        repo_dir / "pipelines" / "out",
        service_connection="sc-azure",
        environment="env: prod",
    )
    parsed = yaml.safe_load(files["templates/y2-fleet-run.yml"])
    assert isinstance(parsed, dict)


@pytest.mark.bug
def test_generate_pipeline_files_allows_double_underscore_in_step_name(tmp_path):
    """EP: '__' in a step name is user data, not a leftover template token."""
    repo_dir, job_yaml = _init_git_repo_with_linux_job(tmp_path)
    job = load_job(job_yaml)
    job.invoke.runcommand.steps[0].name = "check__os"
    files = generate_pipeline_files(
        job,
        B_PROFILE,
        repo_dir / "pipelines" / "out",
        service_connection="sc-azure",
        environment="y2-lab",
    )
    assert "check__os" in files["templates/y2-fleet-run.yml"]


@pytest.mark.bug
def test_explain_vm_filter_numbers_selected_vms_from_one(tmp_path):
    """BVA: filtering to the second CSV VM must render 'VM 1/1', not 'VM 2/1'."""
    job = load_job("examples/linux/job.yaml")
    job.output_csv = tmp_path / "result.csv"
    job.output_log = tmp_path / "result.log"
    rows, _, _ = read_input_csv(job.input, job=job)
    selected = [rows[1]]
    md = render_md(build_plan(job, selected, B_PROFILE, payload_sha256=PAYLOAD_SHA))
    assert f"## VM 1/1: {rows[1].vm_name}" in md


@pytest.mark.bug
def test_conformance_keys_mismatches_per_vm_not_just_vm_name():
    """EP: two VMs sharing a vm_name across subscriptions must both count as mismatched."""
    job = load_job("examples/linux/job.yaml")
    row_a = make_vm_row(1, "dup-vm", subscription="Sub A")
    row_b = make_vm_row(2, "dup-vm", subscription="Sub B")
    ctx = PlanContext.from_rows(job, [row_a, row_b], B_PROFILE, payload_sha256=PAYLOAD_SHA)
    recorder = TraceRecorder()
    recorder.expect(1, "invoke")
    recorder.expect(2, "invoke")
    report = evaluate(ctx, [row_a, row_b], recorder, phases_run=["invoke"])
    assert report.matched == 0
