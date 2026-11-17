"""Shared skip helpers for private-workspace squad tests."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def skip_if_missing_assets(*rel_paths: str) -> None:
    """Skip the calling test module when any required workspace path is absent."""
    missing = [str(REPO_ROOT / rel) for rel in rel_paths if not (REPO_ROOT / rel).exists()]
    if missing:
        pytest.skip(
            "private agent workspace not present: " + ", ".join(missing),
            allow_module_level=True,
        )
