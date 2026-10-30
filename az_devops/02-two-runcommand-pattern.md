# 02: The "exactly 2 run-commands per VM" pattern in ADO

## 1. What the guarantee is today

Each VM that passes its prechecks gets:

| # | Call | Payload | Blocking? | Code |
|---|------|---------|-----------|------|
| 1 | `az rest --method post --url /subscriptions/<id>/…/virtualMachines/<vm>/runCommand?api-version=… --body @remote.sh\|.ps1.body.json` (body: `commandId` `RunShellScript\|RunPowerShellScript` + script) | Compiled composite script: all steps, writes `$RES_FILE` with `__Y2B_RUN__`…`__Y2B_DONE__` | No: returns on `202 Accepted`, on any azure-cli version | [dispatcher.py:368](../y2shared/dispatcher.py#L368) `invoke_nowait` |
| 2 | `az vm run-command invoke … --scripts @collect.sh\|.ps1` | Reader: prints `$RES_FILE` between `===BEGIN_RESULT_RES===` / `===END_RESULT_RES===` | Yes, but it only `cat`s a file, so it takes seconds | [dispatcher.py:430](../y2shared/dispatcher.py#L430) `read_results` |

Precisely: **two run-commands execute on the VM.** ARM may see more *attempts*:

- `429 TooManyRequests` on either call → backoff and retry. ARM rejected the request, so nothing ran on the VM.
- `Conflict` on call #1 → the VM is busy with someone else's run-command; retried up to 3×. Nothing ran.
- `Conflict` on call #2 → **our own script is still running** (Run Command allows one at a time per VM) → `STILL_RUNNING` → retried after `collect_retry_interval`, up to `collect_retries`. Nothing ran.

The reader is read-only and idempotent. Repeating it is harmless, and this is why the collect side can be rerun freely in ADO.

## 2. Mapping the two phases onto ADO

```
┌─────────────────────────── Stage: Invoke ───────────────────────────┐
│ deployment job (agent: ubuntu-latest), environment: vm-fleet-prod    │
│   checkout                                                           │
│   y2b validate job.yaml                                              │
│   y2b run job.yaml --phase invoke -y                                 │
│     • prechecks per VM (bash on agent)  → SKIPPED/ENDED parts        │
│     • run-command #1 (POST, no wait)    → collect_list.jsonl         │
│   tar .result.parts → artifact "y2-state-invoke"     (always())      │
│   output var: fired=<count of confirmed invokes>                     │
└──────────────────────────────┬───────────────────────────────────────┘
                               │ condition: fired > 0
┌──────────────────────────────▼───── Stage: Wait ─────────────────────┐
│ job pool: server (agentless)                                         │
│   Delay@1  delayForMinutes = sleep_before_collect                    │
└──────────────────────────────┬───────────────────────────────────────┘
┌──────────────────────────────▼───── Stage: Collect ──────────────────┐
│ job (agent: ubuntu-latest)                                           │
│   checkout (same commit → same job.yaml → same job_hash)             │
│   download "y2-state-invoke" → untar into job dir                    │
│   y2b run job.yaml --phase collect -y                                │
│     • run-command #2 (reader) per invoked VM, STILL_RUNNING retries  │
│     • computed columns (bash on agent)                               │
│     • merge → result.csv / result.log in input order                 │
│   publish result.csv, result.log                                     │
│   if still_running: tar .result.parts → "y2-state-collect-<attempt>" │
└──────────────────────────────────────────────────────────────────────┘
```

### Why this works without changing the engine's state model

[runner.py](../y2shared/runner.py) already persists everything Phase 2 needs:

| State | Written by | Read back on `--resume` by |
|-------|-----------|----------------------------|
| `run.json` → `run_id`, `job_hash`, `phase` | `init_parts_dir` ([parts.py](../y2shared/parts.py)) | `init_parts_dir(resume=True)`: refuses if `job_hash` differs, otherwise returns the **same `run_id`**, so the reader's `__Y2B_RUN__` check matches |
| `collect_list.jsonl` → one record per fired VM with `invoked_at`, `confirmed` | Phase 1, before and after each invoke | `CollectList.load()` → Phase 2 work set |
| `NNNN-<vm>.csv/.log` part files | prechecks (SKIPPED/ENDED), invoke errors, collect | `is_part_finished` / merger |

None of these files store absolute paths, so the collect job can unpack them on a different agent with a different `_work/N/s` directory.

Phase 2 schedules each VM at `invoked_at + sleep_before_collect` ([runner.py:137](../y2shared/runner.py#L137)). If the ADO Delay has already covered that time, collection starts immediately. If the Delay is shorter, the collect job waits for the remainder **while holding an agent**. **Set `Delay ≥ sleep_before_collect`.**

## 3. The one CLI change needed: `--phase`

Today `run` (and `run --resume`) always does Phase 1 → Phase 2 → merge in one process. ADO needs to stop after Phase 1 and later start at Phase 2.

### Proposed spec

```
y2b|y2p run JOB.yaml [--phase all|invoke|collect]     (default: all = today's behaviour)
```

| `--phase` | Behaviour | Exit code |
|-----------|-----------|-----------|
| `all` | Unchanged | 0 / 1 / 2 / 3 as today |
| `invoke` | Fresh run (parts dir must not exist; same rule as today). Run Phase 1, set `run.json.phase = "collect"`, **do not merge, keep parts**. Print `fired=<n>` (confirmed invokes) on the last stdout line so the pipeline can read it | `0` Phase 1 finished (per-VM invoke errors are already in part files); `1` config/CSV error; `3` aborted (auth breaker, Ctrl-C) |
| `collect` | Implies `--resume`. Requires `run.json` + `collect_list.jsonl`. **Skips Phase 1 entirely**: rows with no ledger record and no part file are left to the merger, which marks them `ERROR:no_result`. Run Phase 2 and merge | `0` / `2` / `3` as today |

The key rule is **`collect` never invokes**. With today's `--resume`, rows missing from the ledger are fired again ([runner.py:401-410](../y2shared/runner.py#L401-L410)). In a pipeline, a collect job that might also fire would break "≤ 1 invoke per VM per run". With `--phase collect` the guarantee holds by construction.

### Where it goes

- [y2shared/cli.py:108](../y2shared/cli.py#L108): add `--phase` with `choices=["all","invoke","collect"]`, `default="all"`; `collect` sets `args.resume = True`. Reject `--phase collect` together with `--dry-run`.
- [y2shared/runner.py](../y2shared/runner.py):
  - Phase 1 block (§9, line 397): skip it when `args.phase == "collect"`.
  - After the post-Phase-1 abort checks (~line 541): if `args.phase == "invoke"`, call `update_run_phase(parts_dir, "collect")`, print the summary and `fired=<n>`, and `return 0` before Phase 2.
- Tests (pattern from `tests/test_m6_fire_and_forget.py` + `tests/fake_az/az`):
  1. `--phase invoke` then `--phase collect` gives the same `result.csv` as `--phase all`.
  2. `fake_az` call log shows exactly one `remote.*` and one `collect.*` invoke per VM across both processes.
  3. `--phase collect` with a row missing from the ledger gives `ERROR:no_result`, and `fake_az` shows **no** invoke for it.
  4. Changing `job.yaml` between phases → collect refuses (job hash mismatch), exit 1.
  5. Same tests through `y2p` (`RunPowerShellScript` command id).

Until this lands, use **L1** (single job, see [03 §4](03-pipeline-reference.md#4-level-1-single-job-no-code-change)). It already makes 2 run-commands per VM. The only cost is the agent held during the wait.

## 4. Handling VMs that are still running

If a step runs longer than `sleep_before_collect + collect_retries × collect_retry_interval`, the VM ends as `ERROR:still_running`. The parts dir is kept ([runner.py:602](../y2shared/runner.py#L602)), and the run exits `2`.

In ADO you can add an optional **second wait + collect**. Each one costs exactly one more reader call for *only* the still-running VMs, so the remote script is never re-fired:

```
Collect  ──(stillRunning == true)──► Wait2 (Delay@1, e.g. 15m) ──► Collect2 (--phase collect)
```

`is_part_still_running` VMs get `first_ready_at = now` on resume ([runner.py:129](../y2shared/runner.py#L129)), so Collect2 reads them immediately after the delay. [03 §3](03-pipeline-reference.md#3-the-reusable-template) shows the wiring.

## 5. Failure and rerun matrix

| What failed | What ADO shows | What to do | Run-commands added |
|-------------|----------------|------------|--------------------|
| Invoke job: config/CSV error (exit 1) | Invoke failed, no state | Fix `job.yaml`/CSV, run a **new pipeline run** | 0 |
| Invoke job: auth breaker (exit 3) | Invoke failed; state artifact published (`always()`) | Fix the service connection. **New run** recommended, see note | VMs fired before the abort have already run once |
| Agentless Delay cancelled | Wait cancelled | "Rerun failed jobs" → Delay → Collect | 0 |
| Collect job: auth/agent lost (exit 3) | Collect failed | **"Rerun failed jobs"**: re-downloads the *invoke* state and collects again (the reader is idempotent) | 1 reader per VM, no re-fire |
| Collect finished with `ERROR:still_running` (exit 2) | SucceededWithIssues | Optional Collect2 stage, or later: a manual pipeline that downloads `y2-state-collect-*` and runs `--phase collect` | 1 reader per still-running VM |
| VM rebooted mid-script | Row `ERROR:remote_incomplete` | Investigate VM; new run for those VMs (`--vm` filter parameter) | — |

> **Note on rerunning the Invoke job:** an ADO rerun of a failed Invoke job starts on a clean agent with no parts dir, so it is a **fresh run with a new `run_id`**, and VMs fired before the failure get fired again. For read-only audit jobs this is harmless. For jobs that change VMs, either make steps idempotent or add a resume path to Invoke: download the previous attempt's `y2-state-invoke` artifact if it exists and run `--phase invoke --resume`. That requires `--phase invoke` to also accept `--resume`, which is easy to add with the same flag.

## 6. Run-command budget

With 500 VMs, 450 passing prechecks and 20 still running after the first collect:

| Call | Count |
|------|-------|
| Prechecks (ARM reads, not run-commands) | 500 × `get-instance-view` + 1 × `account show` per subscription. With Resource Graph ([04 §5](04-scale-and-operations.md#5-cheaper-prechecks-with-azure-resource-graph)) this drops to 1 query |
| Run-command #1 (invoke) | 450 |
| Run-command #2 (reader) | 450 |
| Extra readers (Collect2) | 20 |
| **Total run-commands executed on VMs** | **920** (vs 2,250 for 5 steps run one by one) |
