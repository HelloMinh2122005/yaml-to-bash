# 03: Pipeline reference (copy-paste YAML)

All examples assume:

- The `yaml_to_bash` repo (this one) is the pipeline's `self` repo, and job folders live under `examples/` or a new `jobs/` folder. If jobs live in another repo, add this repo under `resources.repositories` and `checkout` both. The `bin/` path below then becomes `$(Pipeline.Workspace)/s/yaml_to_bash/bin`.
- Agents are **Linux** (`ubuntu-latest`, or a self-hosted Linux pool inside your network). `y2p` runs on Linux too, because host prechecks and computed columns are bash.
- `output_csv` / `output_log` in `job.yaml` sit next to `job.yaml` (as in both examples). The parts dir is then `<job dir>/.<csv stem>.parts`.
- [Level 2](README.md#adoption-levels) uses the proposed `--phase` flag from [02 §3](02-two-runcommand-pattern.md#3-the-one-cli-change-needed---phase). [§4](#4-level-1-single-job-no-code-change) works today without it.

Suggested layout:

```
pipelines/
├── templates/
│   ├── y2-setup.yml          # steps: venv + PATH
│   └── y2-fleet-run.yml      # stages: Validate → Fleet (Invoke → Wait → Collect [→ Wait2 → Collect2])
├── hip-compliance.yml        # y2b example consumer
└── windows-baseline.yml      # y2p example consumer
```

## 1. One-time ADO setup

| Item | Setting | Why |
|------|---------|-----|
| **ARM service connection** (e.g. `sc-vm-fleet-prod`) | Workload identity federation; scope = management group or each subscription in the inventory | Replaces `az login`. The identity needs `Microsoft.Compute/virtualMachines/runCommand/action` + `read` (e.g. *Virtual Machine Contributor*, or a custom role with just those) |
| **Environment** `vm-fleet-prod` | Checks: **Approvals**, **Business hours**, **Exclusive lock** | Gates only the Invoke job (the one that changes anything). Collect is not gated, so a collect never waits for a second approval |
| Pipeline setting | `lockBehavior: sequential` at the top of the consumer pipeline | Used by Exclusive lock: queued runs wait instead of overlapping on the same VMs |
| Optional: **Required template** check on the Environment | Template = `pipelines/templates/y2-fleet-run.yml@<repo>` | Only pipelines built from this template can target the fleet |

## 2. Setup steps template

```yaml
# pipelines/templates/y2-setup.yml
steps:
  - bash: |
      set -euo pipefail
      VENV="$(Agent.TempDirectory)/y2venv"
      python3 -m venv "$VENV"                       # Ubuntu 24.04 blocks system-wide pip (PEP 668)
      "$VENV/bin/pip" install --quiet 'PyYAML>=6.0'
      echo "##vso[task.prependpath]$VENV/bin"       # bin/y2b execs `python3` → the venv one
      echo "##vso[task.prependpath]$(Build.SourcesDirectory)/bin"
      python3 --version; bash --version | head -1; az version -o tsv --query '"azure-cli"'
    displayName: Set up y2b / y2p
```

## 3. The reusable template

```yaml
# pipelines/templates/y2-fleet-run.yml
parameters:
  - name: tool                      # y2b = Linux fleet, y2p = Windows fleet
    type: string
    values: [y2b, y2p]
  - name: jobFile                   # repo-relative path to job.yaml
    type: string
  - name: serviceConnection
    type: string
  - name: environment               # ADO Environment carrying Approvals / Business hours / Exclusive lock
    type: string
  - name: delayMinutes              # MUST be >= sleep_before_collect in job.yaml
    type: number
  - name: recollectDelayMinutes     # 0 = no second collect for ERROR:still_running VMs
    type: number
    default: 0
  - name: parallel                  # 0 = use job.yaml `parallel`
    type: number
    default: 0
  - name: vmFilter                  # optional list of VM names (same as repeated --vm)
    type: object
    default: []
  - name: resultFiles               # relative to the job dir; match output_csv / output_log
    type: object
    default: [result.csv, result.log]
  - name: pool
    type: object
    default:
      vmImage: ubuntu-latest

stages:
  # ------------------------------------------------------------------ Validate
  - stage: Validate
    jobs:
      - job: Validate
        pool: ${{ parameters.pool }}
        steps:
          - checkout: self
          - template: y2-setup.yml
          - bash: |
              set -euo pipefail
              ${{ parameters.tool }} validate "${{ parameters.jobFile }}"
              mkdir -p "$(Build.ArtifactStagingDirectory)/payload"
              ${{ parameters.tool }} compile "${{ parameters.jobFile }}" \
                -o "$(Build.ArtifactStagingDirectory)/payload/remote.${{ replace(replace(parameters.tool, 'y2b', 'sh'), 'y2p', 'ps1') }}"
            displayName: Validate job + compile payload
          - publish: $(Build.ArtifactStagingDirectory)/payload
            artifact: y2-payload               # reviewers can read exactly what will run on the VMs

  # ------------------------------------------------------------------ Fleet
  - stage: Fleet
    dependsOn: Validate
    variables:
      Y2_JOB: ${{ parameters.jobFile }}
      Y2_VM_FILTER: ${{ join(' ', parameters.vmFilter) }}
      Y2_PARALLEL: ${{ parameters.parallel }}
    jobs:
      # ---------- Phase 1: run-command #1 (runCommand POST, no wait) ----------
      - deployment: Invoke
        environment: ${{ parameters.environment }}     # ← Approvals / Business hours / Exclusive lock
        pool: ${{ parameters.pool }}
        timeoutInMinutes: 120
        strategy:
          runOnce:
            deploy:
              steps:
                - download: none
                - checkout: self
                - template: y2-setup.yml
                - task: AzureCLI@2
                  name: fire
                  displayName: Phase 1 · invoke (run-command 1 of 2)
                  inputs:
                    azureSubscription: ${{ parameters.serviceConnection }}
                    scriptType: bash
                    scriptLocation: inlineScript
                    inlineScript: |
                      set -uo pipefail
                      args=(); for v in $Y2_VM_FILTER; do args+=(--vm "$v"); done
                      [ "$Y2_PARALLEL" -gt 0 ] && args+=(--parallel "$Y2_PARALLEL")
                      out="$(Agent.TempDirectory)/invoke.out"
                      ${{ parameters.tool }} run "$Y2_JOB" --phase invoke -y "${args[@]}" | tee "$out"
                      rc=${PIPESTATUS[0]}
                      fired=$(sed -n 's/^fired=//p' "$out" | tail -1)
                      echo "##vso[task.setvariable variable=fired;isOutput=true]${fired:-0}"
                      exit "$rc"
                - bash: |
                    set -euo pipefail
                    cd "$(dirname "$Y2_JOB")"
                    if ls -d .*.parts >/dev/null 2>&1; then
                      tar -czf "$(Build.ArtifactStagingDirectory)/y2-state.tgz" .*.parts
                      echo "##vso[task.setvariable variable=hasState]true"
                    fi
                  displayName: Pack run state
                  condition: always()
                - publish: $(Build.ArtifactStagingDirectory)/y2-state.tgz
                  artifact: y2-state-invoke
                  displayName: Publish run state (for Collect)
                  condition: and(succeeded(), eq(variables.hasState, 'true'))
                - publish: $(Build.ArtifactStagingDirectory)/y2-state.tgz
                  artifact: y2-state-invoke-failed-$(System.JobAttempt)
                  displayName: Publish run state (for debugging)
                  condition: and(failed(), eq(variables.hasState, 'true'))

      # ---------- Wait: agentless, holds no agent ----------
      - job: Wait
        dependsOn: Invoke
        condition: and(succeeded(), ne(dependencies.Invoke.outputs['Invoke.fire.fired'], '0'))
        pool: server
        timeoutInMinutes: 240                          # must exceed delayMinutes (agentless default is 60)
        steps:
          - task: Delay@1
            inputs:
              delayForMinutes: '${{ parameters.delayMinutes }}'

      # ---------- Phase 2: run-command #2 (reader) + merge ----------
      - job: Collect
        dependsOn: [Invoke, Wait]
        # Collect still runs when nothing was fired (all SKIPPED/ENDED), so result.csv is always produced
        condition: and(eq(dependencies.Invoke.result, 'Succeeded'), in(dependencies.Wait.result, 'Succeeded', 'Skipped'))
        pool: ${{ parameters.pool }}
        timeoutInMinutes: 120
        steps:
          - checkout: self
          - template: y2-setup.yml
          - download: current
            artifact: y2-state-invoke
          - bash: tar -xzf "$(Pipeline.Workspace)/y2-state-invoke/y2-state.tgz" -C "$(dirname "$Y2_JOB")"
            displayName: Restore run state
          - task: AzureCLI@2
            name: collect
            displayName: Phase 2 · collect (run-command 2 of 2) + merge
            inputs:
              azureSubscription: ${{ parameters.serviceConnection }}
              scriptType: bash
              scriptLocation: inlineScript
              inlineScript: |
                set -uo pipefail
                args=(); for v in $Y2_VM_FILTER; do args+=(--vm "$v"); done
                [ "$Y2_PARALLEL" -gt 0 ] && args+=(--parallel "$Y2_PARALLEL")
                ${{ parameters.tool }} run "$Y2_JOB" --phase collect -y "${args[@]}"
                rc=$?
                # parts dir survives the merge only when VMs are still running
                if ls -d "$(dirname "$Y2_JOB")"/.*.parts >/dev/null 2>&1; then still=true; else still=false; fi
                echo "##vso[task.setvariable variable=stillRunning;isOutput=true]$still"
                if [ "$rc" -eq 2 ]; then
                  echo "##vso[task.logissue type=warning]Some VMs are not OK (SKIPPED/ENDED/ERROR). See result.csv."
                  echo "##vso[task.complete result=SucceededWithIssues;]"
                  rc=0
                fi
                exit "$rc"
          - bash: |
              set -euo pipefail
              JOB_DIR="$(dirname "$Y2_JOB")"; OUT="$(Build.ArtifactStagingDirectory)/results"; mkdir -p "$OUT"
              for f in ${{ join(' ', parameters.resultFiles) }}; do cp "$JOB_DIR/$f" "$OUT/"; done
              if [ "$(collect.stillRunning)" = "true" ]; then
                (cd "$JOB_DIR" && tar -czf "$(Build.ArtifactStagingDirectory)/y2-state.tgz" .*.parts)
              fi
            displayName: Stage results
          - publish: $(Build.ArtifactStagingDirectory)/results
            artifact: y2-results
          - publish: $(Build.ArtifactStagingDirectory)/y2-state.tgz
            artifact: y2-state-collect
            condition: and(succeeded(), eq(variables['collect.stillRunning'], 'true'))

      # ---------- Optional: one more reader for ERROR:still_running VMs only ----------
      - ${{ if gt(parameters.recollectDelayMinutes, 0) }}:
        - job: Wait2
          dependsOn: Collect
          condition: and(succeeded(), eq(dependencies.Collect.outputs['collect.stillRunning'], 'true'))
          pool: server
          timeoutInMinutes: 240
          steps:
            - task: Delay@1
              inputs:
                delayForMinutes: '${{ parameters.recollectDelayMinutes }}'
        - job: Collect2
          dependsOn: Wait2
          pool: ${{ parameters.pool }}
          steps:
            - checkout: self
            - template: y2-setup.yml
            - download: current
              artifact: y2-state-collect
            - bash: tar -xzf "$(Pipeline.Workspace)/y2-state-collect/y2-state.tgz" -C "$(dirname "$Y2_JOB")"
              displayName: Restore run state
            - task: AzureCLI@2
              displayName: Re-collect still-running VMs
              inputs:
                azureSubscription: ${{ parameters.serviceConnection }}
                scriptType: bash
                scriptLocation: inlineScript
                inlineScript: |
                  set -uo pipefail
                  args=(); for v in $Y2_VM_FILTER; do args+=(--vm "$v"); done
                  ${{ parameters.tool }} run "$Y2_JOB" --phase collect -y "${args[@]}"
                  rc=$?
                  [ "$rc" -eq 2 ] && { echo "##vso[task.complete result=SucceededWithIssues;]"; rc=0; }
                  exit "$rc"
            - bash: |
                set -euo pipefail
                JOB_DIR="$(dirname "$Y2_JOB")"; OUT="$(Build.ArtifactStagingDirectory)/results"; mkdir -p "$OUT"
                for f in ${{ join(' ', parameters.resultFiles) }}; do cp "$JOB_DIR/$f" "$OUT/"; done
              displayName: Stage results
            - publish: $(Build.ArtifactStagingDirectory)/results
              artifact: y2-results-final
```

Notes on the template:

- **The same `--vm` filter must be passed to both phases.** Part-file names use the row index padded to the width of the *target* row count ([parts.py `get_part_paths`](../y2shared/parts.py)), so a different filter in Collect would not find Invoke's part files. The stage-level `Y2_VM_FILTER` variable keeps them identical.
- **The same commit runs in every job** of one pipeline run, so `job_hash` matches and `--phase collect` accepts the state. If someone edits `job.yaml` and starts a *new* run, that run gets its own state, so runs cannot mix.
- Deployment-job output variables are addressed as `dependencies.<job>.outputs['<job>.<step>.<var>']` (the job name appears twice). This is how ADO addresses `runOnce` deployment jobs.
- `Delay@1` only runs in an agentless job (`pool: server`), which is what makes the wait free.

## 4. Level 1: single job, no code change

Use this today to prove the service connection, network path and permissions. It already does 2 run-commands per VM. The cost is an agent held during `sleep_before_collect`.

```yaml
# pipelines/hip-compliance-l1.yml
trigger: none
pool:
  vmImage: ubuntu-latest

jobs:
  - job: Run
    timeoutInMinutes: 180            # > invoke time + sleep_before_collect + collect time
    steps:
      - checkout: self
      - template: templates/y2-setup.yml
      - task: AzureCLI@2
        displayName: y2b run (invoke + wait + collect in one process)
        inputs:
          azureSubscription: sc-vm-fleet-prod
          scriptType: bash
          scriptLocation: inlineScript
          inlineScript: |
            set -uo pipefail
            y2b run examples/hip-compliance/job.yaml -y
            rc=$?
            [ "$rc" -eq 2 ] && { echo "##vso[task.complete result=SucceededWithIssues;]"; rc=0; }
            exit "$rc"
      - publish: examples/hip-compliance/result.csv
        artifact: y2-result-csv
        condition: succeededOrFailed()
      - publish: examples/hip-compliance/result.log
        artifact: y2-result-log
        condition: succeededOrFailed()
```

## 5. Consumer pipelines (Level 2/3)

### Linux fleet (`y2b`)

```yaml
# pipelines/hip-compliance.yml
name: hip-compliance-$(Date:yyyyMMdd)$(Rev:.r)
trigger: none
lockBehavior: sequential
schedules:
  - cron: "0 1 * * 1"                # Mondays 01:00 UTC
    displayName: Weekly HIP compliance
    branches: { include: [main] }
    always: true

parameters:
  - name: vmFilter
    displayName: Only these VMs (empty = whole inventory)
    type: object
    default: []

extends:
  template: templates/y2-fleet-run.yml
  parameters:
    tool: y2b
    jobFile: examples/hip-compliance/job.yaml
    serviceConnection: sc-vm-fleet-prod
    environment: vm-fleet-prod
    delayMinutes: 5                  # job.yaml: sleep_before_collect: "5m"
    recollectDelayMinutes: 15
    vmFilter: ${{ parameters.vmFilter }}
```

### Windows fleet (`y2p`)

```yaml
# pipelines/windows-baseline.yml
trigger: none
lockBehavior: sequential

extends:
  template: templates/y2-fleet-run.yml
  parameters:
    tool: y2p                        # → RunPowerShellScript; agent is still Linux
    jobFile: examples/windows-baseline/job.yaml
    serviceConnection: sc-vm-fleet-prod
    environment: vm-fleet-prod
    delayMinutes: 3                  # job.yaml: sleep_before_collect: "3m"
```

Using `extends:` (rather than `stages: - template:`) is what lets the **Required template** check enforce that every fleet run goes through this template.

## 6. Exit codes → ADO results

| `y2` exit | Meaning | Template turns it into |
|-----------|---------|------------------------|
| `0` | All VMs OK | ✅ Succeeded |
| `1` | YAML / CSV / config invalid | ❌ Failed (Validate stage normally catches this first) |
| `2` | Run finished; some rows `SKIPPED` / `ENDED` / `ERROR` | ⚠️ **SucceededWithIssues** + warning annotation; results still published |
| `3` | Aborted (auth breaker, interrupted, merge error) | ❌ Failed; state artifact published for debugging or rerun |

## 7. Optional: per-VM results in the Tests tab

ADO's **Tests** tab gives a searchable pass/fail view per VM with history across runs. A ~20-line converter that turns `result.csv` into JUnit XML (one `<testcase name="vm-app-01">` per row; `<failure>` when any column starts with `ERROR:`; `<skipped>` for `SKIPPED:`/`ENDED:`), followed by:

```yaml
- task: PublishTestResults@2
  condition: succeededOrFailed()
  inputs:
    testResultsFormat: JUnit
    testResultsFiles: $(Build.ArtifactStagingDirectory)/results/junit.xml
    testRunTitle: $(Build.DefinitionName) fleet results
```

The converter would belong in `y2shared/` as a `y2b report --junit` subcommand, so it is tested like the rest of the engine. It is not required for the pattern to work.
