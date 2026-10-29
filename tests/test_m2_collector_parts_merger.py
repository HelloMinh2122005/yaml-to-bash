"""Tests for Milestone M2: Collector, parts, and merger."""

import csv
from pathlib import Path
import pytest

from y2shared.config import load_job
from y2shared.collector import collect_for_vm
from y2shared.dispatcher import RemoteResult
from y2shared.inputcsv import VMRow, read_input_csv
from y2shared.merger import merge_parts, MergeError
from y2shared.parts import (
    get_parts_dir,
    init_parts_dir,
    write_part_files,
    is_part_finished,
    get_part_paths,
)
from y2shared.vmlog import VMLogger


def test_collector_and_merger_spec_5_4(tmp_path):
    """Verify that collecting and merging matches spec 5.4 exactly."""
    job_path = Path("examples/hip-compliance/job.yaml")
    job = load_job(job_path)

    # Use tmp_path for output files
    job.output_csv = tmp_path / "result.csv"
    job.output_log = tmp_path / "result.log"

    rows, _, _ = read_input_csv(job.input, job=job)
    assert len(rows) == 2

    # Fixture RemoteResults matching spec 5.4
    # vm-app-01: HIP_VERSION="22102026", KERNEL_COUNT="3" -> recommendation="Compliant", extended="OK"
    res1 = RemoteResult(vars={"HIP_VERSION": "22102026", "KERNEL_COUNT": "3"})
    # vm-app-02: HIP_VERSION="NOT_INSTALLED", KERNEL_COUNT="2" -> recommendation="Install HIP", extended="FAILED"
    res2 = RemoteResult(vars={"HIP_VERSION": "NOT_INSTALLED", "KERNEL_COUNT": "2"})

    parts_dir = get_parts_dir(job.output_csv)
    init_parts_dir(parts_dir, job, total_rows=len(rows), parallel=10)

    # Process and write parts
    log1 = VMLogger(rows[0].vm_name, rows[0].index)
    vals1 = collect_for_vm(job, rows[0], remote_result=res1, logger=log1)
    write_part_files(parts_dir, rows[0], total_rows=2, row_values=vals1, logger=log1)

    log2 = VMLogger(rows[1].vm_name, rows[1].index)
    vals2 = collect_for_vm(job, rows[1], remote_result=res2, logger=log2)
    write_part_files(parts_dir, rows[1], total_rows=2, row_values=vals2, logger=log2)

    # Merge
    counts = merge_parts(job, rows, parts_dir, keep_parts=True)
    assert counts["OK"] == 2

    # Verify CSV content
    with open(job.output_csv, "r", encoding="utf-8") as f:
        reader = list(csv.reader(f))

    expected_header = ["vm_name", "resource_group", "subscription", "os", "hip_version", "kernel_count", "recommendation", "extended_compliance"]
    assert reader[0] == expected_header
    assert reader[1] == ["vm-app-01", "rg-prod-eastus", "sub-prod-01", "rhel8", "22102026", "3", "Compliant", "OK"]
    assert reader[2] == ["vm-app-02", "rg-prod-eastus", "sub-prod-01", "rhel8", "NOT_INSTALLED", "2", "Install HIP", "FAILED"]


def test_tricky_values_and_computed_columns(tmp_path):
    """Test values with commas, quotes, and newlines."""
    test_yaml = tmp_path / "tricky.yaml"
    test_yaml.write_text(
        """
input: "./test.csv"
output_log: "./result.log"
output_csv: "./result.csv"
actions: "skip"
parallel: 1
sleep_before_collect: 0
invoke:
  runcommand:
    res_file: "/var/tmp/result.res"
    log_file: "/var/tmp/result.log"
    steps:
      - name: "step1"
        script: "write_result RAW_VAR test"
collect:
  - column: "vm_name"
    source: "input.vm_name"
  - column: "raw_var"
    var: "RAW_VAR"
  - column: "computed_multi"
    script: |
      echo "line 1"
      echo "line 2"
  - column: "computed_quotes"
    script: |
      echo 'value with "quotes" and, commas'
""",
        encoding="utf-8",
    )

    csv_file = tmp_path / "test.csv"
    csv_file.write_text("vm_name,resource_group,subscription,os\nvm-01,rg1,sub1,rhel8\n", encoding="utf-8")

    job = load_job(test_yaml)
    rows, _, _ = read_input_csv(csv_file, job=job)

    res = RemoteResult(vars={"RAW_VAR": 'hello, "world"'})
    vals = collect_for_vm(job, rows[0], remote_result=res)

    assert vals[0] == "vm-01"
    assert vals[1] == 'hello, "world"'
    # Multiline replaced by space:
    assert vals[2] == "line 1 line 2"
    assert vals[3] == 'value with "quotes" and, commas'

    # Write and merge
    parts_dir = get_parts_dir(job.output_csv)
    init_parts_dir(parts_dir, job, total_rows=1, parallel=1)
    logger = VMLogger(rows[0].vm_name, rows[0].index)
    write_part_files(parts_dir, rows[0], total_rows=1, row_values=vals, logger=logger)
    merge_parts(job, rows, parts_dir)

    # Read back from CSV file
    with open(job.output_csv, "r", encoding="utf-8") as f:
        reader = list(csv.reader(f))
    assert reader[1] == vals


def test_skipped_and_error_vm_outcome_markers(tmp_path):
    """Skipped or failed VMs must fill passthrough columns and outcome marker elsewhere."""
    job_path = Path("examples/hip-compliance/job.yaml")
    job = load_job(job_path)
    job.output_csv = tmp_path / "result.csv"
    job.output_log = tmp_path / "result.log"

    rows, _, _ = read_input_csv(job.input, job=job)

    # VM 1 skipped by precheck
    vals1 = collect_for_vm(job, rows[0], outcome_marker="SKIPPED:check-if-vm-is-running")
    # VM 2 error in dispatch
    vals2 = collect_for_vm(job, rows[1], outcome_marker="ERROR:run_command_timeout")

    # Verify passthrough columns
    assert vals1[0:4] == ["vm-app-01", "rg-prod-eastus", "sub-prod-01", "rhel8"]
    assert vals1[4:] == ["SKIPPED:check-if-vm-is-running"] * 4

    assert vals2[0:4] == ["vm-app-02", "rg-prod-eastus", "sub-prod-01", "rhel8"]
    assert vals2[4:] == ["ERROR:run_command_timeout"] * 4


def test_missing_part_synthesizes_error_no_result(tmp_path):
    """If a VM has no finished part, merger synthesizes ERROR:no_result row."""
    job_path = Path("examples/hip-compliance/job.yaml")
    job = load_job(job_path)
    job.output_csv = tmp_path / "result.csv"
    job.output_log = tmp_path / "result.log"

    rows, _, _ = read_input_csv(job.input, job=job)
    parts_dir = get_parts_dir(job.output_csv)
    init_parts_dir(parts_dir, job, total_rows=len(rows), parallel=10)

    # Only write part for VM 1; leave VM 2 missing
    res1 = RemoteResult(vars={"HIP_VERSION": "22102026", "KERNEL_COUNT": "3"})
    vals1 = collect_for_vm(job, rows[0], remote_result=res1)
    logger1 = VMLogger(rows[0].vm_name, rows[0].index)
    write_part_files(parts_dir, rows[0], total_rows=2, row_values=vals1, logger=logger1)

    # Merge
    counts = merge_parts(job, rows, parts_dir, keep_parts=True)
    assert counts["OK"] == 1
    assert counts["ERROR"] == 1

    with open(job.output_csv, "r", encoding="utf-8") as f:
        reader = list(csv.reader(f))

    assert len(reader) == 3  # Header + 2 data rows
    assert reader[1][0] == "vm-app-01"
    assert reader[2][0] == "vm-app-02"
    assert reader[2][1] == "rg-prod-eastus"
    assert reader[2][4] == "ERROR:no_result"
    assert reader[2][7] == "ERROR:no_result"


def test_tmp_part_is_ignored_and_treated_as_missing(tmp_path):
    """Leftover .tmp files must NOT be treated as finished."""
    job_path = Path("examples/hip-compliance/job.yaml")
    job = load_job(job_path)
    job.output_csv = tmp_path / "result.csv"
    job.output_log = tmp_path / "result.log"

    rows, _, _ = read_input_csv(job.input, job=job)
    parts_dir = get_parts_dir(job.output_csv)
    init_parts_dir(parts_dir, job, total_rows=len(rows), parallel=10)

    # Write a .tmp file for VM 1 (simulating crashed worker)
    csv_part, _ = get_part_paths(parts_dir, rows[0], total_rows=2)
    tmp_file = csv_part.with_suffix(".csv.tmp")
    tmp_file.write_text("vm-app-01,rg-prod-eastus,sub-prod-01,rhel8,partial,row\n", encoding="utf-8")

    assert not is_part_finished(parts_dir, rows[0], total_rows=2)

    counts = merge_parts(job, rows, parts_dir, keep_parts=True)
    assert counts["ERROR"] == 2  # Both synthesized as ERROR:no_result


def test_merge_order_preserved(tmp_path):
    """Parts written out of order must be merged in 1-based index order."""
    job_path = Path("examples/hip-compliance/job.yaml")
    job = load_job(job_path)
    job.output_csv = tmp_path / "result.csv"
    job.output_log = tmp_path / "result.log"

    rows, _, _ = read_input_csv(job.input, job=job)
    parts_dir = get_parts_dir(job.output_csv)
    init_parts_dir(parts_dir, job, total_rows=2, parallel=2)

    # Write VM 2 FIRST
    res2 = RemoteResult(vars={"HIP_VERSION": "NOT_INSTALLED", "KERNEL_COUNT": "2"})
    vals2 = collect_for_vm(job, rows[1], remote_result=res2)
    logger2 = VMLogger(rows[1].vm_name, rows[1].index)
    write_part_files(parts_dir, rows[1], total_rows=2, row_values=vals2, logger=logger2)

    # Write VM 1 SECOND
    res1 = RemoteResult(vars={"HIP_VERSION": "22102026", "KERNEL_COUNT": "3"})
    vals1 = collect_for_vm(job, rows[0], remote_result=res1)
    logger1 = VMLogger(rows[0].vm_name, rows[0].index)
    write_part_files(parts_dir, rows[0], total_rows=2, row_values=vals1, logger=logger1)

    merge_parts(job, rows, parts_dir)

    with open(job.output_csv, "r", encoding="utf-8") as f:
        reader = list(csv.reader(f))

    assert reader[1][0] == "vm-app-01"
    assert reader[2][0] == "vm-app-02"

