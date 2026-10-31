# y2 Squad Paper Walkthrough (Lifecycle Verification)

Date: 2026-10-31  
Specification: `docs/superpowers/specs/2026-10-31-y2-squad-design.md`  
Contract: `.handoffs/PROTOCOL.md`

This document verifies the end-to-end operational mechanics and hook allowlists of the two-platform squad through two detailed scenarios:
1. **Scenario 1:** A Lite Request ("Add a validator warning for `res_file` under `/tmp`")
2. **Scenario 2:** A Full Request ("T3 real-Azure test harness")

---

## Scenario 1: Lite Request — Validator Warning for `/tmp`

- **Request ID:** `20261101-tmp-resfile-warning`
- **Scope:** 1 file modified in `y2shared/validate.py` (qualifies for Lite Mode per PROTOCOL §12).

### Stage 1: DRAFTING
- **Baton Holder:** `claude-code/y2-lead`
- **Action:** Lead initializes folder `.handoffs/20261101-tmp-resfile-warning/`.
- **Files Written:**
  - `state.json`: `"mode": "lite"`, `"stage": "DRAFTING"`, `"baton": "claude-code/y2-lead"`.
    - *Guard Check:* Allowed by `guard_y2_writes.py` (`state.json` owned by lead).
  - `20-lite-plan.md`: Formulates brief, AC-1 (warning logged when res_file is in `/tmp`), INV-1..2, card T-01, and test item TP-01 (T1 pytest).
    - *Guard Check:* Allowed by `guard_y2_writes.py` (`2[0-3]-[^/]+\.md` matches `20-lite-plan.md`).
- **Owner Gate:** Lead sets stage to `AWAITING_PLAN_APPROVAL` and presents the plan to the repository owner.

### Stage 2: PLAN_READY
- **Baton Holder:** `claude-code/y2-lead` → `antigravity/y2-tech-lead`
- **Action:** Owner approves plan. Lead updates `state.json` to `"stage": "PLAN_READY"` and passes baton to `"antigravity/y2-tech-lead"`.
- **Message Sent:**
  ```text
  [PLAN_READY] 20261101-tmp-resfile-warning · plan v1 · cycle 0/2
  dir: .handoffs/20261101-tmp-resfile-warning/
  read: 20-lite-plan.md
  summary: Approved lite plan adding validator warning for res_file under /tmp.
  next: Intake Definition of Ready, sequence card T-01, implement and run T1 tests.
  ```

### Stage 3: IMPLEMENTING
- **Baton Holder:** `antigravity/y2-tech-lead`
- **Action:** Tech Lead performs DoR intake on card T-01 in `20-lite-plan.md`. DoR passes. Updates `state.json` stage to `IMPLEMENTING`.
- **Delegation:** Tech Lead delegates T-01 to `y2-engineer`.
  - `y2-engineer` edits `y2shared/validate.py` to add warning check.
  - *Guard Check:* Allowed by `protect_handoff.py` (outside protected directories).

### Stage 4: INTERNAL_QA
- **Baton Holder:** `antigravity/y2-tech-lead`
- **Action:** Tech Lead transitions stage to `INTERNAL_QA`.
- **Code Review:** Tech Lead spawns `y2-reviewer`.
  - `y2-reviewer` reviews changes against Python 3.9 rules and card T-01.
  - Writes `.handoffs/20261101-tmp-resfile-warning/43-code-review-c1.md`: Verdict APPROVED, 0 findings.
  - *Guard Check:* Allowed by `protect_handoff.py` (`43-code-review-c[0-9]+\.md`).
- **Test Execution:** Tech Lead spawns `y2-test-engineer`.
  - Adds test in `tests/test_validate.py`. Allowed by `protect_handoff.py`.
  - Runs `python3 -m pytest tests/ -v`. Allowed by `lab_guard.py`.
  - Saves evidence to `.handoffs/20261101-tmp-resfile-warning/42-evidence/t1/pytest.log`. Allowed by `protect_handoff.py` (`42-evidence/.+`).

### Stage 5: DELIVERED
- **Baton Holder:** `antigravity/y2-tech-lead` → `claude-code/y2-lead`
- **Action:** All QA passed. Tech Lead generates `41-diff.patch` and compiles `40-delivery-report.md`.
  - *Guard Check:* Allowed by `protect_handoff.py`.
  - Updates `state.json` to `"stage": "DELIVERED"`, `"baton": "claude-code/y2-lead"`.
- **Message Sent:**
  ```text
  [DELIVERY] 20261101-tmp-resfile-warning · plan v1 · cycle 0/2
  dir: .handoffs/20261101-tmp-resfile-warning/
  read: 40-delivery-report.md, 41-diff.patch, 43-code-review-c1.md
  summary: T1 pytest green, golden unchanged, 0 code review findings.
  next: Perform evidence check and owner sign-off.
  ```

### Stage 6: ACCEPTANCE & DONE
- **Baton Holder:** `claude-code/y2-lead`
- **Action:** Lead performs cold-read evidence check:
  - Audits `40-delivery-report.md` against AC-1 and TP-01. All green.
  - Writes `.handoffs/20261101-tmp-resfile-warning/50-acceptance-c1.md`.
    - *Guard Check:* Allowed by `guard_y2_writes.py`.
  - Presents delivery to owner for Gate 3 (`AWAITING_SIGNOFF`).
  - Owner signs off.
  - Lead authors `.handoffs/20261101-tmp-resfile-warning/90-retro.md`. Allowed by `guard_y2_writes.py`.
  - Updates `state.json` to `"stage": "DONE"`.
- **Message Sent:** `DONE` to `antigravity/y2-tech-lead`.

---

## Scenario 2: Full Request — T3 Real-Azure Test Harness

- **Request ID:** `20261101-t3-real-azure-harness`
- **Scope:** Full feature flow with technical research probe and live Azure lab execution.

### Stage 1: DRAFTING & Research Plan
- **Baton Holder:** `claude-code/y2-lead`
- **Action:** Lead initializes folder `.handoffs/20261101-t3-real-azure-harness/`.
- **Files Written:**
  - `state.json`: `"mode": "full"`, `"stage": "DRAFTING"`. Allowed by `guard_y2_writes.py`.
  - `00-brief.md`: Defines requirements for automated live testing against `rg-lab-10vms`. Allowed by `guard_y2_writes.py`.
- **Research Formulation:** Technical uncertainty regarding Run Command output formatting when VMs are deallocated mid-stream.
  - Lead spawns `y2-research-planner`.
  - `y2-research-planner` authors `10-research-plan.md` with item `R-01` (`lab-probe`).
  - *Guard Check:* Allowed by `guard_y2_writes.py`.

### Stage 2: AWAITING_RESEARCH_APPROVAL & RESEARCHING
- **Baton Holder:** `owner` → `antigravity/y2-tech-lead`
- **Action:** Because `10-research-plan.md` contains a `lab-probe`, Lead sets stage to `AWAITING_RESEARCH_APPROVAL`.
- **Owner Gate:** Owner reviews probe command and approves.
- **Dispatch:** Lead updates `state.json` to `"stage": "RESEARCHING"`, passes baton to `antigravity/y2-tech-lead`.
- **Message Sent:** `RESEARCH_REQUEST`.
- **Probe Execution:** Tech Lead delegates to `y2-researcher`.
  - `y2-researcher` executes:
    `lab/run-lab.sh --evidence .handoffs/20261101-t3-real-azure-harness/42-evidence/research/ -- az vm get-instance-view -g rg-lab-10vms -n lin01`
  - *Guard Check:* `lab_guard.py` allows command because it starts with `lab/run-lab.sh`.
  - Runner performs SP login, acquires lock, executes probe through `az` shim, deallocates VMs on trap, writes `pre-state.txt` and `post-state.txt`.
  - `y2-researcher` writes `11-research-findings.md`. Allowed by `protect_handoff.py`.
- **Return:** Tech Lead updates `state.json` to `DRAFTING`, passes baton back to Claude with `RESEARCH_DONE`.

### Stage 3: Specification & Parallel Planning
- **Baton Holder:** `claude-code/y2-lead`
- **Action:** Lead incorporates research findings into `20-spec.md` (`AC-1` fleet run against lab, `AC-2` result verification, `AC-3` deallocation enforcement) and `adr/ADR-001-harness-design.md`. Allowed by `guard_y2_writes.py`.
- **Parallel Helpers:**
  - `y2-planner` writes `21-plan.md` and `22-tasks.md` (cards T-01 through T-03). Allowed by `guard_y2_writes.py`.
  - `y2-test-planner` writes `23-test-plan.md` (TP-01 unit, TP-02 container, TP-03 live Azure with 1800s budget). Allowed by `guard_y2_writes.py`.
- **Consistency Pass:** Lead validates that AC-1..3 are covered by T-01..03 and TP-01..03.

### Stage 4: Owner Plan Approval Gate
- **Baton Holder:** `owner`
- **Action:** Lead sets stage to `AWAITING_PLAN_APPROVAL`. Owner reviews spec and test plan (explicitly authorizing Tier 3 live Azure runs).
- **Dispatch:** Owner approves. Lead sets `state.json` to `"stage": "PLAN_READY"`, passes baton to `antigravity/y2-tech-lead`.
- **Message Sent:** `PLAN_READY`.

### Stage 5: IMPLEMENTING & Tier 1/2 Verification
- **Baton Holder:** `antigravity/y2-tech-lead`
- **Action:** Tech Lead checks Definition of Ready on cards T-01, T-02, T-03. All pass.
- **Card Execution:**
  - Card T-01 & T-02: `y2-engineer` implements harness runner in `tests/test_lab_e2e.py`. Allowed by `protect_handoff.py`.
  - Code Review: `y2-reviewer` inspects code, writes `43-code-review-c1.md` (0 findings). Allowed by `protect_handoff.py`.
  - T1/T2 Testing: `y2-test-engineer` executes local test suite. Logs saved to `42-evidence/t1/`. Allowed by `protect_handoff.py`.

### Stage 6: Tier 3 Live Azure Execution
- **Baton Holder:** `antigravity/y2-tech-lead`
- **Action:** Card T-03 assigned to `y2-azure-tester`.
  - Runs:
    ```bash
    lab/run-lab.sh --csv examples/hip-compliance/list_vm.csv \
      --evidence .handoffs/20261101-t3-real-azure-harness/42-evidence/t3/ \
      -- python3 -m pytest tests/test_lab_e2e.py -v
    ```
  - *Guard Check:* `lab_guard.py` allows command.
  - Runner verifies CSV rows target `rg-lab-10vms` and known lab VMs.
  - Runner executes test suite under timeout budget.
  - Runner trap fires on completion: invokes `toogle_vm.sh stop --wait` through `az` shim.
  - Final status recorded in `42-evidence/t3/post-state.txt` confirming all 5 VMs deallocated.

### Stage 7: Delivery & Sign-off
- **Baton Holder:** `antigravity/y2-tech-lead` → `claude-code/y2-lead`
- **Action:** Tech Lead verifies all tiers green and deallocation confirmed. Generates `41-diff.patch` and `40-delivery-report.md`. Passes baton with `DELIVERY`.
- **Acceptance:** Claude Lead audits `post-state.txt`, confirms all AC-1..3 and TP-1..3 satisfied, authors `50-acceptance-c1.md`.
- **Sign-off:** Owner approves Gate 3. Lead authors `90-retro.md`, sets `state.json` stage to `DONE`, and sends `DONE` to Antigravity.

---

## Conclusion

The paper walk-through confirms:
1. Every file has exactly one authoritative writer.
2. Hook guards on both Claude (`guard_y2_writes.py`) and Antigravity (`protect_handoff.py`, `lab_guard.py`) permit all legitimate operations while blocking unauthorized writes and unshielded cloud actions.
3. Every agent-chat message explicitly names its referenced files and provides unambiguous next steps.

