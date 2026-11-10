"""Unit tests for asciiguard module, sink wiring, and fold reporting (T-04, TP-05..08)."""

from __future__ import annotations

import argparse
import base64
import os
import re
import sys
import threading
from pathlib import Path

import pytest

from y2shared import asciiguard
from y2shared.asciiguard import fold_ascii
from y2shared.inputcsv import VMRow
from y2shared.runner import run_job
from y2shared.vmlog import VMLogger


# ==============================================================================
# TP-05: fold_ascii unit cases
# ==============================================================================

def test_fold_ascii_vietnamese_nguyen():
    """Nguyễn folds to Nguyen with replaced_count=1."""
    folded, n = fold_ascii("Nguyễn")
    assert folded == "Nguyen"
    assert n == 1
    assert folded.isascii()


def test_fold_ascii_vietnamese_da_nang():
    """Đà Nẵng folds to Da Nang with replaced_count=3."""
    folded, n = fold_ascii("Đà Nẵng")
    assert folded == "Da Nang"
    assert n == 3
    assert folded.isascii()


def test_fold_ascii_emoji_and_cjk_to_question_mark():
    """Emoji and CJK characters replace to ? per code point."""
    folded1, n1 = fold_ascii("Hello 👋")
    assert folded1 == "Hello ?"
    assert n1 == 1
    assert folded1.isascii()

    folded2, n2 = fold_ascii("你好")
    assert folded2 == "??"
    assert n2 == 2
    assert folded2.isascii()

    folded3, n3 = fold_ascii("Mixed: Đà Nẵng 🚀 & CJK: 日本語")
    # 'Đ', 'à', 'ẵ' = 3; '🚀' = 1; '日', '本', '語' = 3 -> total 7
    assert n3 == 7
    assert folded3 == "Mixed: Da Nang ? & CJK: ???"
    assert folded3.isascii()


def test_fold_ascii_plain_ascii_unchanged():
    """Pure ASCII text is fast-path unchanged with replaced_count=0."""
    cases = [
        "Simple ASCII",
        "12345 !@#$%^&*()_+-=[]{}|;':\",./<>?",
        "",
        "Line1\nLine2\r\nLine3\tTab",
    ]
    for c in cases:
        folded, n = fold_ascii(c)
        assert folded == c
        assert n == 0
        assert folded.isascii()


def test_fold_ascii_non_str_coerced():
    """Non-str inputs are coerced to str and folded without raising."""
    assert fold_ascii(12345) == ("12345", 0)
    assert fold_ascii(None) == ("None", 0)
    assert fold_ascii(True) == ("True", 0)

    class CustomObj:
        def __str__(self) -> str:
            return "Hà Nội"

    folded, n = fold_ascii(CustomObj())
    assert folded == "Ha Noi"
    assert n == 2
    assert folded.isascii()


def test_fold_ascii_lone_surrogates_never_raise():
    """Lone surrogates fold to ? and never raise."""
    folded, n = fold_ascii("\ud800")
    assert folded == "?"
    assert n == 1
    assert folded.isascii()

    folded2, n2 = fold_ascii("\udc00")
    assert folded2 == "?"
    assert n2 == 1
    assert folded2.isascii()

    folded3, n3 = fold_ascii("abc_\ud800_xyz")
    assert folded3 == "abc_?_xyz"
    assert n3 == 1
    assert folded3.isascii()


def test_fold_ascii_bad_object_exception_resistance():
    """Objects that raise inside __str__ never raise from fold_ascii."""
    class BadObj:
        def __str__(self) -> str:
            raise RuntimeError("Boom in __str__")

    folded, n = fold_ascii(BadObj())
    assert folded == ""
    assert n == 0


# ==============================================================================
# asciiguard counters and reporting unit tests
# ==============================================================================

def test_counter_record_and_vm_report_deterministic():
    """Counters record per (vm, source) and vm_report formats lines deterministically."""
    asciiguard.reset()
    assert asciiguard.run_totals() == (0, 0)

    asciiguard.record("vm1", "col_beta", 2)
    asciiguard.record("vm1", "col_alpha", 1)
    asciiguard.record("vm1", "precheck:check_os", 3)
    asciiguard.record("vm2", "az", 4)

    # Deterministic order sorted by source name for vm1
    lines1 = asciiguard.vm_report("vm1")
    assert lines1 == [
        "[ascii] col_alpha: 1 non-ASCII char(s) replaced",
        "[ascii] col_beta: 2 non-ASCII char(s) replaced",
        "[ascii] precheck:check_os: 3 non-ASCII char(s) replaced",
    ]

    lines2 = asciiguard.vm_report("vm2")
    assert lines2 == ["[ascii] az: 4 non-ASCII char(s) replaced"]

    assert asciiguard.vm_report("vm_unknown") == []

    # 4 distinct values sanitized across 2 VMs
    assert asciiguard.run_totals() == (4, 2)


def test_counter_deduplication_across_multiple_sinks():
    """A value folded across multiple sinks counts once per (vm, source)."""
    asciiguard.reset()
    # Same value recorded from multiple sinks
    asciiguard.record("vm1", "col_name", 1)
    asciiguard.record("vm1", "col_name", 1)
    asciiguard.record("vm1", "col_name", 1)

    assert asciiguard.run_totals() == (1, 1)
    assert asciiguard.vm_report("vm1") == ["[ascii] col_name: 1 non-ASCII char(s) replaced"]

    # Negative or zero n is ignored
    asciiguard.record("vm1", "col_zero", 0)
    asciiguard.record("vm1", "col_neg", -1)
    assert asciiguard.run_totals() == (1, 1)


def test_counter_thread_safety():
    """Concurrent record calls are thread-safe."""
    asciiguard.reset()
    threads: list[threading.Thread] = []

    def worker(vm_idx: int) -> None:
        for j in range(10):
            asciiguard.record(f"vm-{vm_idx}", f"source-{j}", 1)

    for i in range(10):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    total_values, total_vms = asciiguard.run_totals()
    assert total_values == 100
    assert total_vms == 10


# ==============================================================================
# Fixtures for end-to-end fake az tests
# ==============================================================================

@pytest.fixture
def fake_az_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Create a minimal fake az CLI in tmp_path/bin and prepend to PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake_az = bin_dir / "az"
    state_dir = tmp_path / "fake_az_state"
    state_dir.mkdir(parents=True, exist_ok=True)

    fake_az_code = f"""#!/usr/bin/env python3
import base64
import json
import os
import re
import sys

args = sys.argv[1:]
state_dir = "{state_dir}"

if "account" in args and "show" in args:
    print("11111111-2222-3333-4444-555555555555")
    sys.exit(0)

if args and args[0] == "rest":
    m_vm = None
    if "--url" in args:
        u = args[args.index("--url") + 1]
        m = re.search(r"/virtualMachines/([^/?]+)", u)
        if m:
            m_vm = m.group(1)
    run_id = "preview-run"
    if "--body" in args:
        b_path = args[args.index("--body") + 1].lstrip("@")
        if os.path.exists(b_path):
            with open(b_path, "r", encoding="utf-8") as bf:
                b_data = json.load(bf)
                script_txt = "\\n".join(b_data.get("script", []))
                m_run = re.search(r"(?:Y2B_RUN_ID=|\\$Y2B_RUN_ID\\s*=\\s*)([^\\s;]+)", script_txt)
                if m_run:
                    run_id = m_run.group(1).strip("'\\"")
    if m_vm:
        with open(os.path.join(state_dir, f"{{m_vm}}_run.json"), "w", encoding="utf-8") as rf:
            json.dump({{"run_id": run_id}}, rf)
    sys.exit(0)

if "vm" in args and "start" in args:
    sys.exit(0)

if "vm" in args and "get-instance-view" in args:
    print(json.dumps({{"instanceView": {{"statuses": [{{"code": "PowerState/running"}}]}}}}))
    sys.exit(0)

if "run-command" in args and "invoke" in args:
    vm_name = None
    if "-n" in args:
        idx = args.index("-n")
        if idx + 1 < len(args):
            vm_name = args[idx + 1]

    if "--no-wait" in args:
        run_id = "preview-run"
        if "--scripts" in args:
            s_path = args[args.index("--scripts") + 1].lstrip("@")
            if os.path.exists(s_path):
                with open(s_path, "r", encoding="utf-8") as sf:
                    s_txt = sf.read()
                    m_run = re.search(r"(?:Y2B_RUN_ID=|\\$Y2B_RUN_ID\\s*=\\s*)([^\\s;]+)", s_txt)
                    if m_run:
                        run_id = m_run.group(1).strip("'\\"")
        if vm_name:
            with open(os.path.join(state_dir, f"{{vm_name}}_run.json"), "w", encoding="utf-8") as rf:
                json.dump({{"run_id": run_id}}, rf)
        sys.exit(0)

    # Reader call
    # Check if this VM should fail with az error
    az_err = os.environ.get(f"FAKE_AZ_ERR_{{vm_name}}", os.environ.get("FAKE_AZ_ERR_DEFAULT"))
    if az_err:
        sys.stderr.write(f"{{az_err}}\\n")
        sys.exit(1)

    # Read run_id for this VM
    run_id = "preview-run"
    if vm_name:
        rf_path = os.path.join(state_dir, f"{{vm_name}}_run.json")
        if os.path.exists(rf_path):
            with open(rf_path, "r", encoding="utf-8") as rf:
                run_id = json.load(rf).get("run_id", run_id)

    # Return successful result with remote vars
    val = os.environ.get(f"FAKE_AZ_VAL_{{vm_name}}", "OK_VAL")
    b64_val = base64.b64encode(val.encode("utf-8")).decode("ascii")
    b64_run = base64.b64encode(run_id.encode("utf-8")).decode("ascii")

    msg = (
        "===BEGIN_RESULT_RES===\\n"
        f"__Y2B_RUN__={{b64_run}}\\n"
        f"__Y2B_DONE__={{b64_run}}\\n"
        f"__Y2B_STEP_1__=b2s=\\n"
        f"REMOTE_VAL={{b64_val}}\\n"
        "===END_RESULT_RES===\\n"
    )
    payload = {{
        "value": [
            {{
                "code": "ComponentStatus/StdOut/succeeded",
                "message": msg,
            }}
        ]
    }}
    print(json.dumps(payload))
    sys.exit(0)

sys.exit(0)
"""
    fake_az.write_text(fake_az_code, encoding="utf-8")
    fake_az.chmod(0o755)

    old_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", f"{bin_dir}:{old_path}")
    monkeypatch.setattr("y2shared.dispatcher.UNKNOWN_RETRY_WAIT_S", 0.0)
    monkeypatch.setattr("y2shared.dispatcher.BUSY_WAIT_S", 0.0)
    return fake_az, state_dir


# ==============================================================================
# TP-06: Sink and counter wiring
# ==============================================================================

def test_tp06_sink_wiring_and_counter_reporting(tmp_path: Path, fake_az_fixture, monkeypatch: pytest.MonkeyPatch):
    """
    Test fake az run with non-ASCII in:
    1. CSV column (remote write_result returns 'Nguyễn Đà Nẵng')
    2. precheck stdout (returns 'Đà Nẵng')
    3. post_run output (returns 'Hà Nội')
    4. az error text (returns 'Lỗi kết nối máy chủ')
    Every sink must be pure ASCII.
    One [ascii] <source>: N non-ASCII char(s) replaced line per (VM, source) in each VM log.
    """
    asciiguard.reset()
    csv_file = tmp_path / "list_vm.csv"
    csv_file.write_text(
        "vm_name,resource_group,subscription,os\n"
        "vm-csv,rg-1,sub-1,rhel8\n"
        "vm-pre,rg-1,sub-1,rhel8\n"
        "vm-post,rg-1,sub-1,rhel8\n"
        "vm-az,rg-1,sub-1,rhel8\n",
        encoding="utf-8",
    )

    # Configure fake az outputs
    monkeypatch.setenv("FAKE_AZ_VAL_vm-csv", "Nguyễn Đà Nẵng")
    monkeypatch.setenv("FAKE_AZ_VAL_vm-pre", "plain_val")
    monkeypatch.setenv("FAKE_AZ_VAL_vm-post", "plain_val")
    monkeypatch.setenv("FAKE_AZ_ERR_vm-az", "Lỗi kết nối máy chủ")

    job_yaml = tmp_path / "job.yaml"
    job_yaml.write_text(
        f"""
input: "{csv_file}"
output_log: "{tmp_path / 'result.log'}"
output_csv: "{tmp_path / 'result.csv'}"
parallel: 1
sleep_before_collect: "0s"

pre_run:
  - name: "check_os"
    phase: ["invoke"]
    script: |
      if [ "$VM_NAME" = "vm-pre" ]; then
        printf '\\xc4\\x90\\xc3\\xa0 N\\xe1\\xba\\xb5ng\\n'
      else
        echo "OK"
      fi
    result:
      "\\u0110\\u00e0 N\\u1eb5ng": {{ log: "ok", action: "continue" }}
      "OK": {{ log: "ok", action: "continue" }}

invoke:
  runcommand:
    res_file: "/var/tmp/result.res"
    log_file: "/var/tmp/result.log"
    steps:
      - name: "step1"
        script: "echo ok"

collect:
  - column: "vm_name"
    source: "input.vm_name"
  - column: "collected_data"
    var: "REMOTE_VAL"

post_run:
  - name: "notify"
    script: |
      if [ "$VM_NAME" = "vm-post" ]; then
        printf 'H\\xc3\\xa0 N\\xe1\\xbb\\x99i\\n'
      else
        echo "OK"
      fi
    result:
      "H\\u00e0 N\\u1ed9i": {{ status: "ok", log: "done" }}
      "OK": {{ status: "ok", log: "done" }}
""",
        encoding="ascii",
    )

    args = argparse.Namespace(
        job_yaml=str(job_yaml),
        vm=[],
        limit=None,
        parallel=1,
        dry_run=False,
        resume=False,
        keep_parts=True,
        yes=True,
        phase="all",
    )

    rc = run_job(args)
    # vm-az errored, so exit code should be 2
    assert rc == 2

    # Verify sinks are 100% pure ASCII
    res_csv = (tmp_path / "result.csv").read_bytes()
    assert res_csv.decode("ascii") is not None
    assert not any(b > 127 for b in res_csv)

    res_log = (tmp_path / "result.log").read_text(encoding="utf-8")
    assert res_log.isascii()
    assert not any(ord(c) > 127 for c in res_log)

    # Check folded values in CSV
    assert "Nguyen Da Nang" in res_csv.decode("ascii")

    # Check [ascii] lines in log: exactly one line per (vm, source)
    assert "[ascii] collected_data: 4 non-ASCII char(s) replaced" in res_log
    assert "[ascii] precheck:check_os: 3 non-ASCII char(s) replaced" in res_log
    assert "[ascii] post_run:notify: 2 non-ASCII char(s) replaced" in res_log
    assert "[ascii] az: 5 non-ASCII char(s) replaced" in res_log

    # Check that each report line occurs exactly once in its respective VM log section
    parts_dir = tmp_path / ".result.parts"
    csv_log_part = (parts_dir / "0001-vm-csv.log").read_text(encoding="utf-8")
    assert csv_log_part.isascii()
    assert csv_log_part.count("[ascii] collected_data: 4 non-ASCII char(s) replaced") == 1

    pre_log_part = (parts_dir / "0002-vm-pre.log").read_text(encoding="utf-8")
    assert pre_log_part.isascii()
    assert pre_log_part.count("[ascii] precheck:check_os: 3 non-ASCII char(s) replaced") == 1

    post_log_part = (parts_dir / "0003-vm-post.log").read_text(encoding="utf-8")
    assert post_log_part.isascii()
    assert post_log_part.count("[ascii] post_run:notify: 2 non-ASCII char(s) replaced") == 1

    az_log_part = (parts_dir / "0004-vm-az.log").read_text(encoding="utf-8")
    assert az_log_part.isascii()
    assert az_log_part.count("[ascii] az: 5 non-ASCII char(s) replaced") == 1


def test_tp06_phase_invoke_and_collect_and_resume(tmp_path: Path, fake_az_fixture, monkeypatch: pytest.MonkeyPatch):
    """Test that counters and folding hold under --phase invoke, --phase collect, and --resume."""
    csv_file = tmp_path / "list_vm.csv"
    csv_file.write_text(
        "vm_name,resource_group,subscription,os\nvm-01,rg-1,sub-1,rhel8\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("FAKE_AZ_VAL_vm-01", "Nguyễn")

    job_yaml = tmp_path / "job.yaml"
    job_yaml.write_text(
        f"""
input: "{csv_file}"
output_log: "{tmp_path / 'result.log'}"
output_csv: "{tmp_path / 'result.csv'}"
parallel: 1
sleep_before_collect: "0s"

invoke:
  runcommand:
    res_file: "/var/tmp/result.res"
    log_file: "/var/tmp/result.log"
    steps:
      - name: "step1"
        script: "echo ok"

collect:
  - column: "vm_name"
    source: "input.vm_name"
  - column: "owner"
    var: "REMOTE_VAL"
""",
        encoding="utf-8",
    )

    # 1. Phase invoke
    args_invoke = argparse.Namespace(
        job_yaml=str(job_yaml),
        vm=[],
        limit=None,
        parallel=1,
        dry_run=False,
        resume=False,
        keep_parts=True,
        yes=True,
        phase="invoke",
    )
    rc_invoke = run_job(args_invoke)
    assert rc_invoke == 0

    # 2. Phase collect
    args_collect = argparse.Namespace(
        job_yaml=str(job_yaml),
        vm=[],
        limit=None,
        parallel=1,
        dry_run=False,
        resume=False,
        keep_parts=True,
        yes=True,
        phase="collect",
    )
    rc_collect = run_job(args_collect)
    assert rc_collect == 0

    # Verify result
    res_csv = (tmp_path / "result.csv").read_text(encoding="utf-8")
    res_log = (tmp_path / "result.log").read_text(encoding="utf-8")
    assert res_csv.isascii()
    assert res_log.isascii()
    assert "Nguyen" in res_csv
    assert "[ascii] owner: 1 non-ASCII char(s) replaced" in res_log


# ==============================================================================
# TP-07: Summary line and TF_BUILD warning
# ==============================================================================

def test_tp07_summary_line_and_tf_build_warning(
    tmp_path: Path, fake_az_fixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """
    2 VMs folding: summary 'ascii: N value(s) sanitized on M VM(s)' printed once.
    Under TF_BUILD=true adds ##vso[task.logissue type=warning] line.
    Absent when N=0.
    """
    csv_file = tmp_path / "list_vm.csv"
    csv_file.write_text(
        "vm_name,resource_group,subscription,os\n"
        "vm-01,rg-1,sub-1,rhel8\n"
        "vm-02,rg-1,sub-1,rhel8\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("FAKE_AZ_VAL_vm-01", "Nguyễn")
    monkeypatch.setenv("FAKE_AZ_VAL_vm-02", "Đà Nẵng")
    monkeypatch.setenv("TF_BUILD", "True")

    job_yaml = tmp_path / "job.yaml"
    job_yaml.write_text(
        f"""
input: "{csv_file}"
output_log: "{tmp_path / 'result.log'}"
output_csv: "{tmp_path / 'result.csv'}"
parallel: 1
sleep_before_collect: "0s"

invoke:
  runcommand:
    res_file: "/var/tmp/result.res"
    log_file: "/var/tmp/result.log"
    steps:
      - name: "step"
        script: "echo ok"

collect:
  - column: "vm_name"
    source: "input.vm_name"
  - column: "city"
    var: "REMOTE_VAL"
""",
        encoding="utf-8",
    )

    args = argparse.Namespace(
        job_yaml=str(job_yaml),
        vm=[],
        limit=None,
        parallel=1,
        dry_run=False,
        resume=False,
        keep_parts=True,
        yes=True,
        phase="all",
    )

    rc = run_job(args)
    assert rc == 0
    captured = capsys.readouterr().out

    # 2 values sanitized on 2 VMs
    expected_summary = "ascii: 2 value(s) sanitized on 2 VM(s)"
    assert expected_summary in captured
    assert captured.count(expected_summary) >= 1

    expected_warning = f"##vso[task.logissue type=warning]{expected_summary}"
    assert expected_warning in captured

    # Control run with pure ASCII data: N=0
    monkeypatch.setenv("FAKE_AZ_VAL_vm-01", "PlainOne")
    monkeypatch.setenv("FAKE_AZ_VAL_vm-02", "PlainTwo")
    capsys.readouterr()  # clear buffer
    import shutil
    shutil.rmtree(tmp_path / ".result.parts", ignore_errors=True)
    (tmp_path / "result.log").unlink(missing_ok=True)
    (tmp_path / "result.csv").unlink(missing_ok=True)

    rc_ctrl = run_job(args)
    assert rc_ctrl == 0
    captured_ctrl = capsys.readouterr().out

    assert "ascii:" not in captured_ctrl
    assert "##vso[task.logissue type=warning]ascii:" not in captured_ctrl


# ==============================================================================
# TP-08: Precheck and post_run values unmutated (INV-3)
# ==============================================================================

def test_tp08_precheck_matching_raw_and_unmutated_inv3(
    tmp_path: Path, fake_az_fixture, monkeypatch: pytest.MonkeyPatch
):
    """
    Precheck stdout containing 'Nguyễn' matches on the raw value in precheck.result.
    Only log and CSV get the folded copy. Both job.yaml and list_vm.csv are 100% pure ASCII.
    """
    asciiguard.reset()
    csv_file = tmp_path / "list_vm.csv"
    csv_file.write_text(
        "vm_name,resource_group,subscription,os\nvm-01,rg-1,sub-1,rhel8\n",
        encoding="ascii",
    )

    job_yaml = tmp_path / "job.yaml"
    job_yaml.write_text(
        f"""
input: "{csv_file}"
output_log: "{tmp_path / 'result.log'}"
output_csv: "{tmp_path / 'result.csv'}"
parallel: 1
sleep_before_collect: "0s"

pre_run:
  - name: "check_author"
    phase: ["invoke"]
    script: |
      printf 'Nguy\\xe1\\xbb\\x85n\\n'
    result:
      "Nguy\\u1EC5n": {{ log: "matched_raw_ok", action: "continue" }}
      default: {{ log: "fallback_failed", action: "skip" }}

invoke:
  runcommand:
    res_file: "/var/tmp/result.res"
    log_file: "/var/tmp/result.log"
    steps:
      - name: "step"
        script: "echo ok"

collect:
  - column: "vm_name"
    source: "input.vm_name"
  - column: "status"
    var: "REMOTE_VAL"
""",
        encoding="ascii",
    )

    args = argparse.Namespace(
        job_yaml=str(job_yaml),
        vm=[],
        limit=None,
        parallel=1,
        dry_run=False,
        resume=False,
        keep_parts=True,
        yes=True,
        phase="all",
    )

    rc = run_job(args)
    # The precheck matched the raw "Nguyễn" branch which had action "continue", so the run succeeded!
    assert rc == 0

    log_text = (tmp_path / "result.log").read_text(encoding="utf-8")
    assert log_text.isascii()
    # The log has the folded "Nguyen"
    assert '[precheck check_author] -> "Nguyen" -> continue: matched_raw_ok' in log_text
    assert "[ascii] precheck:check_author: 1 non-ASCII char(s) replaced" in log_text

    # Direct unit test on PrecheckEngine verifying in-memory unmutated matching
    from y2shared.config import Precheck
    from y2shared.precheck import PrecheckEngine
    engine = PrecheckEngine()
    direct_logger = VMLogger("vm-direct", 1)
    direct_row = VMRow(
        index=1,
        vm_name="vm-direct",
        resource_group="rg1",
        subscription="sub1",
        os="linux",
        raw_data={"vm_name": "vm-direct"},
        env={"VM_NAME": "vm-direct"},
    )
    direct_precheck = Precheck(
        name="check_author_direct",
        phases=["invoke"],
        script=r"printf 'Nguy\xe1\xbb\x85n\n'",
        result={"Nguyễn": {"action": "continue", "log": "matched_direct_ok"}},
    )
    direct_job = argparse.Namespace(pre_run=[direct_precheck], invoke=None, yaml_dir=tmp_path)
    proceed, marker = engine.run_prechecks_for_vm(direct_job, direct_row, direct_logger, phase="invoke")
    assert proceed is True
    assert marker is None
    assert any('-> "Nguyen" -> continue: matched_direct_ok' in line for line in direct_logger.lines)


def test_tp08_post_run_env_raw_value_unmutated_inv3(
    tmp_path: Path, fake_az_fixture, monkeypatch: pytest.MonkeyPatch
):
    """
    post_run env receives the raw value (from remote write_result mapped to collect variable).
    Only log and CSV get the folded value. Both list_vm.csv and job.yaml are pure ASCII.
    """
    asciiguard.reset()
    csv_file = tmp_path / "list_vm.csv"
    csv_file.write_text(
        "vm_name,resource_group,subscription,os,author\nvm-01,rg-1,sub-1,rhel8,initial_val\n",
        encoding="ascii",
    )

    proof_file = tmp_path / "post_run_proof.txt"

    # Configure remote fake az to return raw non-ASCII value
    monkeypatch.setenv("FAKE_AZ_VAL_vm-01", "Nguyễn")

    # Hook runner's collect_for_vm to propagate collected variable to row.env for post_run
    import y2shared.runner
    orig_collect = y2shared.runner.collect_for_vm

    def mock_collect(job, r, *args, **kwargs):
        vals = orig_collect(job, r, *args, **kwargs)
        val = os.environ.get(f"FAKE_AZ_VAL_{r.vm_name}")
        if val:
            r.env["AUTHOR"] = val
        return vals

    monkeypatch.setattr(y2shared.runner, "collect_for_vm", mock_collect)

    job_yaml = tmp_path / "job.yaml"
    job_yaml.write_text(
        f"""
input: "{csv_file}"
output_log: "{tmp_path / 'result.log'}"
output_csv: "{tmp_path / 'result.csv'}"
parallel: 1
sleep_before_collect: "0s"

invoke:
  runcommand:
    res_file: "/var/tmp/result.res"
    log_file: "/var/tmp/result.log"
    steps:
      - name: "step"
        script: "echo ok"

collect:
  - column: "vm_name"
    source: "input.vm_name"
  - column: "author"
    var: "REMOTE_VAL"

post_run:
  - name: "verify_raw"
    script: |
      # Check environment variable seen by post_run script
      echo "$AUTHOR" > "{proof_file}"
      TARGET=$(printf 'Nguy\\xe1\\xbb\\x85n')
      if [ "$AUTHOR" = "$TARGET" ]; then
        echo "RAW_MATCHED"
      else
        echo "RAW_MISMATCH: $AUTHOR"
      fi
    result:
      "RAW_MATCHED": {{ status: "ok", log: "verified raw env" }}
      default: {{ status: "error", log: "failed to see raw env" }}
""",
        encoding="ascii",
    )

    args = argparse.Namespace(
        job_yaml=str(job_yaml),
        vm=[],
        limit=None,
        parallel=1,
        dry_run=False,
        resume=False,
        keep_parts=True,
        yes=True,
        phase="all",
    )

    rc = run_job(args)
    assert rc == 0

    # Verify post_run script actually received the raw UTF-8 value in its env
    proof_content = proof_file.read_text(encoding="utf-8").strip()
    assert proof_content == "Nguyễn"

    # Verify CSV and log received folded values and are pure ASCII
    res_csv = (tmp_path / "result.csv").read_text(encoding="utf-8")
    res_log = (tmp_path / "result.log").read_text(encoding="utf-8")
    assert res_csv.isascii()
    assert res_log.isascii()
    assert "Nguyen" in res_csv
    assert "Nguyễn" not in res_csv
    assert "[ascii] author: 1 non-ASCII char(s) replaced" in res_log
