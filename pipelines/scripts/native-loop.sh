#!/usr/bin/env bash
# ==============================================================================
# Native Azure CLI Loop Script
# Iterates over each VM sequentially, invokes RunCommand, and aggregates CSV
# Reference: az_devops/04-scale-and-operations.md
# ==============================================================================
set -euo pipefail

INPUT_CSV="${1:-}"
OUTPUT_CSV="${2:-$(dirname "$0")/native_result.csv}"
SNAPSHOT_TSV="${3:-}"

if [[ -z "$INPUT_CSV" || ! -f "$INPUT_CSV" ]]; then
  echo "Error: Input CSV '$INPUT_CSV' not found or not provided." >&2
  exit 1
fi

echo "======================================================================"
echo "Starting Native Loop Execution"
echo "Input Inventory: $INPUT_CSV"
echo "Output Report:   $OUTPUT_CSV"
[[ -n "$SNAPSHOT_TSV" && -f "$SNAPSHOT_TSV" ]] && echo "Power Snapshot:  $SNAPSHOT_TSV"
echo "======================================================================"

# Write CSV header
echo "vm_name,resource_group,subscription,os,random_a,random_b,random_sum,status" > "$OUTPUT_CSV"

START_TIME=$(date +%s)
TOTAL_VMS=0
SUCCESS_COUNT=0

# Read CSV skipping header
tail -n +2 "$INPUT_CSV" | while IFS=',' read -r vm_name rg sub os; do
  # Trim whitespace/carriage returns
  vm_name=$(echo "$vm_name" | tr -d '\r\n[:space:]')
  rg=$(echo "$rg" | tr -d '\r\n[:space:]')
  sub=$(echo "$sub" | tr -d '\r\n[:space:]')
  os=$(echo "$os" | tr -d '\r\n[:space:]')

  [[ -z "$vm_name" ]] && continue
  TOTAL_VMS=$((TOTAL_VMS + 1))

  echo -n "[$(date +'%T')] Visiting $vm_name ($os in $rg)... "

  # 1. Pre-check power state (Resource Graph snapshot or ARM Read fallback)
  if [[ -n "$SNAPSHOT_TSV" && -f "$SNAPSHOT_TSV" ]]; then
    POWER_STATE=$(awk -F'\t' -v vm="$vm_name" -v r="$rg" '
      tolower($1) == tolower(vm) && tolower($2) == tolower(r) { print $3; found = 1 }
      END { if (!found) print "Unknown" }' "$SNAPSHOT_TSV")
  else
    POWER_STATE=$(az vm get-instance-view \
      --subscription "$sub" \
      -g "$rg" \
      -n "$vm_name" \
      --query "instanceView.statuses[?starts_with(code, 'PowerState/')].code" \
      -o tsv 2>/dev/null || echo "Unknown")
  fi

  if [[ "$POWER_STATE" != "PowerState/running" ]]; then
    echo "SKIPPED (State: $POWER_STATE)"
    echo "$vm_name,$rg,$sub,$os,SKIPPED,SKIPPED,SKIPPED,$POWER_STATE" >> "$OUTPUT_CSV"
    continue
  fi

  # 2. Invoke RunCommand synchronously based on OS family
  CMD_ID="RunShellScript"
  SCRIPT_PAYLOAD='R1=$RANDOM; R2=$((1000 + RANDOM % 9000)); echo "$R1,$R2"'
  if [[ "$os" == *"win"* ]]; then
    CMD_ID="RunPowerShellScript"
    SCRIPT_PAYLOAD='$r1 = Get-Random -Minimum 1 -Maximum 32767; $r2 = Get-Random -Minimum 1000 -Maximum 9999; "$r1,$r2"'
  fi

  # Run Command call (blocking ~25-45 seconds)
  RAW_OUT=$(az vm run-command invoke \
    --subscription "$sub" \
    -g "$rg" \
    -n "$vm_name" \
    --command-id "$CMD_ID" \
    --scripts "$SCRIPT_PAYLOAD" \
    --query "value[0].message" \
    -o tsv 2>/dev/null || echo "ERROR")

  # 3. Clean up Azure Run Command stdout wrapper ("[stdout]\n...")
  CLEAN_OUT=$(echo "$RAW_OUT" | sed -e 's/\[stdout\]//g' -e 's/\[stderr\]//g' | tr -d '\r' | grep -v '^[[:space:]]*$' | tail -1 || echo "")

  if [[ "$CLEAN_OUT" =~ ^([0-9]+),([0-9]+)$ ]]; then
    RND_A="${BASH_REMATCH[1]}"
    RND_B="${BASH_REMATCH[2]}"
    RND_SUM=$((RND_A + RND_B))
    echo "OK (RND_A=$RND_A, RND_B=$RND_B, SUM=$RND_SUM)"
    echo "$vm_name,$rg,$sub,$os,$RND_A,$RND_B,$RND_SUM,OK" >> "$OUTPUT_CSV"
    SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
  else
    echo "FAILED ($RAW_OUT)"
    echo "$vm_name,$rg,$sub,$os,ERROR,ERROR,ERROR,FAILED" >> "$OUTPUT_CSV"
  fi
done

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

echo "======================================================================"
echo "Native Loop Complete in ${DURATION}s"
echo "Output written to: $OUTPUT_CSV"
echo "======================================================================"
