# resolve.ps1 - Resolve target VMs via Azure Resource Graph with pagination and OS filtering
# Part of ADO-native patch template package (R1). Compatible with Windows PowerShell 5.1 and pwsh 7+.
# Pure ASCII, LF line endings, BOM-less UTF-8.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$TargetsKql,

    [Parameter(Mandatory = $true, Position = 1)]
    [ValidateSet('windows', 'linux', IgnoreCase = $true)]
    [string]$OsType,

    [Parameter(Mandatory = $true, Position = 2)]
    [string]$WorkDir,

    [Parameter(Mandatory = $true, Position = 3)]
    [string]$ReportDir,

    [Parameter(Mandatory = $true, Position = 4)]
    [string]$RunId
)

$ErrorActionPreference = 'Stop'

function Get-Y2ItemProperty {
    param(
        [object]$Item,
        [string[]]$Names
    )

    if ($null -eq $Item) {
        return $null
    }

    foreach ($n in $Names) {
        if ($Item -is [System.Collections.IDictionary]) {
            try {
                if ($Item.Contains($n)) {
                    $v = $Item[$n]
                    if ($null -ne $v) {
                        return $v
                    }
                }
            } catch {
                # Fall through to property reflection
            }
        }

        if ($Item.PSObject -and $Item.PSObject.Properties[$n]) {
            $v = $Item.PSObject.Properties[$n].Value
            if ($null -ne $v) {
                return $v
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

    if ([string]::IsNullOrWhiteSpace($TargetsKql)) {
        throw "TargetsKql parameter is required and cannot be empty."
    }

    $targetOsNormalized = $OsType.Trim().ToLowerInvariant()

    # Ensure directories exist
    $resolvedWorkDir = [System.IO.Path]::GetFullPath($WorkDir)
    $resolvedReportDir = [System.IO.Path]::GetFullPath($ReportDir)

    if (-not [System.IO.Directory]::Exists($resolvedWorkDir)) {
        [System.IO.Directory]::CreateDirectory($resolvedWorkDir) | Out-Null
    }
    if (-not [System.IO.Directory]::Exists($resolvedReportDir)) {
        [System.IO.Directory]::CreateDirectory($resolvedReportDir) | Out-Null
    }

    $targetsPath = Join-Path $resolvedWorkDir "targets.csv"
    $statePath = Join-Path $resolvedWorkDir "state.csv"

    # Export ADO variables and process environment
    $env:Y2_TARGETS_CSV = $targetsPath
    $env:Y2_REPORT_DIR = $resolvedReportDir
    $env:Y2_RUN_ID = $RunId

    Write-Host "##vso[task.setvariable variable=Y2_TARGETS_CSV]$targetsPath"
    Write-Host "##vso[task.setvariable variable=Y2_REPORT_DIR]$resolvedReportDir"
    Write-Host "##vso[task.setvariable variable=Y2_RUN_ID]$RunId"

    # Construct KQL query with projection and deterministic ordering
    $cleanKql = $TargetsKql.Trim().TrimEnd(';').Trim()

    if ($cleanKql -match '(?i)\bextend\b.*?\bosType\b' -or ($cleanKql -match '(?i)\bproject\b' -and $cleanKql -match '(?i)\bosType\b')) {
        $finalQuery = "$cleanKql`n| project id, name, resourceGroup, subscriptionId, osType, powerState, tags`n| order by name asc"
    } else {
        $finalQuery = "$cleanKql`n| extend osType = tostring(coalesce(properties.storageProfile.osDisk.osType, properties.osDisk.osType)), powerState = tostring(coalesce(properties.extended.instanceView.powerState.code, properties.instanceView.powerState.code))`n| project id, name, resourceGroup, subscriptionId, osType, powerState, tags`n| order by name asc"
    }

    # Paging query loop via Invoke-Y2Graph wrapper
    $allRows = [System.Collections.Generic.List[object]]::new()
    $skipToken = $null

    do {
        $graphParams = @{
            Query = $finalQuery
            First = 1000
        }
        if (-not [string]::IsNullOrEmpty($skipToken)) {
            $graphParams['SkipToken'] = $skipToken
        }

        $res = Invoke-Y2Graph @graphParams

        if ($null -ne $res) {
            $batch = $null
            if ($res -is [System.Collections.IDictionary]) {
                if ($res.ContainsKey('data')) { $batch = $res['data'] }
                elseif ($res.ContainsKey('Data')) { $batch = $res['Data'] }
                else { $batch = $res }
            } elseif ($res.PSObject.Properties['data']) {
                $batch = $res.data
            } elseif ($res.PSObject.Properties['Data']) {
                $batch = $res.Data
            } else {
                $batch = $res
            }

            if ($null -ne $batch) {
                if ($batch -is [System.Collections.IEnumerable] -and -not ($batch -is [string]) -and -not ($batch -is [System.Collections.IDictionary])) {
                    foreach ($item in $batch) {
                        if ($null -ne $item) {
                            $allRows.Add($item)
                        }
                    }
                } else {
                    $allRows.Add($batch)
                }
            }
        }

        $skipToken = $null
        if ($null -ne $res) {
            if ($res -is [System.Collections.IDictionary]) {
                if ($res.ContainsKey('SkipToken') -and -not [string]::IsNullOrEmpty($res['SkipToken'])) {
                    $skipToken = [string]$res['SkipToken']
                } elseif ($res.ContainsKey('skipToken') -and -not [string]::IsNullOrEmpty($res['skipToken'])) {
                    $skipToken = [string]$res['skipToken']
                }
            } elseif ($res.PSObject.Properties['SkipToken']) {
                $skipToken = [string]$res.SkipToken
            } elseif ($res.PSObject.Properties['skipToken']) {
                $skipToken = [string]$res.skipToken
            }
        }

    } while (-not [string]::IsNullOrEmpty($skipToken))

    # Deduplicate VMs by id
    $seenIds = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    $uniqueVMs = [System.Collections.Generic.List[object]]::new()

    foreach ($item in $allRows) {
        $vmId = Get-Y2ItemProperty -Item $item -Names @('id', 'Id', 'ID')
        if (-not [string]::IsNullOrEmpty($vmId)) {
            if ($seenIds.Add([string]$vmId)) {
                $uniqueVMs.Add($item)
            }
        } else {
            $vmName = Get-Y2ItemProperty -Item $item -Names @('name', 'Name', 'vm_name', 'VMName')
            if (-not [string]::IsNullOrEmpty($vmName)) {
                if ($seenIds.Add("name:" + [string]$vmName)) {
                    $uniqueVMs.Add($item)
                }
            } else {
                $uniqueVMs.Add($item)
            }
        }
    }

    # Build target and state records
    $targetRows = @()
    $stateRows = @()

    foreach ($vm in $uniqueVMs) {
        $vmId = Get-Y2ItemProperty -Item $vm -Names @('id', 'Id', 'ID')

        $name = Get-Y2ItemProperty -Item $vm -Names @('name', 'Name', 'vm_name', 'VMName')
        if ([string]::IsNullOrEmpty($name) -and -not [string]::IsNullOrEmpty($vmId)) {
            $name = [System.IO.Path]::GetFileName([string]$vmId)
        }

        $rg = Get-Y2ItemProperty -Item $vm -Names @('resourceGroup', 'ResourceGroup', 'resource_group', 'ResourceGroupName', 'rg')
        if ([string]::IsNullOrEmpty($rg) -and -not [string]::IsNullOrEmpty($vmId)) {
            if ($vmId -match '/resource[Gg]roups/([^/]+)/') {
                $rg = $Matches[1]
            }
        }

        $sub = Get-Y2ItemProperty -Item $vm -Names @('subscriptionId', 'SubscriptionId', 'subscription', 'Subscription', 'sub')
        if ([string]::IsNullOrEmpty($sub) -and -not [string]::IsNullOrEmpty($vmId)) {
            if ($vmId -match '/subscriptions/([^/]+)/') {
                $sub = $Matches[1]
            }
        }

        $osRaw = Get-Y2ItemProperty -Item $vm -Names @('osType', 'OsType', 'os_type', 'OSType')
        if ([string]::IsNullOrEmpty($osRaw)) {
            $props = Get-Y2ItemProperty -Item $vm -Names @('properties', 'Properties')
            if ($props) {
                if ($props.storageProfile -and $props.storageProfile.osDisk -and $props.storageProfile.osDisk.osType) {
                    $osRaw = [string]$props.storageProfile.osDisk.osType
                } elseif ($props.osDisk -and $props.osDisk.osType) {
                    $osRaw = [string]$props.osDisk.osType
                }
            }
        }
        $osNormalized = if (-not [string]::IsNullOrEmpty($osRaw)) {
            ([string]$osRaw).Trim().ToLowerInvariant()
        } else {
            'unknown'
        }

        $power = Get-Y2ItemProperty -Item $vm -Names @('powerState', 'PowerState', 'initial_power', 'power')
        if ([string]::IsNullOrEmpty($power)) {
            $props = Get-Y2ItemProperty -Item $vm -Names @('properties', 'Properties')
            if ($props) {
                if ($props.extended -and $props.extended.instanceView -and $props.extended.instanceView.powerState -and $props.extended.instanceView.powerState.code) {
                    $power = [string]$props.extended.instanceView.powerState.code
                } elseif ($props.instanceView -and $props.instanceView.powerState -and $props.instanceView.powerState.code) {
                    $power = [string]$props.instanceView.powerState.code
                } elseif ($props.powerState) {
                    $power = [string]$props.powerState
                }
            }
        }
        $powerStr = if (-not [string]::IsNullOrEmpty($power)) {
            ([string]$power).Trim()
        } else {
            ''
        }

        $tags = Get-Y2ItemProperty -Item $vm -Names @('tags', 'Tags')
        $tagsStr = if ($null -eq $tags) {
            ""
        } elseif ($tags -is [string]) {
            $tags
        } elseif ($tags -is [System.Collections.IDictionary] -or $tags.PSObject) {
            $count = 0
            if ($tags -is [System.Collections.IDictionary]) {
                $count = $tags.Count
            } elseif ($tags.PSObject.Properties) {
                $count = @($tags.PSObject.Properties).Count
            }
            if ($count -gt 0) {
                $tags | ConvertTo-Json -Compress -Depth 10
            } else {
                ""
            }
        } else {
            ""
        }

        $targetRows += [PSCustomObject][ordered]@{
            vm_name        = [string]$name
            resource_group = [string]$rg
            subscription   = [string]$sub
            os_type        = $osNormalized
            initial_power  = $powerStr
            tags           = $tagsStr
        }

        $isMismatch = ($osNormalized -ne $targetOsNormalized)

        $stateRows += [PSCustomObject][ordered]@{
            vm_name            = [string]$name
            verified_power     = ''
            started_by_run     = 'false'
            start              = if ($isMismatch) { 'skipped' } else { '' }
            run                = if ($isMismatch) { 'os_mismatch' } else { '' }
            restore            = if ($isMismatch) { 'skipped' } else { '' }
            output_bytes       = 0
            possibly_truncated = 'false'
        }
    }

    # Write output files
    Write-Y2Targets -Path $targetsPath -Rows $targetRows
    Write-Y2State -Path $statePath -Rows $stateRows

    # If 0 VMs match, write header-only status.csv in ReportDir as well
    if ($uniqueVMs.Count -eq 0) {
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
        $statusHeader = ($statusColumns -join ',') + "`n"
        $statusPath = Join-Path $resolvedReportDir "status.csv"
        Write-Y2File -Path $statusPath -Content $statusHeader
    }

    exit 0

} catch {
    Write-Error "Resolve error: $($_.Exception.Message)"
    exit 1
}
