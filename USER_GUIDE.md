# `yaml_to_bash` (`y2b`) — Comprehensive User Guide & Manual

This guide is a complete, step-by-step reference for operators, systems engineers, and developers using `yaml_to_bash` to automate fleet-wide checks, diagnostics, and remediation across Azure Virtual Machines.

---

## Table of Contents

1. [Architecture & How It Works](#1-architecture--how-it-works)
2. [Installation & Prerequisites](#2-installation--prerequisites)
3. [Step 1: Preparing Your Inventory (`list_vm.csv`)](#3-step-1-preparing-your-inventory-list_vmcsv)
4. [Step 2: Authoring a Job Specification (`job.yaml`)](#4-step-2-authoring-a-job-specification-jobyaml)
   - [4.1 Root Configuration](#41-root-configuration)
   - [4.2 Prechecks (Host-side Pre-flight Checks)](#42-prechecks-host-side-pre-flight-checks)
   - [4.3 RunCommand Steps (Remote VM Execution)](#43-runcommand-steps-remote-vm-execution)
   - [4.4 Collect & Transform (Report Generation)](#44-collect--transform-report-generation)
5. [Step 3: CLI Commands & Workflows](#5-step-3-cli-commands--workflows)
   - [5.1 Validating: `y2b validate`](#51-validating-y2b-validate)
   - [5.2 Inspecting Compiled Payload: `y2b compile`](#52-inspecting-compiled-payload-y2b-compile)
   - [5.3 Safe Dry-Run: `y2b run --dry-run`](#53-safe-dry-run-y2b-run---dry-run)
   - [5.4 Production Run: `y2b run`](#54-production-run-y2b-run)
   - [5.5 Resuming Interrupted Runs: `y2b run --resume`](#55-resuming-interrupted-runs-y2b-run---resume)
6. [Understanding Outputs & Logs](#6-understanding-outputs--logs)
   - [The Final Report (`result.csv`)](#the-final-report-resultcsv)
   - [The Consolidated Log (`result.log`)](#the-consolidated-log-resultlog)
   - [Outcome Markers Reference](#outcome-markers-reference)
7. [Operational Best Practices & Troubleshooting](#7-operational-best-practices--troubleshooting)
   - [Handling 4 KB Output Limits](#handling-4-kb-output-limits)
   - [Handling ARM Throttling & Concurrency](#handling-arm-throttling--concurrency)
   - [Authentication & The Circuit Breaker](#authentication--the-circuit-breaker)

---

## 1. Architecture & How It Works

Traditional automation runs a separate SSH command or Azure Run-Command per check. On Azure, each `az vm run-command invoke` takes 30–90 seconds to initiate. Running 5 checks across 500 VMs with the naive approach would trigger 2,500 run-command sessions, taking hours and exceeding Azure Resource Manager (ARM) rate limits.

`yaml_to_bash` solves this by:
1. **Host Prechecks**: Evaluating tenant/subscription permissions and power states locally on your machine before touching the VM.
2. **Single-Session Compilation**: Bundling all remote checks into **one single composite bash script**, sent in **one single `az vm run-command invoke` call** per VM.
3. **Isolated Part Files**: Running concurrent workers where each worker writes strictly to its own VM part files (`NNNN-<vm_name>.csv` and `.log`).
4. **Deterministic Merge**: Sorting and merging all results in original input order into the final CSV report and log after all workers finish.

```
┌────────────────────────────────────────────────────────┐
│ HOST MACHINE / RUNNER                                  │
│                                                        │
│ list_vm.csv ──► [ Precheck ] ──► [ Compile Payload ]   │
│                   (Host)           (1 script)          │
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼ (Single az vm run-command per VM)
┌────────────────────────────────────────────────────────┐
│ REMOTE LINUX VM                                        │
│                                                        │
│ Executes steps 1..N in isolated subshells              │
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
1. **Linux Runner**: Ubuntu, Debian, RHEL, Rocky, Fedora, CentOS, or macOS.
2. **Python**: Python ≥ 3.9 installed.
3. **PyYAML**: Install via your system package manager or pip:
   ```bash
   pip3 install pyyaml
   ```
4. **Azure CLI (`az`)**: Installed and authenticated:
   ```bash
   az login
   ```
   *(Or running under an Azure Managed Identity on a VM runner).*

### Automated Virtual Environment Setup

The repository provides `setup_venv.sh` and `requirements.txt` to automatically initialize the Python virtual environment, install dependencies (`PyYAML`, `pytest`), and put `y2b` into your shell's `PATH`:

```bash
cd yaml_to_bash

# Create venv, install requirements and activate in current shell:
source setup_venv.sh

# Verify CLI is ready:
y2b --help
```

*(If executed as `./setup_venv.sh`, it prepares the `.venv` and prompts you to activate it with `source .venv/bin/activate` or `source setup_venv.sh`).*

---

## 3. Step 1: Preparing Your Inventory (`list_vm.csv`)

The input CSV lists target VMs. It must contain the four required headers: `vm_name`, `resource_group`, `subscription`, and `os`. Any extra columns are automatically preserved and made available to your scripts.

### Format Example
`list_vm.csv`:
```csv
vm_name,resource_group,subscription,os,environment,app_tier
vm-app-01,rg-prod-eastus,00000000-0000-0000-0000-000000000001,rhel8,production,frontend
vm-app-02,rg-prod-eastus,00000000-0000-0000-0000-000000000001,rhel8,production,frontend
vm-db-01,rg-prod-eastus,00000000-0000-0000-0000-000000000001,rocky9,production,database
```

### Rules & Validation
- **UTF-8 with BOM Support**: Files exported from Microsoft Excel (`utf-8-sig`) are supported natively.
- **Linux VMs Only (v1)**: The `os` column must indicate a Linux OS (`rhel`, `centos`, `ubuntu`, `debian`, `rocky`, `alma`, etc.). Windows entries are caught and rejected during validation.
- **Duplicate Prevention**: No duplicate combinations of `(subscription, resource_group, vm_name)` are permitted.
- **Environment Export**: Every CSV header is exported as an uppercase environment variable (e.g. `APP_TIER=frontend`, `RESOURCE_GROUP=rg-prod-eastus`).

---

## 4. Step 2: Authoring a Job Specification (`job.yaml`)

A job YAML is composed of:
1. Root settings
2. `invoke.precheck` (Host prechecks)
3. `invoke.runcommand` (Remote steps)
4. `collect` (Column mapping & host transformations)

### 4.1 Root Configuration

```yaml
# Relative paths are automatically resolved relative to the YAML file's directory
input: "./list_vm.csv"             # Target inventory CSV
output_csv: "./result.csv"         # Final merged CSV report
output_log: "./result.log"         # Consolidated execution trace log

# Power state policy if precheck detects VM is stopped/deallocated:
# "skip"  -> Stop VM processing with SKIPPED:<precheck>
# "start" -> Run 'az vm start', re-run precheck once to confirm 'PowerState/running', proceed
actions: "skip"

# Concurrency: Number of concurrent VM workers (1 to 100)
parallel: 10

# Collect timing (fire and forget)
sleep_before_collect: "5m"         # REQUIRED. Delay between a VM's invoke and first collect attempt
collect_retry_interval: "30s"      # Optional (default "30s"). Wait before re-reading still-busy VM
collect_retries: 3                 # Optional (default 3). Re-reads after first attempt
```

---

### 4.2 Prechecks (Host-side Pre-flight Checks)

Prechecks execute **on your runner machine** before invoking the remote VM. They are executed in top-to-bottom sequence.

```yaml
invoke:
  precheck:
    # Example 1: Verify subscription access with caching
    - name: "check-if-subscription-in-tenant"
      cache: "subscription"        # Evaluated ONCE per distinct subscription, then cached
      timeout: 60                  # Timeout in seconds (default 120)
      script: |
        az account show --subscription "$SUBSCRIPTION" > /dev/null 2>&1
        echo $?
      result:
        0:
          log: "Subscription verified in tenant"
          action: "continue"       # Proceed to next check
        1:
          log: "Subscription not found - skipping"
          action: "end_check"      # Terminate VM processing with ENDED:<name>
        default:
          log: "Unexpected output from account show"
          action: "end_check"

    # Example 2: Check VM power state
    - name: "check-if-vm-is-running"
      timeout: 120
      script: |
        az vm get-instance-view -g "$RESOURCE_GROUP" -n "$VM_NAME" \
          --query "instanceView.statuses[?starts_with(code, 'PowerState/')].code" -o tsv
      result:
        "PowerState/running":
          log: "VM is running"
          action: "continue"
        "PowerState/deallocated":
          log: "VM is deallocated"
          action: "skip"            # Triggers root 'actions' policy
```

#### Precheck Actions
- `action: "continue"`: Check passed; proceed to the next precheck or remote dispatch.
- `action: "skip"`:
  - If root `actions: "skip"`: Stops processing this VM; non-passthrough columns receive `SKIPPED:<precheck_name>`.
  - If root `actions: "start"`: Automatically executes `az vm start`, waits, and re-evaluates this precheck once. If running, continues; otherwise logs `ERROR:start_failed:<precheck_name>`.
- `action: "end_check"`: Stops processing this VM immediately; non-passthrough columns receive `ENDED:<precheck_name>`.
- `default`: Fallback handler when the script's stdout does not match any listed key.

---

### 4.3 RunCommand Steps (Remote VM Execution)

All steps under `runcommand.steps` are combined into a **single remote bash script**.

```yaml
invoke:
  runcommand:
    res_file: "/var/tmp/runcommand/result.res"
    log_file: "/var/tmp/runcommand/result.log"
    steps:
      # Step 1: Inline script querying a package version
      - name: "check-hip-version"
        timeout: 120               # Step timeout (seconds)
        script: |
          VERSION=$(rpm -q --queryformat '%{VERSION}' hip 2>/dev/null || echo "NOT_INSTALLED")
          write_result HIP_VERSION "$VERSION"

      # Step 2: Inline script counting installed kernels
      - name: "check-kernel-count"
        timeout: 120
        script: |
          COUNT=$(dnf repoquery --installed kernel 2>/dev/null | wc -l)
          write_result KERNEL_COUNT "$COUNT"

      # Step 3: External script file
      - name: "complex-hardware-audit"
        timeout: 300
        script_path: "./scripts/audit_hardware.sh"
```

#### The `write_result NAME VALUE` Helper Contract
Steps no longer declare `var:`. The step script explicitly names its result variables using:
```bash
write_result NAME VALUE
```
- A single step can call `write_result` multiple times to record multiple values.
- `NAME` must match `^[A-Z_][A-Z0-9_]*$` and cannot begin with `__Y2B_` (reserved).
- Values are base64-encoded into `$RES_FILE` to guarantee that characters like `"`, `$`, `` ` ``, and newlines cannot corrupt parsing.
- Human-readable logs are simultaneously appended to `$LOG_FILE`.
- Each step runs in an isolated `bash -c` subshell with timeout protection. Subsequent steps **continue to run**, and each step's completion status is recorded in `$RES_FILE` (`ok`, `rc=<exit_code>`, or `timeout`).
- When a collect column `var: X` resolves:
  1. If `X` was written, its value is used (written values always win, even if the step later failed).
  2. If `X` was not written and the step that statically writes `X` exited non-zero or timed out, the column receives `__ERROR__ <status>` (e.g. `__ERROR__ rc=1` or `__ERROR__ timeout`).
  3. Otherwise, the column receives `__MISSING__`.

---

### 4.4 Collect & Transform (Report Generation)

The `collect` section runs **on the host runner** after the remote run completes. It compiles the final columns for the report CSV.

```yaml
collect:
  # 1. Passthrough Columns (extracted from list_vm.csv)
  - column: "vm_name"
    source: "input.vm_name"

  - column: "resource_group"
    source: "input.resource_group"

  - column: "subscription"
    source: "input.subscription"

  - column: "os"
    source: "input.os"

  # 2. Remote Variable Columns (captured from res_file on VM)
  - column: "hip_version"
    var: "HIP_VERSION"

  - column: "kernel_count"
    var: "KERNEL_COUNT"

  # 3. Computed Columns (evaluated in order on the host)
  - column: "recommendation"
    script: |
      EXPECTED="22102026"
      if [ "$HIP_VERSION" = "NOT_INSTALLED" ]; then
        echo "Install HIP"
      elif [ "$HIP_VERSION" != "$EXPECTED" ]; then
        echo "Update to $EXPECTED"
      else
        echo "Compliant"
      fi

  # 4. Chained Computed Column (uses $COL_RECOMMENDATION from above)
  - column: "extended_compliance"
    script_path: "./scripts/calc_compliance.sh"
```

#### Computed Columns Chaining
Computed columns are evaluated in order. Each computed value is exported to subsequent scripts as `COL_<COLUMN_UPPER>` (for example, the result of column `recommendation` is available as `$COL_RECOMMENDATION`).

---

## 5. Step 3: CLI Commands & Workflows

### 5.1 Validating: `y2b validate`

Always validate your job before launching it across hundreds of VMs:
```bash
y2b validate job.yaml
```

**What it verifies:**
- YAML syntax and dataclass schema.
- All referenced external script files (`script_path`) exist and are readable.
- `input.col` mappings correspond to actual headers in `list_vm.csv`.
- Remote variable names follow bash identifier conventions (`^[A-Z_][A-Z0-9_]*$`).
- No duplicate step names, precheck names, or variable names.
- OS compatibility (warns/rejects Windows entries).

---

### 5.2 Inspecting Compiled Payload: `y2b compile`

To view the exact, unified bash payload that VMs will execute:
```bash
# Print payload to stdout
y2b compile job.yaml

# Save payload to a file
y2b compile job.yaml -o /tmp/payload.sh
```
The compiler automatically runs `bash -n` to ensure there are no bash syntax errors in your inline scripts or external files.

---

### 5.3 Safe Dry-Run: `y2b run --dry-run`

Before making actual Azure API calls, execute a dry run:
```bash
y2b run job.yaml --dry-run
```
- Performs full schema validation and CSV verification.
- Runs all host-side prechecks against the VMs.
- Compiles the remote bash payload and stores it in the `.parts/` folder.
- Skips `az vm run-command invoke`.
- Generates the final CSV with `DRY_RUN:ok` markers for all remote columns.

---

### 5.4 Production Run: `y2b run`

```bash
# Standard run (prompts for confirmation before starting)
y2b run job.yaml

# Run non-interactively with custom concurrency
y2b run job.yaml --parallel 20 --yes

# Target specific VMs
y2b run job.yaml --vm vm-app-01 --vm vm-app-02

# Limit to the first N VMs in the CSV
y2b run job.yaml --limit 25
```

During execution, a live status bar appears on `stderr`:
```
[ 312/500 ] ok 290 · skipped 14 · error 8 · running 10 · eta 4m
```

---

### 5.5 Resuming Interrupted Runs: `y2b run --resume`

If a run is interrupted by network failure or a user pressing `Ctrl-C`:
1. The framework preserves the working parts folder (e.g., `./.result.parts/`).
2. VMs that already finished writing their `.csv` parts are safely preserved.
3. Simply execute:
   ```bash
   y2b run job.yaml --resume
   ```
4. `y2b` verifies that the `job_hash` in `run.json` matches, skips all finished VMs, executes the remaining ones, and merges the final report.

---

## 6. Understanding Outputs & Logs

### The Final Report (`result.csv`)
The output CSV reflects the exact order of columns specified in your `collect` section:
```csv
vm_name,resource_group,subscription,os,hip_version,kernel_count,recommendation,extended_compliance
vm-app-01,rg-prod-eastus,sub-prod-01,rhel8,22102026,3,Compliant,OK
vm-app-02,rg-prod-eastus,sub-prod-01,rhel8,NOT_INSTALLED,2,Install HIP,FAILED
vm-app-03,rg-prod-eastus,sub-prod-01,rhel8,SKIPPED:check-if-vm-is-running,SKIPPED:check-if-vm-is-running,SKIPPED:check-if-vm-is-running,SKIPPED:check-if-vm-is-running
```
> **Guaranteed Row Count**: Every row in `list_vm.csv` is guaranteed to have exactly one corresponding row in `result.csv`, even if the VM was stopped or failed.

---

### The Consolidated Log (`result.log`)
The log aggregates execution traces in input-CSV order:
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

---

### Outcome Markers Reference

When a VM does not finish with `OK`, non-passthrough columns carry an outcome marker:

| Marker | Cause | Action |
|---|---|---|
| `SKIPPED:<precheck>` | Precheck returned `skip` policy | Inspect VM power state or precheck logic |
| `ENDED:<precheck>` | Precheck returned `end_check` | Verify tenant/subscription permissions |
| `ERROR:still_running` | VM was still busy after sleep and collect retries | Parts folder kept; rerun later with `y2b run <job> --resume` |
| `ERROR:remote_incomplete` | Remote script started but did not finish writing done marker | Script killed, VM rebooted, or exceeded Azure 90m cap |
| `ERROR:stale_result` | `$RES_FILE` on VM belongs to a different run | Stale result ignored |
| `ERROR:not_started` | `$RES_FILE` is missing or empty; script never executed | Check Azure agent and run-command status |
| `ERROR:throttled` | Persistent ARM rate limiting (429) | Lower `--parallel` concurrency (e.g. from 20 to 10) |
| `ERROR:run_command_busy` | Another Run-Command is already running on VM | Wait a minute and rerun with `--resume` |
| `ERROR:vm_agent_unavailable` | Azure Linux Guest Agent (`waagent`) not ready | Check guest agent service status on VM |
| `ERROR:vm_not_found` | VM does not exist in resource group/subscription | Verify `list_vm.csv` accuracy |
| `ERROR:auth` | Authentication expired or unauthorized | Run `az login` and retry with `--resume` |
| `ERROR:run_command_timeout` | Command exceeded `run_timeout` | Check for hung commands (e.g. interactive prompts) |
| `ERROR:result_truncated` | VM stdout exceeded 4,096 bytes buffer | Redirect diagnostic stdout to `$LOG_FILE`, keep `write_result` values concise |
| `ERROR:no_result` | Worker process died before completing row | Resume run with `y2b run ... --resume` |

---

## 7. Operational Best Practices & Troubleshooting

### Handling 4 KB Output Limits
Azure Run-Command returns only the last **4,096 bytes** of command stdout.
- **Do**: Always call `write_result "$VAL"` for structured data.
- **Do**: Write verbose diagnostic output to `$LOG_FILE` (`echo "details" >> "$LOG_FILE"`).
- **Don't**: Print large dumps or file contents directly to stdout without redirecting to `$LOG_FILE`.

### Handling ARM Throttling & Concurrency
- For fleets under 100 VMs: `--parallel 15` is typically safe.
- For fleets between 100 and 500+ VMs: Start with `--parallel 10` or `--parallel 20`.
- The engine automatically implements exponential backoff with jitter (15s, 30s, 60s, 120s, 240s) when throttled.

### Authentication & The Circuit Breaker
If Azure CLI credentials expire during a long-running batch, running 500 requests would generate 500 noisy authentication errors. `yaml_to_bash` includes an **Auth Circuit Breaker**:
- If 5 consecutive authentication failures occur, the run aborts immediately (exit code `3`).
- Part files are preserved. Once you refresh your credentials via `az login`, resume with `--resume`.

