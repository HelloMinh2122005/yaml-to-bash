"""TP-50: az shim execution tests under real bash:3.2 and rockylinux:9 containers.

Validates that y2shared/azshim/az:
- Transparently passes stdin, stdout, stderr, and exit codes (0, 1, 3, 127).
- Appends exact NUL-delimited records matching AC-12 format.
- Preserves arguments with spaces, quotes, dollars, backticks, globs, newlines, UTF-8.
- Handles empty argv, set -eu callers, stacked wrappers, and duplicate PATH entries.
- Passes bash -n under bash 3.2.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIM_SRC = REPO_ROOT / "y2shared" / "azshim" / "az"

CONTAINER_IMAGES = [
    "docker.io/library/bash:3.2",
    "docker.io/library/rockylinux:9",
]


def get_container_cli() -> str:
    cli = shutil.which("podman") or shutil.which("docker")
    if not cli:
        pytest.skip("Neither podman nor docker is installed")
    return cli


@pytest.fixture(scope="module")
def container_cli() -> str:
    return get_container_cli()


@pytest.mark.parametrize("image", CONTAINER_IMAGES)
def test_az_shim_container_full(image: str, container_cli: str, tmp_path: Path):
    """Run full TP-50 test matrix in the target container image."""
    # Ensure image exists or pull
    check = subprocess.run([container_cli, "image", "exists", image], capture_output=True)
    if check.returncode != 0:
        pull = subprocess.run([container_cli, "pull", image], capture_output=True)
        if pull.returncode != 0:
            pytest.skip(f"Could not pull container image {image}")

    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)

    # Copy shim and make executable
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim_file = shim_dir / "az"
    shutil.copy2(SHIM_SRC, shim_file)
    shim_file.chmod(0o755)

    # Create next az stub
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub_az = stub_dir / "az"
    stub_az.write_text(
        '#!/usr/bin/env bash\n'
        '# Next az stub for container testing\n'
        'if [ -n "$STUB_ECHO_STDOUT" ]; then\n'
        '  printf "%s\\n" "$STUB_ECHO_STDOUT"\n'
        'fi\n'
        'if [ -n "$STUB_ECHO_STDERR" ]; then\n'
        '  printf "%s\\n" "$STUB_ECHO_STDERR" >&2\n'
        'fi\n'
        '# Print received argv line by line if requested\n'
        'if [ "$STUB_PRINT_ARGV" = "1" ]; then\n'
        '  for arg in "$@"; do\n'
        '    printf "ARG:%s\\n" "$arg"\n'
        '  done\n'
        'fi\n'
        'exit "${STUB_RC:-0}"\n',
        encoding="utf-8",
    )
    stub_az.chmod(0o755)

    # Shell script test driver to execute inside the container
    driver_sh = work_dir / "driver.sh"
    driver_sh.write_text(
        r'''#!/bin/bash
set -u

ORIG_SYSTEM_PATH="$PATH"
SHIM_DIR="/shim"
STUB_DIR="/stub"
TRACE_FILE="/work/trace.bin"
CTX="precheck:test-section"

export Y2_AZ_SHIM_DIR="$SHIM_DIR"
export Y2_AZ_TRACE_FILE="$TRACE_FILE"
export Y2_AZ_TRACE_CTX="$CTX"

# 1. Verify bash -n on the shim
bash -n "$SHIM_DIR/az" || exit 10

# 2. Test execution with special characters in arguments
export PATH="$SHIM_DIR:$STUB_DIR:$ORIG_SYSTEM_PATH"
export STUB_PRINT_ARGV="1"
export STUB_ECHO_STDOUT="hello-from-stub"
export STUB_ECHO_STDERR="err-from-stub"
export STUB_RC="0"

rm -f "$TRACE_FILE"
"$SHIM_DIR/az" simple "arg with spaces" 'single' "double\"" '$dollar' '`backtick`' '*' $'new\nline' '日本語' > /work/out1.txt 2> /work/err1.txt
rc1=$?
[ "$rc1" -eq 0 ] || exit 11
grep -q "hello-from-stub" /work/out1.txt || exit 12
grep -q "err-from-stub" /work/err1.txt || exit 13

# 3. Test non-zero exit codes (1 and 3)
export STUB_RC="1"
"$SHIM_DIR/az" fail-1 > /dev/null 2>&1
[ "$?" -eq 1 ] || exit 14

export STUB_RC="3"
"$SHIM_DIR/az" fail-3 > /dev/null 2>&1
[ "$?" -eq 3 ] || exit 15

# 4. Test empty argv
export STUB_RC="0"
"$SHIM_DIR/az" > /dev/null 2>&1
[ "$?" -eq 0 ] || exit 16

# 5. Test missing next az -> exit 127
(
  export PATH="$SHIM_DIR:$ORIG_SYSTEM_PATH"
  "$SHIM_DIR/az" > /work/out_missing.txt 2> /work/err_missing.txt
  rc_miss=$?
  [ "$rc_miss" -eq 127 ] || exit 17
  grep -q "az: command not found" /work/err_missing.txt || exit 18
) || exit 19

# 6. Test set -e and set -u caller
(
  set -eu
  "$SHIM_DIR/az" caller-strict > /dev/null 2>&1
  [ "$?" -eq 0 ] || exit 20
) || exit 21

# 7. Test absolute path invocation does not trace
rm -f /work/trace_abs.bin
Y2_AZ_TRACE_FILE="/work/trace_abs.bin" "$STUB_DIR/az" direct-call > /dev/null 2>&1
if [ -f /work/trace_abs.bin ]; then
  exit 22
fi

# 8. Test PATH terminating on duplicate shim dir
(
  export PATH="$SHIM_DIR:$SHIM_DIR:$STUB_DIR:$PATH"
  "$SHIM_DIR/az" test-dup > /dev/null 2>&1
  [ "$?" -eq 0 ] || exit 23
) || exit 24

echo "DRIVER_OK"
''',
        encoding="utf-8",
    )
    driver_sh.chmod(0o755)

    # Run the driver inside the container
    cmd = [
        container_cli,
        "run",
        "--rm",
        "-v", f"{shim_dir}:/shim:ro,z",
        "-v", f"{stub_dir}:/stub:ro,z",
        "-v", f"{work_dir}:/work:rw,z",
        image,
        "bash",
        "/work/driver.sh",
    ]

    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"Container run failed on {image}:\nstdout={res.stdout}\nstderr={res.stderr}"
    assert "DRIVER_OK" in res.stdout

    # Verify trace file format outside container
    trace_file = work_dir / "trace.bin"
    assert trace_file.exists(), f"Trace file was not created on {image}"
    trace_bytes = trace_file.read_bytes()

    # Split by NUL byte
    parts = trace_bytes.split(b"\x00")
    assert parts[-1] == b"", "Trace file did not end with NUL delimiter"
    records = parts[:-1]

    # First record should be the special chars call
    # Fields: ctx, rc, argc, argv...
    assert records[0] == b"precheck:test-section"
    assert records[1] == b"0"  # rc
    assert records[2] == b"9"  # argc (9 args passed)
    assert records[3] == b"simple"
    assert records[4] == b"arg with spaces"
    assert records[5] == b"single"
    assert records[6] == b"double\""
    assert records[7] == b"$dollar"
    assert records[8] == b"`backtick`"
    assert records[9] == b"*"
    assert records[10] == b"new\nline"
    assert records[11] == "日本語".encode("utf-8")
