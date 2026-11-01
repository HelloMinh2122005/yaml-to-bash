# 04: Scale and operations (500+ VMs)

## 1. Rough timing for one pipeline run

Rough estimates for 500 VMs at `parallel: 20`. Measure your own with L1 first.

| Segment | What happens | Rough duration | Holds an agent? |
|---------|--------------|----------------|-----------------|
| Validate | `validate` + `compile` | < 1 min | yes (short) |
| Invoke | 500 × (prechecks ≈ 2–4 s ARM reads + runCommand POST accept ≈ 5–20 s) / 20 workers | ~5–15 min | yes |
| Wait | `Delay@1` = `sleep_before_collect` | e.g. 5 min | **no** |
| Collect | ~450 × synchronous reader (≈ 20–40 s each, mostly Run Command overhead) / 20 workers + merge | ~8–15 min | yes |
| Collect2 (optional) | still-running VMs only | minutes | yes |

The first VMs are fired several minutes before the last ones, so by the time the Delay ends, early VMs have waited `sleep_before_collect + invoke duration`. That is fine: the engine only enforces a *minimum* wait per VM.

## 2. Throttling: `parallel` is a fleet-wide budget

- ARM throttles **per subscription and per identity**. All ADO jobs using the same service connection share one budget.
- The engine already backs off on 429 (15/30/60/120/240 s with jitter) and marks `ERROR:throttled` after that.
- Start at `parallel: 10–20`. Raise it only while `result.log` shows no `Throttled by Azure` lines.
- **Do not get more speed by running several pipelines or matrix shards against the same subscriptions.** Total concurrency is what ARM sees. `lockBehavior: sequential` + Exclusive lock exist to prevent exactly this.

## 3. Sharding: usually not needed

One Linux agent drives 500+ VMs: the work is I/O-bound `az` subprocesses, and `parallel` controls thread count. Shard only when:

- the inventory spans **many subscriptions** (each has its own ARM budget), and
- the run is too slow at the highest `parallel` that is safe per subscription.

If you do shard, **shard by subscription**: one matrix leg per subscription, each with its own `Invoke → Wait → Collect` chain, then a final job that concatenates the per-shard `result.csv` files (header once). Two things are missing today:

1. An input override such as `y2b run job.yaml --input shard.csv` (today `input:` is fixed in `job.yaml`, and it is part of `job_hash` because the hash is over the YAML text).
2. A deterministic cross-shard merge. Within a shard, order is input order. Across shards you get shard order unless the merge sorts by original row number.

Treat both as a separate design item. Do not hack them into the pipeline.

## 4. Time limits

| Limit | Value (check your org) | Effect | Mitigation |
|-------|------------------------|--------|------------|
| Microsoft-hosted job timeout | 60 min on free private-project tier; up to 360 min with paid parallel jobs | L1 single job can hit it on large fleets | L2 split: each agent job is short |
| Agentless job timeout | 60 min default | `Delay@1` longer than that is cancelled | `timeoutInMinutes` on the Wait job (the template sets 240) |
| Self-hosted job timeout | Set by `timeoutInMinutes` (0 = no limit) | — | — |
| Run Command script runtime | 90 min (Azure cap for action Run Command) | Longer scripts end as `ERROR:remote_incomplete` | Split the job; keep steps short |
| `az` call timeout in the engine | 300 s for invoke and reader ([dispatcher.py](../y2shared/dispatcher.py) `INVOKE_CALL_TIMEOUT`, `READER_CALL_TIMEOUT`) | `ERROR:run_command_timeout` | Usually ARM slowness; lower `parallel` |
| Run Command output | last 4,096 bytes of stdout | `ERROR:result_truncated` | Unchanged from CLI use: verbose output → `$LOG_FILE`, keep `write_result` values short |

## 5. Cheaper prechecks with Azure Resource Graph

`check-if-vm-is-running` makes **one ARM read per VM** (`az vm get-instance-view`), which is 500 calls that share the throttle budget with run-command. On ADO you can replace them with **one** Resource Graph query in the Invoke job and keep the per-VM precheck (so `SKIPPED:` rows still appear in `result.csv`). The precheck then reads a local file instead of calling ARM.

**Step in the Invoke job, before `y2b run --phase invoke`** (inside the same `AzureCLI@2` script so it uses the service connection):

```bash
az config set extension.use_dynamic_install=yes_without_prompt >/dev/null
az graph query --first 1000 -o tsv --query "data[].[name, rg, power]" -q "
  resources
  | where type =~ 'microsoft.compute/virtualmachines'
  | project name = tolower(name), rg = tolower(resourceGroup),
            power = tostring(properties.extended.instanceView.powerState.code)" \
  > "$(dirname "$Y2_JOB")/powerstate.tsv"
```

**Precheck in `job.yaml`** (runs with cwd = job dir, bash 3.2–compatible):

```yaml
    - name: "check-if-vm-is-running"
      script: |
        awk -F'\t' -v vm="$VM_NAME" -v rg="$RESOURCE_GROUP" '
          tolower($1) == tolower(vm) && tolower($2) == tolower(rg) { print $3; found = 1 }
          END { if (!found) print "NOT_IN_GRAPH" }' ./powerstate.tsv
      result:
        "PowerState/running":     { log: "VM is running",     action: "continue" }
        "PowerState/deallocated": { log: "VM is deallocated", action: "skip" }
        "PowerState/stopped":     { log: "VM is stopped",     action: "skip" }
        default:                  { log: "VM not found in Resource Graph snapshot", action: "end_check" }
```

Caveats:

- Resource Graph can lag by a few minutes. A VM started just before the run may still show as deallocated, so it is skipped and you rerun it with `vmFilter`.
- **Do not combine this with `action: "start"` or `action: "start-stop"` results.** After `az vm start`, the engine re-runs the precheck once ([precheck.py](../y2shared/precheck.py)), and a snapshot file still says "deallocated", which gives `ERROR:start_failed`. Keep the live `get-instance-view` precheck for jobs that start VMs. Also, for VMs matched by a `start-stop` or `stop` result, `.power` files (`NNNN-<vm>.power`) track the original VM power state inside the `.parts/` folder to ensure proper restoration or deallocation after post-run hooks.
- Above 1,000 VMs, page with `--skip-token`, or scope with `--subscriptions` / `--management-groups`.
- This precheck depends on a file the pipeline creates, so local `y2b run` needs the same file. Generate it with the same `az graph query` command.

The subscription precheck (`az account show`, `cache: subscription`) is already one call per subscription. Leave it as is.

## 6. Auth inside long runs

- `AzureCLI@2` logs `az` in from the service connection at task start. With **workload identity federation**, the credential cannot always be refreshed inside a very long task, and failures show up as `AADSTS…` errors. The engine classifies these as `auth`, and after 5 in a row the circuit breaker exits `3`.
- The L2 split avoids this by design: Invoke and Collect are separate, short tasks, and each gets a fresh login. This is one more reason to prefer L2 over L1 for large fleets.
- If Collect fails on auth, use **"Rerun failed jobs"**. Collect is idempotent (the reader only reads `$RES_FILE`), so rerunning costs one reader per VM and never re-fires the payload.

## 7. Network and agents

- Run Command goes **agent → ARM → VM guest agent**. The ADO agent needs **no network path to the VMs**, only to `management.azure.com` and Entra ID. Microsoft-hosted agents work unless your org restricts ARM access by source IP.
- Use a self-hosted Linux pool (or Managed DevOps Pools) if policy requires private egress, or if you need job times beyond hosted limits.
- `y2p` does not need Windows agents.

## 8. Data handling

- `result.csv`, `result.log` and the state tarball are **pipeline artifacts**. Anyone with read access to the pipeline can download them, and they live for the pipeline's retention period. `write_result` values and step output in `result.log` can contain hostnames, versions and configuration. Set retention and pipeline permissions accordingly.
- The state tarball contains `remote.sh`/`.ps1` (the full payload) and per-VM logs. Treat it like the results.
- `$RES_FILE` / `$LOG_FILE` stay on each VM (as with CLI use). The next run overwrites them, and `run_id` stops a stale file from being read as current.

## 9. Rollout order

1. **L1 on 2 VMs** (`--vm` / `vmFilter`) with `examples/linux`: proves the service connection, RBAC and `result.csv` artifact.
2. **L1 on the full Linux fleet** with Approvals on the Environment. Record Invoke and Collect durations and any throttling.
3. **Implement `--phase`** ([02 §3](02-two-runcommand-pattern.md#3-the-one-cli-change-needed---phase)) with its tests. Confirm with `tests/fake_az/az` that there are exactly 2 run-commands per VM across the two processes.
4. **L2 template** on the Linux fleet. Compare `result.csv` with the L1 run (it should match except for timestamps).
5. **`y2p`** through the same template (`tool: y2p`) on the Windows fleet.
6. **L3**: switch consumers to `extends:`, add Required template, Business hours and Exclusive lock checks, then turn on `schedules:`.
7. Optional: Resource Graph precheck (§5), JUnit Tests tab ([03 §7](03-pipeline-reference.md#7-optional-per-vm-results-in-the-tests-tab)), and sharding by subscription (§3) if the fleet outgrows one agent.
