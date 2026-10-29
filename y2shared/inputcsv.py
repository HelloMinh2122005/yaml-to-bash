"""Input CSV parsing and validation for yaml_to_bash."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from y2shared.config import Job
from y2shared.validate import ValidationIssue, ValidationResult

REQUIRED_COLUMNS = ["vm_name", "resource_group", "subscription", "os"]
ENV_NAME_RE = re.compile(r"[^A-Z0-9_]")

WINDOWS_INDICATORS = [
    "win",
    "windows",
]


@dataclass
class VMRow:
    index: int  # 1-based index in the input CSV (data rows)
    vm_name: str
    resource_group: str
    subscription: str
    os: str
    raw_data: dict[str, str]
    env: dict[str, str]

    @property
    def safe_name(self) -> str:
        """Sanitized VM name safe for filenames."""
        return re.sub(r"[^A-Za-z0-9._-]", "_", self.vm_name)

    def part_prefix(self, width: int = 4) -> str:
        """Formatted prefix for part files: e.g. 0001-vm-app-01"""
        return f"{self.index:0{width}d}-{self.safe_name}"


def to_env_var_name(col_name: str) -> str:
    """Convert column header to uppercase environment variable name."""
    cleaned = ENV_NAME_RE.sub("_", col_name.strip().upper())
    # Ensure leading character is valid for bash identifier
    if cleaned and cleaned[0].isdigit():
        cleaned = "_" + cleaned
    return cleaned


def os_family(os_str: str) -> str:
    """Determine OS family ('linux' or 'windows')."""
    val = os_str.strip().lower()
    if not val:
        return "linux"
    if val.startswith("win") or "windows" in val:
        return "windows"
    return "linux"


_detect_os_family = os_family


def is_linux_os(os_str: str) -> bool:
    """Check if the OS string is Linux-like (D3)."""
    return os_family(os_str) == "linux"


def read_input_csv(
    csv_path: str | Path,
    job: Job | None = None,
    os_family: str = "linux",
) -> tuple[list[VMRow], list[str], ValidationResult]:
    """
    Read, parse and validate list_vm.csv.
    Returns:
        (rows, headers, validation_result)
    """
    path = Path(csv_path).resolve()
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []

    if not path.is_file():
        errors.append(ValidationIssue(path=str(path), message=f"Input CSV file not found: {path}"))
        return [], [], ValidationResult(errors, warnings)

    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            content = f.read()
    except Exception as e:
        errors.append(ValidationIssue(path=str(path), message=f"Failed to read CSV file: {e}"))
        return [], [], ValidationResult(errors, warnings)

    reader = csv.reader(io.StringIO(content))
    raw_rows = list(reader)

    # Filter out blank rows
    non_blank_rows = [
        [c.strip() for c in r]
        for r in raw_rows
        if any(c.strip() for c in r)
    ]

    if not non_blank_rows:
        errors.append(ValidationIssue(path=str(path), message="CSV file is empty or contains no data"))
        return [], [], ValidationResult(errors, warnings)

    headers = [h.strip() for h in non_blank_rows[0]]

    # Check required headers
    missing_headers = [req for req in REQUIRED_COLUMNS if req not in headers]
    if missing_headers:
        errors.append(
            ValidationIssue(
                path=str(path),
                message=f"Missing required columns in CSV header: {missing_headers}. Present columns: {headers}",
            )
        )

    # Check job collect sources if job provided
    if job:
        for i, col in enumerate(job.collect):
            if col.source:
                m = re.match(r"^input\.(.+)$", col.source)
                if m:
                    src_field = m.group(1)
                    if src_field not in headers:
                        errors.append(
                            ValidationIssue(
                                path=f"collect[{i}].source",
                                message=f"Field '{src_field}' from '{col.source}' not found in CSV header: {headers}",
                            )
                        )

    rows: list[VMRow] = []
    seen_vms: dict[tuple[str, str, str], int] = {}

    for row_idx, r in enumerate(non_blank_rows[1:], start=1):
        if len(r) != len(headers):
            errors.append(
                ValidationIssue(
                    path=f"row[{row_idx}]",
                    message=f"Row column count ({len(r)}) does not match header column count ({len(headers)})",
                )
            )
            continue

        row_dict = dict(zip(headers, r))
        vm_name = row_dict.get("vm_name", "")
        rg = row_dict.get("resource_group", "")
        sub = row_dict.get("subscription", "")
        os_val = row_dict.get("os", "")

        # Check required fields are non-empty
        if not vm_name:
            errors.append(ValidationIssue(path=f"row[{row_idx}].vm_name", message="vm_name cannot be empty"))
        if not rg:
            errors.append(ValidationIssue(path=f"row[{row_idx}].resource_group", message="resource_group cannot be empty"))
        if not sub:
            errors.append(ValidationIssue(path=f"row[{row_idx}].subscription", message="subscription cannot be empty"))

        # OS family check
        if os_val:
            row_fam = _detect_os_family(os_val)
            if row_fam != os_family:
                if os_family == "linux":
                    msg = f"VM '{vm_name}' has non-Linux OS '{os_val}'; y2b only targets Linux VMs (use y2p)"
                else:
                    msg = f"VM '{vm_name}' has non-Windows OS '{os_val}'; y2p only targets Windows VMs (use y2b)"
                errors.append(
                    ValidationIssue(
                        path=f"row[{row_idx}].os",
                        message=msg,
                    )
                )

        # Duplicate check: (subscription, resource_group, vm_name)
        vm_key = (sub.lower(), rg.lower(), vm_name.lower())
        if vm_key in seen_vms:
            first_idx = seen_vms[vm_key]
            errors.append(
                ValidationIssue(
                    path=f"row[{row_idx}]",
                    message=f"Duplicate VM (subscription='{sub}', resource_group='{rg}', vm_name='{vm_name}') already defined in row {first_idx}",
                )
            )
        else:
            seen_vms[vm_key] = row_idx

        # Generate exported environment variables
        env_vars = {to_env_var_name(k): v for k, v in row_dict.items()}

        rows.append(
            VMRow(
                index=row_idx,
                vm_name=vm_name,
                resource_group=rg,
                subscription=sub,
                os=os_val,
                raw_data=row_dict,
                env=env_vars,
            )
        )

    return rows, headers, ValidationResult(errors, warnings)

