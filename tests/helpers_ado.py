"""Test fixtures and helpers for ADO export, explain, and trace tests (FX-1..FX-4)."""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Union

import y2shared.dispatcher
from y2shared.profile import Profile

ENGINE_TRACE_RE = re.compile(
    r"^(\[[^\]]+\] )?\[az\] engine:(?P<kind>sub-resolve|power-state|vm-start|invoke|reader|deallocate|powerOff) "
    r"attempt=(?P<n>\d+) rc=(?P<rc>-?\d+|timeout) "
    r"status=(?P<status>SUCCESS|STILL_RUNNING|retry:(throttled|busy|unknown)|ERROR:\S+) "
    r"dur=(?P<dur>[0-9.]+)s :: (?P<argv>.+)$"
)

SCRIPT_TRACE_RE = re.compile(
    r"^(\[[^\]]+\] )?\[az\] script:(?P<section>precheck|column|post_run):(?P<name>\S+) "
    r"rc=(?P<rc>-?\d+) :: (?P<argv>.+)$"
)


def parse_engine_trace_line(line: str) -> Optional[Dict[str, Any]]:
    m = ENGINE_TRACE_RE.match(line.strip())
    if not m:
        return None
    d = m.groupdict()
    d["n"] = int(d["n"])
    d["rc"] = d["rc"] if d["rc"] == "timeout" else int(d["rc"])
    d["dur"] = float(d["dur"])
    d["argv_list"] = shlex.split(d["argv"])
    return d


def parse_script_trace_line(line: str) -> Optional[Dict[str, Any]]:
    m = SCRIPT_TRACE_RE.match(line.strip())
    if not m:
        return None
    d = m.groupdict()
    d["rc"] = int(d["rc"])
    d["argv_list"] = shlex.split(d["argv"])
    return d


def parse_az_trace_lines(text: str) -> List[Dict[str, Any]]:
    """Parse all engine and script az trace lines from execution log text."""
    current_vm = None
    results: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        vm_m = re.match(r"^VM:\s+(\S+)", line)
        if vm_m:
            current_vm = vm_m.group(1)
        if "[az]" not in line:
            continue
        eng = parse_engine_trace_line(line)
        if eng:
            eng["vm"] = current_vm
            eng["engine_kind"] = eng["kind"]
            eng["kind"] = "engine"
            eng["argv_raw"] = eng["argv"]
            eng["argv"] = eng["argv_list"]
            results.append(eng)
            continue
        sc = parse_script_trace_line(line)
        if sc:
            sc["vm"] = current_vm
            sc["kind"] = "script"
            sc["argv_raw"] = sc["argv"]
            sc["argv"] = sc["argv_list"]
            results.append(sc)
            continue
    return results


def parse_events(events_path: Union[Path, str]) -> List[Dict[str, Any]]:
    """Parse JSON lines from FAKE_AZ_EVENTS."""
    p = Path(events_path)
    if not p.exists():
        return []
    lines = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    events: List[Dict[str, Any]] = []
    for ln in lines:
        try:
            events.append(json.loads(ln))
        except Exception:
            pass
    return events


def classify_argv(argv: List[str]) -> str:
    """Classify an az argv list into one of the engine kinds or other."""
    if argv and argv[0] == "az":
        argv = argv[1:]
    if len(argv) >= 2 and argv[0] == "account" and argv[1] == "show":
        return "sub-resolve"
    if "vm" in argv and "get-instance-view" in argv:
        return "power-state"
    if "vm" in argv and "start" in argv:
        return "vm-start"
    if argv and argv[0] == "rest":
        for i, a in enumerate(argv):
            if a == "--url" and i + 1 < len(argv):
                url = argv[i + 1]
                if "/deallocate" in url:
                    return "deallocate"
                if "/powerOff" in url:
                    return "powerOff"
                if "/runCommand" in url:
                    return "invoke"
    if "run-command" in argv and "invoke" in argv:
        if "--no-wait" in argv:
            return "invoke"
        return "reader"
    return "other"


def normalise_argv(argv: List[str]) -> List[str]:
    """Normalise an argv list by replacing @path with @basename and placeholder IDs."""
    out = []
    for a in argv:
        if a.startswith("@"):
            base = os.path.basename(a[1:])
            out.append(f"@{base}")
        elif a == "11111111-2222-3333-4444-555555555555":
            out.append("<id-of:Prod Subscription>")
        elif "/subscriptions/11111111-2222-3333-4444-555555555555/" in a:
            out.append(a.replace("11111111-2222-3333-4444-555555555555", "<id-of:Prod Subscription>"))
        else:
            out.append(a)
    return out


def normalise_console_output(stdout: str) -> str:
    """Normalise dynamic tokens (timestamps, run-ids, paths) in console stdout."""
    lines = [line for line in stdout.splitlines() if not line.startswith("conformance:")]
    text = "\n".join(lines)
    text = re.sub(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?",
        "<TIMESTAMP>",
        text,
    )
    text = re.sub(r"\d{8}T\d{6}Z-[0-9a-f]{8}", "<RUN_ID>", text)
    text = re.sub(r"(Output (?:CSV|Log):\s+)\S+/(result\.(?:csv|log))", r"\1<DIR>/\2", text)
    return text.strip() + "\n"



@contextlib.contextmanager
def fast_backoff(timeout: Optional[float] = None) -> Iterator[None]:
    """Wraps y2shared.dispatcher._az_run_command with zero backoff and sleep."""
    orig = y2shared.dispatcher._az_run_command

    def patched(cmd: List[str], timeout_arg: int = 30, **kwargs: Any) -> Any:
        kwargs["delay_multiplier"] = 0.0
        kwargs["sleep_fn"] = lambda s: None
        passed_timeout = kwargs.pop("timeout", timeout_arg)
        eff_timeout = timeout if timeout is not None else passed_timeout
        return orig(cmd, timeout=eff_timeout, **kwargs)

    y2shared.dispatcher._az_run_command = patched
    try:
        yield
    finally:
        y2shared.dispatcher._az_run_command = orig


def run_cli(
    tool_or_profile: Union[str, Profile],
    argv: List[str],
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[Union[Path, str]] = None,
) -> subprocess.CompletedProcess[str]:
    """Run y2b/y2p cli in a subprocess with fast_backoff applied and fake_az on PATH."""
    repo_root = Path(__file__).resolve().parent.parent
    fake_az_dir = repo_root / "tests" / "fake_az"

    sub_env = dict(os.environ)
    if env:
        sub_env.update(env)

    # Ensure fake_az is first on PATH
    cur_path = sub_env.get("PATH", "")
    sub_env["PATH"] = f"{fake_az_dir}:{cur_path}"

    # Ensure repo_root is on PYTHONPATH
    cur_pythonpath = sub_env.get("PYTHONPATH", "")
    sub_env["PYTHONPATH"] = f"{repo_root}:{cur_pythonpath}" if cur_pythonpath else str(repo_root)

    tool_name = (
        tool_or_profile if isinstance(tool_or_profile, str) else tool_or_profile.tool
    )

    prelude = (
        "import y2shared.dispatcher as d; "
        "orig = d._az_run_command; "
        "d._az_run_command = lambda cmd, timeout=30, **kw: orig(cmd, timeout=timeout, **dict(kw, delay_multiplier=0.0, sleep_fn=lambda s: None)); "
        "import y2shared.cli as c; "
        "from y2b.profile import PROFILE as b_prof; "
        "from y2p.profile import PROFILE as p_prof; "
        "import sys; "
        "prof = b_prof if sys.argv[1] == 'y2b' else p_prof; "
        "sys.exit(c.main(prof, sys.argv[2:]))"
    )

    cmd = [sys.executable, "-c", prelude, tool_name] + list(argv)
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else str(repo_root),
        env=sub_env,
        capture_output=True,
        text=True,
    )


def make_fleet(
    tmp_path: Path,
    n: int = 3,
    os_type: str = "linux",
    sub: str = "GUID",
    states: Optional[Dict[str, str]] = None,
    prechecks: Optional[List[Dict[str, Any]]] = None,
    post_run: Optional[List[Dict[str, Any]]] = None,
    variant: str = "a",
    computed_az: bool = False,
    res_file: Optional[str] = None,
    log_file: Optional[str] = None,
    sleep_before_collect: float = 1.0,
    collect_retry_interval: float = 1.0,
    collect_retries: int = 1,
    sentinel_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """Generate a fleet fixture (job.yaml, list_vm.csv, scripts/)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    default_sub_guid = "79f7da80-0c64-4e4b-9ed4-316a88142152"
    default_sub_name = "Prod Subscription"
    rg_name = "rg-lab-10vms"

    # 1. Build list_vm.csv
    csv_rows = ["subscription,resource_group,vm_name,os"]
    vm_names = []
    prefix = "lin" if os_type == "linux" else "win"
    os_val = "rhel9" if os_type == "linux" else "windows2022"
    for i in range(1, n + 1):
        vname = f"{prefix}{i:02d}" if n <= 99 else f"vm-{i:04d}"
        vm_names.append(vname)
        if sub == "GUID":
            sub_val = default_sub_guid
        elif sub == "name":
            sub_val = default_sub_name
        elif sub == "mixed":
            sub_val = default_sub_guid if i % 2 == 1 else default_sub_name
        else:
            sub_val = sub
        csv_rows.append(f"{sub_val},{rg_name},{vname},{os_val}")

    csv_path = tmp_path / "list_vm.csv"
    csv_path.write_text("\n".join(csv_rows) + "\n", encoding="utf-8")

    # Set initial states if provided
    if states:
        state_dir = os.environ.get("FAKE_AZ_STATE_DIR", "/tmp/fake_az_state")
        os.makedirs(state_dir, exist_ok=True)
        for vm, st in states.items():
            with open(os.path.join(state_dir, f"{vm}_state.txt"), "w", encoding="utf-8") as f:
                f.write(st)

    # 2. Build steps & scripts based on variant
    is_windows = os_type == "windows"
    script_ext = "ps1" if is_windows else "sh"
    step1_script = (
        scripts_dir / f"step1.{script_ext}"
    )
    if is_windows:
        step1_script.write_text(
            'write_result STEP_STATUS "OK"\nwrite_result OS_BUILD "20348"\n',
            encoding="utf-8",
        )
    else:
        step1_script.write_text(
            'write_result STEP_STATUS "OK"\nwrite_result KERNEL_COUNT "3"\n',
            encoding="utf-8",
        )

    # Handle prechecks
    resolved_prechecks = []
    if prechecks is not None:
        resolved_prechecks = prechecks
    elif variant == "a":
        # Reads state file directly without az
        chk_sh = scripts_dir / "check_state.sh"
        chk_sh.write_text(
            '#!/bin/bash\n'
            'STATE_FILE="${FAKE_AZ_STATE_DIR:-/tmp/fake_az_state}/${VM_NAME}_state.txt"\n'
            'if [ -f "$STATE_FILE" ]; then\n'
            '  cat "$STATE_FILE"\n'
            'else\n'
            '  echo "PowerState/running"\n'
            'fi\n',
            encoding="utf-8",
        )
        resolved_prechecks = [
            {
                "name": "check-vm-state",
                "phase": ["invoke"],
                "timeout": 5,
                "script_path": "./scripts/check_state.sh",
                "result": {
                    "PowerState/running": {"log": "running", "action": "continue"},
                    "PowerState/deallocated": {"log": "deallocated", "action": "start-stop"},
                    "PowerState/stopped": {"log": "stopped", "action": "start-stop"},
                    "default": {"log": "default", "action": "continue"},
                },
            }
        ]
    elif variant == "b":
        # Calls az in precheck
        chk_sh = scripts_dir / "check_with_az.sh"
        chk_sh.write_text(
            '#!/bin/bash\n'
            'az account show --subscription "$SUBSCRIPTION" >/dev/null 2>&1\n'
            'echo "PowerState/running"\n',
            encoding="utf-8",
        )
        resolved_prechecks = [
            {
                "name": "check-with-az",
                "phase": ["invoke"],
                "timeout": 5,
                "script_path": "./scripts/check_with_az.sh",
                "result": {
                    "PowerState/running": {"log": "running", "action": "continue"},
                    "default": {"log": "default", "action": "continue"},
                },
            }
        ]
    elif variant == "c":
        # All actions job
        sub_chk = scripts_dir / "sub_check.sh"
        sub_chk.write_text(
            '#!/bin/bash\n'
            'echo "in_tenant"\n',
            encoding="utf-8",
        )
        power_chk = scripts_dir / "all_power_check.sh"
        power_chk.write_text(
            '#!/bin/bash\n'
            'case "$VM_NAME" in\n'
            '  *01) echo "PowerState/running" ;;\n'
            '  *02) echo "PowerState/deallocated" ;;\n'
            '  *03) echo "skip_me" ;;\n'
            '  *04) echo "end_me" ;;\n'
            '  *05) echo "stop_me" ;;\n'
            '  *06) echo "start_me" ;;\n'
            '  *) echo "unknown_state" ;;\n'
            'esac\n',
            encoding="utf-8",
        )
        resolved_prechecks = [
            {
                "name": "tenant-check",
                "phase": ["invoke"],
                "cache": "subscription",
                "timeout": 5,
                "script_path": "./scripts/sub_check.sh",
                "result": {
                    "in_tenant": {"log": "in_tenant", "action": "continue"},
                    "default": {"log": "default", "action": "end_check"},
                },
            },
            {
                "name": "power-actions-check",
                "phase": ["invoke"],
                "timeout": 5,
                "script_path": "./scripts/all_power_check.sh",
                "result": {
                    "PowerState/running": {"log": "running", "action": "continue"},
                    "PowerState/deallocated": {"log": "deallocated", "action": "start-stop"},
                    "start_me": {"log": "start", "action": "start"},
                    "stop_me": {"log": "stop", "action": "stop"},
                    "skip_me": {"log": "skip", "action": "skip"},
                    "end_me": {"log": "end", "action": "end_check"},
                    "default": {"log": "default", "action": "continue"},
                },
            },
        ]
    elif variant in ("d1", "d2"):
        sfile = sentinel_file or (tmp_path / "sentinel.txt")
        inv_sh = scripts_dir / "inv_power.sh"
        inv_sh.write_text(
            f'#!/bin/bash\n'
            f'echo "invoke:$VM_NAME" >> "{sfile}"\n'
            f'STATE_FILE="${{FAKE_AZ_STATE_DIR:-/tmp/fake_az_state}}/${{VM_NAME}}_state.txt"\n'
            f'if [ -f "$STATE_FILE" ]; then cat "$STATE_FILE"; else echo "PowerState/running"; fi\n',
            encoding="utf-8",
        )
        col_sh = scripts_dir / "col_power.sh"
        col_sh.write_text(
            f'#!/bin/bash\n'
            f'echo "collect:$VM_NAME" >> "{sfile}"\n'
            f'STATE_FILE="${{FAKE_AZ_STATE_DIR:-/tmp/fake_az_state}}/${{VM_NAME}}_state.txt"\n'
            f'if [ -f "$STATE_FILE" ]; then cat "$STATE_FILE"; else echo "PowerState/running"; fi\n',
            encoding="utf-8",
        )
        col_action = "start-stop" if variant == "d2" else "continue"
        resolved_prechecks = [
            {
                "name": "inv-power",
                "phase": ["invoke"],
                "timeout": 5,
                "script_path": "./scripts/inv_power.sh",
                "result": {
                    "PowerState/deallocated": {"log": "deallocated", "action": "start-stop"},
                    "PowerState/running": {"log": "running", "action": "continue"},
                    "default": {"log": "default", "action": "continue"},
                },
            },
            {
                "name": "col-power",
                "phase": ["collect"],
                "timeout": 5,
                "script_path": "./scripts/col_power.sh",
                "result": {
                    "PowerState/deallocated": {"log": "deallocated", "action": col_action},
                    "PowerState/running": {"log": "running", "action": "continue"},
                    "default": {"log": "default", "action": "continue"},
                },
            },
        ]

    # Handle post_run
    resolved_post_run = []
    if post_run is not None:
        resolved_post_run = post_run
    elif variant == "b":
        post_sh = scripts_dir / "post_hook.sh"
        post_sh.write_text(
            '#!/bin/bash\n'
            'az account show --subscription "$SUBSCRIPTION" >/dev/null 2>&1\n'
            'echo ok\n',
            encoding="utf-8",
        )
        resolved_post_run = [
            {
                "name": "audit-hook",
                "timeout": 5,
                "script_path": "./scripts/post_hook.sh",
                "result": {
                    "ok": {"log": "Audit ok"},
                },
            }
        ]

    # Computed columns
    collect_list: List[Dict[str, Any]] = [
        {"column": "status", "var": "STEP_STATUS"},
    ]
    if variant == "b" or computed_az:
        comp_sh = scripts_dir / "computed_col.sh"
        comp_sh.write_text(
            '#!/bin/bash\n'
            'az account show --subscription "$SUBSCRIPTION" >/dev/null 2>&1\n'
            'echo "verified"\n',
            encoding="utf-8",
        )
        collect_list.append(
            {
                "column": "verdict",
                "script_path": "./scripts/computed_col.sh",
            }
        )

    # 3. Assemble job dict
    default_res = (
        r"C:\Windows\Temp\y2_test.res" if is_windows else "/tmp/y2_test.res"
    )
    default_log = (
        r"C:\Windows\Temp\y2_test.log" if is_windows else "/tmp/y2_test.log"
    )
    job_dict: Dict[str, Any] = {
        "input": "./list_vm.csv",
        "output_csv": "./result.csv",
        "output_log": "./result.log",
        "sleep_before_collect": f"{sleep_before_collect}s",
        "collect_retry_interval": f"{collect_retry_interval}s",
        "collect_retries": collect_retries,
        "pre_run": resolved_prechecks,
        "invoke": {
            "runcommand": {
                "res_file": res_file or default_res,
                "log_file": log_file or default_log,
                "steps": [
                    {
                        "name": "step1",
                        "timeout": 10,
                        "script_path": f"./scripts/step1.{script_ext}",
                    }
                ],
            },
        },
        "collect": collect_list,
    }
    if resolved_post_run:
        job_dict["post_run"] = resolved_post_run

    import yaml

    job_yaml_path = tmp_path / "job.yaml"
    job_yaml_path.write_text(yaml.safe_dump(job_dict, sort_keys=False), encoding="utf-8")

    return {
        "dir": tmp_path,
        "job_path": job_yaml_path,
        "job_yaml": job_yaml_path,
        "csv_path": csv_path,
        "scripts_dir": scripts_dir,
        "vm_names": vm_names,
    }
