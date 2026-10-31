# Squad Learnings (Durable)

Durable operational learnings for the y2 squad across Claude planning and Antigravity engineering. Keep concise (<100 lines).

- **Concurrency & Concurrency Limits:** Target scale is 500+ VMs. Do not block the dispatcher thread; collection uses event scheduler in Phase 2.
- **Power Management & Azure Quotas:** Azure for Students has a 6 vCPU quota in indiasouthcentral. All real lab runs must execute via `lab/run-lab.sh` and end with all 5 VMs deallocated.
- **Python 3.9 Stdlib & PyYAML:** No external dependencies beyond PyYAML; preserve stdlib 3.9 compatibility across all modules.

