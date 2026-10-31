---
name: y2-lab-testing
description: Procedures and safety rules for executing Tier 3 tests against the real Azure test lab.
---

# y2-lab-testing

Guides `y2-azure-tester` in safely running live tests against the 5-VM Azure test lab.

## Procedure
1. Confirm the approved test plan in `23-test-plan.md` authorizes Tier 3 testing.
2. Ensure service principal credentials are setup in `~/.config/y2-lab/sp.env`.
3. Execute all runs through `lab/run-lab.sh`:
   ```bash
   lab/run-lab.sh [--csv <csv_file>] --evidence .handoffs/<id>/42-evidence/t3/ -- <test_command>
   ```
4. Verify deallocation: Read generated `.handoffs/<id>/42-evidence/t3/post-state.txt` and confirm all 5 VMs are deallocated.

