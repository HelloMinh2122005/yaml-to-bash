# yaml_to_bash (`y2b`) & yaml_to_powershell (`y2p`)

A declarative Azure VM automation framework designed to execute fleet-wide diagnostics, configuration, and compliance checks across hundreds of virtual machines (500+ VMs) via Azure CLI (`az vm run-command invoke`).

The framework provides two specialized frontends backed by a common, robust orchestration engine:
- **`y2b` (`yaml_to_bash`)**: For Azure Linux virtual machines (`RunShellScript` with bash payloads).
- **`y2p` (`yaml_to_powershell`)**: For Azure Windows virtual machines (`RunPowerShellScript` with Windows PowerShell 5.1 payloads).

Both tools run on **macOS (Apple Silicon / Intel with zsh)** and **Linux** hosts.

> 📖 **Looking for the comprehensive user manual?** See [USER_GUIDE.md](USER_GUIDE.md) for an in-depth walkthrough, YAML schema authoring guide, precheck & computed column chaining tutorials, Windows/y2p guide, and troubleshooting tips.

---

## 1. Architecture & Feature Matrix

The shared engine (`y2shared`) handles host-side prechecks, concurrency control, fire-and-forget two-phase scheduling, Azure CLI retry/backoff, and part-file merging. The frontends (`y2b` and `y2p`) supply OS-specific validation, compilation templates, and remote output parsing:

| Feature / Dimension | `y2b` (Linux) | `y2p` (Windows) |
|---|---|---|
| **CLI Command** | `bin/y2b` or `python3 -m y2b` | `bin/y2p` or `python3 -m y2p` |
| **Target Guest OS** | Linux (RHEL, Rocky, Ubuntu, Debian, SLES...) | Windows (Server 2016/2019/2022/2025, Win 10/11) |
| **Azure Command ID** | `RunShellScript` | `RunPowerShellScript` |
| **Remote Script Engine** | POSIX / bash subshell | Windows PowerShell 5.1 |
| **Path Conventions** | `/var/tmp/runcommand/result.res` | `C:\ProgramData\y2p\result.res` |
| **Step Result Function** | `write_result KEY VALUE` | `write_result KEY VALUE` (alias `Write-Y2Result`) |
| **Host Prechecks** | Bash (executed on runner host) | Bash (executed on runner host) |
| **Host Computed Columns** | Bash (executed on runner host) | Bash (executed on runner host) |
| **Output JSON Format** | Standard Azure `[stdout]` / `[stderr]` string | Azure two-element `ComponentStatus/StdOut/succeeded` |
| **Timeout Handling** | Native `timeout <sec> bash -c ...` | `Start-Process` with process tree kill (`taskkill /T /F`) |
| **Example Directory** | `examples/linux/` | `examples/windows/` |
| **Golden Test File** | `tests/golden/hip_compliance_remote.sh` | `tests/golden/windows_baseline_remote.ps1` |

### Key Highlights
- **Fire-and-Forget Two-Phase Execution**: In Phase 1 (Invoke), remote scripts are fired asynchronously with one runCommand POST per VM (`az rest`, which returns on `202 Accepted` without waiting for the script). In Phase 2 (Collect), the host collects results via reader scripts on an event scheduler. Workers are never held blocked waiting for remote scripts to complete.
- **Root Prechecks (`pre_run`) with Phase Filtering**: Host-side checks run at root `pre_run` with `phase: ["invoke"]`, `["collect"]`, or `["invoke", "collect"]`. Catch power drift before reading results; memoize checks per subscription (`cache: subscription`) to prevent redundant queries. (Legacy `invoke.precheck` remains fully supported).
- **Post-Run Hooks & Power Management**: Root `post_run` hooks run per collected VM before power actions, passing `$Y2_OUTCOME`, `$Y2_RUN_ID`, `$Y2_ACTIONS`, and `$Y2_PREV_POWER_STATE`. Power management (`actions: skip | start | start-stop | stop`) restores original VM states (`start-stop`) or deallocates target VMs (`stop`), backed by atomic `.power` state files and interactive safety confirmation banners.
- **Zero-Corruption Concurrency (Part Files & Merge)**: Workers write exclusively to per-VM part files (`NNNN-<vm_name>.csv` / `.log` / `.power`). A single-threaded merge step builds `result.csv` and `result.log` in original input order after all workers complete.
- **Resilient & Resumable**: Safely resume interrupted runs (`--resume`) without repeating work on finished VMs. Built-in auth circuit breaker, exponential backoff with jitter on Azure throttling, and conflict retries.
- **Safe Evaluation**: Remote values are stored base64-encoded to prevent shell injection, and computed columns run isolated on the host with exported environment variables.

---

## 2. Requirements & Installation

- **Runner Host**: macOS (zsh, Python ≥ 3.9) or Linux host.
- **Python**: Python ≥ 3.9 and PyYAML (`pip install pyyaml`).
- **Azure CLI**: `az` CLI installed and authenticated on the runner (`az login` or Managed Identity). Any version works: Phase 1 does not use `run-command invoke --no-wait`, which silently drops the request before azure-cli 2.85.0.
- **Target VMs**:
  - Linux VMs with Azure Linux Agent for `y2b`.
  - Windows VMs with Azure Windows Guest Agent for `y2p`.

### Quick Setup & Virtual Environment

You can automatically create the virtual environment, install dependencies (`PyYAML`, `pytest`), and activate `y2b` and `y2p` in one command:

```bash
# Clone or navigate to repository
cd yaml_to_bash

# Create, install requirements & activate .venv in your current shell:
source setup_venv.sh

# Both tools are now ready on your PATH!
y2b --help
y2p --help
```

*(Alternatively: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && chmod +x bin/y2b bin/y2p`).*

---

## 3. Host Environment & macOS Considerations

Both `y2b` and `y2p` include built-in host environment hardening when executed on macOS (Darwin) runners:

1. **Host Bash Resolution**: macOS ships with `/bin/bash` 3.2. Host prechecks and computed columns execute using bash resolved via `y2shared.hostenv`:
   - If `$Y2_BASH` is set, that binary is used.
   - Otherwise, the first `bash` on `PATH` is used.
   - If the resolved bash version is < 4.0, a friendly warning is emitted recommending `brew install bash`. Host scripts in `examples/` are 3.2-compatible out of the box.
2. **File Descriptor Limits**: macOS defaults to `ulimit -n 256`, which can exhaust descriptors during high concurrency. At run start, the soft open-file limit is automatically raised up to 4096 (or the system hard limit) via `raise_nofile()`.
3. **Sleep Prevention**: Long runs during `sleep_before_collect` or collection could stall if the laptop sleeps. On macOS, `caffeinate -i -w <pid>` is automatically started for the duration of the run. (Note: Keep laptops connected to AC power, as closing the lid on battery will still sleep).
4. **Zsh Glob Quoting**: In zsh, unquoted glob patterns like `--vm vm-[01]` will fail with `zsh: no matches found`. Always quote arguments containing glob characters: `--vm "vm-[01]"`.
5. **Recommended Concurrency**: For macOS runner workstations with 16 GB RAM, `--parallel 20` to `30` is recommended.

---

## 4. CLI Usage

Both `y2b` and `y2p` share identical subcommands and flags.

### Validate Configuration & Input CSV
```bash
# Linux job validation
y2b validate examples/linux/job.yaml

# Windows job validation
y2p validate examples/windows/job.yaml
```
Checks YAML schema integrity, validates script paths and syntax, inspects the CSV header and VM rows (verifies correct OS family, prevents cross-tool mismatch).

### Compile Remote Payload
```bash
# Preview the remote payload
y2b compile examples/linux/job.yaml
y2p compile examples/windows/job.yaml

# Save to file
y2b compile examples/linux/job.yaml -o remote.sh
y2p compile examples/windows/job.yaml -o remote.ps1
```

### Execute Run
```bash
# Full execution across all target VMs
y2b run examples/linux/job.yaml
y2p run examples/windows/job.yaml

# Filter by VM name and concurrency
y2b run examples/linux/job.yaml --vm lin01 --vm lin02 --parallel 20 --yes

# Limit to first N VMs
y2p run examples/windows/job.yaml --limit 10

# Dry run (run prechecks, compile payload, skip run-command)
y2b run examples/linux/job.yaml --dry-run
y2p run examples/windows/job.yaml --dry-run

# Resume an interrupted run
y2b run examples/linux/job.yaml --resume
y2p run examples/windows/job.yaml --resume

# Keep per-VM part files for debugging
y2b run examples/linux/job.yaml --keep-parts
```

### Exit Codes
- `0`: All VMs completed successfully (`OK`), post hooks passed, power actions completed.
- `1`: Configuration, YAML schema, or input CSV validation error.
- `2`: Run finished, but some VMs had non-OK outcomes (`SKIPPED`, `ENDED`, or `ERROR`), post hooks / power actions failed, or VMs were left held/running remotely.
- `3`: Run aborted (Ctrl-C, 5 consecutive authentication failures, or merge error).

---

## 5. Outcome Markers & Error Classification

| Marker / Pattern | Meaning | Handling |
|------------------|---------|----------|
| `OK` | Prechecks, dispatch, and collection succeeded | Normal report row |
| `SKIPPED:<precheck>` | VM stopped by precheck with action `skip` | Passthrough columns kept, other columns marked `SKIPPED:...` |
| `ENDED:<precheck>` | VM stopped by precheck with action `end_check` | Passthrough columns kept, other columns marked `ENDED:...` |
| `ERROR:not_running_at_collect:<precheck>` | VM stopped or deallocated before collect read under `skip`/`stop` policy | Immediate error recorded; prevents futile reader retries |
| `ERROR:still_running` | The VM was still busy after the sleep and every collect retry | Retried up to `collect_retries` times; parts folder preserved, can be read later with `--resume` |
| `ERROR:remote_incomplete` | The script started but did not write the done marker | Script killed, VM rebooted, or exceeded Azure's 90-minute cap |
| `ERROR:stale_result` | `$RES_FILE` belongs to another run | Logged, no retry |
| `ERROR:not_started` | `$RES_FILE` is missing or empty: the fired script never ran | Logged, no retry |
| `ERROR:throttled` | Azure ARM rate limit (429 / TooManyRequests) | Exponential backoff with jitter (15s, 30s, 60s, 120s, 240s) before marking error |
| `ERROR:run_command_busy` | Conflicting run-command already active on VM | Retried up to 3 times with 60s wait |
| `ERROR:vm_agent_unavailable` | Guest agent unresponsive | Logged, no retry |
| `ERROR:vm_not_found` | VM does not exist in resource group/subscription | Logged, no retry |
| `ERROR:auth` | Authentication failure (`az login` required) | Circuit breaker aborts entire run after 5 consecutive auth errors |
| `ERROR:result_truncated` | Output exceeded 4 KB buffer limit | Ensure step output goes to `$LOG_FILE`, keep `write_result` values concise |
| `ERROR:no_result` | Worker did not produce a finished part file | Synthesized by merger to guarantee row count equals input |

---

## 6. Testing

The framework includes a comprehensive test suite (100+ tests) covering:
- Shared engine components (scheduler, dispatcher, collector, merger, collect-list).
- Host environment utilities (bash resolution, fd limit raising, caffeinate lifecycle).
- Input CSV OS validation (Linux rows for `y2b`, Windows rows for `y2p`).
- Golden file validation for both `y2b` (`tests/golden/hip_compliance_remote.sh`) and `y2p` (`tests/golden/windows_baseline_remote.ps1`).
- Real container payloads:
  - Linux container test (`rockylinux:9` via Podman/Docker).
  - Windows PowerShell test (`mcr.microsoft.com/powershell:7.4-ubuntu-22.04` via Podman/Docker).
- Azure CLI error dispatching, retry logic, and auth circuit breaker.
- Full 20-VM integration runs with mock `az`.

Run the full test suite:
```bash
python3 -m pytest tests/ -v
```
