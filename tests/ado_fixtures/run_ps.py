# tests/ado_fixtures/run_ps.py - Container-based PowerShell test runner for Tier 2 tests
# Pure ASCII, LF line endings, BOM-less UTF-8, Python 3.9 compatible (stdlib only).

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence, Union

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

DEFAULT_PWSH_IMAGES = (
    "mcr.microsoft.com/powershell:7.4-alpine",
    "mcr.microsoft.com/powershell:7.4-ubuntu-22.04",
)


@dataclass
class PwshResult:
    """Execution result from container PowerShell invocation."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        return self.returncode == 0


def get_container_cli() -> Optional[str]:
    """Return available container runtime executable (podman preferred, then docker)."""
    if shutil.which("podman"):
        return "podman"
    if shutil.which("docker"):
        return "docker"
    return None


def get_available_image(cli: Optional[str] = None) -> Optional[str]:
    """Find a locally available or pullable PowerShell container image."""
    active_cli = cli or get_container_cli()
    if not active_cli:
        return None

    # Check environment override first
    env_img = os.environ.get("PWSH_CONTAINER_IMAGE")
    candidate_images = [env_img] if env_img else list(DEFAULT_PWSH_IMAGES)

    # First pass: check for locally present image
    for img in candidate_images:
        check = subprocess.run([active_cli, "image", "inspect", img], capture_output=True)
        if check.returncode == 0:
            return img

    # Second pass: attempt pull for preferred images
    for img in candidate_images:
        pull = subprocess.run([active_cli, "pull", img], capture_output=True)
        if pull.returncode == 0:
            return img

    return None


def require_container_runtime(
    cli: Optional[str] = None,
    image: Optional[str] = None,
) -> tuple[str, str]:
    """Ensure container CLI and image are present; skip gracefully under pytest if not."""
    active_cli = cli or get_container_cli()
    if not active_cli:
        skip_msg = "Neither podman nor docker is installed"
        if "pytest" in sys.modules:
            import pytest

            pytest.skip(skip_msg)
        raise RuntimeError(skip_msg)

    active_image = image or get_available_image(active_cli)
    if not active_image:
        skip_msg = "Could not find or pull a PowerShell container image"
        if "pytest" in sys.modules:
            import pytest

            pytest.skip(skip_msg)
        raise RuntimeError(skip_msg)

    return active_cli, active_image


def run_pwsh_script(
    script_text: str,
    work_dir: Optional[Union[str, Path]] = None,
    mount_repo: bool = True,
    timeout: int = 60,
    env: Optional[Mapping[str, str]] = None,
    extra_args: Optional[Sequence[str]] = None,
    image: Optional[str] = None,
) -> PwshResult:
    """Run inline PowerShell script inside a container with hermetic network (--network none).

    Mounts:
      - work_dir as /work:z (working directory)
      - repo root as /repo:ro,z (read-only)
    """
    cli, active_image = require_container_runtime(image=image)

    # Use existing work_dir or create temporary directory
    temp_dir_ctx = None
    if work_dir is None:
        temp_dir_ctx = tempfile.TemporaryDirectory(prefix="pwsh_test_")
        target_dir = Path(temp_dir_ctx.name)
    else:
        target_dir = Path(work_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

    try:
        script_file = target_dir / "test_exec.ps1"
        script_file.write_text(script_text, encoding="utf-8")

        cmd = [
            cli,
            "run",
            "--rm",
            "--network",
            "none",
            "-v",
            f"{target_dir}:/work:z",
        ]

        if mount_repo:
            cmd.extend(["-v", f"{REPO_ROOT}:/repo:ro,z"])

        cmd.extend([
            "-e",
            "Y2_REPO_DIR=/repo",
            "-e",
            "Y2_WORK_DIR=/work",
        ])

        if env:
            for k, v in env.items():
                cmd.extend(["-e", f"{k}={v}"])

        cmd.extend([
            "-w",
            "/work",
            active_image,
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            "/work/test_exec.ps1",
        ])

        if extra_args:
            cmd.extend(extra_args)

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        return PwshResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    finally:
        if temp_dir_ctx is not None:
            temp_dir_ctx.cleanup()


def run_pwsh_command(
    command: str,
    work_dir: Optional[Union[str, Path]] = None,
    mount_repo: bool = True,
    timeout: int = 60,
    env: Optional[Mapping[str, str]] = None,
    image: Optional[str] = None,
) -> PwshResult:
    """Run a single PowerShell -Command string inside the container."""
    return run_pwsh_script(
        script_text=command,
        work_dir=work_dir,
        mount_repo=mount_repo,
        timeout=timeout,
        env=env,
        image=image,
    )


def run_pwsh_file(
    file_path: Union[str, Path],
    args: Optional[Sequence[str]] = None,
    work_dir: Optional[Union[str, Path]] = None,
    mount_repo: bool = True,
    timeout: int = 60,
    env: Optional[Mapping[str, str]] = None,
    image: Optional[str] = None,
) -> PwshResult:
    """Execute an existing PowerShell script file inside the container."""
    cli, active_image = require_container_runtime(image=image)
    abs_path = Path(file_path).resolve()

    temp_dir_ctx = None
    if work_dir is None:
        temp_dir_ctx = tempfile.TemporaryDirectory(prefix="pwsh_test_")
        target_dir = Path(temp_dir_ctx.name)
    else:
        target_dir = Path(work_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Determine in-container path
        try:
            rel_repo = abs_path.relative_to(REPO_ROOT)
            container_file = f"/repo/{rel_repo.as_posix()}"
        except ValueError:
            # Not in repo, copy to work_dir
            dest_file = target_dir / abs_path.name
            shutil.copy2(abs_path, dest_file)
            container_file = f"/work/{abs_path.name}"

        cmd = [
            cli,
            "run",
            "--rm",
            "--network",
            "none",
            "-v",
            f"{target_dir}:/work:z",
        ]

        if mount_repo:
            cmd.extend(["-v", f"{REPO_ROOT}:/repo:ro,z"])

        cmd.extend([
            "-e",
            "Y2_REPO_DIR=/repo",
            "-e",
            "Y2_WORK_DIR=/work",
        ])

        if env:
            for k, v in env.items():
                cmd.extend(["-e", f"{k}={v}"])

        cmd.extend([
            "-w",
            "/work",
            active_image,
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            container_file,
        ])

        if args:
            cmd.extend(args)

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        return PwshResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    finally:
        if temp_dir_ctx is not None:
            temp_dir_ctx.cleanup()
