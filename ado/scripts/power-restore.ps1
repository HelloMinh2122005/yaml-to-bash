# power-restore.ps1 - Restore started target VMs to their pre-run power state with auth resilience
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

function Get-Y2RowProperty {
    param(
        [object]$Row,
        [string]$Name
    )

    if ($null -eq $Row) {
        return $null
    }

    if ($Row -is [System.Collections.IDictionary]) {
        if ($Row.ContainsKey($Name)) {
            return $Row[$Name]
        }
        return $null
    }

    if ($Row.PSObject -and $Row.PSObject.Properties[$Name]) {
        return $Row.$Name
    }

    return $null
}

function Set-Y2RowProperty {
    param(
        [object]$Row,
        [string]$Name,
        [object]$Value
    )

    if ($null -eq $Row) {
        return
    }

    if ($Row -is [System.Collections.IDictionary]) {
        $Row[$Name] = $Value
    } elseif ($Row.PSObject -and $Row.PSObject.Properties[$Name]) {
        $Row.$Name = $Value
    } else {
        $Row | Add-Member -NotePropertyName $Name -NotePropertyValue $Value -Force
    }
}

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

function Test-Y2PowerStateMatches {
    param(
        [string]$Actual,
        [string]$Target
    )

    if ([string]::IsNullOrEmpty($Actual) -or [string]::IsNullOrEmpty($Target)) {
        return $false
    }

    $normActual = $Actual.Trim().ToLowerInvariant()
    $normTarget = $Target.Trim().ToLowerInvariant()

    if ($normActual -eq $normTarget) {
        return $true
    }

    $isActualDeallocated = ($normActual -eq 'powerstate/deallocated' -or $normActual -eq 'deallocated')
    $isTargetDeallocated = ($normTarget -eq 'powerstate/deallocated' -or $normTarget -eq 'deallocated')
    if ($isActualDeallocated -and $isTargetDeallocated) {
        return $true
    }

    $isActualStopped = ($normActual -eq 'powerstate/stopped' -or $normActual -eq 'stopped')
    $isTargetStopped = ($normTarget -eq 'powerstate/stopped' -or $normTarget -eq 'stopped')
    if ($isActualStopped -and $isTargetStopped) {
        return $true
    }

    return $false
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

    $resolvedWorkDir = if ($ExecutionContext -and $ExecutionContext.SessionState) {
        $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($WorkDir)
    } else {
        [System.IO.Path]::GetFullPath($WorkDir)
    }

    $statePath = Join-Path $resolvedWorkDir "state.csv"
    $targetsPath = Join-Path $resolvedWorkDir "targets.csv"

    # If state.csv does not exist, resolve never ran; exit 0 cleanly (AC-6, DoD 2)
    if (-not (Test-Path $statePath)) {
        Write-Host "No state.csv found at $statePath. Resolve never ran; nothing to restore."
        exit 0
    }

    $stateList = Read-Y2State -Path $statePath
    if ($null -eq $stateList -or $stateList.Count -eq 0) {
        Write-Host "state.csv is empty. 0 VMs to process."
        exit 0
    }

    # Read targets.csv to map vm_name -> resource_group and fallback power state
    $targets = if (Test-Path $targetsPath) {
        Read-Y2Targets -Path $targetsPath
    } else {
        @()
    }

    $targetMap = @{}
    if ($targets) {
        foreach ($t in $targets) {
            $tName = Get-Y2RowProperty -Row $t -Name 'vm_name'
            if ([string]::IsNullOrEmpty($tName)) {
                $tName = Get-Y2RowProperty -Row $t -Name 'Name'
            }
            if (-not [string]::IsNullOrEmpty($tName)) {
                $targetMap[$tName.ToLowerInvariant()] = $t
            }
        }
    }

    # Reset auth failure counter for restore (AC-5, TP-05, DoD 2)
    # Works when breaker tripped in earlier steps, but restore itself is bounded by 5 failures.
    Reset-Y2AuthCounter

    # Filter candidate VMs: ONLY rows where started_by_run is 'submitted' or 'true' (AC-6, INV-3)
    $candidates = [System.Collections.Generic.List[object]]::new()

    foreach ($row in $stateList) {
        $vName = Get-Y2RowProperty -Row $row -Name 'vm_name'
        if ([string]::IsNullOrEmpty($vName)) {
            continue
        }

        $sbr = Get-Y2RowProperty -Row $row -Name 'started_by_run'
        $sbrStr = "$sbr".Trim().ToLowerInvariant()

        # VMs running at snapshot (where started_by_run is 'false' or empty) are NEVER stopped or touched
        if ($sbrStr -ne 'submitted' -and $sbrStr -ne 'true') {
            continue
        }

        # Resolve resource group from targets or state
        $tObj = if ($targetMap.ContainsKey($vName.ToLowerInvariant())) {
            $targetMap[$vName.ToLowerInvariant()]
        } else {
            $null
        }

        $rg = ""
        if ($null -ne $tObj) {
            $rg = [string](Get-Y2RowProperty -Row $tObj -Name 'resource_group')
        }
        if ([string]::IsNullOrEmpty($rg)) {
            $rg = [string](Get-Y2RowProperty -Row $row -Name 'resource_group')
        }

        if ([string]::IsNullOrEmpty($rg)) {
            Write-Warning "ResourceGroup for VM '$vName' could not be resolved from targets.csv or state.csv."
            Set-Y2RowProperty -Row $row -Name 'restore' -Value 'failed'
            Write-Y2State -Path $statePath -Rows $stateList
            continue
        }

        # Determine target power state from verified_power (fallback to initial_power)
        $vPower = [string](Get-Y2RowProperty -Row $row -Name 'verified_power')
        if ([string]::IsNullOrEmpty($vPower) -or $vPower.Trim().ToLowerInvariant() -eq 'powerstate/unknown') {
            if ($null -ne $tObj) {
                $initP = [string](Get-Y2RowProperty -Row $tObj -Name 'initial_power')
                if (-not [string]::IsNullOrEmpty($initP)) {
                    $vPower = $initP
                }
            }
        }

        $normVPower = "$vPower".Trim().ToLowerInvariant()
        if ($normVPower -eq 'powerstate/running' -or $normVPower -eq 'running') {
            # Target power was running; do not stop
            continue
        }

        $isStopped = ($normVPower -eq 'powerstate/stopped' -or $normVPower -eq 'stopped')
        $targetPower = if ($isStopped) { 'PowerState/stopped' } else { 'PowerState/deallocated' }

        $candidates.Add(@{
            Name            = [string]$vName
            ResourceGroup   = [string]$rg
            StayProvisioned = $isStopped
            TargetPower     = $targetPower
            StateRow        = $row
        })
    }

    if ($candidates.Count -eq 0) {
        Write-Host "No VMs were started by this run. Nothing to restore."
        exit 0
    }

    # Phase 1: Dispatch ALL restore calls BEFORE any status checks (AC-6, INV-3, INV-9, DoD 2)
    $submittedVMs = [System.Collections.Generic.List[object]]::new()

    foreach ($cand in $candidates) {
        if (Test-Y2AuthTripped) {
            break
        }

        $vName = $cand.Name
        $rg = $cand.ResourceGroup
        $stateRow = $cand.StateRow

        try {
            if ($cand.StayProvisioned) {
                Stop-Y2Vm -ResourceGroup $rg -Name $vName -StayProvisioned | Out-Null
            } else {
                Stop-Y2Vm -ResourceGroup $rg -Name $vName | Out-Null
            }
            Record-Y2AuthSuccess

            # Accepted: update restore=submitted immediately to disk (INV-9)
            Set-Y2RowProperty -Row $stateRow -Name 'restore' -Value 'submitted'
            Write-Y2State -Path $statePath -Rows $stateList

            $submittedVMs.Add($cand)
        } catch {
            $errRec = $_
            $classification = Classify-Y2Error -ErrorObject $errRec
            if ($classification.Type -eq 'AuthFailure') {
                Record-Y2AuthFailure -ErrorObject $errRec | Out-Null
                if (Test-Y2AuthTripped) {
                    Set-Y2RowProperty -Row $stateRow -Name 'restore' -Value 'auth_aborted'
                    Write-Y2State -Path $statePath -Rows $stateList
                    break
                }
            }

            Write-Warning "Failed to submit restore for VM ${vName}: $($errRec.Exception.Message)"
            Set-Y2RowProperty -Row $stateRow -Name 'restore' -Value 'failed'
            Write-Y2State -Path $statePath -Rows $stateList
        }
    }

    if (Test-Y2AuthTripped) {
        foreach ($cand in $candidates) {
            $sRow = $cand.StateRow
            $curRestore = [string](Get-Y2RowProperty -Row $sRow -Name 'restore')
            if ([string]::IsNullOrEmpty($curRestore) -or $curRestore -eq 'submitted') {
                Set-Y2RowProperty -Row $sRow -Name 'restore' -Value 'auth_aborted'
            }
        }
        Write-Y2State -Path $statePath -Rows $stateList
        Write-Error "Authentication circuit breaker tripped during restore submission."
        exit 1
    }

    # Phase 2: Poll status of all submitted VMs until desired power state confirmed or timeout
    if ($submittedVMs.Count -gt 0) {
        $deadline = (Get-Y2Date).AddSeconds($TimeoutSeconds)
        $pending = [System.Collections.ArrayList]::new($submittedVMs)

        while ($pending.Count -gt 0) {
            if ((Get-Y2Date) -ge $deadline) {
                Write-Warning "Timeout waiting for VMs to restore ($TimeoutSeconds seconds)."
                foreach ($cand in $pending) {
                    Set-Y2RowProperty -Row $cand.StateRow -Name 'restore' -Value 'failed'
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
                    if (Test-Y2PowerStateMatches -Actual $pState -Target $cand.TargetPower) {
                        Set-Y2RowProperty -Row $stateRow -Name 'restore' -Value 'ok'
                        Write-Y2State -Path $statePath -Rows $stateList
                    } else {
                        $stillPending.Add($cand) | Out-Null
                    }
                } catch {
                    $errRec = $_
                    $classification = Classify-Y2Error -ErrorObject $errRec
                    if ($classification.Type -eq 'AuthFailure') {
                        Record-Y2AuthFailure -ErrorObject $errRec | Out-Null
                        if (Test-Y2AuthTripped) {
                            break
                        }
                    }

                    $stillPending.Add($cand) | Out-Null
                }
            }

            if (Test-Y2AuthTripped) {
                foreach ($cand in $pending) {
                    Set-Y2RowProperty -Row $cand.StateRow -Name 'restore' -Value 'auth_aborted'
                }
                Write-Y2State -Path $statePath -Rows $stateList
                Write-Error "Authentication circuit breaker tripped during restore polling."
                exit 1
            }

            $pending = $stillPending
        }
    }

    # Final flush to ensure ledger is on disk (INV-9)
    Write-Y2State -Path $statePath -Rows $stateList
    exit 0

} catch {
    Write-Error "power-restore error: $($_.Exception.Message)"
    if ($stateList -and $statePath) {
        try {
            Write-Y2State -Path $statePath -Rows $stateList
        } catch {
            # Ignore secondary write failure
        }
    }
    exit 1
}
