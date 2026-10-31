---
name: systematic-debugging
description: Enforce a disciplined 5-phase protocol for diagnosing and resolving test failures across unit, container, and lab tiers.
license: MIT
---

> Vendored from sickn33/antigravity-awesome-skills@dcbd2ae71a skills/phase-gated-debugging/SKILL.md · License: MIT · Adapted for yaml_to_bash: phase-gated systematic debugging for y2-debugger.

# Systematic Debugging Protocol

Enforces a disciplined 5-phase debugging protocol where code edits are blocked until the root cause is confirmed.

## The 5-Phase Protocol

### Phase 1: REPRODUCE
Run the minimal failing test command. Capture the exact error and stack trace.
- Do NOT hypothesize or speculate.
- Do NOT edit any source code.

### Phase 2: ISOLATE
Read the relevant modules and part files. Add temporary diagnostic logging if necessary.
- Trace variables and state transitions.
- Do not apply fixes during isolation.

### Phase 3: ROOT CAUSE ANALYSIS (RCA)
Analyze WHY the failure occurred at the isolated point.
Document in `.handoffs/<request-id>/44-debug-log.md`:
- **Symptom:** Exact error and test name.
- **Reproduction:** Command used.
- **Root Cause:** Explicit technical defect.

### Phase 4: TARGETED FIX
Apply the minimal sufficient fix addressing the root cause:
- Strictly observe Python 3.9 stdlib and Bash 3.2 constraints.
- Do not refactor unrelated code.

### Phase 5: VERIFICATION & REGRESSION CHECK
1. Run the failing test command — verify it now passes.
2. Run the full test suite (`python3 -m pytest tests/ -v`).
3. Hand the fix back to `y2-reviewer` for code review.
