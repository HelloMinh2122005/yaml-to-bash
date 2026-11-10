# FakeArm.psm1 - Fake Azure PowerShell harness for Tier 2 container tests
# Simulates Search-AzGraph, Get-AzVM, Start-AzVM, Stop-AzVM, Invoke-AzVMRunCommand (-AsJob),
# Get-Job, Receive-Job, Remove-Job, and overrides Start-Y2Sleep with virtual time.
# Compatible with Windows PowerShell 5.1 and PowerShell Core 7+.
# Pure ASCII, LF line endings, BOM-less UTF-8.

$script:Fleet = [System.Collections.Generic.List[object]]::new()
$script:FleetMap = @{}
$script:CallLog = [System.Collections.ArrayList]::new()
$script:Jobs = @{}
$script:ActiveJobCount = 0
$script:PeakConcurrentJobs = 0
$script:VirtualTime = [DateTime]::UtcNow
$script:ErrorRules = [System.Collections.ArrayList]::new()
$script:JobOutputs = @{}
$script:NextJobId = 1
$script:PagingOverride = $null

function Record-FakeArmCall {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Cmdlet,

        [Parameter(Mandatory = $false)]
        [hashtable]$Parameters = @{}
    )

    $paramsCopy = @{}
    if ($null -ne $Parameters) {
        foreach ($k in $Parameters.Keys) {
            $val = $Parameters[$k]
            if ($val -is [switch]) {
                $paramsCopy[$k] = [bool]$val.IsPresent
            } else {
                $paramsCopy[$k] = $val
            }
        }
    }

    $entry = [PSCustomObject]@{
        Index          = $script:CallLog.Count + 1
        Cmdlet         = $Cmdlet
        Parameters     = $paramsCopy
        Timestamp      = $script:VirtualTime.ToString("o")
        ConcurrentJobs = $script:ActiveJobCount
    }

    $script:CallLog.Add($entry) | Out-Null
}

function Get-FakeArmVirtualTime {
    [CmdletBinding()]
    param()
    return $script:VirtualTime
}

function Set-FakeArmVirtualTime {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $false, Position = 0)]
        [object]$Time = $null
    )
    if ($null -eq $Time) {
        $script:VirtualTime = [DateTime]::UtcNow
    } else {
        $script:VirtualTime = [DateTime]$Time
    }
    $setY2Time = Get-Command -Name 'Set-Y2VirtualTime' -ErrorAction SilentlyContinue
    if ($setY2Time) {
        & $setY2Time -Time $script:VirtualTime
    }
}

function Advance-FakeArmVirtualTime {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [double]$Seconds
    )
    $script:VirtualTime = $script:VirtualTime.AddSeconds($Seconds)
    $setY2Time = Get-Command -Name 'Set-Y2VirtualTime' -ErrorAction SilentlyContinue
    if ($setY2Time) {
        & $setY2Time -Time $script:VirtualTime
    }
}

function Start-Y2Sleep {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [double]$Seconds
    )
    Advance-FakeArmVirtualTime -Seconds $Seconds
    Record-FakeArmCall -Cmdlet 'Start-Y2Sleep' -Parameters @{ Seconds = $Seconds }
}

function Start-Sleep {
    [CmdletBinding(DefaultParameterSetName = 'Seconds')]
    param(
        [Parameter(ParameterSetName = 'Seconds', Mandatory = $false, Position = 0)]
        [int]$Seconds,

        [Parameter(ParameterSetName = 'Milliseconds', Mandatory = $false)]
        [int]$Milliseconds
    )
    $sec = 0.0
    if ($PSCmdlet.ParameterSetName -eq 'Milliseconds') {
        $sec = [double]$Milliseconds / 1000.0
    } else {
        $sec = [double]$Seconds
    }
    Start-Y2Sleep -Seconds $sec
}

function Set-FakeArmFleet {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [AllowEmptyCollection()]
        [object[]]$Fleet
    )
    $script:Fleet.Clear()
    $script:FleetMap.Clear()
    foreach ($item in $Fleet) {
        $name = ""
        if ($item -is [System.Collections.IDictionary]) {
            if ($item.ContainsKey('name')) { $name = [string]$item['name'] }
            elseif ($item.ContainsKey('vm_name')) { $name = [string]$item['vm_name'] }
        } elseif ($item.PSObject.Properties['name']) {
            $name = [string]$item.name
        } elseif ($item.PSObject.Properties['vm_name']) {
            $name = [string]$item.vm_name
        }

        $script:Fleet.Add($item)
        if (-not [string]::IsNullOrEmpty($name)) {
            $script:FleetMap[$name.ToLower()] = $item
        }
    }
}

function Import-FakeArmFleet {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $false, Position = 0)]
        [string]$Path = ""
    )
    if ([string]::IsNullOrEmpty($Path)) {
        $Path = Join-Path $PSScriptRoot "vm_fleet.json"
    }
    if (Test-Path $Path) {
        $raw = Get-Content -Path $Path -Raw
        $loaded = $raw | ConvertFrom-Json
        Set-FakeArmFleet -Fleet @($loaded)
    }
}

function Get-FakeArmFleet {
    [CmdletBinding()]
    param()
    return @($script:Fleet)
}

function Set-FakeArmVmStatus {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [string]$Name,

        [Parameter(Mandatory = $true, Position = 1)]
        [string]$PowerState
    )
    $key = $Name.ToLower()
    if ($script:FleetMap.ContainsKey($key)) {
        $vm = $script:FleetMap[$key]
        if ($vm -is [System.Collections.IDictionary]) {
            $vm['powerState'] = $PowerState
            $vm['initial_power'] = $PowerState
        } else {
            if ($vm.PSObject.Properties['powerState']) { $vm.powerState = $PowerState }
            else { $vm | Add-Member -NotePropertyName 'powerState' -NotePropertyValue $PowerState -Force }
            if ($vm.PSObject.Properties['initial_power']) { $vm.initial_power = $PowerState }
            else { $vm | Add-Member -NotePropertyName 'initial_power' -NotePropertyValue $PowerState -Force }
        }
        return
    }
}

function Set-FakeArmPagingTest {
    <#
    .SYNOPSIS
        Configures synthetic paging test for Search-AzGraph (e.g. 2,500 VMs across 3 pages: 1000, 1000, 500).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $false)]
        [int]$Count = 2500,

        [Parameter(Mandatory = $false)]
        [int]$PageSize = 1000,

        [Parameter(Mandatory = $false)]
        [string]$OsType = "Windows",

        [Parameter(Mandatory = $false)]
        [string]$PowerState = "PowerState/running"
    )

    $syntheticFleet = [System.Collections.Generic.List[object]]::new()
    for ($i = 1; $i -le $Count; $i++) {
        $vmName = "vm-paged-{0:D4}" -f $i
        $subId = "sub-00000000-0000-0000-0000-000000000001"
        $rg = "rg-patch-paged"
        $vmObj = [PSCustomObject][ordered]@{
            id             = "/subscriptions/$subId/resourceGroups/$rg/providers/Microsoft.Compute/virtualMachines/$vmName"
            name           = $vmName
            vm_name        = $vmName
            resourceGroup  = $rg
            rg             = $rg
            subscriptionId = $subId
            sub            = $subId
            subscription   = $subId
            osType         = $OsType
            os_type        = $OsType.ToLower()
            powerState     = $PowerState
            initial_power  = $PowerState
            tags           = @{
                Environment = "Production"
                Index       = $i
            }
        }
        $syntheticFleet.Add($vmObj)
    }

    Set-FakeArmFleet -Fleet @($syntheticFleet)
    $script:PagingOverride = @{
        Count    = $Count
        PageSize = $PageSize
    }
}

function New-FakeArmErrorRecord {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $false)]
        [int]$StatusCode = 500,

        [Parameter(Mandatory = $false)]
        [object]$RetryAfter = $null,

        [Parameter(Mandatory = $false)]
        [string]$Message = "",

        [Parameter(Mandatory = $false)]
        [string]$ErrorType = "GeneralFailure"
    )

    if ([string]::IsNullOrEmpty($Message)) {
        if ($StatusCode -eq 409) {
            $Message = "Conflict: The run command extension execution is in progress."
        } elseif ($StatusCode -eq 429) {
            $Message = "TooManyRequests: Request was throttled by Azure Resource Manager."
        } elseif ($StatusCode -eq 401) {
            $Message = "Unauthorized: Invalid authentication token or credentials."
        } elseif ($StatusCode -eq 403) {
            $Message = "AuthorizationFailed: The client does not have authorization to perform action."
        } else {
            $Message = "Azure operation failed with status code $StatusCode."
        }
    }

    $ex = [System.InvalidOperationException]::new($Message)
    $headers = @{}
    if ($null -ne $RetryAfter) {
        $headers['Retry-After'] = @([string]$RetryAfter)
        $headers['retry-after'] = @([string]$RetryAfter)
    }

    $resp = [PSCustomObject]@{
        StatusCode = [int]$StatusCode
        Headers    = $headers
    }

    $ex | Add-Member -NotePropertyName 'Response' -NotePropertyValue $resp -Force
    $ex | Add-Member -NotePropertyName 'StatusCode' -NotePropertyValue [int]$StatusCode -Force
    $ex | Add-Member -NotePropertyName 'Status' -NotePropertyValue [int]$StatusCode -Force

    $errorId = "AzureClientError_$StatusCode"
    if ($StatusCode -eq 409) { $errorId = "ConflictOperationInProgress" }
    elseif ($StatusCode -eq 429) { $errorId = "TooManyRequests" }
    elseif ($StatusCode -eq 401) { $errorId = "InvalidAuthenticationToken" }
    elseif ($StatusCode -eq 403) { $errorId = "AuthorizationFailed" }

    return [System.Management.Automation.ErrorRecord]::new(
        $ex,
        $errorId,
        [System.Management.Automation.ErrorCategory]::InvalidOperation,
        $null
    )
}

function Set-FakeArmErrorRule {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Cmdlet,

        [Parameter(Mandatory = $false)]
        [string]$VmName = '*',

        [Parameter(Mandatory = $false)]
        [int]$StatusCode = 500,

        [Parameter(Mandatory = $false)]
        [string]$ErrorType = 'GeneralFailure',

        [Parameter(Mandatory = $false)]
        [object]$RetryAfter = $null,

        [Parameter(Mandatory = $false)]
        [string]$Message = '',

        [Parameter(Mandatory = $false)]
        [int]$Count = 1,

        [Parameter(Mandatory = $false)]
        [string]$Stage = 'Submit'
    )

    $rule = @{
        Cmdlet     = $Cmdlet
        VmName     = $VmName
        StatusCode = $StatusCode
        ErrorType  = $ErrorType
        RetryAfter = $RetryAfter
        Message    = $Message
        Count      = $Count
        Stage      = $Stage
    }

    $script:ErrorRules.Add($rule) | Out-Null
}

function Clear-FakeArmErrorRules {
    [CmdletBinding()]
    param()
    $script:ErrorRules.Clear()
}

function Match-FakeArmErrorRule {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Cmdlet,

        [Parameter(Mandatory = $false)]
        [string]$VmName = '',

        [Parameter(Mandatory = $false)]
        [string]$Stage = 'Submit'
    )

    for ($i = 0; $i -lt $script:ErrorRules.Count; $i++) {
        $r = $script:ErrorRules[$i]
        $cmdletMatch = ($r.Cmdlet -eq '*' -or $r.Cmdlet -eq $Cmdlet)
        if (-not $cmdletMatch) {
            if (($r.Cmdlet -eq 'Start-Y2RunJob' -and $Cmdlet -eq 'Invoke-AzVMRunCommand') -or
                ($r.Cmdlet -eq 'Invoke-AzVMRunCommand' -and $Cmdlet -eq 'Start-Y2RunJob') -or
                ($r.Cmdlet -eq 'Start-Y2Vm' -and $Cmdlet -eq 'Start-AzVM') -or
                ($r.Cmdlet -eq 'Start-AzVM' -and $Cmdlet -eq 'Start-Y2Vm') -or
                ($r.Cmdlet -eq 'Stop-Y2Vm' -and $Cmdlet -eq 'Stop-AzVM') -or
                ($r.Cmdlet -eq 'Stop-AzVM' -and $Cmdlet -eq 'Stop-Y2Vm') -or
                ($r.Cmdlet -eq 'Invoke-Y2Graph' -and $Cmdlet -eq 'Search-AzGraph') -or
                ($r.Cmdlet -eq 'Search-AzGraph' -and $Cmdlet -eq 'Invoke-Y2Graph')) {
                $cmdletMatch = $true
            }
        }

        $stageMatch = ($r.Stage -eq '*' -or $r.Stage -eq $Stage)
        $vmMatch = ($r.VmName -eq '*' -or [string]::IsNullOrEmpty($VmName) -or $r.VmName.ToLower() -eq $VmName.ToLower())

        if ($cmdletMatch -and $stageMatch -and $vmMatch) {
            if ($r.Count -gt 0) {
                $r.Count--
                if ($r.Count -eq 0) {
                    $script:ErrorRules.RemoveAt($i)
                }
            }
            return $r
        }
    }
    return $null
}

function Invoke-Y2Graph {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [string]$Query,

        [Parameter(Mandatory = $false, Position = 1)]
        [int]$First = 1000,

        [Parameter(Mandatory = $false, Position = 2)]
        [string]$SkipToken
    )

    $errRule = Match-FakeArmErrorRule -Cmdlet 'Invoke-Y2Graph' -Stage 'Submit'
    if ($errRule) {
        Record-FakeArmCall -Cmdlet 'Invoke-Y2Graph' -Parameters $PSBoundParameters
        $errRec = New-FakeArmErrorRecord -StatusCode $errRule.StatusCode -RetryAfter $errRule.RetryAfter -Message $errRule.Message -ErrorType $errRule.ErrorType
        throw $errRec
    }

    Record-FakeArmCall -Cmdlet 'Invoke-Y2Graph' -Parameters $PSBoundParameters

    $total = $script:Fleet.Count
    if ($total -eq 0) {
        return [PSCustomObject]@{
            data         = @()
            SkipToken    = $null
            TotalRecords = 0
            Count        = 0
        }
    }

    $startIndex = 0
    if (-not [string]::IsNullOrEmpty($SkipToken)) {
        if ($SkipToken -match '(\d+)$') {
            $startIndex = [int]$Matches[1]
        }
    }

    $pageSize = if ($First -gt 0) { $First } else { 1000 }
    if ($pageSize -gt 1000) {
        $pageSize = 1000
    }

    $pageItems = [System.Collections.Generic.List[object]]::new()
    $endIndex = [Math]::Min($startIndex + $pageSize, $total)

    for ($i = $startIndex; $i -lt $endIndex; $i++) {
        $pageItems.Add($script:Fleet[$i])
    }

    $nextToken = $null
    if ($endIndex -lt $total) {
        $nextToken = "skip_token_$endIndex"
    }

    $itemsArray = @($pageItems)
    return [PSCustomObject]@{
        data         = $itemsArray
        SkipToken    = $nextToken
        TotalRecords = $total
        Count        = $itemsArray.Count
    }
}

function Search-AzGraph {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [string]$Query,

        [Parameter(Mandatory = $false, Position = 1)]
        [int]$First = 1000,

        [Parameter(Mandatory = $false, Position = 2)]
        [string]$SkipToken
    )

    Invoke-Y2Graph @PSBoundParameters
}

function Get-Y2VmStatus {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [Alias('ResourceGroupName', 'rg')]
        [string]$ResourceGroup,

        [Parameter(Mandatory = $true, Position = 1)]
        [Alias('VMName', 'vm_name')]
        [string]$Name
    )

    $errRule = Match-FakeArmErrorRule -Cmdlet 'Get-Y2VmStatus' -VmName $Name -Stage 'Submit'
    if ($errRule) {
        Record-FakeArmCall -Cmdlet 'Get-Y2VmStatus' -Parameters $PSBoundParameters
        $errRec = New-FakeArmErrorRecord -StatusCode $errRule.StatusCode -RetryAfter $errRule.RetryAfter -Message $errRule.Message -ErrorType $errRule.ErrorType
        throw $errRec
    }

    Record-FakeArmCall -Cmdlet 'Get-Y2VmStatus' -Parameters $PSBoundParameters

    $key = $Name.ToLower()
    if (-not $script:FleetMap.ContainsKey($key)) {
        return $null
    }

    $vm = $script:FleetMap[$key]
    $pState = if ($vm -is [System.Collections.IDictionary]) { $vm['powerState'] } else { $vm.powerState }
    $os = if ($vm -is [System.Collections.IDictionary]) { $vm['osType'] } else { $vm.osType }
    $rg = if ($vm -is [System.Collections.IDictionary]) { $vm['resourceGroup'] } else { $vm.resourceGroup }
    $id = if ($vm -is [System.Collections.IDictionary]) { $vm['id'] } else { $vm.id }
    $tags = if ($vm -is [System.Collections.IDictionary]) { $vm['tags'] } else { $vm.tags }

    return [PSCustomObject]@{
        Name              = $Name
        ResourceGroupName = $rg
        PowerState        = $pState
        Statuses          = @(
            [PSCustomObject]@{
                Code          = 'ProvisioningState/succeeded'
                DisplayStatus = 'Provisioning succeeded'
            },
            [PSCustomObject]@{
                Code          = $pState
                DisplayStatus = "VM $pState"
            }
        )
        StorageProfile    = [PSCustomObject]@{
            OsDisk = [PSCustomObject]@{
                OsType = $os
            }
        }
        Tags              = $tags
        Id                = $id
    }
}

function Get-AzVM {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $false, Position = 0)]
        [string]$ResourceGroupName,

        [Parameter(Mandatory = $false, Position = 1)]
        [string]$Name,

        [Parameter(Mandatory = $false)]
        [switch]$Status
    )

    if (-not [string]::IsNullOrEmpty($Name) -and -not [string]::IsNullOrEmpty($ResourceGroupName)) {
        Get-Y2VmStatus -ResourceGroup $ResourceGroupName -Name $Name
    } elseif (-not [string]::IsNullOrEmpty($Name)) {
        Get-Y2VmStatus -ResourceGroup "" -Name $Name
    } else {
        return @($script:Fleet)
    }
}

function Start-Y2Vm {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [Alias('ResourceGroupName', 'rg')]
        [string]$ResourceGroup,

        [Parameter(Mandatory = $true, Position = 1)]
        [Alias('VMName', 'vm_name')]
        [string]$Name
    )

    $errRule = Match-FakeArmErrorRule -Cmdlet 'Start-Y2Vm' -VmName $Name -Stage 'Submit'
    if ($errRule) {
        Record-FakeArmCall -Cmdlet 'Start-Y2Vm' -Parameters $PSBoundParameters
        $errRec = New-FakeArmErrorRecord -StatusCode $errRule.StatusCode -RetryAfter $errRule.RetryAfter -Message $errRule.Message -ErrorType $errRule.ErrorType
        throw $errRec
    }

    Record-FakeArmCall -Cmdlet 'Start-Y2Vm' -Parameters $PSBoundParameters

    Set-FakeArmVmStatus -Name $Name -PowerState 'PowerState/running' | Out-Null

    return [PSCustomObject]@{
        Status    = 'InProgress'
        IsSuccess = $true
    }
}

function Start-AzVM {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [string]$ResourceGroupName,

        [Parameter(Mandatory = $true, Position = 1)]
        [string]$Name,

        [Parameter(Mandatory = $false)]
        [switch]$NoWait
    )

    Start-Y2Vm -ResourceGroup $ResourceGroupName -Name $Name
}

function Stop-Y2Vm {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [Alias('ResourceGroupName', 'rg')]
        [string]$ResourceGroup,

        [Parameter(Mandatory = $true, Position = 1)]
        [Alias('VMName', 'vm_name')]
        [string]$Name,

        [Parameter(Mandatory = $false)]
        [switch]$StayProvisioned
    )

    $errRule = Match-FakeArmErrorRule -Cmdlet 'Stop-Y2Vm' -VmName $Name -Stage 'Submit'
    if ($errRule) {
        Record-FakeArmCall -Cmdlet 'Stop-Y2Vm' -Parameters $PSBoundParameters
        $errRec = New-FakeArmErrorRecord -StatusCode $errRule.StatusCode -RetryAfter $errRule.RetryAfter -Message $errRule.Message -ErrorType $errRule.ErrorType
        throw $errRec
    }

    Record-FakeArmCall -Cmdlet 'Stop-Y2Vm' -Parameters $PSBoundParameters

    $targetPower = if ($StayProvisioned) { 'PowerState/stopped' } else { 'PowerState/deallocated' }
    Set-FakeArmVmStatus -Name $Name -PowerState $targetPower | Out-Null

    return [PSCustomObject]@{
        Status    = 'InProgress'
        IsSuccess = $true
    }
}

function Stop-AzVM {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [string]$ResourceGroupName,

        [Parameter(Mandatory = $true, Position = 1)]
        [string]$Name,

        [Parameter(Mandatory = $false)]
        [switch]$StayProvisioned,

        [Parameter(Mandatory = $false)]
        [switch]$Force,

        [Parameter(Mandatory = $false)]
        [switch]$NoWait
    )

    $stopParams = @{
        ResourceGroup = $ResourceGroupName
        Name          = $Name
    }
    if ($StayProvisioned) {
        $stopParams['StayProvisioned'] = $true
    }
    Stop-Y2Vm @stopParams
}

function Set-FakeArmJobOutput {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$VmName,

        [Parameter(Mandatory = $false)]
        [string]$StdOut = "",

        [Parameter(Mandatory = $false)]
        [string]$StdErr = ""
    )

    $key = $VmName.ToLower()
    $script:JobOutputs[$key] = @{
        Value = @(
            @{ Code = 'ComponentStatus/StdOut/succeeded'; Message = $StdOut },
            @{ Code = 'ComponentStatus/StdErr/succeeded'; Message = $StdErr }
        )
    }
}

function Start-Y2RunJob {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [Alias('ResourceGroupName', 'rg')]
        [string]$ResourceGroup,

        [Parameter(Mandatory = $true, Position = 1)]
        [Alias('Name', 'vm_name')]
        [string]$VMName,

        [Parameter(Mandatory = $true, Position = 2)]
        [string]$CommandId,

        [Parameter(Mandatory = $true, Position = 3)]
        [string]$ScriptPath
    )

    $errRule = Match-FakeArmErrorRule -Cmdlet 'Start-Y2RunJob' -VmName $VMName -Stage 'Submit'
    if ($errRule) {
        Record-FakeArmCall -Cmdlet 'Start-Y2RunJob' -Parameters $PSBoundParameters
        $errRec = New-FakeArmErrorRecord -StatusCode $errRule.StatusCode -RetryAfter $errRule.RetryAfter -Message $errRule.Message -ErrorType $errRule.ErrorType
        throw $errRec
    }

    $script:ActiveJobCount++
    if ($script:ActiveJobCount -gt $script:PeakConcurrentJobs) {
        $script:PeakConcurrentJobs = $script:ActiveJobCount
    }

    Record-FakeArmCall -Cmdlet 'Start-Y2RunJob' -Parameters $PSBoundParameters

    $recvRule = Match-FakeArmErrorRule -Cmdlet 'Receive-Y2Job' -VmName $VMName -Stage 'Receive'

    $jobOutput = $null
    $key = $VMName.ToLower()
    if ($script:JobOutputs.ContainsKey($key)) {
        $jobOutput = $script:JobOutputs[$key]
    } else {
        $jobOutput = @{
            Value = @(
                @{ Code = 'ComponentStatus/StdOut/succeeded'; Message = "[stdout]`nOperation completed successfully on $VMName`n" },
                @{ Code = 'ComponentStatus/StdErr/succeeded'; Message = "" }
            )
        }
    }

    $jobId = $script:NextJobId++
    $job = [PSCustomObject]@{
        Id             = $jobId
        Name           = "FakeRunCommandJob_$VMName"
        State          = 'Running'
        PSJobTypeName  = 'AzureLongRunningJob'
        ChildJobs      = @()
        Error          = @()
        JobStateInfo   = [PSCustomObject]@{ State = 'Running'; Reason = $null }
        VmName         = $VMName
        ResourceGroup  = $ResourceGroup
        CommandId      = $CommandId
        ScriptPath     = $ScriptPath
        Output         = $jobOutput
        SimulatedError = $recvRule
        PollCount      = 0
    }

    $script:Jobs[$jobId] = $job
    return $job
}

function Invoke-AzVMRunCommand {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$ResourceGroupName,

        [Parameter(Mandatory = $true)]
        [string]$VMName,

        [Parameter(Mandatory = $true)]
        [string]$CommandId,

        [Parameter(Mandatory = $true)]
        [string]$ScriptPath,

        [Parameter(Mandatory = $false)]
        [switch]$AsJob
    )

    Start-Y2RunJob -ResourceGroup $ResourceGroupName -VMName $VMName -CommandId $CommandId -ScriptPath $ScriptPath
}

function Get-Y2Job {
    [CmdletBinding()]
    param(
        [Parameter(Position = 0, ValueFromPipeline = $true)]
        [object]$Id,

        [Parameter()]
        [string]$Name,

        [Parameter()]
        [string]$State
    )

    process {
        Record-FakeArmCall -Cmdlet 'Get-Y2Job' -Parameters $PSBoundParameters

        $matching = [System.Collections.Generic.List[object]]::new()

        if ($null -ne $Id) {
            $parsedId = 0
            if ($Id -is [int]) { $parsedId = $Id }
            elseif ([int]::TryParse([string]$Id, [ref]$parsedId)) {}

            if ($script:Jobs.ContainsKey($parsedId)) {
                $matching.Add($script:Jobs[$parsedId])
            }
        } elseif (-not [string]::IsNullOrEmpty($Name)) {
            foreach ($j in $script:Jobs.Values) {
                if ($j.Name -eq $Name) { $matching.Add($j) }
            }
        } else {
            foreach ($j in $script:Jobs.Values) {
                $matching.Add($j)
            }
        }

        foreach ($j in $matching) {
            $j.PollCount++
            if ($j.State -eq 'Running') {
                if ($null -ne $j.SimulatedError) {
                    $j.State = 'Failed'
                    $j.JobStateInfo.State = 'Failed'
                    $errRec = New-FakeArmErrorRecord -StatusCode $j.SimulatedError.StatusCode -RetryAfter $j.SimulatedError.RetryAfter -Message $j.SimulatedError.Message -ErrorType $j.SimulatedError.ErrorType
                    $j.Error = @($errRec)
                    $j.ChildJobs = @([PSCustomObject]@{ Error = @($errRec) })
                    $j.JobStateInfo.Reason = $errRec.Exception
                } else {
                    $j.State = 'Completed'
                    $j.JobStateInfo.State = 'Completed'
                }
            }
        }

        if (-not [string]::IsNullOrEmpty($State)) {
            $filtered = @($matching | Where-Object { $_.State -eq $State })
            return $filtered
        }

        if ($null -ne $Id -and $matching.Count -eq 1) {
            return $matching[0]
        }
        return @($matching)
    }
}

function Get-Job {
    [CmdletBinding()]
    param(
        [Parameter(Position = 0, ValueFromPipeline = $true)]
        [object]$Id,

        [Parameter()]
        [string]$Name,

        [Parameter()]
        [string]$State
    )
    process {
        Get-Y2Job @PSBoundParameters
    }
}

function Receive-Y2Job {
    [CmdletBinding()]
    param(
        [Parameter(Position = 0, ValueFromPipeline = $true)]
        [object]$Job,

        [Parameter()]
        [object]$Id,

        [Parameter()]
        [string]$Name,

        [Parameter()]
        [switch]$Keep,

        [Parameter()]
        [switch]$Wait,

        [Parameter()]
        [switch]$Force
    )

    process {
        Record-FakeArmCall -Cmdlet 'Receive-Y2Job' -Parameters $PSBoundParameters

        $target = $null
        if ($null -ne $Job) {
            if ($Job.PSObject.Properties['Id']) {
                $targetId = [int]$Job.Id
                if ($script:Jobs.ContainsKey($targetId)) {
                    $target = $script:Jobs[$targetId]
                } else {
                    $target = $Job
                }
            } else {
                $target = $Job
            }
        } elseif ($null -ne $Id) {
            $parsedId = 0
            if ($Id -is [int]) { $parsedId = $Id }
            elseif ([int]::TryParse([string]$Id, [ref]$parsedId)) {}
            if ($script:Jobs.ContainsKey($parsedId)) {
                $target = $script:Jobs[$parsedId]
            }
        }

        if ($null -eq $target) {
            return $null
        }

        if ($target.State -eq 'Failed') {
            if ($target.Error -and $target.Error.Count -gt 0) {
                Write-Error -ErrorRecord $target.Error[0]
            } elseif ($target.JobStateInfo -and $target.JobStateInfo.Reason) {
                Write-Error -Exception $target.JobStateInfo.Reason -Message $target.JobStateInfo.Reason.Message
            }
            return $null
        }

        return $target.Output
    }
}

function Receive-Job {
    [CmdletBinding()]
    param(
        [Parameter(Position = 0, ValueFromPipeline = $true)]
        [object]$Job,

        [Parameter()]
        [object]$Id,

        [Parameter()]
        [string]$Name,

        [Parameter()]
        [switch]$Keep,

        [Parameter()]
        [switch]$Wait,

        [Parameter()]
        [switch]$Force
    )
    process {
        Receive-Y2Job @PSBoundParameters
    }
}

function Remove-Y2Job {
    [CmdletBinding()]
    param(
        [Parameter(Position = 0, ValueFromPipeline = $true)]
        [object]$Job,

        [Parameter()]
        [object]$Id,

        [Parameter()]
        [string]$Name,

        [Parameter()]
        [switch]$Force
    )

    process {
        Record-FakeArmCall -Cmdlet 'Remove-Y2Job' -Parameters $PSBoundParameters

        $targetId = $null
        if ($null -ne $Job -and $Job.PSObject.Properties['Id']) {
            $targetId = [int]$Job.Id
        } elseif ($null -ne $Id) {
            $parsed = 0
            if ($Id -is [int]) { $targetId = $Id }
            elseif ([int]::TryParse([string]$Id, [ref]$parsed)) { $targetId = $parsed }
        }

        if ($null -ne $targetId -and $script:Jobs.ContainsKey($targetId)) {
            $script:Jobs.Remove($targetId)
            if ($script:ActiveJobCount -gt 0) {
                $script:ActiveJobCount--
            }
        }
    }
}

function Remove-Job {
    [CmdletBinding()]
    param(
        [Parameter(Position = 0, ValueFromPipeline = $true)]
        [object]$Job,

        [Parameter()]
        [object]$Id,

        [Parameter()]
        [string]$Name,

        [Parameter()]
        [switch]$Force
    )
    process {
        Remove-Y2Job @PSBoundParameters
    }
}

function Get-FakeArmCallLog {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $false)]
        [string]$Cmdlet = ""
    )

    if ([string]::IsNullOrEmpty($Cmdlet)) {
        return @($script:CallLog)
    }
    return @($script:CallLog | Where-Object { $_.Cmdlet -eq $Cmdlet })
}

function Get-FakeArmPeakConcurrency {
    [CmdletBinding()]
    param()
    return $script:PeakConcurrentJobs
}

function Reset-FakeArmCallLog {
    [CmdletBinding()]
    param()
    $script:CallLog.Clear()
    $script:PeakConcurrentJobs = $script:ActiveJobCount
}

function Export-FakeArmCallLog {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [string]$Path
    )

    $json = if ($null -eq $script:CallLog -or $script:CallLog.Count -eq 0) {
        "[]"
    } else {
        ConvertTo-Json -InputObject @($script:CallLog) -Depth 10
    }
    $parentDir = [System.IO.Path]::GetDirectoryName($Path)
    if (-not [string]::IsNullOrEmpty($parentDir) -and -not [System.IO.Directory]::Exists($parentDir)) {
        [System.IO.Directory]::CreateDirectory($parentDir) | Out-Null
    }
    $encoding = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($Path, ($json + "`n"), $encoding)
}

function Reset-FakeArmState {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $false)]
        [string]$FleetPath = ""
    )

    $script:CallLog.Clear()
    $script:Jobs.Clear()
    $script:ActiveJobCount = 0
    $script:PeakConcurrentJobs = 0
    $script:VirtualTime = [DateTime]::UtcNow
    $script:ErrorRules.Clear()
    $script:JobOutputs.Clear()
    $script:NextJobId = 1
    $script:PagingOverride = $null

    Import-FakeArmFleet -Path $FleetPath
}

# Auto-initialize upon import
Reset-FakeArmState

Export-ModuleMember -Function @(
    'Invoke-Y2Graph',
    'Search-AzGraph',
    'Get-Y2VmStatus',
    'Get-AzVM',
    'Start-Y2Vm',
    'Start-AzVM',
    'Stop-Y2Vm',
    'Stop-AzVM',
    'Start-Y2RunJob',
    'Invoke-AzVMRunCommand',
    'Get-Y2Job',
    'Get-Job',
    'Receive-Y2Job',
    'Receive-Job',
    'Remove-Y2Job',
    'Remove-Job',
    'Start-Y2Sleep',
    'Start-Sleep',
    'Set-FakeArmFleet',
    'Import-FakeArmFleet',
    'Get-FakeArmFleet',
    'Set-FakeArmVmStatus',
    'Set-FakeArmPagingTest',
    'New-FakeArmErrorRecord',
    'Set-FakeArmErrorRule',
    'Clear-FakeArmErrorRules',
    'Match-FakeArmErrorRule',
    'Set-FakeArmJobOutput',
    'Get-FakeArmCallLog',
    'Get-FakeArmPeakConcurrency',
    'Export-FakeArmCallLog',
    'Reset-FakeArmCallLog',
    'Get-FakeArmVirtualTime',
    'Set-FakeArmVirtualTime',
    'Advance-FakeArmVirtualTime',
    'Reset-FakeArmState'
)
