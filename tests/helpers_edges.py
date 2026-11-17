"""Shared fixtures and factories for adversarial edge-case test suites."""

from __future__ import annotations

from pathlib import Path

from y2shared.config import Job, load_job
from y2shared.inputcsv import VMRow, read_input_csv

LINUX_JOB_YAML = Path("examples/linux/job.yaml")


def make_vm_row(
    index: int = 1,
    vm_name: str = "vm-1",
    subscription: str = "Sub One",
    resource_group: str = "rg-1",
    os_name: str = "linux",
) -> VMRow:
    """Construct a minimal VMRow for unit tests without reading a CSV file."""
    return VMRow(
        index=index,
        vm_name=vm_name,
        resource_group=resource_group,
        subscription=subscription,
        os=os_name,
        raw_data={"vm_name": vm_name},
        env={"VM_NAME": vm_name},
    )


def load_linux_job(tmp_path: Path) -> tuple[Job, list[VMRow]]:
    """Load the Linux example job with output paths redirected under tmp_path."""
    job = load_job(LINUX_JOB_YAML)
    job.output_csv = tmp_path / "result.csv"
    job.output_log = tmp_path / "result.log"
    rows, _, _ = read_input_csv(job.input, job=job)
    return job, rows
