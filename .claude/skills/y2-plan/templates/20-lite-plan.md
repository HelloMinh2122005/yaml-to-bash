# Lite Plan: [Title]

## 1. Problem & Scope
<!-- Concise statement of need and boundaries -->

## 2. Acceptance Criteria
- **AC-1:** [Description & Expected Outcome]

## 3. Invariants
- **INV-1:** Python 3.9 stdlib compatibility.
- **INV-2:** Goldens remain byte-identical.

## 4. Task Cards
### T-01: [Title]
- **owner:** `y2-engineer`
- **scope.create:** []
- **scope.modify:** []
- **scope.must_not_touch:** [lab/, tests/golden/*]
- **behaviour:** <= 10 lines
- **covers:** [AC-1]
- **done_when:** [TP-01]
- **size:** S
- **depends_on:** []

## 5. Test Plan
### TP-01: [Title]
- **tier:** `T1`
- **covers:** AC-1
- **command:** `python3 -m pytest tests/ -v`
- **pass_criteria:** Clean exit code 0.
