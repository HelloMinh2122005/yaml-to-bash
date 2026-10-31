"""Lab environment configuration and CSV validator for y2 squad."""

from __future__ import annotations

import csv
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class LabEnv:
    subscription_id: str
    subscription_name: str
    resource_group: str
    vms: tuple[str, ...]


def load_lab_env(path: Optional[str] = None) -> LabEnv:
    """Load LabEnv from a shell-sourceable env file."""
    env_path = path or os.environ.get("Y2_LAB_ENV")
    if not env_path:
        env_path = str(Path(__file__).resolve().parent / "lab.env")

    p = Path(env_path)
    if not p.is_file():
        raise ValueError(f"Lab env file not found: {env_path}")

    raw_vars: dict[str, str] = {}
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                raw_vars[key] = val

    required = ["LAB_SUBSCRIPTION_ID", "LAB_SUBSCRIPTION_NAME", "LAB_RESOURCE_GROUP", "LAB_VMS"]
    missing = [k for k in required if k not in raw_vars]
    if missing:
        raise ValueError(f"Missing required keys in lab env {env_path}: {missing}")

    sub_id = raw_vars["LAB_SUBSCRIPTION_ID"].strip().lower()
    sub_name = raw_vars["LAB_SUBSCRIPTION_NAME"].strip()
    rg = raw_vars["LAB_RESOURCE_GROUP"].strip()
    vms = tuple(v.strip().lower() for v in raw_vars["LAB_VMS"].split() if v.strip())

    return LabEnv(
        subscription_id=sub_id,
        subscription_name=sub_name,
        resource_group=rg,
        vms=vms,
    )


def is_lab_subscription(value: str, lab: LabEnv) -> bool:
    """Check if value matches the lab subscription by GUID or non-empty name."""
    val = value.strip()
    if val.lower() == lab.subscription_id.lower():
        return True
    if lab.subscription_name and val == lab.subscription_name:
        return True
    return False


def validate_csv(csv_path: str, lab: LabEnv) -> list[str]:
    """Validate that every row in CSV targets the lab environment.
    
    Returns a list of error strings prefixed by 'row <n>: ...'.
    """
    errors: list[str] = []
    p = Path(csv_path)
    if not p.is_file():
        return [f"CSV file not found: {csv_path}"]

    with open(p, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader, start=2):
            vm_raw = row.get("vm_name", "")
            rg_raw = row.get("resource_group", "")
            sub_raw = row.get("subscription", "")

            vm = vm_raw.strip().lower()
            rg = rg_raw.strip().lower()
            sub = sub_raw.strip()

            row_errors: list[str] = []
            if not is_lab_subscription(sub, lab):
                row_errors.append(f"subscription '{sub_raw.strip()}' is not the lab subscription")
            if rg != lab.resource_group.lower():
                row_errors.append(f"resource group '{rg_raw.strip()}' is not the lab resource group '{lab.resource_group}'")
            if vm not in lab.vms:
                row_errors.append(f"VM '{vm_raw.strip()}' is not in the lab VM list")

            if row_errors:
                errors.append(f"row {idx}: {'; '.join(row_errors)}")

    return errors


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "check-csv":
        csv_files = sys.argv[2:]
        if not csv_files:
            print("Usage: labenv.py check-csv <csv>...", file=sys.stderr)
            sys.exit(64)
        lab = load_lab_env()
        all_errors: list[str] = []
        for cf in csv_files:
            errs = validate_csv(cf, lab)
            for err in errs:
                all_errors.append(f"{cf}: {err}")
        if all_errors:
            for err in all_errors:
                print(err, file=sys.stderr)
            sys.exit(65)
        sys.exit(0)
    else:
        print("Usage: python3 lab/labenv.py check-csv <csv>...", file=sys.stderr)
        sys.exit(64)


if __name__ == "__main__":
    main()
