"""Part files management (per-VM files, atomic writing, discovery)."""

from __future__ import annotations

import csv
import datetime
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

from y2shared.config import Job
from y2shared.inputcsv import VMRow
from y2shared.vmlog import VMLogger


def get_parts_dir(output_csv: Path) -> Path:
    """Derive the parts directory path from output_csv."""
    resolved = output_csv.resolve()
    return resolved.parent / f".{resolved.stem}.parts"


def generate_run_id() -> str:
    """Generate a run ID: <UTC yyyymmddThhmmssZ>-<8 hex>."""
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    return f"{now_utc.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"


def get_run_info(parts_dir: Path) -> dict[str, Any]:
    """Read run.json metadata."""
    run_json_path = parts_dir / "run.json"
    with open(run_json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def update_run_phase(parts_dir: Path, phase: str) -> None:
    """Update phase in run.json atomically ('invoke' | 'collect' | 'post' | 'merged')."""
    run_json_path = parts_dir / "run.json"
    with open(run_json_path, "r", encoding="utf-8") as f:
        info = json.load(f)
    info["phase"] = phase
    tmp_path = run_json_path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, run_json_path)


def init_parts_dir(
    parts_dir: Path,
    job: Job,
    total_rows: int,
    parallel: int,
    resume: bool = False,
    run_id: str | None = None,
) -> str:
    """Initialize or validate parts directory for a run. Returns run_id."""
    run_json_path = parts_dir / "run.json"

    if parts_dir.exists() and not resume:
        raise FileExistsError(
            f"Parts folder '{parts_dir}' already exists. Use --resume to resume an interrupted run, or remove it."
        )

    if resume:
        if not parts_dir.exists() or not run_json_path.exists():
            raise FileNotFoundError(
                f"Cannot resume: parts folder '{parts_dir}' or run.json does not exist."
            )
        try:
            with open(run_json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            stored_hash = data.get("job_hash")
            if stored_hash != job.job_hash:
                raise ValueError(
                    f"Cannot resume: stored job hash '{stored_hash}' does not match current job hash '{job.job_hash}'."
                )
            stored_run_id = data.get("run_id") or generate_run_id()
            return stored_run_id
        except Exception as e:
            raise RuntimeError(f"Failed to validate run.json during resume: {e}") from e

    # Fresh run
    parts_dir.mkdir(parents=True, exist_ok=True)
    actual_run_id = run_id or generate_run_id()
    info = {
        "job_hash": job.job_hash,
        "job_name": job.yaml_path.name,
        "run_id": actual_run_id,
        "phase": "invoke",
        "start_time": datetime.datetime.now().astimezone().isoformat(),
        "total_rows": total_rows,
        "parallel": parallel,
    }
    with open(run_json_path, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2)
    return actual_run_id


def get_part_paths(parts_dir: Path, row: VMRow, total_rows: int) -> tuple[Path, Path]:
    """Return (csv_part_path, log_part_path) for a VM."""
    width = max(4, len(str(total_rows)))
    prefix = row.part_prefix(width=width)
    return parts_dir / f"{prefix}.csv", parts_dir / f"{prefix}.log"


def is_part_finished(parts_dir: Path, row: VMRow, total_rows: int) -> bool:
    """Check if a finished (non-.tmp) .csv part exists for this VM and does not hold ERROR:still_running."""
    csv_path, _ = get_part_paths(parts_dir, row, total_rows)
    if not (csv_path.is_file() and csv_path.stat().st_size > 0):
        return False
    row_vals = read_part_row(csv_path)
    if any("ERROR:still_running" in str(c) for c in row_vals):
        return False
    return True


def is_part_still_running(parts_dir: Path, row: VMRow, total_rows: int) -> bool:
    """Check if a part file exists and holds ERROR:still_running."""
    csv_path, _ = get_part_paths(parts_dir, row, total_rows)
    if not (csv_path.is_file() and csv_path.stat().st_size > 0):
        return False
    row_vals = read_part_row(csv_path)
    return any("ERROR:still_running" in str(c) for c in row_vals)


def write_part_files(
    parts_dir: Path,
    row: VMRow,
    total_rows: int,
    row_values: list[str],
    logger: VMLogger,
) -> None:
    """
    Atomically write the VM's .log and .csv part files.
    The .log is finalized and renamed before the .csv so a finished .csv
    always has its corresponding .log.
    """
    csv_final, log_final = get_part_paths(parts_dir, row, total_rows)
    csv_tmp = csv_final.with_suffix(".csv.tmp")
    log_tmp = log_final.with_suffix(".log.tmp")

    # 1. Write log atomically
    logger.write_to_file(log_tmp)
    # fsync
    with open(log_tmp, "a", encoding="utf-8") as f:
        f.flush()
        os.fsync(f.fileno())
    os.replace(log_tmp, log_final)

    # 2. Write CSV row atomically
    with open(csv_tmp, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        writer.writerow(row_values)
        f.flush()
        os.fsync(f.fileno())
    os.replace(csv_tmp, csv_final)


def read_part_row(csv_path: Path) -> list[str]:
    """Read data row from a part CSV file."""
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for r in reader:
            if r:
                return r
    return []


def power_path(parts_dir: Path, row: VMRow, total_rows: int) -> Path:
    """Return the .power path for a VM."""
    width = max(4, len(str(total_rows)))
    prefix = row.part_prefix(width=width)
    return parts_dir / f"{prefix}.power"


def write_power_once(power_file: Path, data: dict[str, Any]) -> bool:
    """
    Atomically write initial power state file (create-if-absent via O_EXCL).
    Returns True if written, False if file already existed.
    """
    try:
        fd = os.open(power_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False

    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    return True


def read_power(power_file: Path) -> dict[str, Any] | None:
    """Read power state file if present, else None."""
    if not power_file.is_file():
        return None
    try:
        with open(power_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def update_power(power_file: Path, **updates) -> dict[str, Any]:
    """Atomically update existing power state file with given keys."""
    data = read_power(power_file) or {}
    data.update(updates)
    tmp_path = power_file.with_suffix(".power.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, power_file)
    return data


def append_part_log(log_path: Path, lines: list[str]) -> None:
    """Atomically append lines to VM part log file."""
    if not lines:
        return
    with open(log_path, "a", encoding="utf-8") as f:
        for line in lines:
            f.write(line if line.endswith("\n") else line + "\n")
        f.flush()
        os.fsync(f.fileno())

