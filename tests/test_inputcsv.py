"""Unit tests for inputcsv.py."""

from pathlib import Path
import pytest
from y2shared.config import load_job
from y2shared.inputcsv import read_input_csv, to_env_var_name, is_linux_os, os_family


def test_env_var_name():
    assert to_env_var_name("resource_group") == "RESOURCE_GROUP"
    assert to_env_var_name("vm-name") == "VM_NAME"
    assert to_env_var_name("123col") == "_123COL"


def test_is_linux_os():
    assert is_linux_os("rhel8") is True
    assert is_linux_os("Ubuntu-22.04") is True
    assert is_linux_os("CentOS 7") is True
    assert is_linux_os("rocky9") is True
    assert is_linux_os("windows") is False
    assert is_linux_os("Win11") is False
    assert is_linux_os("WindowsServer2022") is False


def test_valid_example_csv():
    job = load_job("examples/hip-compliance/job.yaml")
    rows, headers, res = read_input_csv("examples/hip-compliance/list_vm.csv", job=job)
    assert res.is_valid
    assert len(rows) == 2
    assert headers == ["vm_name", "resource_group", "subscription", "os"]
    assert rows[0].vm_name == "vm-app-01"
    assert rows[0].env["RESOURCE_GROUP"] == "rg-prod-eastus"
    assert rows[0].part_prefix(width=4) == "0001-vm-app-01"
    assert rows[1].part_prefix(width=4) == "0002-vm-app-02"


def test_csv_missing_headers(tmp_path):
    csv_file = tmp_path / "bad.csv"
    csv_file.write_text("vm_name,os\nvm-01,rhel8\n", encoding="utf-8")
    rows, headers, res = read_input_csv(csv_file)
    assert not res.is_valid
    assert any("Missing required columns in CSV header" in str(e) for e in res.errors)


def test_csv_windows_os_rejected(tmp_path):
    csv_file = tmp_path / "win.csv"
    csv_file.write_text("vm_name,resource_group,subscription,os\nvm-win,rg1,sub1,WindowsServer\n", encoding="utf-8")
    rows, headers, res = read_input_csv(csv_file)
    assert not res.is_valid
    assert any("non-Linux OS 'WindowsServer'" in str(e) for e in res.errors)


def test_csv_duplicate_vms(tmp_path):
    csv_file = tmp_path / "dup.csv"
    csv_file.write_text(
        "vm_name,resource_group,subscription,os\n"
        "vm-01,rg1,sub1,rhel8\n"
        "vm-01,rg1,sub1,rhel8\n",
        encoding="utf-8"
    )
    rows, headers, res = read_input_csv(csv_file)
    assert not res.is_valid
    assert any("Duplicate VM" in str(e) for e in res.errors)


def test_csv_missing_collect_source_field(tmp_path):
    job = load_job("examples/hip-compliance/job.yaml")
    # CSV has required columns but is missing a column that collect expects if collect had input.custom_field
    csv_file = tmp_path / "test.csv"
    csv_file.write_text(
        "vm_name,resource_group,subscription,os\n"
        "vm-01,rg1,sub1,rhel8\n",
        encoding="utf-8"
    )
    # job expects vm_name, resource_group, subscription, os, which are all present
    _, _, res = read_input_csv(csv_file, job=job)
    assert res.is_valid

    # Now let's modify job to expect input.non_existent
    job.collect[0].source = "input.non_existent"
    _, _, res2 = read_input_csv(csv_file, job=job)
    assert not res2.is_valid
    assert any("Field 'non_existent' from 'input.non_existent' not found in CSV header" in str(e) for e in res2.errors)

