# tests/test_ado_steps.py - Tier 2 step tests for ADO-native patch templates
# Covers: TP-02 (resolve), TP-03 (start), TP-06 (restore), TP-08 (hookvars), TP-09 (status/result)
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
# TP-02: Resolve Paging (AC-2)
# ===========================================================================


def test_resolve_zero_vms(tmp_path: Path):
    """TP-02 (a): 0 VMs match -> exit 0, header-only CSVs, exactly 1 Search-AzGraph call."""
    require_container_runtime()

    script = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    Set-FakeArmFleet -Fleet @()

    & /repo/ado/scripts/resolve.ps1 `
        -TargetsKql 'Resources | where type == "none"' `
        -OsType windows `
        -WorkDir /work `
        -ReportDir /work/report `
        -RunId '100-1'

    Export-FakeArmCallLog -Path /work/calls.json
    """

    res = run_pwsh_script(script, work_dir=tmp_path)
    assert res.returncode == 0, f"resolve.ps1 failed: {res.stderr}"

    targets_csv = tmp_path / "targets.csv"
    assert targets_csv.is_file(), "targets.csv was not created"
    t_rows = _read_csv_rows(targets_csv)
    assert len(t_rows) == 0, f"Expected 0 targets, got {len(t_rows)}"

    status_csv = tmp_path / "report" / "status.csv"
    assert status_csv.is_file(), "status.csv in report dir was not created"
    s_rows = _read_csv_rows(status_csv)
    assert len(s_rows) == 0, f"Expected 0 status rows, got {len(s_rows)}"

    calls_file = tmp_path / "calls.json"
    assert calls_file.is_file()
    _dump_evidence("tp02", calls_file)

    calls = json.loads(calls_file.read_text(encoding="utf-8"))
    graph_calls = [c for c in calls if c["Cmdlet"] in ("Invoke-Y2Graph", "Search-AzGraph")]
    assert len(graph_calls) == 1, f"Expected exactly 1 graph call, got {len(graph_calls)}"

    other_az_calls = [c for c in calls if c["Cmdlet"] not in ("Invoke-Y2Graph", "Search-AzGraph", "Start-Y2Sleep")]
    assert len(other_az_calls) == 0, f"Expected 0 other Azure calls, got {other_az_calls}"


def test_resolve_paging_2500_vms(tmp_path: Path):
    """TP-02 (b): 2,500 VMs across 3 pages (1000/1000/500) -> 2,500 distinct rows, ordered."""
    require_container_runtime()

    script = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    Set-FakeArmPagingTest -Count 2500 -PageSize 1000 -OsType Windows

    & /repo/ado/scripts/resolve.ps1 `
        -TargetsKql 'Resources | where type =~ "microsoft.compute/virtualmachines"' `
        -OsType windows `
        -WorkDir /work `
        -ReportDir /work/report `
        -RunId '100-1'

    Export-FakeArmCallLog -Path /work/calls.json
    """

    res = run_pwsh_script(script, work_dir=tmp_path)
    assert res.returncode == 0, f"resolve.ps1 failed: {res.stderr}"

    targets_csv = tmp_path / "targets.csv"
    assert targets_csv.is_file()
    rows = _read_csv_rows(targets_csv)
    assert len(rows) == 2500, f"Expected 2500 rows, got {len(rows)}"

    names = [r["vm_name"] for r in rows]
    assert len(set(names)) == 2500, "Found duplicate VM names in targets.csv"
    assert names == sorted(names), "targets.csv rows were not sorted by name ascending"

    calls_file = tmp_path / "calls.json"
    assert calls_file.is_file()
    _dump_evidence("tp02", calls_file)

    calls = json.loads(calls_file.read_text(encoding="utf-8"))
    graph_calls = [c for c in calls if c["Cmdlet"] in ("Invoke-Y2Graph", "Search-AzGraph")]
    assert len(graph_calls) == 3, f"Expected exactly 3 graph calls, got {len(graph_calls)}"

    for c in graph_calls:
        query = c["Parameters"].get("Query", "")
        assert "order by name asc" in query.lower(), f"Missing 'order by name asc' in query: {query}"


def test_resolve_os_mismatch(tmp_path: Path):
    """TP-02 (c): Other os_type marked os_mismatch in state.csv, receives 0 later calls."""
    require_container_runtime()

    script = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    # 1. Resolve targeting windows (fleet has 35 win, 25 lnx)
    & /repo/ado/scripts/resolve.ps1 `
        -TargetsKql 'Resources | where type =~ "microsoft.compute/virtualmachines"' `
        -OsType windows `
        -WorkDir /work `
        -ReportDir /work/report `
        -RunId '100-1'

    # Create dummy payload
    Set-Content -Path /work/payload.ps1 -Value 'Write-Output "ok"'

    # 2. Start
    & /repo/ado/scripts/power-start.ps1 -WorkDir /work -PollSeconds 5

    # 3. Run
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
    assert res.returncode == 0, f"Pipeline execution failed: {res.stderr}"

    state_csv = tmp_path / "state.csv"
    assert state_csv.is_file()
    state_rows = _read_csv_rows(state_csv)

    lnx_rows = [r for r in state_rows if r["vm_name"].startswith("vm-lnx")]
    win_rows = [r for r in state_rows if r["vm_name"].startswith("vm-win")]

    assert len(lnx_rows) == 25, f"Expected 25 linux rows, got {len(lnx_rows)}"
    assert len(win_rows) == 35, f"Expected 35 windows rows, got {len(win_rows)}"

    for r in lnx_rows:
        assert r["run"] == "os_mismatch", f"Linux VM {r['vm_name']} run status was {r['run']}"
        assert r["start"] == "skipped", f"Linux VM {r['vm_name']} start status was {r['start']}"
        assert r["restore"] == "skipped", f"Linux VM {r['vm_name']} restore status was {r['restore']}"

    calls_file = tmp_path / "calls.json"
    calls = json.loads(calls_file.read_text(encoding="utf-8"))

    lnx_names = {r["vm_name"].lower() for r in lnx_rows}
    for c in calls:
        if c["Cmdlet"] in ("Get-Y2VmStatus", "Start-Y2Vm", "Stop-Y2Vm", "Start-Y2RunJob"):
            target_name = (c["Parameters"].get("Name") or c["Parameters"].get("VMName") or "").lower()
            assert target_name not in lnx_names, f"Forbidden call {c['Cmdlet']} targeted os_mismatch VM {target_name}"


def test_resolve_error_exit_1(tmp_path: Path):
    """TP-02 (d): Resolve error -> exit 1."""
    require_container_runtime()

    script = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    Set-FakeArmErrorRule -Cmdlet 'Invoke-Y2Graph' -StatusCode 500 -Count 1

    & /repo/ado/scripts/resolve.ps1 `
        -TargetsKql 'Resources | where type =~ "microsoft.compute/virtualmachines"' `
        -OsType windows `
        -WorkDir /work `
        -ReportDir /work/report `
        -RunId '100-1'
    """

    res = run_pwsh_script(script, work_dir=tmp_path)
    assert res.returncode == 1, f"Expected exit code 1 on resolve error, got {res.returncode}"


# ===========================================================================
# TP-03: Verified Start (AC-3)
# ===========================================================================


def test_start_verified_power_already_running(tmp_path: Path):
    """TP-03: Snapshot=deallocated but Get-AzVM says running: no Start-AzVM, started_by_run=false."""
    require_container_runtime()

    script = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    # 1. Resolve
    & /repo/ado/scripts/resolve.ps1 `
        -TargetsKql 'Resources | where type =~ "microsoft.compute/virtualmachines"' `
        -OsType windows `
        -WorkDir /work `
        -ReportDir /work/report `
        -RunId '100-1'

    # VM vm-win-019 was deallocated at snapshot, but externally started before power-start
    Set-FakeArmVmStatus -Name 'vm-win-019' -PowerState 'PowerState/running'

    # 2. Start
    & /repo/ado/scripts/power-start.ps1 -WorkDir /work -PollSeconds 5

    # 3. Restore
    & /repo/ado/scripts/power-restore.ps1 -WorkDir /work -PollSeconds 5

    Export-FakeArmCallLog -Path /work/calls.json
    """

    res = run_pwsh_script(script, work_dir=tmp_path)
    assert res.returncode == 0, f"Execution failed: {res.stderr}"

    state_csv = tmp_path / "state.csv"
    state_rows = _read_csv_rows(state_csv)
    v19_state = next(r for r in state_rows if r["vm_name"] == "vm-win-019")

    assert v19_state["verified_power"] == "PowerState/running"
    assert v19_state["started_by_run"] == "false"

    calls_file = tmp_path / "calls.json"
    assert calls_file.is_file()
    _dump_evidence("tp03", calls_file)

    calls = json.loads(calls_file.read_text(encoding="utf-8"))

    start_calls_v19 = [
        c for c in calls
        if c["Cmdlet"] in ("Start-Y2Vm", "Start-AzVM") and c["Parameters"].get("Name") == "vm-win-019"
    ]
    assert len(start_calls_v19) == 0, f"Expected 0 Start-AzVM calls for vm-win-019, got {len(start_calls_v19)}"

    stop_calls_v19 = [
        c for c in calls
        if c["Cmdlet"] in ("Stop-Y2Vm", "Stop-AzVM") and c["Parameters"].get("Name") == "vm-win-019"
    ]
    assert len(stop_calls_v19) == 0, f"Expected 0 Stop-AzVM calls for vm-win-019, got {len(stop_calls_v19)}"


def test_start_subset_only_status_reads(tmp_path: Path):
    """TP-03: Status read only for not-running subset; running VMs get 0 status reads."""
    require_container_runtime()

    script = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    # Resolve
    & /repo/ado/scripts/resolve.ps1 `
        -TargetsKql 'Resources | where type =~ "microsoft.compute/virtualmachines"' `
        -OsType windows `
        -WorkDir /work `
        -ReportDir /work/report `
        -RunId '100-1'

    Reset-FakeArmCallLog

    # Power start
    & /repo/ado/scripts/power-start.ps1 -WorkDir /work -PollSeconds 5

    Export-FakeArmCallLog -Path /work/calls.json
    """

    res = run_pwsh_script(script, work_dir=tmp_path)
    assert res.returncode == 0, f"power-start failed: {res.stderr}"

    calls_file = tmp_path / "calls.json"
    calls = json.loads(calls_file.read_text(encoding="utf-8"))

    # vm-win-001 through vm-win-018 were running at snapshot
    running_names = {f"vm-win-{i:03d}" for i in range(1, 19)}

    for c in calls:
        if c["Cmdlet"] in ("Get-Y2VmStatus", "Get-AzVM"):
            target_name = c["Parameters"].get("Name", "")
            assert target_name not in running_names, (
                f"Initially running VM {target_name} received forbidden status check during start: {c}"
            )


def test_start_transition_and_tracking(tmp_path: Path):
    """TP-03: Still-not-running gets Start-AzVM -NoWait; started_by_run=submitted then true."""
    require_container_runtime()

    script = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    # Single deallocated VM test
    $singleVm = @(
        [PSCustomObject]@{
            id = '/subscriptions/sub-1/resourceGroups/rg-1/providers/Microsoft.Compute/virtualMachines/vm-test-start'
            name = 'vm-test-start'
            vm_name = 'vm-test-start'
            resourceGroup = 'rg-1'
            rg = 'rg-1'
            subscriptionId = 'sub-1'
            sub = 'sub-1'
            subscription = 'sub-1'
            osType = 'Windows'
            os_type = 'windows'
            powerState = 'PowerState/deallocated'
            initial_power = 'PowerState/deallocated'
            tags = @{}
        }
    )
    Set-FakeArmFleet -Fleet $singleVm

    & /repo/ado/scripts/resolve.ps1 `
        -TargetsKql 'Resources' `
        -OsType windows `
        -WorkDir /work `
        -ReportDir /work/report `
        -RunId '100-1'

    Reset-FakeArmCallLog

    & /repo/ado/scripts/power-start.ps1 -WorkDir /work -PollSeconds 5

    Export-FakeArmCallLog -Path /work/calls.json
    """

    res = run_pwsh_script(script, work_dir=tmp_path)
    assert res.returncode == 0, f"power-start failed: {res.stderr}"

    state_rows = _read_csv_rows(tmp_path / "state.csv")
    r = state_rows[0]
    assert r["verified_power"] == "PowerState/deallocated"
    assert r["started_by_run"] == "true"
    assert r["start"] == "ok"

    calls = json.loads((tmp_path / "calls.json").read_text(encoding="utf-8"))
    start_calls = [c for c in calls if c["Cmdlet"] in ("Start-Y2Vm", "Start-AzVM")]
    assert len(start_calls) == 1


def test_start_failed_skipped_by_run(tmp_path: Path):
    """TP-03: Failed start -> start_failed; Run skips it (run=skipped, 0 run calls)."""
    require_container_runtime()

    script = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    $vm = @(
        [PSCustomObject]@{
            id = '/subscriptions/sub-1/resourceGroups/rg-1/providers/Microsoft.Compute/virtualMachines/vm-fail-start'
            name = 'vm-fail-start'
            vm_name = 'vm-fail-start'
            resourceGroup = 'rg-1'
            rg = 'rg-1'
            subscriptionId = 'sub-1'
            sub = 'sub-1'
            subscription = 'sub-1'
            osType = 'Windows'
            os_type = 'windows'
            powerState = 'PowerState/deallocated'
            initial_power = 'PowerState/deallocated'
            tags = @{}
        }
    )
    Set-FakeArmFleet -Fleet $vm

    & /repo/ado/scripts/resolve.ps1 `
        -TargetsKql 'Resources' `
        -OsType windows `
        -WorkDir /work `
        -ReportDir /work/report `
        -RunId '100-1'

    # Inject failure on Start-Y2Vm
    Set-FakeArmErrorRule -Cmdlet 'Start-Y2Vm' -VmName 'vm-fail-start' -StatusCode 500 -Count 1

    & /repo/ado/scripts/power-start.ps1 -WorkDir /work -PollSeconds 5

    Set-Content -Path /work/payload.ps1 -Value 'Write-Output "ok"'

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

    state_rows = _read_csv_rows(tmp_path / "state.csv")
    r = state_rows[0]
    assert r["start"] == "start_failed"
    assert r["run"] == "skipped"

    calls = json.loads((tmp_path / "calls.json").read_text(encoding="utf-8"))
    run_calls = [c for c in calls if c["Cmdlet"] in ("Start-Y2RunJob", "Invoke-AzVMRunCommand")]
    assert len(run_calls) == 0, f"Expected 0 run command calls for start_failed VM, got {len(run_calls)}"


# ===========================================================================
# TP-06: Safe Restore (T2 part) (AC-6)
# ===========================================================================


def test_restore_cancel_simulated_submitted(tmp_path: Path):
    """TP-06: Cancel simulated when started_by_run=submitted -> VM is restored."""
    require_container_runtime()

    script = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    $vm = @(
        [PSCustomObject]@{
            id = '/subscriptions/sub-1/resourceGroups/rg-1/providers/Microsoft.Compute/virtualMachines/vm-cancel-restore'
            name = 'vm-cancel-restore'
            vm_name = 'vm-cancel-restore'
            resourceGroup = 'rg-1'
            rg = 'rg-1'
            subscriptionId = 'sub-1'
            sub = 'sub-1'
            subscription = 'sub-1'
            osType = 'Windows'
            os_type = 'windows'
            powerState = 'PowerState/running'
            initial_power = 'PowerState/deallocated'
            tags = @{}
        }
    )
    Set-FakeArmFleet -Fleet $vm

    # Write targets.csv
    Write-Y2Targets -Path /work/targets.csv -Rows @(
        @{
            vm_name = 'vm-cancel-restore'
            resource_group = 'rg-1'
            subscription = 'sub-1'
            os_type = 'windows'
            initial_power = 'PowerState/deallocated'
            tags = ''
        }
    )

    # Write state.csv representing cancellation right after Start POST accepted
    Write-Y2State -Path /work/state.csv -Rows @(
        @{
            vm_name = 'vm-cancel-restore'
            verified_power = 'PowerState/deallocated'
            started_by_run = 'submitted'
            start = ''
            run = ''
            restore = ''
            output_bytes = 0
            possibly_truncated = 'false'
        }
    )

    Reset-FakeArmCallLog

    & /repo/ado/scripts/power-restore.ps1 -WorkDir /work -PollSeconds 5

    Export-FakeArmCallLog -Path /work/calls.json
    """

    res = run_pwsh_script(script, work_dir=tmp_path)
    assert res.returncode == 0, f"power-restore failed: {res.stderr}"

    calls_file = tmp_path / "calls.json"
    assert calls_file.is_file()
    _dump_evidence("tp06", calls_file)

    calls = json.loads(calls_file.read_text(encoding="utf-8"))
    stop_calls = [c for c in calls if c["Cmdlet"] in ("Stop-Y2Vm", "Stop-AzVM")]
    assert len(stop_calls) == 1, f"Expected 1 Stop-AzVM call, got {len(stop_calls)}"

    state_rows = _read_csv_rows(tmp_path / "state.csv")
    assert state_rows[0]["restore"] == "ok"


def test_restore_snapshot_running_never_stopped(tmp_path: Path):
    """TP-06: VM running at snapshot receives 0 Stop-AzVM calls."""
    require_container_runtime()

    script = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    # targets and state where VM was running at snapshot
    Write-Y2Targets -Path /work/targets.csv -Rows @(
        @{
            vm_name = 'vm-always-running'
            resource_group = 'rg-1'
            subscription = 'sub-1'
            os_type = 'windows'
            initial_power = 'PowerState/running'
            tags = ''
        }
    )

    Write-Y2State -Path /work/state.csv -Rows @(
        @{
            vm_name = 'vm-always-running'
            verified_power = 'PowerState/running'
            started_by_run = 'false'
            start = ''
            run = 'ok'
            restore = ''
            output_bytes = 100
            possibly_truncated = 'false'
        }
    )

    Reset-FakeArmCallLog

    & /repo/ado/scripts/power-restore.ps1 -WorkDir /work -PollSeconds 5

    Export-FakeArmCallLog -Path /work/calls.json
    """

    res = run_pwsh_script(script, work_dir=tmp_path)
    assert res.returncode == 0, f"power-restore failed: {res.stderr}"

    calls = json.loads((tmp_path / "calls.json").read_text(encoding="utf-8"))
    stop_calls = [c for c in calls if c["Cmdlet"] in ("Stop-Y2Vm", "Stop-AzVM")]
    assert len(stop_calls) == 0, f"Expected 0 Stop-AzVM calls for running VM, got {len(stop_calls)}"


def test_restore_missing_state_csv_exit_0(tmp_path: Path):
    """TP-06: No state.csv -> exit 0 cleanly (resolve never ran)."""
    require_container_runtime()

    script = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    & /repo/ado/scripts/power-restore.ps1 -WorkDir /work -PollSeconds 5
    """

    res = run_pwsh_script(script, work_dir=tmp_path)
    assert res.returncode == 0, f"Expected exit 0 with missing state.csv, got {res.returncode}"


def test_restore_calls_before_status_check(tmp_path: Path):
    """TP-06: All restore calls (-NoWait) issued before any status check."""
    require_container_runtime()

    script = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    # 3 VMs started by run
    $fleet = @(
        @{ id = '/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-r1'; name = 'vm-r1'; resourceGroup = 'rg'; powerState = 'PowerState/running' },
        @{ id = '/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-r2'; name = 'vm-r2'; resourceGroup = 'rg'; powerState = 'PowerState/running' },
        @{ id = '/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-r3'; name = 'vm-r3'; resourceGroup = 'rg'; powerState = 'PowerState/running' }
    )
    Set-FakeArmFleet -Fleet $fleet

    Write-Y2Targets -Path /work/targets.csv -Rows @(
        @{ vm_name = 'vm-r1'; resource_group = 'rg'; subscription = 's'; os_type = 'windows'; initial_power = 'PowerState/deallocated'; tags = '' },
        @{ vm_name = 'vm-r2'; resource_group = 'rg'; subscription = 's'; os_type = 'windows'; initial_power = 'PowerState/deallocated'; tags = '' },
        @{ vm_name = 'vm-r3'; resource_group = 'rg'; subscription = 's'; os_type = 'windows'; initial_power = 'PowerState/deallocated'; tags = '' }
    )

    Write-Y2State -Path /work/state.csv -Rows @(
        @{ vm_name = 'vm-r1'; verified_power = 'PowerState/deallocated'; started_by_run = 'true'; start = 'ok'; run = 'ok'; restore = ''; output_bytes = 50; possibly_truncated = 'false' },
        @{ vm_name = 'vm-r2'; verified_power = 'PowerState/deallocated'; started_by_run = 'true'; start = 'ok'; run = 'ok'; restore = ''; output_bytes = 50; possibly_truncated = 'false' },
        @{ vm_name = 'vm-r3'; verified_power = 'PowerState/deallocated'; started_by_run = 'true'; start = 'ok'; run = 'ok'; restore = ''; output_bytes = 50; possibly_truncated = 'false' }
    )

    Reset-FakeArmCallLog

    & /repo/ado/scripts/power-restore.ps1 -WorkDir /work -PollSeconds 5

    Export-FakeArmCallLog -Path /work/calls.json
    """

    res = run_pwsh_script(script, work_dir=tmp_path)
    assert res.returncode == 0, f"power-restore failed: {res.stderr}"

    calls = json.loads((tmp_path / "calls.json").read_text(encoding="utf-8"))

    # Verify that all 3 Stop calls appear before any Get-Y2VmStatus call
    first_get_idx = None
    last_stop_idx = None

    for idx, c in enumerate(calls):
        cmd = c["Cmdlet"]
        if cmd in ("Stop-Y2Vm", "Stop-AzVM"):
            last_stop_idx = idx
        elif cmd in ("Get-Y2VmStatus", "Get-AzVM") and first_get_idx is None:
            first_get_idx = idx

    assert last_stop_idx is not None, "No stop calls found"
    assert first_get_idx is not None, "No status get calls found"
    assert last_stop_idx < first_get_idx, (
        f"Expected all Stop calls (last at {last_stop_idx}) before first status read (at {first_get_idx})"
    )


def test_restore_power_state_deallocated_and_stopped(tmp_path: Path):
    """TP-06: verified_power deallocated -> Stop-AzVM -Force -NoWait; stopped -> StayProvisioned."""
    require_container_runtime()

    script = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    $fleet = @(
        @{ id = '/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-deal'; name = 'vm-deal'; resourceGroup = 'rg'; powerState = 'PowerState/running' },
        @{ id = '/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-stop'; name = 'vm-stop'; resourceGroup = 'rg'; powerState = 'PowerState/running' }
    )
    Set-FakeArmFleet -Fleet $fleet

    Write-Y2Targets -Path /work/targets.csv -Rows @(
        @{ vm_name = 'vm-deal'; resource_group = 'rg'; subscription = 's'; os_type = 'windows'; initial_power = 'PowerState/deallocated'; tags = '' },
        @{ vm_name = 'vm-stop'; resource_group = 'rg'; subscription = 's'; os_type = 'windows'; initial_power = 'PowerState/stopped'; tags = '' }
    )

    Write-Y2State -Path /work/state.csv -Rows @(
        @{ vm_name = 'vm-deal'; verified_power = 'PowerState/deallocated'; started_by_run = 'true'; start = 'ok'; run = 'ok'; restore = ''; output_bytes = 10; possibly_truncated = 'false' },
        @{ vm_name = 'vm-stop'; verified_power = 'PowerState/stopped'; started_by_run = 'true'; start = 'ok'; run = 'ok'; restore = ''; output_bytes = 10; possibly_truncated = 'false' }
    )

    Reset-FakeArmCallLog

    & /repo/ado/scripts/power-restore.ps1 -WorkDir /work -PollSeconds 5

    Export-FakeArmCallLog -Path /work/calls.json
    """

    res = run_pwsh_script(script, work_dir=tmp_path)
    assert res.returncode == 0, f"power-restore failed: {res.stderr}"

    calls = json.loads((tmp_path / "calls.json").read_text(encoding="utf-8"))

    stop_deal = next(
        c for c in calls if c["Cmdlet"] in ("Stop-Y2Vm", "Stop-AzVM") and c["Parameters"].get("Name") == "vm-deal"
    )
    stop_stop = next(
        c for c in calls if c["Cmdlet"] in ("Stop-Y2Vm", "Stop-AzVM") and c["Parameters"].get("Name") == "vm-stop"
    )

    assert not stop_deal["Parameters"].get("StayProvisioned", False), "vm-deal should not have StayProvisioned"
    assert stop_stop["Parameters"].get("StayProvisioned", False), "vm-stop must have StayProvisioned=True"


# ===========================================================================
# TP-08: Hook Variables (AC-8)
# ===========================================================================


def test_hookvars_resolve_output(tmp_path: Path):
    """TP-08: resolve.ps1 emits ##vso[task.setvariable] for Y2_TARGETS_CSV, Y2_REPORT_DIR, Y2_RUN_ID."""
    require_container_runtime()

    run_id = "8899-2"
    script = f"""
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    & /repo/ado/scripts/resolve.ps1 `
        -TargetsKql 'Resources | where type =~ "microsoft.compute/virtualmachines"' `
        -OsType windows `
        -WorkDir /work `
        -ReportDir /work/report `
        -RunId '{run_id}'
    """

    res = run_pwsh_script(script, work_dir=tmp_path)
    assert res.returncode == 0, f"resolve.ps1 failed: {res.stderr}"

    stdout = res.stdout
    assert "##vso[task.setvariable variable=Y2_TARGETS_CSV]" in stdout
    assert "##vso[task.setvariable variable=Y2_REPORT_DIR]" in stdout
    assert f"##vso[task.setvariable variable=Y2_RUN_ID]{run_id}" in stdout

    targets_csv = tmp_path / "targets.csv"
    assert targets_csv.is_file(), "targets.csv does not exist on disk"

    report_dir = tmp_path / "report"
    assert report_dir.is_dir(), "report directory does not exist on disk"


# ===========================================================================
# TP-09: Outputs and Job Result (AC-9)
# ===========================================================================


def test_status_csv_columns_and_formatting(tmp_path: Path):
    """TP-09: status.csv header exactly matches spec, BOM-less UTF-8, LF endings."""
    require_container_runtime()

    script = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    # 1. Resolve
    & /repo/ado/scripts/resolve.ps1 `
        -TargetsKql 'Resources | where type =~ "microsoft.compute/virtualmachines"' `
        -OsType windows `
        -WorkDir /work `
        -ReportDir /work/report `
        -RunId '100-1'

    Set-Content -Path /work/payload.ps1 -Value 'Write-Output "ok"'

    # 2. Start
    & /repo/ado/scripts/power-start.ps1 -WorkDir /work -PollSeconds 5

    # 3. Run
    & /repo/ado/scripts/run-command.ps1 `
        -OsType windows `
        -PayloadFile /work/payload.ps1 `
        -WorkDir /work `
        -ReportDir /work/report `
        -MaxJobs 20 `
        -PollSeconds 5

    # 4. Restore
    & /repo/ado/scripts/power-restore.ps1 -WorkDir /work -PollSeconds 5

    # 5. Finalize
    & /repo/ado/scripts/finalize.ps1 -WorkDir /work -ReportDir /work/report
    """

    res = run_pwsh_script(script, work_dir=tmp_path)
    assert res.returncode == 0, f"Pipeline execution failed: {res.stderr}"

    status_csv = tmp_path / "report" / "status.csv"
    assert status_csv.is_file(), "status.csv was not generated"

    raw_bytes = status_csv.read_bytes()
    assert not raw_bytes.startswith(b"\xef\xbb\xbf"), "status.csv contains forbidden UTF-8 BOM"
    assert b"\r\n" not in raw_bytes, "status.csv contains forbidden CRLF"

    lines = status_csv.read_text(encoding="utf-8").splitlines()
    expected_header = (
        '"vm_name","resource_group","subscription","initial_power","verified_power",'
        '"start","run","restore","output_bytes","possibly_truncated"'
    )
    assert lines[0] == expected_header


def test_output_possibly_truncated_boundary(tmp_path: Path):
    """TP-09: possibly_truncated false at 4095 bytes, true at 4096 and above."""
    require_container_runtime()

    script = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    $fleet = @(
        @{ id = '/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-4095'; name = 'vm-4095'; resourceGroup = 'rg'; powerState = 'PowerState/running' },
        @{ id = '/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-4096'; name = 'vm-4096'; resourceGroup = 'rg'; powerState = 'PowerState/running' },
        @{ id = '/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-5000'; name = 'vm-5000'; resourceGroup = 'rg'; powerState = 'PowerState/running' }
    )
    Set-FakeArmFleet -Fleet $fleet

    Write-Y2Targets -Path /work/targets.csv -Rows @(
        @{ vm_name = 'vm-4095'; resource_group = 'rg'; subscription = 's'; os_type = 'windows'; initial_power = 'PowerState/running'; tags = '' },
        @{ vm_name = 'vm-4096'; resource_group = 'rg'; subscription = 's'; os_type = 'windows'; initial_power = 'PowerState/running'; tags = '' },
        @{ vm_name = 'vm-5000'; resource_group = 'rg'; subscription = 's'; os_type = 'windows'; initial_power = 'PowerState/running'; tags = '' }
    )

    Write-Y2State -Path /work/state.csv -Rows @(
        @{ vm_name = 'vm-4095'; verified_power = 'PowerState/running'; started_by_run = 'false'; start = ''; run = ''; restore = ''; output_bytes = 0; possibly_truncated = 'false' },
        @{ vm_name = 'vm-4096'; verified_power = 'PowerState/running'; started_by_run = 'false'; start = ''; run = ''; restore = ''; output_bytes = 0; possibly_truncated = 'false' },
        @{ vm_name = 'vm-5000'; verified_power = 'PowerState/running'; started_by_run = 'false'; start = ''; run = ''; restore = ''; output_bytes = 0; possibly_truncated = 'false' }
    )

    # Set custom stdout lengths
    $out4095 = "A" * 4095
    $out4096 = "B" * 4096
    $out5000 = "C" * 5000

    Set-FakeArmJobOutput -VmName 'vm-4095' -StdOut $out4095
    Set-FakeArmJobOutput -VmName 'vm-4096' -StdOut $out4096
    Set-FakeArmJobOutput -VmName 'vm-5000' -StdOut $out5000

    Set-Content -Path /work/payload.ps1 -Value 'Write-Output "ok"'

    # Run
    & /repo/ado/scripts/run-command.ps1 `
        -OsType windows `
        -PayloadFile /work/payload.ps1 `
        -WorkDir /work `
        -ReportDir /work/report `
        -MaxJobs 20 `
        -PollSeconds 5

    # Finalize
    & /repo/ado/scripts/finalize.ps1 -WorkDir /work -ReportDir /work/report
    """

    res = run_pwsh_script(script, work_dir=tmp_path)
    assert res.returncode == 0, f"run-command failed: {res.stderr}"

    status_rows = _read_csv_rows(tmp_path / "report" / "status.csv")
    r_4095 = next(r for r in status_rows if r["vm_name"] == "vm-4095")
    r_4096 = next(r for r in status_rows if r["vm_name"] == "vm-4096")
    r_5000 = next(r for r in status_rows if r["vm_name"] == "vm-5000")

    assert int(r_4095["output_bytes"]) == 4095
    assert r_4095["possibly_truncated"] == "false"

    assert int(r_4096["output_bytes"]) == 4096
    assert r_4096["possibly_truncated"] == "true"

    assert int(r_5000["output_bytes"]) == 5000
    assert r_5000["possibly_truncated"] == "true"


def test_job_result_succeeded_with_issues_and_failed(tmp_path: Path):
    """TP-09: All ok -> Succeeded (no issue line); some VM fail -> SucceededWithIssues; breaker -> Failed."""
    require_container_runtime()

    # Case 1: All ok -> Succeeded
    script_all_ok = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    Write-Y2Targets -Path /work/targets.csv -Rows @(
        @{ vm_name = 'vm-ok'; resource_group = 'rg'; subscription = 's'; os_type = 'windows'; initial_power = 'PowerState/running'; tags = '' }
    )
    Write-Y2State -Path /work/state.csv -Rows @(
        @{ vm_name = 'vm-ok'; verified_power = 'PowerState/running'; started_by_run = 'false'; start = ''; run = 'ok'; restore = ''; output_bytes = 10; possibly_truncated = 'false' }
    )

    & /repo/ado/scripts/finalize.ps1 -WorkDir /work -ReportDir /work/report
    """
    res1 = run_pwsh_script(script_all_ok, work_dir=tmp_path / "case1")
    assert res1.returncode == 0
    assert "##vso[task.complete" not in res1.stdout

    # Case 2: Partial failure -> SucceededWithIssues
    script_partial = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    Write-Y2Targets -Path /work/targets.csv -Rows @(
        @{ vm_name = 'vm-ok'; resource_group = 'rg'; subscription = 's'; os_type = 'windows'; initial_power = 'PowerState/running'; tags = '' },
        @{ vm_name = 'vm-bad'; resource_group = 'rg'; subscription = 's'; os_type = 'windows'; initial_power = 'PowerState/running'; tags = '' }
    )
    Write-Y2State -Path /work/state.csv -Rows @(
        @{ vm_name = 'vm-ok'; verified_power = 'PowerState/running'; started_by_run = 'false'; start = ''; run = 'ok'; restore = ''; output_bytes = 10; possibly_truncated = 'false' },
        @{ vm_name = 'vm-bad'; verified_power = 'PowerState/running'; started_by_run = 'false'; start = ''; run = 'failed'; restore = ''; output_bytes = 0; possibly_truncated = 'false' }
    )

    & /repo/ado/scripts/finalize.ps1 -WorkDir /work -ReportDir /work/report
    """
    res2 = run_pwsh_script(script_partial, work_dir=tmp_path / "case2")
    assert res2.returncode == 0
    assert "##vso[task.complete result=SucceededWithIssues;]" in res2.stdout

    # Case 3: Breaker tripped / auth_aborted -> Failed
    script_auth_abort = """
    Import-Module /repo/tests/ado_fixtures/FakeArm.psm1
    Import-Module /repo/ado/scripts/Y2Az.psm1

    Write-Y2Targets -Path /work/targets.csv -Rows @(
        @{ vm_name = 'vm-aborted'; resource_group = 'rg'; subscription = 's'; os_type = 'windows'; initial_power = 'PowerState/running'; tags = '' }
    )
    Write-Y2State -Path /work/state.csv -Rows @(
        @{ vm_name = 'vm-aborted'; verified_power = 'PowerState/running'; started_by_run = 'false'; start = ''; run = 'auth_aborted'; restore = ''; output_bytes = 0; possibly_truncated = 'false' }
    )

    & /repo/ado/scripts/finalize.ps1 -WorkDir /work -ReportDir /work/report
    """
    res3 = run_pwsh_script(script_auth_abort, work_dir=tmp_path / "case3")
    assert res3.returncode == 1, f"Expected finalize exit 1 on auth_aborted, got {res3.returncode}"
