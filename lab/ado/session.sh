#!/usr/bin/env bash
# lab/ado/session.sh: Lab session wrapper that starts all 5 VMs, waits for the
# self-hosted Windows agent to come online (<= 5 min), executes the requested command,
# and returns its exit code. Intended to run inside lab/run-lab.sh.

set -euo pipefail

# Ensure set +x so secrets are never echoed to stdout/stderr
set +x

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAB_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
    echo "Usage: $0 <command> [args...]" >&2
    echo "Starts all 5 VMs, waits for win01 ADO agent to be online, and runs <command>." >&2
    exit "${1:-64}"
}

if [ $# -eq 0 ]; then
    usage 64
fi

if [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
    usage 0
fi

# 1. Start all 5 lab VMs
echo "[session] Starting all 5 lab VMs via toogle_vm.sh start --wait..." >&2
bash "$LAB_DIR/toogle_vm.sh" start --wait

# 2. Read credentials from ~/.config/y2-lab/ado.env or $ADO_ENV_FILE
CONFIG_DIR="${Y2_LAB_CONFIG_DIR:-$HOME/.config/y2-lab}"
ADO_ENV_FILE="${ADO_ENV_FILE:-$CONFIG_DIR/ado.env}"

ADO_ORG_URL="${ADO_ORG_URL:-}"
ADO_PROJECT="${ADO_PROJECT:-}"
ADO_PAT="${ADO_PAT:-}"

if [ -f "$ADO_ENV_FILE" ]; then
    while IFS='=' read -r key val || [ -n "$key" ]; do
        key=$(printf '%s\n' "$key" | tr -d ' \t\r\n')
        [ -z "$key" ] && continue
        case "$key" in
            \#*) continue ;;
        esac
        val=$(printf '%s\n' "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
        case "$val" in
            \"*\"|\'*\' )
                val="${val#?}"
                val="${val%?}"
                ;;
        esac
        case "$key" in
            ADO_ORG_URL) ADO_ORG_URL="$val" ;;
            ADO_PROJECT) ADO_PROJECT="$val" ;;
            ADO_PAT) ADO_PAT="$val" ;;
        esac
    done < "$ADO_ENV_FILE"
fi

if [ -z "$ADO_ORG_URL" ]; then
    echo "Error: ADO_ORG_URL is missing (not set in environment or in $ADO_ENV_FILE)" >&2
    exit 1
fi

if [ -z "$ADO_PAT" ]; then
    echo "Error: ADO_PAT is missing (not set in environment or in $ADO_ENV_FILE)" >&2
    exit 1
fi

ORG_URL="${ADO_ORG_URL%/}"
B64_PAT="$(printf ':%s' "$ADO_PAT" | base64 | tr -d '\r\n')"
POOL_NAME="${ADO_POOL_NAME:-Production Windows Agent}"
AGENT_NAME="${ADO_AGENT_NAME:-win01}"

# 3. Locate pool ID
echo "[session] Resolving pool ID for '$POOL_NAME'..." >&2
POOLS_JSON=$(curl -s -S -f \
    -H "Authorization: Basic $B64_PAT" \
    -H "Accept: application/json;api-version=7.0" \
    "$ORG_URL/_apis/distributedtask/pools?api-version=7.0" 2>/dev/null || echo "{}")

POOL_ID=$(python3 -c '
import sys, json
target = sys.argv[1]
try:
    doc = json.load(sys.stdin)
    for p in doc.get("value", []):
        if p.get("name") == target:
            print(p.get("id", ""))
            sys.exit(0)
    sys.exit(1)
except Exception:
    sys.exit(2)
' "$POOL_NAME" <<< "$POOLS_JSON" || true)

if [ -z "$POOL_ID" ]; then
    echo "Error: Agent pool '$POOL_NAME' not found in ADO organization $ORG_URL." >&2
    exit 1
fi

# 4. Poll agent status until online (timeout 5 minutes / 300s)
TIMEOUT_SECONDS=300
POLL_INTERVAL=10
ELAPSED=0
AGENT_STATUS="offline"

echo "[session] Waiting for agent '$AGENT_NAME' in pool '$POOL_NAME' to come online (timeout: 5m)..." >&2

while [ $ELAPSED -lt $TIMEOUT_SECONDS ]; do
    AGENTS_JSON=$(curl -s -S -f \
        -H "Authorization: Basic $B64_PAT" \
        -H "Accept: application/json;api-version=7.0" \
        "$ORG_URL/_apis/distributedtask/pools/$POOL_ID/agents?api-version=7.0" 2>/dev/null || echo "{}")

    AGENT_STATUS=$(python3 -c '
import sys, json
target = sys.argv[1]
try:
    doc = json.load(sys.stdin)
    for a in doc.get("value", []):
        if a.get("name") == target:
            print(a.get("status", "unknown").lower())
            sys.exit(0)
    print("not_found")
except Exception:
    print("error")
' "$AGENT_NAME" <<< "$AGENTS_JSON")

    if [ "$AGENT_STATUS" = "online" ]; then
        echo "[session] Agent '$AGENT_NAME' is ONLINE (ready after ${ELAPSED}s)." >&2
        break
    fi

    sleep $POLL_INTERVAL
    ELAPSED=$((ELAPSED + POLL_INTERVAL))

    if [ $((ELAPSED % 30)) -eq 0 ]; then
        echo "[session] Agent '$AGENT_NAME' status: $AGENT_STATUS (${ELAPSED}s elapsed / timeout: ${TIMEOUT_SECONDS}s)..." >&2
    fi
done

if [ "$AGENT_STATUS" != "online" ]; then
    echo "Error: Agent '$AGENT_NAME' in pool '$POOL_NAME' did not come online within 5 minutes (last status: $AGENT_STATUS)." >&2
    exit 1
fi

# 5. Execute requested command and capture exit code
echo "[session] Executing command: $*" >&2

set +e
"$@"
CMD_RC=$?
set -e

echo "[session] Command finished with exit code $CMD_RC." >&2
exit $CMD_RC
