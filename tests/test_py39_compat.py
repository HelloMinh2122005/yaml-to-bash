"""TP-71: Static Python 3.9 compatibility and bash 3.2 guard tests.

Performs AST and text analysis across all changed or added Python files
versus base commit af5d690 to ensure Python 3.9 compatibility, stdlib-only
dependencies (plus PyYAML and pytest), and bash 3.2 compatibility for the az shim.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_COMMIT = "af5d69049f4d8daa02ee08bc22227a5118cc6755"

ALLOWED_STDLIB = {
    "__future__", "argparse", "ast", "atexit", "base64", "collections",
    "concurrent", "contextlib", "copy", "csv", "dataclasses", "datetime",
    "enum", "fcntl", "fnmatch", "functools", "glob", "hashlib", "heapq",
    "importlib", "inspect", "io", "itertools", "json", "logging", "math",
    "os", "pathlib", "queue", "random", "re", "resource", "shlex", "shutil", "signal",
    "socket", "stat", "string", "subprocess", "sys", "tempfile", "threading",
    "time", "traceback", "typing", "unittest", "urllib", "uuid", "warnings",
}

ALLOWED_THIRD_PARTY = {"yaml"}
ALLOWED_TEST_ONLY = {"pytest"}
ALLOWED_INTERNAL = {"y2b", "y2p", "y2shared", "tests"}


def get_changed_or_added_py_files() -> list[Path]:
    """Return all .py files added or modified versus base commit af5d690."""
    tracked_res = subprocess.run(
        ["git", "diff", "--name-only", BASE_COMMIT, "--", "*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    tracked_files = [REPO_ROOT / line.strip() for line in tracked_res.stdout.splitlines() if line.strip()]

    untracked_res = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    untracked_files = [REPO_ROOT / line.strip() for line in untracked_res.stdout.splitlines() if line.strip()]

    all_files = sorted(set(tracked_files + untracked_files))
    # Exclude files outside repo or in venvs
    return [
        f for f in all_files
        if f.exists() and not any(part.startswith((".", "venv", "__pycache__")) for part in f.parts)
    ]


@pytest.fixture(scope="module")
def py_files() -> list[Path]:
    files = get_changed_or_added_py_files()
    assert len(files) >= 15, f"Expected at least 15 changed/added python files, found {len(files)}"
    return files


def test_ast_parse_feature_version_3_9(py_files: list[Path]):
    """Verify all changed/added Python files parse cleanly under Python 3.9 feature set."""
    for path in py_files:
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src, filename=str(path), feature_version=(3, 9))
            assert tree is not None
        except SyntaxError as exc:
            pytest.fail(f"SyntaxError parsing {path} with feature_version=(3, 9): {exc}")


def test_forbidden_py310_syntax_constructs(py_files: list[Path]):
    """Verify absence of Python 3.10+ syntax: Match statements, zip(strict=), dataclass slots/kw_only."""
    for path in py_files:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(path))

        for node in ast.walk(tree):
            # No match statements (ast.Match in Python 3.10+)
            assert type(node).__name__ != "Match", f"Found Match statement in {path} at line {node.lineno}"

            # Check calls for forbidden kwargs
            if isinstance(node, ast.Call):
                # zip(..., strict=...)
                if isinstance(node.func, ast.Name) and node.func.id == "zip":
                    for kw in node.keywords:
                        assert kw.arg != "strict", f"Found zip(strict=...) in {path} at line {node.lineno}"

                # @dataclass(slots=..., kw_only=...)
                func_name = ""
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                if func_name == "dataclass":
                    for kw in node.keywords:
                        assert kw.arg not in ("slots", "kw_only"), (
                            f"Found dataclass({kw.arg}=...) in {path} at line {node.lineno}"
                        )


def test_future_annotations_on_pipe_syntax(py_files: list[Path]):
    """Ensure modules using PEP 604 pipe type annotations import annotations from __future__."""
    for path in py_files:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(path))

        # Check if file has from __future__ import annotations
        has_future_annotations = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                for alias in node.names:
                    if alias.name == "annotations":
                        has_future_annotations = True
                        break

        # Check if type annotations use BitOr (X | Y)
        uses_pipe_annotation = False
        for node in ast.walk(tree):
            # Function annotations
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.returns and isinstance(node.returns, ast.BinOp) and isinstance(node.returns.op, ast.BitOr):
                    uses_pipe_annotation = True
                for arg in node.args.args + node.args.kwonlyargs:
                    if arg.annotation and isinstance(arg.annotation, ast.BinOp) and isinstance(arg.annotation.op, ast.BitOr):
                        uses_pipe_annotation = True
            # Variable annotations
            if isinstance(node, ast.AnnAssign) and isinstance(node.annotation, ast.BinOp) and isinstance(node.annotation.op, ast.BitOr):
                uses_pipe_annotation = True

        if uses_pipe_annotation:
            assert has_future_annotations, f"Module {path} uses pipe '|' type annotations without 'from __future__ import annotations'"


def test_import_allowlist(py_files: list[Path]):
    """Verify imported modules strictly conform to allowed stdlib and dependencies."""
    for path in py_files:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(path))
        is_test_file = "tests" in path.parts

        for node in ast.walk(tree):
            imported_modules: list[str] = []
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.append(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.append(node.module.split(".")[0])

            for mod in imported_modules:
                is_allowed = (
                    mod in ALLOWED_STDLIB
                    or mod in ALLOWED_THIRD_PARTY
                    or mod in ALLOWED_INTERNAL
                    or (is_test_file and mod in ALLOWED_TEST_ONLY)
                )
                assert is_allowed, f"Forbidden import '{mod}' in {path} at line {node.lineno}"


def test_az_shim_bash_3_2_compatibility():
    """Verify y2shared/azshim/az complies with bash 3.2 restrictions."""
    shim_path = REPO_ROOT / "y2shared" / "azshim" / "az"
    assert shim_path.exists(), "az shim does not exist"

    content = shim_path.read_text(encoding="utf-8")
    first_line = content.splitlines()[0]
    assert first_line in ("#!/usr/bin/env bash", "#!/bin/bash"), f"Unexpected shebang in shim: {first_line}"

    forbidden_constructs = [
        ("declare -A", "associative arrays require bash 4+"),
        ("mapfile", "mapfile requires bash 4+"),
        ("readarray", "readarray requires bash 4+"),
        (r"${", ",,}", "case modification requires bash 4+"),
        (r"${", "^^}", "case modification requires bash 4+"),
        ("[[ -v", "variable check [[ -v requires bash 4.2+"),
        ("&>>", "redirect &>> requires bash 4+"),
        ("|&", "pipe |& requires bash 4+"),
        ("local -n", "namerefs require bash 4.3+"),
        ("coproc", "coproc requires bash 4+"),
    ]

    for item in forbidden_constructs:
        if len(item) == 2:
            token, reason = item
            assert token not in content, f"Forbidden bash 4+ construct '{token}' found in {shim_path}: {reason}"
        elif len(item) == 3:
            pre, post, reason = item
            assert not (pre in content and post in content), f"Forbidden pattern found in {shim_path}: {reason}"

    # Verify bash -n
    res = subprocess.run(["bash", "-n", str(shim_path)], capture_output=True, text=True)
    assert res.returncode == 0, f"bash -n failed on {shim_path}:\n{res.stderr}"
