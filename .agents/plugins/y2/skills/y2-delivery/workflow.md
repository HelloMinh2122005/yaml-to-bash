# Delivery Workflow

Owner: `y2-tech-lead`. Squad orchestrator mandate: **Think, Plan, Delegate, and Review — NEVER Implement Directly.**

---

## 1. Intake & Definition of Ready
- When `PLAN_READY` is received: read `state.json`, spec (`20-spec.md`), plan (`21-plan.md`), task cards (`22-tasks.md`), and test plan (`23-test-plan.md`) (or `20-lite-plan.md`).
- Validate each card against `references/definition-of-ready.md`.
- If issues or ambiguities are found: batch all questions in `.handoffs/<id>/30-questions.md` as `Q-nn` and send `CLARIFY`. Stop.
- Once ready: update `state.json` stage to `IMPLEMENTING`.

## 2. Card Execution Loop (via `invoke_subagent`)
For each card in dependency order (`depends_on`), `y2-tech-lead` delegates to specialists:

1. **Implementation (`y2-engineer`):**
   - Delegate implementation of declared `scope.create` and `scope.modify` via `invoke_subagent(TypeName="y2-engineer", ...)`.
   - Lead does NOT write or edit code in `y2shared/`, `y2b/`, `y2p/`, `bin/`, or `examples/`.
   - Stop turn and wait for `y2-engineer` to complete.

2. **Independent Code Review (`y2-reviewer`):**
   - Delegate read-only code review via `invoke_subagent(TypeName="y2-reviewer", ...)`.
   - `y2-reviewer` authors `.handoffs/<id>/43-code-review-cN.md`.
   - If findings include BLOCKER or MAJOR: delegate fix to `y2-engineer` and re-review (up to `max_internal_fix_cycles`).

3. **Test Verification (`y2-test-engineer` & `y2-azure-tester`):**
   - Tier 1 & Tier 2: Delegate to `y2-test-engineer` via `invoke_subagent` to run pytest and container tests. Save logs to `42-evidence/t1/` and `t2/`.
   - Tier 3 (if card covers T3 test): Delegate to `y2-azure-tester` via `invoke_subagent` to run via `lab/run-lab.sh`. Save logs to `42-evidence/t3/`.
   - If any test fails: delegate to `y2-debugger` via `invoke_subagent` to root-cause in `44-debug-log.md` and apply targeted fix; then re-review and re-test.

## 3. Delivery Compilation & Hand Back
1. Verify all required test tiers are green, reviewer verdict has 0 BLOCKER and 0 MAJOR, and all lab VMs are confirmed deallocated.
2. Generate `41-diff.patch` via `git diff <base>...HEAD`.
3. Author `.handoffs/<id>/40-delivery-report.md` using `references/delivery-report-template.md`.
4. Update `state.json` stage to `DELIVERED`, pass baton to planning lead `y2-lead`.
5. Send `DELIVERY` message to `y2-lead` via agent-chat.

## 4. Research Workflow
When `RESEARCH_REQUEST` is received:
1. Update `state.json` stage to `RESEARCHING`.
2. Delegate to `y2-researcher` via `invoke_subagent` to execute research items from `10-research-plan.md`.
3. Researcher compiles `.handoffs/<id>/11-research-findings.md` and logs under `42-evidence/research/`.
4. Pass baton back to planning lead `y2-lead` via `RESEARCH_DONE`.
