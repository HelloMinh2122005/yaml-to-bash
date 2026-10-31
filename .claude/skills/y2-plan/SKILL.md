---
name: y2-plan
description: Author request specifications, implementation plans, and three-tier test plans for y2 squad requests.
disable-model-invocation: true
model: claude-opus-5-5
effort: high
---

# /y2-plan — Request Planning Workflow

Follow this 6-step planning pipeline for request `$ARGUMENTS` (`<request-id> <brief>`):

## P1: Frame & Select Mode
1. Assess scope against `.handoffs/PROTOCOL.md` §12 lite criteria (≤ 2 code files, no changes to concurrency/dispatcher/runner/power semantics, no lab probes).
2. If lite: author `.handoffs/<request-id>/20-lite-plan.md` using `templates/20-lite-plan.md`. Set `"mode": "lite"` in `state.json`.
3. If full: initialize `.handoffs/<request-id>/00-brief.md` using `templates/00-brief.md`. Set `"mode": "full"` in `state.json`.

## P2: Research Unknowns (if needed)
1. If technical uncertainties exist, spawn `y2-research-planner` to author `.handoffs/<request-id>/10-research-plan.md` using `templates/10-research-plan.md`.
2. If research items contain `lab-probe`, pause in `AWAITING_RESEARCH_APPROVAL` until repository owner gives consent.
3. Pass baton to `antigravity/y2-tech-lead` via `RESEARCH_REQUEST`. On receipt of `RESEARCH_DONE`, review findings in `11-research-findings.md`.

## P3: Specification & Architecture Decisions
1. Author `.handoffs/<request-id>/20-spec.md` using `templates/20-spec.md` defining WHAT/WHY and `AC-n` acceptance criteria.
2. If architectural choices require record, create `.handoffs/<request-id>/adr/ADR-NNN-<slug>.md` using `templates/adr.md`.

## P4: Parallel Plan & Test Plan Generation
Spawn helpers in parallel:
- Spawn `y2-planner` to author `.handoffs/<request-id>/21-plan.md` (using `templates/21-plan.md`) and `.handoffs/<request-id>/22-tasks.md` (using `templates/22-tasks.md`).
- Spawn `y2-test-planner` to author `.handoffs/<request-id>/23-test-plan.md` (using `templates/23-test-plan.md`) covering T1, T2, and T3 tiers.

## P5: Lead Consistency Pass
Verify complete traceability:
- Every `AC-n` is covered by at least one task card `T-nn` in `22-tasks.md`.
- Every `AC-n` is covered by at least one test item `TP-nn` in `23-test-plan.md`.
- Definition of Ready is satisfied for every card.

## P6: Owner Gate 1 & Dispatch
1. Present plan summary to repository owner for approval (`AWAITING_PLAN_APPROVAL`).
2. Upon approval, update `state.json` to stage `PLAN_READY`, pass baton to `antigravity/y2-tech-lead`.
3. Send `PLAN_READY` message to `antigravity/y2-tech-lead` via agent-chat.

