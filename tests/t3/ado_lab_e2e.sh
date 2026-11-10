#!/usr/bin/env bash
# tests/t3/ado_lab_e2e.sh - Tier 3 live Azure test lab E2E verification (TP-14)
# Executed via lab/run-lab.sh against real Azure lab fleet (rg-lab-10vms).
# Pure ASCII, LF line endings, BOM-less UTF-8, Bash 3.2+ compatible.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LAB_DIR="$REPO_ROOT/lab"
EVIDENCE_DIR="${1:-${EVIDENCE_DIR:-$REPO_ROOT/.handoffs/20261108-ado-patch-templates/42-evidence/t3}}"

mkdir -p "$EVIDENCE_DIR"
EVIDENCE_DIR="$(cd "$EVIDENCE_DIR" && pwd)"

echo "================================================================="
echo "=== Tier 3 Live Azure Lab E2E Harness (TP-14) ==="
echo "=== Evidence Directory: $EVIDENCE_DIR ==="
echo "================================================================="

REAL_AZ="${Y2_REAL_AZ:-$(command -v az)}"
if [ -z "$REAL_AZ" ] || [ ! -x "$REAL_AZ" ]; then
  echo "Error: Real az CLI not found or not executable" >&2
  exit 1
fi

# -----------------------------------------------------------------------------
# 1. Fleet Initial State Setup
# Pre-start win02; ensure remaining 4 VMs (win01, lin01, lin02, lin03) are deallocated
# -----------------------------------------------------------------------------
echo "`date -u +'%Y-%m-%dT%H:%M:%SZ'` [INFO] Step 1: Initializing fleet power state..."
echo "[INFO] Ensuring all VMs deallocated before test setup..."
bash "$LAB_DIR/toogle_vm.sh" stop --wait

echo "[INFO] Pre-starting win02 (must remain running throughout restore)..."
bash "$LAB_DIR/toogle_vm.sh" start win02 --wait

echo "[INFO] Verifying pre-run power state:"
bash "$LAB_DIR/toogle_vm.sh" status | tee "$EVIDENCE_DIR/pre-run-fleet-status.txt"

WIN02_INIT_POWER="$("$REAL_AZ" vm show -g rg-lab-10vms -n win02 -d --query powerState -o tsv 2>/dev/null || true)"
if [[ "$WIN02_INIT_POWER" != *"running"* ]]; then
  echo "Error: win02 failed to pre-start (powerState: $WIN02_INIT_POWER)" >&2
  exit 1
fi
echo "[INFO] Pre-start verified: win02 is $WIN02_INIT_POWER"

# -----------------------------------------------------------------------------
# 2. Acquire ARM Access Token & Context (ADR-004)
# set +x strictly enforced: token string is NEVER printed or logged
# -----------------------------------------------------------------------------
echo "`date -u +'%Y-%m-%dT%H:%M:%SZ'` [INFO] Step 2: Acquiring ARM access token (ADR-004)..."
set +x
ARM_TOKEN="$("$REAL_AZ" account get-access-token --resource https://management.azure.com/ -o tsv --query accessToken)"
if [ -z "$ARM_TOKEN" ]; then
  echo "Error: Failed to obtain ARM access token from az CLI" >&2
  exit 1
fi

SUB_ID="$("$REAL_AZ" account show --query id -o tsv 2>/dev/null || echo "")"
if [ -z "$SUB_ID" ]; then
  SUB_ID="79f7da80-0c64-4e4b-9ed4-316a88142152"
fi

TENANT_ID="$("$REAL_AZ" account show --query tenantId -o tsv 2>/dev/null || echo "")"
ACCOUNT_ID="$("$REAL_AZ" account show --query user.name -o tsv 2>/dev/null || echo "")"
if [ -z "$ACCOUNT_ID" ]; then
  ACCOUNT_ID="$("$REAL_AZ" account show --query user -o tsv 2>/dev/null || echo "")"
fi

echo "[INFO] Azure context resolved: Subscription=$SUB_ID, Account=$ACCOUNT_ID, Tenant=$TENANT_ID"

# -----------------------------------------------------------------------------
# 3. Locate Container Runtime and Image
# -----------------------------------------------------------------------------
echo "`date -u +'%Y-%m-%dT%H:%M:%SZ'` [INFO] Step 3: Resolving container runtime and pwsh 7 image..."
CONTAINER_CLI=""
if command -v podman >/dev/null 2>&1; then
  CONTAINER_CLI="podman"
elif command -v docker >/dev/null 2>&1; then
  CONTAINER_CLI="docker"
else
  echo "Error: Neither podman nor docker found on host" >&2
  exit 1
fi

CONTAINER_IMAGE="${PWSH_CONTAINER_IMAGE:-mcr.microsoft.com/powershell:7.4-ubuntu-22.04}"
MODULE_CACHE_DIR="${Y2_MODULE_CACHE_DIR:-$HOME/.cache/y2-pwsh-modules}"
mkdir -p "$MODULE_CACHE_DIR"

echo "[INFO] Container CLI: $CONTAINER_CLI | Image: $CONTAINER_IMAGE | Module cache: $MODULE_CACHE_DIR"

# -----------------------------------------------------------------------------
# 4. Generate In-Container PowerShell Harness Script
# -----------------------------------------------------------------------------
cat <<'EOF' > "$EVIDENCE_DIR/harness.ps1"
# harness.ps1 - In-container test execution driver for Tier 3 ADO patch scripts
$ErrorActionPreference = 'Stop'

Write-Host "=== [Container] Initializing Az Modules ==="
if (-not (Get-Module -ListAvailable -Name Az.Accounts, Az.Compute, Az.ResourceGraph | Where-Object { $_.Name -eq 'Az.Compute' })) {
    Write-Host "[Container] Installing Az modules (Az.Accounts, Az.Compute, Az.ResourceGraph)..."
    Set-PSRepository -Name PSGallery -InstallationPolicy Trusted
    Install-Module -Name Az.Accounts, Az.Compute, Az.ResourceGraph -Scope CurrentUser -Force -Repository PSGallery
}

Import-Module Az.Accounts -Force
Import-Module Az.Compute -Force
Import-Module Az.ResourceGraph -Force

Write-Host "[Container] Az modules verified:"
Get-Module -Name Az.Accounts, Az.Compute, Az.ResourceGraph | Select-Object Name, Version | Format-Table -AutoSize | Out-String | Write-Host

Write-Host "=== [Container] Connecting to Azure via ARM token (ADR-004) ==="
$token = $env:ARM_TOKEN
$accountId = $env:ACCOUNT_ID
$tenantId = $env:TENANT_ID
$subId = $env:SUBSCRIPTION_ID

if ([string]::IsNullOrEmpty($token)) {
    throw "ARM_TOKEN environment variable is not set."
}

$connectArgs = @{
    AccessToken = $token
    AccountId   = $accountId
}
if (-not [string]::IsNullOrEmpty($tenantId)) {
    $connectArgs['Tenant'] = $tenantId
}
if (-not [string]::IsNullOrEmpty($subId)) {
    $connectArgs['Subscription'] = $subId
}

$null = Connect-AzAccount @connectArgs
if (-not [string]::IsNullOrEmpty($subId)) {
    try {
        $null = Set-AzContext -Subscription $subId
    } catch {
        Write-Warning "Set-AzContext: $($_.Exception.Message)"
    }
}
$ctx = Get-AzContext
Write-Host "[Container] Successfully connected to Azure. Subscription: $($ctx.Subscription.Id) ($($ctx.Subscription.Name))"

$scriptsDir = "/repo/ado/scripts"
$evidenceDir = $env:EVIDENCE_DIR
$kqlQuery = 'Resources | where resourceGroup =~ "rg-lab-10vms" and type =~ "microsoft.compute/virtualmachines"'

# -----------------------------------------------------------------------------
# PHASE 1: Invoke Workflow (Windows Fleet)
# win02 is pre-started; win01 is deallocated.
# win01 should start, run payload, and be restored to deallocated.
# win02 should NOT be stopped by restore (remains running).
# lin01-lin03 should be marked os_mismatch.
# -----------------------------------------------------------------------------
Write-Host "`n========================================================"
Write-Host "=== [Container] Phase 1: Invoke (Windows Fleet) ==="
Write-Host "========================================================"

$invokeWorkDir = Join-Path $evidenceDir "invoke/work"
$invokeReportDir = Join-Path $evidenceDir "invoke/report"
New-Item -ItemType Directory -Path $invokeWorkDir -Force | Out-Null
New-Item -ItemType Directory -Path $invokeReportDir -Force | Out-Null

Write-Host "`n[Container] Running resolve.ps1 (windows)..."
& "$scriptsDir/resolve.ps1" `
    -TargetsKql $kqlQuery `
    -OsType 'windows' `
    -WorkDir $invokeWorkDir `
    -ReportDir $invokeReportDir `
    -RunId "t3-invoke-$(Get-Date -Format 'yyyyMMddHHmmss')"

Write-Host "`n[Container] Running power-start.ps1..."
& "$scriptsDir/power-start.ps1" `
    -WorkDir $invokeWorkDir `
    -PollSeconds 10 `
    -TimeoutSeconds 600

Write-Host "`n[Container] Running run-command.ps1 (sample-windows.ps1)..."
& "$scriptsDir/run-command.ps1" `
    -OsType 'windows' `
    -PayloadFile "/repo/ado/examples/payloads/sample-windows.ps1" `
    -WorkDir $invokeWorkDir `
    -ReportDir $invokeReportDir `
    -MaxJobs 20 `
    -PollSeconds 10

Write-Host "`n[Container] Running power-restore.ps1..."
& "$scriptsDir/power-restore.ps1" `
    -WorkDir $invokeWorkDir `
    -PollSeconds 10 `
    -TimeoutSeconds 600

Write-Host "`n[Container] Running finalize.ps1..."
& "$scriptsDir/finalize.ps1" `
    -WorkDir $invokeWorkDir `
    -ReportDir $invokeReportDir

# -----------------------------------------------------------------------------
# PHASE 2: Collect Workflow (Linux Fleet)
# lin01-lin03 are deallocated.
# lin01-lin03 should start, run payload, and be restored to deallocated.
# win01-win02 should be marked os_mismatch.
# win02 must remain untouched (still running).
# -----------------------------------------------------------------------------
Write-Host "`n========================================================"
Write-Host "=== [Container] Phase 2: Collect (Linux Fleet) ==="
Write-Host "========================================================"

$collectWorkDir = Join-Path $evidenceDir "collect/work"
$collectReportDir = Join-Path $evidenceDir "collect/report"
New-Item -ItemType Directory -Path $collectWorkDir -Force | Out-Null
New-Item -ItemType Directory -Path $collectReportDir -Force | Out-Null

Write-Host "`n[Container] Running resolve.ps1 (linux)..."
& "$scriptsDir/resolve.ps1" `
    -TargetsKql $kqlQuery `
    -OsType 'linux' `
    -WorkDir $collectWorkDir `
    -ReportDir $collectReportDir `
    -RunId "t3-collect-$(Get-Date -Format 'yyyyMMddHHmmss')"

Write-Host "`n[Container] Running power-start.ps1..."
& "$scriptsDir/power-start.ps1" `
    -WorkDir $collectWorkDir `
    -PollSeconds 10 `
    -TimeoutSeconds 600

Write-Host "`n[Container] Running run-command.ps1 (sample-linux.sh)..."
& "$scriptsDir/run-command.ps1" `
    -OsType 'linux' `
    -PayloadFile "/repo/ado/examples/payloads/sample-linux.sh" `
    -WorkDir $collectWorkDir `
    -ReportDir $collectReportDir `
    -MaxJobs 20 `
    -PollSeconds 10

Write-Host "`n[Container] Running power-restore.ps1..."
& "$scriptsDir/power-restore.ps1" `
    -WorkDir $collectWorkDir `
    -PollSeconds 10 `
    -TimeoutSeconds 600

Write-Host "`n[Container] Running finalize.ps1..."
& "$scriptsDir/finalize.ps1" `
    -WorkDir $collectWorkDir `
    -ReportDir $collectReportDir

Write-Host "`n[Container] All phases completed successfully."
EOF

# -----------------------------------------------------------------------------
# 5. Execute Harness Inside Container
# -----------------------------------------------------------------------------
echo "`date -u +'%Y-%m-%dT%H:%M:%SZ'` [INFO] Step 4: Running pwsh container harness..."

set +x
"$CONTAINER_CLI" run --rm \
  --network host \
  -v "$REPO_ROOT:/repo:ro,z" \
  -v "$EVIDENCE_DIR:/evidence:z" \
  -v "$MODULE_CACHE_DIR:/root/.local/share/powershell/Modules:z" \
  -e ARM_TOKEN="$ARM_TOKEN" \
  -e ACCOUNT_ID="$ACCOUNT_ID" \
  -e TENANT_ID="$TENANT_ID" \
  -e SUBSCRIPTION_ID="$SUB_ID" \
  -e EVIDENCE_DIR="/evidence" \
  -w "/evidence" \
  "$CONTAINER_IMAGE" \
  pwsh -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "/evidence/harness.ps1" \
  > "$EVIDENCE_DIR/harness.log" 2>&1
CONTAINER_RC=$?
set -e

cat "$EVIDENCE_DIR/harness.log"

if [ "$CONTAINER_RC" -ne 0 ]; then
  echo "Error: In-container PowerShell execution failed with exit code $CONTAINER_RC" >&2
  exit "$CONTAINER_RC"
fi

# -----------------------------------------------------------------------------
# 6. Post-Run Fleet Status & Assertions
# -----------------------------------------------------------------------------
echo "`date -u +'%Y-%m-%dT%H:%M:%SZ'` [INFO] Step 5: Performing post-execution assertions..."

echo "[INFO] Current fleet status before run-lab cleanup:"
bash "$LAB_DIR/toogle_vm.sh" status | tee "$EVIDENCE_DIR/pre-cleanup-fleet-status.txt"

# 1. Assert pre-started VM (win02) is STILL RUNNING
echo "[ASSERTION 1] Verifying win02 was NEVER stopped by restore..."
WIN02_CURR_POWER="$("$REAL_AZ" vm show -g rg-lab-10vms -n win02 -d --query powerState -o tsv 2>/dev/null || true)"
echo "[INFO] win02 current power state: $WIN02_CURR_POWER"
if [[ "$WIN02_CURR_POWER" != *"running"* ]]; then
  echo "ASSERTION FAILED: win02 was stopped during restore! Expected running, got $WIN02_CURR_POWER" >&2
  exit 1
fi
echo "[PASS] win02 remained running throughout all test phases."

# 2. Assert the other 4 VMs were restored to deallocated
echo "[ASSERTION 2] Verifying win01, lin01, lin02, lin03 are restored to deallocated..."
for vm in win01 lin01 lin02 lin03; do
  VM_CURR_POWER="$("$REAL_AZ" vm show -g rg-lab-10vms -n "$vm" -d --query powerState -o tsv 2>/dev/null || true)"
  echo "[INFO] $vm power state: $VM_CURR_POWER"
  if [[ "$VM_CURR_POWER" != *"deallocated"* ]]; then
    echo "ASSERTION FAILED: $vm was not restored to deallocated (got $VM_CURR_POWER)" >&2
    exit 1
  fi
done
echo "[PASS] All 4 candidate VMs restored to deallocated."

# 3. Assert status CSVs
INVOKE_CSV="$EVIDENCE_DIR/invoke/report/status.csv"
COLLECT_CSV="$EVIDENCE_DIR/collect/report/status.csv"

echo "[ASSERTION 3] Validating status CSVs..."
if [ ! -f "$INVOKE_CSV" ]; then
  echo "ASSERTION FAILED: Missing invoke status.csv at $INVOKE_CSV" >&2
  exit 1
fi
if [ ! -f "$COLLECT_CSV" ]; then
  echo "ASSERTION FAILED: Missing collect status.csv at $COLLECT_CSV" >&2
  exit 1
fi

echo "--- Invoke status.csv ---"
cat "$INVOKE_CSV"
echo "--- Collect status.csv ---"
cat "$COLLECT_CSV"

# Invoke assertions:
# win01: restored to deallocated (start=ok, run=ok, restore=ok)
if ! grep -q 'win01.*ok.*ok.*ok' "$INVOKE_CSV"; then
  echo "ASSERTION FAILED: win01 did not have ok,ok,ok in invoke status.csv" >&2
  exit 1
fi
# win02: run=ok, restore not submitted/ok (remained running)
if ! grep -q 'win02.*ok' "$INVOKE_CSV"; then
  echo "ASSERTION FAILED: win02 did not have run=ok in invoke status.csv" >&2
  exit 1
fi
# Linux VMs in invoke: os_mismatch
for vm in lin01 lin02 lin03; do
  if ! grep -q "$vm.*os_mismatch" "$INVOKE_CSV"; then
    echo "ASSERTION FAILED: $vm did not have os_mismatch in invoke status.csv" >&2
    exit 1
  fi
done

# Collect assertions:
# lin01-lin03: start=ok, run=ok, restore=ok
for vm in lin01 lin02 lin03; do
  if ! grep -q "$vm.*ok.*ok.*ok" "$COLLECT_CSV"; then
    echo "ASSERTION FAILED: $vm did not have ok,ok,ok in collect status.csv" >&2
    exit 1
  fi
done
# Windows VMs in collect: os_mismatch
for vm in win01 win02; do
  if ! grep -q "$vm.*os_mismatch" "$COLLECT_CSV"; then
    echo "ASSERTION FAILED: $vm did not have os_mismatch in collect status.csv" >&2
    exit 1
  fi
done
echo "[PASS] Status CSVs accurately reflect fleet execution and restore outcomes."

# Save status CSVs at top-level evidence directory
cp "$INVOKE_CSV" "$EVIDENCE_DIR/status-invoke.csv"
cp "$COLLECT_CSV" "$EVIDENCE_DIR/status-collect.csv"
cp "$INVOKE_CSV" "$EVIDENCE_DIR/status.csv"

# 4. Assert payload stdout outputs
echo "[ASSERTION 4] Validating run command payload outputs..."
for vm in win01 win02; do
  STDOUT_FILE="$EVIDENCE_DIR/invoke/report/$vm.stdout.txt"
  if [ ! -f "$STDOUT_FILE" ] || ! grep -q "Sample Windows Payload Executing" "$STDOUT_FILE"; then
    echo "ASSERTION FAILED: Missing or invalid payload output for $vm at $STDOUT_FILE" >&2
    exit 1
  fi
done
for vm in lin01 lin02 lin03; do
  STDOUT_FILE="$EVIDENCE_DIR/collect/report/$vm.stdout.txt"
  if [ ! -f "$STDOUT_FILE" ] || ! grep -q "Sample Linux Payload Executing" "$STDOUT_FILE"; then
    echo "ASSERTION FAILED: Missing or invalid payload output for $vm at $STDOUT_FILE" >&2
    exit 1
  fi
done
echo "[PASS] All 5 VMs successfully executed payload scripts and captured stdout."

# 5. Assert zero token strings in evidence
echo "[ASSERTION 5] Checking evidence directory for credential/token leakage (INV-5)..."
if grep -rn "eyJ" "$EVIDENCE_DIR" >/dev/null 2>&1; then
  echo "ASSERTION FAILED: Detected token pattern (eyJ) in evidence directory!" >&2
  exit 1
fi
echo "[PASS] Zero token strings found across all evidence files."

echo "================================================================="
echo "=== TP-14 Azure Lab E2E Verification PASSED successfully ==="
echo "================================================================="
exit 0
