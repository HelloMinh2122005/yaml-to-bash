# Rule: Handoff Protocol

1. Read `.handoffs/PROTOCOL.md` at the start of every turn; its contracts strictly supersede local prompt instructions.
2. Only write to Antigravity-owned handoff files: `11-research-findings.md`, `30-questions.md`, `40-delivery-report.md`, `41-diff.patch`, `42-evidence/**`, `43-code-review-cN.md`, `44-debug-log.md`, and `state.json` (when holding baton).
3. Never edit `.handoffs/PROTOCOL.md`, `.claude/**`, or `docs/superpowers/**`.
4. Honor the Definition of Ready before accepting any task card for implementation.
5. Respect loop caps in `state.json` (`max_clarify_rounds`, `max_internal_fix_cycles`); never loop beyond caps.
6. Baton passing requires updating `state.json` before sending any agent-chat message.

