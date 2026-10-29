"""Tests for Milestone M2: Collect list, VMLogger.from_lines, and parts tracking."""

import json
from pathlib import Path
import pytest

from y2b.collectlist import CollectList
from y2b.config import Job, Invoke, RunCommand
from y2b.inputcsv import VMRow
from y2b.parts import (
    init_parts_dir,
    get_run_info,
    update_run_phase,
    is_part_finished,
    is_part_still_running,
    write_part_files,
)
from y2b.vmlog import VMLogger


def make_test_row(index: int = 1, name: str = "vm-01") -> VMRow:
    return VMRow(
        index=index,
        vm_name=name,
        resource_group="rg-test",
        subscription="sub-12345",
        os="rhel8",
        raw_data={"vm_name": name, "resource_group": "rg-test", "subscription": "sub-12345", "os": "rhel8"},
        env={"VM_NAME": name, "RESOURCE_GROUP": "rg-test", "SUBSCRIPTION": "sub-12345"},
    )


def test_collect_list_append_and_load(tmp_path):
    cl = CollectList(tmp_path)
    r1 = {
        "index": 1,
        "vm_name": "vm-01",
        "subscription": "sub-1",
        "resource_group": "rg-1",
        "invoked_at": "2026-10-29T10:00:00Z",
        "confirmed": False,
        "attempts": 1,
        "log": ["line 1", "line 2"],
    }
    cl.append(r1)

    loaded = cl.load()
    assert len(loaded) == 1
    assert loaded[1]["confirmed"] is False
    assert loaded[1]["vm_name"] == "vm-01"

    # Append updated record for index 1 (confirmed: True)
    r1_updated = dict(r1)
    r1_updated["confirmed"] = True
    r1_updated["log"] = ["line 1", "line 2", "accepted"]
    cl.append(r1_updated)

    loaded2 = cl.load()
    assert len(loaded2) == 1
    assert loaded2[1]["confirmed"] is True
    assert loaded2[1]["log"] == ["line 1", "line 2", "accepted"]


def test_collect_list_truncated_last_line_tolerated(tmp_path):
    cl = CollectList(tmp_path)
    cl.append({"index": 1, "vm_name": "vm-01", "confirmed": True})

    # Manually append a truncated line (e.g. process was killed mid-write)
    with open(cl.path, "a", encoding="utf-8") as f:
        f.write('{"index": 2, "vm_name": "vm-02", "confir\n')

    # Warning issued, but loads valid records
    with pytest.warns(UserWarning, match="Ignoring malformed last line"):
        loaded = cl.load()

    assert len(loaded) == 1
    assert 1 in loaded


def test_collect_list_mid_file_corruption_rejected(tmp_path):
    cl = CollectList(tmp_path)
    cl.append({"index": 1, "vm_name": "vm-01"})

    # Write corrupt line in middle
    with open(cl.path, "a", encoding="utf-8") as f:
        f.write('corrupt mid-file json\n')

    # Write subsequent line
    cl.append({"index": 3, "vm_name": "vm-03"})

    with pytest.raises(ValueError, match="Malformed line 2"):
        cl.load()


def test_collect_list_vm_name_mismatch_rejected(tmp_path):
    cl = CollectList(tmp_path)
    cl.append({"index": 1, "vm_name": "vm-expected"})

    rows = [make_test_row(index=1, name="vm-DIFFERENT")]
    with pytest.raises(ValueError, match="Input CSV changed"):
        cl.load(rows)


def test_vmlogger_from_lines():
    phase1_lines = [
        "==================================================",
        "VM: vm-01 (Row index 1) — Started 2026-10-29T10:00:00Z",
        "==================================================",
        "[2026-10-29T10:00:01Z] [dispatch] invoke accepted",
    ]
    logger = VMLogger.from_lines("vm-01", 1, phase1_lines)
    assert logger.lines == phase1_lines

    logger.log_dispatch("collecting reader output")
    logger.finalize("OK")

    text = logger.get_text()
    assert "invoke accepted" in text
    assert "collecting reader output" in text
    assert "Final outcome: OK" in text


def test_parts_run_json_and_still_running_status(tmp_path):
    job = Job(
        yaml_path=tmp_path / "job.yaml",
        yaml_dir=tmp_path,
        raw_yaml={},
        job_hash="hash123",
        input=tmp_path / "list.csv",
        output_log=tmp_path / "out.log",
        output_csv=tmp_path / "out.csv",
    )
    parts_dir = tmp_path / "parts"

    # 1. Fresh run initialization
    run_id = init_parts_dir(parts_dir, job, total_rows=10, parallel=5)
    assert len(run_id) > 10

    info = get_run_info(parts_dir)
    assert info["run_id"] == run_id
    assert info["phase"] == "invoke"

    # Update phase
    update_run_phase(parts_dir, "collect")
    assert get_run_info(parts_dir)["phase"] == "collect"

    # 2. Write part with ERROR:still_running
    row = make_test_row(index=1, name="vm-01")
    logger = VMLogger(row.vm_name, row.index)
    write_part_files(
        parts_dir=parts_dir,
        row=row,
        total_rows=10,
        row_values=["vm-01", "rg", "sub", "os", "ERROR:still_running"],
        logger=logger,
    )

    # is_part_finished MUST return False because it holds ERROR:still_running!
    assert is_part_finished(parts_dir, row, total_rows=10) is False
    assert is_part_still_running(parts_dir, row, total_rows=10) is True

    # Overwrite part with successful OK values
    write_part_files(
        parts_dir=parts_dir,
        row=row,
        total_rows=10,
        row_values=["vm-01", "rg", "sub", "os", "22102026"],
        logger=logger,
    )
    assert is_part_finished(parts_dir, row, total_rows=10) is True
    assert is_part_still_running(parts_dir, row, total_rows=10) is False

