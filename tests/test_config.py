"""Unit tests for config.py."""

from pathlib import Path
import pytest
from y2b.config import load_job, parse_job_from_dict, compute_content_hash


def test_load_example_job():
    job_path = Path("examples/hip-compliance/job.yaml")
    job = load_job(job_path)

    assert job.yaml_path.resolve() == job_path.resolve()
    assert job.yaml_dir == job_path.resolve().parent
    assert job.input == (job.yaml_dir / "list_vm.csv").resolve()
    assert job.output_csv == (job.yaml_dir / "result.csv").resolve()
    assert job.output_log == (job.yaml_dir / "result.log").resolve()
    assert job.actions == "skip"
    assert job.parallel == 10
    assert len(job.invoke.precheck) == 2
    assert job.invoke.precheck[0].name == "check-if-subscription-in-tenant"
    assert "0" in job.invoke.precheck[0].result
    assert job.invoke.precheck[0].result["0"]["action"] == "continue"

    assert job.invoke.runcommand.res_file == "/var/tmp/runcommand/result.res"
    assert job.invoke.runcommand.log_file == "/var/tmp/runcommand/result.log"
    assert len(job.invoke.runcommand.steps) == 3
    assert job.invoke.runcommand.steps[0].var == "HIP_VERSION"
    assert job.invoke.runcommand.steps[2].script_path == (job.yaml_dir / "scripts/audit_hardware.sh").resolve()

    assert len(job.collect) == 8
    assert job.collect[0].column == "vm_name"
    assert job.collect[0].source == "input.vm_name"
    assert job.collect[4].column == "hip_version"
    assert job.collect[4].var == "HIP_VERSION"
    assert job.collect[6].column == "recommendation"
    assert "EXPECTED" in job.collect[6].script
    assert job.collect[7].column == "extended_compliance"
    assert job.collect[7].script_path == (job.yaml_dir / "scripts/calc_compliance.sh").resolve()


def test_compute_content_hash():
    h1 = compute_content_hash("hello")
    h2 = compute_content_hash("hello")
    h3 = compute_content_hash("world")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64

