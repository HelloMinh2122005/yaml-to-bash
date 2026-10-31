from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
import pytest

# Ensure lab directory is on sys.path
_repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_repo_root / "lab"))


@pytest.fixture
def repo_root() -> Path:
    return _repo_root


@pytest.fixture
def lab_env(repo_root: Path):
    from labenv import load_lab_env
    return load_lab_env(str(repo_root / "lab" / "lab.env"))


@pytest.fixture
def stub_az(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    calls_file = tmp_path / "az_calls.txt"
    calls_file.touch()

    az_script = bin_dir / "az"
    az_script.write_text(
        f"""#!/usr/bin/env bash
echo "$*" >> "{calls_file}"
if [ -n "${{STUB_AZ_STDOUT:-}}" ]; then
    echo -e "$STUB_AZ_STDOUT"
fi
exit "${{STUB_AZ_RC:-0}}"
"""
    )
    az_script.chmod(az_script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir, calls_file
