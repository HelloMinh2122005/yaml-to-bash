---
name: y2-accept
description: Perform evidence-based delivery acceptance and retrospective authoring for completed y2 requests.
disable-model-invocation: true
model: claude-opus-5-5
effort: high
---

# /y2-accept — Delivery Acceptance Workflow

Follow this 4-step acceptance protocol for request `$ARGUMENTS` (`<request-id>`):

## A1: Cold-Read Verification
1. Read `state.json` and confirm stage is `DELIVERED` and baton is with `claude-code/y2-lead`.
2. Inspect `40-delivery-report.md`, `41-diff.patch`, `43-code-review-cN.md`, and evidence in `42-evidence/`.

## A2: Evidence Audit
1. Verify every acceptance criterion (`AC-n`) declared in the spec has a corresponding row with passing evidence in `40-delivery-report.md`.
2. Verify all test plan items (`TP-nn`) show passing execution logs across the required tiers (T1, T2, T3).
3. Confirm code review in `43-code-review-cN.md` has 0 BLOCKER and 0 MAJOR open findings.

## A3: Lab VM Deallocation Verification
If Tier 3 executed against the Azure lab:
- Read `42-evidence/t3/post-state.txt` (or the lab status output in `40-delivery-report.md`).
- Ensure all 5 lab VMs (`lin01`–`lin03`, `win01`–`win02`) ended in `VM deallocated` state.

## A4: Acceptance Verdict & Sign-off
1. Author `.handoffs/<request-id>/50-acceptance-cN.md` using `templates/50-acceptance.md`.
2. If evidence gaps or failing tests are found:
   - Increment `acceptance_cycle` in `state.json`.
   - Update stage to `CHANGES_REQUESTED` and return baton to `antigravity/y2-tech-lead`.
   - Send `ACCEPTANCE_RESULT` message detailing required fixes.
3. If evidence is complete and verified:
   - Present delivery summary to repository owner for final sign-off (`AWAITING_SIGNOFF`).
   - Upon owner sign-off: update `state.json` stage to `DONE`.
   - Author `.handoffs/<request-id>/90-retro.md` using `templates/90-retro.md`.
   - Send `DONE` message to `antigravity/y2-tech-lead`.

