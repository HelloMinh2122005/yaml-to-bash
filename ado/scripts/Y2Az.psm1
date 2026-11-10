# Y2Az.psm1 - Core Azure PowerShell cmdlet wrappers, error classification, and resilient job handling
# Compatible with Windows PowerShell 5.1 (Desktop) and PowerShell 7+ (Core).
# Pure ASCII, LF line endings, BOM-less UTF-8.

# Set TLS 1.2 on module import (INV-8, AC-10)
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# ---------------------------------------------------------------------------
# Section 1: Thin Cmdlet Wrappers (INV-6, INV-10)
# All Azure RM / Resource Graph / Job cmdlets must be invoked solely through
# these wrapper functions to provide a testable seam for FakeArm mocking.
# ---------------------------------------------------------------------------

function Invoke-Y2WrapperSeam {
    <#
    .SYNOPSIS
        Internal dispatcher checking for FakeArm mock module or global test handlers.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$CommandName,

        [Parameter(Mandatory = $true)]
        [hashtable]$BoundParams
    )

    $fake = Get-Command -Name "FakeArm\$CommandName" -ErrorAction SilentlyContinue
    if ($fake) {
        return @{ Dispatched = $true; Result = (& $fake @BoundParams) }
    }

    $cleanName = $CommandName -replace '-', ''
    $shortName = $CommandName -replace 'Y2', '' -replace '-', ''
    $candidateNames = @(
        "${cleanName}Handler",
        "Y2${cleanName}Handler",
        "Y2${shortName}Handler",
        "${shortName}Handler"
    )

    foreach ($varName in $candidateNames) {
        $handler = Get-Variable -Name $varName -Scope Global -ValueOnly -ErrorAction SilentlyContinue
        if ($handler) {
            return @{ Dispatched = $true; Result = (& $handler @BoundParams) }
        }
    }

    return @{ Dispatched = $false; Result = $null }
}

function Invoke-Y2Graph {
    <#
    .SYNOPSIS
        Thin wrapper for Search-AzGraph queries with pagination support.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [string]$Query,

        [Parameter(Mandatory = $false, Position = 1)]
        [int]$First = 1000,

        [Parameter(Mandatory = $false, Position = 2)]
        [string]$SkipToken
    )

    $seam = Invoke-Y2WrapperSeam -CommandName 'Invoke-Y2Graph' -BoundParams $PSBoundParameters
    if ($seam.Dispatched) {
        return $seam.Result
    }

    $params = @{
        Query = $Query
        First = $First
    }
    if (-not [string]::IsNullOrEmpty($SkipToken)) {
        $params['SkipToken'] = $SkipToken
    }

    Search-AzGraph @params
}

function Get-Y2VmStatus {
    <#
    .SYNOPSIS
        Thin wrapper for Get-AzVM -Status (instance view).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [Alias('ResourceGroupName', 'rg')]
        [string]$ResourceGroup,

        [Parameter(Mandatory = $true, Position = 1)]
        [Alias('VMName', 'vm_name')]
        [string]$Name
    )

    $seam = Invoke-Y2WrapperSeam -CommandName 'Get-Y2VmStatus' -BoundParams $PSBoundParameters
    if ($seam.Dispatched) {
        return $seam.Result
    }

    Get-AzVM -ResourceGroupName $ResourceGroup -Name $Name -Status
}

function Start-Y2Vm {
    <#
    .SYNOPSIS
        Thin wrapper for Start-AzVM -NoWait.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [Alias('ResourceGroupName', 'rg')]
        [string]$ResourceGroup,

        [Parameter(Mandatory = $true, Position = 1)]
        [Alias('VMName', 'vm_name')]
        [string]$Name
    )

    $seam = Invoke-Y2WrapperSeam -CommandName 'Start-Y2Vm' -BoundParams $PSBoundParameters
    if ($seam.Dispatched) {
        return $seam.Result
    }

    Start-AzVM -ResourceGroupName $ResourceGroup -Name $Name -NoWait
}

function Stop-Y2Vm {
    <#
    .SYNOPSIS
        Thin wrapper for Stop-AzVM with -Force -NoWait, preserving allocation if requested.
    #>
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

    $seam = Invoke-Y2WrapperSeam -CommandName 'Stop-Y2Vm' -BoundParams $PSBoundParameters
    if ($seam.Dispatched) {
        return $seam.Result
    }

    if ($StayProvisioned) {
        Stop-AzVM -ResourceGroupName $ResourceGroup -Name $Name -StayProvisioned -Force -NoWait
    } else {
        Stop-AzVM -ResourceGroupName $ResourceGroup -Name $Name -Force -NoWait
    }
}

function Start-Y2RunJob {
    <#
    .SYNOPSIS
        Thin wrapper for Invoke-AzVMRunCommand executed as a PowerShell background job (-AsJob).
    #>
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

    $seam = Invoke-Y2WrapperSeam -CommandName 'Start-Y2RunJob' -BoundParams $PSBoundParameters
    if ($seam.Dispatched) {
        return $seam.Result
    }

    Invoke-AzVMRunCommand -ResourceGroupName $ResourceGroup -VMName $VMName -CommandId $CommandId -ScriptPath $ScriptPath -AsJob
}

function Get-Y2Job {
    <#
    .SYNOPSIS
        Thin wrapper for Get-Job passing through bound parameters.
    #>
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
        $seam = Invoke-Y2WrapperSeam -CommandName 'Get-Y2Job' -BoundParams $PSBoundParameters
        if ($seam.Dispatched) {
            return $seam.Result
        }

        if ($PSBoundParameters.Count -gt 0) {
            Get-Job @PSBoundParameters
        } else {
            Get-Job
        }
    }
}

function Receive-Y2Job {
    <#
    .SYNOPSIS
        Thin wrapper for Receive-Job passing through bound parameters.
    #>
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
        $seam = Invoke-Y2WrapperSeam -CommandName 'Receive-Y2Job' -BoundParams $PSBoundParameters
        if ($seam.Dispatched) {
            return $seam.Result
        }

        if ($PSBoundParameters.Count -gt 0) {
            Receive-Job @PSBoundParameters
        } else {
            Receive-Job
        }
    }
}

function Remove-Y2Job {
    <#
    .SYNOPSIS
        Thin wrapper for Remove-Job passing through bound parameters.
    #>
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
        $seam = Invoke-Y2WrapperSeam -CommandName 'Remove-Y2Job' -BoundParams $PSBoundParameters
        if ($seam.Dispatched) {
            return $seam.Result
        }

        if ($PSBoundParameters.Count -gt 0) {
            Remove-Job @PSBoundParameters
        } else {
            Remove-Job
        }
    }
}

$script:Y2SleepHandler = $null
$script:Y2VirtualTime = $null

function Get-Y2Date {
    <#
    .SYNOPSIS
        Returns the current UTC timestamp, or virtual time if advancing in test mode.
    #>
    [CmdletBinding()]
    param()

    if ($null -ne $script:Y2VirtualTime) {
        return $script:Y2VirtualTime
    }
    return [DateTime]::UtcNow
}

function Set-Y2VirtualTime {
    <#
    .SYNOPSIS
        Enables or resets virtual time for testing asynchronous delays without wall-clock sleep.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $false, Position = 0)]
        [object]$Time = $null
    )

    if ($null -eq $Time) {
        $script:Y2VirtualTime = [DateTime]::UtcNow
    } else {
        $script:Y2VirtualTime = [DateTime]$Time
    }
}

function Reset-Y2VirtualTime {
    <#
    .SYNOPSIS
        Disables virtual time and returns to real UtcNow.
    #>
    [CmdletBinding()]
    param()

    $script:Y2VirtualTime = $null
}

function Set-Y2SleepHandler {
    <#
    .SYNOPSIS
        Sets a custom sleep handler callback for tests to intercept Start-Y2Sleep.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $false, Position = 0)]
        [scriptblock]$Handler
    )

    $script:Y2SleepHandler = $Handler
    if ($null -eq $script:Y2VirtualTime) {
        $script:Y2VirtualTime = [DateTime]::UtcNow
    }
}

function Start-Y2Sleep {
    <#
    .SYNOPSIS
        Thin wrapper for Start-Sleep serving as an overridable seam for tests.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [double]$Seconds
    )

    if ($null -ne $script:Y2VirtualTime) {
        $script:Y2VirtualTime = $script:Y2VirtualTime.AddSeconds($Seconds)
    }

    if ($null -ne $script:Y2SleepHandler) {
        & $script:Y2SleepHandler $Seconds
        return
    }

    if ($null -ne $global:Y2SleepHandler) {
        & $global:Y2SleepHandler $Seconds
        return
    }

    $seam = Invoke-Y2WrapperSeam -CommandName 'Start-Y2Sleep' -BoundParams $PSBoundParameters
    if ($seam.Dispatched) {
        return $seam.Result
    }

    Start-Sleep -Seconds $Seconds
}

# ---------------------------------------------------------------------------
# Section 2: Error Classification and Throttling / Conflict Handling
# ---------------------------------------------------------------------------

function Classify-Y2Error {
    <#
    .SYNOPSIS
        Inspects an ErrorRecord, Exception, or response object to classify error types.
        Returns hashtable:
          Type       = 'Throttled' | 'ConflictBusy' | 'AuthFailure' | 'GeneralFailure'
          StatusCode = int (e.g. 429, 409, 401, 403)
          RetryAfter = int or $null
          Message    = string
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0, ValueFromPipeline = $true)]
        [object]$ErrorObject
    )

    $ex = $null
    $errorId = ""
    $statusCode = 0
    $retryAfter = $null
    $msg = ""

    # Unpack ErrorRecord, Exception, hashtable, or PSCustomObject
    if ($ErrorObject -is [System.Collections.IDictionary]) {
        if ($ErrorObject.ContainsKey('StatusCode') -and $null -ne $ErrorObject['StatusCode']) {
            $statusCode = [int]$ErrorObject['StatusCode']
        }
        if ($ErrorObject.ContainsKey('RetryAfter') -and $null -ne $ErrorObject['RetryAfter']) {
            $retryAfter = [int]$ErrorObject['RetryAfter']
        }
        if ($ErrorObject.ContainsKey('Message') -and $null -ne $ErrorObject['Message']) {
            $msg = [string]$ErrorObject['Message']
        }
        if ($ErrorObject.ContainsKey('FullyQualifiedErrorId') -and $null -ne $ErrorObject['FullyQualifiedErrorId']) {
            $errorId = [string]$ErrorObject['FullyQualifiedErrorId']
        }
        if ($ErrorObject.ContainsKey('Exception') -and $null -ne $ErrorObject['Exception']) {
            $ex = $ErrorObject['Exception']
        }
    } elseif ($ErrorObject -is [System.Management.Automation.ErrorRecord]) {
        $ex = $ErrorObject.Exception
        $errorId = [string]$ErrorObject.FullyQualifiedErrorId
        if ($null -ne $ErrorObject.TargetObject -and $ErrorObject.TargetObject.PSObject.Properties['StatusCode']) {
            $statusCode = [int]$ErrorObject.TargetObject.StatusCode
        }
    } elseif ($ErrorObject -is [System.Exception]) {
        $ex = $ErrorObject
    } else {
        if ($ErrorObject.PSObject.Properties['Exception'] -and $ErrorObject.Exception) {
            $ex = $ErrorObject.Exception
        }
        if ($ErrorObject.PSObject.Properties['FullyQualifiedErrorId'] -and $ErrorObject.FullyQualifiedErrorId) {
            $errorId = [string]$ErrorObject.FullyQualifiedErrorId
        }
        if ($ErrorObject.PSObject.Properties['StatusCode'] -and $ErrorObject.StatusCode) {
            $statusCode = [int]$ErrorObject.StatusCode
        }
        if ($ErrorObject.PSObject.Properties['Status'] -and $ErrorObject.Status) {
            $statusCode = [int]$ErrorObject.Status
        }
        if ($ErrorObject.PSObject.Properties['RetryAfter'] -and $null -ne $ErrorObject.RetryAfter) {
            $retryAfter = [int]$ErrorObject.RetryAfter
        }
        if ($ErrorObject.PSObject.Properties['Message'] -and $ErrorObject.Message) {
            $msg = [string]$ErrorObject.Message
        }
    }

    # Unwrap inner exception if needed
    if ($ex -and $ex.InnerException -and -not ($ex.PSObject.Properties['Response'] -and $ex.Response) -and -not ($ex.PSObject.Properties['Status'] -and $ex.Status)) {
        $ex = $ex.InnerException
    }

    # Extract status code from Exception if not yet obtained
    if ($statusCode -eq 0 -and $ex) {
        if ($ex.PSObject.Properties['Response'] -and $ex.Response) {
            $resp = $ex.Response
            if ($resp.PSObject.Properties['StatusCode'] -and $resp.StatusCode) {
                $statusCode = [int]$resp.StatusCode
            }
        } elseif ($ex.PSObject.Properties['Status'] -and $ex.Status) {
            $statusCode = [int]$ex.Status
        } elseif ($ex.PSObject.Properties['StatusCode'] -and $ex.StatusCode) {
            $statusCode = [int]$ex.StatusCode
        }
    }

    # Extract Retry-After header from Response if not yet obtained
    if ($null -eq $retryAfter -and $ex -and $ex.PSObject.Properties['Response'] -and $ex.Response) {
        $resp = $ex.Response
        if ($resp.PSObject.Properties['Headers'] -and $resp.Headers) {
            $headers = $resp.Headers
            $rawHeader = $null
            if ($headers -is [System.Collections.IDictionary]) {
                if ($headers.ContainsKey('Retry-After')) {
                    $rawHeader = $headers['Retry-After']
                } elseif ($headers.ContainsKey('retry-after')) {
                    $rawHeader = $headers['retry-after']
                }
            } elseif ($headers.PSObject.Properties['Retry-After']) {
                $rawHeader = $headers.'Retry-After'
            } elseif ($headers.PSObject.Properties['retry-after']) {
                $rawHeader = $headers.'retry-after'
            }

            if ($null -ne $rawHeader) {
                $firstHeader = $rawHeader | Select-Object -First 1
                $parsed = 0
                if ([int]::TryParse([string]$firstHeader, [ref]$parsed)) {
                    $retryAfter = $parsed
                }
            }
        }
    }

    # Extract message if not yet set
    if ([string]::IsNullOrEmpty($msg)) {
        if ($ex -and $ex.Message) {
            $msg = [string]$ex.Message
        } elseif ($null -ne $ErrorObject) {
            $msg = [string]$ErrorObject.ToString()
        }
    }

    # Check for HTTP 429 / Throttled
    if ($statusCode -eq 429 -or $errorId -match '429|TooManyRequests' -or $msg -match '429|TooManyRequests|throttl') {
        return @{
            Type       = 'Throttled'
            StatusCode = 429
            RetryAfter = $retryAfter
            Message    = $msg
        }
    }

    # Check for HTTP 409 / Conflict (VM Busy)
    if ($statusCode -eq 409 -or $errorId -match 'Conflict|409' -or $msg -match 'Conflict|in progress|ConflictOperationInProgress') {
        return @{
            Type       = 'ConflictBusy'
            StatusCode = 409
            RetryAfter = $null
            Message    = $msg
        }
    }

    # Check for HTTP 401 / 403 / Auth / Token Failure
    if ($statusCode -in 401, 403 -or $errorId -match 'Authentication|Authorization|ExpiredToken|InvalidAuthenticationToken' -or $msg -match 'token|unauthorized|forbidden|AuthenticationFailed|AuthorizationFailed') {
        $authStatus = if ($statusCode -gt 0) { $statusCode } else { 401 }
        return @{
            Type       = 'AuthFailure'
            StatusCode = $authStatus
            RetryAfter = $null
            Message    = $msg
        }
    }

    return @{
        Type       = 'GeneralFailure'
        StatusCode = $statusCode
        RetryAfter = $null
        Message    = $msg
    }
}

# Alias for backwards compatibility with research notes
Set-Alias -Name Get-Y2ErrorClassification -Value Classify-Y2Error

function Get-Y2BackoffSeconds {
    <#
    .SYNOPSIS
        Calculates retry backoff delay for HTTP 429 throttling.
        If RetryAfter is specified, returns it directly.
        Otherwise selects from (15, 30, 60, 120, 240)s with +-20% jitter.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $false, Position = 0)]
        [object]$RetryAfter = $null,

        [Parameter(Mandatory = $false, Position = 1)]
        [int]$Attempt = 1
    )

    if ($null -ne $RetryAfter) {
        $parsed = 0
        if ([int]::TryParse([string]$RetryAfter, [ref]$parsed) -and $parsed -gt 0) {
            return [double]$parsed
        }
    }

    $schedule = @(15, 30, 60, 120, 240)
    $index = $Attempt - 1
    if ($index -lt 0) {
        $index = 0
    }
    if ($index -ge $schedule.Length) {
        $index = $schedule.Length - 1
    }

    $base = $schedule[$index]
    $min = $base * 0.8
    $max = $base * 1.2
    $jittered = Get-Random -Minimum $min -Maximum $max
    return [Math]::Round([double]$jittered, 1)
}

function Wait-Y2Backoff {
    <#
    .SYNOPSIS
        Executes backoff sleep for HTTP 429 throttling via Start-Y2Sleep.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $false, Position = 0)]
        [object]$ErrorOrClassification = $null,

        [Parameter(Mandatory = $false)]
        [object]$RetryAfter = $null,

        [Parameter(Mandatory = $false)]
        [int]$Attempt = 1
    )

    $effectiveRetryAfter = $RetryAfter

    if ($null -eq $effectiveRetryAfter -and $null -ne $ErrorOrClassification) {
        if ($ErrorOrClassification -is [System.Collections.IDictionary]) {
            if ($ErrorOrClassification.ContainsKey('RetryAfter')) {
                $effectiveRetryAfter = $ErrorOrClassification['RetryAfter']
            }
        } elseif ($ErrorOrClassification.PSObject.Properties['RetryAfter']) {
            $effectiveRetryAfter = $ErrorOrClassification.RetryAfter
        } else {
            $classification = Classify-Y2Error -ErrorObject $ErrorOrClassification
            $effectiveRetryAfter = $classification['RetryAfter']
        }
    }

    $delay = Get-Y2BackoffSeconds -RetryAfter $effectiveRetryAfter -Attempt $Attempt
    Start-Y2Sleep -Seconds $delay
    return $delay
}

Set-Alias -Name Invoke-Y2Backoff -Value Wait-Y2Backoff

function Get-Y2BusyNextAttempt {
    <#
    .SYNOPSIS
        Calculates next attempt time for a busy VM (INV-11).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $false)]
        [int]$DelaySeconds = 60
    )

    return (Get-Y2Date).AddSeconds($DelaySeconds)
}

function Set-Y2VmBusy {
    <#
    .SYNOPSIS
        Updates VM tracking hashtable for HTTP 409 conflict. Does not sleep inline (INV-11).
        Re-queues VM with next_attempt_at = now + 60s, up to 3 retries, then marks run_command_busy.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [hashtable]$VmTracker,

        [Parameter(Mandatory = $false)]
        [int]$DelaySeconds = 60,

        [Parameter(Mandatory = $false)]
        [int]$MaxRetries = 3
    )

    $retries = 0
    if ($VmTracker.ContainsKey('Retries')) {
        $retries = [int]$VmTracker['Retries']
    } elseif ($VmTracker.ContainsKey('retry_count')) {
        $retries = [int]$VmTracker['retry_count']
    }
    $retries++

    $nextAttempt = Get-Y2BusyNextAttempt -DelaySeconds $DelaySeconds

    $VmTracker['Retries'] = $retries
    $VmTracker['retry_count'] = $retries
    $VmTracker['NextAttemptAt'] = $nextAttempt
    $VmTracker['next_attempt_at'] = $nextAttempt

    if ($retries -gt $MaxRetries) {
        $VmTracker['Status'] = 'run_command_busy'
        $VmTracker['status'] = 'run_command_busy'
    } else {
        $VmTracker['Status'] = 'busy'
        $VmTracker['status'] = 'busy'
    }

    return $VmTracker
}

function Test-Y2VmReady {
    <#
    .SYNOPSIS
        Checks if a busy VM is ready for its next attempt.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [hashtable]$VmTracker
    )

    $nextAttempt = $null
    if ($VmTracker.ContainsKey('NextAttemptAt')) {
        $nextAttempt = $VmTracker['NextAttemptAt']
    } elseif ($VmTracker.ContainsKey('next_attempt_at')) {
        $nextAttempt = $VmTracker['next_attempt_at']
    }

    if ($null -eq $nextAttempt) {
        return $true
    }

    return ((Get-Y2Date) -ge $nextAttempt)
}

# ---------------------------------------------------------------------------
# Section 3: Authentication Breaker (AC-5, TP-05)
# ---------------------------------------------------------------------------

$script:Y2AuthFailureCount = 0
$script:Y2AuthBreakerThreshold = 5

function Reset-Y2AuthCounter {
    <#
    .SYNOPSIS
        Resets the consecutive authentication failure counter.
    #>
    [CmdletBinding()]
    param()

    $script:Y2AuthFailureCount = 0
}

function Record-Y2AuthSuccess {
    <#
    .SYNOPSIS
        Records a successful operation, resetting the auth failure counter.
    #>
    [CmdletBinding()]
    param()

    $script:Y2AuthFailureCount = 0
}

function Record-Y2AuthFailure {
    <#
    .SYNOPSIS
        Records an authentication failure (401/403/token error).
        Trips breaker after 5 consecutive failures.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $false, Position = 0)]
        [object]$ErrorObject = $null
    )

    $shouldCount = $true
    if ($null -ne $ErrorObject) {
        $classification = Classify-Y2Error -ErrorObject $ErrorObject
        if ($classification['Type'] -ne 'AuthFailure') {
            $shouldCount = $false
        }
    }

    if ($shouldCount) {
        $script:Y2AuthFailureCount++
    }

    return ($script:Y2AuthFailureCount -ge $script:Y2AuthBreakerThreshold)
}

function Test-Y2AuthTripped {
    <#
    .SYNOPSIS
        Checks whether the consecutive auth failure breaker is currently tripped.
    #>
    [CmdletBinding()]
    param()

    return ($script:Y2AuthFailureCount -ge $script:Y2AuthBreakerThreshold)
}

function Get-Y2AuthBreaker {
    <#
    .SYNOPSIS
        Returns current auth breaker diagnostic state.
    #>
    [CmdletBinding()]
    param()

    return @{
        FailureCount = $script:Y2AuthFailureCount
        Threshold    = $script:Y2AuthBreakerThreshold
        IsTripped    = ($script:Y2AuthFailureCount -ge $script:Y2AuthBreakerThreshold)
    }
}

function Set-Y2AuthBreakerThreshold {
    <#
    .SYNOPSIS
        Sets the threshold for the auth breaker.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [int]$Threshold
    )

    $script:Y2AuthBreakerThreshold = $Threshold
}

# ---------------------------------------------------------------------------
# Section 4: Utility Functions (BOM-less UTF-8, JSON) (AC-10, INV-8)
# ---------------------------------------------------------------------------

function Write-Y2File {
    <#
    .SYNOPSIS
        Writes content to file using BOM-less UTF-8 encoding.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [string]$Path,

        [Parameter(Mandatory = $false, Position = 1)]
        [AllowEmptyString()]
        [AllowNull()]
        [string]$Content = ""
    )

    $resolvedPath = if ($ExecutionContext -and $ExecutionContext.SessionState) {
        $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
    } else {
        [System.IO.Path]::GetFullPath($Path)
    }

    $parentDir = [System.IO.Path]::GetDirectoryName($resolvedPath)
    if (-not [string]::IsNullOrEmpty($parentDir) -and -not [System.IO.Directory]::Exists($parentDir)) {
        [System.IO.Directory]::CreateDirectory($parentDir) | Out-Null
    }

    if ($null -eq $Content) {
        $Content = ""
    }

    $encoding = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($resolvedPath, $Content, $encoding)
}

function Write-Y2Json {
    <#
    .SYNOPSIS
        Converts object to JSON using ConvertTo-Json -Depth 10 and writes via Write-Y2File.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [string]$Path,

        [Parameter(Mandatory = $true, Position = 1, ValueFromPipeline = $true)]
        [AllowNull()]
        [object]$InputObject,

        [Parameter(Mandatory = $false)]
        [int]$Depth = 10
    )

    $json = $InputObject | ConvertTo-Json -Depth $Depth
    Write-Y2File -Path $Path -Content $json
}

# ---------------------------------------------------------------------------
# Section 5: State and Target Helpers (INV-9, AC-2, AC-9)
# ---------------------------------------------------------------------------

function Read-Y2State {
    <#
    .SYNOPSIS
        Reads state.csv from the specified path. Returns null if file does not exist,
        or an array of PSCustomObject rows.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [string]$Path
    )

    if (-not (Test-Path $Path)) {
        return $null
    }

    $raw = Import-Csv -Path $Path
    if ($null -eq $raw) {
        return @()
    }
    return @($raw)
}

function Write-Y2State {
    <#
    .SYNOPSIS
        Writes state.csv with columns:
        vm_name,verified_power,started_by_run,start,run,restore,output_bytes,possibly_truncated
        Uses RFC 4180 CSV quoting, BOM-less UTF-8, and LF line endings.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [string]$Path,

        [Parameter(Mandatory = $false, Position = 1)]
        [object[]]$Rows = @()
    )

    $columns = @(
        'vm_name',
        'verified_power',
        'started_by_run',
        'start',
        'run',
        'restore',
        'output_bytes',
        'possibly_truncated'
    )

    if ($null -eq $Rows -or $Rows.Count -eq 0) {
        $header = $columns -join ','
        Write-Y2File -Path $Path -Content ($header + "`n")
        return
    }

    $normalizedRows = @()
    foreach ($r in $Rows) {
        $rowObj = [ordered]@{}
        foreach ($col in $columns) {
            $val = ""
            if ($r -is [System.Collections.IDictionary]) {
                if ($r.ContainsKey($col) -and $null -ne $r[$col]) {
                    $val = $r[$col]
                }
            } elseif ($r.PSObject.Properties[$col] -and $null -ne $r.$col) {
                $val = $r.$col
            }
            if ($val -is [bool]) {
                $val = if ($val) { "true" } else { "false" }
            }
            $rowObj[$col] = $val
        }
        $normalizedRows += [PSCustomObject]$rowObj
    }

    $csvLines = ($normalizedRows | ConvertTo-Csv -NoTypeInformation) -join "`n"
    Write-Y2File -Path $Path -Content ($csvLines + "`n")
}

function Read-Y2Targets {
    <#
    .SYNOPSIS
        Reads targets.csv from the specified path. Returns null if file does not exist,
        or an array of PSCustomObject rows.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [string]$Path
    )

    if (-not (Test-Path $Path)) {
        return $null
    }

    $raw = Import-Csv -Path $Path
    if ($null -eq $raw) {
        return @()
    }
    return @($raw)
}

function Write-Y2Targets {
    <#
    .SYNOPSIS
        Writes targets.csv with columns:
        vm_name,resource_group,subscription,os_type,initial_power,tags
        Properly escapes CSV special characters and embedded JSON tags.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [string]$Path,

        [Parameter(Mandatory = $false, Position = 1)]
        [object[]]$Rows = @()
    )

    $columns = @(
        'vm_name',
        'resource_group',
        'subscription',
        'os_type',
        'initial_power',
        'tags'
    )

    if ($null -eq $Rows -or $Rows.Count -eq 0) {
        $header = $columns -join ','
        Write-Y2File -Path $Path -Content ($header + "`n")
        return
    }

    $normalizedRows = @()
    foreach ($r in $Rows) {
        $rowObj = [ordered]@{}
        foreach ($col in $columns) {
            $val = ""
            if ($r -is [System.Collections.IDictionary]) {
                if ($r.ContainsKey($col) -and $null -ne $r[$col]) {
                    $val = $r[$col]
                }
            } elseif ($r.PSObject.Properties[$col] -and $null -ne $r.$col) {
                $val = $r.$col
            }
            $rowObj[$col] = $val
        }
        $normalizedRows += [PSCustomObject]$rowObj
    }

    $csvLines = ($normalizedRows | ConvertTo-Csv -NoTypeInformation) -join "`n"
    Write-Y2File -Path $Path -Content ($csvLines + "`n")
}

function Add-Y2Result {
    <#
    .SYNOPSIS
        Updates VM state record and flushes to state.csv after transition (INV-9).
        Also supports marking all uncompleted VMs as auth_aborted when breaker trips.
    #>
    [CmdletBinding(DefaultParameterSetName = 'UpdateVm')]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [object]$StateRecords,

        [Parameter(ParameterSetName = 'UpdateVm', Mandatory = $false, Position = 1)]
        [string]$VmName,

        [Parameter(ParameterSetName = 'UpdateVm', Mandatory = $false, Position = 2)]
        [string]$RunStatus = 'ok',

        [Parameter(ParameterSetName = 'UpdateVm', Mandatory = $false)]
        [int]$OutputBytes = 0,

        [Parameter(ParameterSetName = 'UpdateVm', Mandatory = $false)]
        [bool]$PossiblyTruncated = $false,

        [Parameter(ParameterSetName = 'AuthAbort', Mandatory = $true)]
        [switch]$MarkRemainingAuthAborted,

        [Parameter(Mandatory = $false)]
        [string]$StateCsvPath
    )

    $records = if ($StateRecords -is [System.Collections.IEnumerable] -and -not ($StateRecords -is [string])) {
        @($StateRecords)
    } else {
        @($StateRecords)
    }

    if ($MarkRemainingAuthAborted) {
        $terminalStatuses = @('ok', 'os_mismatch', 'start_failed', 'run_command_busy', 'auth_aborted', 'failed', 'skipped')
        foreach ($r in $records) {
            $currentRun = if ($r -is [System.Collections.IDictionary]) {
                $r['run']
            } elseif ($r.PSObject.Properties['run']) {
                $r.run
            } else {
                ""
            }

            if ([string]::IsNullOrEmpty($currentRun) -or $currentRun -notin $terminalStatuses) {
                if ($r -is [System.Collections.IDictionary]) {
                    $r['run'] = 'auth_aborted'
                } else {
                    if ($r.PSObject.Properties['run']) {
                        $r.run = 'auth_aborted'
                    } else {
                        $r | Add-Member -NotePropertyName 'run' -NotePropertyValue 'auth_aborted' -Force
                    }
                }
            }
        }
    } elseif (-not [string]::IsNullOrEmpty($VmName)) {
        foreach ($r in $records) {
            $name = if ($r -is [System.Collections.IDictionary]) {
                $r['vm_name']
            } elseif ($r.PSObject.Properties['vm_name']) {
                $r.vm_name
            } else {
                ""
            }

            if ($name -eq $VmName) {
                $truncStr = if ($PossiblyTruncated) { "true" } else { "false" }
                if ($r -is [System.Collections.IDictionary]) {
                    $r['run'] = $RunStatus
                    $r['output_bytes'] = $OutputBytes
                    $r['possibly_truncated'] = $truncStr
                } else {
                    if ($r.PSObject.Properties['run']) { $r.run = $RunStatus } else { $r | Add-Member -NotePropertyName 'run' -NotePropertyValue $RunStatus -Force }
                    if ($r.PSObject.Properties['output_bytes']) { $r.output_bytes = $OutputBytes } else { $r | Add-Member -NotePropertyName 'output_bytes' -NotePropertyValue $OutputBytes -Force }
                    if ($r.PSObject.Properties['possibly_truncated']) { $r.possibly_truncated = $truncStr } else { $r | Add-Member -NotePropertyName 'possibly_truncated' -NotePropertyValue $truncStr -Force }
                }
                break
            }
        }
    }

    if (-not [string]::IsNullOrEmpty($StateCsvPath)) {
        Write-Y2State -Path $StateCsvPath -Rows $records
    }

    return $records
}

function Get-Y2RunCommandOutput {
    <#
    .SYNOPSIS
        Extracts stdout and stderr streams from an Invoke-AzVMRunCommand response.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [object]$JobOutput
    )

    $stdout = ""
    $stderr = ""

    $entries = $null
    if ($JobOutput -is [System.Collections.IDictionary] -and $JobOutput.ContainsKey('Value')) {
        $entries = $JobOutput['Value']
    } elseif ($JobOutput.PSObject.Properties['Value']) {
        $entries = $JobOutput.Value
    } elseif ($JobOutput -is [System.Collections.IEnumerable] -and -not ($JobOutput -is [string])) {
        $entries = $JobOutput
    }

    if ($entries) {
        foreach ($entry in $entries) {
            $code = ""
            $msg = ""
            if ($entry -is [System.Collections.IDictionary]) {
                if ($entry.ContainsKey('code')) { $code = [string]$entry['code'] }
                elseif ($entry.ContainsKey('Code')) { $code = [string]$entry['Code'] }
                if ($entry.ContainsKey('message')) { $msg = [string]$entry['message'] }
                elseif ($entry.ContainsKey('Message')) { $msg = [string]$entry['Message'] }
            } else {
                if ($entry.PSObject.Properties['Code']) { $code = [string]$entry.Code }
                elseif ($entry.PSObject.Properties['code']) { $code = [string]$entry.code }
                if ($entry.PSObject.Properties['Message']) { $msg = [string]$entry.Message }
                elseif ($entry.PSObject.Properties['message']) { $msg = [string]$entry.message }
            }

            if ($code -match 'StdOut') {
                $stdout = $msg
            } elseif ($code -match 'StdErr') {
                $stderr = $msg
            }
        }
    }

    if ([string]::IsNullOrEmpty($stdout) -and [string]::IsNullOrEmpty($stderr)) {
        $rawText = if ($JobOutput -is [string]) {
            $JobOutput
        } elseif ($entries -and $entries.Count -gt 0 -and $entries[0].Message) {
            [string]$entries[0].Message
        } elseif ($entries -and $entries.Count -gt 0 -and $entries[0].message) {
            [string]$entries[0].message
        } else {
            [string]$JobOutput
        }

        if ($rawText -match '\[stdout\]') {
            $parts = $rawText -split '\[stdout\]', 2
            $afterStdout = $parts[1]
            if ($afterStdout -match '\[stderr\]') {
                $subParts = $afterStdout -split '\[stderr\]', 2
                $stdout = $subParts[0].TrimStart("`r`n").TrimEnd("`r`n")
                $stderr = $subParts[1].TrimStart("`r`n").TrimEnd("`r`n")
            } else {
                $stdout = $afterStdout.TrimStart("`r`n").TrimEnd("`r`n")
            }
        } else {
            $stdout = $rawText
        }
    }

    return @{
        StdOut = $stdout
        StdErr = $stderr
    }
}

# ---------------------------------------------------------------------------
# Section 6: Job Pool Implementation (AC-4, TP-04)
# ---------------------------------------------------------------------------

function Invoke-Y2JobPool {
    <#
    .SYNOPSIS
        Executes Run Command jobs concurrently across a pool of VMs with throttling (429),
        conflict re-queuing (409), and authentication circuit breaker protection (AC-4, AC-5).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0, ValueFromPipeline = $true)]
        [AllowEmptyCollection()]
        [Alias('Targets', 'Rows')]
        [object[]]$VMs,

        [Parameter(Mandatory = $true, Position = 1)]
        [string]$CommandId,

        [Parameter(Mandatory = $true, Position = 2)]
        [string]$ScriptPath,

        [Parameter(Mandatory = $false)]
        [int]$MaxJobs = 20,

        [Parameter(Mandatory = $false)]
        [int]$PollSeconds = 15,

        [Parameter(Mandatory = $false)]
        [string]$StateCsvPath,

        [Parameter(Mandatory = $false)]
        [string]$ReportDir,

        [Parameter(Mandatory = $false)]
        [object[]]$StateRecords = @()
    )

    if ($null -eq $VMs -or $VMs.Count -eq 0) {
        if (-not [string]::IsNullOrEmpty($StateCsvPath)) {
            Write-Y2State -Path $StateCsvPath -Rows @()
        }
        return @()
    }

    if (-not [string]::IsNullOrEmpty($ReportDir) -and -not (Test-Path $ReportDir)) {
        $fullReportDir = if ($ExecutionContext -and $ExecutionContext.SessionState) {
            $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ReportDir)
        } else {
            [System.IO.Path]::GetFullPath($ReportDir)
        }
        if (-not [System.IO.Directory]::Exists($fullReportDir)) {
            [System.IO.Directory]::CreateDirectory($fullReportDir) | Out-Null
        }
    }

    # Initialize or load state records
    $stateList = @()
    if (-not [string]::IsNullOrEmpty($StateCsvPath) -and (Test-Path $StateCsvPath)) {
        $loaded = Read-Y2State -Path $StateCsvPath
        if ($loaded) {
            $stateList = @($loaded)
        }
    } elseif ($StateRecords -and $StateRecords.Count -gt 0) {
        $stateList = @($StateRecords)
    }

    # If state is still empty, initialize from VMs
    if ($stateList.Count -eq 0) {
        foreach ($vm in $VMs) {
            $vName = ""
            if ($vm -is [System.Collections.IDictionary]) {
                if ($vm.ContainsKey('vm_name')) { $vName = [string]$vm['vm_name'] }
                elseif ($vm.ContainsKey('Name')) { $vName = [string]$vm['Name'] }
                elseif ($vm.ContainsKey('VMName')) { $vName = [string]$vm['VMName'] }
            } else {
                if ($vm.PSObject.Properties['vm_name']) { $vName = [string]$vm.vm_name }
                elseif ($vm.PSObject.Properties['Name']) { $vName = [string]$vm.Name }
                elseif ($vm.PSObject.Properties['VMName']) { $vName = [string]$vm.VMName }
            }

            if (-not [string]::IsNullOrEmpty($vName)) {
                $stateList += [PSCustomObject][ordered]@{
                    vm_name            = $vName
                    verified_power     = ''
                    started_by_run     = 'false'
                    start              = ''
                    run                = ''
                    restore            = ''
                    output_bytes       = 0
                    possibly_truncated = 'false'
                }
            }
        }
        if (-not [string]::IsNullOrEmpty($StateCsvPath)) {
            Write-Y2State -Path $StateCsvPath -Rows $stateList
        }
    }

    # Build queue of runnable VMs
    $pendingVMs = [System.Collections.ArrayList]::new()
    $busyVMs = [System.Collections.ArrayList]::new()
    $runningJobs = @{}
    $throttleAttempt = 1

    foreach ($vm in $VMs) {
        $vName = ""
        $rg = ""
        if ($vm -is [System.Collections.IDictionary]) {
            if ($vm.ContainsKey('vm_name')) { $vName = [string]$vm['vm_name'] }
            elseif ($vm.ContainsKey('Name')) { $vName = [string]$vm['Name'] }
            elseif ($vm.ContainsKey('VMName')) { $vName = [string]$vm['VMName'] }

            if ($vm.ContainsKey('resource_group')) { $rg = [string]$vm['resource_group'] }
            elseif ($vm.ContainsKey('ResourceGroup')) { $rg = [string]$vm['ResourceGroup'] }
            elseif ($vm.ContainsKey('rg')) { $rg = [string]$vm['rg'] }
            elseif ($vm.ContainsKey('ResourceGroupName')) { $rg = [string]$vm['ResourceGroupName'] }
        } else {
            if ($vm.PSObject.Properties['vm_name']) { $vName = [string]$vm.vm_name }
            elseif ($vm.PSObject.Properties['Name']) { $vName = [string]$vm.Name }
            elseif ($vm.PSObject.Properties['VMName']) { $vName = [string]$vm.VMName }

            if ($vm.PSObject.Properties['resource_group']) { $rg = [string]$vm.resource_group }
            elseif ($vm.PSObject.Properties['ResourceGroup']) { $rg = [string]$vm.ResourceGroup }
            elseif ($vm.PSObject.Properties['rg']) { $rg = [string]$vm.rg }
            elseif ($vm.PSObject.Properties['ResourceGroupName']) { $rg = [string]$vm.ResourceGroupName }
        }

        if ([string]::IsNullOrEmpty($vName)) {
            continue
        }

        # Check if VM is runnable
        $skip = $false
        foreach ($rec in $stateList) {
            $recName = if ($rec -is [System.Collections.IDictionary]) { $rec['vm_name'] } else { $rec.vm_name }
            if ($recName -eq $vName) {
                $runVal = if ($rec -is [System.Collections.IDictionary]) { $rec['run'] } else { $rec.run }
                $startVal = if ($rec -is [System.Collections.IDictionary]) { $rec['start'] } else { $rec.start }
                if ($runVal -in @('os_mismatch', 'start_failed', 'ok') -or $startVal -eq 'start_failed') {
                    $skip = $true
                }
                break
            }
        }

        if (-not $skip) {
            $pendingVMs.Add(@{
                VmName        = $vName
                ResourceGroup = $rg
                VmItem        = $vm
                Retries       = 0
                NextAttemptAt = [DateTime]::MinValue
                Status        = 'pending'
            }) | Out-Null
        }
    }

    # Main pool coordination loop
    while ($pendingVMs.Count -gt 0 -or $busyVMs.Count -gt 0 -or $runningJobs.Count -gt 0) {
        if (Test-Y2AuthTripped) {
            Add-Y2Result -StateRecords $stateList -MarkRemainingAuthAborted -StateCsvPath $StateCsvPath | Out-Null
            break
        }

        # Promote any busy VMs whose cooldown has expired
        $now = Get-Y2Date
        $stillBusy = [System.Collections.ArrayList]::new()
        foreach ($b in $busyVMs) {
            if ($now -ge $b.NextAttemptAt) {
                $pendingVMs.Add($b) | Out-Null
            } else {
                $stillBusy.Add($b) | Out-Null
            }
        }
        $busyVMs = $stillBusy

        # Submit jobs while capacity is available and pending VMs exist
        while ($runningJobs.Count -lt $MaxJobs -and $pendingVMs.Count -gt 0) {
            if (Test-Y2AuthTripped) {
                Add-Y2Result -StateRecords $stateList -MarkRemainingAuthAborted -StateCsvPath $StateCsvPath | Out-Null
                break
            }

            $candidate = $pendingVMs[0]
            $pendingVMs.RemoveAt(0)

            $vmName = $candidate.VmName
            $rg = $candidate.ResourceGroup

            try {
                $job = Start-Y2RunJob -ResourceGroup $rg -VMName $vmName -CommandId $CommandId -ScriptPath $ScriptPath -ErrorAction Stop
                Record-Y2AuthSuccess
                $throttleAttempt = 1

                $jobId = if ($null -ne $job.Id) { [string]$job.Id } else { [string]([Guid]::NewGuid()) }
                $runningJobs[$jobId] = @{
                    Job           = $job
                    VmName        = $vmName
                    ResourceGroup = $rg
                    Tracker       = $candidate
                }
            } catch {
                $err = $_
                $classification = Classify-Y2Error -ErrorObject $err

                if ($classification.Type -eq 'Throttled') {
                    # 429: entire pool pauses!
                    Wait-Y2Backoff -ErrorOrClassification $classification -Attempt $throttleAttempt
                    $throttleAttempt++
                    $pendingVMs.Insert(0, $candidate) | Out-Null
                    break
                } elseif ($classification.Type -eq 'ConflictBusy') {
                    # 409: VM busy! Re-queue with next_attempt_at = now + 60s without inline sleep (INV-11)
                    Set-Y2VmBusy -VmTracker $candidate | Out-Null
                    if ($candidate.Status -eq 'run_command_busy') {
                        Add-Y2Result -StateRecords $stateList -VmName $vmName -RunStatus 'run_command_busy' -StateCsvPath $StateCsvPath | Out-Null
                    } else {
                        $busyVMs.Add($candidate) | Out-Null
                    }
                } elseif ($classification.Type -eq 'AuthFailure') {
                    Record-Y2AuthFailure -ErrorObject $err | Out-Null
                    if (Test-Y2AuthTripped) {
                        Add-Y2Result -StateRecords $stateList -MarkRemainingAuthAborted -StateCsvPath $StateCsvPath | Out-Null
                        break
                    }
                    Add-Y2Result -StateRecords $stateList -VmName $vmName -RunStatus 'failed' -StateCsvPath $StateCsvPath | Out-Null
                } else {
                    Add-Y2Result -StateRecords $stateList -VmName $vmName -RunStatus 'failed' -StateCsvPath $StateCsvPath | Out-Null
                }
            }
        }

        if (Test-Y2AuthTripped) {
            Add-Y2Result -StateRecords $stateList -MarkRemainingAuthAborted -StateCsvPath $StateCsvPath | Out-Null
            break
        }

        # Sleep poll interval if jobs are running or all remaining VMs are waiting on cooldown
        if ($runningJobs.Count -gt 0 -or ($pendingVMs.Count -eq 0 -and $busyVMs.Count -gt 0)) {
            Start-Y2Sleep -Seconds $PollSeconds
        }

        # Poll and collect finished jobs
        if ($runningJobs.Count -gt 0) {
            $finishedJobIds = @()
            foreach ($jobId in @($runningJobs.Keys)) {
                $tracked = $runningJobs[$jobId]
                $job = $tracked.Job

                $currentJob = Get-Y2Job -Id $job.Id -ErrorAction SilentlyContinue
                if ($null -eq $currentJob) {
                    $currentJob = $job
                }

                if ($currentJob.State -in @('Completed', 'Failed', 'Stopped')) {
                    $finishedJobIds += $jobId
                    $vmName = $tracked.VmName

                    $jobOutput = $null
                    $jobErrors = @()
                    try {
                        $jobOutput = Receive-Y2Job -Job $currentJob -Keep -ErrorAction SilentlyContinue -ErrorVariable recvErrors
                        if ($recvErrors) {
                            $jobErrors += $recvErrors
                        }
                    } catch {
                        $jobErrors += $_
                    }

                    if ($currentJob.Error -and $currentJob.Error.Count -gt 0) {
                        $jobErrors += $currentJob.Error
                    }
                    if ($currentJob.ChildJobs) {
                        foreach ($cj in $currentJob.ChildJobs) {
                            if ($cj.Error -and $cj.Error.Count -gt 0) {
                                $jobErrors += $cj.Error
                            }
                        }
                    }
                    if ($currentJob.JobStateInfo -and $currentJob.JobStateInfo.Reason) {
                        $jobErrors += $currentJob.JobStateInfo.Reason
                    }

                    if ($currentJob.State -eq 'Failed' -or ($jobErrors.Count -gt 0 -and $null -eq $jobOutput)) {
                        $errObj = if ($jobErrors.Count -gt 0) { $jobErrors[0] } else { "Job failed without error record" }
                        $classification = Classify-Y2Error -ErrorObject $errObj

                        if ($classification.Type -eq 'Throttled') {
                            Wait-Y2Backoff -ErrorOrClassification $classification -Attempt $throttleAttempt
                            $throttleAttempt++
                            $pendingVMs.Insert(0, $tracked.Tracker) | Out-Null
                        } elseif ($classification.Type -eq 'ConflictBusy') {
                            Set-Y2VmBusy -VmTracker $tracked.Tracker | Out-Null
                            if ($tracked.Tracker.Status -eq 'run_command_busy') {
                                Add-Y2Result -StateRecords $stateList -VmName $vmName -RunStatus 'run_command_busy' -StateCsvPath $StateCsvPath | Out-Null
                            } else {
                                $busyVMs.Add($tracked.Tracker) | Out-Null
                            }
                        } elseif ($classification.Type -eq 'AuthFailure') {
                            Record-Y2AuthFailure -ErrorObject $errObj | Out-Null
                            if (Test-Y2AuthTripped) {
                                Add-Y2Result -StateRecords $stateList -MarkRemainingAuthAborted -StateCsvPath $StateCsvPath | Out-Null
                            } else {
                                Add-Y2Result -StateRecords $stateList -VmName $vmName -RunStatus 'failed' -StateCsvPath $StateCsvPath | Out-Null
                            }
                        } else {
                            Add-Y2Result -StateRecords $stateList -VmName $vmName -RunStatus 'failed' -StateCsvPath $StateCsvPath | Out-Null
                        }
                    } else {
                        Record-Y2AuthSuccess
                        $throttleAttempt = 1

                        $out = Get-Y2RunCommandOutput -JobOutput $jobOutput
                        $stdoutText = $out.StdOut
                        $stderrText = $out.StdErr

                        if (-not [string]::IsNullOrEmpty($ReportDir)) {
                            $stdoutPath = Join-Path $ReportDir "$vmName.stdout.txt"
                            $stderrPath = Join-Path $ReportDir "$vmName.stderr.txt"
                            Write-Y2File -Path $stdoutPath -Content $stdoutText
                            Write-Y2File -Path $stderrPath -Content $stderrText
                        }

                        $outputBytes = [System.Text.Encoding]::UTF8.GetByteCount($stdoutText)
                        $possiblyTruncated = ($outputBytes -ge 4096)

                        Add-Y2Result -StateRecords $stateList -VmName $vmName -RunStatus 'ok' -OutputBytes $outputBytes -PossiblyTruncated $possiblyTruncated -StateCsvPath $StateCsvPath | Out-Null
                    }

                    try {
                        Remove-Y2Job -Job $currentJob -Force -ErrorAction SilentlyContinue
                    } catch {}
                }
            }

            foreach ($fId in $finishedJobIds) {
                $runningJobs.Remove($fId)
            }
        }
    }

    if (-not [string]::IsNullOrEmpty($StateCsvPath)) {
        Write-Y2State -Path $StateCsvPath -Rows $stateList
    }

    return $stateList
}

# ---------------------------------------------------------------------------
# Section 7: Module Exports
# ---------------------------------------------------------------------------

Export-ModuleMember -Function @(
    'Invoke-Y2Graph',
    'Get-Y2VmStatus',
    'Start-Y2Vm',
    'Stop-Y2Vm',
    'Start-Y2RunJob',
    'Get-Y2Job',
    'Receive-Y2Job',
    'Remove-Y2Job',
    'Start-Y2Sleep',
    'Set-Y2SleepHandler',
    'Get-Y2Date',
    'Set-Y2VirtualTime',
    'Reset-Y2VirtualTime',
    'Classify-Y2Error',
    'Get-Y2BackoffSeconds',
    'Wait-Y2Backoff',
    'Get-Y2BusyNextAttempt',
    'Set-Y2VmBusy',
    'Test-Y2VmReady',
    'Reset-Y2AuthCounter',
    'Record-Y2AuthSuccess',
    'Record-Y2AuthFailure',
    'Test-Y2AuthTripped',
    'Get-Y2AuthBreaker',
    'Set-Y2AuthBreakerThreshold',
    'Write-Y2File',
    'Write-Y2Json',
    'Read-Y2State',
    'Write-Y2State',
    'Read-Y2Targets',
    'Write-Y2Targets',
    'Add-Y2Result',
    'Get-Y2RunCommandOutput',
    'Invoke-Y2JobPool'
) -Alias @(
    'Get-Y2ErrorClassification',
    'Invoke-Y2Backoff'
)
