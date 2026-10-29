"""Collector engine: extracts passthrough, remote, and computed columns."""

from __future__ import annotations

import re
import time
from typing import Any

from y2b.compiler import scan_written_vars
from y2b.config import Job
from y2b.dispatcher import RemoteResult
from y2b.inputcsv import VMRow, to_env_var_name
from y2b.shell import run_bash
from y2b.vmlog import VMLogger


def collect_for_vm(
    job: Job,
    row: VMRow,
    remote_result: RemoteResult | None = None,
    outcome_marker: str | None = None,
    logger: VMLogger | None = None,
) -> list[str]:
    """
    Produce the list of column values for a single VM according to job.collect.
    If outcome_marker is set (e.g. SKIPPED, ENDED, ERROR), passthrough columns
    are filled and all other columns receive the outcome marker without running scripts.
    """
    row_values: list[str] = []

    if outcome_marker is not None:
        for col in job.collect:
            if col.source:
                m = re.match(r"^input\.(.+)$", col.source)
                field_name = m.group(1) if m else col.source
                val = row.raw_data.get(field_name, "")
            else:
                val = outcome_marker
            row_values.append(val)
            if logger:
                logger.log_collect(col.column, val)
        return row_values

    # Prepare environment for computed scripts
    env: dict[str, str] = dict(row.env)
    env["VM_STATUS"] = "OK"

    if remote_result:
        for k, v in remote_result.vars.items():
            if not k.startswith("__Y2B_"):
                env[k] = v

    # Map static vars to step indices (1-based), with later step overwriting earlier
    var_to_step: dict[str, int] = {}
    if job.invoke.runcommand and job.invoke.runcommand.steps:
        for i, step in enumerate(job.invoke.runcommand.steps, start=1):
            for v in scan_written_vars(step):
                var_to_step[v] = i

    for col in job.collect:
        if col.source:
            m = re.match(r"^input\.(.+)$", col.source)
            field_name = m.group(1) if m else col.source
            val = row.raw_data.get(field_name, "")
        elif col.var:
            if remote_result and col.var in remote_result.vars:
                # Rule 1: X was written -> its value
                val = remote_result.vars[col.var]
            else:
                # Rule 2: X was not written, and the scan maps X to a step whose status is not ok -> __ERROR__ <status>
                step_idx = var_to_step.get(col.var)
                if remote_result and step_idx is not None:
                    step_status = remote_result.vars.get(f"__Y2B_STEP_{step_idx}__")
                    if step_status and step_status != "ok":
                        val = f"__ERROR__ {step_status}"
                    else:
                        val = "__MISSING__"
                else:
                    # Rule 3: Otherwise -> __MISSING__
                    val = "__MISSING__"
        elif col.script or col.script_path:
            script_code = col.script or ""
            if col.script_path:
                try:
                    with open(col.script_path, "r", encoding="utf-8") as sf:
                        script_code = sf.read()
                except Exception as e:
                    val = f"ERROR:column:read_error"
                    if logger:
                        logger.log_error(f"Failed to read script_path '{col.script_path}': {e}")
                    row_values.append(val)
                    continue

            # Run computed column script
            res = run_bash(
                script=script_code,
                env=env,
                timeout=col.timeout,
                cwd=job.yaml_dir,
            )
            if res.timed_out:
                val = "ERROR:column:timeout"
                if logger:
                    logger.log_error(f"Computed column '{col.column}' timed out: {res.stderr}")
            elif res.rc != 0:
                val = f"ERROR:column:rc={res.rc}"
                if logger:
                    logger.log_error(f"Computed column '{col.column}' failed with rc={res.rc}: {res.stderr}")
            else:
                # Stdout stripped; internal newlines replaced by a single space
                lines = res.stdout.strip().splitlines()
                val = " ".join(ln.strip() for ln in lines if ln.strip())
        else:
            val = ""

        row_values.append(val)
        if logger:
            logger.log_collect(col.column, val)

        # Export computed column for subsequent columns (A8)
        col_env_var = "COL_" + to_env_var_name(col.column)
        env[col_env_var] = val

    return row_values

