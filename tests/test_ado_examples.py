# tests/test_ado_examples.py - Tests for example pipelines and sample payloads
# Covers: TP-12, TP-11, AC-12, AC-11, INV-5
# Pure ASCII, LF line endings, BOM-less UTF-8, Python 3.9 compatible (stdlib + PyYAML + pytest).

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "ado" / "examples"
PAYLOADS_DIR = EXAMPLES_DIR / "payloads"
TEMPLATES_DIR = REPO_ROOT / "ado" / "templates"

INVOKE_EXAMPLE_PATH = EXAMPLES_DIR / "patch-invoke.yml"
COLLECT_EXAMPLE_PATH = EXAMPLES_DIR / "patch-collect.yml"
SAMPLE_LINUX_PATH = PAYLOADS_DIR / "sample-linux.sh"
SAMPLE_WINDOWS_PATH = PAYLOADS_DIR / "sample-windows.ps1"


def load_yaml(path: Path) -> Any:
    """Load and parse YAML file using PyYAML."""
    with open(path, mode="r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def evaluate_hook_allowlist(step: Dict[str, Any], service_connection: str) -> Tuple[bool, Optional[str]]:
    """Simulate the ADO compile-time allowlist logic from hooks.yml.

    Allowed steps:
      - checkout (any non-empty)
      - download (any non-empty)
      - publish (any non-empty)
      - powershell (any non-empty)
      - task in ('PowerShell@2', 'PublishPipelineArtifact@1', 'DownloadPipelineArtifact@2')
      - task == 'AzurePowerShell@5' and inputs.azureSubscription == serviceConnection

    Returns (is_allowed, error_key_or_none).
    """
    if not isinstance(step, dict):
        return False, "ERROR_DISALLOWED_STEP"

    has_checkout = bool(step.get("checkout"))
    has_download = bool(step.get("download"))
    has_publish = bool(step.get("publish"))
    has_powershell = bool(step.get("powershell"))

    task_name = step.get("task")
    allowed_tasks = {"PowerShell@2", "PublishPipelineArtifact@1", "DownloadPipelineArtifact@2"}
    is_allowed_task = task_name in allowed_tasks

    is_allowed_azure_ps = False
    if task_name == "AzurePowerShell@5":
        inputs = step.get("inputs")
        if isinstance(inputs, dict):
            sub = inputs.get("azureSubscription")
            if sub == service_connection:
                is_allowed_azure_ps = True

    if has_checkout or has_download or has_publish or has_powershell or is_allowed_task or is_allowed_azure_ps:
        return True, None

    return False, "ERROR_DISALLOWED_STEP"


# ==============================================================================
# TP-12: Example Pair & Sample Payloads (AC-12, INV-5)
# Filter: -k "example or payload or invoke or collect"
# ==============================================================================

@pytest.mark.parametrize(
    "path",
    [
        INVOKE_EXAMPLE_PATH,
        COLLECT_EXAMPLE_PATH,
        SAMPLE_LINUX_PATH,
        SAMPLE_WINDOWS_PATH,
    ],
)
def test_example_files_exist_and_ascii(path: Path):
    """TP-12: Example YAML files and payloads exist, are pure ASCII, and use LF line endings."""
    assert path.is_file(), f"Expected file does not exist: {path}"
    raw_bytes = path.read_bytes()
    assert len(raw_bytes) > 0, f"File must not be empty: {path}"

    # BOM-less UTF-8 / ASCII check
    assert not raw_bytes.startswith(b"\xef\xbb\xbf"), f"File must not contain UTF-8 BOM: {path}"
    assert b"\r\n" not in raw_bytes, f"File must have LF line endings, found CRLF: {path}"

    try:
        content = raw_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        pytest.fail(f"File contains non-ASCII characters: {path}: {exc}")

    assert content.isascii(), f"File content is not ASCII: {path}"


def test_example_invoke_structure():
    """TP-12: patch-invoke.yml has trigger: none, root cron schedules, and extends patch-invoke template."""
    doc = load_yaml(INVOKE_EXAMPLE_PATH)
    assert isinstance(doc, dict), "Root of patch-invoke.yml must be a mapping"

    # trigger: none
    assert doc.get("trigger") == "none", "patch-invoke.yml must declare 'trigger: none'"

    # schedules: cron block present
    assert "schedules" in doc, "patch-invoke.yml must define a root 'schedules:' block"
    schedules = doc["schedules"]
    assert isinstance(schedules, list) and len(schedules) > 0, "schedules must be a non-empty list"

    sched = schedules[0]
    assert "cron" in sched, "Schedule entry must have 'cron' field"
    cron_str = str(sched["cron"]).strip()
    # Basic 5-segment cron format check (minute hour day month day-of-week)
    cron_parts = cron_str.split()
    assert len(cron_parts) == 5, f"Expected 5 cron segments, got: {cron_str}"

    assert "displayName" in sched, "Schedule entry should have 'displayName'"
    assert "branches" in sched and isinstance(sched["branches"], dict), "Schedule entry must define 'branches'"
    assert "include" in sched["branches"], "Schedule branches must specify 'include'"
    assert "main" in sched["branches"]["include"], "Schedule should target 'main' branch"
    assert sched.get("always") is True, "Schedule should set 'always: true'"

    # extends template
    assert "extends" in doc, "patch-invoke.yml must have 'extends:' section"
    extends = doc["extends"]
    assert isinstance(extends, dict)
    template_ref = extends.get("template", "")
    assert template_ref == "../templates/patch-invoke.yml", (
        f"Expected extends template '../templates/patch-invoke.yml', got '{template_ref}'"
    )

    # parameters check
    params = extends.get("parameters", {})
    assert params.get("serviceConnection") == "sc-azure-lab"
    assert "microsoft.compute/virtualmachines" in params.get("targetsKql", "").lower()
    assert params.get("osType") == "windows"
    assert params.get("payloadPath") == "ado/examples/payloads/sample-windows.ps1"
    assert params.get("maxJobs") == 20
    assert params.get("pollSeconds") == 15


def test_example_invoke_sample_hooks_pass_allowlist():
    """TP-12: Sample hooks in patch-invoke.yml pass the hook allowlist and verify hook patterns."""
    doc = load_yaml(INVOKE_EXAMPLE_PATH)
    params = doc.get("extends", {}).get("parameters", {})
    sc = params.get("serviceConnection", "sc-azure-lab")

    # before_start is empty
    assert params.get("before_start") == []

    # after_run hooks
    after_run = params.get("after_run", [])
    assert isinstance(after_run, list) and len(after_run) > 0, "patch-invoke.yml should have after_run sample hook"
    for step in after_run:
        allowed, err = evaluate_hook_allowlist(step, sc)
        assert allowed, f"after_run step rejected by allowlist: {step} ({err})"

    # after_run step uses $(Y2_RUN_ID)
    after_run_raw = str(after_run)
    assert "$(Y2_RUN_ID)" in after_run_raw or "$env:Y2_RUN_ID" in after_run_raw, (
        "after_run hook should reference Y2_RUN_ID"
    )

    # after_restore hooks
    after_restore = params.get("after_restore", [])
    assert isinstance(after_restore, list) and len(after_restore) > 0, "patch-invoke.yml should have after_restore hook"
    for step in after_restore:
        allowed, err = evaluate_hook_allowlist(step, sc)
        assert allowed, f"after_restore step rejected by allowlist: {step} ({err})"
        # Spec requirement: Hooks intending to run after errors or cancellation specify their own condition
        assert step.get("condition") == "succeededOrFailed()", (
            f"Expected after_restore sample hook to specify 'condition: succeededOrFailed()', got: {step.get('condition')}"
        )


def test_example_collect_structure():
    """TP-12: patch-collect.yml has trigger: none, no schedules block, and extends patch-collect template."""
    doc = load_yaml(COLLECT_EXAMPLE_PATH)
    assert isinstance(doc, dict), "Root of patch-collect.yml must be a mapping"

    # trigger: none
    assert doc.get("trigger") == "none", "patch-collect.yml must declare 'trigger: none'"

    # no schedules block
    assert "schedules" not in doc or doc["schedules"] is None or doc["schedules"] == [], (
        "patch-collect.yml must NOT define a schedules block"
    )

    # extends template
    assert "extends" in doc, "patch-collect.yml must have 'extends:' section"
    extends = doc["extends"]
    assert isinstance(extends, dict)
    template_ref = extends.get("template", "")
    assert template_ref == "../templates/patch-collect.yml", (
        f"Expected extends template '../templates/patch-collect.yml', got '{template_ref}'"
    )

    # parameters check
    params = extends.get("parameters", {})
    assert params.get("serviceConnection") == "sc-azure-lab"
    assert "microsoft.compute/virtualmachines" in params.get("targetsKql", "").lower()
    assert params.get("osType") == "linux"
    assert params.get("payloadPath") == "ado/examples/payloads/sample-linux.sh"
    assert params.get("maxJobs") == 20
    assert params.get("pollSeconds") == 15


def test_example_collect_sample_hooks_pass_allowlist_and_read_report_dir():
    """TP-12: Sample hooks in patch-collect.yml pass allowlist and read Y2_REPORT_DIR."""
    doc = load_yaml(COLLECT_EXAMPLE_PATH)
    params = doc.get("extends", {}).get("parameters", {})
    sc = params.get("serviceConnection", "sc-azure-lab")

    # before_start and after_run are empty
    assert params.get("before_start") == []
    assert params.get("after_run") == []

    # after_restore hooks
    after_restore = params.get("after_restore", [])
    assert isinstance(after_restore, list) and len(after_restore) > 0, "patch-collect.yml should have after_restore hook"
    for step in after_restore:
        allowed, err = evaluate_hook_allowlist(step, sc)
        assert allowed, f"after_restore step rejected by allowlist: {step} ({err})"

    # after_restore hook script reads $env:Y2_REPORT_DIR or $(Y2_REPORT_DIR)
    script_texts = []
    for step in after_restore:
        inputs = step.get("inputs", {})
        if isinstance(inputs, dict) and "script" in inputs:
            script_texts.append(inputs["script"])

    combined_scripts = "\n".join(script_texts)
    has_report_dir_ref = (
        "$env:Y2_REPORT_DIR" in combined_scripts
        or "$(Y2_REPORT_DIR)" in combined_scripts
    )
    assert has_report_dir_ref, (
        f"patch-collect.yml after_restore hook must read $env:Y2_REPORT_DIR or $(Y2_REPORT_DIR). Script:\n{combined_scripts}"
    )


# ==============================================================================
# Sample Payloads Hygiene & Secret Scanning (INV-5)
# Filter: -k "payload or secret"
# ==============================================================================

FORBIDDEN_SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN[ A-Z0-9_-]*PRIVATE KEY-----"), "Private Key Header"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"), "JWT Bearer Token"),
    (re.compile(r"(?i)(password|secret|client_secret|ado_pat)\s*[:=]\s*[\"'][^\"']+[\"']"), "Hardcoded Credential Assignment"),
    (re.compile(r"(?i)sig=[A-Za-z0-9%]{20,}"), "Storage SAS Signature"),
]


@pytest.mark.parametrize("payload_path", [SAMPLE_LINUX_PATH, SAMPLE_WINDOWS_PATH])
def test_sample_payloads_hygiene_and_no_secrets(payload_path: Path):
    """TP-12 / INV-5: Sample payloads exist, are non-empty, and contain zero hardcoded secrets."""
    assert payload_path.is_file(), f"Payload missing: {payload_path}"
    content = payload_path.read_text(encoding="ascii")
    assert len(content.strip()) > 0, f"Payload is empty: {payload_path}"

    for pattern, desc in FORBIDDEN_SECRET_PATTERNS:
        match = pattern.search(content)
        assert not match, (
            f"INV-5 Violation: Found forbidden secret pattern ({desc}) in {payload_path.name}: {match.group(0)}"
        )


def test_sample_linux_payload_syntax_and_shebang():
    """TP-12: sample-linux.sh has valid shebang, set -euo pipefail, and exits 0."""
    content = SAMPLE_LINUX_PATH.read_text(encoding="ascii")
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    assert lines[0] in {"#!/usr/bin/env bash", "#!/bin/bash"}, (
        f"sample-linux.sh must start with bash shebang, got '{lines[0]}'"
    )
    assert any("set -euo pipefail" in line for line in lines), (
        "sample-linux.sh must include 'set -euo pipefail'"
    )
    assert any(line == "exit 0" for line in lines), "sample-linux.sh must terminate with 'exit 0'"


def test_sample_windows_payload_syntax_and_binding():
    """TP-12: sample-windows.ps1 uses [CmdletBinding()] and param() with exit 0."""
    content = SAMPLE_WINDOWS_PATH.read_text(encoding="ascii")
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    assert any("[cmdletbinding()]" in line.lower() for line in lines), (
        "sample-windows.ps1 must include [CmdletBinding()]"
    )
    assert any("param()" in line.lower() for line in lines), (
        "sample-windows.ps1 must include param()"
    )
    assert any(line.lower() == "exit 0" for line in lines), (
        "sample-windows.ps1 must terminate with 'exit 0'"
    )


# ==============================================================================
# TP-11: Template Location Independence in Examples (AC-11)
# Filter: -k "locate or templatesRepo or location"
# ==============================================================================

@pytest.mark.parametrize("example_path", [INVOKE_EXAMPLE_PATH, COLLECT_EXAMPLE_PATH])
def test_example_location_independence_comment_block(example_path: Path):
    """TP-11: Example pipelines document separate repository resource configuration with templatesRepo."""
    raw_text = example_path.read_text(encoding="ascii")

    # Documented separate repository resource block
    assert "resources:" in raw_text, (
        f"{example_path.name} must document 'resources:' in comment block"
    )
    assert "repositories:" in raw_text, (
        f"{example_path.name} must document 'repositories:' in comment block"
    )
    assert "templatesRepo" in raw_text, (
        f"{example_path.name} must document 'templatesRepo' parameter in comment block"
    )
    assert "@patch_templates" in raw_text or "@<repo>" in raw_text, (
        f"{example_path.name} must show '@repository' template reference syntax in comment block"
    )


@pytest.mark.parametrize("example_path", [INVOKE_EXAMPLE_PATH, COLLECT_EXAMPLE_PATH])
def test_example_parameters_conform_to_template_schema(example_path: Path):
    """TP-12: Example parameters conform to the exact parameter schema declared by the templates."""
    doc = load_yaml(example_path)
    extends = doc.get("extends", {})
    template_rel = extends.get("template")
    template_path = (example_path.parent / template_rel).resolve()
    assert template_path.is_file(), f"Target template file does not exist: {template_path}"

    template_doc = load_yaml(template_path)
    allowed_param_names = {p["name"] for p in template_doc.get("parameters", [])}

    example_params = extends.get("parameters", {})
    for param_name in example_params.keys():
        assert param_name in allowed_param_names, (
            f"Example {example_path.name} passes unknown parameter '{param_name}' to {template_path.name}"
        )
