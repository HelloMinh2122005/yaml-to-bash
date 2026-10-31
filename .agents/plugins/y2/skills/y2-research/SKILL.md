---
name: y2-research
description: Execute structured technical research and diagnostic Azure probes for yaml_to_bash.
---

# y2-research

Guides `y2-researcher` in executing research plans (`10-research-plan.md`) from Claude planning squad.

## Protocol
1. Read `10-research-plan.md` to identify all items `R-nn`.
2. For each item:
   - If method is `docs`: Search official documentation, verify citations.
   - If method is `code`: Trace local implementation and interfaces.
   - If method is `lab-probe`: Execute diagnostic probe strictly via `lab/run-lab.sh --evidence .handoffs/<id>/42-evidence/research/ -- <command>`.
3. Synthesize findings in `.handoffs/<id>/11-research-findings.md` using `templates/11-research-findings.md`.

