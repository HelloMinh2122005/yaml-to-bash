# y2 squad — Claude plans, Antigravity builds (design)

Date: 2026-10-31 · Status: draft for approval · Repo: `yaml_to_bash` (y2b / y2p)

## 1. Intent

The owner wants a two-platform agent squad that lives inside this repo and does all feature work on y2b/y2p:

- **Claude squad — thinks.** Frames requests, writes the spec, the implementation plan, **research plans** and **test plans**. Never writes code and never reviews code.
- **Antigravity squad — engineers.** Implements, carries out research (docs, code, and live probes on the Azure lab), reviews its own code, debugs, and runs every test tier including **real Azure**.

Why: the codebase has sharp edges (500-VM concurrency, resume, Python 3.9, byte-identical goldens, PS 5.1 payloads) and no real-cloud test tier yet. Planning on Claude and execution on Gemini Flash splits the cost the way the owner wants and forces every change through a written plan and a written test plan.

`yaml_to_bash` is a separate product from Occupify; it only sits under `~/Downloads/Occupify/` for now and will move. The squad must be **self-contained in this repo** and must not depend on anything in the Occupify root.

## 2. Goals, non-goals, anti-goals

**Goals**
- G1. Every request goes request → plan (+ research plan when there are unknowns) → owner approval → build → three test tiers → delivery → owner sign-off, with files as the only source of truth.
- G2. A real-Azure test tier on the owner's lab, run by Antigravity without the owner triggering each run, once the owner has approved the test plan.
- G3. Small requests stay cheap (lite mode).
- G4. The lab never runs up cost: every real-Azure run ends with all lab VMs deallocated.

**Non-goals**
- Building the real-Azure test harness itself in this project. It is the squad's **first request** (see §10).
- Planning around the in-flight v4 work (`IMPLEMENT_PLAN_v4.md`).
- CI integration, Azure DevOps pipelines.

**Anti-goals** (failure even if everything "works")
- An agent touches anything in Azure outside the lab subscription + `rg-lab-10vms`.
- Any agent runs `terraform` (apply, destroy, plan — any of it), or reads `lab/terraform.tfvars` / `*.tfstate`.
- Claude agents write code, or Antigravity agents edit the plan.
- The squad depends on Occupify-root files (`.handoffs/PROTOCOL.md`, `chief-architect`, its hooks).

## 3. Constraints (from the repo)

- Runtime deps: Python stdlib + PyYAML only. Python 3.9 compatible (no `match`, no `X | Y` outside annotations, no `zip(strict=)`, no `dataclass(slots=/kw_only=)`).
- Goldens `tests/golden/hip_compliance_remote.sh` and `tests/golden/windows_baseline_remote.ps1` must stay byte-identical unless a plan explicitly changes them.
- Host side must tolerate macOS bash 3.2 (`y2shared/hostenv.py`); targets are RHEL 8/9, Oracle Linux 8, Windows Server 2016/2022 running **Windows PowerShell 5.1** (the PowerShell container test runs pwsh 7, so only the lab exercises real 5.1).
- Lab (`lab/README.md`): subscription `79f7da80-0c64-4e4b-9ed4-316a88142152`, RG `rg-lab-10vms`, VMs `lin01–lin03`, `win01–win02`, Azure for Students quota. Power control via `lab/toogle_vm.sh` (`stop` = deallocate). Terraform is owned by the owner.
- agent-chat: nobody listens live; the owner starts every agent turn (`/check-message <name>`). Every hop costs a human trigger.

## 4. Approaches considered

| | Approach | Trade-off |
|---|---|---|
| **A** | Adapt the proven Occupify contract: locked `PROTOCOL.md`, one folder per request, `state.json` baton, typed lead-to-lead messages, loop caps → escalation, write-guard hooks on both sides. Strip Occupify specifics; add a research stage and real-Azure evidence. | More ceremony per request; proven, and the owner already operates it. |
| B | Lighter contract from scratch: numbered files + a few message types, no baton or caps. | Cheaper for tiny changes; nothing stops a non-converging loop or records where a request stands. |
| C | A + a **lite mode** (lead plans alone, no research hop). | A few extra rules. |

**Chosen: A with C's lite mode** (owner's pick, 2026-10-31). Most y2 requests are small (a validator rule, a doc fix); v4-sized features still get the full flow.

## 5. Roster

Names are prefixed `y2-` so agent-chat addresses don't collide with Occupify's squads.

### 5.1 Claude squad — `.claude/agents/`

| Agent | Model / effort | Job | Writes |
|---|---|---|---|
| `y2-lead` | `claude-opus-5-5` / high | Frames the request, picks lite/full, writes brief + spec + decisions, merges helper drafts, holds the owner gates, answers `CLARIFY`/`BLOCKED`, does the **evidence check** on deliveries (§6.4). **Only Claude agent on agent-chat.** Runs as the main session. | Claude handoff files, `state.json` when holding the baton, `.claude/squad-learnings.md` |
| `y2-planner` | `claude-sonnet-5-5` / high | Implementation plan, invariants, task cards. High because runner/dispatcher/resume are concurrency-heavy. | `21-plan.md`, `22-tasks.md` drafts |
| `y2-research-planner` | `claude-sonnet-5-5` / medium | Turns unknowns into research briefs: question, why it matters, method (docs / code / lab probe), exact commands for lab probes, evidence expected, stop condition. | `10-research-plan.md` draft |
| `y2-test-planner` | `claude-sonnet-5-5` / high | Three-tier test plan per request (§7): which tier proves which AC, golden/py3.9/bash-3.2/PS-5.1 checks, lab run matrix, cost budget. | `23-test-plan.md` draft |

Helpers are spawned by `y2-lead` only (`tools: Agent(y2-planner, y2-research-planner, y2-test-planner), …`). Helpers get `Read, Grep, Glob, Write` (+ `WebSearch, WebFetch` for the research planner); no `Bash`, no `Edit` on code, no `mcp__*`.

### 5.2 Antigravity squad — `.agents/plugins/y2/agents/`

All `model: flash`, `subagent: true`, with the house convention line **"Reasoning Effort Profile: HIGH — Gemini 3.8 Flash at High thinking."**

| Agent | Job | Writes |
|---|---|---|
| `y2-tech-lead` | Intake (Definition of Ready), sequences and delegates cards, runs the internal QA gate, writes the delivery report. Orchestrates; does not code. **Only Antigravity agent on agent-chat.** | `30-questions.md`, `40-delivery-report.md`, `41-diff.patch`, `state.json` when holding the baton |
| `y2-engineer` | Implements cards in `y2shared/`, `y2b/`, `y2p/`, `bin/`, examples, docs. | code + docs in scope of the card |
| `y2-researcher` | Executes `10-research-plan.md`: Microsoft docs, the codebase, and lab probes under the lab guard (§8). | `11-research-findings.md`, `42-evidence/research/` |
| `y2-test-engineer` | Writes and runs tier 1 (pytest + fake_az) and tier 2 (containers). | `tests/**`, `42-evidence/t1`, `t2` |
| `y2-azure-tester` | Runs tier 3 on the lab through the lab runner (§8). Writes tier-3 test code only if the test plan says so. | tier-3 tests, `42-evidence/t3` |
| `y2-reviewer` | Reviews every change against the plan and the repo constraints (§3) before QA passes. **Never edits.** Different agent from the author. | `43-code-review-cN.md` |
| `y2-debugger` | Root-causes any failing tier (incl. flaky lab runs), writes the RCA, applies the fix; the fix goes back through `y2-reviewer`. | code fix + RCA in `44-debug-log.md` |

**Entry points.** Claude: `/y2-lead` (inbox) · `/y2-lead plan <id> <brief>` · `/y2-lead status`, or `claude --agent y2-lead`. Antigravity: `/check-message y2-tech-lead` or `@y2-tech-lead` (same as the Occupify AI squad). Antigravity has no `settings.json`-style deny list, so its restrictions live in `hooks.json` scripts only.

## 6. Contract — `.handoffs/PROTOCOL.md` (locked, owner-only)

Adapted from Occupify's protocol. Both sides read it every turn; hooks block writes to it.

### 6.1 Addresses
`claude-code/y2-lead` ⇄ `antigravity/y2-tech-lead`. No other agent uses the queue.

### 6.2 Folder and files — `.handoffs/<request-id>/`
`<request-id>` = `YYYYMMDD-<kebab-slug>` (no Jira here).

| File | Writer | Content |
|---|---|---|
| `state.json` | baton holder | stage, mode, baton, plan version, counters |
| `00-brief.md` | Claude | problem, success, non-goals, constraints |
| `10-research-plan.md` | Claude | research items `R-nn` (§5.1) |
| `11-research-findings.md` | Antigravity | answer + evidence per `R-nn` |
| `adr/ADR-NNN-*.md` | Claude | decisions |
| `20-spec.md` | Claude | WHAT/WHY, acceptance criteria `AC-n` |
| `21-plan.md` | Claude | HOW, invariants `INV-n`, changelog |
| `22-tasks.md` | Claude | task cards `T-nn` |
| `23-test-plan.md` | Claude | test items `TP-nn` per tier, lab run matrix + budget |
| `30-questions.md` | Antigravity | `Q-nn` (clarify) / `B-nn` (blocked) |
| `31-answers.md` | Claude | answers by id |
| `40-delivery-report.md` | Antigravity | per §6.5 |
| `41-diff.patch` | Antigravity | `git diff <base>...<branch>` |
| `42-evidence/` | Antigravity | test logs per tier, research probe logs, deallocation proof |
| `43-code-review-cN.md` | Antigravity (`y2-reviewer`) | findings `F-nn` + verdict |
| `44-debug-log.md` | Antigravity (`y2-debugger`) | RCA per failure |
| `50-acceptance-cN.md` | Claude | evidence-check verdict |
| `90-retro.md` | Claude | retro with a "For Antigravity" section |

Lite mode merges `00/20/21/22/23` into one `20-lite-plan.md` (brief, ACs, cards, test plan).

**Write allowlists (hook-enforced, both sides):**
- Antigravity `protect-handoff.sh`: inside `.handoffs/` only the Antigravity files of the table above (`11`, `30`, `40`, `41`, `42-evidence/**`, `43`, `44`, and `state.json`); always deny `.handoffs/PROTOCOL.md`, `.claude/**`, `docs/superpowers/**`. Code, tests and user docs (`README.md`, `USER_GUIDE.md`, `az_devops/`) are allowed.
- Claude `guard-y2-writes.sh`: each Claude role writes only its own Claude-owned files in `.handoffs/<id>/` (helpers only their draft files), `y2-lead` also `.claude/squad-learnings.md`; everything else denied, including all code, tests, `lab/`, and `PROTOCOL.md`. The inline lead session has no `agent_type`, so the `/y2-lead` skill passes `y2-lead` as the hook argument (same trick as Occupify's `/claude-ai-lead`).

### 6.3 Stages

```
DRAFTING ─┬─(unknowns)→ AWAITING_RESEARCH_APPROVAL* → RESEARCHING → DRAFTING
          └→ AWAITING_PLAN_APPROVAL → PLAN_READY → [CLARIFYING] → IMPLEMENTING
             → INTERNAL_QA (review · debug · T1 · T2 · T3) → DELIVERED
             → [CHANGES_REQUESTED → IMPLEMENTING …] → AWAITING_SIGNOFF → DONE
Any cap hit / unresolvable BLOCKED → ESCALATED (owner decides)
* only when the research plan contains a lab probe; docs/code-only research skips this gate
```

| Stage | Baton |
|---|---|
| DRAFTING, CLARIFYING, DELIVERED | `claude-code/y2-lead` |
| RESEARCHING, PLAN_READY, IMPLEMENTING, INTERNAL_QA, CHANGES_REQUESTED | `antigravity/y2-tech-lead` |
| AWAITING_RESEARCH_APPROVAL, AWAITING_PLAN_APPROVAL, AWAITING_SIGNOFF, ESCALATED | owner |

Caps in `state.json`: `max_research_rounds 2`, `max_clarify_rounds 2`, `max_internal_fix_cycles 3`, `max_acceptance_cycles 2`. Counters only go up. Pass the baton = write `state.json`, then send.

**Owner gates:** research approval (lab probes only), plan approval (covers the test plan — this is the approval that authorises tier-3 runs), final sign-off.

### 6.4 Messages (≤2000 chars, envelope as Occupify: type, request-id, plan version, `read:` files, ≤5-line summary, `next:`)

| Type | From → To | When |
|---|---|---|
| `RESEARCH_REQUEST` | Claude → AG | research plan ready (approved if it has lab probes) |
| `RESEARCH_DONE` | AG → Claude | `11-research-findings.md` written |
| `PLAN_READY` | Claude → AG | owner approved plan + test plan |
| `CLARIFY` / `BLOCKED` | AG → Claude | DoR fails / plan wrong mid-build (batched) |
| `ANSWER` / `PLAN_AMENDMENT` | Claude → AG | plan unchanged / plan changed (`plan_version`+1) |
| `DELIVERY` | AG → Claude | internal QA passed (review clean, all required tiers green, lab deallocated) |
| `ACCEPTANCE_RESULT` | Claude → AG | evidence gaps → `CHANGES_REQUESTED` |
| `DONE` | Claude → AG | evidence complete **and** owner signed off |
| `ESCALATED` | either | cap hit |

No acknowledgement-only messages. Cold start every turn. A message that doesn't match `state.json` is answered with the mismatch.

**Evidence check (my call):** Claude does not review code. `y2-lead` checks only that every `AC-n` and every `TP-nn` has evidence in `40-delivery-report.md`/`42-evidence/`, that deviations are declared, and that the tier-3 run ended with all lab VMs deallocated. Gaps → `CHANGES_REQUESTED`; otherwise → owner sign-off.

### 6.5 Delivery report minimum
Plan version · branch + HEAD sha · card status · AC → evidence table · TP → result per tier (counts, durations) · reviewer verdict + open findings · debugger RCAs · deviations (or "none") · lab run summary (VMs touched, start/end power states, wall time, final `toogle_vm.sh status`) · exact reproduction commands.

### 6.6 IDs, Definition of Ready, severity
As Occupify: `AC-n → T-nn (covers) → TP-nn → evidence row → F-nn`. Card DoR: one owner role from §5.2, `scope.create/modify/must_not_touch`, behaviour ≤10 lines, `covers:`, `done_when:` naming `TP-nn`, size S/M, `depends_on`. Review severity BLOCKER/MAJOR/MINOR/NIT; `y2-reviewer` blocks QA on BLOCKER/MAJOR.

## 7. Test tiers (what every `23-test-plan.md` must address)

| Tier | What | Runner | Owner |
|---|---|---|---|
| T1 | pytest with `tests/fake_az/az` — unit, integration, golden byte-compare, py3.9 compat check | `python3 -m pytest tests/ -v` | `y2-test-engineer` |
| T2 | container tests — Rocky Linux 9 bash, PowerShell container | existing `test_compiler.py`, `test_y2p_container.py` | `y2-test-engineer` |
| T3 | real Azure — `y2b`/`y2p` against `lin01–03`, `win01–02` | lab runner (§8) | `y2-azure-tester` |

Each `TP-nn` names its tier, the AC it proves, the command, and pass criteria. A plan may say "T3 not needed" with a reason (e.g. docs-only); the lead's evidence check holds it to that.

## 8. Lab guard (real Azure safety)

Defence in depth; each layer must fail **closed**.

0. **Scoped identity (owner decision, 2026-10-31)** — agents never use the owner's own Azure login. The owner creates, once, a service principal with **Virtual Machine Contributor on `rg-lab-10vms` only**, e.g. `az ad sp create-for-rbac --name y2-lab-agents --role "Virtual Machine Contributor" --scopes /subscriptions/<lab-sub>/resourceGroups/rg-lab-10vms`. Its credentials live **outside the repo** in `~/.config/y2-lab/sp.env` (mode 600; agents never read or print it — hooks deny `view_file` on it). `run-lab.sh` logs in with it into a private `AZURE_CONFIG_DIR` (`~/.config/y2-lab/azure`), so Azure itself refuses anything outside the lab RG — including `rg-tfstate`. The builder ships a `lab/README.md` section with the one-time setup; agents cannot create the SP. **Fallback** if the student tenant blocks SP creation: the owner's own login + layers 1–5, with the residual risk written into `lab/README.md`. **Residual risk either way:** an agent that bypasses the hooks could still reach the owner's personal `~/.azure` session if one is logged in on the same machine — the docs advise `az logout` of the personal session while the squad runs.
1. **Single source of lab identity** — a committed `lab/lab.env` (subscription id, RG, VM names `lin01 lin02 lin03 win01 win02` — the full set per `lab/main.tf` locals; "10vms" in the RG name is legacy). `toogle_vm.sh` is edited to source it (builder may change that script); the runner never passes its `-g/-s` overrides. No secrets in it. Note the same subscription also holds `rg-tfstate` (Terraform state) — the shim must refuse it explicitly.
2. **Lab runner** `lab/run-lab.sh <command…>` — the only way Antigravity agents run anything against Azure:
   - takes a lock (one lab run at a time) holding PID + start time; a lock whose PID is dead is reclaimed;
   - logs in with the service principal (§8.0) into the private `AZURE_CONFIG_DIR`; missing `sp.env` or failed login → stop before touching any VM; `y2-tech-lead` raises `BLOCKED` for the owner;
   - records every lab VM's power state at start (evidence only);
   - `az account set` to the lab subscription and verifies it;
   - prepends an **`az` shim** to `PATH` (below), so `y2b`/`y2p` subprocesses are covered too;
   - validates every input CSV row targets the lab subscription + RG + a known VM name, else refuses. A subscription given by name is accepted only if it equals `LAB_SUBSCRIPTION_NAME` in `lab.env` (empty → GUID only); no `az` call is needed to validate;
   - enforces a wall-clock budget (default 60 min, overridable per test plan). On expiry: kill the child process group, deallocate, mark T3 failed; `y2-debugger` triages and the retry counts toward `max_internal_fix_cycles`;
   - `trap … EXIT INT TERM` → `lab/toogle_vm.sh stop --wait`, then `status`, and writes the status output to `42-evidence/t3/` — on success **and** failure. **All 5 VMs end deallocated, including any the owner had running before the run** (owner decision, 2026-10-31).
3. **`az` shim** — an allowlist that parses the request, not just the prefix. Passes through only:
   - `account show` (any `--subscription`, read-only — needed for name resolution), `account set` to the lab GUID;
   - `vm show|list|get-instance-view|start|deallocate|run-command (invoke|show|list)` with `-g rg-lab-10vms` and a lab VM name;
   - `rest --method get` under `/subscriptions/<lab-sub>/resourceGroups/rg-lab-10vms/`;
   - `rest --method post` only to `…/virtualMachines/<lab-vm>/` + the action suffixes the dispatcher uses (`runCommand`, `start`, `deallocate`, `powerOff`; builder confirms the exact list from `y2shared/dispatcher.py`).
   Everything else → refuse, exit ≠0, clear message: other methods (`put|patch|delete`), other suffixes (`restart`, `delete`, …), any `create|delete|update`, `group`, `role`, `network`, `keyvault`, `storage`, anything mentioning `rg-tfstate`, and any `--subscription`/`-g`/URL outside the lab. The **script content** sent by run-command is out of scope — running arbitrary scripts on lab VMs is what y2 does.
4. **Antigravity hooks** (best effort; the shim is the main layer) — `run_command`: deny `terraform`, deny `az …` not prefixed by `lab/run-lab.sh` (and by `y2-researcher`/`y2-azure-tester` only, if the hook can see the agent). `view_file`: deny `lab/terraform.tfvars`, `*.tfstate*`, `lab/.terraform/**`, `~/.config/y2-lab/**`. Unparseable input → **deny** (Occupify's hook fails open — do not copy that). String matching can be bypassed (`bash -c`, a Python subprocess); the design accepts that and relies on the shim + §8.0.
5. **Claude side** — `settings.json` `permissions.deny` for `Read(./lab/terraform.tfvars)`, `Read(./lab/**/*.tfstate*)`, `Read(~/.config/y2-lab/**)`, `Bash(terraform:*)`, `Bash(az:*)`.

No Terraform, ever. Deallocate only — never `destroy`.

## 9. Files to build

```
yaml_to_bash/
├── CLAUDE.md                              # + "Squad" section: roles, entry points, lab rules
├── .handoffs/PROTOCOL.md                  # locked contract (§6)
├── .claude/
│   ├── settings.json                      # write-guard hook + deny rules (§8.5)
│   ├── squad-learnings.md
│   ├── agents/y2-lead.md · y2-planner.md · y2-research-planner.md · y2-test-planner.md
│   ├── hooks/guard-y2-writes.sh           # per-role write allowlist; no code writes
│   └── skills/
│       ├── y2-lead/SKILL.md               # entry: inbox | plan <id> <brief> | status
│       ├── y2-plan/  (SKILL.md + templates 00,10,20,21,22,23,20-lite)
│       └── y2-accept/ (SKILL.md + templates 50, 90)
└── .agents/
    ├── plugins/y2/
    │   ├── plugin.json · hooks.json
    │   ├── agents/  (7 agents, §5.2)
    │   ├── rules/handoff-protocol.md · lab-safety.md · repo-constraints.md · project-learnings.md
    │   ├── scripts/protect-handoff.sh · lab-guard.sh
    │   └── skills/
    │       ├── y2-delivery/      (workflow, DoR, delivery-report template)
    │       ├── y2-research/      (findings template, probe procedure)
    │       ├── y2-lab-testing/   (how to use lab/run-lab.sh, evidence format)
    │       └── vendored/ …       (reuse plan §11)
    └── (lab/) lab.env · run-lab.sh · az-shim/az     # under repo lab/, next to toogle_vm.sh
```

## 10. First request for the squad (my call)

Once the squad is built and reviewed, its first lite/full request is **"T3 real-Azure test harness"**: `y2-test-planner` designs the tier-3 suite (which examples to run against which VMs, expected CSV outcomes, power-state matrix, budget), Antigravity builds it on top of `lab/run-lab.sh`. This keeps the safety rails (§8) part of the squad build and the test content part of normal feature flow.

## 11. Reuse plan (from `my-agent-factory/.research/20261031-y2-squad-prior-art/repos-claude.md`)

Copy individual files into `.agents/plugins/y2/skills/vendored/<name>/`, keeping a header with source repo, path, commit sha and licence; edit for repo constraints.

| Skill | Source @ sha | For | Adaptation |
|---|---|---|---|
| `bash-defensive-patterns`, `shellcheck-configuration` | wshobson/agents @ 7e2fb559cf | engineer, reviewer | note bash 3.2 host / RHEL bash targets; drop Bash 5-only advice |
| `python-testing-patterns` | wshobson/agents @ 7e2fb559cf | test-engineer | point at fake_az, goldens, py3.9 |
| `azure-vms` | sickn33/antigravity-awesome-skills @ dcbd2ae71a | researcher, azure-tester | add "only via lab/run-lab.sh"; drop create/delete sections |
| `powershell-windows` | sickn33/antigravity-awesome-skills @ dcbd2ae71a | engineer, reviewer | stress PS 5.1 (UTF-8 BOM, no pwsh-7 syntax) |
| a systematic-debugging skill | sickn33 mirror (e.g. `phase-gated-debugging`) | debugger | as-is |

Claude side reuses installed superpowers (`writing-plans`, `brainstorming`) by reference. MicrosoftDocs `azure-virtual-machines` is referenced by the researcher as a docs index, not copied (CC-BY-4.0). **Rejected:** `microsoft/azure-skills` plugin (telemetry hooks, unpinned `npx @azure/mcp@latest`), Azure MCP server (second credentialed path, no Run Command tool).

Sources: the builder may read them via `gh api` or the local mirror `~/.agent-factory/vendor/`; all listed sources are MIT (checked 2026-10-31). If a file can't be fetched, write the skill from scratch rather than skip it.

## 12. Acceptance checks for this build

1. Frontmatter: `y2-lead` = `claude-opus-5-5`/`high`; three helpers `claude-sonnet-5-5` with efforts per §5.1; all seven Antigravity agents `model: flash` + HIGH line. (These exact IDs already work in the owner's Occupify squads.)
2. `guard-y2-writes.sh` and `protect-handoff.sh`: table-driven tests with crafted hook JSON — allowed and denied paths per role, `PROTOCOL.md` denied for all, malformed input denied.
3. `az` shim: tests with a stub real-`az` that records calls — in-lab calls pass, out-of-lab / mutating / `terraform` refused, nothing reaches Azure.
4. `run-lab.sh`: trap fires on success, failure and SIGINT (verified with the stub, no real Azure); CSV validation rejects a non-lab row; missing `sp.env` stops before any VM call; budget expiry kills the child and still deallocates.
5. `skill-check` passes on every new skill.
6. A paper walk-through of one lite request and one full request through the stages, checking each file has exactly one writer and each message names its files.
7. Internal review by `peer-reviewer`, then cross-platform peer review.
8. Repo tests still pass (`python3 -m pytest tests/ -v`), goldens unchanged.

## 13. Left to the builder
- Exact Antigravity hook payload fields for `run_command` / `view_file`, and whether hooks see the calling agent — verify, then pick path- vs agent-based rules.
- Template wording, skill bodies, message envelope examples.
- Whether `.handoffs/<request-id>/` folders are committed (recommended: commit, except large logs under `42-evidence/`, which are gitignored).
