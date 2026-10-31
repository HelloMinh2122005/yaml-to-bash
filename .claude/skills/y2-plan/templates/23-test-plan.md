# Three-Tier Test Plan: [Title]

## Tiers & Matrix

### TP-01: [Unit / Integration Title]
- **tier:** `T1`
- **covers:** AC-1
- **description:** [Scenario description]
- **command:** `python3 -m pytest tests/ -v`
- **pass_criteria:** Exit code 0, all tests pass.

### TP-02: [Container Test Title]
- **tier:** `T2`
- **covers:** AC-2
- **description:** [Container scenario description]
- **command:** `python3 -m pytest tests/test_compiler.py -v`
- **pass_criteria:** Payload executes cleanly in container.

### TP-03: [Real Azure Lab Test Title]
- **tier:** `T3`
- **covers:** AC-3
- **description:** [Real Azure execution on lab VMs]
- **target_vms:** [lin01, win01]
- **command:** `lab/run-lab.sh --evidence .handoffs/<id>/42-evidence/t3/ -- y2b run examples/job.yaml -y`
- **budget_seconds:** 1800
- **pass_criteria:** Real execution completes, result.csv matches golden, all 5 lab VMs deallocated.
