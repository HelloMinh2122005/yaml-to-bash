# power-start.ps1 - Start non-running target VMs with verified state check and auth breaker
# Part of ADO-native patch template package (R1). Compatible with Windows PowerShell 5.1 and pwsh 7+.
# Pure ASCII, LF line endings, BOM-less UTF-8.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$WorkDir,

    [Parameter(Mandatory = $false, Position = 1)]
    [int]$PollSeconds = 15,

    [Parameter(Mandatory = $false, Position = 2)]
    [int]$TimeoutSeconds = 600
)

$ErrorActionPreference = 'Stop'

function Get-Y2PowerStateFromStatus {
    param([object]$StatusObj)

    if ($null -eq $StatusObj) {
        return $null
    }

    if ($StatusObj.PSObject.Properties['PowerState'] -and -not [string]::IsNullOrEmpty($StatusObj.PowerState)) {
        return [string]$StatusObj.PowerState
    }

    if ($StatusObj.PSObject.Properties['Statuses'] -and $null -ne $StatusObj.Statuses) {
        foreach ($s in $StatusObj.Statuses) {
            $code = if ($s -is [System.Collections.IDictionary]) {
                if ($s.ContainsKey('Code')) { $s['Code'] } elseif ($s.ContainsKey('code')) { $s['code'] } else { "" }
            } elseif ($s.PSObject.Properties['Code']) {
                $s.Code
            } elseif ($s.PSObject.Properties['code']) {
                $s.code
            } else {
                ""
            }

            if ([string]$code -like 'PowerState/*') {
                return [string]$code
            }
        }
    }

    return $null
}

try {
    # Ensure Y2Az module is loaded
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    if ([string]::IsNullOrEmpty($scriptDir)) {
        $scriptDir = $PSScriptRoot
    }
    if ([string]::IsNullOrEmpty($scriptDir)) {
        $scriptDir = "."
    }
    $modulePath = Join-Path $scriptDir 'Y2Az.psm1'
    if (Test-Path $modulePath) {
        Import-Module $modulePath -Force
    }

    $resolvedWorkDir = [System.IO.Path]::GetFullPath($WorkDir)
    $targetsPath = Join-Path $resolvedWorkDir "targets.csv"
    $statePath = Join-Path $resolvedWorkDir "state.csv"

    if (-not (Test-Path $targetsPath)) {
        Write-Host "No targets.csv found at $targetsPath. Nothing to start."
        exit 0
    }

    $targets = Read-Y2Targets -Path $targetsPath
    if ($null -eq $targets -or $targets.Count -eq 0) {
        Write-Host "targets.csv is empty. 0 VMs to process."
        exit 0
    }

    $stateList = Read-Y2State -Path $statePath
    if ($null -eq $stateList -or $stateList.Count -eq 0) {
        $stateList = @()
        foreach ($t in $targets) {
            $vName = if ($t.vm_name) { $t.vm_name } else { $t.Name }
            $stateList += [PSCustomObject][ordered]@{
                vm_name            = [string]$vName
                verified_power     = ''
                started_by_run     = 'false'
                start              = ''
                run                = ''
                restore            = ''
                output_bytes       = 0
                possibly_truncated = 'false'
            }
        }
        Write-Y2State -Path $statePath -Rows $stateList
    }

    # Index state by lowercase VM name
    $stateMap = @{}
    foreach ($s in $stateList) {
        $vName = if ($s -is [System.Collections.IDictionary]) { $s['vm_name'] } else { $s.vm_name }
        if (-not [string]::IsNullOrEmpty($vName)) {
            $stateMap[$vName.ToLowerInvariant()] = $s
        }
    }

    # Filter candidate subset (not running and not os_mismatch)
    $candidates = [System.Collections.Generic.List[object]]::new()

    foreach ($t in $targets) {
        $vName = if ($t -is [System.Collections.IDictionary]) { $t['vm_name'] } else { $t.vm_name }
        if ([string]::IsNullOrEmpty($vName)) {
            continue
        }

        $key = $vName.ToLowerInvariant()
        $stateRow = if ($stateMap.ContainsKey($key)) { $stateMap[$key] } else { $null }
        if ($null -eq $stateRow) {
            continue
        }

        # Check os_mismatch
        $startVal = if ($stateRow -is [System.Collections.IDictionary]) { $stateRow['start'] } else { $stateRow.start }
        $runVal = if ($stateRow -is [System.Collections.IDictionary]) { $stateRow['run'] } else { $stateRow.run }

        if ($runVal -eq 'os_mismatch' -or $startVal -eq 'os_mismatch' -or $startVal -eq 'skipped') {
            # Skip os_mismatch VMs completely (never queried, never started)
            continue
        }

        # Check initial_power
        $initPower = if ($t -is [System.Collections.IDictionary]) { $t['initial_power'] } else { $t.initial_power }
        if (-not [string]::IsNullOrEmpty($initPower) -and $initPower.Trim() -eq 'PowerState/running') {
            # Snapshot confirmed running: do not query Get-Y2VmStatus and do not start
            if ([string]::IsNullOrEmpty($stateRow.verified_power)) {
                $stateRow.verified_power = 'PowerState/running'
            }
            $stateRow.started_by_run = 'false'
            continue
        }

        $rg = if ($t -is [System.Collections.IDictionary]) {
            if ($t.ContainsKey('resource_group')) { $t['resource_group'] } else { "" }
        } elseif ($t.resource_group) {
            $t.resource_group
        } else {
            ""
        }

        $candidates.Add(@{
            Name          = [string]$vName
            ResourceGroup = [string]$rg
            TargetRow     = $t
            StateRow      = $stateRow
        })
    }

    # Phase 1: Re-read power state of candidate subset only
    $toSubmit = [System.Collections.Generic.List[object]]::new()

    foreach ($cand in $candidates) {
        if (Test-Y2AuthTripped) {
            break
        }

        $vName = $cand.Name
        $rg = $cand.ResourceGroup
        $stateRow = $cand.StateRow

        try {
            $statusObj = Get-Y2VmStatus -ResourceGroup $rg -Name $vName
            Record-Y2AuthSuccess

            $verifiedPower = Get-Y2PowerStateFromStatus -StatusObj $statusObj
            if ([string]::IsNullOrEmpty($verifiedPower)) {
                $verifiedPower = 'PowerState/unknown'
            }

            $stateRow.verified_power = $verifiedPower

            if ($verifiedPower -eq 'PowerState/running') {
                # VM is already running on re-read: do NOT issue start
                $stateRow.started_by_run = 'false'
                Write-Y2State -Path $statePath -Rows $stateList
            } else {
                # Still not running: queue for Start-AzVM
                $cand['VerifiedPower'] = $verifiedPower
                $toSubmit.Add($cand)
                Write-Y2State -Path $statePath -Rows $stateList
            }
        } catch {
            $errRec = $_
            $classification = Classify-Y2Error -ErrorObject $errRec
            if ($classification.Type -eq 'AuthFailure') {
                Record-Y2AuthFailure
                if (Test-Y2AuthTripped) {
                    break
                }
            }

            $stateRow.verified_power = 'PowerState/unknown'
            $stateRow.start = 'start_failed'
            Write-Y2State -Path $statePath -Rows $stateList
        }
    }

    if (Test-Y2AuthTripped) {
        foreach ($r in $stateList) {
            if ([string]::IsNullOrEmpty($r.start) -or $r.start -notin @('ok', 'skipped', 'os_mismatch', 'start_failed', 'auth_aborted')) {
                $r.start = 'auth_aborted'
            }
            if ([string]::IsNullOrEmpty($r.run) -or $r.run -notin @('ok', 'skipped', 'os_mismatch', 'start_failed', 'run_command_busy', 'auth_aborted')) {
                $r.run = 'auth_aborted'
            }
        }
        Write-Y2State -Path $statePath -Rows $stateList
        Write-Error "Authentication circuit breaker tripped during status check."
        exit 1
    }

    # Phase 2: Submit Start-AzVM -NoWait for still-not-running VMs
    $submittedVMs = [System.Collections.Generic.List[object]]::new()

    foreach ($cand in $toSubmit) {
        if (Test-Y2AuthTripped) {
            break
        }

        $vName = $cand.Name
        $rg = $cand.ResourceGroup
        $stateRow = $cand.StateRow

        try {
            $startRes = Start-Y2Vm -ResourceGroup $rg -Name $vName
            Record-Y2AuthSuccess

            # Accepted: write started_by_run=submitted immediately to disk (INV-9)
            $stateRow.started_by_run = 'submitted'
            Write-Y2State -Path $statePath -Rows $stateList

            $submittedVMs.Add($cand)
        } catch {
            $errRec = $_
            $classification = Classify-Y2Error -ErrorObject $errRec
            if ($classification.Type -eq 'AuthFailure') {
                Record-Y2AuthFailure
                if (Test-Y2AuthTripped) {
                    break
                }
            }

            $stateRow.start = 'start_failed'
            Write-Y2State -Path $statePath -Rows $stateList
        }
    }

    if (Test-Y2AuthTripped) {
        foreach ($r in $stateList) {
            if ([string]::IsNullOrEmpty($r.start) -or $r.start -notin @('ok', 'skipped', 'os_mismatch', 'start_failed', 'auth_aborted')) {
                $r.start = 'auth_aborted'
            }
            if ([string]::IsNullOrEmpty($r.run) -or $r.run -notin @('ok', 'skipped', 'os_mismatch', 'start_failed', 'run_command_busy', 'auth_aborted')) {
                $r.run = 'auth_aborted'
            }
        }
        Write-Y2State -Path $statePath -Rows $stateList
        Write-Error "Authentication circuit breaker tripped during VM start submission."
        exit 1
    }

    # Phase 3: Poll submitted VMs until running or timeout
    if ($submittedVMs.Count -gt 0) {
        $deadline = (Get-Y2Date).AddSeconds($TimeoutSeconds)
        $pending = [System.Collections.ArrayList]::new($submittedVMs)

        while ($pending.Count -gt 0) {
            if ((Get-Y2Date) -ge $deadline) {
                Write-Warning "Timeout waiting for VMs to start ($TimeoutSeconds seconds)."
                foreach ($cand in $pending) {
                    $cand.StateRow.start = 'start_failed'
                }
                Write-Y2State -Path $statePath -Rows $stateList
                break
            }

            Start-Y2Sleep -Seconds $PollSeconds

            $stillPending = [System.Collections.ArrayList]::new()

            foreach ($cand in $pending) {
                if (Test-Y2AuthTripped) {
                    break
                }

                $vName = $cand.Name
                $rg = $cand.ResourceGroup
                $stateRow = $cand.StateRow

                try {
                    $statusObj = Get-Y2VmStatus -ResourceGroup $rg -Name $vName
                    Record-Y2AuthSuccess

                    $pState = Get-Y2PowerStateFromStatus -StatusObj $statusObj
                    if ($pState -eq 'PowerState/running') {
                        $stateRow.started_by_run = 'true'
                        $stateRow.start = 'ok'
                        Write-Y2State -Path $statePath -Rows $stateList
                    } else {
                        $stillPending.Add($cand) | Out-Null
                    }
                } catch {
                    $errRec = $_
                    $classification = Classify-Y2Error -ErrorObject $errRec
                    if ($classification.Type -eq 'AuthFailure') {
                        Record-Y2AuthFailure
                        if (Test-Y2AuthTripped) {
                            break
                        }
                    }

                    $stillPending.Add($cand) | Out-Null
                }
            }

            if (Test-Y2AuthTripped) {
                foreach ($r in $stateList) {
                    if ([string]::IsNullOrEmpty($r.start) -or $r.start -notin @('ok', 'skipped', 'os_mismatch', 'start_failed', 'auth_aborted')) {
                        $r.start = 'auth_aborted'
                    }
                    if ([string]::IsNullOrEmpty($r.run) -or $r.run -notin @('ok', 'skipped', 'os_mismatch', 'start_failed', 'run_command_busy', 'auth_aborted')) {
                        $r.run = 'auth_aborted'
                    }
                }
                Write-Y2State -Path $statePath -Rows $stateList
                Write-Error "Authentication circuit breaker tripped during status polling."
                exit 1
            }

            $pending = $stillPending
        }
    }

    # Final flush to ensure ledger is up to date
    Write-Y2State -Path $statePath -Rows $stateList
    exit 0

} catch {
    if (Test-Y2AuthTripped) {
        if ($stateList -and $statePath) {
            foreach ($r in $stateList) {
                if ([string]::IsNullOrEmpty($r.start) -or $r.start -notin @('ok', 'skipped', 'os_mismatch', 'start_failed', 'auth_aborted')) {
                    $r.start = 'auth_aborted'
                }
                if ([string]::IsNullOrEmpty($r.run) -or $r.run -notin @('ok', 'skipped', 'os_mismatch', 'start_failed', 'run_command_busy', 'auth_aborted')) {
                    $r.run = 'auth_aborted'
                }
            }
            Write-Y2State -Path $statePath -Rows $stateList
        }
    } else {
        if ($stateList -and $statePath) {
            Write-Y2State -Path $statePath -Rows $stateList
        }
    }
    Write-Error "power-start error: $($_.Exception.Message)"
    exit 1
}
