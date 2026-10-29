"""Remote bash payload compiler for yaml_to_bash."""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

from y2shared.config import Job, Step
from y2shared.scan import scan_written_vars as _scan_written_vars

TEMPLATE_PATH = Path(__file__).parent / "templates" / "remote.sh.tpl"
COLLECT_TEMPLATE_PATH = Path(__file__).parent / "templates" / "collect.sh.tpl"
MAX_RECOMMENDED_SIZE_BYTES = 200 * 1024  # 200 KB size warning threshold


WRITE_RESULT_PATTERN = re.compile(r'write_result\s+["\']?([A-Z_][A-Z0-9_]*)\b')


def scan_written_vars(step: Step | dict | str) -> set[str]:
    """
    Statically scan a step body or script file for written variable names.
    Matches write_result NAME VALUE or write_result "NAME" ...
    Returns set of variable names (excluding reserved __Y2B_*).
    """
    return _scan_written_vars(step, pattern=WRITE_RESULT_PATTERN)


def _clean_script_body(body: str, is_file: bool = False) -> str:
    """Normalize body, strip leading shebang if from file, ensure non-empty valid bash."""
    lines = body.splitlines()
    if is_file and lines and lines[0].startswith("#!"):
        lines = lines[1:]

    # Remove empty lines from start and end
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    # Check if there are only comments or empty lines
    non_comment_lines = [ln for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    if not non_comment_lines:
        lines.append(":")

    return "\n".join(lines)


def compile_job(job: Job, run_id: str = "preview") -> tuple[str, list[str]]:
    """
    Compile runcommand steps into a single composite bash script.
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

    step_funcs: list[str] = []
    func_names: list[str] = []
    run_step_lines: list[str] = []

    for i, step in enumerate(rc.steps, start=1):
        func_name = f"y2b_step_{i}"
        func_names.append(func_name)

        if step.script_path:
            p = step.script_path
            try:
                with open(p, "r", encoding="utf-8") as sf:
                    raw_body = sf.read()
            except Exception as e:
                errors.append(f"Failed to read script_path '{p}' for step '{step.name}': {e}")
                raw_body = ":"
            comment_src = f"  (from {step.script_path.name})"
            is_file = True
        else:
            raw_body = step.script or ":"
            comment_src = ""
            is_file = False

        cleaned = _clean_script_body(raw_body, is_file=is_file)
        # Indent body
        indented_lines = ["  " + ln if ln.strip() else "" for ln in cleaned.splitlines()]
        body_indented = "\n".join(indented_lines)

        func_decl = f"{func_name}() {{            # {step.name}{comment_src}\n{body_indented}\n}}"
        step_funcs.append(func_decl)

        run_step_lines.append(f"run_step {func_name} {step.timeout} {i} {shlex.quote(step.name)}")

    step_functions_block = "\n".join(step_funcs)
    export_functions_line = f"export -f {' '.join(func_names)}"
    run_steps_block = "\n".join(run_step_lines)

    compiled = (
        template
        .replace("__JOB_NAME__", job.yaml_path.name)
        .replace("__JOB_HASH__", job.job_hash)
        .replace("__RUN_ID__", run_id)
        .replace("__RES_FILE__", shlex.quote(rc.res_file))
        .replace("__LOG_FILE__", shlex.quote(rc.log_file))
        .replace("__STEP_FUNCTIONS__", step_functions_block)
        .replace("__EXPORT_FUNCTIONS__", export_functions_line)
        .replace("__RUN_STEPS__", run_steps_block)
    )

    # Size check
    size_bytes = len(compiled.encode("utf-8"))
    if size_bytes > MAX_RECOMMENDED_SIZE_BYTES:
        import sys
        print(
            f"WARNING: Compiled remote script size ({size_bytes / 1024:.1f} KB) exceeds recommended {MAX_RECOMMENDED_SIZE_BYTES / 1024:.0f} KB threshold",
            file=sys.stderr,
        )

    # Check bash -n syntax
    try:
        proc = subprocess.run(
            ["bash", "-n"],
            input=compiled,
            text=True,
            capture_output=True,
        )
        if proc.returncode != 0:
            errors.append(f"Syntax error in compiled bash script:\n{proc.stderr.strip()}")
    except FileNotFoundError:
        # bash not installed on host, skip bash -n
        pass

    return compiled, errors


def compile_collect_script(job: Job) -> tuple[str, list[str]]:
    """
    Compile host-side reader script template.
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

    compiled = template.replace("__RES_FILE__", shlex.quote(res_file))

    try:
        proc = subprocess.run(
            ["bash", "-n"],
            input=compiled,
            text=True,
            capture_output=True,
        )
        if proc.returncode != 0:
            errors.append(f"Syntax error in compiled collect script:\n{proc.stderr.strip()}")
    except FileNotFoundError:
        pass

    return compiled, errors
