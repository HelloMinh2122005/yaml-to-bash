# Delivery Workflow

## 1. Intake & Definition of Ready
- When `PLAN_READY` is received: read `state.json`, spec (`20-spec.md`), plan (`21-plan.md`), task cards (`22-tasks.md`), and test plan (`23-test-plan.md`) (or `20-lite-plan.md`).
- Validate each card against `references/definition-of-ready.md`.
- If issues or ambiguities are found: batch questions in `.handoffs/<id>/30-questions.md` as `Q-nn` and send `CLARIFY`.
- Once ready: update `state.json` stage to `IMPLEMENTING`.

## 2. Card Execution Loop
For each card in sequence / dependency order:
1. **Implementation:** Delegate to `y2-engineer` to implement changes in scope.
2. **Review:** Delegate to `y2-reviewer` to author `43-code-review-cN.md`.
   - If findings include BLOCKER or MAJOR: delegate fix to `y2-engineer` and re-review (up to `max_internal_fix_cycles`).
3. **Verification:**
   - Tier 1 & Tier 2: Delegate to `y2-test-engineer` to execute pytest and container tests. Save logs to `42-evidence/t1/` and `t2/`.
   - Tier 3 (if card covers T3 test): Delegate to `y2-azure-tester` to run via `lab/run-lab.sh`. Save logs to `42-evidence/t3/`.
   - If any test fails: delegate to `y2-debugger` to root-cause in `44-debug-log.md` and apply fix; then re-review and re-test.

## 3. Delivery Compilation
1. Verify all tests pass, reviewer verdict is clean, and all lab VMs are deallocated.
2. Generate `41-diff.patch` via `git diff <base>...HEAD`.
3. Author `.handoffs/<id>/40-delivery-report.md` using `references/delivery-report-template.md`.
4. Update `state.json` stage to `DELIVERED`, pass baton to planning lead `y2-lead`.
5. Send `DELIVERY` message to `y2-lead` via agent-chat.

## 4. Research Workflow
When `RESEARCH_REQUEST` is received:
1. Update `state.json` stage to `RESEARCHING`.
2. Delegate to `y2-researcher` to execute research items from `10-research-plan.md`.
3. Researcher compiles `.handoffs/<id>/11-research-findings.md` and logs under `42-evidence/research/`.
4. Pass baton back to planning lead `y2-lead` via `RESEARCH_DONE`.
