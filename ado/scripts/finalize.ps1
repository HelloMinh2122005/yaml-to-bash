# finalize.ps1 - Aggregate targets and state into status.csv, evaluate ADO job result, and print summary
# Part of ADO-native patch template package (R1). Compatible with Windows PowerShell 5.1 and pwsh 7+.
# Pure ASCII, LF line endings, BOM-less UTF-8.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$WorkDir,

    [Parameter(Mandatory = $true, Position = 1)]
    [string]$ReportDir
)

$ErrorActionPreference = 'Stop'

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
    $resolvedReportDir = [System.IO.Path]::GetFullPath($ReportDir)

    if (-not [System.IO.Directory]::Exists($resolvedReportDir)) {
        [System.IO.Directory]::CreateDirectory($resolvedReportDir) | Out-Null
    }

    $targetsPath = Join-Path $resolvedWorkDir "targets.csv"
    $statePath = Join-Path $resolvedWorkDir "state.csv"
    $statusPath = Join-Path $resolvedReportDir "status.csv"

    $statusColumns = @(
        'vm_name',
        'resource_group',
        'subscription',
        'initial_power',
        'verified_power',
        'start',
        'run',
        'restore',
        'output_bytes',
        'possibly_truncated'
    )

    $targets = if (Test-Path $targetsPath) { Read-Y2Targets -Path $targetsPath } else { @() }
    $stateList = if (Test-Path $statePath) { Read-Y2State -Path $statePath } else { @() }

    # Handle 0 VMs in targets
    if ($null -eq $targets -or $targets.Count -eq 0) {
        $headerLine = ($statusColumns -join ',') + "`n"
        Write-Y2File -Path $statusPath -Content $headerLine
        Write-Host "`nNo VMs processed (0 targets). Status CSV written."
        exit 0
    }

    # Index state by lowercase VM name
    $stateMap = @{}
    if ($stateList) {
        foreach ($s in $stateList) {
            $vName = if ($s -is [System.Collections.IDictionary]) { $s['vm_name'] } else { $s.vm_name }
            if (-not [string]::IsNullOrEmpty($vName)) {
                $stateMap[$vName.ToLowerInvariant()] = $s
            }
        }
    }

    # Join targets and state records
    $statusRows = @()
    $hasFailure = $false
    $hasAuthAbort = $false

    foreach ($t in $targets) {
        $vName = if ($t -is [System.Collections.IDictionary]) { $t['vm_name'] } else { $t.vm_name }
        $key = if (-not [string]::IsNullOrEmpty($vName)) { $vName.ToLowerInvariant() } else { "" }

        $s = if ($stateMap.ContainsKey($key)) { $stateMap[$key] } else { $null }

        $rg = if ($t -is [System.Collections.IDictionary]) { $t['resource_group'] } else { $t.resource_group }
        $sub = if ($t -is [System.Collections.IDictionary]) { $t['subscription'] } else { $t.subscription }
        $initPower = if ($t -is [System.Collections.IDictionary]) { $t['initial_power'] } else { $t.initial_power }

        $verifiedPower = ""
        $start = ""
        $run = ""
        $restore = ""
        $outputBytes = 0
        $possiblyTruncated = "false"

        if ($null -ne $s) {
            $verifiedPower = if ($s -is [System.Collections.IDictionary]) { $s['verified_power'] } else { $s.verified_power }
            $start = if ($s -is [System.Collections.IDictionary]) { $s['start'] } else { $s.start }
            $run = if ($s -is [System.Collections.IDictionary]) { $s['run'] } else { $s.run }
            $restore = if ($s -is [System.Collections.IDictionary]) { $s['restore'] } else { $s.restore }
            $outputBytes = if ($s -is [System.Collections.IDictionary]) { $s['output_bytes'] } else { $s.output_bytes }
            $possiblyTruncated = if ($s -is [System.Collections.IDictionary]) { $s['possibly_truncated'] } else { $s.possibly_truncated }
        }

        if ([string]::IsNullOrEmpty($verifiedPower)) { $verifiedPower = "" }
        if ([string]::IsNullOrEmpty($start)) { $start = "" }
        if ([string]::IsNullOrEmpty($run)) { $run = "" }
        if ([string]::IsNullOrEmpty($restore)) { $restore = "" }
        if ($null -eq $outputBytes) { $outputBytes = 0 }
        if ($possiblyTruncated -is [bool]) {
            $possiblyTruncated = if ($possiblyTruncated) { "true" } else { "false" }
        } elseif ([string]::IsNullOrEmpty($possiblyTruncated)) {
            $possiblyTruncated = "false"
        }

        # Check outcome for this VM
        if ($run -eq 'auth_aborted' -or $start -eq 'auth_aborted' -or $restore -eq 'auth_aborted') {
            $hasAuthAbort = $true
            $hasFailure = $true
        } elseif ($run -ne 'ok' -or $start -in @('start_failed', 'failed') -or $restore -in @('restore_failed', 'failed')) {
            $hasFailure = $true
        }

        $statusRows += [PSCustomObject][ordered]@{
            vm_name            = [string]$vName
            resource_group     = [string]$rg
            subscription       = [string]$sub
            initial_power      = [string]$initPower
            verified_power     = [string]$verifiedPower
            start              = [string]$start
            run                = [string]$run
            restore            = [string]$restore
            output_bytes       = [int]$outputBytes
            possibly_truncated = [string]$possiblyTruncated
        }
    }

    # Write status.csv (DoD 2, AC-9)
    $csvLines = ($statusRows | ConvertTo-Csv -NoTypeInformation) -join "`n"
    Write-Y2File -Path $statusPath -Content ($csvLines + "`n")

    # Print summary table to stdout (DoD 4, INV-5)
    Write-Host "`n=================== Patch Execution Summary ==================="
    $summary = $statusRows | Select-Object vm_name, initial_power, verified_power, start, run, restore, output_bytes, possibly_truncated
    $summary | Format-Table -AutoSize | Out-String | Write-Host

    # Check if auth circuit breaker tripped (DoD 3, TP-09)
    if ($hasAuthAbort -or (Test-Y2AuthTripped)) {
        Write-Error "Pipeline failed: Authentication circuit breaker tripped or VMs auth_aborted."
        exit 1
    }

    # Evaluate ADO completion result
    if ($hasFailure) {
        Write-Host "##vso[task.complete result=SucceededWithIssues;]Some VMs did not complete successfully."
    }

    exit 0

} catch {
    Write-Error "finalize error: $($_.Exception.Message)"
    exit 1
}
