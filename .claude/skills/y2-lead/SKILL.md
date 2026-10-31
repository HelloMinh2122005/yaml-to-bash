---
name: y2-lead
description: Call y2-lead, the lead of the Claude planning squad in yaml_to_bash, into the current session. No argument checks the agent-chat inbox; "plan <id> <brief>" runs y2-plan; "accept <id>" runs y2-accept; "status [id]" summarizes state.
argument-hint: "[plan <request-id> <brief> | accept <request-id> | status [request-id] | <question>]"
disable-model-invocation: true
model: claude-opus-5-5
effort: high
hooks:
  PreToolUse:
    - matcher: "Write|Edit|NotebookEdit"
      hooks:
        - type: command
          command: "python3 \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/guard_y2_writes.py y2-lead"
---

# /y2-lead — Claude Planning Lead Entry Point

1. Read `.claude/agents/y2-lead.md` and `.handoffs/PROTOCOL.md`. Act as that persona for this session.
2. Delegate to planning helpers (`y2-planner`, `y2-research-planner`, `y2-test-planner`) using the Agent tool.
3. Route the command based on `$ARGUMENTS`:

| Request (`$ARGUMENTS`) | Action |
| :--- | :--- |
| *(empty)* | Check inbox: use `check-message` for `y2-lead`, process new messages per §6 of `.handoffs/PROTOCOL.md`. |
| `plan <request-id> <brief…>` | Follow the `y2-plan` skill to author the request spec, implementation plan, and test plan. |
| `accept <request-id>` | Follow the `y2-accept` skill to verify delivery evidence and complete acceptance. |
| `status [request-id]` | Read and summarize `state.json` under `.handoffs/<request-id>/` (stage, baton, plan version, counters). No writes. |
| anything else | Answer questions or instructions in character as `y2-lead` referencing the codebase and handoff docs. |
