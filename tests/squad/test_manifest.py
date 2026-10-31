from __future__ import annotations

import re
from pathlib import Path
import pytest


def test_protocol_covers_contract(repo_root: Path):
    protocol_path = repo_root / ".handoffs" / "PROTOCOL.md"
    assert protocol_path.is_file(), "PROTOCOL.md does not exist"
    content = protocol_path.read_text()

    # Stages
    stages = [
        "DRAFTING",
        "AWAITING_RESEARCH_APPROVAL",
        "RESEARCHING",
        "AWAITING_PLAN_APPROVAL",
        "PLAN_READY",
        "CLARIFYING",
        "IMPLEMENTING",
        "INTERNAL_QA",
        "DELIVERED",
        "CHANGES_REQUESTED",
        "AWAITING_SIGNOFF",
        "DONE",
        "ESCALATED",
    ]
    for stage in stages:
        assert stage in content, f"Missing stage '{stage}' in PROTOCOL.md"

    # Message types
    message_types = [
        "RESEARCH_REQUEST",
        "RESEARCH_DONE",
        "PLAN_READY",
        "CLARIFY",
        "BLOCKED",
        "ANSWER",
        "PLAN_AMENDMENT",
        "DELIVERY",
        "ACCEPTANCE_RESULT",
        "DONE",
        "ESCALATED",
    ]
    for msg in message_types:
        assert msg in content, f"Missing message type '{msg}' in PROTOCOL.md"

    # Addresses
    assert "claude-code/y2-lead" in content
    assert "antigravity/y2-tech-lead" in content

    # File names from spec §6.2
    files = [
        "state.json",
        "00-brief.md",
        "10-research-plan.md",
        "11-research-findings.md",
        "20-spec.md",
        "21-plan.md",
        "22-tasks.md",
        "23-test-plan.md",
        "30-questions.md",
        "31-answers.md",
        "40-delivery-report.md",
        "41-diff.patch",
        "42-evidence/",
        "43-code-review-cN.md",
        "44-debug-log.md",
        "50-acceptance-cN.md",
        "90-retro.md",
        "20-lite-plan.md",
    ]
    for f in files:
        assert f in content, f"Missing file '{f}' in PROTOCOL.md"

    # Counters and caps
    assert "max_research_rounds" in content and "2" in content
    assert "max_clarify_rounds" in content and "2" in content
    assert "max_internal_fix_cycles" in content and "3" in content
    assert "max_acceptance_cycles" in content and "2" in content
