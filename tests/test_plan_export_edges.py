"""Adversarial edge-case tests for plan rendering, export, and compilers.

Every test asserts one intended behavior of ``plan``, ``explain``,
``conformance``, ``export_ado``, and the ``y2b``/``y2p`` compilers. Tests
marked ``bug`` reproduce a confirmed defect.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from y2b.compiler import _clean_script_body, compile_job as compile_bash
from y2b.profile import PROFILE as B_PROFILE
from y2p.compiler import compile_job as compile_ps
from y2p.compiler import ps_quote
from y2p.profile import PROFILE as P_PROFILE
from y2shared.conformance import ConformanceReport, format_summary
from y2shared.config import load_job
from y2shared.explain import render_json, render_md, render_text
from y2shared.export_ado import format_header_comment, sanitize_one_line, validate_safe_token
from y2shared.inputcsv import VMRow, read_input_csv
from y2shared.plan import PlanContext, build_plan, format_script_display

PAYLOAD_SHA = "0" * 64


def make_row(index: int, vm_name: str, subscription: str) -> VMRow:
    return VMRow(
        index=index,
        vm_name=vm_name,
        resource_group="rg-1",
        subscription=subscription,
        os="linux",
        raw_data={"vm_name": vm_name},
        env={"VM_NAME": vm_name},
    )


def load_linux_plan(tmp_path: Path):
    job = load_job("examples/linux/job.yaml")
    job.output_csv = tmp_path / "result.csv"
    job.output_log = tmp_path / "result.log"
    rows, _, _ = read_input_csv(job.input, job=job)
    return job, rows, build_plan(job, rows, B_PROFILE, payload_sha256=PAYLOAD_SHA)


# ---------------------------------------------------------------------------
# plan.py
# ---------------------------------------------------------------------------


def test_plan_context_groups_duplicate_subscriptions():
    """branch: the first VM owns the subscription; later VMs are listed as shared."""
    job = load_job("examples/linux/job.yaml")
    rows = [
        make_row(1, "vm-a", "Sub One"),
        make_row(2, "vm-b", "Sub One"),
        make_row(3, "vm-c", "Sub Two"),
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


def test_plan_contains_payload_hash_and_api_version(tmp_path):
    """EP: the structured plan exposes the supplied payload hash and the pinned API version."""
    _, _, plan = load_linux_plan(tmp_path)
    assert plan["schema"] == "y2-plan/1"
    assert plan["api_version"] == "2024-07-01"
    assert plan["fleet"]["payload"]["sha256"] == PAYLOAD_SHA
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


def test_validate_safe_token_allows_spaces_only_when_requested():
    """EP: allow_spaces opens exactly the space character, nothing else."""
    validate_safe_token("a b", "name", allow_spaces=True)
    with pytest.raises(ValueError):
        validate_safe_token("a b", "name", allow_spaces=False)


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


def test_clean_script_body_strips_shebang_and_keeps_placeholders():
    """branch: a file body loses its shebang; a comments-only body becomes ':'."""
    assert _clean_script_body("#!/bin/bash\necho hi\n", is_file=True) == "echo hi"
    assert _clean_script_body("# only a comment", is_file=False) == "# only a comment\n:"


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
