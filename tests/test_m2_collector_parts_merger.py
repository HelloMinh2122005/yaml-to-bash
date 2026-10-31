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
    """Verify that collecting and merging matches job specification."""
    job_path = Path("examples/linux/job.yaml")
    job = load_job(job_path)

    # Use tmp_path for output files
    job.output_csv = tmp_path / "result.csv"
    job.output_log = tmp_path / "result.log"

    rows, _, _ = read_input_csv(job.input, job=job)
    assert len(rows) == 3

    res1 = RemoteResult(vars={"OS_NAME": "Red Hat Enterprise Linux", "OS_VERSION": "9.4", "KERNEL_VERSION": "5.14.0", "UPTIME_SECS": "1200", "LOAD_1M": "0.05", "ROOT_DISK_USAGE": "15%", "HW_AUDIT_STATUS": "PASS"})
    res2 = RemoteResult(vars={"OS_NAME": "Red Hat Enterprise Linux", "OS_VERSION": "8.10", "KERNEL_VERSION": "4.18.0", "UPTIME_SECS": "2400", "LOAD_1M": "0.10", "ROOT_DISK_USAGE": "20%", "HW_AUDIT_STATUS": "PASS"})
    res3 = RemoteResult(vars={"OS_NAME": "Oracle Linux Server", "OS_VERSION": "8.10", "KERNEL_VERSION": "5.15.0", "UPTIME_SECS": "3600", "LOAD_1M": "0.20", "ROOT_DISK_USAGE": "25%", "HW_AUDIT_STATUS": "FAIL"})

    parts_dir = get_parts_dir(job.output_csv)
    init_parts_dir(parts_dir, job, total_rows=len(rows), parallel=10)

    # Process and write parts
    log1 = VMLogger(rows[0].vm_name, rows[0].index)
    vals1 = collect_for_vm(job, rows[0], remote_result=res1, logger=log1)
    write_part_files(parts_dir, rows[0], total_rows=3, row_values=vals1, logger=log1)

    log2 = VMLogger(rows[1].vm_name, rows[1].index)
    vals2 = collect_for_vm(job, rows[1], remote_result=res2, logger=log2)
    write_part_files(parts_dir, rows[1], total_rows=3, row_values=vals2, logger=log2)

    log3 = VMLogger(rows[2].vm_name, rows[2].index)
    vals3 = collect_for_vm(job, rows[2], remote_result=res3, logger=log3)
    write_part_files(parts_dir, rows[2], total_rows=3, row_values=vals3, logger=log3)

    # Merge
    counts = merge_parts(job, rows, parts_dir, keep_parts=True)
    assert counts["OK"] == 3

    # Verify CSV content
    with open(job.output_csv, "r", encoding="utf-8") as f:
        reader = list(csv.reader(f))

    expected_header = ["vm_name", "resource_group", "subscription", "os_target", "os_name", "os_version", "kernel_version", "uptime_secs", "load_1m", "root_disk_usage", "hw_audit_status", "verdict"]
    assert reader[0] == expected_header
    assert reader[1] == ["lin01", "rg-lab-10vms", rows[0].subscription, "rhel9", "Red Hat Enterprise Linux", "9.4", "5.14.0", "1200", "0.05", "15%", "PASS", "HEALTHY"]
    assert reader[2] == ["lin02", "rg-lab-10vms", rows[1].subscription, "rhel8", "Red Hat Enterprise Linux", "8.10", "4.18.0", "2400", "0.10", "20%", "PASS", "HEALTHY"]
    assert reader[3] == ["lin03", "rg-lab-10vms", rows[2].subscription, "ol8", "Oracle Linux Server", "8.10", "5.15.0", "3600", "0.20", "25%", "FAIL", "ATTENTION"]


def test_tricky_values_and_computed_columns(tmp_path):
    """Test values with commas, quotes, and newlines."""
    test_yaml = tmp_path / "tricky.yaml"
    test_yaml.write_text(
        """
input: "./test.csv"
output_log: "./result.log"
output_csv: "./result.csv"
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
    job_path = Path("examples/linux/job.yaml")
    job = load_job(job_path)
    job.output_csv = tmp_path / "result.csv"
    job.output_log = tmp_path / "result.log"

    rows, _, _ = read_input_csv(job.input, job=job)

    # VM 1 skipped by precheck
    vals1 = collect_for_vm(job, rows[0], outcome_marker="SKIPPED:check-if-vm-is-running")
    # VM 2 error in dispatch
    vals2 = collect_for_vm(job, rows[1], outcome_marker="ERROR:run_command_timeout")

    # Verify passthrough columns
    assert vals1[0:4] == ["lin01", "rg-lab-10vms", rows[0].subscription, "rhel9"]
    assert vals1[4:] == ["SKIPPED:check-if-vm-is-running"] * 8

    assert vals2[0:4] == ["lin02", "rg-lab-10vms", rows[1].subscription, "rhel8"]
    assert vals2[4:] == ["ERROR:run_command_timeout"] * 8


def test_missing_part_synthesizes_error_no_result(tmp_path):
    """If a VM has no finished part, merger synthesizes ERROR:no_result row."""
    job_path = Path("examples/linux/job.yaml")
    job = load_job(job_path)
    job.output_csv = tmp_path / "result.csv"
    job.output_log = tmp_path / "result.log"

    rows, _, _ = read_input_csv(job.input, job=job)
    parts_dir = get_parts_dir(job.output_csv)
    init_parts_dir(parts_dir, job, total_rows=len(rows), parallel=10)

    # Only write part for VM 1; leave VM 2 and 3 missing
    res1 = RemoteResult(vars={"OS_NAME": "Red Hat Enterprise Linux", "OS_VERSION": "9.4", "KERNEL_VERSION": "5.14.0", "UPTIME_SECS": "1200", "LOAD_1M": "0.05", "ROOT_DISK_USAGE": "15%", "HW_AUDIT_STATUS": "PASS"})
    vals1 = collect_for_vm(job, rows[0], remote_result=res1)
    logger1 = VMLogger(rows[0].vm_name, rows[0].index)
    write_part_files(parts_dir, rows[0], total_rows=3, row_values=vals1, logger=logger1)

    # Merge
    counts = merge_parts(job, rows, parts_dir, keep_parts=True)
    assert counts["OK"] == 1
    assert counts["ERROR"] == 2

    with open(job.output_csv, "r", encoding="utf-8") as f:
        reader = list(csv.reader(f))

    assert len(reader) == 4  # Header + 3 data rows
    assert reader[1][0] == "lin01"
    assert reader[2][0] == "lin02"
    assert reader[2][1] == "rg-lab-10vms"
    assert reader[2][4] == "ERROR:no_result"
    assert reader[2][11] == "ERROR:no_result"


def test_tmp_part_is_ignored_and_treated_as_missing(tmp_path):
    """Leftover .tmp files must NOT be treated as finished."""
    job_path = Path("examples/linux/job.yaml")
    job = load_job(job_path)
    job.output_csv = tmp_path / "result.csv"
    job.output_log = tmp_path / "result.log"

    rows, _, _ = read_input_csv(job.input, job=job)
    parts_dir = get_parts_dir(job.output_csv)
    init_parts_dir(parts_dir, job, total_rows=len(rows), parallel=10)

    # Write a .tmp file for VM 1 (simulating crashed worker)
    csv_part, _ = get_part_paths(parts_dir, rows[0], total_rows=3)
    tmp_file = csv_part.with_suffix(".csv.tmp")
    tmp_file.write_text("lin01,rg-lab-10vms,sub,rhel9,partial,row\n", encoding="utf-8")

    assert not is_part_finished(parts_dir, rows[0], total_rows=3)

    counts = merge_parts(job, rows, parts_dir, keep_parts=True)
    assert counts["ERROR"] == 3  # All 3 synthesized as ERROR:no_result


def test_merge_order_preserved(tmp_path):
    """Parts written out of order must be merged in 1-based index order."""
    job_path = Path("examples/linux/job.yaml")
    job = load_job(job_path)
    job.output_csv = tmp_path / "result.csv"
    job.output_log = tmp_path / "result.log"

    rows, _, _ = read_input_csv(job.input, job=job)
    parts_dir = get_parts_dir(job.output_csv)
    init_parts_dir(parts_dir, job, total_rows=3, parallel=3)

    # Write VM 2 FIRST
    res2 = RemoteResult(vars={"OS_NAME": "Red Hat Enterprise Linux", "OS_VERSION": "8.10", "KERNEL_VERSION": "4.18.0", "UPTIME_SECS": "2400", "LOAD_1M": "0.10", "ROOT_DISK_USAGE": "20%", "HW_AUDIT_STATUS": "PASS"})
    vals2 = collect_for_vm(job, rows[1], remote_result=res2)
    logger2 = VMLogger(rows[1].vm_name, rows[1].index)
    write_part_files(parts_dir, rows[1], total_rows=3, row_values=vals2, logger=logger2)

    # Write VM 1 SECOND
    res1 = RemoteResult(vars={"OS_NAME": "Red Hat Enterprise Linux", "OS_VERSION": "9.4", "KERNEL_VERSION": "5.14.0", "UPTIME_SECS": "1200", "LOAD_1M": "0.05", "ROOT_DISK_USAGE": "15%", "HW_AUDIT_STATUS": "PASS"})
    vals1 = collect_for_vm(job, rows[0], remote_result=res1)
    logger1 = VMLogger(rows[0].vm_name, rows[0].index)
    write_part_files(parts_dir, rows[0], total_rows=3, row_values=vals1, logger=logger1)

    # Write VM 3 THIRD
    res3 = RemoteResult(vars={"OS_NAME": "Oracle Linux Server", "OS_VERSION": "8.10", "KERNEL_VERSION": "5.15.0", "UPTIME_SECS": "3600", "LOAD_1M": "0.20", "ROOT_DISK_USAGE": "25%", "HW_AUDIT_STATUS": "FAIL"})
    vals3 = collect_for_vm(job, rows[2], remote_result=res3)
    logger3 = VMLogger(rows[2].vm_name, rows[2].index)
    write_part_files(parts_dir, rows[2], total_rows=3, row_values=vals3, logger=logger3)

    merge_parts(job, rows, parts_dir)

    with open(job.output_csv, "r", encoding="utf-8") as f:
        reader = list(csv.reader(f))

    assert reader[1][0] == "lin01"
    assert reader[2][0] == "lin02"
    assert reader[3][0] == "lin03"

