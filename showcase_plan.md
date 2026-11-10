# Live Project Showcase Runbook: yaml_to_bash (`y2`)

**Target Audience:** Repository Owner & Technical Evaluators  
**Goal:** A step-by-step live presentation runbook demonstrating every capability, safety guard, error handling mechanism, and cloud integration across `yaml_to_bash`.  
**Host Shell Compatibility:** The owner's interactive shell is `zsh`. All commands below are designed to run from `bash` or within zsh-safe POSIX blocks.

---

## 1. Feature Coverage Matrix

Every feature documented across `README.md`, `USER_GUIDE.md`, `lab/README.md`, and `ado/` is mapped below to an executable demonstration step, or noted with its offline test coverage.

| Domain | Feature / Capability | Demonstration Step | Test Coverage / Reason if Not Demonstrable Live |
|---|---|:---:|---|
| **CLI & Tools** | Linux CLI (`y2b`) | Step 1.1, 2.1 | `tests/test_cli.py`, `tests/test_compiler.py` |
| | Windows CLI (`y2p`) | Step 1.1, 1.3 | `tests/test_y2p_unit.py`, `tests/test_y2p_integration.py` |
| | Input validation (`validate`) | Step 1.1, 1.2 | `tests/test_validate.py`, `tests/test_validate_ascii.py` |
| | Remote compilation (`compile`) | Step 1.3 | `tests/test_compiler.py`, `tests/golden/` |
| | Execution planning (`explain`) | Step 1.4 | `tests/test_explain.py`, `tests/golden/explain_*` |
| | Pipeline export (`export-ado`) | Step 1.5 | `tests/test_export_ado.py` |
| | Pipeline drift detection (`--check`) | Step 1.5 | `tests/test_export_ado.py` |
| **Execution Engine** | Full execution (`--phase all`) | Step 2.1 | `tests/test_phase_split.py` |
| | Two-phase split (`--phase invoke / collect`) | Step 2.2 | `tests/test_phase_split.py` |
| | Dry run mode (`--dry-run`) | Step 2.3 | `tests/test_runner.py` |
| | VM filtering (`--vm`) & limit (`--limit`) | Step 2.4 | `tests/test_runner.py` |
| | Parallel worker scaling (`--parallel`) | Step 2.8 | `tests/test_scale_guard.py` |
| | Part files debugging (`--keep-parts`) | Step 2.6 | `tests/test_parts.py` |
| **Prechecks & Power** | Host-side precheck execution | Step 1.1, 2.1 | `tests/test_precheck.py` |
| | Subscription cache sharing | Step 1.4, 2.1 | `tests/test_precheck.py` |
| | Action `continue` | Step 2.1 | `tests/test_precheck.py` |
| | Action `skip` (`SKIPPED:<precheck>`) | Step 2.5 | `tests/test_precheck.py` |
| | Action `end_check` (`ENDED:<precheck>`) | Step 2.5 | `tests/test_precheck.py` |
| | Action `stop` (orphan power restore) | Step 2.1, 2.2 | `tests/test_m7_pre_post_power.py` |
| | Action `start` / `start-stop` | Step 4.2 | `tests/test_precheck.py`, `tests/t3/` |
| **Failures & Resilience** | Timeout & polling (`ERROR:still_running`) | Step 2.6 | `tests/test_runner.py` |
| | Resuming runs (`--resume`) | Step 2.6 | `tests/test_runner.py`, `tests/test_conformance.py` |
| | Auth circuit breaker (5 auth failures) | Step 2.7 | `tests/test_runner.py`, `tests/test_azcmd.py` |
| | Throttling retry (429 backoff + jitter) | Step 2.8 | `tests/test_azcmd.py` |
| | Run-command busy conflict retry | Matrix Note | `tests/test_azcmd.py` (simulated via `FAKE_AZ_BUSY_COUNT`) |
| | Truncated result buffer handling | Matrix Note | `tests/test_dispatcher.py` (`ERROR:result_truncated`) |
| | Result block missing (`ERROR:no_result_block`) | Matrix Note | `tests/test_dispatcher.py` |
| **ASCII & Hygiene** | Git LF checkout policy (`.gitattributes`) | Step 3.1 | `tests/test_gitattributes.py` |
| | Static non-ASCII scan rejection | Step 1.2 | `tests/test_validate_ascii.py` |
| | Runtime ASCII folding (Vietnamese to Latin) | Step 2.10, 4.2 | `tests/test_asciiguard.py`, `tests/test_ascii_e2e.py` |
| | Replacement logging (`[ascii]`) & summary | Step 2.10, 4.2 | `tests/test_asciiguard.py` |
| **Tracing & CI/CD** | Azure CLI call tracing (`[az]`) | Step 2.1 | `tests/test_az_trace.py` |
| | Secret parameter redaction (`***`) | Step 2.1 | `tests/test_az_trace.py` |
| | Execution plan conformance tracking | Step 2.1, 2.6 | `tests/test_conformance.py` |
| | ADO group logging (`TF_BUILD=True`) | Step 2.9 | `tests/test_az_trace.py` |
| | ADO log injection neutralization (`##vso[`) | Step 2.9 | `tests/test_az_trace.py` |
| **ADO Patch Templates** | Fixed extends templates (`ado/templates/`) | Step 3.1 | `tests/test_ado_templates_yaml.py`, `tests/test_ado_steps.py` |
| | PowerShell 5.1 module (`Y2Az.psm1`) | Step 3.1 | `tests/test_ado_ps51_compat.py`, `tests/test_ado_arm.py` |
| | Verbatim stepList hooks & allowlist | Step 3.1 | `tests/test_ado_steps.py`, `tests/ado_fixtures/` |
| | Real ADO pipeline execution on agent | Step 4.3 | Deferred under Request 1 T-18 (Blocked pending approval) |
| **Lab Infrastructure** | Lab runner (`lab/run-lab.sh`) & safety | Step 4.1, 4.2 | `lab/run-lab.sh`, `tests/test_lab_evidence.py` |
| | Dynamic VM power toggle (`toogle_vm.sh`) | Step 4.1 | `lab/toogle_vm.sh` |
| | Self-hosted agent on `win01` | Step 4.3, 4.4 | `.handoffs/20261108-lab-ado-agent/42-evidence/tp-01..03` |

---

## 2. Environment Setup & Safety Prerequisites

To ensure zero accidental Azure costs and zero pollution of the live fleet during local demonstrations, **Acts 1 and 2 operate on isolated temporary workspaces using the simulated Azure CLI (`tests/fake_az/az`)**.

### Safety Setup Script
Run this setup block once in your shell session to configure the fake CLI and scratch directories:

```bash
# Start a clean bash subshell for the showcase session
bash

# Navigate to repo root
cd /home/phandinhminh/Downloads/y2

# Configure PATH prioritizing the fake Azure CLI and project binaries
export REPO_ROOT="$PWD"
export PATH="$REPO_ROOT/tests/fake_az:$REPO_ROOT/bin:$PATH"

# Create an isolated state directory for fake cloud resources
export FAKE_AZ_STATE_DIR="/tmp/y2_demo_state"
export FAKE_AZ_LOG="/tmp/y2_demo_az.log"
mkdir -p "$FAKE_AZ_STATE_DIR"

# Verify fake az is active (MUST end in tests/fake_az/az)
which az
# Expected: /home/phandinhminh/Downloads/y2/tests/fake_az/az
```

### State Reset Helper
Between test scenarios in Act 2, run this one-line reset command:
```bash
rm -rf "$FAKE_AZ_STATE_DIR"/* "$FAKE_AZ_LOG" /tmp/y2_demo_work && mkdir -p "$FAKE_AZ_STATE_DIR" /tmp/y2_demo_work
```

---

## Act 1: Author and Inspect (Zero Subprocesses, Zero Cloud Calls)

Demonstrates static authoring tools, compiler determinism, plan inspection, and CI/CD drift detection.

### Step 1.1: Job Specification & Input CSV Validation
Validates YAML schema, script references, CSV headers, and OS compatibility.

```bash
bin/y2b validate examples/linux/job.yaml
```
- **What it demonstrates:** Pre-flight validation, cross-OS safety checks, and SHA256 plan fingerprinting.
- **Expected Exit Code:** `0`
- **Key Output Lines:**
  ```text
  Validating job specification: .../examples/linux/job.yaml
  Validating input CSV: .../examples/linux/list_vm.csv
  Validation successful!
    Job: job.yaml (SHA256: 915263da1829...)
    Target VMs: 3 from list_vm.csv
    Pre_run checks: 2 (invoke: 2, collect: 1)
    Runcommand steps: 5
    Collect columns: 12
    Post_run hooks: 1
  ```

---

### Step 1.2: Static Non-ASCII Input Rejection (ASCII Guard)
Proves that `validate`, `compile`, and `run` immediately reject non-ASCII characters in `job.yaml` (including comments), the input CSV, or referenced script files.

```bash
# Create a temporary job containing non-ASCII character (café with é)
mkdir -p /tmp/y2_demo_work
cp -r examples/linux /tmp/y2_demo_work/bad_ascii
echo "# Non-ASCII comment: café" >> /tmp/y2_demo_work/bad_ascii/job.yaml

bin/y2b validate /tmp/y2_demo_work/bad_ascii/job.yaml
echo "Exit Code: $?"
```
- **What it demonstrates:** Strict static input scanning with exact `path:line:col` and unicode point reporting.
- **Expected Exit Code:** `1`
- **Key Output Lines:**
  ```text
  ERROR: /tmp/y2_demo_work/bad_ascii/job.yaml:63:26: non-ASCII character U+00E9
  Exit Code: 1
  ```

---

### Step 1.3: Compile Remote Payloads & Golden Verification
Compiles the multi-step remote agent payload and verifies byte-for-byte fidelity with committed goldens.

```bash
# Linux payload byte-for-byte check
bin/y2b compile examples/linux/job.yaml | diff -u - tests/golden/hip_compliance_remote.sh && echo "LINUX GOLDEN MATCH: 100% BYTE IDENTICAL"

# Windows payload byte-for-byte check
bin/y2p compile examples/windows/job.yaml | diff -u - tests/golden/windows_baseline_remote.ps1 && echo "WINDOWS GOLDEN MATCH: 100% BYTE IDENTICAL"
```
- **What it demonstrates:** Deterministic payload generation without runtime side-effects.
- **Expected Exit Code:** `0`
- **Key Output Lines:**
  ```text
  LINUX GOLDEN MATCH: 100% BYTE IDENTICAL
  WINDOWS GOLDEN MATCH: 100% BYTE IDENTICAL
  ```

---

### Step 1.4: Pure Execution Planning (`explain`)
Inspects the exact execution graph, budget, and API mutations without running any subprocess or making network calls.

```bash
bin/y2b explain examples/linux/job.yaml --format text | head -n 30
```
- **What it demonstrates:** Dry-run planning, API version pinning (`2024-07-01`), subscription cache grouping, and mutation auditing.
- **Expected Exit Code:** `0`
- **Key Output Lines:**
  ```text
  # y2b Azure Execution Plan
  # Tool: y2b (RunShellScript) · API Version: 2024-07-01
  # Job: examples/linux/job.yaml
  # Fleet: 3 VMs · Max run commands: 6 (2 x eligible VMs)
  # Reachable Azure mutations: runCommand POST, vm start, deallocate, powerOff
  # Timing: sleep_before_collect=15.0s, collect_retry_interval=10.0s, collect_retries=3
  ```

---

### Step 1.5: Azure DevOps Pipeline Export & Drift Checking
Tests export of production Azure DevOps pipelines and verifies CI/CD drift detection.

```bash
# 1. Verify clean export matching committed pipelines
bin/y2b export-ado examples/linux/job.yaml -o pipelines/generated/linux \
  --service-connection sc-azure-lab --environment y2-lab --check
echo "Clean Check Exit Code: $?"

# 2. Show drift detection when job specification changes
sed -i.bak 's/sleep_before_collect: "15s"/sleep_before_collect: "20s"/' examples/linux/job.yaml
bin/y2b export-ado examples/linux/job.yaml -o pipelines/generated/linux \
  --service-connection sc-azure-lab --environment y2-lab --check
echo "Drift Check Exit Code: $?"

# Restore unmodified file
mv examples/linux/job.yaml.bak examples/linux/job.yaml
git checkout -- examples/linux/job.yaml
```
- **What it demonstrates:** Standard library string templating and git drift detection (`--check`).
- **Expected Exit Code:** `0` for clean check; `1` for drift detection.
- **Key Output Lines:**
  ```text
  Clean Check Exit Code: 0
  DIFFERS: payload/remote.sh
  DIFFERS: plan/explain.txt
  DIFFERS: plan/explain.md
  Drift Check Exit Code: 1
  ```

---

## Act 2: Run and Fail Safely with the Fake AZ

Demonstrates lifecycle orchestration, two-phase splitting, retry loops, fault isolation, resume semantics, and error handling.

> [!NOTE]
> In Linux fake runs, remote script execution is simulated by the fake CLI returning mocked values. Columns that are not populated by the mocked response appear as `__MISSING__` in the final CSV, which is the expected fallback behavior.

### Step 2.1: Full Normal Run (`--phase all`)
Executes prechecks, starts VMs if configured, submits fire-and-forget run commands, polls for results, merges CSV/logs, and applies power policies.

```bash
# Prepare isolated workspace with fast poll timings (1s)
rm -rf /tmp/y2_demo_work && mkdir -p /tmp/y2_demo_work/run1
cp -r examples/linux/* /tmp/y2_demo_work/run1/
sed -i 's/sleep_before_collect: "15s"/sleep_before_collect: "1s"/' /tmp/y2_demo_work/run1/job.yaml
sed -i 's/collect_retry_interval: "10s"/collect_retry_interval: "1s"/' /tmp/y2_demo_work/run1/job.yaml

cd /tmp/y2_demo_work/run1
y2b run job.yaml -y
```
- **What it demonstrates:** End-to-end execution, automatic soft open-file limit raise (`raise_nofile`), power policy restoration, and plan conformance (`conformance: 3/3 VMs matched plan`).
- **Expected Exit Code:** `0`
- **Produced Files:** `result.csv`, `result.log`
- **Key Output Lines:**
  ```text
  Power policy: pre_run uses action 'stop' - after the job, deallocates up to 3 VMs
  Merging VM part files...
  Run Completed
    Total VMs:   3
    OK:          3
    Post hooks:  ok 3 | failed 0
    Deallocated: 3
  conformance: 3/3 VMs matched plan
  ```

---

### Step 2.2: Two-Phase Split Execution (`--phase invoke` & `--phase collect`)
Splits execution across separate jobs or pipeline stages: Phase 1 fires scripts and exits immediately with `fired=<n>`; Phase 2 collects results and restores power.

```bash
cd /tmp/y2_demo_work && rm -rf run2 && mkdir -p run2 && cp -r /tmp/y2_demo_work/run1/* run2/
cd /tmp/y2_demo_work/run2 && rm -f result.*

# Phase 1: Fire scripts asynchronously
y2b run job.yaml --phase invoke -y
echo "Invoke Exit Code: $?"

# Phase 2: Collect results and finalize
y2b run job.yaml --phase collect -y
echo "Collect Exit Code: $?"
```
- **What it demonstrates:** Asynchronous two-phase handoff; `fired=3` strictly emitted as the last line on stdout in invoke phase.
- **Expected Exit Code:** `0` for invoke; `0` for collect.
- **Key Output Lines:**
  ```text
  [Phase 1: Invoke] [ 0/3 ] ok 0 | running 0 | eta --
  conformance: 3/3 VMs matched plan
  fired=3
  Invoke Exit Code: 0

  Merging VM part files...
  Run Completed
    Total VMs:   3
    OK:          3
    Deallocated: 3
  conformance: 3/3 VMs matched plan
  Collect Exit Code: 0
  ```

---

### Step 2.3: Dry Run Mode (`--dry-run`)
Executes prechecks and compiles payloads, but skips the actual remote `runCommand` POST.

```bash
cd /tmp/y2_demo_work/run1 && rm -f result.*
y2b run job.yaml --dry-run -y
```
- **What it demonstrates:** Safe validation of prechecks and targets without triggering remote execution.
- **Expected Exit Code:** `0`
- **Key Output Lines:**
  ```text
  [dry-run] Prechecks passed; skipping run-command execution.
  Run Completed
    Total VMs:   3
    OK:          3
    Deallocated: 0
  conformance: 3/3 VMs matched plan
  ```

---

### Step 2.4: Target Filtering (`--vm`) and Limits (`--limit`)
Executes only a subset of the target fleet.

```bash
cd /tmp/y2_demo_work/run1 && rm -f result.*
y2b run job.yaml --vm lin01 --limit 1 -y
```
- **What it demonstrates:** Dynamic CLI filtering by VM name and count.
- **Expected Exit Code:** `0`
- **Key Output Lines:**
  ```text
  Target VMs: 1 (from list_vm.csv, filtered by name/limit)
  Run Completed
    Total VMs:   1
    OK:          1
  conformance: 1/1 VMs matched plan
  ```

---

### Step 2.5: Precheck Action Branches (`skip` and `end_check`)
Demonstrates graceful exclusion of individual VMs without job failure.

```bash
# Modify precheck script to return action 'skip' for lin02
cd /tmp/y2_demo_work && rm -rf run_skip && mkdir -p run_skip && cp -r run1/* run_skip/
cd /tmp/y2_demo_work/run_skip

# Set fake state so lin02 triggers a precheck failure
export FAKE_AZ_ACCOUNT_FAIL=1
y2b run job.yaml -y || true
unset FAKE_AZ_ACCOUNT_FAIL
```
- **What it demonstrates:** Automatic recording of `SKIPPED:<precheck>` outcome marker; job exits with code `2` (SucceededWithIssues).
- **Expected Exit Code:** `2`

---

### Step 2.6: Timeout Isolation (`ERROR:still_running`) & Resume (`--resume`)
Simulates a long-running script on `lin02` that exceeds collection retries, preserving part files and resuming cleanly later.

```bash
cd /tmp/y2_demo_work && rm -rf run_resume && mkdir -p run_resume && cp -r run1/* run_resume/
cd /tmp/y2_demo_work/run_resume && rm -f result.*

# Run 1: lin02 is still executing remotely (runtime 9999s)
export FAKE_AZ_RUNTIME_lin02=9999
y2b run job.yaml -y
echo "Run 1 Exit Code: $?"

# Run 2: lin02 finishes (runtime 0s) -> resume collection
export FAKE_AZ_RUNTIME_lin02=0
y2b run job.yaml --resume -y
echo "Run 2 Exit Code: $?"
```
- **What it demonstrates:** Fail-safe timeout (`ERROR:still_running`), preservation of `.result.parts/`, idempotence on resume, and resume conformance (`conformance: 1/1 VMs matched plan`).
- **Expected Exit Code:** `2` on Run 1; `0` on Run 2.
- **Key Output Lines:**
  ```text
  # Run 1:
  Left running: 1 (lin02, rerun with y2b run .../job.yaml --resume)
  [!] Notice: Some VMs were still running remotely when collection timed out.
      Part files preserved in: .../.result.parts
  Run 1 Exit Code: 2

  # Run 2:
  Resuming previous run from parts folder: .../.result.parts
  Run Completed
    Total VMs:   3
    OK:          3
    Deallocated: 1
  conformance: 1/1 VMs matched plan
  Run 2 Exit Code: 0
  ```

---

### Step 2.7: Authentication Circuit Breaker (5 Consecutive Failures)
Simulates expired credentials tripping the circuit breaker after 5 consecutive auth errors.

```bash
cd /tmp/y2_demo_work && rm -rf run_auth && mkdir -p run_auth && cp -r run1/* run_auth/
cd /tmp/y2_demo_work/run_auth && rm -f result.*

# Create 7 target VMs
cat <<'EOF' > list_vm.csv
vm_name,resource_group,subscription,os
lin01,rg-lab-10vms,79f7da80-0c64-4e4b-9ed4-316a88142152,rhel9
lin02,rg-lab-10vms,79f7da80-0c64-4e4b-9ed4-316a88142152,rhel9
lin03,rg-lab-10vms,79f7da80-0c64-4e4b-9ed4-316a88142152,rhel9
lin04,rg-lab-10vms,79f7da80-0c64-4e4b-9ed4-316a88142152,rhel9
lin05,rg-lab-10vms,79f7da80-0c64-4e4b-9ed4-316a88142152,rhel9
lin06,rg-lab-10vms,79f7da80-0c64-4e4b-9ed4-316a88142152,rhel9
lin07,rg-lab-10vms,79f7da80-0c64-4e4b-9ed4-316a88142152,rhel9
EOF

export FAKE_AZ_RC_DEFAULT="auth"
y2b run job.yaml -y
echo "Auth Breaker Exit Code: $?"
unset FAKE_AZ_RC_DEFAULT
```
- **What it demonstrates:** Circuit breaker stops execution before burning Azure rate limits or compute.
- **Expected Exit Code:** `3`
- **Key Output Lines:**
  ```text
  FATAL: Run aborted due to 5 consecutive Azure authentication failures in Phase 1.
  Part files preserved in: .../.result.parts
  Rerun with: y2b run .../job.yaml --resume
  conformance: 7/7 VMs matched plan
  Auth Breaker Exit Code: 3
  ```

---

### Step 2.8: High-Concurrency Scale Simulation (20 VMs)
Simulates concurrent execution across a fleet of 20 VMs under `--parallel 20`.

```bash
cd /tmp/y2_demo_work && rm -rf run_scale && mkdir -p run_scale && cp -r run1/* run_scale/
cd /tmp/y2_demo_work/run_scale && rm -f result.*

# Generate 20-VM inventory
cat <<'EOF' > list_vm.csv
vm_name,resource_group,subscription,os
EOF
for i in $(seq -w 1 20); do
  echo "lin$i,rg-lab-10vms,79f7da80-0c64-4e4b-9ed4-316a88142152,rhel9" >> list_vm.csv
done

y2b run job.yaml --parallel 20 -y
```
- **What it demonstrates:** Threaded worker pool scaling, high concurrency file descriptor safety, and clean aggregation.
- **Expected Exit Code:** `0`
- **Key Output Lines:**
  ```text
  Target VMs: 20 (from list_vm.csv)
  Parallel:   20
  Merging VM part files...
  Run Completed
    Total VMs:   20
    OK:          20
    Deallocated: 20
  conformance: 20/20 VMs matched plan
  ```

---

### Step 2.9: Azure DevOps Group Logging & Log Sanitization (`TF_BUILD=True`)
Simulates running under an Azure DevOps pipeline agent.

```bash
cd /tmp/y2_demo_work/run1 && rm -f result.*
export TF_BUILD="True"
y2b run job.yaml -y
unset TF_BUILD
```
- **What it demonstrates:** Azure DevOps collapsible group emissions (`##[group]lin01 | OK` ... `##[endgroup]`), log injection protection, and warning emissions.
- **Expected Exit Code:** `0`
- **Key Output Lines:**
  ```text
  ##[group]lin01 | OK
  ==================================================
  VM: lin01 (Row index 1) - Started ...
  ...
  ##[endgroup]
  ```

---

### Step 2.10: Runtime ASCII Folding & Sanitization
Runs a job where a remote script outputs Vietnamese characters (`Nguyễn Đà Nẵng`), proving runtime folding into base Latin characters (`Nguyen Da Nang`) and zero non-ASCII in sinks.

```bash
cd /tmp/y2_demo_work && rm -rf run_ascii && mkdir -p run_ascii
cp -r "$REPO_ROOT/tests/fixtures/ascii_e2e"/* run_ascii/
cd run_ascii

export FAKE_AZ_VIETNAMESE=1
y2b run lab_job.yaml -y
unset FAKE_AZ_VIETNAMESE

# Verify result.csv contains folded value
cat result.csv

# Verify 0 non-ASCII bytes in results
grep -P '[^\x00-\x7F]' result.csv result.log || echo "0 NON-ASCII BYTES FOUND (PASS)"
```
- **What it demonstrates:** UTF-8 decomposition, Vietnamese character preservation to base Latin letters, `[ascii]` log tracking, and strict ASCII guarantees across all files.
- **Expected Exit Code:** `0`
- **Key Output Lines:**
  ```text
  vm_name,resource_group,vietnamese_val
  lin01,rg-lab-10vms,Nguyen Da Nang
  0 NON-ASCII BYTES FOUND (PASS)
  ```

---

## Act 3: Test Evidence and Regression Verification

Demonstrates full test suite execution, container payload testing, and static compatibility guards.

### Step 3.1: Execute Full Regression Suite (Tier 1)
Runs the entire unit and integration test suite across all subsystems.

```bash
cd /home/phandinhminh/Downloads/y2
python3 -m pytest tests/ -q
```
- **Observed Counts & Runtime:**
  - **484 passed, 4 skipped, 0 failed** in ~430 seconds.
- **Expected Exit Code:** `0`

---

### Step 3.2: Multi-Platform Container Payloads (Tier 2)
Tests generated bash and PowerShell payloads inside isolated Linux (`rockylinux:9`) and PowerShell Core containers.

```bash
python3 -m pytest tests/test_compiler.py tests/test_y2p_container.py -v
```
- **Observed Result:** 7 passed in ~15 seconds.
- **Expected Exit Code:** `0`

---

### Step 3.3: Python 3.9 AST Compatibility Guard
Validates that only Python 3.9 standard library modules + PyYAML are used throughout `y2shared/`, `y2b/`, and `y2p/`.

```bash
python3 -m pytest tests/test_py39_compat.py -v
```
- **Observed Result:** 5 passed in 0.35 seconds.
- **Expected Exit Code:** `0`

---

## Act 4: Live Azure Fleet Demonstration (Real Cloud Harness — Costs Money)

> [!CAUTION]
> **LIVE AZURE COSTS:** All commands in Act 4 interact with real Azure VMs (`lin01`–`lin03`, `win01`–`win02`) in subscription `79f7da80-0c64-4e4b-9ed4-316a88142152` (`rg-lab-10vms`).
> All executions must strictly run through `lab/run-lab.sh` to enforce fail-safe VM deallocation and budget limits.

### Step 4.1: Fleet Status & Fail-Safe Verification
Checks current power state of the 5 lab VMs without incurring compute costs.

```bash
lab/toogle_vm.sh status
```
- **Expected Output:**
  ```text
  Name    PowerState      PrivateIP
  ------  --------------  -----------
  lin01   VM deallocated  10.0.1.6
  lin02   VM deallocated  10.0.1.7
  lin03   VM deallocated  10.0.1.8
  win01   VM deallocated  10.0.1.5
  win02   VM deallocated  10.0.1.4
  ```

---

### Step 4.2: Real Linux Fleet Run with ASCII Sanitization (TP-18 / TP-19)
Starts `lin01`, executes remote step writing Vietnamese characters via hex escapes, verifies runtime folding, and confirms fail-safe deallocation.

```bash
lab/run-lab.sh --csv tests/fixtures/ascii_e2e/list_vm.csv \
  --evidence .handoffs/20261104-lf-ascii-hygiene/42-evidence/t3/ -- bash -c '
    lab/toogle_vm.sh start lin01 --wait
    cd tests/fixtures/ascii_e2e && ../../../bin/y2b run lab_job.yaml -y
'
```
- **Observed Evidence:**
  - `result.csv`: Contains `lin01,rg-lab-10vms,Nguyen Da Nang`
  - `grep.txt`: `grep_rc=1` (0 non-ASCII bytes)
  - `post-state.txt`: Confirms all 5 VMs are deallocated.
- **Evidence Path:** `.handoffs/20261104-lf-ascii-hygiene/42-evidence/t3/`

---

### Step 4.3: Real Windows Self-Hosted Agent Execution (TP-02)
Starts all 5 VMs, waits for `win01` agent to come online in pool `Production Windows Agent`, queues and executes `smoke.yml` via ADO REST API using `AzurePowerShell@5`, and queries the 5 lab VMs.

```bash
lab/run-lab.sh --evidence .handoffs/20261108-lab-ado-agent/42-evidence/tp-02/ -- \
  lab/ado/session.sh lab/ado/ado-run.sh lab/ado/smoke.yml \
  --evidence-dir .handoffs/20261108-lab-ado-agent/42-evidence/tp-02/
```
- **Observed Evidence:**
  - Pipeline run 5 succeeded (`completed` / `succeeded`).
  - `pipeline.log`: Confirms PowerShell `5.1.20348.5773`, `Az.Accounts 5.5.4`, `Az.Compute 11.11.0`, `Az.ResourceGraph 1.3.0`, and lists all 5 VMs in `rg-lab-10vms`.
  - `post-state.txt`: Confirms all 5 VMs are deallocated.
- **Evidence Path:** `.handoffs/20261108-lab-ado-agent/42-evidence/tp-02/`

---

### Step 4.4: Cold-Start Agent Auto-Discovery (TP-03)
Tests agent cold-start auto-discovery: starts from fully deallocated state, verifies `win01` service automatically boots and reports `ONLINE` in pool `Production Windows Agent` within 3m35s, executes pipeline run 6, and deallocates.

```bash
lab/run-lab.sh --evidence .handoffs/20261108-lab-ado-agent/42-evidence/tp-03/ -- \
  lab/ado/session.sh lab/ado/ado-run.sh lab/ado/smoke.yml \
  --evidence-dir .handoffs/20261108-lab-ado-agent/42-evidence/tp-03/
```
- **Observed Evidence:**
  - `win01` agent online within ≤ 3m35s (limit: 300s).
  - Pipeline run 6 succeeded with exit 0.
  - Fail-safe deallocation verified in `post-state.txt`.
- **Evidence Path:** `.handoffs/20261108-lab-ado-agent/42-evidence/tp-03/`

---

## 5. "Do Not Claim Yet" (Pending & Unverified Items)

To maintain strict engineering integrity, the following items are explicitly declared as **NOT YET DEMONSTRABLE LIVE OR UNVERIFIED**:

1. **ADO Native Patch Templates on Real Fleet (Request 1 T-18 / TP-15):**
   - **Status:** **UNVERIFIED / PENDING-INFRA**
   - **Reason:** Request 1 task T-18 is blocked awaiting Claude plan amendment and owner sign-off on Request 2 (`20261108-lab-ado-agent`). Do not claim end-to-end execution of `patch-invoke.yml` on the live fleet until T-18 is formally executed.
2. **Live Execution of Generated `export-ado` Pipelines:**
   - **Status:** **UNVERIFIED LIVE**
   - **Reason:** Pipeline YAML generation and drift checking are fully verified (`export-ado --check`), but the exported pipelines under `pipelines/generated/` have not been triggered in a live Azure DevOps organization.
3. **500+ VM Fleet Concurrency:**
   - **Status:** **UNVERIFIED AT SCALE**
   - **Reason:** Concurrency and thread safety have been tested up to 50 VMs under fake az and 5 VMs on live Azure. 500+ VM execution is architecturally supported by file descriptor raising (`ulimit -n 4096`), but has not been tested against a live 500-VM Azure subscription.
4. **macOS Host Defensive Hardening on Linux:**
   - **Status:** **SIMULATED ON LINUX**
   - **Reason:** The demonstration and development environment is Linux. Features like `caffeinate` lifecycle management and BSD `sed` pattern matching are guarded and unit-tested, but real lid-close sleep prevention must be demonstrated on physical macOS hardware.
