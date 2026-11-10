<#
.SYNOPSIS
    Installs and configures Azure DevOps self-hosted agent on Windows Server 2022 (win01).
.DESCRIPTION
    Windows PowerShell 5.1 compatible script to install required Az modules, download
    and extract Azure Pipelines agent package, configure the agent as an automatic
    Windows service, and output a JSON object containing PowerShell and module versions.
#>

[CmdletBinding()]
param (
    [Parameter(Mandatory = $true)]
    [string]$OrgUrl,

    [Parameter(Mandatory = $true)]
    [string]$Pat,

    [Parameter(Mandatory = $false)]
    [string]$PoolName = "Production Windows Agent",

    [Parameter(Mandatory = $false)]
    [string]$AgentName = "win01",

    [Parameter(Mandatory = $false)]
    [string]$PackageUrl = ""
)

$ErrorActionPreference = "Stop"

# Enforce TLS 1.2
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# 1. Install required Az modules for Windows PowerShell 5.1 if missing
$requiredModules = @("Az.Accounts", "Az.Compute", "Az.ResourceGraph")
$missingModules = @()
foreach ($mod in $requiredModules) {
    $found = Get-Module -ListAvailable -Name $mod -ErrorAction SilentlyContinue
    if (-not $found) {
        $missingModules += $mod
    }
}

if ($missingModules.Count -gt 0) {
    # Ensure NuGet package provider is present and >= 2.8.5.201
    $nuget = Get-PackageProvider -Name NuGet -ListAvailable -ErrorAction SilentlyContinue | Where-Object { $_.Version -ge [Version]"2.8.5.201" }
    if (-not $nuget) {
        Install-PackageProvider -Name NuGet -MinimumVersion 2.8.5.201 -Force -Scope AllUsers -ErrorAction Stop | Out-Null
    }

    # Trust PSGallery to avoid interactive prompts
    $gallery = Get-PSRepository -Name "PSGallery" -ErrorAction SilentlyContinue
    if ($gallery -and ($gallery.InstallationPolicy -ne "Trusted")) {
        Set-PSRepository -Name "PSGallery" -InstallationPolicy Trusted -ErrorAction SilentlyContinue
    }

    # Install missing Az modules
    foreach ($mod in $missingModules) {
        Install-Module -Name $mod -Force -AllowClobber -Scope AllUsers -ErrorAction Stop
    }
}

# 2. Download and extract Azure Pipelines agent package to C:\agent if not present
$agentDir = "C:\agent"
if (-not (Test-Path $agentDir)) {
    New-Item -ItemType Directory -Path $agentDir -Force | Out-Null
}

$isExtracted = (Test-Path (Join-Path $agentDir "config.cmd")) -and (Test-Path (Join-Path $agentDir "bin\Agent.Listener.exe"))
if (-not $isExtracted) {
    $zipPath = Join-Path $env:TEMP "vsts-agent-win-x64.zip"
    $defaultCdnUrl = "https://download.agent.dev.azure.com/agent/5.281.1/vsts-agent-win-x64-5.281.1.zip"
    $downloadUrl = if ($PackageUrl) { $PackageUrl } else { "" }

    if (-not $downloadUrl) {
        try {
            $headers = @{ "User-Agent" = "PowerShell-Agent-Installer" }
            $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/microsoft/azure-pipelines-agent/releases/latest" -Headers $headers -TimeoutSec 15 -ErrorAction Stop
            $asset = $rel.assets | Where-Object { $_.name -like "vsts-agent-win-x64-*.zip" } | Select-Object -First 1
            if ($asset -and $asset.browser_download_url) {
                $downloadUrl = $asset.browser_download_url
            }
        } catch {
            # Fallback to default active CDN URL
        }
    }

    if (-not $downloadUrl) {
        $downloadUrl = $defaultCdnUrl
    }

    Write-Host "Downloading Azure Pipelines agent from $downloadUrl..."

    # Download with retry loop (3 attempts with 5s delay)
    $downloadSuccess = $false
    $maxAttempts = 3
    $attempt = 1
    $webClient = New-Object System.Net.WebClient

    while (($attempt -le $maxAttempts) -and (-not $downloadSuccess)) {
        try {
            if (Test-Path $zipPath) {
                Remove-Item -Path $zipPath -Force -ErrorAction SilentlyContinue
            }
            $webClient.DownloadFile($downloadUrl, $zipPath)
            if ((Test-Path $zipPath) -and ((Get-Item $zipPath).Length -gt 0)) {
                $downloadSuccess = $true
            } else {
                throw "Downloaded file is empty or missing."
            }
        } catch {
            if ($attempt -lt $maxAttempts) {
                Write-Warning "Download attempt $attempt failed ($($_.Exception.Message)). Retrying in 5 seconds..."
                Start-Sleep -Seconds 5
            } else {
                if ($downloadUrl -ne $defaultCdnUrl) {
                    try {
                        Write-Host "Trying fallback to default CDN: $defaultCdnUrl"
                        $downloadUrl = $defaultCdnUrl
                        $webClient.DownloadFile($downloadUrl, $zipPath)
                        if ((Test-Path $zipPath) -and ((Get-Item $zipPath).Length -gt 0)) {
                            $downloadSuccess = $true
                        } else {
                            throw "Downloaded fallback file is empty."
                        }
                    } catch {
                        throw "Failed to download agent package after $maxAttempts attempts: $($_.Exception.Message)"
                    }
                } else {
                    throw "Failed to download agent package after $maxAttempts attempts: $($_.Exception.Message)"
                }
            }
        }
        $attempt += 1
    }

    Write-Host "Extracting agent to $agentDir..."
    Expand-Archive -Path $zipPath -DestinationPath $agentDir -Force
    Remove-Item -Path $zipPath -Force -ErrorAction SilentlyContinue
}

# 3. Configure the agent (idempotent: only configure if not already configured with same url & agent name)
$existingService = Get-Service -Name "vstsagent.*" -ErrorAction SilentlyContinue | Select-Object -First 1
$agentConfigFile = Join-Path $agentDir ".agent"
$isAlreadyConfigured = $false

if ($existingService -and (Test-Path $agentConfigFile)) {
    try {
        $agentConfig = Get-Content $agentConfigFile -Raw | ConvertFrom-Json
        $cfgUrl = $agentConfig.serverUrl.TrimEnd('/')
        $targetUrl = $OrgUrl.TrimEnd('/')
        if (($cfgUrl -ieq $targetUrl) -and ($agentConfig.agentName -ieq $AgentName)) {
            $isAlreadyConfigured = $true
        }
    } catch {
        $isAlreadyConfigured = $false
    }
}

if (-not $isAlreadyConfigured) {
    if ($existingService) {
        Stop-Service -Name $existingService.Name -Force -ErrorAction SilentlyContinue
        & sc.exe delete $existingService.Name | Out-Null
        Start-Sleep -Seconds 2
    }

    Push-Location $agentDir
    try {
        $logFile = Join-Path $env:TEMP "agent_config_$PID.log"
        $errFile = Join-Path $env:TEMP "agent_config_err_$PID.log"

        $cmdArgs = "/c config.cmd --unattended --url `"$OrgUrl`" --auth pat --token `"$Pat`" --pool `"$PoolName`" --agent `"$AgentName`" --runAsService --replace --acceptTeeEula"
        $proc = Start-Process -FilePath "cmd.exe" -ArgumentList $cmdArgs `
            -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $logFile -RedirectStandardError $errFile

        if ($proc.ExitCode -ne 0) {
            $errText = ""
            if (Test-Path $logFile) { $errText += (Get-Content $logFile -Raw) }
            if (Test-Path $errFile) { $errText += "`n" + (Get-Content $errFile -Raw) }
            $cleanErr = $errText -replace [regex]::Escape($Pat), "********"
            Write-Error "Agent configuration failed (exit code $($proc.ExitCode)):`n$cleanErr"
            exit $proc.ExitCode
        }
    } finally {
        Remove-Item -Path $logFile, $errFile -Force -ErrorAction SilentlyContinue
        Pop-Location
    }
}

# 4. Ensure Windows service is running and set to automatic startup
$svc = Get-Service -Name "vstsagent.*" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($svc) {
    Set-Service -Name $svc.Name -StartupType Automatic -ErrorAction SilentlyContinue
    if ($svc.Status -eq "Running") {
        Restart-Service -Name $svc.Name -Force
    } else {
        Start-Service -Name $svc.Name
    }
    # Wait up to 30 seconds for service to reach Running state
    $timeout = 30
    $elapsed = 0
    while ($elapsed -lt $timeout) {
        $svc = Get-Service -Name "vstsagent.*" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($svc -and ($svc.Status -eq "Running")) {
            break
        }
        Start-Sleep -Seconds 2
        $elapsed += 2
    }
} else {
    Write-Error "Agent service ('vstsagent.*') was not found after configuration."
    exit 1
}

# 5. Emit JSON with PSVersion, Az module versions, and agent status
$accountsMod = Get-Module -ListAvailable -Name Az.Accounts -ErrorAction SilentlyContinue | Sort-Object Version -Descending | Select-Object -First 1
$computeMod = Get-Module -ListAvailable -Name Az.Compute -ErrorAction SilentlyContinue | Sort-Object Version -Descending | Select-Object -First 1
$rgMod = Get-Module -ListAvailable -Name Az.ResourceGraph -ErrorAction SilentlyContinue | Sort-Object Version -Descending | Select-Object -First 1

$accountsVer = if ($accountsMod) { $accountsMod.Version.ToString() } else { "None" }
$computeVer = if ($computeMod) { $computeMod.Version.ToString() } else { "None" }
$rgVer = if ($rgMod) { $rgMod.Version.ToString() } else { "None" }

$finalSvc = Get-Service -Name "vstsagent.*" -ErrorAction SilentlyContinue | Select-Object -First 1
$svcStatus = if ($finalSvc) { $finalSvc.Status.ToString() } else { "Unknown" }

$versions = [ordered]@{
    PSVersion = $PSVersionTable.PSVersion.ToString()
    "Az.Accounts" = $accountsVer
    "Az.Compute" = $computeVer
    "Az.ResourceGraph" = $rgVer
    AgentName = $AgentName
    PoolName = $PoolName
    ServiceStatus = $svcStatus
}

$versions | ConvertTo-Json
