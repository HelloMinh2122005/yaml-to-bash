#!/usr/bin/env bash
# Tier 3 aborted invoke + power restore driver (y2b, lin01)
# Bash 3.2 compatible. Usage: bash tests/t3/t3_abort_restore.sh <evidence_dir>

set -u

EVIDENCE_DIR="${1:-}"

if [ -z "$EVIDENCE_DIR" ]; then
  echo "Usage: $0 <evidence_dir>" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "$EVIDENCE_DIR"
EVIDENCE_DIR="$(cd "$EVIDENCE_DIR" && pwd)"

TOOL="$REPO_ROOT/bin/y2b"
EXAMPLE_DIR="$REPO_ROOT/examples/linux"
SIGINT_SHIM_DIR="$REPO_ROOT/tests/t3/sigint_after_first_post"

WORK="$EVIDENCE_DIR/work"
rm -rf "$WORK"
mkdir -p "$WORK"

# Copy example files
cp -r "$EXAMPLE_DIR"/* "$WORK/"

# Verify CSV byte-identity
cmp -s "$EXAMPLE_DIR/list_vm.csv" "$WORK/list_vm.csv" || {
  echo "Error: list_vm.csv copy in work does not match source" >&2
  exit 1
}

# Clean marker file
SIGINT_MARKER="$EVIDENCE_DIR/sigint_marker"
rm -f "$SIGINT_MARKER"
export Y2_SIGINT_MARKER="$SIGINT_MARKER"

# 1. Invoke phase with sigint wrapper
ORIG_PATH="$PATH"
echo "=== Running invoke phase with sigint shim (parallel=1) ==="
export PATH="$SIGINT_SHIM_DIR:$PATH"

set +e
"$TOOL" run "$WORK/job.yaml" --phase invoke --parallel 1 -y > "$EVIDENCE_DIR/invoke.stdout" 2> "$EVIDENCE_DIR/invoke.stderr"
INVOKE_RC=$?
set -e
echo "$INVOKE_RC" > "$EVIDENCE_DIR/invoke.rc"

# Restore original PATH
export PATH="$ORIG_PATH"

if [ "$INVOKE_RC" -ne 3 ]; then
  echo "Error: Expected invoke.rc == 3, got $INVOKE_RC" >&2
  cat "$EVIDENCE_DIR/invoke.stderr" >&2
  exit 1
fi

# Copy intermediate evidence
if [ -f "$WORK/.result.parts/run.json" ]; then
  cp "$WORK/.result.parts/run.json" "$EVIDENCE_DIR/run.after-invoke.json"
fi
if [ -f "$WORK/.result.parts/collect_list.jsonl" ]; then
  cp "$WORK/.result.parts/collect_list.jsonl" "$EVIDENCE_DIR/collect_list.after-invoke.jsonl"
fi
POWER_FILE=$(ls "$WORK/.result.parts/"*lin01*.power 2>/dev/null | head -n 1 || true)
if [ -n "$POWER_FILE" ] && [ -f "$POWER_FILE" ]; then
  cp "$POWER_FILE" "$EVIDENCE_DIR/power.lin01.after-invoke.json"
fi

# 2. Collect phase to restore power and collect lin01
echo "=== Running collect phase to restore power and collect lin01 ==="
set +e
"$TOOL" run "$WORK/job.yaml" --phase collect -y > "$EVIDENCE_DIR/collect.stdout" 2> "$EVIDENCE_DIR/collect.stderr"
COLLECT_RC=$?
set -e
echo "$COLLECT_RC" > "$EVIDENCE_DIR/collect.rc"

# Copy final results
if [ -f "$WORK/result.csv" ]; then
  cp "$WORK/result.csv" "$EVIDENCE_DIR/result.csv"
fi
if [ -f "$WORK/result.log" ]; then
  cp "$WORK/result.log" "$EVIDENCE_DIR/result.log"
fi

if [ "$COLLECT_RC" -ne 2 ]; then
  echo "Error: Expected collect.rc == 2, got $COLLECT_RC" >&2
  exit 1
fi

echo "=== T3 abort-restore completed successfully (invoke.rc=3, collect.rc=2) ==="
exit 0

