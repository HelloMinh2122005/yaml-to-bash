"""Bash-specific validation rules for yaml_to_bash."""

from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import Any

from y2shared.profile import Reporter, Profile
from y2shared.validate import (
    ValidationIssue,
    ValidationResult,
    validate_job_dict as shared_validate_job_dict,
    validate_job_file as shared_validate_job_file,
)
from y2shared.config import Job


def validate_runcommand_bash(rc: dict[str, Any], yaml_dir: Path, reporter: Reporter) -> None:
    """Validate bash-specific runcommand configuration (POSIX absolute paths)."""
    res_file = rc.get("res_file")
    if not res_file or not isinstance(res_file, str) or not posixpath.isabs(res_file) or re.match(r"^[a-zA-Z]:", res_file):
        reporter.err("invoke.runcommand.res_file", f"res_file must be an absolute path, got '{res_file}'")

    log_file = rc.get("log_file")
    if not log_file or not isinstance(log_file, str) or not posixpath.isabs(log_file) or re.match(r"^[a-zA-Z]:", log_file):
        reporter.err("invoke.runcommand.log_file", f"log_file must be an absolute path, got '{log_file}'")


def validate_job_dict(raw: dict[str, Any], yaml_path: str | Path, profile: Profile | None = None) -> ValidationResult:
    """Validate raw YAML dictionary using y2b profile."""
    if profile is None:
        from y2b.profile import PROFILE
        profile = PROFILE
    return shared_validate_job_dict(raw, yaml_path, profile=profile)


def validate_job_file(yaml_path: str | Path, profile: Profile | None = None) -> tuple[Job | None, ValidationResult]:
    """Validate job YAML file using y2b profile."""
    if profile is None:
        from y2b.profile import PROFILE
        profile = PROFILE
    return shared_validate_job_file(yaml_path, profile=profile)

