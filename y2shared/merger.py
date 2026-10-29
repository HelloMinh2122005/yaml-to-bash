"""Merger engine: aggregates per-VM part files into output_csv and output_log."""

from __future__ import annotations

import csv
import datetime
import os
import re
import shutil
from pathlib import Path
from typing import Any

from y2shared.config import Job
from y2shared.inputcsv import VMRow
from y2shared.parts import get_part_paths, is_part_finished, read_part_row


class MergeError(Exception):
    """Raised when merging part files fails."""
    pass


def merge_parts(
    job: Job,
    all_rows: list[VMRow],
    parts_dir: Path,
    keep_parts: bool = False,
) -> dict[str, int]:
    """
    Merge all part files in original input order into output_csv and output_log.
    Returns outcome counts dictionary: {"OK": n, "SKIPPED": n, "ENDED": n, "ERROR": n, ...}
    """
    total_rows = len(all_rows)
    headers = [col.column for col in job.collect]

    csv_final = job.output_csv.resolve()
    log_final = job.output_log.resolve()

    csv_final.parent.mkdir(parents=True, exist_ok=True)
    log_final.parent.mkdir(parents=True, exist_ok=True)

    csv_tmp = csv_final.with_suffix(".csv.tmp")
    log_tmp = log_final.with_suffix(".log.tmp")

    outcome_counts: dict[str, int] = {
        "OK": 0,
        "SKIPPED": 0,
        "ENDED": 0,
        "ERROR": 0,
    }

    try:
        # 1. Merge CSV
        with open(csv_tmp, "w", encoding="utf-8", newline="") as f_csv:
            writer = csv.writer(f_csv, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
            writer.writerow(headers)

            for row in all_rows:
                csv_part, _ = get_part_paths(parts_dir, row, total_rows)
                if csv_part.is_file() and csv_part.stat().st_size > 0:
                    data_row = read_part_row(csv_part)
                else:
                    # Synthesize ERROR:no_result row (spec 5.5 rule 6)
                    data_row = []
                    for col in job.collect:
                        if col.source:
                            m = re.match(r"^input\.(.+)$", col.source)
                            field_name = m.group(1) if m else col.source
                            data_row.append(row.raw_data.get(field_name, ""))
                        else:
                            data_row.append("ERROR:no_result")

                writer.writerow(data_row)

                # Classify outcome based on columns
                # If any column starts with ERROR: -> ERROR
                # Else if any column starts with SKIPPED: -> SKIPPED
                # Else if any column starts with ENDED: -> ENDED
                # Else OK
                row_str = " ".join(data_row)
                if "ERROR:" in row_str:
                    outcome_counts["ERROR"] += 1
                elif "SKIPPED:" in row_str:
                    outcome_counts["SKIPPED"] += 1
                elif "ENDED:" in row_str:
                    outcome_counts["ENDED"] += 1
                else:
                    outcome_counts["OK"] += 1

            f_csv.flush()
            os.fsync(f_csv.fileno())

        # 2. Merge Log
        with open(log_tmp, "w", encoding="utf-8") as f_log:
            now_str = datetime.datetime.now().astimezone().isoformat()
            f_log.write(f"######################################################################\n")
            f_log.write(f"# yaml_to_bash Execution Log\n")
            f_log.write(f"# Job: {job.yaml_path.name} (SHA256: {job.job_hash})\n")
            f_log.write(f"# Merged: {now_str}\n")
            f_log.write(f"# Total VMs: {total_rows} (OK={outcome_counts['OK']}, SKIPPED={outcome_counts['SKIPPED']}, ENDED={outcome_counts['ENDED']}, ERROR={outcome_counts['ERROR']})\n")
            f_log.write(f"######################################################################\n\n")

            for row in all_rows:
                _, log_part = get_part_paths(parts_dir, row, total_rows)
                if log_part.is_file() and log_part.stat().st_size > 0:
                    try:
                        with open(log_part, "r", encoding="utf-8", errors="replace") as lf:
                            f_log.write(lf.read())
                            f_log.write("\n")
                    except Exception as e:
                        f_log.write(f"=== Error reading log for {row.vm_name} (row {row.index}): {e} ===\n\n")
                else:
                    f_log.write(f"=== No log recorded for {row.vm_name} (row {row.index}) ===\n\n")

            f_log.write(f"######################################################################\n")
            f_log.write(f"# End of Run Log\n")
            f_log.write(f"######################################################################\n")
            f_log.flush()
            os.fsync(f_log.fileno())

        # 3. Atomic replacement
        os.replace(csv_tmp, csv_final)
        os.replace(log_tmp, log_final)

    except Exception as e:
        if csv_tmp.exists():
            csv_tmp.unlink()
        if log_tmp.exists():
            log_tmp.unlink()
        raise MergeError(
            f"Merge failed: {e}. Part files preserved in {parts_dir}. "
            f"Retry with: y2b run {job.yaml_path} --resume"
        ) from e

    # 4. Cleanup parts directory unless --keep-parts
    if not keep_parts:
        try:
            shutil.rmtree(parts_dir)
        except Exception:
            pass

    return outcome_counts

