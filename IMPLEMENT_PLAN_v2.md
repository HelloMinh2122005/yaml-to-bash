# IMPLEMENT_PLAN_v2 — Fire-and-forget execution

Status: draft · 2026-10-29 · applies to `y2b` at commit `c131244`

## 1. Goal

Today each worker runs one VM end to end: precheck → `az vm run-command invoke` (blocks until the remote script ends) → collect → part file. A VM whose script runs for 10 minutes holds a worker for 10 minutes, so the time for the whole fleet scales with `total script time / parallel`.

v2 keeps one pipeline and changes it into two phases:

1. **Invoke phase**: loop through the VMs in the input list. Precheck each one and fire the remote script with `--no-wait`. Every VM whose script was fired goes on the **collect list**.
2. **Collect phase**: run only on the collect list. Read the results each script left on its VM and build the report.

Requirements, and where each is handled:

| # | Requirement | Section |
|---|---|---|
| R1 | Fire and forget: fire the script on every VM first, instead of finishing one VM before the next | §3.1 |
| R2 | A VM that is skipped or unreachable in the invoke phase is never touched in the collect phase: no second precheck, no az call | §3.1, §3.4 |
| R3 | The invoke phase produces a temporary file, the collect list, and the collect phase runs on that list only | §3.2 |
| R4 | Collect waits for a sleep that the job YAML defines (`sleep_before_collect`, required). If a VM is still busy, it waits `collect_retry_interval` and tries again, up to `collect_retries` times. All three are job YAML keys | §3.4, §3.6 |
| R5 | Steps no longer declare `var`: the step script names its result in `write_result NAME VALUE` | §3.8 |

**Non-goals:** a second, synchronous mode (v1's blocking path is replaced, not kept beside v2); Windows VMs; changes to the precheck, collect or step schema beyond §3.6; changes to the merge step or the output format.

## 2. Flow

```
input list ──► Phase 1  INVOKE   (loop over input list, `parallel` workers)
                 precheck stops VM ──────────► part file now  (SKIPPED / ENDED / ERROR:precheck…)
                 az invoke --no-wait fails ──► part file now  (ERROR:vm_not_found / auth / …)
                 dry run ────────────────────► part file now  (DRY_RUN:ok)
                 az invoke --no-wait ok ─────► append to .result.parts/collect_list.jsonl
                                                        │
               Phase 2  COLLECT  (collect list only)    ▼
                 sleep `sleep_before_collect` from that VM's invoke → reader run-command (cat $RES_FILE)
                 busy? wait `collect_retry_interval`, retry up to `collect_retries` times → else ERROR:still_running
                 result → collect columns → part file
                                                        │
               Merge  (unchanged: input order, ERROR:no_result for gaps)
```

Every VM in the input ends with exactly one part file. Phase 1 writes it for VMs that were not fired; phase 2 writes it for VMs on the collect list.

## 3. Design

### 3.1 Phase 1 — Invoke

The input rows (after `--vm` / `--limit`) run through the existing `ThreadPoolExecutor` (`parallel` workers). For each VM:

1. Run `PrecheckEngine.run_prechecks_for_vm` as today (subscription cache and `actions: start` included).
2. **Precheck stops the VM** → write the part file now with `collect_for_vm(..., outcome_marker=marker)` (only passthrough columns get values; no scripts run). The VM never goes on the collect list.
3. **`--dry-run`** → write the part file now with `DRY_RUN:ok`. It is not on the collect list.
4. **Fire the script:**
   1. Append a collect-list record **before** the az call: `invoked_at` = now, `confirmed: false` (see §3.2 for why).
   2. Run `az vm run-command invoke ... --scripts @<parts>/remote.sh --no-wait`.
      - rc 0 → append the same record with `confirmed: true`. The output is ignored.
      - Failure → classify it with the existing `classify_error` and apply the existing retry policy (throttle backoff 15/30/60/120/240 s, busy 3 × 60 s, unknown once after 30 s). A final failure → write the part file now with the marker (`ERROR:vm_not_found`, `ERROR:vm_agent_unavailable`, `ERROR:run_command_busy`, `ERROR:throttled`, `ERROR:auth`, `ERROR:az:…`). The finished part file takes the VM out of phase 2.
   3. The subprocess timeout is a new constant `INVOKE_CALL_TIMEOUT = 300` s, because the call should return as soon as ARM accepts it.
5. The auth circuit breaker works as today: 5 consecutive auth failures abort the run with exit 3.

### 3.2 Collect list — `<parts>/collect_list.jsonl` (R3)

The collect list is one file in the existing parts folder (`.<output_csv stem>.parts/`), holding one JSON object per line. It is deleted with the parts folder after a successful merge (but see "collect later" in §3.7).

```json
{"index": 12, "vm_name": "vm-app-12", "subscription": "…", "resource_group": "rg-prod",
 "invoked_at": "2026-10-29T10:15:02+00:00", "confirmed": true, "attempts": 1,
 "log": ["[…] [precheck check-sub] → \"0\" → continue: …", "[…] [dispatch] invoke --no-wait accepted"]}
```

**Phase 2 work set** = VMs that have a record in the collect list **and** no finished part file. A VM whose fire failed is excluded by its finished part file.

**Why a record is written before the az call.** If y2b crashes after Azure accepted the invoke but before the record was saved, a resume would fire the script a second time. Firing a script twice on a VM is the one mistake this design must not make. With the record written first, every VM that *might* be running the script is on the list. Resume never fires it again; it only collects. If the script never actually started, the run-id check returns `ERROR:not_started` (§3.4).

New module `y2b/collectlist.py` with class `CollectList(parts_dir)`:

- `append(record)`: takes a `threading.Lock`, writes one line, then calls `flush()` and `os.fsync()`. Writes never interleave.
- `load() -> dict[int, record]`: keys records by `index`; if a row appears twice, the last record wins. A malformed **last** line (a crash in the middle of a write) is ignored with a warning. A malformed line anywhere else is a hard error.
- Each record checks `vm_name` against the `VMRow` at that `index`. A mismatch means the input CSV changed, and resume refuses to continue.
- `log` holds the phase-1 `VMLogger.lines`, so the final per-VM log covers both phases. Add `VMLogger.from_lines(vm_name, index, lines)` for phase 2.

### 3.3 Remote script changes — run id and done marker

The collect phase reads `$RES_FILE` long after the invoke. It must be able to tell three cases apart: results from this run, a script that never started, and a script that was killed halfway. Two reserved lines in `$RES_FILE` do this. Both use the existing `KEY=base64` format, so the parser does not change.

`templates/remote.sh.tpl`:

```bash
Y2B_RUN_ID=__RUN_ID__
...
: > "$RES_FILE"
printf '__Y2B_RUN__=%s\n'  "$(printf '%s' "$Y2B_RUN_ID" | base64 -w0)" >> "$RES_FILE"
...
__RUN_STEPS__
printf '__Y2B_DONE__=%s\n' "$(printf '%s' "$Y2B_RUN_ID" | base64 -w0)" >> "$RES_FILE"
exit 0
```

- The `===BEGIN/END_RESULT_RES===` echo at the end of `remote.sh` is removed: nobody reads its output any more. The reader script prints that block instead.
- `run_id` = `<UTC yyyymmddThhmmssZ>-<8 hex>`. It is generated on a fresh run, stored in `run.json`, and reused on `--resume`.
- `compile_job(job, run_id: str = "preview")`: `y2b compile` uses `preview`. Regenerate `tests/golden/hip_compliance_remote.sh`.
- Names that start with `__Y2B_` are reserved: `write_result` refuses them at runtime (§3.8). `collector.py` drops `__Y2B_*` keys before it exports variables to computed columns.

New template `templates/collect.sh.tpl` (the reader), written once per run to `<parts>/collect.sh`:

```bash
#!/bin/bash
RES_FILE=__RES_FILE__
echo "===BEGIN_RESULT_RES==="
cat "$RES_FILE" 2>/dev/null
echo "===END_RESULT_RES==="
exit 0
```

### 3.4 Phase 2 — Collect (R2, R4)

Phase 2 starts once phase 1 has handled every target VM. It loads the collect list and takes the work set from §3.2. Nothing outside that set is touched: skipped and unreachable VMs already have their part file from phase 1.

**Timing rule (R4).** All three values come from the job YAML (§3.6); nothing in the code fixes them. Sleep for `sleep_before_collect`, then collect. If the VM is still busy, wait `collect_retry_interval` and try again, up to `collect_retries` times. In the worst case a VM is read `1 + collect_retries` times.

The sleep is counted from **each VM's own** `invoked_at`, not from the end of phase 1. The first VMs fired are therefore read first. If phase 1 took longer than the sleep (for example 500 VMs), the early VMs are ready as soon as phase 2 starts.

**Scheduler.** It runs on the main thread. Waiting happens in a heap, not with `time.sleep` inside a worker, so a busy VM never holds one of the `parallel` workers:

```
heap = [(invoked_at + sleep_before_collect, index, retries_used=0)]; in_flight = {}
loop until heap empty and no in_flight:
    while len(in_flight) < parallel and heap[0].ready_at <= now: submit collect_attempt(vm)
    wait on the first finished future (timeout = time until next ready_at)
    result == STILL_RUNNING and retries_used < collect_retries → push (now + collect_retry_interval, index, retries_used + 1)
    result == STILL_RUNNING and retries exhausted              → finalize ERROR:still_running
    otherwise                                                  → finalize with vars or marker
```

**`collect_attempt(vm)`** runs `az vm run-command invoke --scripts @<parts>/collect.sh` synchronously. It shares phase 1's retry loop for throttle, auth and unknown errors, with one difference: when classify returns **busy**, it returns `STILL_RUNNING` at once. Busy means the fired script is still running, because Azure allows only one run-command per VM at a time (confirm in the spike, §4). Only busy uses the `collect_retries` budget.

What the reader output means:

| Reader result | Outcome |
|---|---|
| Busy / Conflict | `STILL_RUNNING` → retry after `collect_retry_interval`, or `ERROR:still_running` when the retries are used up |
| `__Y2B_RUN__` = this run_id **and** `__Y2B_DONE__` = this run_id | Success: `collect_for_vm` with the step vars |
| `__Y2B_RUN__` = this run_id, no `__Y2B_DONE__` | `ERROR:remote_incomplete` (script killed, VM rebooted, Azure's 90-minute cap) |
| `__Y2B_RUN__` present but a different run_id | `ERROR:stale_result` (the script did not run in this run; the file is from an earlier run) |
| Empty `$RES_FILE` / no `__Y2B_RUN__` | `ERROR:not_started` |
| agent / missing / auth / throttled / truncated | The existing markers (`ERROR:vm_agent_unavailable`, …); the auth breaker applies |

A step that failed or timed out on the VM still produces `__ERROR__ …` values inside a DONE result, as today.

### 3.5 New outcome markers

Add these to the README §5 table and to USER_GUIDE "Outcome Markers Reference":

| Marker | Meaning |
|---|---|
| `ERROR:still_running` | The VM was still busy after the sleep and every collect retry. It can be read again later with `--resume` (§3.7) |
| `ERROR:remote_incomplete` | The script started but did not write the done marker |
| `ERROR:stale_result` | `$RES_FILE` belongs to another run |
| `ERROR:not_started` | `$RES_FILE` is missing or empty: the fired script never ran |

### 3.6 Config and CLI

The collect timing lives in the job YAML, next to `parallel`, so each job sets it to match how long its own steps take:

```yaml
parallel: 10

# Collect timing (fire and forget)
sleep_before_collect: "5m"      # REQUIRED. Wait between a VM's invoke and its first collect attempt
collect_retry_interval: "30s"   # optional, default "30s". Wait before re-reading a VM that is still busy
collect_retries: 3              # optional, default 3. Re-reads after the first attempt
```

| Key / flag | Change |
|---|---|
| Durations | `sleep_before_collect` and `collect_retry_interval` accept a number of seconds or a duration string: `90`, `"30s"`, `"5m"`, `"1h"`, `"1m30s"`. Add a `parse_duration()` helper in `config.py`; validation rejects anything else and negative values. |
| `sleep_before_collect` | **Required, no default.** A job without it fails `y2b validate` with exit 1, because the right value depends on how long that job's steps run and the code cannot guess it. `0` is allowed and means "collect right away". Recommended: about the expected script runtime. The per-VM `time.sleep` in `collector.py` is removed; the scheduler owns the wait. |
| `collect_retry_interval` (new) | Optional, default `"30s"`, must be > 0. |
| `collect_retries` (new) | Optional, default `3`, must be ≥ 0. |
| Readiness warning | Validation warns when the sum of step `timeout`s is greater than `sleep_before_collect + collect_retries × collect_retry_interval`, the longest a VM can be waited for. |
| `examples/hip-compliance/job.yaml` | Add the three keys above. This changes the job hash, so the golden file is regenerated anyway in M1. |
| `run_timeout` | Deprecated: it was the timeout of v1's blocking call, which no longer exists. It is still accepted, so old YAML stays valid, but ignored with a warning. Validation also warns when the sum of step timeouts is greater than 5400 s (Azure's 90-minute run-command cap). |
| `run.json` | Add `run_id` and `phase` (`invoke` \| `collect` \| `merged`). |
| CLI | No new flags. |

### 3.7 Resume, Ctrl-C, abort

**Rule.** A VM that has a finished part file, or that is on the collect list, is never prechecked or fired again. A VM with a finished part file is never collected again. The one exception is a part file that holds `ERROR:still_running`, which is counted as not finished.

| State when the run stopped | What `--resume` does |
|---|---|
| Interrupted in phase 1 | Phase 1 continues only for VMs that have neither a part file nor a collect-list record, then phase 2 runs on the full work set |
| Interrupted in phase 2 (`run.json` phase = `collect`) | Phase 1 is skipped. Phase 2 runs on the work set. `$RES_FILE` is still on the VM, and the `run_id` guards against stale data |
| Interrupted during merge | As today |
| Completed, but some VMs ended `ERROR:still_running` | See "Collect later" below |

**Collect later.** When at least one VM ends with `ERROR:still_running`, the merge still writes `result.csv` and the run exits 2. The parts folder is **kept**, even without `--keep-parts`, and `run.json` records `phase: "merged"`. The summary prints `y2b run <job> --resume` to read those VMs later.

On that resume:
- Only the `ERROR:still_running` VMs are collected again.
- The first attempt runs immediately, with no new sleep, because the scripts started long ago. The retries then follow `collect_retry_interval` and `collect_retries` from the YAML, as before.
- The merge writes `result.csv` again.

**Decided.**
- Resume never fires a script again, including on a VM whose fire failed with a temporary error such as throttling. A VM that failed to fire should be rerun with a fresh run plus `--vm`.
- The `job_hash` check in `init_parts_dir` stays. On resume, `run_id` is read from `run.json`, never regenerated.
- If `collect_list.jsonl` is missing while `run.json` says the run got past phase 1, resume refuses to continue.
- Ctrl-C: the first press stops submitting new work and waits for in-flight calls, then exits 3. A second press calls `os._exit(3)`, as today. The output tells the user that already-fired scripts **keep running on the VMs**.

### 3.8 Step `var` removed — `write_result NAME VALUE` (R5)

**Before.** Each step declared `var:`. `run_step` exported it as `CURRENT_STEP_VAR`, and `write_result "$VALUE"` wrote under that name. The declaration did three jobs:
1. It named the value.
2. It gave `run_step` a key for the failure fallback (`__ERROR__ rc=N` / `__ERROR__ timeout`).
3. It let validation reject a collect `var:` with a typo.

**After.** The step script names its own result, and one step may write several values:

```yaml
steps:
  - name: "check-hip-version"
    script: |
      write_result HIP_VERSION "$(rpm -q --queryformat '%{VERSION}' hip 2>/dev/null || echo NOT_INSTALLED)"
      write_result HIP_RELEASE "$(rpm -q --queryformat '%{RELEASE}' hip 2>/dev/null || echo NOT_INSTALLED)"
```

Jobs 2 and 3 are covered as follows.

**Template (`remote.sh.tpl`):**

```bash
write_result() {          # $1=NAME $2=VALUE
  if [ "$#" -ne 2 ] || [[ ! "$1" =~ ^[A-Z_][A-Z0-9_]*$ ]] || [[ "$1" == __Y2B_* ]]; then
    echo "write_result: expected 'write_result NAME VALUE' with NAME matching ^[A-Z_][A-Z0-9_]*\$ (step $Y2B_STEP_NAME)" >> "$LOG_FILE"
    return 1
  fi
  printf '%s=%s\n' "$1" "$(printf '%s' "$2" | base64 -w0)" >> "$RES_FILE"
  printf '%s="%s"\n' "$1" "$2" >> "$LOG_FILE"
}

run_step() {              # $1=function $2=timeout $3=step index $4=step name
  export Y2B_STEP_NAME="$4"
  echo "=== $(date -Is) step $3 $4" >> "$LOG_FILE"
  timeout "$2" bash -c "$1" >> "$LOG_FILE" 2>&1
  local rc=$? status=ok
  if [ "$rc" -eq 124 ]; then status=timeout; elif [ "$rc" -ne 0 ]; then status="rc=$rc"; fi
  printf '__Y2B_STEP_%s__=%s\n' "$3" "$(printf '%s' "$status" | base64 -w0)" >> "$RES_FILE"
}
```

- Every step now records its own status (`ok`, `rc=N` or `timeout`) under a reserved key. This replaces the per-var fallback and costs about 30 bytes per step out of the 4 KB output limit.
- A one-argument `write_result "$X"` (the old form) fails loudly in the VM log instead of writing under a wrong name.

**Static scan (`compiler.py`, `scan_written_vars(step) -> set[str]`).** A regex finds literal names in each step's `script` or `script_path` body: `write_result\s+["']?([A-Z_][A-Z0-9_]*)\b`. The result is a map from var name to the steps that write it. It is used twice:
- **Validation** (job 3):
  - A collect `var:` that no step writes literally → **warning**, not error, because a name built at runtime (`write_result "$NAME" …`) cannot be seen statically.
  - A name written literally by two steps → warning (the last write wins).
  - A step that still has a `var:` key → **error** with the hint "remove `var`; use `write_result NAME VALUE`", so an old job fails at validate time instead of silently on the VMs.
- **Collector** (job 2). For a column `var: X`, the rules are applied in order:
  1. X was written → its value. A written value always wins, even if the step later exited non-zero, as in v1.
  2. X was not written, and the scan maps X to a step whose status is not `ok` → `__ERROR__ <status>` (same text as v1, for example `__ERROR__ rc=1`).
  3. Otherwise → `__MISSING__`.

**Other changes:**
- `config.Step.var` is removed.
- The compiler no longer pads by var length; it emits `run_step y2b_step_N <timeout> N <quoted step name>`.
- The collector filters `__Y2B_STEP_*__` keys out of the env for computed columns, together with the other `__Y2B_*` keys.
- `examples/hip-compliance/job.yaml` and `scripts/audit_hardware.sh` move to the two-argument form, for example `write_result HW_AUDIT_STATUS "PASS"`.

### 3.9 Timing and cost trade-off

- v1 ≈ `N × (overhead + script) / parallel`.
- v2 ≈ `N × overhead_nowait / parallel` + `sleep_before_collect` + `N × overhead_reader / parallel` (+ `collect_retry_interval` for each retry a busy VM needs).

The reader is a full run-command, so it carries the usual 30–90 s session overhead. v2 wins clearly when scripts run longer than one run-command session. **For very short checks it is slower than v1**, because each VM now needs two sessions. With one pipeline and no sync mode, this is an accepted cost.

v2 makes at least 2 ARM write calls per fired VM, plus at most `collect_retries` more for each busy VM. Phase 1 also fires faster than v1, so throttling is more likely; the existing backoff covers it. Setting `sleep_before_collect` close to the real runtime keeps retries rare.

## 4. M0 — Spike before any code (needs a real VM and `az`)

These assumptions are not verified (there is no `az` in the dev environment). Run each one against 1–2 test VMs and record the stderr text and timings in this file:

1. `az vm run-command invoke --no-wait` exists in our az version, returns rc 0 within seconds, and the script still runs to the end.
2. A second invoke while the first script is running fails with a Conflict that `PATTERNS_BUSY` already matches. Measure how fast that Conflict comes back. Also check what happens if the first invoke is still queued and has not started yet.
3. `$RES_FILE` under `/var/tmp/runcommand/` survives after the run-command ends, and is readable by the next run-command (same root context).
4. How agent-down and VM-deallocated errors appear with `--no-wait`: in phase 1 (synchronous) or only in phase 2.

If (2) fails (Azure queues the second call instead of rejecting it), the reader stops relying on Conflict and decides from the markers alone. `RUN` without `DONE` is then treated as `STILL_RUNNING`, with the same `collect_retries` × `collect_retry_interval` budget, and ends as `ERROR:still_running`. `ERROR:remote_incomplete` is dropped, because "killed" and "still running" can no longer be told apart.

## 5. Milestones and file changes

| M | Scope | Files |
|---|---|---|
| M0 | Spike (§4) | this file |
| M1 | run_id + done marker + reader template; durations; new config keys; `run_timeout` deprecation; golden regen | `templates/remote.sh.tpl`, `templates/collect.sh.tpl` (new), `compiler.py`, `validate.py`, `config.py` (`collect_retry_interval`, `collect_retries`, `parse_duration`), `examples/hip-compliance/job.yaml`, `tests/golden/…` |
| M1b | Step `var` removed (§3.8): two-argument `write_result`, per-step status, `scan_written_vars`, validation errors and warnings, collector rules 1–3. It changes the same template, compiler and validation code as M1, so do it in the same pass; the uncommitted M1 work already adds a `__Y2B_` check on step `var`, which goes away | `templates/remote.sh.tpl`, `compiler.py`, `validate.py`, `config.py`, `collector.py`, `examples/hip-compliance/job.yaml`, `examples/hip-compliance/scripts/audit_hardware.sh`, `tests/golden/…` |
| M2 | Collect list | `collectlist.py` (new), `vmlog.py` (`from_lines`), `parts.py` (`run.json` fields, `is_part_finished` treats `ERROR:still_running` as not finished) |
| M3 | Dispatcher: replace `dispatch_vm` with `invoke_nowait(...)` and `read_results(...) -> ReadResult` (vars \| STILL_RUNNING \| marker), both on a shared `_az_run_command(cmd, timeout, busy_policy)` retry loop | `dispatcher.py` |
| M4 | Runner: phase 1 loop (part file for non-fired VMs, collect list for fired ones), phase 2 scheduler on the collect list, resume and Ctrl-C per phase, keep the parts folder on `still_running`; remove the sleep from `collector.py`; filter `__Y2B_*` | `runner.py`, `collector.py` |
| M5 | Docs, including the new `write_result NAME VALUE` contract and the removed step `var` | `README.md`, `USER_GUIDE.md`, `CLAUDE.md` |

`merger.py` does not change.

## 6. Tests

**Fake az (`tests/fake_az/az`).** Add state in `$FAKE_AZ_STATE_DIR` (a file per VM). Each `az` call is a new process, so the current `os.environ` counter decrement for `FAKE_AZ_*_COUNT_*` never carries over to the next call. Move those counters into the state dir as well.

- `--no-wait`: read `Y2B_RUN_ID=` from the `@script` file and write `{run_id, started_at}`.
- Reader call: return Conflict while `now < started_at + FAKE_AZ_RUNTIME_<vm>`, then return a result block containing `__Y2B_RUN__`/`__Y2B_DONE__` and the step vars.
- `FAKE_AZ_ASYNC_<vm>=agent|never_started|killed|stale` simulates async failures.

**Unit**
- Collect list: append/load, last record wins, truncated last line tolerated, mid-file corruption rejected, `vm_name` mismatch rejected.
- `parse_duration`: `90`, `"30s"`, `"5m"`, `"1h"`, `"1m30s"` are accepted; `"5x"`, `""` and `-1` are rejected.
- Validation: a job without `sleep_before_collect` fails with exit 1; `sleep_before_collect: 0` passes; when `collect_retry_interval` / `collect_retries` are missing, they get `30s` / `3`. The YAML fixtures in `test_m2…`, `test_m5…` and `test_validate.py` gain the key.
- Reader outcome table (§3.4): one test per row.
- Scheduler with an injected clock: the first attempt waits exactly `sleep_before_collect` from `invoked_at`. A VM busy on every attempt is read 1 + `collect_retries` times, `collect_retry_interval` apart, then ends `ERROR:still_running`. A VM busy twice then done ends OK. A busy VM never occupies a worker while it waits.
- Compiler: `run_id` injected; no result echo in `remote.sh`; `bash -n` passes; golden updated.
- Step `var` removal (§3.8):
  - A step with a `var:` key fails validation with the migration hint.
  - A collect `var:` not written by any step gives a warning, not an error.
  - One step writing two values fills two columns.
  - Collector rules 1–3: a written value wins over a non-`ok` status; a missing value from a failed step gives `__ERROR__ rc=N` or `__ERROR__ timeout`; a missing value from an `ok` step, or from a name the scan can't see, gives `__MISSING__`.
  - In the container test, `write_result "only-one-arg"` and `write_result lower_case x` write nothing and log an error.
- Existing `test_m4_dispatcher.py` cases move to `invoke_nowait` / `read_results` with the same error classes.

**Integration (`test_m5_runner.py`, add `test_m6_fire_and_forget.py`)**
- R1: 50 VMs with `FAKE_AZ_RUNTIME=2` and `parallel 5`. Wall time is close to one runtime, not 10 × runtime, and every `--no-wait` call in `FAKE_AZ_LOG` comes before the first reader call.
- R2: in a mix of skipped, not-found and OK VMs, skipped and not-found VMs get **zero** reader calls and are absent from the collect list, and each precheck script runs exactly once per VM (count via a counter file).
- R3: after phase 1 the collect list holds exactly the fired VMs, and phase 2's reader calls in `FAKE_AZ_LOG` match that list one to one.
- R4: covered by the scheduler unit tests, plus one runner test with `sleep_before_collect: 1s`, `collect_retry_interval: 1s`.
- The existing "500 VMs at parallel 50 == parallel 1" test passes on the new pipeline.
- Resume after an interrupt in phase 1 (no double fire, including a record with `confirmed: false`) and in phase 2 (no re-fire, collect completes).
- Collect later: a VM with a runtime longer than the whole retry budget ends `ERROR:still_running` with exit 2 and the parts folder kept. A later `--resume` reads only that VM, with no sleep and no fire, and rewrites `result.csv` with its real values.
- Auth breaker trips in phase 1 and in phase 2 → exit 3.

**Container (`rockylinux:9`)**: run the compiled `remote.sh`, then `collect.sh` in the same container, and check the RUN/DONE markers and the decoded vars.

## 7. Open questions

- **Q1.** Should phase 1 and phase 2 have separate concurrency (`collect_parallel`)? Phase 1 calls are short and phase 2 calls are long, so one shared `parallel` may be too low for phase 1 and too high for ARM limits in phase 2.
