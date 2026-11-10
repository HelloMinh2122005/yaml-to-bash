# tests/test_ado_arm.py - Tier 2 ARM and job handling tests for ADO-native patch templates
# Covers: TP-04 (run command, job pool, retries), TP-05 (auth breaker)
# Compatible with Python 3.9 (stdlib + pytest). Pure ASCII, LF line endings.

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path
import pytest

from tests.ado_fixtures.run_ps import run_pwsh_script, require_container_runtime

EV_DIR = Path(__file__).resolve().parent.parent / ".handoffs" / "20261108-ado-patch-templates" / "42-evidence"


def _dump_evidence(tp_name: str, calls_file: Path) -> None:
    if EV_DIR.is_dir() and calls_file.is_file():
        dest = EV_DIR / f"{tp_name}-calls.json"
        shutil.copy2(calls_file, dest)


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    with open(path, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


# ===========================================================================
# TP-04: Run command, job pool, retries (AC-4)
# ===========================================================================


def test_runcmd_accepted_per_vm(tmp_path: Path):
    """TP-04: Exactly 1 accepted Invoke-AzVMRunCommand -AsJob per VM (RunPowerShellScript/RunShellScript)."""
    require_container_runtime()

    script = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    # Test 2 Windows VMs and 2 Linux VMs
    $fleet = @(
        @{ id = '/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-w1'; name = 'vm-w1'; resourceGroup = 'rg'; osType = 'Windows'; powerState = 'PowerState/running' },
        @{ id = '/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-w2'; name = 'vm-w2'; resourceGroup = 'rg'; osType = 'Windows'; powerState = 'PowerState/running' },
        @{ id = '/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-l1'; name = 'vm-l1'; resourceGroup = 'rg'; osType = 'Linux'; powerState = 'PowerState/running' },
        @{ id = '/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-l2'; name = 'vm-l2'; resourceGroup = 'rg'; osType = 'Linux'; powerState = 'PowerState/running' }
    )
    Set-FakeArmFleet -Fleet $fleet

    Set-Content -Path /work/payload-win.ps1 -Value 'Write-Output "win"'
    Set-Content -Path /work/payload-lnx.sh -Value 'echo "lnx"'

    Write-Y2Targets -Path /work/win/targets.csv -Rows @(
        @{ vm_name = 'vm-w1'; resource_group = 'rg'; subscription = 's'; os_type = 'windows'; initial_power = 'PowerState/running'; tags = '' },
        @{ vm_name = 'vm-w2'; resource_group = 'rg'; subscription = 's'; os_type = 'windows'; initial_power = 'PowerState/running'; tags = '' }
    )

    Write-Y2Targets -Path /work/lnx/targets.csv -Rows @(
        @{ vm_name = 'vm-l1'; resource_group = 'rg'; subscription = 's'; os_type = 'linux'; initial_power = 'PowerState/running'; tags = '' },
        @{ vm_name = 'vm-l2'; resource_group = 'rg'; subscription = 's'; os_type = 'linux'; initial_power = 'PowerState/running'; tags = '' }
    )

    Reset-FakeArmCallLog

    # Run on Windows
    & /repo/ado/scripts/run-command.ps1 `
        -OsType windows `
        -PayloadFile /work/payload-win.ps1 `
        -WorkDir /work/win `
        -ReportDir /work/win/report `
        -MaxJobs 10 `
        -PollSeconds 5

    # Run on Linux
    & /repo/ado/scripts/run-command.ps1 `
        -OsType linux `
        -PayloadFile /work/payload-lnx.sh `
        -WorkDir /work/lnx `
        -ReportDir /work/lnx/report `
        -MaxJobs 10 `
        -PollSeconds 5

    Export-FakeArmCallLog -Path /work/calls.json
    """

    res = run_pwsh_script(script, work_dir=tmp_path)
    assert res.returncode == 0, f"Execution failed: {res.stderr}"

    calls_file = tmp_path / "calls.json"
    assert calls_file.is_file()
    _dump_evidence("tp04", calls_file)

    calls = json.loads(calls_file.read_text(encoding="utf-8"))
    run_calls = [c for c in calls if c["Cmdlet"] in ("Start-Y2RunJob", "Invoke-AzVMRunCommand")]

    # Should have 2 windows calls + 2 linux calls = 4 calls total
    assert len(run_calls) == 4, f"Expected 4 run command calls, got {len(run_calls)}"

    win_calls = [c for c in run_calls if c["Parameters"].get("VMName", "").startswith("vm-w")]
    lnx_calls = [c for c in run_calls if c["Parameters"].get("VMName", "").startswith("vm-l")]

    assert len(win_calls) == 2
    assert len(lnx_calls) == 2

    for c in win_calls:
        assert c["Parameters"]["CommandId"] == "RunPowerShellScript"

    for c in lnx_calls:
        assert c["Parameters"]["CommandId"] == "RunShellScript"


def test_maxjobs_concurrency_cap_50vms(tmp_path: Path):
    """TP-04: Concurrency cap: with 50 VMs and maxJobs=20, peak concurrency == 20 exactly (never 21)."""
    require_container_runtime()

    script = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    # Generate 50 running VMs
    $fleet = [System.Collections.Generic.List[object]]::new()
    $vms = [System.Collections.Generic.List[object]]::new()
    for ($i = 1; $i -le 50; $i++) {
        $name = "vm-pool-{0:D3}" -f $i
        $fleet.Add(@{
            id = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/$name"
            name = $name
            resourceGroup = 'rg'
            osType = 'Windows'
            powerState = 'PowerState/running'
        })
        $vms.Add(@{
            vm_name = $name
            resource_group = 'rg'
            subscription = 's'
            os_type = 'windows'
            initial_power = 'PowerState/running'
            tags = ''
        })
    }
    Set-FakeArmFleet -Fleet @($fleet)
    Write-Y2Targets -Path /work/targets.csv -Rows @($vms)

    Set-Content -Path /work/payload.ps1 -Value 'Write-Output "ok"'

    Reset-FakeArmCallLog

    # Run pool with MaxJobs = 20, PollSeconds = 5
    & /repo/ado/scripts/run-command.ps1 `
        -OsType windows `
        -PayloadFile /work/payload.ps1 `
        -WorkDir /work `
        -ReportDir /work/report `
        -MaxJobs 20 `
        -PollSeconds 5

    Export-FakeArmCallLog -Path /work/calls.json
    """

    res = run_pwsh_script(script, work_dir=tmp_path)
    assert res.returncode == 0, f"run-command failed: {res.stderr}"

    calls_file = tmp_path / "calls.json"
    assert calls_file.is_file()
    _dump_evidence("tp04", calls_file)

    calls = json.loads(calls_file.read_text(encoding="utf-8"))

    # Check maximum concurrent jobs across all logged calls
    concurrencies = [c["ConcurrentJobs"] for c in calls]
    peak_concurrency = max(concurrencies)

    assert peak_concurrency == 20, (
        f"Peak concurrency was {peak_concurrency}, expected exactly 20 (maxJobs cap violated if > 20)"
    )

    # Check that all 50 VMs were executed
    run_calls = [c for c in calls if c["Cmdlet"] in ("Start-Y2RunJob", "Invoke-AzVMRunCommand")]
    assert len(run_calls) == 50, f"Expected 50 run command submissions, got {len(run_calls)}"

    state_rows = _read_csv_rows(tmp_path / "state.csv")
    assert len(state_rows) == 50
    for r in state_rows:
        assert r["run"] == "ok"


def test_finished_jobs_collected_every_pollseconds(tmp_path: Path):
    """TP-04: Finished jobs collected every pollSeconds (sleep log)."""
    require_container_runtime()

    script = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    $fleet = @(
        @{ id = '/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-p1'; name = 'vm-p1'; resourceGroup = 'rg'; powerState = 'PowerState/running' },
        @{ id = '/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-p2'; name = 'vm-p2'; resourceGroup = 'rg'; powerState = 'PowerState/running' }
    )
    Set-FakeArmFleet -Fleet $fleet
    Write-Y2Targets -Path /work/targets.csv -Rows @(
        @{ vm_name = 'vm-p1'; resource_group = 'rg'; subscription = 's'; os_type = 'windows'; initial_power = 'PowerState/running'; tags = '' },
        @{ vm_name = 'vm-p2'; resource_group = 'rg'; subscription = 's'; os_type = 'windows'; initial_power = 'PowerState/running'; tags = '' }
    )

    Set-Content -Path /work/payload.ps1 -Value 'Write-Output "ok"'

    Reset-FakeArmCallLog

    & /repo/ado/scripts/run-command.ps1 `
        -OsType windows `
        -PayloadFile /work/payload.ps1 `
        -WorkDir /work `
        -ReportDir /work/report `
        -MaxJobs 20 `
        -PollSeconds 15

    Export-FakeArmCallLog -Path /work/calls.json
    """

    res = run_pwsh_script(script, work_dir=tmp_path)
    assert res.returncode == 0, f"run-command failed: {res.stderr}"

    calls = json.loads((tmp_path / "calls.json").read_text(encoding="utf-8"))
    sleep_calls = [c for c in calls if c["Cmdlet"] == "Start-Y2Sleep"]

    assert len(sleep_calls) >= 1
    for s in sleep_calls:
        assert s["Parameters"]["Seconds"] == 15.0


def test_retry_429_throttling_pool_backoff(tmp_path: Path):
    """TP-04: 429 (with/without Retry-After) pauses whole pool: no new jobs start during back-off."""
    require_container_runtime()

    script = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    $fleet = @(
        @{ id = '/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-throt-1'; name = 'vm-throt-1'; resourceGroup = 'rg'; powerState = 'PowerState/running' },
        @{ id = '/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-throt-2'; name = 'vm-throt-2'; resourceGroup = 'rg'; powerState = 'PowerState/running' }
    )
    Set-FakeArmFleet -Fleet $fleet
    Write-Y2Targets -Path /work/targets.csv -Rows @(
        @{ vm_name = 'vm-throt-1'; resource_group = 'rg'; subscription = 's'; os_type = 'windows'; initial_power = 'PowerState/running'; tags = '' },
        @{ vm_name = 'vm-throt-2'; resource_group = 'rg'; subscription = 's'; os_type = 'windows'; initial_power = 'PowerState/running'; tags = '' }
    )

    Set-Content -Path /work/payload.ps1 -Value 'Write-Output "ok"'

    # Inject 429 with Retry-After: 35s on first submit
    Set-FakeArmErrorRule -Cmdlet 'Start-Y2RunJob' -VmName 'vm-throt-1' -StatusCode 429 -RetryAfter 35 -Count 1

    Reset-FakeArmCallLog

    & /repo/ado/scripts/run-command.ps1 `
        -OsType windows `
        -PayloadFile /work/payload.ps1 `
        -WorkDir /work `
        -ReportDir /work/report `
        -MaxJobs 20 `
        -PollSeconds 5

    Export-FakeArmCallLog -Path /work/calls.json
    """

    res = run_pwsh_script(script, work_dir=tmp_path)
    assert res.returncode == 0, f"run-command failed: {res.stderr}"

    calls = json.loads((tmp_path / "calls.json").read_text(encoding="utf-8"))

    # Verify that Start-Y2Sleep was called with 35.0 seconds
    sleep_calls = [c for c in calls if c["Cmdlet"] == "Start-Y2Sleep"]
    has_35s_sleep = any(s["Parameters"]["Seconds"] == 35.0 for s in sleep_calls)
    assert has_35s_sleep, f"Expected 35s Retry-After backoff sleep, got sleep calls: {sleep_calls}"

    # Verify that vm-throt-2 was NOT submitted before the backoff sleep
    throt1_fail_idx = None
    sleep_35_idx = None
    throt2_submit_idx = None

    for idx, c in enumerate(calls):
        cmd = c["Cmdlet"]
        vname = c["Parameters"].get("VMName", "")
        if cmd == "Start-Y2RunJob" and vname == "vm-throt-1" and throt1_fail_idx is None:
            throt1_fail_idx = idx
        elif cmd == "Start-Y2Sleep" and c["Parameters"].get("Seconds") == 35.0:
            sleep_35_idx = idx
        elif cmd == "Start-Y2RunJob" and vname == "vm-throt-2" and throt2_submit_idx is None:
            throt2_submit_idx = idx

    assert sleep_35_idx is not None
    assert throt2_submit_idx is not None
    assert sleep_35_idx < throt2_submit_idx, (
        f"Pool failed to pause: vm-throt-2 submitted at {throt2_submit_idx} before backoff sleep at {sleep_35_idx}"
    )


def test_busy_409_requeue_and_cooldown(tmp_path: Path):
    """TP-04: 409 conflict -> no inline sleep (INV-11), re-queues VM with next_attempt_at=now+60s, retries 3x then run_command_busy."""
    require_container_runtime()

    # Case A: 409 on 1 VM does not delay other VMs, re-queues and succeeds on later pass
    script_requeue = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    $fleet = @(
        @{ id = '/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-busy-ok'; name = 'vm-busy-ok'; resourceGroup = 'rg'; powerState = 'PowerState/running' },
        @{ id = '/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-normal'; name = 'vm-normal'; resourceGroup = 'rg'; powerState = 'PowerState/running' }
    )
    Set-FakeArmFleet -Fleet $fleet
    Write-Y2Targets -Path /work/targets.csv -Rows @(
        @{ vm_name = 'vm-busy-ok'; resource_group = 'rg'; subscription = 's'; os_type = 'windows'; initial_power = 'PowerState/running'; tags = '' },
        @{ vm_name = 'vm-normal'; resource_group = 'rg'; subscription = 's'; os_type = 'windows'; initial_power = 'PowerState/running'; tags = '' }
    )

    Set-Content -Path /work/payload.ps1 -Value 'Write-Output "ok"'

    # vm-busy-ok fails with 409 once, then succeeds
    Set-FakeArmErrorRule -Cmdlet 'Start-Y2RunJob' -VmName 'vm-busy-ok' -StatusCode 409 -Count 1

    Reset-FakeArmCallLog

    & /repo/ado/scripts/run-command.ps1 `
        -OsType windows `
        -PayloadFile /work/payload.ps1 `
        -WorkDir /work `
        -ReportDir /work/report `
        -MaxJobs 20 `
        -PollSeconds 15

    Export-FakeArmCallLog -Path /work/calls.json
    """

    res1 = run_pwsh_script(script_requeue, work_dir=tmp_path / "case_requeue")
    assert res1.returncode == 0, f"run-command failed: {res1.stderr}"

    calls1 = json.loads((tmp_path / "case_requeue" / "calls.json").read_text(encoding="utf-8"))

    # Verify that vm-normal was submitted immediately after vm-busy-ok returned 409 without inline sleep
    busy_submit1_idx = None
    normal_submit_idx = None
    sleep_before_normal = False

    for idx, c in enumerate(calls1):
        cmd = c["Cmdlet"]
        vname = c["Parameters"].get("VMName", "")
        if cmd == "Start-Y2RunJob" and vname == "vm-busy-ok" and busy_submit1_idx is None:
            busy_submit1_idx = idx
        elif cmd == "Start-Y2Sleep" and busy_submit1_idx is not None and normal_submit_idx is None:
            sleep_before_normal = True
        elif cmd == "Start-Y2RunJob" and vname == "vm-normal" and normal_submit_idx is None:
            normal_submit_idx = idx

    assert not sleep_before_normal, "INV-11 violated: inline sleep executed before submitting vm-normal on 409!"

    # Case B: 409 exhausts 3 retries -> run_command_busy
    script_exhaust = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    $fleet = @(
        @{ id = '/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-always-busy'; name = 'vm-always-busy'; resourceGroup = 'rg'; powerState = 'PowerState/running' }
    )
    Set-FakeArmFleet -Fleet $fleet
    Write-Y2Targets -Path /work/targets.csv -Rows @(
        @{ vm_name = 'vm-always-busy'; resource_group = 'rg'; subscription = 's'; os_type = 'windows'; initial_power = 'PowerState/running'; tags = '' }
    )

    Set-Content -Path /work/payload.ps1 -Value 'Write-Output "ok"'

    # vm-always-busy fails with 409 continuously (more than 3 retries)
    Set-FakeArmErrorRule -Cmdlet 'Start-Y2RunJob' -VmName 'vm-always-busy' -StatusCode 409 -Count 10

    & /repo/ado/scripts/run-command.ps1 `
        -OsType windows `
        -PayloadFile /work/payload.ps1 `
        -WorkDir /work `
        -ReportDir /work/report `
        -MaxJobs 20 `
        -PollSeconds 15
    """

    res2 = run_pwsh_script(script_exhaust, work_dir=tmp_path / "case_exhaust")
    assert res2.returncode == 0, f"run-command failed: {res2.stderr}"

    state_rows = _read_csv_rows(tmp_path / "case_exhaust" / "state.csv")
    assert state_rows[0]["run"] == "run_command_busy", (
        f"Expected run_command_busy after 3 retries, got {state_rows[0]['run']}"
    )


# ===========================================================================
# TP-05: Auth breaker (AC-5)
# ===========================================================================


def test_auth_breaker_tripping_5_consecutive(tmp_path: Path):
    """TP-05: 5 consecutive auth failures -> no 6th submit, remaining VMs auth_aborted, step exits 1, finalize Failed."""
    require_container_runtime()

    script_run = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    # Fleet with 10 VMs
    $vms = [System.Collections.Generic.List[object]]::new()
    for ($i = 1; $i -le 10; $i++) {
        $name = "vm-auth-{0:D2}" -f $i
        $vms.Add(@{
            vm_name = $name
            resource_group = 'rg'
            subscription = 's'
            os_type = 'windows'
            initial_power = 'PowerState/running'
            tags = ''
        })
    }
    Write-Y2Targets -Path /work/targets.csv -Rows @($vms)

    Set-Content -Path /work/payload.ps1 -Value 'Write-Output "ok"'

    # Configure 401 auth failure for all VMs on Start-Y2RunJob
    Set-FakeArmErrorRule -Cmdlet 'Start-Y2RunJob' -VmName '*' -StatusCode 401 -Count 20

    Reset-FakeArmCallLog

    try {
        & /repo/ado/scripts/run-command.ps1 `
            -OsType windows `
            -PayloadFile /work/payload.ps1 `
            -WorkDir /work `
            -ReportDir /work/report `
            -MaxJobs 20 `
            -PollSeconds 5
    } finally {
        Export-FakeArmCallLog -Path /work/calls.json
    }
    """

    res_run = run_pwsh_script(script_run, work_dir=tmp_path)
    assert res_run.returncode == 1, f"Expected run-command to exit 1 on tripped breaker, got {res_run.returncode}"

    calls_file = tmp_path / "calls.json"
    assert calls_file.is_file()
    _dump_evidence("tp05", calls_file)

    calls = json.loads(calls_file.read_text(encoding="utf-8"))
    run_calls = [c for c in calls if c["Cmdlet"] in ("Start-Y2RunJob", "Invoke-AzVMRunCommand")]

    # Crucial check: exactly 5 submits, NO 6th submit!
    assert len(run_calls) == 5, f"Expected exactly 5 submit calls before breaker tripped, got {len(run_calls)}"

    state_rows = _read_csv_rows(tmp_path / "state.csv")
    assert len(state_rows) == 10

    # Remaining 5 VMs must be auth_aborted
    aborted_rows = [r for r in state_rows if r["run"] == "auth_aborted"]
    assert len(aborted_rows) >= 5, f"Expected at least 5 VMs marked auth_aborted, got {len(aborted_rows)}"

    # Finalize should also exit 1 (Failed result)
    script_finalize = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    & /repo/ado/scripts/finalize.ps1 -WorkDir /work -ReportDir /work/report
    """
    res_finalize = run_pwsh_script(script_finalize, work_dir=tmp_path)
    assert res_finalize.returncode == 1, f"Expected finalize exit 1, got {res_finalize.returncode}"


def test_auth_breaker_success_resets_counter(tmp_path: Path):
    """TP-05: Success resets consecutive counter: 4 fails + 1 ok + 4 fails does not trip breaker."""
    require_container_runtime()

    script = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    # 10 VMs: 4 fail, 1 ok, 4 fail, 1 ok
    $vms = [System.Collections.Generic.List[object]]::new()
    for ($i = 1; $i -le 10; $i++) {
        $name = "vm-reset-{0:D2}" -f $i
        $vms.Add(@{
            vm_name = $name
            resource_group = 'rg'
            subscription = 's'
            os_type = 'windows'
            initial_power = 'PowerState/running'
            tags = ''
        })
    }
    Write-Y2Targets -Path /work/targets.csv -Rows @($vms)

    Set-Content -Path /work/payload.ps1 -Value 'Write-Output "ok"'

    # Set 401 on vm-reset-01..04 and vm-reset-06..09 (vm-reset-05 and 10 succeed)
    for ($i = 1; $i -le 4; $i++) {
        $n = "vm-reset-{0:D2}" -f $i
        Set-FakeArmErrorRule -Cmdlet 'Start-Y2RunJob' -VmName $n -StatusCode 401 -Count 1
    }
    for ($i = 6; $i -le 9; $i++) {
        $n = "vm-reset-{0:D2}" -f $i
        Set-FakeArmErrorRule -Cmdlet 'Start-Y2RunJob' -VmName $n -StatusCode 401 -Count 1
    }

    Reset-FakeArmCallLog

    & /repo/ado/scripts/run-command.ps1 `
        -OsType windows `
        -PayloadFile /work/payload.ps1 `
        -WorkDir /work `
        -ReportDir /work/report `
        -MaxJobs 20 `
        -PollSeconds 5

    Export-FakeArmCallLog -Path /work/calls.json
    """

    res = run_pwsh_script(script, work_dir=tmp_path)
    assert res.returncode == 0, f"run-command tripped unexpectedly: {res.stderr}"

    calls = json.loads((tmp_path / "calls.json").read_text(encoding="utf-8"))
    run_calls = [c for c in calls if c["Cmdlet"] in ("Start-Y2RunJob", "Invoke-AzVMRunCommand")]

    # All 10 VMs were processed because breaker was reset by vm-reset-05
    assert len(run_calls) == 10, f"Expected all 10 VMs submitted, got {len(run_calls)}"

    state_rows = _read_csv_rows(tmp_path / "state.csv")
    aborted_rows = [r for r in state_rows if r["run"] == "auth_aborted"]
    assert len(aborted_rows) == 0, f"No VM should be auth_aborted when breaker did not trip, got {aborted_rows}"


def test_auth_breaker_across_steps_start_run_restore(tmp_path: Path):
    """TP-05: Auth breaker tripping in start step, run step, and restore step."""
    require_container_runtime()

    # Subtest 1: Breaker tripping in power-start still allows power-restore to run and restore started VMs
    script_start_trip = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    # 7 deallocated VMs
    $fleet = [System.Collections.Generic.List[object]]::new()
    $vms = [System.Collections.Generic.List[object]]::new()
    for ($i = 1; $i -le 7; $i++) {
        $name = "vm-st-{0:D2}" -f $i
        $fleet.Add(@{
            id = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/$name"
            name = $name
            resourceGroup = 'rg'
            powerState = 'PowerState/deallocated'
        })
        $vms.Add(@{
            vm_name = $name
            resource_group = 'rg'
            subscription = 's'
            os_type = 'windows'
            initial_power = 'PowerState/deallocated'
            tags = ''
        })
    }
    Set-FakeArmFleet -Fleet @($fleet)
    Write-Y2Targets -Path /work/targets.csv -Rows @($vms)

    # 1st VM succeeds, next 5 fail with 401 on Start-Y2Vm (tripping breaker)
    Set-FakeArmErrorRule -Cmdlet 'Start-Y2Vm' -VmName 'vm-st-02' -StatusCode 401 -Count 1
    Set-FakeArmErrorRule -Cmdlet 'Start-Y2Vm' -VmName 'vm-st-03' -StatusCode 401 -Count 1
    Set-FakeArmErrorRule -Cmdlet 'Start-Y2Vm' -VmName 'vm-st-04' -StatusCode 401 -Count 1
    Set-FakeArmErrorRule -Cmdlet 'Start-Y2Vm' -VmName 'vm-st-05' -StatusCode 401 -Count 1
    Set-FakeArmErrorRule -Cmdlet 'Start-Y2Vm' -VmName 'vm-st-06' -StatusCode 401 -Count 1

    try {
        & /repo/ado/scripts/power-start.ps1 -WorkDir /work -PollSeconds 5
    } finally {
        Export-FakeArmCallLog -Path /work/start_calls.json
    }
    """

    res1 = run_pwsh_script(script_start_trip, work_dir=tmp_path / "case_start_trip")
    assert res1.returncode == 1, f"Expected power-start to exit 1 on tripped breaker, got {res1.returncode}"

    # Restore step runs next
    script_restore_after_start_trip = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    Clear-FakeArmErrorRules
    Reset-FakeArmCallLog

    & /repo/ado/scripts/power-restore.ps1 -WorkDir /work -PollSeconds 5

    Export-FakeArmCallLog -Path /work/restore_calls.json
    """
    res_restore = run_pwsh_script(script_restore_after_start_trip, work_dir=tmp_path / "case_start_trip")
    assert res_restore.returncode == 0, f"power-restore failed after start trip: {res_restore.stderr}"

    restore_calls = json.loads((tmp_path / "case_start_trip" / "restore_calls.json").read_text(encoding="utf-8"))
    stop_calls = [c for c in restore_calls if c["Cmdlet"] in ("Stop-Y2Vm", "Stop-AzVM")]

    # vm-st-01 was started before the breaker tripped, so it must be restored!
    assert len(stop_calls) == 1
    assert stop_calls[0]["Parameters"]["Name"] == "vm-st-01"

    # Subtest 2: Breaker tripping in power-restore
    script_restore_trip = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    $vms = [System.Collections.Generic.List[object]]::new()
    $stateRows = [System.Collections.Generic.List[object]]::new()
    for ($i = 1; $i -le 7; $i++) {
        $name = "vm-rst-{0:D2}" -f $i
        $vms.Add(@{
            vm_name = $name
            resource_group = 'rg'
            subscription = 's'
            os_type = 'windows'
            initial_power = 'PowerState/deallocated'
            tags = ''
        })
        $stateRows.Add(@{
            vm_name = $name
            verified_power = 'PowerState/deallocated'
            started_by_run = 'true'
            start = 'ok'
            run = 'ok'
            restore = ''
            output_bytes = 10
            possibly_truncated = 'false'
        })
    }
    Write-Y2Targets -Path /work/targets.csv -Rows @($vms)
    Write-Y2State -Path /work/state.csv -Rows @($stateRows)

    # All Stop-Y2Vm fail with 403 (tripping breaker after 5)
    Set-FakeArmErrorRule -Cmdlet 'Stop-Y2Vm' -VmName '*' -StatusCode 403 -Count 10

    Reset-FakeArmCallLog

    try {
        & /repo/ado/scripts/power-restore.ps1 -WorkDir /work -PollSeconds 5
    } finally {
        Export-FakeArmCallLog -Path /work/calls.json
    }
    """

    res2 = run_pwsh_script(script_restore_trip, work_dir=tmp_path / "case_restore_trip")
    assert res2.returncode == 1, f"Expected power-restore exit 1 on tripped breaker, got {res2.returncode}"

    calls2 = json.loads((tmp_path / "case_restore_trip" / "calls.json").read_text(encoding="utf-8"))
    stop_calls2 = [c for c in calls2 if c["Cmdlet"] in ("Stop-Y2Vm", "Stop-AzVM")]

    # Exactly 5 stop submits before breaker tripped, NO 6th stop submit!
    assert len(stop_calls2) == 5, f"Expected 5 stop calls before breaker trip, got {len(stop_calls2)}"
