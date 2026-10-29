# `yaml_to_bash` (`y2b`) & `yaml_to_powershell` (`y2p`) — Comprehensive User Guide

This guide is a complete, step-by-step reference for operators, systems engineers, and developers using `y2b` (for Linux) and `y2p` (for Windows) to automate fleet-wide checks, diagnostics, and remediation across Azure Virtual Machines.

---

## Table of Contents

1. [Architecture & How It Works](#1-architecture--how-it-works)
2. [Installation & Prerequisites](#2-installation--prerequisites)
3. [Step 1: Preparing Your Inventory (`list_vm.csv`)](#3-step-1-preparing-your-inventory-list_vmcsv)
4. [Step 2: Authoring a Linux Job (`y2b`)](#4-step-2-authoring-a-linux-job-y2b)
   - [4.1 Root Configuration](#41-root-configuration)
   - [4.2 Prechecks (Host-side Pre-flight Checks)](#42-prechecks-host-side-pre-flight-checks)
   - [4.3 RunCommand Steps (Remote Linux VM Execution)](#43-runcommand-steps-remote-linux-vm-execution)
   - [4.4 Collect & Transform (Report Generation)](#44-collect--transform-report-generation)
5. [Step 3: Authoring a Windows Job (`y2p`)](#5-step-3-authoring-a-windows-job-y2p)
   - [5.1 Windows Target Environment](#51-windows-target-environment)
   - [5.2 YAML Backslash Quoting & Path Rules](#52-yaml-backslash-quoting--path-rules)
   - [5.3 PowerShell Step Contract & Exit Code Semantics](#53-powershell-step-contract--exit-code-semantics)
   - [5.4 Host-Side Scripts in Windows Jobs](#54-host-side-scripts-in-windows-jobs)
   - [5.5 Full Windows Example](#55-full-windows-example)
6. [Step 4: Host Runner Environment & macOS Best Practices](#6-step-4-host-runner-environment--macos-best-practices)
7. [Step 5: CLI Commands & Workflows](#7-step-5-cli-commands--workflows)
   - [7.1 Validating: `y2b validate` / `y2p validate`](#71-validating-y2b-validate--y2p-validate)
   - [7.2 Inspecting Compiled Payload: `y2b compile` / `y2p compile`](#72-inspecting-compiled-payload-y2b-compile--y2p-compile)
   - [7.3 Safe Dry-Run: `--dry-run`](#73-safe-dry-run---dry-run)
   - [7.4 Production Run: `run`](#74-production-run-run)
   - [7.5 Resuming Interrupted Runs: `--resume`](#75-resuming-interrupted-runs---resume)
8. [Understanding Outputs & Logs](#8-understanding-outputs--logs)
   - [The Final Report (`result.csv`)](#the-final-report-resultcsv)
   - [The Consolidated Log (`result.log`)](#the-consolidated-log-resultlog)
   - [Outcome Markers Reference](#outcome-markers-reference)
9. [Operational Best Practices & Troubleshooting](#9-operational-best-practices--troubleshooting)
   - [Handling 4 KB Output Limits](#handling-4-kb-output-limits)
   - [Handling ARM Throttling & Concurrency](#handling-arm-throttling--concurrency)
   - [Authentication & The Circuit Breaker](#authentication--the-circuit-breaker)

---

## 1. Architecture & How It Works

Traditional automation runs a separate SSH/WinRM command or Azure Run-Command per check. On Azure, each `az vm run-command invoke` takes 30–90 seconds to initiate. Running 5 checks across 500 VMs with the naive approach triggers 2,500 run-command sessions, taking hours and exceeding Azure Resource Manager (ARM) rate limits.

`yaml_to_bash` (`y2b`) and `yaml_to_powershell` (`y2p`) solve this through a shared two-phase architecture:
1. **Host Prechecks**: Evaluating tenant/subscription permissions and power states locally on your runner machine before touching the VM. Non-target or deallocated VMs are skipped before any remote execution.
2. **Single-Session Compilation**: Bundling all remote checks into **one single composite script** (bash for `y2b`, PowerShell for `y2p`), sent in **one single `az vm run-command invoke` call** per VM.
3. **Fire-and-Forget Two-Phase Execution**:
   - **Phase 1 (Invoke)**: Fired with `--no-wait`. Workers do not block waiting for long-running scripts.
   - **Phase 2 (Collect)**: Results are collected after a configured delay (`sleep_before_collect`) via a lightweight reader script on a priority event scheduler.
4. **Zero-Corruption Concurrency**: Concurrent workers write strictly to their own VM part files (`NNNN-<vm_name>.csv` and `.log`).
5. **Deterministic Merge**: Sorting and merging all results in original input order into the final CSV report and log after all workers finish.

```
┌────────────────────────────────────────────────────────┐
│ HOST MACHINE / RUNNER (macOS or Linux)                 │
│                                                        │
│ list_vm.csv ──► [ Precheck ] ──► [ Compile Payload ]   │
│                   (Host)           (1 script)          │
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼ (Single az vm run-command per VM)
                            │   y2b: RunShellScript (remote.sh)
                            │   y2p: RunPowerShellScript (remote.ps1)
┌───────────────────────────┴────────────────────────────┐
│ REMOTE VIRTUAL MACHINE                                 │
│                                                        │
│ Executes steps 1..N in isolated subshells/processes    │
│ Appends detailed traces to $LOG_FILE                   │
│ Exports write_result base64 values to $RES_FILE        │
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼ (Output marker block returned in stdout)
┌────────────────────────────────────────────────────────┐
│ HOST MACHINE / RUNNER                                  │
│                                                        │
│ Collect & Computed Columns ──► Write Part Files        │
│                                (NNNN-vm.csv/.log)      │
│                                      │                 │
│                                      ▼                 │
│ Final Merge Step ────────────► result.csv + result.log │
└────────────────────────────────────────────────────────┘
```

---

## 2. Installation & Prerequisites

### Prerequisites
1. **Runner Host**: macOS (Apple Silicon / Intel with zsh) or Linux (Ubuntu, Debian, RHEL, Rocky, etc.).
2. **Python**: Python ≥ 3.9 installed.
3. **PyYAML**: Install via pip (`pip3 install pyyaml`).
4. **Azure CLI (`az`)**: Installed and authenticated (`az login` or Azure Managed Identity).
5. **Target Virtual Machines**:
   - Linux VMs with Azure Linux Guest Agent (`waagent`) for `y2b`.
   - Windows VMs with Azure Windows Guest Agent for `y2p`.

### Automated Virtual Environment Setup

The repository provides `setup_venv.sh` to initialize the virtual environment, install dependencies, and put both `y2b` and `y2p` into your shell's `PATH`:

```bash
cd yaml_to_bash

# Create venv, install requirements and activate in current shell:
source setup_venv.sh

# Verify CLIs are ready:
y2b --help
y2p --help
```

---

## 3. Step 1: Preparing Your Inventory (`list_vm.csv`)

The input inventory is a standard CSV file defining target VMs.

### Required Header Columns
The CSV must include at least these four columns (case-insensitive headers):
- `vm_name`: Name of the Azure Virtual Machine.
- `resource_group`: Resource group containing the VM.
- `subscription`: Azure subscription ID or name.
- `os`: Operating system name or family.

### OS Family Validation Rules
To prevent costly mistakes (e.g. sending bash scripts to Windows or PowerShell scripts to Linux), each CLI enforces strict OS family matching on the `os` column:

- **`y2b` (Linux)**: Accepts Linux distributions (`rhel8`, `rhel9`, `ubuntu`, `debian`, `centos`, `rocky`, `sles`, `oracle`, `alma`).
  - *If a Windows OS row is encountered (`windows2022`, `win11`), `y2b` fails validation and instructs you to use `y2p`.*
- **`y2p` (Windows)**: Accepts Windows distributions (`windows`, `win`, `windows2019`, `windows2022`, `windows10`, `winserver`).
  - *If a Linux OS row is encountered (`rhel8`, `ubuntu`), `y2p` fails validation and instructs you to use `y2b`.*

### Example Inventory
```csv
vm_name,resource_group,subscription,os,environment,owner
vm-app-01,rg-prod-east,11111111-2222-3333-4444-555555555555,rhel8,production,team-alpha
vm-app-02,rg-prod-east,11111111-2222-3333-4444-555555555555,rhel9,production,team-alpha
```

All CSV columns (e.g. `environment`, `owner`) are automatically exported as uppercase environment variables (`$ENVIRONMENT`, `$OWNER`) to host prechecks and computed column scripts.

---

## 4. Step 2: Authoring a Linux Job (`y2b`)

A job specification is written in YAML and defines pre-flight checks, remote VM steps, and host-side report transformations.

### 4.1 Root Configuration
```yaml
input: "./list_vm.csv"             # Target inventory CSV
output_log: "./result.log"         # Consolidated execution log
output_csv: "./result.csv"         # Final CSV report
actions: "skip"                    # Power state policy: "skip" | "start"
parallel: 15                       # Concurrent VM workers
sleep_before_collect: "5m"         # REQUIRED. Delay before first collect attempt
collect_retry_interval: "30s"      # Optional (default 30s). Wait before retrying still-busy VM
collect_retries: 3                 # Optional (default 3). Maximum collect retries
```

### 4.2 Prechecks (Host-side Pre-flight Checks)
Prechecks run on the **host runner machine** before invoking remote commands:
```yaml
invoke:
  precheck:
    - name: "check-if-subscription-in-tenant"
      cache: "subscription"        # Query once per subscription; results cached across threads
      script: |
        az account show --subscription "$SUBSCRIPTION" > /dev/null 2>&1
        echo $?
      result:
        0:
          log: "Subscription verified in tenant"
          action: "continue"
        default:
          log: "Subscription not found in tenant - skipping VM"
          action: "end_check"

    - name: "check-if-vm-is-running"
      script: |
        az vm get-instance-view -g "$RESOURCE_GROUP" -n "$VM_NAME" \
          --query "instanceView.statuses[?starts_with(code, 'PowerState/')].code" -o tsv
      result:
        "PowerState/running":
          log: "VM is running"
          action: "continue"
        "PowerState/deallocated":
          log: "VM is deallocated"
          action: "skip"
```

#### Precheck Actions
- `action: "continue"`: Check passed; proceed to the next precheck or remote dispatch.
- `action: "skip"`:
  - If root `actions: "skip"`: Stops processing this VM; non-passthrough columns receive `SKIPPED:<precheck_name>`.
  - If root `actions: "start"`: Automatically executes `az vm start`, waits, and re-evaluates this precheck once.
- `action: "end_check"`: Stops processing this VM immediately; non-passthrough columns receive `ENDED:<precheck_name>`.
- `default`: Fallback handler when script stdout does not match any listed key.

### 4.3 RunCommand Steps (Remote Linux VM Execution)
All steps under `runcommand.steps` are combined into a **single remote bash script**:
```yaml
invoke:
  runcommand:
    res_file: "/var/tmp/runcommand/result.res"
    log_file: "/var/tmp/runcommand/result.log"
    steps:
      - name: "check-hip-version"
        timeout: 120
        script: |
          VERSION=$(rpm -q --queryformat '%{VERSION}' hip 2>/dev/null || echo "NOT_INSTALLED")
          write_result HIP_VERSION "$VERSION"

      - name: "check-kernel-count"
        timeout: 120
        script: |
          COUNT=$(dnf repoquery --installed kernel 2>/dev/null | wc -l)
          write_result KERNEL_COUNT "$COUNT"

      - name: "custom-audit-script"
        timeout: 300
        script_path: "./scripts/audit.sh"
```

#### The `write_result NAME VALUE` Helper Contract
- Steps explicitly record results using `write_result NAME VALUE`.
- `NAME` must match `^[A-Z_][A-Z0-9_]*$` and cannot start with `__Y2B_`.
- Values are base64-encoded into `$RES_FILE` to avoid escaping issues with quotes, spaces, or newlines.
- Human-readable logs are simultaneously written to `$LOG_FILE`.
- Each step runs in an isolated `bash -c` subshell with timeout protection (`ok`, `rc=<N>`, or `timeout`).

### 4.4 Collect & Transform (Report Generation)
The `collect` section runs on the host runner to construct the final CSV report:
```yaml
collect:
  - column: "vm_name"
    source: "input.vm_name"

  - column: "hip_version"
    var: "HIP_VERSION"

  - column: "recommendation"
    script: |
      if [ "$HIP_VERSION" = "NOT_INSTALLED" ]; then
        echo "Install HIP"
      else
        echo "Compliant"
      fi

  - column: "extended_compliance"
    script_path: "./scripts/calc_compliance.sh"
```

- **Passthrough columns** (`source: input.<col>`): Copied directly from the input inventory CSV.
- **Remote variable columns** (`var: <NAME>`): Populated from remote `write_result` calls:
  - If written, the value is used (written values win even if the step later failed).
  - If missing and the step failed, the column receives `__ERROR__ <status>` (e.g. `__ERROR__ rc=1`).
  - Otherwise, the column receives `__MISSING__`.
- **Computed columns** (`script:` or `script_path:`): Executed locally on the host with all previous columns exported as `$COL_<NAME>`.

---

## 5. Step 3: Authoring a Windows Job (`y2p`)

`y2p` compiles declarative YAML jobs into Windows PowerShell 5.1 scripts executed via Azure `RunPowerShellScript`.

### 5.1 Windows Target Environment
- **Target OS**: Windows Server 2016, 2019, 2022, 2025, or Windows 10/11.
- **PowerShell Target**: Windows PowerShell 5.1 (`powershell.exe`).
- **Privilege Context**: Runs under the local `NT AUTHORITY\SYSTEM` account via Azure VM Guest Agent.

### 5.2 YAML Backslash Quoting & Path Rules

> [!WARNING]
> **The YAML Backslash Pitfall**: In YAML double quotes (`"..."`), the backslash `\` is an escape character. Writing `"C:\temp\res.res"` turns `\t` into a literal TAB character! Writing `"C:\ProgramData\..."` causes an invalid YAML escape error (`\P`).
>
> **Always use single quotes (`'...'`) for Windows paths in YAML**, or use forward slashes:
> ```yaml
> # RECOMMENDED:
> res_file: 'C:\ProgramData\y2p\result.res'
> log_file: 'C:\ProgramData\y2p\result.log'
>
> # ALSO VALID:
> res_file: 'C:/ProgramData/y2p/result.res'
> log_file: 'C:/ProgramData/y2p/result.log'
> ```

- `res_file` and `log_file` must be absolute Windows paths with drive letter (e.g. `C:\...` or `C:/...`).
- Recommended location: `C:\ProgramData\y2p\result.res` (persists across runs and is accessible to SYSTEM).

### 5.3 PowerShell Step Contract & Exit Code Semantics

Each step in `y2p` runs in an isolated `powershell.exe` child process with timeout protection.

#### The `write_result` Helper
In your PowerShell step script, use `write_result` (or `Write-Y2Result`):
```powershell
write_result KEY VALUE
```
- Example:
  ```powershell
  $enabled = (Get-MpPreference).DisableRealtimeMonitoring -eq $false
  write_result DEFENDER_ENABLED $enabled
  ```
- Values are base64-encoded to preserve multiline output, quotes, spaces, and Unicode characters safely.

#### Exit Code Semantics
- **Clean Completion**: If the script runs to completion without `exit`, status is `ok`.
- **Explicit Exit (`exit N`)**: Calling `exit N` explicitly terminates the step with exit code `N` (records status `rc=N`).
- **Terminating Errors (`throw`)**: Any terminating PowerShell exception or `throw` records status `rc=1`.
- **Non-Terminating Errors**: In PowerShell, native commands writing to stderr or non-terminating cmdlet errors do not terminate the script; the step still records `ok` unless you explicitly inspect `$LASTEXITCODE` or configure `$ErrorActionPreference = 'Stop'`.
- **Timeouts**: If the step exceeds `timeout:` seconds, `y2p` terminates the process tree using `taskkill /T /F` and records status `timeout`. The main script resumes and safely finishes writing `__Y2B_DONE__`.

#### External PowerShell Script Files
Steps can reference `.ps1` files:
```yaml
steps:
  - name: "check-tls"
    timeout: 60
    script_path: "./scripts/check_tls.ps1"
```

### 5.4 Host-Side Scripts in Windows Jobs

> [!NOTE]
> Even when managing Windows VMs with `y2p`, **host-side prechecks and computed columns execute on your local runner machine using Bash**.
>
> This allows you to reuse the exact same Azure CLI prechecks (verifying subscription access or checking VM power state) across both Linux and Windows fleets.

### 5.5 Full Windows Example

See `examples/windows-baseline/job.yaml`:
```yaml
input: "./list_vm.csv"
output_log: "./result.log"
output_csv: "./result.csv"
actions: "skip"
parallel: 10

sleep_before_collect: "3m"
collect_retry_interval: "30s"
collect_retries: 3

invoke:
  # Host-side bash prechecks executed on the runner host
  precheck:
    - name: "check-if-subscription-in-tenant"
      cache: "subscription"
      script: |
        az account show --subscription "$SUBSCRIPTION" > /dev/null 2>&1
        echo $?
      result:
        0:
          log: "Subscription verified in tenant"
          action: "continue"
        default:
          log: "Subscription not found in tenant - skipping VM"
          action: "end_check"

    - name: "check-if-vm-is-running"
      script: |
        az vm get-instance-view -g "$RESOURCE_GROUP" -n "$VM_NAME" \
          --query "instanceView.statuses[?starts_with(code, 'PowerState/')].code" -o tsv
      result:
        "PowerState/running":
          log: "VM is running"
          action: "continue"
        "PowerState/deallocated":
          log: "VM is deallocated"
          action: "skip"

  # Remote Windows PowerShell runcommand
  runcommand:
    res_file: 'C:\ProgramData\y2p\result.res'
    log_file: 'C:\ProgramData\y2p\result.log'
    steps:
      - name: "check-defender"
        timeout: 60
        script: |
          $pref = Get-MpComputerStatus
          $realtime = $pref.RealTimeProtectionEnabled
          write_result DEFENDER_ENABLED $realtime
          $days = (New-TimeSpan -Start $pref.AntivirusSignatureLastUpdated -End (Get-Date)).Days
          write_result SIGNATURE_AGE_DAYS $days

      - name: "check-hotfix"
        timeout: 90
        script: |
          $hf = (Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object -First 1).HotFixID
          write_result LAST_HOTFIX ($hf ?? "NONE")

      - name: "check-tls"
        timeout: 60
        script_path: "./scripts/check_tls.ps1"

collect:
  - column: "vm_name"
    source: "input.vm_name"

  - column: "os"
    source: "input.os"

  - column: "defender_enabled"
    var: "DEFENDER_ENABLED"

  - column: "signature_age_days"
    var: "SIGNATURE_AGE_DAYS"

  - column: "last_hotfix"
    var: "LAST_HOTFIX"

  - column: "tls12_enabled"
    var: "TLS12_ENABLED"

  - column: "compliance_status"
    script: |
      if [ "$DEFENDER_ENABLED" = "True" ] && [ "$TLS12_ENABLED" = "1" ]; then
        echo "COMPLIANT"
      else
        echo "NON_COMPLIANT"
      fi
```

---

## 6. Step 4: Host Runner Environment & macOS Best Practices

Both `y2b` and `y2p` are optimized to run reliably on macOS developer laptops as well as Linux CI/CD build agents.

### macOS Host Hardening
1. **Bash 3.2 Compatibility**:
   - macOS ships with `/bin/bash` version 3.2. Host scripts executed by `y2b` / `y2p` use the resolved bash from `y2shared.hostenv`.
   - If `/bin/bash` is used, a non-fatal warning is displayed recommending `brew install bash`.
   - To use Homebrew bash explicitly:
     ```bash
     brew install bash
     export Y2_BASH="/opt/homebrew/bin/bash"
     ```
2. **BSD vs GNU Userland Differences**:
   - Remote VM scripts run on the target OS (GNU/Linux for `y2b`, Windows for `y2p`).
   - Host-side prechecks and computed column scripts run on your local machine. If writing custom host scripts on macOS, avoid GNU-specific options like `sed -i` without a backup suffix, `grep -P`, or `date -d`.
3. **Automatic Open File Limit Raising (`raise_nofile`)**:
   - The macOS default terminal soft file limit is 256. Running batches with high concurrency (`--parallel 50`) can easily exceed this limit.
   - `y2b` and `y2p` automatically raise the process soft limit up to 4096 on startup.
4. **Sleep Prevention via `caffeinate`**:
   - During long runs or prolonged `sleep_before_collect` intervals, a laptop going to sleep would interrupt network sockets and cause Azure token expiration.
   - On macOS, `caffeinate -i -w <pid>` is automatically spawned in the background during `run` to keep the machine awake.
   - *Tip: Keep your laptop plugged into power. Closing the lid while on battery power can still trigger system sleep.*
5. **Zsh Argument Quoting**:
   - In zsh, arguments containing glob wildcards (`?`, `*`, `[0-9]`) must be quoted to prevent zsh pattern expansion:
     ```bash
     # Correct:
     y2b run job.yaml --vm "vm-[01]"
     ```

---

## 7. Step 5: CLI Commands & Workflows

Both `y2b` and `y2p` share identical subcommands and flags.

### 7.1 Validating: `y2b validate` / `y2p validate`
Validates YAML structure, required fields, script paths, and cross-checks the inventory CSV:
```bash
y2b validate examples/hip-compliance/job.yaml
y2p validate examples/windows-baseline/job.yaml
```

### 7.2 Inspecting Compiled Payload: `y2b compile` / `y2p compile`
Compiles and inspects the remote script that will be delivered to the VMs:
```bash
# Print to stdout:
y2b compile examples/hip-compliance/job.yaml
y2p compile examples/windows-baseline/job.yaml

# Save to file:
y2b compile examples/hip-compliance/job.yaml -o remote.sh
y2p compile examples/windows-baseline/job.yaml -o remote.ps1
```

### 7.3 Safe Dry-Run: `--dry-run`
Runs all host-side prechecks and compiles the remote payload, but halts before calling Azure Run-Command:
```bash
y2b run examples/hip-compliance/job.yaml --dry-run
y2p run examples/windows-baseline/job.yaml --dry-run
```

### 7.4 Production Run: `run`
Executes Phase 1 (Invoke) and Phase 2 (Collect) across the inventory:
```bash
# Full execution:
y2b run examples/hip-compliance/job.yaml

# Target specific VMs:
y2b run examples/hip-compliance/job.yaml --vm vm-app-01 --vm vm-app-02

# Limit number of VMs:
y2p run examples/windows-baseline/job.yaml --limit 10

# Adjust worker concurrency:
y2p run examples/windows-baseline/job.yaml --parallel 20

# Bypass interactive prompt:
y2p run examples/windows-baseline/job.yaml -y
```

### 7.5 Resuming Interrupted Runs: `--resume`
If a run is aborted or VMs were still busy (`ERROR:still_running`), resume without re-running finished VMs:
```bash
y2b run examples/hip-compliance/job.yaml --resume
y2p run examples/windows-baseline/job.yaml --resume
```

---

## 8. Understanding Outputs & Logs

### The Final Report (`result.csv`)
Generated in original input CSV order once all workers complete:
```csv
vm_name,resource_group,subscription,os,hip_version,kernel_count,recommendation,extended_compliance
vm-app-01,rg-prod-east,11111111-2222-3333-4444-555555555555,rhel8,22102026,3,Compliant,OK
vm-app-02,rg-prod-east,11111111-2222-3333-4444-555555555555,rhel8,NOT_INSTALLED,1,Install HIP,OK
```

### The Consolidated Log (`result.log`)
Aggregates execution traces in input-CSV order:
```text
######################################################################
# yaml_to_bash Execution Log
# Job: job.yaml (SHA256: 664269df2d84...)
# Merged: 2026-10-29T01:30:00+00:00
# Total VMs: 3 (OK=2, SKIPPED=1, ENDED=0, ERROR=0)
######################################################################

==================================================
VM: vm-app-01 (Row index 1) — Started 2026-10-29T01:29:01+00:00
==================================================
[precheck check-if-subscription-in-tenant] → "0" → continue: Subscription verified in tenant
[precheck check-if-vm-is-running] → "PowerState/running" → continue: VM is running
[dispatch] Executing az vm run-command invoke (attempt 1)...
[dispatch] Run-command finished in 34.2s with 3 variables.
[collect] hip_version = 22102026
[collect] kernel_count = 3
[collect] recommendation = Compliant
[collect] extended_compliance = OK
Final outcome: OK
==================================================
```

### Outcome Markers Reference

When a VM does not finish with `OK`, non-passthrough columns carry an outcome marker:

| Marker | Cause | Action |
|---|---|---|
| `SKIPPED:<precheck>` | Precheck returned `skip` policy | Inspect VM power state or precheck logic |
| `ENDED:<precheck>` | Precheck returned `end_check` | Verify tenant/subscription permissions |
| `ERROR:still_running` | VM was still busy after sleep and collect retries | Parts folder kept; rerun later with `--resume` |
| `ERROR:remote_incomplete` | Remote script started but did not finish writing done marker | Script killed, VM rebooted, or exceeded Azure 90m cap |
| `ERROR:stale_result` | `$RES_FILE` on VM belongs to a different run | Stale result ignored |
| `ERROR:not_started` | `$RES_FILE` is missing or empty; script never executed | Check Azure agent and run-command status |
| `ERROR:throttled` | Persistent ARM rate limiting (429) | Lower `--parallel` concurrency (e.g. from 20 to 10) |
| `ERROR:run_command_busy` | Another Run-Command is already running on VM | Wait a minute and rerun with `--resume` |
| `ERROR:vm_agent_unavailable` | Azure Guest Agent not ready | Check guest agent service status on VM |
| `ERROR:vm_not_found` | VM does not exist in resource group/subscription | Verify `list_vm.csv` accuracy |
| `ERROR:auth` | Authentication expired or unauthorized | Run `az login` and retry with `--resume` |
| `ERROR:run_command_timeout` | Command exceeded `run_timeout` | Check for hung commands |
| `ERROR:result_truncated` | VM stdout exceeded 4,096 bytes buffer | Redirect diagnostic stdout to `$LOG_FILE`, keep `write_result` values concise |
| `ERROR:no_result` | Worker process died before completing row | Resume run with `--resume` |

---

## 9. Operational Best Practices & Troubleshooting

### Handling 4 KB Output Limits
Azure Run-Command returns only the last **4,096 bytes** of command stdout.
- **Do**: Always call `write_result KEY VALUE` for structured data.
- **Do**: Write verbose diagnostic output to `$LOG_FILE` (`echo "details" >> "$LOG_FILE"` in bash or `Add-Content $LogFile "details"` in PowerShell).
- **Don't**: Print large dumps or file contents directly to stdout without redirecting to `$LOG_FILE`.

### Handling ARM Throttling & Concurrency
- For fleets under 100 VMs: `--parallel 15` is typically safe.
- For fleets between 100 and 500+ VMs: Start with `--parallel 10` or `--parallel 20`.
- The engine automatically implements exponential backoff with jitter (15s, 30s, 60s, 120s, 240s) when throttled.

### Authentication & The Circuit Breaker
If Azure CLI credentials expire during a long-running batch, running 500 requests would generate 500 noisy authentication errors. The framework includes an **Auth Circuit Breaker**:
- If 5 consecutive authentication failures occur, the run aborts immediately (exit code `3`).
- Part files are preserved. Once you refresh your credentials via `az login`, resume with `--resume`.
