"""Unit tests for config.py."""

from pathlib import Path
import pytest
from y2shared.config import load_job, parse_job_from_dict, compute_content_hash, parse_duration


def test_load_example_job():
    job_path = Path("examples/linux/job.yaml")
    job = load_job(job_path)

    assert job.yaml_path.resolve() == job_path.resolve()
    assert job.yaml_dir == job_path.resolve().parent
    assert job.input == (job.yaml_dir / "list_vm.csv").resolve()
    assert job.output_csv == (job.yaml_dir / "result.csv").resolve()
    assert job.output_log == (job.yaml_dir / "result.log").resolve()
    assert job.actions == "start-stop"
    assert job.parallel == 5
    assert job.sleep_before_collect == 15.0
    assert job.collect_retry_interval == 10.0
    assert job.collect_retries == 3
    assert len(job.pre_run) == 2
    assert job.pre_run[0].name == "check-if-subscription-in-tenant"
    assert "0" in job.pre_run[0].result
    assert job.pre_run[0].result["0"]["action"] == "continue"

    assert job.invoke.runcommand.res_file == "/var/tmp/runcommand/result.res"
    assert job.invoke.runcommand.log_file == "/var/tmp/runcommand/result.log"
    assert len(job.invoke.runcommand.steps) == 5
    assert job.invoke.runcommand.steps[0].name == "check-os-release"
    assert job.invoke.runcommand.steps[1].name == "check-kernel-version"
    assert job.invoke.runcommand.steps[2].name == "check-uptime-and-load"
    assert job.invoke.runcommand.steps[3].name == "check-disk-usage"
    assert job.invoke.runcommand.steps[4].script_path == (job.yaml_dir / "scripts/audit_hardware.sh").resolve()

    assert len(job.collect) == 12
    assert job.collect[0].column == "vm_name"
    assert job.collect[0].source == "input.vm_name"
    assert job.collect[4].column == "os_name"
    assert job.collect[4].var == "OS_NAME"
    assert job.collect[5].column == "os_version"
    assert job.collect[5].var == "OS_VERSION"
    assert job.collect[6].column == "kernel_version"
    assert job.collect[6].var == "KERNEL_VERSION"
    assert job.collect[7].column == "uptime_secs"
    assert job.collect[7].var == "UPTIME_SECS"
    assert job.collect[8].column == "load_1m"
    assert job.collect[8].var == "LOAD_1M"
    assert job.collect[9].column == "root_disk_usage"
    assert job.collect[9].var == "ROOT_DISK_USAGE"
    assert job.collect[10].column == "hw_audit_status"
    assert job.collect[10].var == "HW_AUDIT_STATUS"
    assert job.collect[11].column == "verdict"

    assert len(job.post_run) == 1
    assert job.post_run[0].name == "log-audit-summary"


def test_parse_duration():
    assert parse_duration(90) == 90.0
    assert parse_duration("90") == 90.0
    assert parse_duration("30s") == 30.0
    assert parse_duration("5m") == 300.0
    assert parse_duration("1h") == 3600.0
    assert parse_duration("1m30s") == 90.0
    assert parse_duration("0") == 0.0
    assert parse_duration(0) == 0.0

    with pytest.raises(ValueError):
        parse_duration("5x")
    with pytest.raises(ValueError):
        parse_duration("")
    with pytest.raises(ValueError):
        parse_duration(-1)
    with pytest.raises(ValueError):
        parse_duration("-1s")
    with pytest.raises(ValueError):
        parse_duration(True)


def test_compute_content_hash():
    h1 = compute_content_hash("hello")
    h2 = compute_content_hash("hello")
    h3 = compute_content_hash("world")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64

