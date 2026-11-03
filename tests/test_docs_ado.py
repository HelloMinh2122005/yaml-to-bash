"""TP-40: Documentation validation tests.

Asserts that documentation for phase split, explain, export-ado, az tracing,
and conformance is complete, accurate, links resolve, and CLI examples parse.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
import pytest

from y2b.profile import PROFILE as B_PROFILE
from y2p.profile import PROFILE as P_PROFILE
from y2shared.cli import build_parser

REPO_ROOT = Path(__file__).resolve().parent.parent

DOC_FILES = {
    "README.md": REPO_ROOT / "README.md",
    "USER_GUIDE.md": REPO_ROOT / "USER_GUIDE.md",
    "az_devops_README.md": REPO_ROOT / "az_devops" / "README.md",
    "az_devops_02.md": REPO_ROOT / "az_devops" / "02-two-runcommand-pattern.md",
    "az_devops_03.md": REPO_ROOT / "az_devops" / "03-pipeline-reference.md",
}


def test_readme_and_user_guide_content():
    """Verify README.md and USER_GUIDE.md contain all required topics from AC-19."""
    for name in ["README.md", "USER_GUIDE.md"]:
        content = DOC_FILES[name].read_text(encoding="utf-8")

        assert "--phase" in content, f"{name} missing --phase"
        assert "explain" in content.lower(), f"{name} missing explain"
        assert "export-ado" in content.lower(), f"{name} missing export-ado"
        assert "[az]" in content, f"{name} missing [az]"
        assert "conformance:" in content.lower(), f"{name} missing conformance:"
        assert "redact" in content.lower() or "***" in content, f"{name} missing redact or ***"

        # Check statement about absolute path / command -p az not being traced
        has_abs_path_note = bool(
            re.search(r"absolute\s+path.*?not\s+traced", content, re.IGNORECASE)
            or re.search(r"command\s+-p\s+az.*?not\s+traced", content, re.IGNORECASE)
        )
        assert has_abs_path_note, f"{name} missing absolute path / command -p az tracing limit note"

        # Check statement that short flags such as -p are not redacted
        has_short_flag_note = bool(
            re.search(r"short\s+flags.*?not\s+redacted", content, re.IGNORECASE)
            or re.search(r"-p.*?not\s+redacted", content, re.IGNORECASE)
        )
        assert has_short_flag_note, f"{name} missing short flags not redacted note"


def test_az_devops_readme_content():
    """Verify az_devops/README.md has updated scope and references export-ado."""
    content = DOC_FILES["az_devops_README.md"].read_text(encoding="utf-8")
    assert "Converting `job.yaml` into ADO YAML" not in content
    assert "export-ado" in content
    assert "pipelines/generated" in content


def test_az_devops_02_pattern_content():
    """Verify az_devops/02-two-runcommand-pattern.md describes Phase 3 and power restore path."""
    content = DOC_FILES["az_devops_02.md"].read_text(encoding="utf-8")
    assert "phase 3" in content.lower()
    assert "power restore" in content.lower()
    assert "aborted-invoke restore path" in content.lower() or "aborted-invoke" in content.lower()


def test_az_devops_03_reference_content():
    """Verify az_devops/03-pipeline-reference.md references export-ado and pipelines/generated."""
    content = DOC_FILES["az_devops_03.md"].read_text(encoding="utf-8")
    assert "export-ado" in content
    assert "pipelines/generated" in content


def test_docs_markdown_links_resolve():
    """Verify every relative markdown link in the 5 documents resolves to an existing file."""
    link_re = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    for name, path in DOC_FILES.items():
        content = path.read_text(encoding="utf-8")
        # Remove code blocks so links inside code are not inspected
        content_no_code = re.sub(r"```.*?```", "", content, flags=re.DOTALL)
        content_no_code = re.sub(r"`[^`]+`", "", content_no_code)

        for match in link_re.finditer(content_no_code):
            target = match.group(2).strip()
            # Ignore absolute URLs and anchor-only links
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            # Strip anchor from path
            target_path = target.split("#")[0]
            if not target_path:
                continue
            resolved = (path.parent / target_path).resolve()
            assert resolved.exists(), f"Broken relative link in {name}: '{target}' -> {resolved}"


def test_fenced_commands_parse_with_cli():
    """Verify every fenced command starting with y2b or y2p parses with argparse."""
    parser_b = build_parser(B_PROFILE)
    parser_p = build_parser(P_PROFILE)

    # Collect fenced code blocks
    for name, path in DOC_FILES.items():
        content = path.read_text(encoding="utf-8")
        blocks = re.findall(r"```(?:bash|sh)?\n(.*?)```", content, re.DOTALL)
        for block in blocks:
            # Handle line continuations
            lines = []
            current = ""
            for raw_line in block.splitlines():
                stripped = raw_line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.endswith("\\"):
                    current += " " + stripped[:-1].strip()
                else:
                    current += " " + stripped
                    lines.append(current.strip())
                    current = ""

            for line in lines:
                match = re.match(r"^(?:bin/)?(y2b|y2p)\s+(.*)", line)
                if not match:
                    continue
                tool, args_str = match.groups()
                # Remove inline comment if any
                if " #" in args_str:
                    args_str = args_str.split(" #")[0].strip()

                try:
                    tokens = shlex.split(args_str)
                except ValueError:
                    continue

                # Ignore help invocations or non-command checks
                if not tokens or tokens[0] in ("--help", "-h"):
                    continue

                parser = parser_b if tool == "y2b" else parser_p
                try:
                    parser.parse_args(tokens)
                except SystemExit as exc:
                    pytest.fail(f"Fenced command failed CLI parse in {name}: '{tool} {args_str}' -> exit {exc.code}")

