# 01: Concept mapping: what ADO gives you and what stays in `y2`

ADO looks like it already has "prechecks" and "run command". It has parts of both, but they work at a different level. This document sorts each `y2` responsibility into **ADO**, **`y2`**, or **both**.

## 1. The two kinds of "precheck"

ADO **Approvals and checks** run *once per stage*. They answer whether **this pipeline run** may proceed now. `y2` **prechecks** run *once per VM*. They answer whether **this VM** should get the payload.

| Question | Granularity | Best home |
|----------|-------------|-----------|
| Is a human OK with this run touching production? | run | ADO **Approvals** |
| Are we inside the maintenance window? | run | ADO **Business hours** check |
| Is another fleet run already in progress on these VMs? | run | ADO **Exclusive lock** (prevents two runs from overwriting each other's `$RES_FILE` → `ERROR:stale_result`) |
| Is the pipeline built from the approved template? | run | ADO **Required template** check |
| Are there active Sev-1 alerts? | run | ADO **Query Azure Monitor alerts** check |
| Is subscription X reachable by this identity? | per subscription | `y2` precheck with `cache: subscription` (keep it, because it produces the `ENDED:` rows in `result.csv`) |
| Is VM Y running / deallocated? | per VM | `y2` precheck (optionally backed by one Resource Graph query, see [04 §5](04-scale-and-operations.md#5-cheaper-prechecks-with-azure-resource-graph)) |
| Should VM Y be started first (`actions: start`)? | per VM | `y2` |

ADO checks are configured on an **Environment** (used by a `deployment` job) or on the **service connection**. You do not write them in pipeline YAML. Putting them on the ARM service connection is the simplest option: every stage that uses that connection is gated.

**Rule of thumb:** if the answer is the same for every VM in the run, use an ADO check. If it differs per VM, use a `y2` precheck. Per-VM answers must appear as a row in `result.csv`, and an ADO check cannot write rows.

## 2. "Run command" in ADO vs `y2`

ADO can run scripts in three ways. None of them gives you `y2`'s model:

| ADO mechanism | Where the script runs | Fit for fleet checks |
|---------------|-----------------------|----------------------|
| `Bash@3` / `PowerShell@2` on a pool agent | On the **agent**, not the VM | Not a VM execution path by itself |
| `AzureCLI@2` calling `az vm run-command invoke` yourself | On the VM via Run Command | Works, but you rebuild batching, fire-and-forget invoke, collect, retry, 4 KB protocol and merge by hand. That is `y2`. |
| Environment **VM resources** (ADO agent installed on each VM) | On the VM via its own agent | Different model; see §5 |

ADO has no built-in task that bundles N steps into one Run Command payload and collects the results later. (Marketplace extensions exist that wrap `az vm run-command`. They still make one call per script.) **So the engine stays `y2`, called from `AzureCLI@2`.**

## 3. Responsibility map

| `y2` concept today | In ADO | Owner |
|--------------------|--------|-------|
| `az login` on a laptop | ARM **service connection** (workload identity federation) used by `AzureCLI@2` | ADO |
| Person typing `y2b run …` | Pipeline run: manual, `schedules:` cron, or triggered by a PR to `jobs/**` | ADO |
| Confirmation prompt (`Proceed? [y/N]`) | **Approvals** check; pipeline passes `-y` | ADO |
| `validate`, `compile` | A `Validate` stage on every PR (`y2b validate`, `y2b compile -o`, then publish `remote.sh`/`.ps1` for reviewers) | `y2` inside ADO |
| Host prechecks (bash) | Run on the Linux agent (bash 5, `az` preinstalled), same scripts | `y2` |
| Compile all steps → 1 payload | Unchanged | `y2` |
| Phase 1: runCommand POST (fire and forget) | `Invoke` job → `y2b run --phase invoke` | `y2` inside ADO |
| `sleep_before_collect` (Python heap wait) | **`Delay@1` in an agentless (`pool: server`) job** | ADO |
| Phase 2: reader run-command + collect retries | `Collect` job → `y2b run --phase collect` | `y2` inside ADO |
| Parts dir `.result.parts/` (`run.json`, `collect_list.jsonl`) | Handed between jobs as a **pipeline artifact** (tarball) | both |
| `--resume` | ADO **"Rerun failed jobs"**: the job re-downloads the state artifact and resumes | both |
| `result.csv`, `result.log` | Published **pipeline artifacts** (optionally also a Tests tab, see [03 §7](03-pipeline-reference.md#7-optional-per-vm-results-in-the-tests-tab)) | ADO |
| Exit code `2` (some VMs non-OK) | Task result **SucceededWithIssues** (orange) instead of failed | ADO |
| Exit code `3` (aborted, auth breaker) | Job **failed**; state artifact still published so a rerun can resume | ADO |
| macOS hardening (`caffeinate`, `ulimit`, bash 3.2) | Not needed on a Linux agent; `start_caffeinate()` is already a no-op off Darwin | none |
| `y2p` (Windows targets) | Still runs on a **Linux** agent: host scripts are bash, only the `--command-id` changes | `y2` |

## 4. Why not translate `job.yaml` into native ADO steps?

ADO template expressions can loop over parameters:

```yaml
# Tempting, but don't
- ${{ each step in parameters.steps }}:
  - task: AzureCLI@2
    inputs:
      inlineScript: az vm run-command invoke ... --scripts "${{ step.script }}"
```

This breaks the core property:

1. **It creates one run-command per step per VM.** With 5 steps and 500 VMs that is 2,500 Run Commands. The `y2` design reduces this to 1,000 (2 per VM). Run Command allows **one action command at a time per VM**, so per-step calls also run serially on each VM.
2. **Template expressions cannot safely concatenate block scalars into one script.** There is no string join over multi-line values with correct indentation, and ADO's runtime macro syntax `$(…)` overlaps with bash command substitution `$(…)`.
3. **You lose what is already tested**: `validate` (write_result name rules, Windows path quoting), byte-for-byte golden payloads, the base64 `$RES_FILE` contract, the 4 KB truncation detection and the container tests.

**Decision:** `job.yaml` stays the job language. The ADO template takes only *run settings* (which job file, which service connection, how long to wait, parallelism), not the job content.

## 5. Alternatives considered

| Option | Run Commands per VM | Pros | Cons |
|--------|---------------------|------|------|
| **A. `y2` in ADO, split phases** (this guide) | **2** | No VM-side install; reuses engine, tests and job files; the wait costs no agent | Needs the `--phase` flag; 4 KB result limit still applies |
| B. `y2` in ADO, single job (L1) | 2 | Zero code change | Agent held for the whole `sleep_before_collect`; long jobs approach agent and token time limits |
| C. **Managed Run Command** (`az vm run-command create --async-execution --output-blob-uri …`) | **1** create, then ARM reads (`az vm run-command show --instance-view`) and a delete | Output goes to blob storage, so no 4 KB limit; status polling is an ARM `GET`, not a VM execution | Needs a storage account and per-VM blob SAS or managed identity; leaves a child resource per VM to clean up; new parser and compile target. This would be a third `Profile`, not a pipeline change. Check the flags against your `az` version. |
| D. ADO agent on every VM (Environment VM resources, `deployment` job with `rolling` strategy) | 0 | Native ADO logs per VM; no 4 KB limit | 500 agents to install, patch and register; network path from each VM to ADO; not how this fleet is managed today |

Option C is the only way to get *below* two run-commands. Keep it as a possible future `y2` profile, not part of the ADO integration.
