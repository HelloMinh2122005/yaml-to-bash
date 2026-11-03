#!/usr/bin/env bash
# Tier 3 split-run driver for y2b (linux) and y2p (windows)
# Bash 3.2 compatible. Usage: bash tests/t3/t3_split_run.sh <linux|windows> <evidence_dir>

set -u

OS_NAME="${1:-}"
EVIDENCE_DIR="${2:-}"

if [ -z "$OS_NAME" ] || [ -z "$EVIDENCE_DIR" ]; then
  echo "Usage: $0 <linux|windows> <evidence_dir>" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "$EVIDENCE_DIR"
EVIDENCE_DIR="$(cd "$EVIDENCE_DIR" && pwd)"

if [ "$OS_NAME" = "linux" ]; then
  TOOL="$REPO_ROOT/bin/y2b"
  EXAMPLE_DIR="$REPO_ROOT/examples/linux"
elif [ "$OS_NAME" = "windows" ]; then
  TOOL="$REPO_ROOT/bin/y2p"
  EXAMPLE_DIR="$REPO_ROOT/examples/windows"
else
  echo "Unknown OS: $OS_NAME (expected linux or windows)" >&2
  exit 1
fi

WORK_A="$EVIDENCE_DIR/work-a"
WORK_B="$EVIDENCE_DIR/work-b"
rm -rf "$WORK_A" "$WORK_B"
mkdir -p "$WORK_A" "$WORK_B"

# 1. Copy example files to work-a and work-b
cp -r "$EXAMPLE_DIR"/* "$WORK_A/"
cp -r "$EXAMPLE_DIR"/* "$WORK_B/"

# Verify CSV byte-identity
cmp -s "$EXAMPLE_DIR/list_vm.csv" "$WORK_A/list_vm.csv" || {
  echo "Error: list_vm.csv copy in work-a does not match source" >&2
  exit 1
}
cmp -s "$EXAMPLE_DIR/list_vm.csv" "$WORK_B/list_vm.csv" || {
  echo "Error: list_vm.csv copy in work-b does not match source" >&2
  exit 1
}

# 2. Validate
echo "=== Running validate ==="
"$TOOL" validate "$WORK_A/job.yaml"

# 3. Explain
echo "=== Generating explain outputs ==="
"$TOOL" explain "$WORK_A/job.yaml" --format text > "$EVIDENCE_DIR/explain.txt"
"$TOOL" explain "$WORK_A/job.yaml" --format md > "$EVIDENCE_DIR/explain.md"
"$TOOL" explain "$WORK_A/job.yaml" --format json > "$EVIDENCE_DIR/explain.json"

# 4. Invoke phase
echo "=== Running invoke phase in work-a ==="
set +e
"$TOOL" run "$WORK_A/job.yaml" --phase invoke -y > "$EVIDENCE_DIR/invoke.stdout" 2> "$EVIDENCE_DIR/invoke.stderr"
INVOKE_RC=$?
set -e
echo "$INVOKE_RC" > "$EVIDENCE_DIR/invoke.rc"

if [ "$INVOKE_RC" -ne 0 ]; then
  echo "Error: invoke phase exited with rc=$INVOKE_RC" >&2
  cat "$EVIDENCE_DIR/invoke.stderr" >&2
  exit "$INVOKE_RC"
fi

# Ensure work-a has no result.csv
if [ -f "$WORK_A/result.csv" ]; then
  echo "Error: invoke phase created result.csv in work-a" >&2
  exit 1
fi

# 5. Tar parts
echo "=== Taring parts from work-a ==="
tar -C "$WORK_A" -cf "$EVIDENCE_DIR/parts.tar" .result.parts
(cd "$EVIDENCE_DIR" && sha256sum parts.tar > parts.tar.sha256)
cp "$WORK_A/.result.parts/run.json" "$EVIDENCE_DIR/run.before.json"

# 6. Extract into work-b
echo "=== Extracting parts into work-b ==="
tar -C "$WORK_B" -xf "$EVIDENCE_DIR/parts.tar"

# 7. Agentless delay
echo "=== Waiting 20 seconds for VM execution ==="
sleep 20

# 8. Collect phase with retry on still_running
COLLECT_PASS=1
COLLECT_SUCCESS=0

while [ "$COLLECT_PASS" -le 4 ]; do
  echo "=== Running collect phase pass $COLLECT_PASS in work-b ==="
  set +e
  "$TOOL" run "$WORK_B/job.yaml" --phase collect -y --keep-parts > "$EVIDENCE_DIR/collect.$COLLECT_PASS.stdout" 2> "$EVIDENCE_DIR/collect.$COLLECT_PASS.stderr"
  COLLECT_RC=$?
  set -e
  echo "$COLLECT_RC" > "$EVIDENCE_DIR/collect.$COLLECT_PASS.rc"

  if [ "$COLLECT_RC" -eq 0 ]; then
    echo "Collect phase succeeded on pass $COLLECT_PASS"
    COLLECT_SUCCESS=1
    break
  elif [ "$COLLECT_RC" -eq 2 ]; then
    # Check if failure is only due to still_running
    if grep -q "ERROR:still_running" "$WORK_B/result.csv" 2>/dev/null || grep -q "Left running" "$EVIDENCE_DIR/collect.$COLLECT_PASS.stdout" 2>/dev/null; then
      echo "Some VMs still running remotely, sleeping 30s before retry..."
      sleep 30
      COLLECT_PASS=$((COLLECT_PASS + 1))
    else
      echo "Collect failed with rc=2 without still_running" >&2
      break
    fi
  else
    echo "Collect failed with terminal rc=$COLLECT_RC" >&2
    break
  fi
done

# 9. Copy final evidence files
if [ -f "$WORK_B/result.csv" ]; then
  cp "$WORK_B/result.csv" "$EVIDENCE_DIR/result.csv"
fi
if [ -f "$WORK_B/result.log" ]; then
  cp "$WORK_B/result.log" "$EVIDENCE_DIR/result.log"
fi
if [ -f "$WORK_B/.result.parts/run.json" ]; then
  cp "$WORK_B/.result.parts/run.json" "$EVIDENCE_DIR/run.after.json"
fi
if [ -f "$WORK_B/.result.parts/collect_list.jsonl" ]; then
  cp "$WORK_B/.result.parts/collect_list.jsonl" "$EVIDENCE_DIR/collect_list.jsonl"
elif [ -f "$WORK_A/.result.parts/collect_list.jsonl" ]; then
  cp "$WORK_A/.result.parts/collect_list.jsonl" "$EVIDENCE_DIR/collect_list.jsonl"
fi

if [ "$COLLECT_SUCCESS" -ne 1 ]; then
  echo "Collect phase did not succeed after $COLLECT_PASS attempts" >&2
  exit 1
fi

echo "=== T3 split-run completed successfully for $OS_NAME ==="
exit 0

