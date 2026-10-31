#!/usr/bin/env bash
set -euo pipefail

SUBSCRIPTION="79f7da80-0c64-4e4b-9ed4-316a88142152"
RESOURCE_GROUP="rg-lab-10vms"
WAIT_FLAG="--no-wait"

usage() {
  cat <<EOF
Usage: $(basename "$0") [command] [vm_name] [options]

Commands:
  toggle [vm_name]   Start if stopped, deallocate if running (default)
  start  [vm_name]   Start all VMs or a specific VM
  stop   [vm_name]   Deallocate all VMs or a specific VM (stops charges)
  status             Show power state and IPs of VMs

Options:
  --wait             Wait for operation to complete (default: --no-wait)
  --no-wait          Trigger operation in background without waiting
  -g, --resource-group <rg>   Override resource group (default: ${RESOURCE_GROUP})
  -s, --subscription <sub>    Override subscription (default: "${SUBSCRIPTION}")
EOF
  exit 0
}

get_vm_ids() {
  local target="${1:-}"
  if [[ -n "$target" ]]; then
    az vm show --name "$target" -g "$RESOURCE_GROUP" --subscription "$SUBSCRIPTION" --query "id" -o tsv 2>/dev/null || true
  else
    az vm list -g "$RESOURCE_GROUP" --subscription "$SUBSCRIPTION" --query "[].id" -o tsv 2>/dev/null || true
  fi
}

start_vms() {
  local target="${1:-}"
  local ids
  ids="$(get_vm_ids "$target")"

  if [[ -z "$ids" ]]; then
    echo "Error: No VM(s) found in '$RESOURCE_GROUP' under subscription '$SUBSCRIPTION'." >&2
    exit 1
  fi

  echo "Starting VM(s) in '$RESOURCE_GROUP'..."
  # shellcheck disable=SC2086
  az vm start --ids $ids --subscription "$SUBSCRIPTION" $WAIT_FLAG
  echo "Start initiated."
}

stop_vms() {
  local target="${1:-}"
  local ids
  ids="$(get_vm_ids "$target")"

  if [[ -z "$ids" ]]; then
    echo "Error: No VM(s) found in '$RESOURCE_GROUP' under subscription '$SUBSCRIPTION'." >&2
    exit 1
  fi

  echo "Deallocating VM(s) in '$RESOURCE_GROUP'..."
  # shellcheck disable=SC2086
  az vm deallocate --ids $ids --subscription "$SUBSCRIPTION" $WAIT_FLAG
  echo "Deallocate initiated."
}

status_vms() {
  az vm list -d \
    --resource-group "$RESOURCE_GROUP" \
    --subscription "$SUBSCRIPTION" \
    --query "[].{Name:name, PowerState:powerState, PrivateIP:privateIps}" \
    --output table
}

toggle_vms() {
  local target="${1:-}"
  local states
  if [[ -n "$target" ]]; then
    states="$(az vm show -d --name "$target" -g "$RESOURCE_GROUP" --subscription "$SUBSCRIPTION" --query "powerState" -o tsv 2>/dev/null || true)"
  else
    states="$(az vm list -d -g "$RESOURCE_GROUP" --subscription "$SUBSCRIPTION" --query "[].powerState" -o tsv 2>/dev/null || true)"
  fi

  if [[ -z "$states" ]]; then
    echo "Error: No VMs found in '$RESOURCE_GROUP' under subscription '$SUBSCRIPTION'." >&2
    exit 1
  fi

  if echo "$states" | grep -qi "running"; then
    echo "Running VM(s) detected. Deallocating..."
    stop_vms "$target"
  else
    echo "VM(s) are stopped. Starting..."
    start_vms "$target"
  fi
}

# Parse arguments
command="toggle"
target_vm=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    start|stop|deallocate|status|toggle)
      command="$1"
      [[ "$command" == "deallocate" ]] && command="stop"
      shift
      if [[ $# -gt 0 && ! "$1" =~ ^- ]]; then
        target_vm="$1"
        shift
      fi
      ;;
    --wait)
      WAIT_FLAG=""
      shift
      ;;
    --no-wait)
      WAIT_FLAG="--no-wait"
      shift
      ;;
    -g|--resource-group)
      RESOURCE_GROUP="$2"
      shift 2
      ;;
    -s|--subscription)
      SUBSCRIPTION="$2"
      shift 2
      ;;
    -h|--help|help)
      usage
      ;;
    *)
      if [[ -z "$target_vm" && ! "$1" =~ ^- ]]; then
        target_vm="$1"
        shift
      else
        echo "Unknown argument: $1" >&2
        usage
      fi
      ;;
  esac
done

case "$command" in
  start)  start_vms "$target_vm" ;;
  stop)   stop_vms "$target_vm" ;;
  status) status_vms ;;
  toggle) toggle_vms "$target_vm" ;;
esac
