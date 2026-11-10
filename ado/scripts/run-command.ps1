# run-command.ps1 - Execute payload script concurrently on target VMs with throttling and error resilience
# Part of ADO-native patch template package (R1). Compatible with Windows PowerShell 5.1 and pwsh 7+.
# Pure ASCII, LF line endings, BOM-less UTF-8.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('windows', 'linux', IgnoreCase = $true)]
    [string]$OsType,

    [Parameter(Mandatory = $true, Position = 1)]
    [Alias('ScriptPath', 'PayloadPath')]
    [string]$PayloadFile,

    [Parameter(Mandatory = $true, Position = 2)]
    [string]$WorkDir,

    [Parameter(Mandatory = $true, Position = 3)]
    [string]$ReportDir,

    [Parameter(Mandatory = $false, Position = 4)]
    [int]$MaxJobs = 20,

    [Parameter(Mandatory = $false, Position = 5)]
    [int]$PollSeconds = 15
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

    if ($MaxJobs -le 0) {
        $MaxJobs = 20
    }
    if ($PollSeconds -le 0) {
        $PollSeconds = 15
    }

    # Verify payload file exists (DoD 1)
    $resolvedPayload = if ($ExecutionContext -and $ExecutionContext.SessionState) {
        $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($PayloadFile)
    } else {
        [System.IO.Path]::GetFullPath($PayloadFile)
    }

    if (-not (Test-Path $resolvedPayload)) {
        Write-Error "Payload file does not exist: $PayloadFile"
        exit 1
    }

    # Map OsType to CommandId (DoD 1)
    $commandId = if ($OsType.Trim().ToLowerInvariant() -eq 'windows') {
        'RunPowerShellScript'
    } else {
        'RunShellScript'
    }

    # Resolve paths and directories
    $resolvedWorkDir = [System.IO.Path]::GetFullPath($WorkDir)
    $resolvedReportDir = [System.IO.Path]::GetFullPath($ReportDir)

    if (-not [System.IO.Directory]::Exists($resolvedReportDir)) {
        [System.IO.Directory]::CreateDirectory($resolvedReportDir) | Out-Null
    }

    $targetsPath = Join-Path $resolvedWorkDir "targets.csv"
    $statePath = Join-Path $resolvedWorkDir "state.csv"

    if (-not (Test-Path $targetsPath)) {
        Write-Host "No targets.csv found at $targetsPath. Nothing to run."
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

    # Mark run='skipped' for VMs with start_failed (DoD 2)
    foreach ($s in $stateList) {
        $startVal = if ($s -is [System.Collections.IDictionary]) { $s['start'] } else { $s.start }
        $runVal = if ($s -is [System.Collections.IDictionary]) { $s['run'] } else { $s.run }
        if ($startVal -eq 'start_failed' -and [string]::IsNullOrEmpty($runVal)) {
            if ($s -is [System.Collections.IDictionary]) {
                $s['run'] = 'skipped'
            } else {
                $s.run = 'skipped'
            }
        }
    }
    Write-Y2State -Path $statePath -Rows $stateList

    # Execute jobs through Invoke-Y2JobPool (bounded by MaxJobs, 429 backoff, 409 re-queuing)
    $poolResult = Invoke-Y2JobPool -VMs $targets `
                                   -CommandId $commandId `
                                   -ScriptPath $resolvedPayload `
                                   -MaxJobs $MaxJobs `
                                   -PollSeconds $PollSeconds `
                                   -StateCsvPath $statePath `
                                   -ReportDir $resolvedReportDir `
                                   -StateRecords $stateList

    # Check if circuit breaker tripped
    if (Test-Y2AuthTripped) {
        Write-Error "Authentication circuit breaker tripped during run command execution."
        exit 1
    }

    exit 0

} catch {
    Write-Error "run-command error: $($_.Exception.Message)"
    if ($stateList -and $statePath) {
        Write-Y2State -Path $statePath -Rows $stateList
    }
    exit 1
}
