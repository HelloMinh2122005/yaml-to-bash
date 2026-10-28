"""Part files management (per-VM files, atomic writing, discovery)."""

from __future__ import annotations

import csv
import datetime
import json
import os
import re
from pathlib import Path
from typing import Any

from y2b.config import Job
from y2b.inputcsv import VMRow
from y2b.vmlog import VMLogger


def get_parts_dir(output_csv: Path) -> Path:
    """Derive the parts directory path from output_csv."""
    resolved = output_csv.resolve()
    return resolved.parent / f".{resolved.stem}.parts"


def init_parts_dir(
    parts_dir: Path,
    job: Job,
    total_rows: int,
    parallel: int,
    resume: bool = False,
) -> None:
    """Initialize or validate parts directory for a run."""
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
        except Exception as e:
            raise RuntimeError(f"Failed to validate run.json during resume: {e}") from e
        return

    # Fresh run
    parts_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "job_hash": job.job_hash,
        "job_name": job.yaml_path.name,
        "start_time": datetime.datetime.now().astimezone().isoformat(),
        "total_rows": total_rows,
        "parallel": parallel,
    }
    with open(run_json_path, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2)


def get_part_paths(parts_dir: Path, row: VMRow, total_rows: int) -> tuple[Path, Path]:
    """Return (csv_part_path, log_part_path) for a VM."""
    width = max(4, len(str(total_rows)))
    prefix = row.part_prefix(width=width)
    return parts_dir / f"{prefix}.csv", parts_dir / f"{prefix}.log"


def is_part_finished(parts_dir: Path, row: VMRow, total_rows: int) -> bool:
    """Check if a finished (non-.tmp) .csv part exists for this VM."""
    csv_path, _ = get_part_paths(parts_dir, row, total_rows)
    return csv_path.is_file() and csv_path.stat().st_size > 0


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

