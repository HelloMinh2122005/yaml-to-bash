"""Remote PowerShell payload compiler for yaml_to_powershell (y2p)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from y2shared.config import Job, Step
from y2shared.scan import scan_written_vars as _scan_written_vars

TEMPLATE_PATH = Path(__file__).parent / "templates" / "remote.ps1.tpl"
COLLECT_TEMPLATE_PATH = Path(__file__).parent / "templates" / "collect.ps1.tpl"
MAX_RECOMMENDED_SIZE_BYTES = 200 * 1024  # 200 KB size warning threshold

WRITE_RESULT_PATTERN = re.compile(r'(?i:write_result)\s+["\']?([A-Z_][A-Z0-9_]*)\b')


def ps_quote(s: str) -> str:
    """Escape and single-quote string literal for PowerShell."""
    return "'" + s.replace("'", "''") + "'"


def scan_written_vars(step: Step | dict | str) -> set[str]:
    """
    Statically scan a step body or script file for written variable names.
    Matches write_result (case-insensitive) NAME VALUE.
    """
    return _scan_written_vars(step, pattern=WRITE_RESULT_PATTERN)


def _clean_script_body(body: str, is_file: bool = False) -> str:
    """Normalize body, strip leading shebang if from file, ensure non-empty body becomes # (empty step)."""
    lines = body.splitlines()
    if is_file and lines and lines[0].startswith("#!"):
        lines = lines[1:]

    # Remove empty lines from start and end
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    non_comment_lines = [ln for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    if not non_comment_lines:
        lines.append("# (empty step)")

    return "\n".join(lines)


def _check_pwsh_syntax(script: str) -> list[str]:
    errors: list[str] = []
    if shutil.which("pwsh"):
        try:
            proc = subprocess.run(
                [
                    "pwsh",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "$code = [Console]::In.ReadToEnd(); "
                    "$tokens = $null; $errors = $null; "
                    "[System.Management.Automation.Language.Parser]::ParseInput($code, [ref]$tokens, [ref]$errors) | Out-Null; "
                    "if ($errors.Count -gt 0) { foreach ($e in $errors) { [Console]::Error.WriteLine($e.ToString()) }; exit 1 }",
                ],
                input=script,
                text=True,
                capture_output=True,
                timeout=10,
            )
            if proc.returncode != 0:
                err_msg = proc.stderr.strip() or proc.stdout.strip()
                errors.append(f"Syntax error in compiled PowerShell script:\n{err_msg}")
        except Exception:
            pass
    return errors


def compile_job(job: Job, run_id: str = "preview") -> tuple[str, list[str]]:
    """
    Compile runcommand steps into a single composite PowerShell script.
    Returns:
        (compiled_script, errors)
    """
    errors: list[str] = []
    rc = job.invoke.runcommand
    if not rc or not rc.steps:
        errors.append("No runcommand steps defined to compile")
        return "", errors

    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = f.read()

    step_entries: list[str] = []

    for i, step in enumerate(rc.steps, start=1):
        if step.script_path:
            p = step.script_path
            try:
                with open(p, "r", encoding="utf-8") as sf:
                    raw_body = sf.read()
            except Exception as e:
                errors.append(f"Failed to read script_path '{p}' for step '{step.name}': {e}")
                raw_body = "# (empty step)"
            is_file = True
        else:
            raw_body = step.script or ""
            is_file = False

        cleaned = _clean_script_body(raw_body, is_file=is_file)
        entry = (
            f"  @{{ Index = {i}; Name = {ps_quote(step.name)}; Timeout = {step.timeout}; Body = @'\n"
            f"{cleaned}\n"
            f"'@ }}"
        )
        step_entries.append(entry)

    step_table_block = ",\n".join(step_entries)

    compiled = (
        template
        .replace("__JOB_NAME__", job.yaml_path.name)
        .replace("__JOB_HASH__", job.job_hash)
        .replace("__RUN_ID__", run_id)
        .replace("__RES_FILE__", ps_quote(rc.res_file))
        .replace("__LOG_FILE__", ps_quote(rc.log_file))
        .replace("__STEP_TABLE__", step_table_block)
    )

    # Size check
    size_bytes = len(compiled.encode("utf-8"))
    if size_bytes > MAX_RECOMMENDED_SIZE_BYTES:
        import sys
        print(
            f"WARNING: Compiled remote script size ({size_bytes / 1024:.1f} KB) exceeds recommended {MAX_RECOMMENDED_SIZE_BYTES / 1024:.0f} KB threshold",
            file=sys.stderr,
        )

    # Check pwsh syntax if installed
    pwsh_errors = _check_pwsh_syntax(compiled)
    errors.extend(pwsh_errors)

    return compiled, errors


def compile_collect_script(job: Job) -> tuple[str, list[str]]:
    """
    Compile host-side PowerShell reader script template.
    Returns:
        (compiled_script, errors)
    """
    errors: list[str] = []
    rc = job.invoke.runcommand
    res_file = rc.res_file if rc else ""
    if not res_file:
        errors.append("No runcommand.res_file defined for collect script")
        return "", errors

    with open(COLLECT_TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = f.read()

    compiled = template.replace("__RES_FILE__", ps_quote(res_file))

    pwsh_errors = _check_pwsh_syntax(compiled)
    errors.extend(pwsh_errors)

    return compiled, errors

