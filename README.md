# yaml_to_bash (`y2b`)

A declarative Azure VM automation framework designed to execute fleet-wide diagnostics, configuration, and compliance checks across hundreds of virtual machines (500+ VMs) via Azure CLI (`az vm run-command invoke`).

> 📖 **Looking for the comprehensive user manual?** See [USER_GUIDE.md](USER_GUIDE.md) for an in-depth walkthrough, YAML schema authoring guide, precheck & computed column chaining tutorials, and troubleshooting tips.

---

## 1. Key Highlights

- **Single Run-Command Session per VM**: All remote steps are compiled into a single composite bash script, executed in a single `az vm run-command invoke` session per VM. Eliminates ARM rate limiting and avoids massive multi-minute API delays.
- **Host-Side Prechecks with Subscription Caching**: Verify tenant subscriptions or VM power states before running commands; memoize checks per subscription (`cache: subscription`) to prevent redundant queries.
- **Zero-Corruption Concurrency (Part Files & Merge)**: Workers write exclusively to per-VM part files (`NNNN-<vm_name>.csv` / `.log`). A single-threaded merge step builds `result.csv` and `result.log` in original input order after all workers complete.
- **Resilient & Resumable**: Safely resume interrupted runs (`--resume`) without repeating work on finished VMs. Built-in auth circuit breaker, exponential backoff with jitter on Azure throttling, and conflict retries.
- **Safe Evaluation**: Remote values are stored base64-encoded to prevent shell injection, and computed columns run isolated on the host with exported environment variables.

---

## 2. Requirements & Installation

- **Runner**: Linux host with **Python ≥ 3.9** and **PyYAML** (`pip install pyyaml`).
- **Azure CLI**: `az` CLI installed and authenticated on the runner (`az login` or Managed Identity).
- **Target VMs**: Linux VMs (`RunShellScript`).

### Quick Setup & Virtual Environment

You can automatically create the virtual environment, install dependencies (`PyYAML`, `pytest`), and activate `y2b` in one command:

```bash
# Clone or navigate to repository
cd yaml_to_bash

# Create, install requirements & activate .venv in your current shell:
source setup_venv.sh

# y2b is now ready in your PATH!
y2b --help
```

*(Alternatively, manual setup: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && chmod +x bin/y2b`).*

---

## 3. CLI Usage

### Validate Configuration & Input CSV
```bash
y2b validate examples/hip-compliance/job.yaml
```
Checks YAML schema integrity, validates script paths, inspects the CSV header and VM rows (duplicate check, Linux OS verification).

### Compile Remote Payload
```bash
# Inspect the composite script that VMs will receive
y2b compile examples/hip-compliance/job.yaml

# Save to file
y2b compile examples/hip-compliance/job.yaml -o remote.sh
```

### Execute Run
```bash
# Full execution across all VMs
y2b run examples/hip-compliance/job.yaml

# Filter by VM name and concurrency
y2b run examples/hip-compliance/job.yaml --vm vm-app-01 --vm vm-app-02 --parallel 20 --yes

# Limit to first N VMs
y2b run examples/hip-compliance/job.yaml --limit 10

# Dry run (run prechecks, compile payload, skip run-command)
y2b run examples/hip-compliance/job.yaml --dry-run

# Resume an interrupted run
y2b run examples/hip-compliance/job.yaml --resume

# Keep per-VM part files for debugging
y2b run examples/hip-compliance/job.yaml --keep-parts
```

### Exit Codes
- `0`: All VMs completed successfully (`OK`).
- `1`: Configuration, YAML schema, or input CSV validation error.
- `2`: Run finished, but some VMs had non-OK outcomes (`SKIPPED`, `ENDED`, or `ERROR`).
- `3`: Run aborted (Ctrl-C, 5 consecutive authentication failures, or merge error).

---

## 4. Writing Jobs

A job definition specifies three sections:

```yaml
input: "./list_vm.csv"             # Target VMs list
output_log: "./result.log"         # Consolidated log file
output_csv: "./result.csv"         # Final report CSV
actions: "skip"                    # Power state policy: "skip" | "start"
parallel: 10                       # Concurrent VM workers
sleep_before_collect: 0            # Optional delay before collection
run_timeout: 1800                  # Per-VM run-command timeout in seconds

invoke:
  precheck:
    - name: "check-sub"
      cache: "subscription"        # Query once per subscription
      script: |
        az account show --subscription "$SUBSCRIPTION" > /dev/null 2>&1
        echo $?
      result:
        0:
          log: "Valid subscription"
          action: "continue"
        default:
          log: "Invalid subscription"
          action: "end_check"

  runcommand:
    res_file: "/var/tmp/runcommand/result.res"
    log_file: "/var/tmp/runcommand/result.log"
    steps:
      - name: "check-hip-version"
        var: "HIP_VERSION"
        script: |
          VERSION=$(rpm -q --queryformat '%{VERSION}' hip 2>/dev/null || echo "NOT_INSTALLED")
          write_result "$VERSION"

      - name: "check-kernel-count"
        var: "KERNEL_COUNT"
        timeout: 120
        script: |
          COUNT=$(dnf repoquery --installed kernel 2>/dev/null | wc -l)
          write_result "$COUNT"

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

### Remote Scripts Contract
- Each step uses `write_result "<value>"`.
- Helper function records base64-encoded values in `$RES_FILE` and human-readable text in `$LOG_FILE`.
- The remote script truncates `$RES_FILE` at start (`: > "$RES_FILE"`).
- Each step runs in an isolated `bash -c` subshell with timeout protection; if a step crashes or times out, it exports `__ERROR__ rc=<exit_code>` or `__ERROR__ timeout` without aborting subsequent steps.

### Host Collection Contract
- Evaluated in `collect` order.
- Each evaluated column is exported to subsequent computed columns as `COL_<COLUMN_UPPER>` (e.g. `$COL_RECOMMENDATION`).

---

## 5. Outcome Markers & Error Classification

| Marker / Pattern | Meaning | Handling |
|------------------|---------|----------|
| `OK` | Prechecks, dispatch, and collection succeeded | Normal report row |
| `SKIPPED:<precheck>` | VM stopped by precheck with action `skip` | Passthrough columns kept, other columns marked `SKIPPED:...` |
| `ENDED:<precheck>` | VM stopped by precheck with action `end_check` | Passthrough columns kept, other columns marked `ENDED:...` |
| `ERROR:throttled` | Azure ARM rate limit (429 / TooManyRequests) | Exponential backoff with jitter (15s, 30s, 60s, 120s, 240s) before marking error |
| `ERROR:run_command_busy` | Conflicting run-command already active on VM | Retried up to 3 times with 60s wait |
| `ERROR:vm_agent_unavailable` | Guest agent unresponsive | Logged, no retry |
| `ERROR:vm_not_found` | VM does not exist in resource group/subscription | Logged, no retry |
| `ERROR:auth` | Authentication failure (`az login` required) | Circuit breaker aborts entire run after 5 consecutive auth errors |
| `ERROR:result_truncated` | Output exceeded 4 KB buffer limit | Ensure step output goes to `$LOG_FILE`, keep `write_result` values concise |
| `ERROR:no_result` | Worker did not produce a finished part file | Synthesized by merger to guarantee row count equals input |

---

## 6. Testing

The framework includes a test suite covering:
- YAML schema and input CSV validation rules
- Compiler payload generation and golden file matching
- Real Linux container testing (`rockylinux:9` via Podman/Docker)
- Precheck caching and action policy execution
- Azure CLI error dispatching and retries using a fake `az` CLI
- Concurrency test (500 VMs at parallel 50 matching parallel 1)
- Resuming interrupted runs

Run the tests:
```bash
python3 -m pytest tests/ -v
```

