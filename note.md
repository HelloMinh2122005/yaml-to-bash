Don't read this page

# Hi claude

I will have to do a huge refactor of y2 in compiling to Azure DevOps, it's just in my thought and I need you to discuss with me on this

Currently associate between these engine and CI CD platform are the ADO agent using Y2 to help compile into the az cli script.
Seems like the Security Architect did not approve this IDEA, and we need the more complicated version: generate all the `ado_pipelines.yaml`

But when I look again in my company's patching, all the job are just only:

## 2 Phases:

### Phase 1: invoke
- Resolving all the VM that need to be patched (using KQL) and Store the list inital PowerState
- Start VM that is not running
- Sending the script to VM and execute, all the result will be store in the file; Using standard az runcommand, but ADO support -Job to run asynconouse
- Revert VM to it's inital tag

### Phase 2: Collect
- Resolving all the VM that need to be patched (using KQL) and Store the list inital PowerState
- Start VM that is not running
- Sending the collect script to VM and execute, collect all the result stored in the file
- Generate a report and push to github repo (with link and PAT key)
- Revert VM to it's inital tag


# My idea

## General
The main idea of y2 is to support this activity: `Sending the collect script to VM and execute`
This is very painful because the script is very long and hard to maintain, that's why y2 is born - Helping people easier in contributing in the script

## Idea 1: Fully supported (I more into this)

support convert in every step from resolving the VM to Generate report and push to repo
People just need to write 1 file `y2_pipelines.yaml` and it fully convert into ADO support pipelines

Currently, please design a version of manualy compile in the local, or prehook or something
`y2_pipelines.yaml` -> manual compile -> `ado_pipelines.yaml`
Then push both of the version into github, ADO just need to run the `ado_pipelines.yaml`

The idea has to also support custom ADO step, for example push to github repo (with link and PAT key)

## Idea 2: Script only

People still have to write the ADO pipeline, but instead of the normal runcommand

current_company_version
```yaml
steps:
- task: AzurePowerShell@5
  displayName: 'doing something with runcommand'
  condition: succeededOrFailed()
  inputs:
    azureSubscription: 'sub...'
    ScriptType: 'FilePath'
    ScriptPath: 'path_to_execute_script.ps1'
    ScriptArguments: >
        -Env '' 
        -SomeFile ''
    errorActionPreference: 'stop'
    workingDirectory: ''
    azurePowerShellVersion: latestVersion
```

my_idea
```yaml
steps:
- task: AzurePowerShell@5
  displayName: 'doing something with runcommand'
  condition: succeededOrFailed()
  inputs: 'path_to_y2_pipelines.yaml'
```


---

No problem is the security architect reject that because it's too hard to control if it fully safe enough, I was adviced: because all of the patching are following the same pattern

Resolve VM list, Start VM, Run script, Stop VM

So why don't we create the template, and let's the engine convert into that template

People would feel more safe when they see the ADO supported pipeline

And one question, why we need the agent to install `pyyaml` and `pytest`?
This is very difficult to ask for custom the agent on Production

The best workaround, is that we can generate the fully ADO supported pipeline, it can be in the local, then push both version to github

ADO agent run on that `ado_pipelines.yaml`

---

1. 
Currently in my company

We have 2 seperate pipelines:

Schedule Patch, create the patch.sh, schedule it and automate execute the script patch.sh (automate run)

Collect and Generate report (manual run)

And in each pipeline, the start and stop VM are the same

Currently, the VM's inital tags are stored in agent's csv

2.
In my idea, I want 2 pipelines, that will match the company current logic, I will adjust later on other usecases

3.
We create and propose the template to the security

---

About the custom step, here, we allow user to write custom ADO pipeline step into the y2 yaml, and when convert, it just Copy Paste

And the custom ADO step must be supported in both invoke and collect

Currently, my company use B, inject the patch.sh into the VM using cat<<... and create a cron

Still of my question is that y2 support the script_path -> what if this A script, call to B and C script, and D and E... 
Can y2 still support that, we need design carefully on this


I have just take a deeper look on my current company design, there's no such thing A script, call to B and C script, and D and E... 

So no need worry on this, I notice there's step update tag on VM, and it will need many csv file as references, is it hard to integrate as feature of y2, or we could right now keep it as custom step

---

I think just leave the tag as custom step

About your concern, VM will be start automatically on the tag maintance widows, that's FinOps job, not our

---

@y2-tech-lead

Owner hand-off (agent-chat may be down, so this replaces queued messages #43 and #44; ignore them if they arrive later).
There are two requests, both at stage PLAN_READY with the baton on you. Cold start: re-read each state.json before acting, and never write state.json from an older copy. R1's state.json was overwritten once already with a stale DRAFTING copy and has since been restored.

==================================================================
REQUEST 1: [PLAN_READY] 20261108-ado-patch-templates · plan v2 · mode full · cycle 0/2
dir: .handoffs/20261108-ado-patch-templates/
read: state.json, 20-spec.md, 21-plan.md, 22-tasks.md, 23-test-plan.md, adr/ADR-001..005
(11-research-findings.md is reference only. Its R-01/R-04 REST details are superseded by ADR-001 rev 2.)

What it is:
- Security rejected running the y2 engine (Python) on ADO agents.
- We ship a fixed ADO extends-template package. Security reviews it once, and y2 later only fills its parameters (R2).
- Two pipelines:
  invoke (scheduled):  Resolve → before_start → Start → Run → after_run → Restore → finalize → publish → after_restore
  collect (manual):    the same sequence; Run saves per-VM stdout/stderr, and finalize writes status.csv.

Fixed runtime (ADR-001 rev 2):
- AzurePowerShell@5 with pwsh: false on pool "Production Windows Agent" (Windows PowerShell 5.1). Nothing is installed on the agent.
- Use only the cmdlets the company already runs: Search-AzGraph (-First 1000 / -SkipToken, order by name asc), Get-AzVM -Status, Start-AzVM -NoWait, Stop-AzVM [-StayProvisioned] -Force -NoWait, and Invoke-AzVMRunCommand -AsJob with at most maxJobs (default 20) jobs running at once.

Rules that must hold:
- ADR-003: Restore only acts on VMs this run started (started_by_run = submitted or true) and returns each to its verified_power. A VM that was running at snapshot time is never stopped. Restore, finalize and publish run with condition: always(); cancelTimeoutInMinutes is 10.
- INV-11: a busy (409) VM is retried on a later pass and never blocks other VMs. A 429 backs off the whole pool. 5 consecutive auth failures trip the breaker.
- ADR-002: the templates are payload-agnostic. They never parse y2 markers.
- ADR-001 hook allowlist, accepted by the owner: checkout, download, publish, powershell, PowerShell@2, PublishPipelineArtifact@1, DownloadPipelineArtifact@2, and AzurePowerShell@5 with the same service connection. Any other step fails ADO compilation. Allowed hook steps are spliced verbatim, and the templates add no condition to them.
- Everything new goes under ado/ and tests/. These paths must stay byte-unchanged: y2shared/, y2b/, y2p/, pipelines/, tests/golden/, lab/.

next:
- Start with T-00 (docs-only cmdlet research: -AsJob on PS 5.1, how 409/429 errors appear, Search-AzGraph paging, -NoWait). T-01 depends on it.
- Then implement T-01..T-17 and run TP-01..TP-14.
- TP-14 (container T3, ADR-004 token login; the SP secret never leaves run-lab.sh) runs only through lab/run-lab.sh and must end with all 5 VMs deallocated.
- T-18/TP-15 (the real ADO lab run) waits until REQUEST 2 is DONE.
- Send DELIVERY when T-00..T-17 are green, with T-18 marked pending-infra in 40-delivery-report.md.

==================================================================
REQUEST 2: [PLAN_READY] 20261108-lab-ado-agent · plan v1 · mode lite · cycle 0/2
dir: .handoffs/20261108-lab-ado-agent/
read: state.json, 00-brief.md, 20-lite-plan.md
(10-research-plan.md and RESEARCH_REQUEST #44 are withdrawn. There is no research round.)

What it is:
- A production-like ADO agent for R1's AC-15/T-18.
- The owner decided that only 5 VMs are allowed, so win01 (WS2022, PS 5.1) hosts the self-hosted agent.
- The agent goes in a lab pool named exactly "Production Windows Agent", so the R1 templates run unchanged.
- win01 is excluded from patch targets.
- No Terraform: lab_guard.py, .claude/settings.json and CLAUDE.md stay unchanged.

Credentials:
- They are in ~/.config/y2-lab/ado.env: ADO_ORG_URL, ADO_PROJECT, ADO_PAT.
- Never print, log, commit or copy them into evidence.
- Pass the PAT only by env or as a run-command parameter, with set +x around it.

next:
- Implement T-01..T-04 (lab/ado/install-agent.ps1, setup-agent.sh, ado-run.sh, smoke.yml, session.sh, plus a lab/README.md section) and run TP-01..TP-05.
- Make Azure calls only via lab/run-lab.sh. ADO calls are curl to $ADO_ORG_URL only.
- Every lab session starts all 5 VMs and ends with all 5 deallocated, including on failure.
- Send DELIVERY when done. That unblocks REQUEST 1's T-18.

==================================================================
Order: run both requests in parallel. Lab safety rules for both: subscription 79f7da80-0c64-4e4b-9ed4-316a88142152, RG rg-lab-10vms, every run ends with all 5 VMs deallocated, and no secrets anywhere in the repo, logs or evidence.
When you send DELIVERY, the owner relays it to claude-code/y2-lead (/y2-lead) for the evidence check.

==__==__==__==__

Resume verification for Request 2 (`20261108-lab-ado-agent`) per `.handoffs/20261108-lab-ado-agent/42-evidence/checkpoint.md` and `20-lite-plan.md`.

### Current State
- Agent pool `Production Windows Agent` exists in ADO (ID: 11).
- **TP-01 is COMPLETED:** `win01` agent is provisioned and registered in pool `Production Windows Agent`. Versions captured in `.handoffs/20261108-lab-ado-agent/42-evidence/tp-01/versions.json` (PS 5.1, Az.Accounts, Az.Compute, Az.ResourceGraph).
- **TP-04 & TP-05 are COMPLETED:** 0 secrets leaked, shell syntax verified.
- All 5 lab VMs are currently deallocated.

### Next Action Required
Execute remaining verification tests TP-02 and TP-03:

1. **TP-02 (Smoke Pipeline):**
   ```bash
   lab/run-lab.sh --evidence .handoffs/20261108-lab-ado-agent/42-evidence/tp-02/ -- lab/ado/session.sh lab/ado/ado-run.sh lab/ado/smoke.yml --evidence-dir .handoffs/20261108-lab-ado-agent/42-evidence/tp-02/
   ```
   - Verify exit 0.
   - Confirm downloaded logs show PS 5.1, Az module versions, and 5 VM names in `rg-lab-10vms`.
   - Confirm all 5 VMs are deallocated in `post-state.txt`.

2. **TP-03 (Cold Start):**
   Rerun the TP-02 command starting from deallocated state:
   ```bash
   lab/run-lab.sh --evidence .handoffs/20261108-lab-ado-agent/42-evidence/tp-03/ -- lab/ado/session.sh lab/ado/ado-run.sh lab/ado/smoke.yml --evidence-dir .handoffs/20261108-lab-ado-agent/42-evidence/tp-03/
   ```
   - Verify agent auto-starts and comes online within 5 minutes.
   - Verify pipeline succeeds and all 5 VMs are deallocated.

3. **Final Secrets Scan:**
   Verify zero PAT/secret leaks across `.handoffs/20261108-lab-ado-agent/42-evidence/`.