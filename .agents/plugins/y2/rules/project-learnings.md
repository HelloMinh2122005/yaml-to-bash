# Rule: Project Learnings

1. **Two-Phase Concurrency Architecture:** Phase 1 dispatches single runCommand POST per VM; Phase 2 polls execution state asynchronously through the event scheduler. Never block the dispatcher thread.
2. **Deterministic Merging:** Result files (`result.csv`, `result.log`) are merged from per-VM part files strictly matching input CSV row order.
3. **Fake Azure Testing:** Mock Azure CLI in `tests/fake_az/az` simulates throttling, conflict states, and network errors. Always verify against fake_az before considering real Azure testing.
4. **Lab Resource Management:** Azure for Students quota allows up to 6 vCPUs in `indiasouthcentral`. Keep lab VM run durations bounded under defined wall-clock budgets.
