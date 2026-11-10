# tests/test_ado_ps51_compat.py - Windows PowerShell 5.1 compatibility & static hygiene tests
# Pure ASCII, LF line endings, BOM-less UTF-8, Python 3.9 compatible (stdlib + pytest).
# Covers: AC-10, TP-10, T-09.

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List, Sequence, Tuple

import pytest

from tests.ado_fixtures.run_ps import require_container_runtime, run_pwsh_script

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "ado" / "scripts"
TEMPLATES_DIR = REPO_ROOT / "ado" / "templates"
SETTINGS_PATH = REPO_ROOT / "tests" / "ado_fixtures" / "PSScriptAnalyzerSettings.psd1"


def get_all_target_files() -> List[Path]:
    """Return all files under ado/scripts/ and ado/templates/."""
    files: List[Path] = []
    if SCRIPTS_DIR.exists():
        files.extend([p for p in SCRIPTS_DIR.rglob("*") if p.is_file()])
    if TEMPLATES_DIR.exists():
        files.extend([p for p in TEMPLATES_DIR.rglob("*") if p.is_file()])
    return sorted(files)


def get_ado_script_files() -> List[Path]:
    """Return all PowerShell script files (*.ps1, *.psm1, *.psd1) under ado/scripts/."""
    if not SCRIPTS_DIR.exists():
        return []
    return sorted([
        p for p in SCRIPTS_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() in {".ps1", ".psm1", ".psd1"}
    ])


def strip_ps_comments_and_strings(content: str) -> str:
    """Remove block comments, single-line comments, here-strings, and string literals.

    This isolates actual PowerShell code tokens for syntactic inspection.
    """
    # Strip block comments <# ... #>
    content = re.sub(r"<#[\s\S]*?#>", "", content)
    # Strip here-strings (@"..."@ and @'...'@)
    content = re.sub(r"@\"[\s\S]*?\"@", "\"\"", content)
    content = re.sub(r"@'[\s\S]*?'@", "''", content)
    # Strip single line comments # ...
    content = re.sub(r"#.*$", "", content, flags=re.MULTILINE)
    # Strip double-quoted strings (handling escaped quotes `")
    content = re.sub(r"\"(?:[^\"`]|`.)*\"", "\"\"", content)
    # Strip single-quoted strings
    content = re.sub(r"'[^']*'", "''", content)
    return content


def find_convertto_json_violations(content: str, filename: str) -> List[str]:
    """Detect any ConvertTo-Json command invocation that lacks -Depth.

    Strips comments and joins backtick line continuations before inspecting commands.
    """
    # Strip block comments
    no_block = re.sub(r"<#[\s\S]*?#>", "", content)
    # Strip single-line comments
    clean_lines = [re.sub(r"#.*$", "", line) for line in no_block.splitlines()]

    # Join backtick-continued lines
    joined_lines: List[str] = []
    buffer = ""
    for line in clean_lines:
        trimmed = line.rstrip()
        if trimmed.endswith("`"):
            buffer += trimmed[:-1] + " "
        else:
            buffer += line
            joined_lines.append(buffer)
            buffer = ""
    if buffer:
        joined_lines.append(buffer)

    violations: List[str] = []
    for line_idx, line in enumerate(joined_lines, 1):
        if "ConvertTo-Json" in line:
            # Match ConvertTo-Json command and its arguments up to pipeline | or semicolon ; or parenthesis/bracket
            matches = list(re.finditer(
                r"\bConvertTo-Json\b(?P<args>[^|;\)\}\]]*)",
                line,
                re.IGNORECASE,
            ))
            for m in matches:
                args = m.group("args")
                if not re.search(r"-Depth\b", args, re.IGNORECASE):
                    violations.append(
                        f"{filename}:{line_idx}: ConvertTo-Json missing -Depth in: '{line.strip()}'"
                    )
    return violations


def find_ps7_syntax_violations(content: str, filename: str) -> List[str]:
    """Detect PowerShell 7+ syntax in code (outside comments and string literals).

    Targets:
    - ?? (null coalescing) and ??= (null coalescing assignment)
    - ?: (ternary operator: <expr> ? <true> : <false>)
    - && (pipeline chain AND)
    - || (pipeline chain OR)
    - -Parallel (ForEach-Object -Parallel or parallel workflow)
    - -AsByteStream (PS7 replacement for -Encoding Byte)
    """
    cleaned = strip_ps_comments_and_strings(content)
    violations: List[str] = []

    # 1. Null-coalescing: ?? or ??=
    if "??" in cleaned:
        violations.append(f"{filename}: Found PowerShell 7+ null-coalescing operator '??'")

    # 2. Pipeline chain AND: &&
    if "&&" in cleaned:
        violations.append(f"{filename}: Found PowerShell 7+ pipeline chain operator '&&'")

    # 3. Pipeline chain OR: ||
    if "||" in cleaned:
        violations.append(f"{filename}: Found PowerShell 7+ pipeline chain operator '||'")

    # 4. Ternary operator: <cond> ? <true> : <false>
    if re.search(r"(?<!\w)\?\s+[^;\n:]+\s+:", cleaned):
        violations.append(f"{filename}: Found PowerShell 7+ ternary operator '?:'")

    # 5. Parallel construct: -Parallel
    if re.search(r"-Parallel\b", cleaned, re.IGNORECASE):
        violations.append(f"{filename}: Found PowerShell 7+ '-Parallel' parameter")

    # 6. -AsByteStream
    if re.search(r"-AsByteStream\b", cleaned, re.IGNORECASE):
        violations.append(f"{filename}: Found PowerShell 7+ '-AsByteStream' parameter")

    return violations


# ==============================================================================
# Static Hygiene Tests (T-09, TP-10, AC-10)
# ==============================================================================

def test_static_hygiene_no_bom():
    """Verify no BOM (UTF-8, UTF-16, UTF-32) in any file under ado/scripts/ and ado/templates/."""
    files = get_all_target_files()
    assert files, "Target file list under ado/scripts/ and ado/templates/ must not be empty"

    bom_signatures = (
        (b"\xef\xbb\xbf", "UTF-8 BOM"),
        (b"\xff\xfe", "UTF-16 LE BOM"),
        (b"\xfe\xff", "UTF-16 BE BOM"),
        (b"\x00\x00\xfe\xff", "UTF-32 BE BOM"),
        (b"\xff\xfe\x00\x00", "UTF-32 LE BOM"),
    )

    failures: List[str] = []
    for p in files:
        raw = p.read_bytes()
        for sig, name in bom_signatures:
            if raw.startswith(sig):
                rel = p.relative_to(REPO_ROOT).as_posix()
                failures.append(f"{rel} starts with {name}")

    assert not failures, "Found BOM in files:\n" + "\n".join(failures)


def test_static_hygiene_lf_only():
    """Verify LF only (no CRLF \\r\\n or lone \\r) across ado/scripts/ and ado/templates/."""
    files = get_all_target_files()
    assert files, "Target file list under ado/scripts/ and ado/templates/ must not be empty"

    failures: List[str] = []
    for p in files:
        raw = p.read_bytes()
        if b"\r\n" in raw:
            rel = p.relative_to(REPO_ROOT).as_posix()
            failures.append(f"{rel} contains CRLF (\\r\\n)")
        elif b"\r" in raw:
            rel = p.relative_to(REPO_ROOT).as_posix()
            failures.append(f"{rel} contains standalone CR (\\r)")

    assert not failures, "Found non-LF line endings in files:\n" + "\n".join(failures)


def test_static_hygiene_pure_ascii():
    """Verify pure ASCII (0 non-ASCII characters) across ado/scripts/ and ado/templates/."""
    files = get_all_target_files()
    assert files, "Target file list under ado/scripts/ and ado/templates/ must not be empty"

    failures: List[str] = []
    for p in files:
        raw = p.read_bytes()
        non_ascii_indices = [idx for idx, b in enumerate(raw) if b >= 128]
        if non_ascii_indices:
            rel = p.relative_to(REPO_ROOT).as_posix()
            sample_bytes = [hex(raw[idx]) for idx in non_ascii_indices[:5]]
            failures.append(
                f"{rel} contains {len(non_ascii_indices)} non-ASCII bytes (e.g. {sample_bytes})"
            )

    assert not failures, "Found non-ASCII bytes in files:\n" + "\n".join(failures)


def test_static_tls12_explicitly_set_in_y2az():
    """Verify TLS 1.2 is explicitly set in ado/scripts/Y2Az.psm1 on module load (INV-8, AC-10)."""
    y2az_path = SCRIPTS_DIR / "Y2Az.psm1"
    assert y2az_path.is_file(), f"Expected {y2az_path} to exist"

    content = y2az_path.read_text(encoding="ascii")
    # Verify exact ServicePointManager assignment pattern
    pattern = r"\[Net\.ServicePointManager\]::SecurityProtocol\s*=\s*\[Net\.SecurityProtocolType\]::Tls12"
    match = re.search(pattern, content)
    assert match is not None, (
        "Y2Az.psm1 must explicitly set "
        "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12"
    )


def test_static_convertto_json_has_depth():
    """Verify every ConvertTo-Json invocation in ado/scripts/ includes -Depth (AC-10)."""
    script_files = get_ado_script_files()
    assert script_files, "Expected PowerShell script files under ado/scripts/"

    all_violations: List[str] = []
    for p in script_files:
        content = p.read_text(encoding="ascii")
        rel = p.relative_to(REPO_ROOT).as_posix()
        violations = find_convertto_json_violations(content, rel)
        all_violations.extend(violations)

    assert not all_violations, "ConvertTo-Json missing -Depth:\n" + "\n".join(all_violations)


def test_static_convertto_json_verifier_unit_cases():
    """Unit test for find_convertto_json_violations ensuring correct detection."""
    # Compliant invocations
    valid_samples = [
        "$data | ConvertTo-Json -Depth 10",
        "$data | ConvertTo-Json -Compress -Depth 5",
        "ConvertTo-Json -InputObject $data -Depth 10",
        "$obj | ConvertTo-Json `\n  -Depth 10 `\n  -Compress",
        "# ConvertTo-Json without depth in comment should be ignored",
        "<# Multi-line\n ConvertTo-Json without depth\n#>",
    ]
    for sample in valid_samples:
        violations = find_convertto_json_violations(sample, "test.ps1")
        assert not violations, f"False positive for valid sample: {sample}"

    # Incompliant invocations
    invalid_samples = [
        ("$x | ConvertTo-Json", "Missing -Depth in pipeline"),
        ("$x | ConvertTo-Json -Compress", "Missing -Depth with -Compress"),
        ("ConvertTo-Json -InputObject $x", "Missing -Depth with parameter"),
        ("ConvertTo-Json $x", "Missing -Depth with positional argument"),
    ]
    for sample, label in invalid_samples:
        violations = find_convertto_json_violations(sample, "test.ps1")
        assert violations, f"Failed to detect violation for: {label} ({sample})"


def test_static_absence_of_powershell_7_syntax():
    """Verify absence of PowerShell 7+ syntax across all ado/scripts/ files (AC-10, TP-10)."""
    script_files = get_ado_script_files()
    assert script_files, "Expected PowerShell script files under ado/scripts/"

    all_violations: List[str] = []
    for p in script_files:
        content = p.read_text(encoding="ascii")
        rel = p.relative_to(REPO_ROOT).as_posix()
        violations = find_ps7_syntax_violations(content, rel)
        all_violations.extend(violations)

    assert not all_violations, "Found PS 7+ syntax violations:\n" + "\n".join(all_violations)


def test_static_ps7_syntax_detector_unit_cases():
    """Unit test for find_ps7_syntax_violations ensuring every targeted construct is caught."""
    # Compliant PowerShell 5.1 constructs that must NOT trigger false positives
    valid_samples = [
        "# Comment with ?? and && and || and -Parallel and ?: and -AsByteStream",
        "<# Block comment with ?? and && and || #>",
        "$msg = 'Text with ?? and && and || and -Parallel and ?: and -AsByteStream'",
        '$str = "Interpolated text with ?? and && and || and -Parallel"',
        "$filtered = $items | Where-Object { $_.Name -eq 'foo' }",
        "if ($cond) { $x = 1 } else { $x = 2 }",
        "$items = @('alpha', 'beta')",
        "$map = @{ key = 'value' }",
    ]
    for sample in valid_samples:
        violations = find_ps7_syntax_violations(sample, "test.ps1")
        assert not violations, f"False positive on valid PS 5.1 sample: {sample}"

    # Incompliant constructs that MUST trigger detection
    invalid_matrix = [
        ("$val = $a ?? $b", "null-coalescing '??'"),
        ("$val ??= $defaultVal", "null-coalescing assignment '??='"),
        ("Get-Process && Stop-Process", "pipeline chain '&&'"),
        ("Get-Process || Write-Error 'failed'", "pipeline chain '||'"),
        ("$res = ($x -gt 0) ? 1 : 0", "ternary operator '?:'"),
        ("ForEach-Object -Parallel { $_ }", "ForEach-Object -Parallel"),
        ("$items | ForEach -Parallel { $_ }", "ForEach -Parallel"),
        ("Get-Content -Path $p -AsByteStream", "-AsByteStream parameter"),
    ]
    for sample, label in invalid_matrix:
        violations = find_ps7_syntax_violations(sample, "test.ps1")
        assert violations, f"Failed to detect PS 7+ construct: {label} in '{sample}'"


# ==============================================================================
# Containerized PSScriptAnalyzer Suite (T-09, TP-10, AC-10)
# ==============================================================================

def test_psscriptanalyzer_settings_file_structure():
    """Verify PSScriptAnalyzerSettings.psd1 exists and configures 5.1 Desktop rules."""
    assert SETTINGS_PATH.is_file(), f"Settings file missing: {SETTINGS_PATH}"

    raw = SETTINGS_PATH.read_bytes()
    assert not raw.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")), "BOM in settings file"
    assert b"\r" not in raw, "CR in settings file"
    assert raw.isascii(), "Non-ASCII in settings file"

    text = SETTINGS_PATH.read_text(encoding="ascii")
    # Verify targeted rules are specified
    assert "PSUseCompatibleSyntax" in text
    assert "PSUseCompatibleCommands" in text
    assert "PSUseCompatibleTypes" in text
    assert "PSUseCompatibleCmdlets" in text
    assert "'5.1'" in text


def test_psscriptanalyzer_51_desktop_profile_clean():
    """Run PSScriptAnalyzer over ado/scripts/* targeting 5.1 Desktop profile via container.

    Asserts zero findings (TP-10, AC-10). Skips if container runtime is unavailable.
    """
    require_container_runtime()

    analyzer_script = """
if (-not (Get-Command Invoke-ScriptAnalyzer -ErrorAction SilentlyContinue)) {
    Write-Output "SKIP: PSScriptAnalyzer module is not installed in the container image"
    exit 42
}

$settingsPath = '/repo/tests/ado_fixtures/PSScriptAnalyzerSettings.psd1'
$scriptsPath = '/repo/ado/scripts'

if (-not (Test-Path $settingsPath)) {
    Write-Error "Settings file not found: $settingsPath"
    exit 2
}

if (-not (Test-Path $scriptsPath)) {
    Write-Error "Scripts directory not found: $scriptsPath"
    exit 3
}

$findings = Invoke-ScriptAnalyzer -Path $scriptsPath -Settings $settingsPath -Recurse

if ($findings) {
    Write-Host "PSScriptAnalyzer detected $($findings.Count) issue(s):"
    $findings | Format-Table ScriptName, Line, RuleName, Severity, Message -AutoSize | Out-String | Write-Host
    exit 1
} else {
    Write-Output "ANALYZER_SUCCESS: 0 findings"
    exit 0
}
"""

    res = run_pwsh_script(analyzer_script, timeout=120)

    if res.returncode == 42 or "SKIP:" in res.stdout:
        pytest.skip(res.stdout.strip())

    assert res.returncode == 0, (
        f"PSScriptAnalyzer failed with exit code {res.returncode}.\n"
        f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    )
    assert "ANALYZER_SUCCESS: 0 findings" in res.stdout


def test_psscriptanalyzer_catches_violations():
    """Verify PSScriptAnalyzer with our settings actively detects incompatible code.

    Asserts that violations are reported for synthetic PS 7+ syntax and incompatible cmdlets.
    """
    require_container_runtime()

    test_script = """
if (-not (Get-Command Invoke-ScriptAnalyzer -ErrorAction SilentlyContinue)) {
    Write-Output "SKIP: PSScriptAnalyzer module is not installed in the container image"
    exit 42
}

$settingsPath = '/repo/tests/ado_fixtures/PSScriptAnalyzerSettings.psd1'
$badCode = @'
$val = $a ?? $b
Test-Json -Json '{"test": 1}'
'@

$findings = Invoke-ScriptAnalyzer -ScriptDefinition $badCode -Settings $settingsPath

if ($findings) {
    $rules = ($findings | Select-Object -ExpandProperty RuleName -Unique) -join ','
    Write-Output "DETECTED_RULES:$rules"
    exit 0
} else {
    Write-Error "Expected PSScriptAnalyzer to detect violations, but 0 were found"
    exit 1
}
"""

    res = run_pwsh_script(test_script, timeout=60)

    if res.returncode == 42 or "SKIP:" in res.stdout:
        pytest.skip(res.stdout.strip())

    assert res.returncode == 0, (
        f"Analyzer test failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    )
    assert "DETECTED_RULES:" in res.stdout
    # Verify that compatibility rules were fired
    assert "PSUseCompatibleSyntax" in res.stdout
    assert "PSUseCompatibleCmdlets" in res.stdout or "PSUseCompatibleCommands" in res.stdout
