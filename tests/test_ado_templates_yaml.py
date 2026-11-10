# tests/test_ado_templates_yaml.py - Tier 1 YAML structure tests for ADO patch templates
# Covers: TP-01, TP-06, TP-07, TP-08, TP-09, TP-11 (AC-1, AC-6, AC-7, AC-8, AC-9, AC-11)
# Pure ASCII, LF line endings, BOM-less UTF-8, Python 3.9 compatible (stdlib + PyYAML + pytest).

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "ado" / "templates"
STEPS_DIR = TEMPLATES_DIR / "steps"
SCRIPTS_DIR = REPO_ROOT / "ado" / "scripts"
FIXTURES_DIR = REPO_ROOT / "tests" / "ado_fixtures"

INVOKE_TEMPLATE_PATH = TEMPLATES_DIR / "patch-invoke.yml"
COLLECT_TEMPLATE_PATH = TEMPLATES_DIR / "patch-collect.yml"
HOOKS_DISALLOWED_PATH = FIXTURES_DIR / "hooks_disallowed.yml"


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
# TP-01: Typed Template Interface & Literal Pool (AC-1)
# Filter: -k "params or pool"
# ==============================================================================

EXPECTED_PARAMS: Dict[str, Dict[str, Any]] = {
    "serviceConnection": {"type": "string", "has_default": False},
    "targetsKql": {"type": "string", "has_default": False},
    "osType": {"type": "string", "has_default": False, "values": ["windows", "linux"]},
    "payloadPath": {"type": "string", "has_default": False},
    "maxJobs": {"type": "number", "has_default": True, "default": 20},
    "pollSeconds": {"type": "number", "has_default": True, "default": 15},
    "templatesRepo": {"type": "string", "has_default": True, "default": "self"},
    "before_start": {"type": "stepList", "has_default": True, "default": []},
    "after_run": {"type": "stepList", "has_default": True, "default": []},
    "after_restore": {"type": "stepList", "has_default": True, "default": []},
}


@pytest.mark.parametrize("template_path", [INVOKE_TEMPLATE_PATH, COLLECT_TEMPLATE_PATH])
def test_entry_templates_exact_ten_params(template_path: Path):
    """TP-01: Entry templates declare exactly 10 typed parameters with correct types and defaults."""
    doc = load_yaml(template_path)
    assert "parameters" in doc, f"Template {template_path.name} missing 'parameters' section"
    params = doc["parameters"]
    assert isinstance(params, list), f"'parameters' must be a list in {template_path.name}"
    assert len(params) == 10, f"Expected exactly 10 parameters in {template_path.name}, got {len(params)}"

    param_map = {p["name"]: p for p in params}
    assert set(param_map.keys()) == set(EXPECTED_PARAMS.keys()), (
        f"Mismatch in parameter names for {template_path.name}. "
        f"Diff: {set(param_map.keys()) ^ set(EXPECTED_PARAMS.keys())}"
    )

    for name, expected in EXPECTED_PARAMS.items():
        actual = param_map[name]
        assert actual.get("type") == expected["type"], (
            f"Param '{name}' in {template_path.name} expected type '{expected['type']}', got '{actual.get('type')}'"
        )
        if expected["has_default"]:
            assert "default" in actual, f"Param '{name}' in {template_path.name} should have a default value"
            assert actual["default"] == expected["default"], (
                f"Param '{name}' in {template_path.name} default mismatch: expected {expected['default']}, got {actual['default']}"
            )
        else:
            assert "default" not in actual, f"Required param '{name}' in {template_path.name} should not have default"

        if "values" in expected:
            assert "values" in actual, f"Param '{name}' in {template_path.name} should declare allowed values"
            assert sorted(actual["values"]) == sorted(expected["values"]), (
                f"Param '{name}' in {template_path.name} allowed values mismatch: {actual['values']}"
            )


@pytest.mark.parametrize("template_path", [INVOKE_TEMPLATE_PATH, COLLECT_TEMPLATE_PATH])
def test_entry_templates_no_pool_param(template_path: Path):
    """TP-01: Entry templates do NOT declare a 'pool' or 'throttleMs' parameter."""
    doc = load_yaml(template_path)
    param_names = {p["name"] for p in doc.get("parameters", [])}
    assert "pool" not in param_names, f"'pool' must not be a parameter in {template_path.name}"
    assert "throttleMs" not in param_names, f"'throttleMs' must not be a parameter in {template_path.name}"


@pytest.mark.parametrize("template_path,expected_job_name", [
    (INVOKE_TEMPLATE_PATH, "PatchInvoke"),
    (COLLECT_TEMPLATE_PATH, "PatchCollect"),
])
def test_entry_templates_literal_pool_and_single_job(template_path: Path, expected_job_name: str):
    """TP-01: Templates define a single job with literal pool 'Production Windows Agent' and timeouts."""
    doc = load_yaml(template_path)
    assert "jobs" in doc, f"Missing 'jobs' section in {template_path.name}"
    jobs = doc["jobs"]
    assert isinstance(jobs, list), f"'jobs' must be a list in {template_path.name}"
    assert len(jobs) == 1, f"Expected exactly 1 job in {template_path.name}, got {len(jobs)}"

    job = jobs[0]
    assert job.get("job") == expected_job_name, (
        f"Expected job name '{expected_job_name}' in {template_path.name}, got '{job.get('job')}'"
    )

    # Validate pool
    assert "pool" in job, f"Job in {template_path.name} must declare 'pool'"
    pool = job["pool"]
    assert isinstance(pool, dict), f"Pool must be a mapping in {template_path.name}"
    assert pool.get("name") == "Production Windows Agent", (
        f"Literal pool name 'Production Windows Agent' expected in {template_path.name}, got '{pool.get('name')}'"
    )

    # Validate timeouts
    assert job.get("timeoutInMinutes") == 360, (
        f"Expected timeoutInMinutes 360 in {template_path.name}, got {job.get('timeoutInMinutes')}"
    )
    assert job.get("cancelTimeoutInMinutes") == 10, (
        f"Expected cancelTimeoutInMinutes 10 in {template_path.name}, got {job.get('cancelTimeoutInMinutes')}"
    )


# ==============================================================================
# TP-06: Safe Restore, Job Cancel, and Always Conditions (AC-6)
# Filter: -k "restore or always or cancel"
# ==============================================================================

@pytest.mark.parametrize("template_path", [INVOKE_TEMPLATE_PATH, COLLECT_TEMPLATE_PATH])
def test_safe_restore_cancel_timeout_in_minutes_at_least_ten(template_path: Path):
    """TP-06: cancelTimeoutInMinutes >= 10 in entry templates to allow clean deallocation."""
    doc = load_yaml(template_path)
    job = doc["jobs"][0]
    cancel_timeout = job.get("cancelTimeoutInMinutes", 0)
    assert cancel_timeout >= 10, (
        f"cancelTimeoutInMinutes must be >= 10 in {template_path.name}, got {cancel_timeout}"
    )


def test_safe_restore_condition_always_in_power_restore():
    """TP-06: power-restore.yml step carries condition: always()."""
    doc = load_yaml(STEPS_DIR / "power-restore.yml")
    steps = doc.get("steps", [])
    assert len(steps) >= 1, "power-restore.yml must have at least one step"
    azure_ps_step = next((s for s in steps if s.get("task") == "AzurePowerShell@5"), None)
    assert azure_ps_step is not None, "power-restore.yml must contain an AzurePowerShell@5 step"
    assert azure_ps_step.get("condition") == "always()", (
        f"power-restore step must have condition: always(), got '{azure_ps_step.get('condition')}'"
    )


def test_safe_restore_condition_always_in_finalize():
    """TP-06: finalize.yml step carries condition: always()."""
    doc = load_yaml(STEPS_DIR / "finalize.yml")
    steps = doc.get("steps", [])
    assert len(steps) >= 1, "finalize.yml must have at least one step"
    finalize_step = next((s for s in steps if s.get("task") == "AzurePowerShell@5"), None)
    assert finalize_step is not None, "finalize.yml must contain an AzurePowerShell@5 step"
    assert finalize_step.get("condition") == "always()", (
        f"finalize step must have condition: always(), got '{finalize_step.get('condition')}'"
    )


@pytest.mark.parametrize("template_path", [INVOKE_TEMPLATE_PATH, COLLECT_TEMPLATE_PATH])
def test_safe_restore_condition_always_in_publish_artifact(template_path: Path):
    """TP-06: PublishPipelineArtifact@1 step in entry templates carries condition: always()."""
    doc = load_yaml(template_path)
    steps = doc["jobs"][0]["steps"]
    publish_step = next((s for s in steps if isinstance(s, dict) and s.get("task") == "PublishPipelineArtifact@1"), None)
    assert publish_step is not None, f"PublishPipelineArtifact@1 step not found in {template_path.name}"
    assert publish_step.get("condition") == "always()", (
        f"PublishPipelineArtifact@1 in {template_path.name} must have condition: always(), got '{publish_step.get('condition')}'"
    )


# ==============================================================================
# TP-07: Hook Allowlist Logic & Rejection (AC-7)
# Filter: -k "hooks or allowlist"
# ==============================================================================

def test_hooks_template_parameters():
    """TP-07: hooks.yml declares hook (stepList), serviceConnection (string), hookName (string)."""
    doc = load_yaml(STEPS_DIR / "hooks.yml")
    params = doc.get("parameters", [])
    param_map = {p["name"]: p for p in params}

    assert "hook" in param_map, "hooks.yml missing 'hook' parameter"
    assert param_map["hook"]["type"] == "stepList"
    assert param_map["hook"]["default"] == []

    assert "serviceConnection" in param_map, "hooks.yml missing 'serviceConnection' parameter"
    assert param_map["serviceConnection"]["type"] == "string"

    assert "hookName" in param_map, "hooks.yml missing 'hookName' parameter"
    assert param_map["hookName"]["type"] == "string"


def test_hooks_allowlist_structural_branches():
    """TP-07: hooks.yml contains loop, allow branch emitting verbatim, and else branch emitting ERROR_DISALLOWED_STEP."""
    doc = load_yaml(STEPS_DIR / "hooks.yml")
    steps = doc.get("steps", [])
    assert len(steps) == 1, "hooks.yml steps should contain exactly the loop construct"

    loop_dict = steps[0]
    loop_key = "${{ each step in parameters.hook }}"
    assert loop_key in loop_dict, f"Expected loop key '{loop_key}' in hooks.yml"

    branches = loop_dict[loop_key]
    assert isinstance(branches, list), "Loop body must be a list of conditional branches"
    assert len(branches) == 2, f"Expected if and else branches, got {len(branches)}"

    # 1. Allowed branch
    if_branch = branches[0]
    if_key = next(k for k in if_branch.keys() if k.startswith("${{ if "))
    assert "ne(step.checkout, '')" in if_key
    assert "ne(step.download, '')" in if_key
    assert "ne(step.publish, '')" in if_key
    assert "ne(step.powershell, '')" in if_key
    assert "'PowerShell@2'" in if_key
    assert "'PublishPipelineArtifact@1'" in if_key
    assert "'DownloadPipelineArtifact@2'" in if_key
    assert "eq(step.task, 'AzurePowerShell@5')" in if_key
    assert "eq(step.inputs.azureSubscription, parameters.serviceConnection)" in if_key

    # Verbatim emission: - ${{ step }} without wrapping or conditions
    assert if_branch[if_key] == ["${{ step }}"], (
        f"Allowed step must be emitted verbatim as ['${{{{ step }}}}'], got {if_branch[if_key]}"
    )

    # 2. Else branch
    else_branch = branches[1]
    assert "${{ else }}" in else_branch, "Missing ${{ else }} branch in hooks.yml"
    else_steps = else_branch["${{ else }}"]
    assert len(else_steps) == 1
    error_step = else_steps[0]
    assert isinstance(error_step, dict)

    error_task_key = next(iter(error_step.keys()))
    assert error_task_key.startswith("ERROR_DISALLOWED_STEP_${{ parameters.hookName }}"), (
        f"Invalid task name in else branch: {error_task_key}"
    )

    error_msg = error_step[error_task_key]
    assert "${{ parameters.hookName }}" in error_msg
    assert "disallowed step" in error_msg
    assert "Allowed steps: checkout, download, publish, powershell" in error_msg
    assert "AzurePowerShell@5 (with matching serviceConnection)" in error_msg


def test_hooks_allowlist_rejects_disallowed_fixture_items():
    """TP-07: All steps in tests/ado_fixtures/hooks_disallowed.yml fail the allowlist and route to ERROR_DISALLOWED_STEP."""
    assert HOOKS_DISALLOWED_PATH.is_file(), f"Disallowed steps fixture not found at {HOOKS_DISALLOWED_PATH}"
    disallowed_steps = load_yaml(HOOKS_DISALLOWED_PATH)
    assert isinstance(disallowed_steps, list), "hooks_disallowed.yml must be a list of step mappings"
    assert len(disallowed_steps) >= 10, f"Expected at least 10 disallowed steps in fixture, got {len(disallowed_steps)}"

    service_connection = "prod-arm-connection"

    for idx, step in enumerate(disallowed_steps):
        is_allowed, error_key = evaluate_hook_allowlist(step, service_connection)
        desc = step.get("displayName") or step.get("task") or list(step.keys())[0]
        assert not is_allowed, (
            f"Step #{idx} ('{desc}') was incorrectly allowed! Content: {step}"
        )
        assert error_key == "ERROR_DISALLOWED_STEP", (
            f"Step #{idx} ('{desc}') should route to ERROR_DISALLOWED_STEP"
        )


def test_hooks_allowlist_accepts_authorized_steps():
    """TP-07: Authorized steps pass the allowlist and are emitted verbatim."""
    service_connection = "prod-arm-connection"
    authorized_steps = [
        {"checkout": "self"},
        {"checkout": "templatesRepo"},
        {"checkout": "none"},
        {"download": "current"},
        {"publish": "$(Pipeline.Workspace)/drop"},
        {"powershell": "Write-Host 'hello from hook'"},
        {
            "task": "PowerShell@2",
            "inputs": {
                "targetType": "inline",
                "script": "Write-Host 'pwsh v2 hook'",
            },
        },
        {
            "task": "PublishPipelineArtifact@1",
            "inputs": {
                "targetPath": "$(Pipeline.Workspace)/report",
                "artifact": "custom-artifact",
            },
        },
        {
            "task": "DownloadPipelineArtifact@2",
            "inputs": {
                "artifact": "custom-artifact",
                "path": "$(Pipeline.Workspace)",
            },
        },
        {
            "task": "AzurePowerShell@5",
            "inputs": {
                "azureSubscription": service_connection,
                "ScriptType": "InlineScript",
                "Inline": "Get-AzContext",
                "azurePowerShellVersion": "LatestVersion",
                "pwsh": False,
            },
        },
    ]

    for idx, step in enumerate(authorized_steps):
        is_allowed, error_key = evaluate_hook_allowlist(step, service_connection)
        desc = step.get("task") or list(step.keys())[0]
        assert is_allowed, f"Authorized step #{idx} ('{desc}') was rejected: {step}"
        assert error_key is None


@pytest.mark.parametrize("template_path", [INVOKE_TEMPLATE_PATH, COLLECT_TEMPLATE_PATH])
def test_hooks_emitted_without_conditions_in_entry_templates(template_path: Path):
    """TP-07: User hooks (before_start, after_run, after_restore) have no condition added by templates."""
    doc = load_yaml(template_path)
    steps = doc["jobs"][0]["steps"]

    hook_steps = [
        s for s in steps
        if isinstance(s, dict) and s.get("template") == "steps/hooks.yml"
    ]
    assert len(hook_steps) == 3, f"Expected 3 hook invocations in {template_path.name}, got {len(hook_steps)}"

    hook_names = {s["parameters"]["hookName"] for s in hook_steps}
    assert hook_names == {"before_start", "after_run", "after_restore"}

    for s in hook_steps:
        assert "condition" not in s, (
            f"Hook step '{s['parameters']['hookName']}' in {template_path.name} must not have a condition added"
        )


# ==============================================================================
# TP-08: Hook Variables Export (AC-8)
# Filter: -k "hookvars"
# ==============================================================================

def test_hookvars_exported_by_locate_and_resolve():
    """TP-08: locate.yml and resolve.ps1 export Y2_TARGETS_CSV, Y2_REPORT_DIR, and Y2_RUN_ID via task.setvariable."""
    locate_doc = load_yaml(STEPS_DIR / "locate.yml")
    locate_steps = locate_doc.get("steps", [])
    assert len(locate_steps) >= 1
    locate_script = locate_steps[0].get("powershell", "")

    # Locate exports report dir and run id
    assert "##vso[task.setvariable variable=Y2_REPORT_DIR]" in locate_script
    assert "##vso[task.setvariable variable=Y2_RUN_ID]" in locate_script
    assert "##vso[task.setvariable variable=Y2_SCRIPTS_DIR]" in locate_script
    assert "##vso[task.setvariable variable=Y2_PAYLOAD]" in locate_script
    assert "##vso[task.setvariable variable=Y2_WORK_DIR]" in locate_script

    # Resolve exports targets CSV, report dir, and run ID
    resolve_ps1 = SCRIPTS_DIR / "resolve.ps1"
    assert resolve_ps1.is_file(), f"resolve.ps1 missing at {resolve_ps1}"
    resolve_content = resolve_ps1.read_text(encoding="utf-8")
    assert "##vso[task.setvariable variable=Y2_TARGETS_CSV]" in resolve_content
    assert "##vso[task.setvariable variable=Y2_REPORT_DIR]" in resolve_content
    assert "##vso[task.setvariable variable=Y2_RUN_ID]" in resolve_content


@pytest.mark.parametrize("template_path", [INVOKE_TEMPLATE_PATH, COLLECT_TEMPLATE_PATH])
def test_hookvars_not_overridden_in_templates(template_path: Path):
    """TP-08: Hook templates and entry steps do not overwrite or clear Y2_TARGETS_CSV, Y2_REPORT_DIR, Y2_RUN_ID."""
    raw_text = template_path.read_text(encoding="utf-8")
    for var in ["Y2_TARGETS_CSV", "Y2_REPORT_DIR", "Y2_RUN_ID"]:
        # Should not set these variables directly in entry templates
        pattern = rf"setvariable\s+variable={var}"
        assert not re.search(pattern, raw_text), f"Entry template {template_path.name} unexpectedly overrides {var}"


# ==============================================================================
# TP-11: Template Location Independence (AC-11)
# Filter: -k "locate or templatesRepo or location"
# ==============================================================================

@pytest.mark.parametrize("template_path", [INVOKE_TEMPLATE_PATH, COLLECT_TEMPLATE_PATH])
def test_templatesRepo_conditional_checkout_and_self_always(template_path: Path):
    """TP-11: checkout: self is always present; templatesRepo checkout is conditional on != 'self'."""
    doc = load_yaml(template_path)
    steps = doc["jobs"][0]["steps"]

    # Step 0 must be checkout: self
    assert steps[0] == {"checkout": "self"}, f"First step in {template_path.name} must be checkout: self"

    # Step 1 must be conditional checkout: ${{ parameters.templatesRepo }}
    cond_step = steps[1]
    assert isinstance(cond_step, dict)
    cond_key = "${{ if ne(parameters.templatesRepo, 'self') }}"
    assert cond_key in cond_step, f"Expected conditional checkout key '{cond_key}' in {template_path.name}"
    inner_steps = cond_step[cond_key]
    assert inner_steps == [{"checkout": "${{ parameters.templatesRepo }}"}], (
        f"Expected checkout of templatesRepo in {template_path.name}, got {inner_steps}"
    )


def test_locate_step_inline_powershell_computes_paths():
    """TP-11: locate.yml is inline powershell that computes Y2_SCRIPTS_DIR and Y2_PAYLOAD for both layouts."""
    locate_doc = load_yaml(STEPS_DIR / "locate.yml")
    params = {p["name"]: p for p in locate_doc.get("parameters", [])}
    assert "templatesRepo" in params
    assert params["templatesRepo"]["default"] == "self"
    assert "payloadPath" in params

    steps = locate_doc.get("steps", [])
    assert len(steps) == 1
    step = steps[0]
    assert "powershell" in step, "locate.yml must use inline powershell (no external script)"

    ps_code = step["powershell"]
    # Validates computation of scriptsDir for self vs remote repo
    assert "$templatesRepo -eq 'self'" in ps_code
    assert "$sourcesDir/ado/scripts" in ps_code
    assert "$sourcesDir/$templatesRepo/ado/scripts" in ps_code
    # Validates computation of payloadPath (handling rooted vs relative)
    assert "[System.IO.Path]::IsPathRooted($payloadPath)" in ps_code
    assert "$sourcesDir/$payloadPath" in ps_code


@pytest.mark.parametrize("template_path", [
    INVOKE_TEMPLATE_PATH,
    COLLECT_TEMPLATE_PATH,
    STEPS_DIR / "locate.yml",
    STEPS_DIR / "resolve.yml",
    STEPS_DIR / "power-start.yml",
    STEPS_DIR / "run-command.yml",
    STEPS_DIR / "power-restore.yml",
    STEPS_DIR / "finalize.yml",
    STEPS_DIR / "hooks.yml",
])
def test_location_no_hardcoded_repo_in_templates(template_path: Path):
    """TP-11: No hardcoded repository name or git URL exists in any template."""
    content = template_path.read_text(encoding="utf-8")
    # Must not contain hardcoded repository aliases other than 'self' or parameter references
    assert "github.com" not in content.lower()
    assert "dev.azure.com" not in content.lower()


# ==============================================================================
# Step Sequence, Artifacts, and Script References (TP-01, TP-09, AC-9)
# ==============================================================================

EXPECTED_STEP_SEQUENCE = [
    "checkout: self",
    "conditional checkout templatesRepo",
    "template: steps/locate.yml",
    "template: steps/resolve.yml",
    "template: steps/hooks.yml (before_start)",
    "template: steps/power-start.yml",
    "template: steps/run-command.yml",
    "template: steps/hooks.yml (after_run)",
    "template: steps/power-restore.yml",
    "template: steps/finalize.yml",
    "task: PublishPipelineArtifact@1",
    "template: steps/hooks.yml (after_restore)",
]


@pytest.mark.parametrize("template_path", [INVOKE_TEMPLATE_PATH, COLLECT_TEMPLATE_PATH])
def test_step_sequence_order(template_path: Path):
    """Verify exact sequence of 12 steps in patch-invoke.yml and patch-collect.yml."""
    doc = load_yaml(template_path)
    steps = doc["jobs"][0]["steps"]
    assert len(steps) == 12, f"Expected exactly 12 steps in {template_path.name}, got {len(steps)}"

    actual_sequence = []
    for s in steps:
        if isinstance(s, dict):
            if s.get("checkout") == "self":
                actual_sequence.append("checkout: self")
            elif "${{ if ne(parameters.templatesRepo, 'self') }}" in s:
                actual_sequence.append("conditional checkout templatesRepo")
            elif s.get("template") == "steps/locate.yml":
                actual_sequence.append("template: steps/locate.yml")
            elif s.get("template") == "steps/resolve.yml":
                actual_sequence.append("template: steps/resolve.yml")
            elif s.get("template") == "steps/power-start.yml":
                actual_sequence.append("template: steps/power-start.yml")
            elif s.get("template") == "steps/run-command.yml":
                actual_sequence.append("template: steps/run-command.yml")
            elif s.get("template") == "steps/power-restore.yml":
                actual_sequence.append("template: steps/power-restore.yml")
            elif s.get("template") == "steps/finalize.yml":
                actual_sequence.append("template: steps/finalize.yml")
            elif s.get("task") == "PublishPipelineArtifact@1":
                actual_sequence.append("task: PublishPipelineArtifact@1")
            elif s.get("template") == "steps/hooks.yml":
                hook_name = s.get("parameters", {}).get("hookName", "")
                actual_sequence.append(f"template: steps/hooks.yml ({hook_name})")
            else:
                actual_sequence.append(str(s))

    assert actual_sequence == EXPECTED_STEP_SEQUENCE, (
        f"Step sequence mismatch in {template_path.name}:\n"
        f"Expected: {EXPECTED_STEP_SEQUENCE}\n"
        f"Actual:   {actual_sequence}"
    )


def test_artifact_names_invoke_and_collect():
    """TP-09: Artifact names are 'y2-report-invoke' and 'y2-report-collect' targeting $(Y2_REPORT_DIR)."""
    invoke_doc = load_yaml(INVOKE_TEMPLATE_PATH)
    invoke_pub = next(s for s in invoke_doc["jobs"][0]["steps"] if isinstance(s, dict) and s.get("task") == "PublishPipelineArtifact@1")
    assert invoke_pub["inputs"]["artifact"] == "y2-report-invoke"
    assert invoke_pub["inputs"]["targetPath"] == "$(Y2_REPORT_DIR)"

    collect_doc = load_yaml(COLLECT_TEMPLATE_PATH)
    collect_pub = next(s for s in collect_doc["jobs"][0]["steps"] if isinstance(s, dict) and s.get("task") == "PublishPipelineArtifact@1")
    assert collect_pub["inputs"]["artifact"] == "y2-report-collect"
    assert collect_pub["inputs"]["targetPath"] == "$(Y2_REPORT_DIR)"


def test_referenced_scripts_exist_under_ado_scripts():
    """Verify all script files referenced by step templates exist on disk under ado/scripts/."""
    expected_scripts = [
        "resolve.ps1",
        "power-start.ps1",
        "run-command.ps1",
        "power-restore.ps1",
        "finalize.ps1",
        "Y2Az.psm1",
    ]
    for script_name in expected_scripts:
        script_path = SCRIPTS_DIR / script_name
        assert script_path.is_file(), f"Referenced script missing: {script_path}"


def test_azure_powershell_steps_configuration():
    """Verify all AzurePowerShell@5 steps configure pwsh: false and proper error handling."""
    step_templates = [
        STEPS_DIR / "resolve.yml",
        STEPS_DIR / "power-start.yml",
        STEPS_DIR / "run-command.yml",
        STEPS_DIR / "power-restore.yml",
        STEPS_DIR / "finalize.yml",
    ]
    for t_path in step_templates:
        doc = load_yaml(t_path)
        for step in doc.get("steps", []):
            if isinstance(step, dict) and step.get("task") == "AzurePowerShell@5":
                inputs = step.get("inputs", {})
                assert inputs.get("pwsh") is False, f"{t_path.name}: pwsh must be false"
                assert inputs.get("azurePowerShellVersion") == "LatestVersion", f"{t_path.name}: azurePowerShellVersion must be LatestVersion"
                assert inputs.get("errorActionPreference") == "stop", f"{t_path.name}: errorActionPreference must be stop"
                assert inputs.get("FailOnStandardError") is False, f"{t_path.name}: FailOnStandardError must be false"
                assert inputs.get("ScriptType") == "FilePath", f"{t_path.name}: ScriptType must be FilePath"


def test_run_command_validates_payload_path():
    """Verify run-command.yml validates payload file existence before submitting tasks."""
    doc = load_yaml(STEPS_DIR / "run-command.yml")
    steps = doc.get("steps", [])
    assert len(steps) == 2, f"Expected 2 steps in run-command.yml, got {len(steps)}"

    validation_step = steps[0]
    assert "powershell" in validation_step
    ps_code = validation_step["powershell"]
    assert "Test-Path -LiteralPath $payload" in ps_code
    assert "Payload file does not exist" in ps_code
    assert "exit 1" in ps_code


def test_no_bash_or_script_steps_in_templates():
    """Verify templates contain no 'bash:', 'script:', or 'pwsh:' steps (INV-1, INV-8)."""
    for yml_path in TEMPLATES_DIR.rglob("*.yml"):
        content = yml_path.read_text(encoding="utf-8")
        doc = load_yaml(yml_path)
        # Check raw lines for disallowed step keys
        for line in content.splitlines():
            stripped = line.strip()
            # Disallowed step keys at step level
            assert not stripped.startswith("- bash:"), f"Found disallowed 'bash:' step in {yml_path.name}"
            assert not stripped.startswith("- script:"), f"Found disallowed 'script:' step in {yml_path.name}"
            assert not stripped.startswith("- pwsh:"), f"Found disallowed 'pwsh:' step in {yml_path.name}"


def test_no_secrets_in_templates():
    """TP-01 / INV-5: Verify templates contain no hardcoded passwords, tokens, or credentials."""
    secret_patterns = [
        re.compile(r"password\s*[:=]\s*['\"][^'\"]+['\"]", re.IGNORECASE),
        re.compile(r"client_secret\s*[:=]\s*['\"][^'\"]+['\"]", re.IGNORECASE),
        re.compile(r"bearer\s+[A-Za-z0-9\-_]{20,}", re.IGNORECASE),
        re.compile(r"ghp_[A-Za-z0-9]{30,}"),
        re.compile(r"ey[A-Za-z0-9\-_]{30,}\.[A-Za-z0-9\-_]{30,}"),
    ]
    for yml_path in TEMPLATES_DIR.rglob("*.yml"):
        content = yml_path.read_text(encoding="utf-8")
        for pat in secret_patterns:
            assert not pat.search(content), f"Potential secret found matching {pat.pattern} in {yml_path.name}"
