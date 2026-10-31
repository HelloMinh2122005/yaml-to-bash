# Squad Handoff Protocol (canonical, locked)

The contract between the **Claude squad** (think · plan · test plan) and the **Antigravity squad** (implement · review · test · execute Azure).
Both squads read this file every turn. **Neither edits it**; hooks on both sides block writes (`guard_y2_writes.py` and `protect_handoff.py`). Only the human repository owner changes it.

Design specification: `docs/superpowers/specs/2026-10-31-y2-squad-design.md`.

---

## 1. Parties and addresses

| Address (agent-chat) | Role | Platform / Location |
| :--- | :--- | :--- |
| `claude-code/y2-lead` | Claude lead. Frames requests, specs, task cards, test plans, and checks evidence. Helpers (`y2-planner`, `y2-research-planner`, `y2-test-planner`) never use agent-chat. | `.claude/agents/` |
| `antigravity/y2-tech-lead` | Antigravity lead. Orchestrates execution, sequences cards, runs QA gate, writes delivery reports. Subagents never use agent-chat. | `.agents/plugins/y2/agents/` |

Nobody listens live: the repository owner starts every turn (`/check-message <name>`). Every hop costs a human trigger — keep communication strictly to one message per baton pass.

---

## 2. Folder and file ownership

One folder per request: `.handoffs/<request-id>/` where `<request-id>` = `YYYYMMDD-<kebab-slug>` (e.g. `20261101-real-azure-harness`).

| File | Writer | Content |
| :--- | :--- | :--- |
| `state.json` | **Current baton holder only** | Stage, mode, baton, plan version, loop counters (§4) |
| `00-brief.md` | Claude (`y2-lead`) | Problem, user value, success criteria, non-goals, constraints |
| `10-research-plan.md` | Claude (`y2-research-planner`) | Research briefs `R-nn`: questions, methods (docs/code/lab), stop criteria |
| `11-research-findings.md` | Antigravity (`y2-researcher`) | Findings and probe log references answering each `R-nn` |
| `adr/ADR-NNN-<slug>.md` | Claude (`y2-lead`) | Architectural decisions |
| `20-spec.md` | Claude (`y2-lead`) | WHAT and WHY, acceptance criteria `AC-n` |
| `21-plan.md` | Claude (`y2-planner`) | HOW, technical invariants `INV-n`, changelog |
| `22-tasks.md` | Claude (`y2-planner`) | Task cards `T-nn` fulfilling the Definition of Ready |
| `23-test-plan.md` | Claude (`y2-test-planner`) | Test items `TP-nn` per tier (T1, T2, T3), lab matrix, budget |
| `30-questions.md` | Antigravity (`y2-tech-lead`) | Intake questions `Q-nn` (CLARIFY) or blocked items `B-nn` (BLOCKED) |
| `31-answers.md` | Claude (`y2-lead`) | Answers keyed by `Q-nn` and `B-nn` |
| `40-delivery-report.md` | Antigravity (`y2-tech-lead`) | Delivery report for the completed cycle (§9) |
| `41-diff.patch` | Antigravity (`y2-tech-lead`) | `git diff <base>...<branch>` |
| `42-evidence/` | Antigravity | Execution evidence (test logs `t1/`, `t2/`, lab runner logs `t3/`, probe logs) |
| `43-code-review-cN.md` | Antigravity (`y2-reviewer`) | Code review findings `F-nn` and verdict for internal QA cycle N |
| `44-debug-log.md` | Antigravity (`y2-debugger`) | Root cause analysis (RCA) and fix summary for test failures |
| `50-acceptance-cN.md` | Claude (`y2-lead`) | Evidence-check verdict for delivery cycle N |
| `90-retro.md` | Claude (`y2-lead`) | Request retrospective, including a dedicated "For Antigravity" section |
| `20-lite-plan.md` | Claude (`y2-lead`) | Merged brief, spec, plan, cards, and test plan for lite mode requests (§12) |

### Write Allowlists (Hook-enforced)
- **Claude side:** Claude roles may only write their owned files in `.handoffs/<request-id>/` and `.claude/squad-learnings.md`. All code, tests, `lab/`, and `.handoffs/PROTOCOL.md` writes are denied.
- **Antigravity side:** Antigravity may write only its assigned handoff files (`11`, `30`, `40`, `41`, `42-evidence/**`, `43`, `44`, `state.json`), code, unit tests, and user docs. Writes to `.handoffs/PROTOCOL.md`, `.claude/`, `docs/superpowers/`, and `lab/` are denied.

---

## 3. Stages

```
DRAFTING ─┬─(unknowns)→ AWAITING_RESEARCH_APPROVAL* → RESEARCHING → DRAFTING
          └→ AWAITING_PLAN_APPROVAL → PLAN_READY → [CLARIFYING] → IMPLEMENTING
             → INTERNAL_QA (review · debug · T1 · T2 · T3) → DELIVERED
             → [CHANGES_REQUESTED → IMPLEMENTING …] → AWAITING_SIGNOFF → DONE
Any cap hit / unresolvable BLOCKED → ESCALATED (owner decides: amend, descope, abort)
* Only required when the research plan contains a live Azure lab probe; docs/code-only research skips this gate.
```

| Stage | Baton Holder |
| :--- | :--- |
| `DRAFTING`, `CLARIFYING`, `DELIVERED` | `claude-code/y2-lead` |
| `RESEARCHING`, `PLAN_READY`, `IMPLEMENTING`, `INTERNAL_QA`, `CHANGES_REQUESTED` | `antigravity/y2-tech-lead` |
| `AWAITING_RESEARCH_APPROVAL`, `AWAITING_PLAN_APPROVAL`, `AWAITING_SIGNOFF`, `ESCALATED` | `owner` |
| `DONE` | none |

---

## 4. `state.json`

Every request directory contains a `state.json` tracking the current lifecycle:

```json
{
  "request_id": "20261101-real-azure-harness",
  "mode": "full",
  "stage": "IMPLEMENTING",
  "baton": "antigravity/y2-tech-lead",
  "plan_version": 1,
  "research_round": 0,       "max_research_rounds": 2,
  "clarify_round": 0,        "max_clarify_rounds": 2,
  "internal_fix_cycle": 0,   "max_internal_fix_cycles": 3,
  "acceptance_cycle": 0,     "max_acceptance_cycles": 2,
  "last_message_id": null,
  "updated_by": "claude-code/y2-lead",
  "updated_at": "2026-10-31T12:00:00Z"
}
```

- `mode`: `"full"` (default) or `"lite"` (single plan file `20-lite-plan.md`, §12).
- **Passing the baton:** Write `state.json` with the new baton holder, stage, and incremented counters *before* sending the agent-chat message.
- Counters only increase. Exceeding any `max_*` cap immediately transitions stage to `ESCALATED`.

---

## 5. Message envelope (≤ 2000 chars, agent-chat)

```
[TYPE] <request-id> · plan v<n> · cycle <c>/2
dir: .handoffs/<request-id>/
read: <file1>, <file2>, ...
summary: <= 5 lines describing the update
next: explicit instruction on what recipient must do
```

Send via agent-chat `chat(from: <sender>, to: <recipient>, message: ..., reply_to: <optional_id>)`.

---

## 6. Message types

| Type | From → To | When | Expected References |
| :--- | :--- | :--- | :--- |
| `RESEARCH_REQUEST` | Claude → AG | Research plan ready (approved by owner if lab probes included) | `10-research-plan.md` |
| `RESEARCH_DONE` | AG → Claude | Research completed and findings documented | `11-research-findings.md` |
| `PLAN_READY` | Claude → AG | Owner approved plan and test plan | `20`, `21`, `22`, `23` (or `20-lite-plan.md`) |
| `CLARIFY` | AG → Claude | Definition of Ready fails at intake (batched) | `30-questions.md` (`Q-nn`) |
| `BLOCKED` | AG → Claude | Fundamental impediment or plan flaw mid-build | `30-questions.md` (`B-nn`) |
| `ANSWER` | Claude → AG | Questions answered without altering plan | `31-answers.md` |
| `PLAN_AMENDMENT` | Claude → AG | Plan changed in response to feedback; `plan_version` + 1 | `21-plan.md` changelog, updated cards |
| `DELIVERY` | AG → Claude | Internal QA passed (review clean, all tiers green, VMs deallocated) | `40`, `41`, `42-evidence/`, `43` |
| `ACCEPTANCE_RESULT` | Claude → AG | Evidence gaps found; requests changes | `50-acceptance-cN.md` |
| `DONE` | Claude → AG | Evidence check passed AND owner signed off | `50-acceptance-cN.md`, `90-retro.md` |
| `ESCALATED` | Either → Other | Any loop cap hit or unresolvable block; owner decides | Document explaining reason |

**Core communication rules:**
1. **No acknowledgement-only messages.**
2. **One message per baton pass.**
3. **Cold start every turn:** Re-read `state.json` and named files; do not rely on conversation memory.
4. Messages that disagree with `state.json` are returned with error details and not acted on.

---

## 7. IDs and traceability

Every requirement and test traces strictly through IDs:
- `AC-n` (Acceptance Criteria in spec)
- `T-nn` (Task Card in plan, declaring `covers: AC-n`)
- `TP-nn` (Test Plan item, declaring `covers: AC-n`, tier T1/T2/T3, commands, criteria)
- `evidence row` in `40-delivery-report.md` proving `TP-nn` passed
- `F-nn` (Code Review finding referencing `AC-n` or constraint)
- `R-nn` (Research item)
- `Q-nn` / `B-nn` (Clarification or blocker item)
- `INV-n` (Technical invariant)

---

## 8. Definition of Ready (task card)

A task card `T-nn` in `22-tasks.md` is ready for implementation only if it contains:
1. Exactly one assigned owner role from Antigravity (`y2-engineer`, `y2-researcher`, `y2-test-engineer`, `y2-azure-tester`).
2. Scope bounds: `scope.create`, `scope.modify`, and `scope.must_not_touch`.
3. Behaviour description in ≤ 10 concise lines.
4. Explicit `covers:` listing one or more `AC-n`.
5. Explicit `done_when:` naming one or more `TP-nn` test items.
6. Size estimate: `S` (≤ 4 hours / simple change) or `M` (up to 1 day / moderate).
7. Dependencies: `depends_on: [T-mm, ...]`.

---

## 9. Delivery report minimum

`40-delivery-report.md` must include:
1. Plan version and git branch + commit HEAD SHA.
2. Status table of all task cards.
3. AC → Evidence mapping table.
4. TP → Result table per tier (T1 unit/integration, T2 container, T3 real-Azure) with test counts and durations.
5. Reviewer verdict and any open findings from `43-code-review-cN.md`.
6. Debugger RCAs from `44-debug-log.md` (or "none").
7. Declared deviations from plan (or "none").
8. Lab run summary: VMs touched, start/end power states, wall-clock duration, final `toogle_vm.sh status` output proving all 5 VMs are deallocated.
9. Exact commands to reproduce verification.

---

## 10. Severity levels

Code review findings (`F-nn`) in `43-code-review-cN.md`:
- **BLOCKER:** Breaks core invariants, leaks credentials, causes out-of-scope Azure mutations, breaks goldens, or violates Python 3.9 / Bash 3.2 constraints. Must be fixed before QA passes.
- **MAJOR:** AC not satisfied, test tier missing, unhandled edge cases in card scope. Must be fixed before QA passes.
- **MINOR:** Stylistic deviation, non-critical documentation omission, minor inefficiency. Can be addressed in a follow-up card.
- **NIT:** Trivial formatting or comment suggestion.

Internal QA passes only when zero BLOCKER and zero MAJOR findings remain.

---

## 11. Lab safety rules summary

All real-Azure testing and research probes must strictly comply with `lab/README.md`:
1. Use dedicated service principal identity via `~/.config/y2-lab/sp.env` outside the repository.
2. Invocations must execute solely through `lab/run-lab.sh`. Bare `az` CLI and direct `y2b run`/`y2p run` commands are prohibited.
3. Target only lab subscription `79f7da80-0c64-4e4b-9ed4-316a88142152` and resource group `rg-lab-10vms` on VMs `lin01`–`lin03`, `win01`–`win02`.
4. Forbidden resource group `rg-tfstate` and Terraform operations (`terraform *`) must never be touched or executed.
5. Every lab execution must terminate with all 5 lab VMs deallocated.

---

## 12. Lite mode

For small changes, the squad operates in `lite` mode (`mode: "lite"` in `state.json`):
- Qualification criteria: ≤ 2 code files touched, no changes to runner/dispatcher/resume/power semantics, and no live Azure lab probes required.
- Single combined artifact: `20-lite-plan.md` merges brief, spec, plan, task cards, and test plan into one concise file.
- Streamlined workflow: skips the separate research stage and helper fan-out while preserving all safety guards, test tiers, and evidence checks.

