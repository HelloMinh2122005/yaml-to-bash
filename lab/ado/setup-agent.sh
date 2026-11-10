#!/usr/bin/env bash
# lab/ado/setup-agent.sh: Ensures ADO agent pool exists and provisions the self-hosted agent on win01.
# Intended to run inside lab/run-lab.sh where az CLI is logged in.

set -euo pipefail

# Ensure set +x so secrets are never echoed to stdout/stderr
set +x

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAB_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# 1. Parse optional command line flags
OUTPUT_FILE=""
while [ $# -gt 0 ]; do
    case "$1" in
        -o|--output)
            if [ $# -lt 2 ]; then
                echo "Usage: $0 [-o OUTPUT_FILE]" >&2
                exit 64
            fi
            OUTPUT_FILE="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [-o OUTPUT_FILE]" >&2
            echo "Ensures ADO agent pool exists and installs agent on win01." >&2
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            echo "Usage: $0 [-o OUTPUT_FILE]" >&2
            exit 64
            ;;
    esac
done

if [ -z "$OUTPUT_FILE" ] && [ -n "${EVIDENCE_DIR:-}" ]; then
    OUTPUT_FILE="$EVIDENCE_DIR/versions.json"
fi

# 2. Read ADO credentials from ~/.config/y2-lab/ado.env or $ADO_ENV_FILE
CONFIG_DIR="${Y2_LAB_CONFIG_DIR:-$HOME/.config/y2-lab}"
ADO_ENV_FILE="${ADO_ENV_FILE:-$CONFIG_DIR/ado.env}"

ADO_ORG_URL="${ADO_ORG_URL:-}"
ADO_PROJECT="${ADO_PROJECT:-}"
ADO_PAT="${ADO_PAT:-}"

if [ -f "$ADO_ENV_FILE" ]; then
    while IFS='=' read -r key val || [ -n "$key" ]; do
        key=$(printf '%s\n' "$key" | tr -d ' \t\r\n')
        # Skip empty lines or comments
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

# 3. Load lab environment variables
LAB_ENV_FILE="${Y2_LAB_ENV:-$LAB_DIR/lab.env}"
if [ -f "$LAB_ENV_FILE" ]; then
    # shellcheck source=/dev/null
    source "$LAB_ENV_FILE"
fi

RESOURCE_GROUP="${LAB_RESOURCE_GROUP:-rg-lab-10vms}"
VM_NAME="${ADO_AGENT_VM:-win01}"
POOL_NAME="${ADO_POOL_NAME:-Production Windows Agent}"
AGENT_NAME="${ADO_AGENT_NAME:-$VM_NAME}"
INSTALL_PS1="$SCRIPT_DIR/install-agent.ps1"

if [ ! -f "$INSTALL_PS1" ]; then
    echo "Error: install-agent.ps1 not found at $INSTALL_PS1" >&2
    exit 1
fi

# 4. Ensure agent pool exists via ADO REST API
ORG_URL="${ADO_ORG_URL%/}"
B64_PAT="$(printf ':%s' "$ADO_PAT" | base64 | tr -d '\r\n')"

echo "[setup-agent] Checking agent pool '$POOL_NAME' via ADO REST API..." >&2

POOLS_RESP=""
if ! POOLS_RESP=$(curl -s -S -f \
    -H "Authorization: Basic $B64_PAT" \
    -H "Accept: application/json;api-version=7.0" \
    "$ORG_URL/_apis/distributedtask/pools?api-version=7.0" 2>&1); then
    echo "Error: Failed to query ADO agent pools from $ORG_URL" >&2
    exit 1
fi

POOL_ID=$(python3 -c '
import sys, json
target = sys.argv[1]
try:
    data = json.load(sys.stdin)
    for p in data.get("value", []):
        if p.get("name") == target:
            print(p.get("id", ""))
            sys.exit(0)
    sys.exit(1)
except Exception:
    sys.exit(2)
' "$POOL_NAME" <<< "$POOLS_RESP" || true)

if [ -n "$POOL_ID" ]; then
    echo "[setup-agent] Agent pool '$POOL_NAME' already exists (pool ID: $POOL_ID)." >&2
else
    echo "[setup-agent] Agent pool '$POOL_NAME' not found. Creating pool..." >&2
    CREATE_PAYLOAD=$(python3 -c '
import sys, json
print(json.dumps({"name": sys.argv[1], "autoProvision": True}))
' "$POOL_NAME")

    CREATE_RESP=""
    if ! CREATE_RESP=$(curl -s -S -f -X POST \
        -H "Authorization: Basic $B64_PAT" \
        -H "Content-Type: application/json" \
        -d "$CREATE_PAYLOAD" \
        "$ORG_URL/_apis/distributedtask/pools?api-version=7.0" 2>&1); then
        echo "Error: Failed to create agent pool '$POOL_NAME' in ADO" >&2
        exit 1
    fi

    POOL_ID=$(python3 -c '
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get("id", ""))
except Exception:
    sys.exit(1)
' <<< "$CREATE_RESP")

    if [ -z "$POOL_ID" ]; then
        echo "Error: Pool creation did not return a valid pool ID" >&2
        exit 1
    fi
    echo "[setup-agent] Created agent pool '$POOL_NAME' (pool ID: $POOL_ID)." >&2
fi

# Ensure project queue exists if ADO_PROJECT is set
if [ -n "$ADO_PROJECT" ] && [ -n "$POOL_ID" ]; then
    PROJECT_ENCODED=$(python3 -c 'import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1]))' "$ADO_PROJECT")
    QUEUES_RESP=$(curl -s -S -f \
        -H "Authorization: Basic $B64_PAT" \
        -H "Accept: application/json;api-version=7.0" \
        "$ORG_URL/$PROJECT_ENCODED/_apis/distributedtask/queues?api-version=7.0" 2>/dev/null || echo "{}")

    HAS_QUEUE=$(python3 -c '
import sys, json
target = sys.argv[1]
try:
    data = json.load(sys.stdin)
    for q in data.get("value", []):
        if q.get("name") == target:
            print("yes")
            sys.exit(0)
    print("no")
except Exception:
    print("no")
' "$POOL_NAME" <<< "$QUEUES_RESP")

    if [ "$HAS_QUEUE" = "no" ]; then
        echo "[setup-agent] Assigning pool '$POOL_NAME' to project '$ADO_PROJECT'..." >&2
        QUEUE_PAYLOAD=$(python3 -c '
import sys, json
print(json.dumps({"name": sys.argv[1], "pool": {"id": int(sys.argv[2])}}))
' "$POOL_NAME" "$POOL_ID")
        curl -s -S -f -X POST \
            -H "Authorization: Basic $B64_PAT" \
            -H "Content-Type: application/json" \
            -d "$QUEUE_PAYLOAD" \
            "$ORG_URL/$PROJECT_ENCODED/_apis/distributedtask/queues?api-version=7.0" >/dev/null 2>&1 || true
    fi
fi

# 5. Resolve official agent download URL for win-x64
DEFAULT_PACKAGE_URL="https://download.agent.dev.azure.com/agent/5.281.1/vsts-agent-win-x64-5.281.1.zip"
PACKAGE_URL=""

echo "[setup-agent] Querying official agent package URL for win-x64..." >&2
PACKAGES_RESP=$(curl -s -S -f \
    -H "Authorization: Basic $B64_PAT" \
    -H "Accept: application/json;api-version=7.0" \
    "$ORG_URL/_apis/distributedtask/packages/agent?platform=win-x64&\$top=1" 2>/dev/null || echo "{}")

PACKAGE_URL=$(python3 -c '
import sys, json
try:
    doc = json.load(sys.stdin)
    val = doc.get("value", [])
    if val:
        url = val[0].get("downloadUrl", "")
        if url:
            print(url)
except Exception:
    pass
' <<< "$PACKAGES_RESP")

if [ -n "$PACKAGE_URL" ]; then
    echo "[setup-agent] Resolved agent package URL: $PACKAGE_URL" >&2
else
    echo "[setup-agent] Could not resolve agent package URL from ADO API. Using default: $DEFAULT_PACKAGE_URL" >&2
    PACKAGE_URL="$DEFAULT_PACKAGE_URL"
fi

# 6. Invoke install-agent.ps1 on VM win01 via az vm run-command invoke
echo "[setup-agent] Invoking install-agent.ps1 on $VM_NAME in $RESOURCE_GROUP..." >&2

# Explicitly ensure set +x so the PAT parameter is NEVER echoed or logged
set +x

CMD=(
    az vm run-command invoke
    -g "$RESOURCE_GROUP"
    -n "$VM_NAME"
    --command-id RunPowerShellScript
    --scripts @"$INSTALL_PS1"
    --parameters
    "OrgUrl=$ADO_ORG_URL"
    "Pat=$ADO_PAT"
    "PoolName=$POOL_NAME"
    "AgentName=$AGENT_NAME"
    "PackageUrl=$PACKAGE_URL"
    -o json
)

if [ -n "${LAB_SUBSCRIPTION_ID:-}" ]; then
    CMD+=(--subscription "$LAB_SUBSCRIPTION_ID")
fi

set +e
RAW_OUTPUT=$("${CMD[@]}" 2>&1)
AZ_RC=$?
set -e

if [ $AZ_RC -ne 0 ]; then
    # Redact PAT from error output if present
    CLEAN_ERR=$(python3 -c '
import sys
err = sys.stdin.read()
pat = sys.argv[1]
if pat:
    err = err.replace(pat, "********")
sys.stdout.write(err)
' "$ADO_PAT" <<< "$RAW_OUTPUT")
    echo "$CLEAN_ERR" >&2
    echo "Error: az vm run-command invoke failed on $VM_NAME (exit code $AZ_RC)" >&2
    exit $AZ_RC
fi

# 7. Extract versions JSON from run-command output
PARSED_JSON=$(python3 -c '
import sys, json

def extract(text):
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start:end+1])
        return None

raw = sys.stdin.read()
pat = sys.argv[1]
if pat:
    raw = raw.replace(pat, "********")

try:
    doc = json.loads(raw)
    messages = [x.get("message", "") for x in doc.get("value", []) if "message" in x]
    full_msg = "\n".join(messages)
    parsed = extract(full_msg)
    if parsed is not None:
        print(json.dumps(parsed, indent=2))
    else:
        sys.stderr.write("Warning: could not parse versions JSON from output message:\n" + full_msg + "\n")
        print(full_msg)
except Exception as e:
    sys.stderr.write(f"Error processing run-command JSON: {e}\n")
    sys.exit(1)
' "$ADO_PAT" <<< "$RAW_OUTPUT")

# Output the parsed JSON to stdout
printf '%s\n' "$PARSED_JSON"

# Save to output file if requested
if [ -n "$OUTPUT_FILE" ]; then
    mkdir -p "$(dirname "$OUTPUT_FILE")"
    printf '%s\n' "$PARSED_JSON" > "$OUTPUT_FILE"
    echo "[setup-agent] Saved versions JSON to $OUTPUT_FILE" >&2
fi

echo "[setup-agent] Agent setup complete on $VM_NAME." >&2
