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


EXPECTED_CLAUDE = {
    "y2-lead": ("claude-opus-5-5", "high"),
    "y2-planner": ("claude-sonnet-5-5", "high"),
    "y2-research-planner": ("claude-sonnet-5-5", "medium"),
    "y2-test-planner": ("claude-sonnet-5-5", "high"),
}


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    import yaml
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return yaml.safe_load(parts[1]) or {}, parts[2]
    return {}, text


def test_claude_agent_models(repo_root: Path):
    agents_dir = repo_root / ".claude" / "agents"
    for name, (expected_model, expected_effort) in EXPECTED_CLAUDE.items():
        agent_file = agents_dir / f"{name}.md"
        assert agent_file.is_file(), f"Agent file {agent_file} not found"
        fm, _ = _parse_frontmatter(agent_file.read_text())
        assert fm.get("name") == name, f"Name mismatch in {name}.md: {fm.get('name')}"
        assert fm.get("model") == expected_model, f"Model mismatch in {name}.md"
        assert fm.get("effort") == expected_effort, f"Effort mismatch in {name}.md"


def test_claude_helpers_cannot_code(repo_root: Path):
    agents_dir = repo_root / ".claude" / "agents"
    helpers = ["y2-planner", "y2-research-planner", "y2-test-planner"]
    for h in helpers:
        agent_file = agents_dir / f"{h}.md"
        assert agent_file.is_file()
        fm, _ = _parse_frontmatter(agent_file.read_text())
        tools_str = str(fm.get("tools", ""))
        assert "Bash" not in tools_str, f"Helper {h} has Bash in tools"
        assert "Edit" not in tools_str, f"Helper {h} has Edit in tools"
        disallowed = str(fm.get("disallowedTools", ""))
        assert "Agent" in disallowed, f"Helper {h} missing Agent in disallowedTools"


def test_only_lead_uses_agent_chat(repo_root: Path):
    agents_dir = repo_root / ".claude" / "agents"
    lead_fm, _ = _parse_frontmatter((agents_dir / "y2-lead.md").read_text())
    assert "mcp__agent-chat" in str(lead_fm.get("tools", "")), "y2-lead missing agent-chat in tools"

    helpers = ["y2-planner", "y2-research-planner", "y2-test-planner"]
    for h in helpers:
        fm, _ = _parse_frontmatter((agents_dir / f"{h}.md").read_text())
        assert "mcp__agent-chat" not in str(fm.get("tools", "")), f"Helper {h} must not have agent-chat"


def test_claude_skills_frontmatter(repo_root: Path):
    skills_dir = repo_root / ".claude" / "skills"
    for sname in ["y2-lead", "y2-plan", "y2-accept"]:
        sfile = skills_dir / sname / "SKILL.md"
        assert sfile.is_file(), f"SKILL.md for {sname} not found"
        fm, body = _parse_frontmatter(sfile.read_text())
        assert fm.get("name") == sname, f"Skill name mismatch for {sname}"
        assert fm.get("description"), f"Skill {sname} has empty description"

        # Check templates referenced in body exist
        for match in re.finditer(r"templates/([a-zA-Z0-9_\-\.]+)", body):
            tpl_name = match.group(0)
            tpl_file = skills_dir / sname / tpl_name
            assert tpl_file.is_file(), f"Template {tpl_file} referenced in {sname} not found"


def test_claude_md_mentions_squad(repo_root: Path):
    claude_md = repo_root / "CLAUDE.md"
    assert claude_md.is_file()
    text = claude_md.read_text()
    assert "y2-lead" in text
    assert "y2-tech-lead" in text
    assert "lab/run-lab.sh" in text

