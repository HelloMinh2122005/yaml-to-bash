"""PowerShell-specific validation rules for yaml_to_powershell (y2p)."""

from __future__ import annotations

import ntpath
import re
from pathlib import Path
from typing import Any

from y2shared.config import Job
from y2shared.profile import Profile, Reporter
from y2shared.validate import (
    ValidationIssue,
    ValidationResult,
    validate_job_dict as shared_validate_job_dict,
    validate_job_file as shared_validate_job_file,
)


def _is_windows_absolute(path: str) -> bool:
    if not isinstance(path, str) or not path.strip():
        return False
    if not re.match(r"^[a-zA-Z]:[\\/]", path):
        return False
    return ntpath.isabs(path)


def _has_control_chars(path: str) -> bool:
    return any(ord(c) < 32 for c in path)


def validate_runcommand_ps(rc: dict[str, Any], yaml_dir: Path, reporter: Reporter) -> None:
    """Frontend rules for y2p runcommand: Windows paths and PowerShell 5.1 compatibility."""
    res_file = rc.get("res_file")
    if isinstance(res_file, str) and _has_control_chars(res_file):
        reporter.warn(
            "invoke.runcommand.res_file",
            f"Path '{res_file}' contains control character (sign of YAML backslash escape; use single quotes)",
        )
    if not res_file or not isinstance(res_file, str) or not _is_windows_absolute(res_file):
        reporter.err(
            "invoke.runcommand.res_file",
            f"res_file must be an absolute Windows path with a drive letter, got '{res_file}'",
        )

    log_file = rc.get("log_file")
    if isinstance(log_file, str) and _has_control_chars(log_file):
        reporter.warn(
            "invoke.runcommand.log_file",
            f"Path '{log_file}' contains control character (sign of YAML backslash escape; use single quotes)",
        )
    if not log_file or not isinstance(log_file, str) or not _is_windows_absolute(log_file):
        reporter.err(
            "invoke.runcommand.log_file",
            f"log_file must be an absolute Windows path with a drive letter, got '{log_file}'",
        )

    steps = rc.get("steps")
    if isinstance(steps, list):
        for i, s in enumerate(steps):
            if not isinstance(s, dict):
                continue
            s_path = f"invoke.runcommand.steps[{i}]"
            step_body = ""
            if s.get("script_path"):
                sp = str(s["script_path"])
                if not sp.endswith(".ps1"):
                    reporter.warn(f"{s_path}.script_path", f"Script path '{sp}' lacks .ps1 extension")
                resolved = (yaml_dir / sp).resolve() if not Path(sp).is_absolute() else Path(sp)
                if resolved.is_file():
                    try:
                        with open(resolved, "r", encoding="utf-8") as sf:
                            step_body = sf.read()
                    except Exception:
                        step_body = ""
            elif s.get("script"):
                step_body = str(s.get("script") or "")

            # Check for '@ at line start (would close here-string)
            for line in step_body.splitlines():
                if line.startswith("'@"):
                    reporter.err(f"{s_path}.script", "Step body line starts with '@ (would close here-string)")
                    break

            # Check for PowerShell 7-only syntax (??, ?., &&, ||, ternary)
            if re.search(r"\?\?|\?\.|&&|\|\||\?[^:\n]+\:[^\n]+", step_body):
                reporter.warn(
                    f"{s_path}.script",
                    "Step body contains PowerShell 7-only syntax (??, ?., &&, ||, or ternary); PowerShell 5.1 is targeted",
                )

            # Check for non-ASCII
            if any(ord(c) > 127 for c in step_body):
                reporter.warn(
                    f"{s_path}.script",
                    "Step body contains non-ASCII characters (encoding through run-command to 5.1 is not guaranteed)",
                )


def validate_job_dict(
    raw: dict[str, Any],
    yaml_path: str | Path,
    profile: Profile | None = None,
) -> ValidationResult:
    """Validate raw YAML dictionary using y2p profile."""
    if profile is None:
        from y2p.profile import PROFILE
        profile = PROFILE
    return shared_validate_job_dict(raw, yaml_path, profile=profile)


def validate_job_file(
    yaml_path: str | Path,
    profile: Profile | None = None,
) -> tuple[Job | None, ValidationResult]:
    """Validate job YAML file using y2p profile."""
    if profile is None:
        from y2p.profile import PROFILE
        profile = PROFILE
    return shared_validate_job_file(yaml_path, profile=profile)
