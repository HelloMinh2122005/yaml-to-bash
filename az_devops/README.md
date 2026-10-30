# Running `y2b` / `y2p` in Azure DevOps

This folder explains how to run the `yaml_to_bash` (`y2b`) and `yaml_to_powershell` (`y2p`) model in **Azure DevOps Pipelines** while keeping the main guarantee: **each target VM gets exactly two Run Commands, one *invoke* and one *collect***.

## Summary

Azure DevOps (ADO) is good at running pipelines: identity, approvals, scheduling, waiting without an agent, artifacts and reruns. It cannot do what `y2b`/`y2p` do: per-VM prechecks, bundling N steps into one remote payload, the 2-call fire-and-forget protocol and the base64 result contract.

So **do not rewrite `job.yaml` as native ADO steps.** Keep `job.yaml` as the job language, keep `y2b`/`y2p` as the engine, and let ADO run it:

```
Stage Invoke   (agent)      y2b run job.yaml --phase invoke    ← run-command #1 per VM (fire, no wait)
     │   state (run.json + collect_list.jsonl) handed over as a pipeline artifact
     ▼
Stage Wait     (agentless)  Delay@1  N minutes                 ← no agent held, no cost
     ▼
Stage Collect  (agent)      y2b run job.yaml --phase collect   ← run-command #2 per VM (reader)
     ▼
result.csv + result.log published as pipeline artifacts
```

This is the same two-phase design the CLI already uses. The difference is that the wait happens in an **agentless Delay job**, not in a Python heap on a laptop that has to stay awake.

## Documents

| # | File | Read it when you want to know… |
|---|------|--------------------------------|
| 1 | [01-concept-mapping.md](01-concept-mapping.md) | Which `y2` features ADO replaces, which stay in `y2`, and why ADO "checks" and "run command" support do not replace the engine |
| 2 | [02-two-runcommand-pattern.md](02-two-runcommand-pattern.md) | How "exactly 2 run-commands per VM" maps to ADO stages, how state passes between jobs, and the small CLI change it needs (`--phase`) |
| 3 | [03-pipeline-reference.md](03-pipeline-reference.md) | Copy-paste pipeline YAML: a reusable template, a `y2b` pipeline, a `y2p` pipeline, exit-code mapping and artifacts |
| 4 | [04-scale-and-operations.md](04-scale-and-operations.md) | 500+ VMs, ARM throttling, sharding, timeouts, auth expiry, Resource Graph prechecks, reruns and rollout order |

## Adoption levels

| Level | What you do | Code change in this repo | Run-commands per VM | Agent held during the wait? |
|-------|-------------|--------------------------|---------------------|-----------------------------|
| **L1: Wrap** | One ADO job runs `y2b run job.yaml -y` as it does today | None | 2 | Yes (the job sleeps for `sleep_before_collect`) |
| **L2: Split** (recommended) | Invoke job → agentless Delay → Collect job | Add `--phase invoke\|collect` (~40 lines + tests, see doc 02) | 2 | **No** |
| **L3: Template + governance** | L2 packaged as a shared template; Approvals, Business hours, Exclusive lock and Required template checks added | None beyond L2 | 2 | No |

Start with **L1** to prove auth and networking from an ADO agent. Move to **L2** once that works. **L3** is how other teams consume it.

## Out of scope

- Converting `job.yaml` into ADO YAML (`${{ each step }}` → one task per step). This creates one run-command per step per VM, which is the pattern `y2` exists to avoid. See [01-concept-mapping.md §4](01-concept-mapping.md#4-why-not-translate-jobyaml-into-native-ado-steps).
- Installing an ADO agent on every VM (Environment VM resources). This is a valid alternative and is compared in [01-concept-mapping.md §5](01-concept-mapping.md#5-alternatives-considered), but it is a different model.
