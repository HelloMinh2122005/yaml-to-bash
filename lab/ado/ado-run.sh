#!/usr/bin/env bash
# lab/ado/ado-run.sh: ADO pipeline runner that registers definitions, queues runs,
# polls status, downloads logs and artifacts to an evidence directory, and exits 0 on success.
# Usage: lab/ado/ado-run.sh <yaml-path> [--evidence-dir <dir>] [--timeout-minutes <mins>] [key=value...]

set -euo pipefail

# Ensure set +x so secrets are never echoed to stdout/stderr
set +x

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAB_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$LAB_DIR/.." && pwd)"

usage() {
    cat <<EOF >&2
Usage: $0 <yaml-path> [--evidence-dir <dir>] [--timeout-minutes <mins>] [key=value...]

Arguments:
  <yaml-path>              Path to pipeline YAML file (required)
  --evidence-dir <dir>     Directory to store downloaded logs and artifacts
  --timeout-minutes <mins> Maximum minutes to wait for pipeline completion (default: 60)
  key=value...             Template parameters passed to the pipeline run
EOF
    exit "${1:-64}"
}

# 1. Parse command line arguments
YAML_PATH=""
EVIDENCE_DIR=""
TIMEOUT_MINUTES=60
PARAMS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --evidence-dir)
            if [ $# -lt 2 ]; then usage; fi
            EVIDENCE_DIR="$2"
            shift 2
            ;;
        --timeout-minutes)
            if [ $# -lt 2 ]; then usage; fi
            TIMEOUT_MINUTES="$2"
            shift 2
            ;;
        -h|--help)
            usage 0
            ;;
        *=*)
            PARAMS+=("$1")
            shift
            ;;
        *)
            if [ -z "$YAML_PATH" ]; then
                YAML_PATH="$1"
                shift
            else
                echo "Unexpected argument: $1" >&2
                usage
            fi
            ;;
    esac
done

if [ -z "$YAML_PATH" ]; then
    echo "Error: <yaml-path> argument is required." >&2
    usage
fi

if [ ! -f "$YAML_PATH" ]; then
    echo "Error: YAML file '$YAML_PATH' not found." >&2
    exit 1
fi

# Normalize YAML path relative to REPO_ROOT
REL_YAML_PATH=$(python3 -c '
import os, sys
yaml_abs = os.path.abspath(sys.argv[1])
repo_abs = os.path.abspath(sys.argv[2])
try:
    print(os.path.relpath(yaml_abs, repo_abs))
except Exception:
    print(yaml_abs)
' "$YAML_PATH" "$REPO_ROOT")

PIPELINE_NAME=$(basename "$YAML_PATH" | sed -e 's/\.ya\?ml$//')

# Set evidence directory
if [ -z "$EVIDENCE_DIR" ]; then
    TIMESTAMP="$(date -u +%Y%m%d_%H%M%SZ)"
    EVIDENCE_DIR="$LAB_DIR/lab-evidence/ado-run-$TIMESTAMP"
fi
mkdir -p "$EVIDENCE_DIR"
EVIDENCE_DIR="$(cd "$EVIDENCE_DIR" && pwd)"

# 2. Read ADO credentials from ~/.config/y2-lab/ado.env or $ADO_ENV_FILE
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

if [ -z "$ADO_PROJECT" ]; then
    echo "Error: ADO_PROJECT is missing (not set in environment or in $ADO_ENV_FILE)" >&2
    exit 1
fi

if [ -z "$ADO_PAT" ]; then
    echo "Error: ADO_PAT is missing (not set in environment or in $ADO_ENV_FILE)" >&2
    exit 1
fi

# Load lab identity if available
LAB_ENV_FILE="${Y2_LAB_ENV:-$LAB_DIR/lab.env}"
if [ -f "$LAB_ENV_FILE" ]; then
    # shellcheck source=/dev/null
    source "$LAB_ENV_FILE"
fi

ORG_URL="${ADO_ORG_URL%/}"
PROJECT_ENCODED=$(python3 -c 'import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1]))' "$ADO_PROJECT")
B64_PAT="$(printf ':%s' "$ADO_PAT" | base64 | tr -d '\r\n')"

# Determine service connection name (allow override via parameter)
SERVICE_CONNECTION_NAME="${ADO_SERVICE_CONNECTION:-sc-azure-lab}"
for p in "${PARAMS[@]}"; do
    case "$p" in
        serviceConnection=*)
            SERVICE_CONNECTION_NAME="${p#serviceConnection=}"
            ;;
    esac
done

# 3. Check / verify service connection in ADO project via Service Endpoints API
echo "[ado-run] Checking service connection '$SERVICE_CONNECTION_NAME' in ADO project '$ADO_PROJECT'..." >&2

ENDPOINTS_JSON=$(curl -s -S -f \
    -H "Authorization: Basic $B64_PAT" \
    -H "Accept: application/json;api-version=7.0" \
    "$ORG_URL/$PROJECT_ENCODED/_apis/serviceendpoint/endpoints?api-version=7.0" 2>/dev/null || echo "{}")

SC_INFO=$(python3 -c '
import sys, json
target = sys.argv[1]
try:
    data = json.load(sys.stdin)
    endpoints = data.get("value", [])
    for ep in endpoints:
        if ep.get("name") == target:
            ep_id = ep.get("id", "")
            is_ready = ep.get("isReady", True)
            ep_type = ep.get("type", "")
            print(f"{ep_id}|{is_ready}|{ep_type}")
            sys.exit(0)
    sys.exit(1)
except Exception:
    sys.exit(2)
' "$SERVICE_CONNECTION_NAME" <<< "$ENDPOINTS_JSON" || true)

if [ -n "$SC_INFO" ]; then
    SC_ID="${SC_INFO%%|*}"
    rest="${SC_INFO#*|}"
    SC_READY="${rest%%|*}"
    SC_TYPE="${rest#*|}"
    echo "[ado-run] Service connection '$SERVICE_CONNECTION_NAME' found (ID: $SC_ID, type: $SC_TYPE, ready: $SC_READY)." >&2

    # Authorize endpoint for all pipelines
    curl -s -S -f -X PATCH \
        -H "Authorization: Basic $B64_PAT" \
        -H "Content-Type: application/json" \
        -d '{"allPipelines": {"authorized": true}}' \
        "$ORG_URL/$PROJECT_ENCODED/_apis/pipelines/pipelinePermissions/endpoint/$SC_ID?api-version=7.0-preview.1" >/dev/null 2>&1 || true
else
    echo "[ado-run] Service connection '$SERVICE_CONNECTION_NAME' not found in project '$ADO_PROJECT'." >&2
    # Attempt creation if SP credentials available in sp.env
    SP_ENV="$CONFIG_DIR/sp.env"
    SP_CLIENT_ID=""
    SP_TENANT_ID=""
    SP_CLIENT_SECRET=""

    if [ -f "$SP_ENV" ]; then
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
                AZURE_CLIENT_ID) SP_CLIENT_ID="$val" ;;
                AZURE_TENANT_ID) SP_TENANT_ID="$val" ;;
                AZURE_CLIENT_SECRET) SP_CLIENT_SECRET="$val" ;;
            esac
        done < "$SP_ENV"
    fi

    LAB_SUB_ID="${LAB_SUBSCRIPTION_ID:-}"
    LAB_SUB_NAME="${LAB_SUBSCRIPTION_NAME:-Azure Lab Subscription}"

    if [ -n "$SP_CLIENT_ID" ] && [ "$SP_CLIENT_ID" != "<appId>" ] && [ -n "$SP_CLIENT_SECRET" ] && [ -n "$LAB_SUB_ID" ]; then
        echo "[ado-run] Creating ARM service connection '$SERVICE_CONNECTION_NAME' for subscription $LAB_SUB_ID..." >&2
        SC_PAYLOAD=$(python3 -c '
import sys, json
name, sub_id, sub_name, tenant_id, client_id, client_secret = sys.argv[1:7]
payload = {
    "name": name,
    "type": "AzureRM",
    "url": "https://management.azure.com/",
    "authorization": {
        "scheme": "ServicePrincipal",
        "parameters": {
            "tenantid": tenant_id,
            "serviceprincipalid": client_id,
            "serviceprincipalkey": client_secret
        }
    },
    "data": {
        "subscriptionId": sub_id,
        "subscriptionName": sub_name,
        "environment": "AzureCloud",
        "scopeLevel": "Subscription",
        "creationMode": "Manual"
    },
    "isReady": True
}
print(json.dumps(payload))
' "$SERVICE_CONNECTION_NAME" "$LAB_SUB_ID" "$LAB_SUB_NAME" "$SP_TENANT_ID" "$SP_CLIENT_ID" "$SP_CLIENT_SECRET")

        CREATE_SC_RESP=""
        if ! CREATE_SC_RESP=$(curl -s -S -f -X POST \
            -H "Authorization: Basic $B64_PAT" \
            -H "Content-Type: application/json" \
            -d "$SC_PAYLOAD" \
            "$ORG_URL/$PROJECT_ENCODED/_apis/serviceendpoint/endpoints?api-version=7.0" 2>&1); then
            echo "Error: Failed to create service connection '$SERVICE_CONNECTION_NAME' in ADO" >&2
            exit 1
        fi

        SC_ID=$(python3 -c '
import sys, json
try:
    print(json.load(sys.stdin).get("id", ""))
except Exception:
    sys.exit(1)
' <<< "$CREATE_SC_RESP")
        echo "[ado-run] Created service connection '$SERVICE_CONNECTION_NAME' (ID: $SC_ID)." >&2

        # Authorize endpoint for all pipelines
        curl -s -S -f -X PATCH \
            -H "Authorization: Basic $B64_PAT" \
            -H "Content-Type: application/json" \
            -d '{"allPipelines": {"authorized": true}}' \
            "$ORG_URL/$PROJECT_ENCODED/_apis/pipelines/pipelinePermissions/endpoint/$SC_ID?api-version=7.0-preview.1" >/dev/null 2>&1 || true
    else
        echo "Error: Service connection '$SERVICE_CONNECTION_NAME' does not exist in ADO project '$ADO_PROJECT', and credentials to create it are not available." >&2
        exit 1
    fi
fi

# 4. Locate or register pipeline definition in ADO project
echo "[ado-run] Locating or registering pipeline for '$REL_YAML_PATH'..." >&2

PIPELINES_JSON=$(curl -s -S -f \
    -H "Authorization: Basic $B64_PAT" \
    -H "Accept: application/json;api-version=7.0" \
    "$ORG_URL/$PROJECT_ENCODED/_apis/pipelines?api-version=7.0" 2>/dev/null || echo "{}")

DEFINITIONS_JSON=$(curl -s -S -f \
    -H "Authorization: Basic $B64_PAT" \
    -H "Accept: application/json;api-version=7.0" \
    "$ORG_URL/$PROJECT_ENCODED/_apis/build/definitions?api-version=7.0" 2>/dev/null || echo "{}")

PIPELINE_ID=$(python3 -c '
import sys, json

target_name = sys.argv[1].lower()
target_path = sys.argv[2].replace("\\", "/").lower()

def match_pipeline(pipes_raw, defs_raw):
    pipes = []
    defs = []
    try:
        pipes = json.loads(pipes_raw).get("value", [])
    except Exception:
        pass
    try:
        defs = json.loads(defs_raw).get("value", [])
    except Exception:
        pass

    # Pass 1: Prioritize matching by exact YAML path
    for p in pipes:
        cfg_path = p.get("configuration", {}).get("path", "").replace("\\", "/").lower()
        if cfg_path and (cfg_path == target_path or cfg_path.endswith("/" + target_path)):
            return p.get("id")

    for d in defs:
        yaml_path = d.get("process", {}).get("yamlFilename", "").replace("\\", "/").lower()
        if yaml_path and (yaml_path == target_path or yaml_path.endswith("/" + target_path)):
            return d.get("id")

    # Pass 2: Fall back to matching by pipeline name
    for p in pipes:
        if p.get("name", "").lower() == target_name:
            return p.get("id")

    for d in defs:
        if d.get("name", "").lower() == target_name:
            return d.get("id")

    return None

pid = match_pipeline(sys.argv[3], sys.argv[4])
if pid:
    print(pid)
    sys.exit(0)
sys.exit(1)
' "$PIPELINE_NAME" "$REL_YAML_PATH" "$PIPELINES_JSON" "$DEFINITIONS_JSON" || true)

if [ -n "$PIPELINE_ID" ]; then
    echo "[ado-run] Found pipeline '$PIPELINE_NAME' (ID: $PIPELINE_ID)." >&2
else
    echo "[ado-run] Pipeline '$PIPELINE_NAME' not found. Registering new pipeline..." >&2

    GIT_REPOS_JSON=$(curl -s -S -f \
        -H "Authorization: Basic $B64_PAT" \
        -H "Accept: application/json;api-version=7.0" \
        "$ORG_URL/$PROJECT_ENCODED/_apis/git/repositories?api-version=7.0" 2>/dev/null || echo "{}")

    CREATE_PIPE_PAYLOAD=$(python3 -c '
import sys, json

pipe_name = sys.argv[1]
yaml_path = sys.argv[2].replace("\\", "/")
defs_raw = sys.argv[3]
repos_raw = sys.argv[4]
project = sys.argv[5]

repo = None
# 1. Borrow repository configuration from existing build definitions
try:
    d_data = json.loads(defs_raw)
    for d in d_data.get("value", []):
        r = d.get("repository")
        if r and r.get("id"):
            repo = {"id": r.get("id"), "name": r.get("name"), "type": r.get("type", "azureReposGit")}
            break
except Exception:
    pass

# 2. Check git repositories in the project
if not repo:
    try:
        r_data = json.loads(repos_raw)
        repos = r_data.get("value", [])
        for r in repos:
            if r.get("name", "").lower() == project.lower():
                repo = {"id": r.get("id"), "name": r.get("name"), "type": "azureReposGit"}
                break
        if not repo and repos:
            repo = {"id": repos[0].get("id"), "name": repos[0].get("name"), "type": "azureReposGit"}
    except Exception:
        pass

if not repo:
    sys.stderr.write("Could not discover repository info to register pipeline\n")
    sys.exit(1)

payload = {
    "name": pipe_name,
    "configuration": {
        "type": "yaml",
        "path": yaml_path,
        "repository": repo
    }
}
print(json.dumps(payload))
' "$PIPELINE_NAME" "$REL_YAML_PATH" "$DEFINITIONS_JSON" "$GIT_REPOS_JSON" "$ADO_PROJECT")

    NEW_PIPE_RESP=""
    if ! NEW_PIPE_RESP=$(curl -s -S -f -X POST \
        -H "Authorization: Basic $B64_PAT" \
        -H "Content-Type: application/json" \
        -d "$CREATE_PIPE_PAYLOAD" \
        "$ORG_URL/$PROJECT_ENCODED/_apis/pipelines?api-version=7.0" 2>&1); then
        echo "Error: Failed to register pipeline '$PIPELINE_NAME' in ADO" >&2
        exit 1
    fi

    PIPELINE_ID=$(python3 -c '
import sys, json
try:
    print(json.load(sys.stdin).get("id", ""))
except Exception:
    sys.exit(1)
' <<< "$NEW_PIPE_RESP")

    if [ -z "$PIPELINE_ID" ]; then
        echo "Error: Registered pipeline did not return a valid pipeline ID" >&2
        exit 1
    fi
    echo "[ado-run] Registered pipeline '$PIPELINE_NAME' (ID: $PIPELINE_ID)." >&2
fi

# 5. Queue pipeline run with parameters
echo "[ado-run] Queueing run for pipeline '$PIPELINE_NAME' (ID: $PIPELINE_ID)..." >&2

QUEUE_PAYLOAD=$(python3 -c '
import sys, json

params = {}
for arg in sys.argv[1:]:
    if "=" in arg:
        k, v = arg.split("=", 1)
        params[k.strip()] = v.strip()

body = {
    "templateParameters": params
}
print(json.dumps(body))
' "${PARAMS[@]}")

RUN_RESP=""
if ! RUN_RESP=$(curl -s -S -f -X POST \
    -H "Authorization: Basic $B64_PAT" \
    -H "Content-Type: application/json" \
    -d "$QUEUE_PAYLOAD" \
    "$ORG_URL/$PROJECT_ENCODED/_apis/pipelines/$PIPELINE_ID/runs?api-version=7.0" 2>&1); then

    # Fallback to Builds API
    BUILD_QUEUE_PAYLOAD=$(python3 -c '
import sys, json
pid = int(sys.argv[1])
params = {}
for arg in sys.argv[2:]:
    if "=" in arg:
        k, v = arg.split("=", 1)
        params[k.strip()] = v.strip()

body = {
    "definition": {"id": pid},
    "templateParameters": params
}
print(json.dumps(body))
' "$PIPELINE_ID" "${PARAMS[@]}")

    if ! RUN_RESP=$(curl -s -S -f -X POST \
        -H "Authorization: Basic $B64_PAT" \
        -H "Content-Type: application/json" \
        -d "$BUILD_QUEUE_PAYLOAD" \
        "$ORG_URL/$PROJECT_ENCODED/_apis/build/builds?api-version=7.0" 2>&1); then
        echo "Error: Failed to queue run for pipeline $PIPELINE_ID" >&2
        exit 1
    fi
fi

RUN_ID=$(python3 -c '
import sys, json
try:
    print(json.load(sys.stdin).get("id", ""))
except Exception:
    sys.exit(1)
' <<< "$RUN_RESP")

if [ -z "$RUN_ID" ]; then
    echo "Error: Queueing run did not return a valid run ID" >&2
    exit 1
fi

echo "[ado-run] Successfully queued run ID: $RUN_ID" >&2

# 6. Poll build status until completion
TIMEOUT_SECONDS=$((TIMEOUT_MINUTES * 60))
POLL_INTERVAL=10
ELAPSED=0
STATUS="inProgress"
RESULT=""

echo "[ado-run] Waiting for run $RUN_ID to complete (timeout: ${TIMEOUT_MINUTES}m)..." >&2

while [ $ELAPSED -lt $TIMEOUT_SECONDS ]; do
    BUILD_RESP=$(curl -s -S -f \
        -H "Authorization: Basic $B64_PAT" \
        -H "Accept: application/json;api-version=7.0" \
        "$ORG_URL/$PROJECT_ENCODED/_apis/build/builds/$RUN_ID?api-version=7.0" 2>/dev/null || echo "{}")

    STATUS_INFO=$(python3 -c '
import sys, json
try:
    doc = json.load(sys.stdin)
    status = doc.get("status", "unknown")
    result = doc.get("result", "")
    print(f"{status}|{result}")
except Exception:
    print("unknown|")
' <<< "$BUILD_RESP")

    STATUS="${STATUS_INFO%%|*}"
    RESULT="${STATUS_INFO#*|}"

    if [ "$STATUS" = "completed" ]; then
        echo "[ado-run] Run $RUN_ID completed with result: $RESULT." >&2
        break
    fi

    sleep $POLL_INTERVAL
    ELAPSED=$((ELAPSED + POLL_INTERVAL))

    if [ $((ELAPSED % 30)) -eq 0 ]; then
        echo "[ado-run] Run $RUN_ID in progress... (${ELAPSED}s elapsed / timeout: ${TIMEOUT_SECONDS}s)" >&2
    fi
done

if [ "$STATUS" != "completed" ]; then
    echo "Error: Pipeline run $RUN_ID timed out after ${TIMEOUT_MINUTES} minutes." >&2
    curl -s -X PATCH \
        -H "Authorization: Basic $B64_PAT" \
        -H "Content-Type: application/json" \
        -d '{"status": "cancelling"}' \
        "$ORG_URL/$PROJECT_ENCODED/_apis/build/builds/$RUN_ID?api-version=7.0" >/dev/null 2>&1 || true
fi

# 7. Download run logs and published artifacts into evidence directory
echo "[ado-run] Downloading run logs and artifacts into $EVIDENCE_DIR..." >&2
mkdir -p "$EVIDENCE_DIR/logs"
mkdir -p "$EVIDENCE_DIR/artifacts"
PIPELINE_LOG="$EVIDENCE_DIR/pipeline.log"
: > "$PIPELINE_LOG"

# Download logs
LOGS_LIST_JSON=$(curl -s -S -f \
    -H "Authorization: Basic $B64_PAT" \
    -H "Accept: application/json;api-version=7.0" \
    "$ORG_URL/$PROJECT_ENCODED/_apis/build/builds/$RUN_ID/logs?api-version=7.0" 2>/dev/null || echo "{}")

LOG_URLS=$(python3 -c '
import sys, json
try:
    doc = json.load(sys.stdin)
    for item in doc.get("value", []):
        lid = item.get("id")
        url = item.get("url")
        if lid is not None and url:
            print(f"{lid} {url}")
except Exception:
    pass
' <<< "$LOGS_LIST_JSON")

if [ -n "$LOG_URLS" ]; then
    while read -r log_id log_url; do
        [ -z "$log_id" ] && continue
        LOG_FILE="$EVIDENCE_DIR/logs/${log_id}.log"
        curl -s -S -L -f \
            -H "Authorization: Basic $B64_PAT" \
            -H "Accept: text/plain" \
            "$log_url" > "$LOG_FILE" 2>/dev/null || true
        if [ -s "$LOG_FILE" ]; then
            cat "$LOG_FILE" >> "$PIPELINE_LOG"
            printf '\n' >> "$PIPELINE_LOG"
        fi
    done <<< "$LOG_URLS"
    echo "[ado-run] Saved run logs to $EVIDENCE_DIR/logs and combined into $PIPELINE_LOG." >&2
else
    echo "[ado-run] No logs found for run $RUN_ID." >&2
fi

# Download artifacts
ARTIFACTS_LIST_JSON=$(curl -s -S -f \
    -H "Authorization: Basic $B64_PAT" \
    -H "Accept: application/json;api-version=7.0" \
    "$ORG_URL/$PROJECT_ENCODED/_apis/build/builds/$RUN_ID/artifacts?api-version=7.0" 2>/dev/null || echo "{}")

ARTIFACT_URLS=$(python3 -c '
import sys, json
try:
    doc = json.load(sys.stdin)
    for item in doc.get("value", []):
        name = item.get("name", "artifact")
        url = item.get("resource", {}).get("downloadUrl", "")
        if url:
            print(f"{name} {url}")
except Exception:
    pass
' <<< "$ARTIFACTS_LIST_JSON")

if [ -n "$ARTIFACT_URLS" ]; then
    while read -r art_name art_url; do
        [ -z "$art_name" ] && continue
        ART_ZIP="$EVIDENCE_DIR/artifacts/${art_name}.zip"
        ART_DIR="$EVIDENCE_DIR/artifacts/${art_name}"
        curl -s -S -L -f \
            -H "Authorization: Basic $B64_PAT" \
            "$art_url" -o "$ART_ZIP" 2>/dev/null || true
        if [ -s "$ART_ZIP" ]; then
            mkdir -p "$ART_DIR"
            python3 -m zipfile -e "$ART_ZIP" "$ART_DIR" 2>/dev/null || true
            echo "[ado-run] Downloaded and extracted artifact '$art_name'." >&2
        fi
    done <<< "$ARTIFACT_URLS"
fi

# Write metadata summary
python3 -c '
import sys, json
summary = {
    "pipelineId": int(sys.argv[1]),
    "pipelineName": sys.argv[2],
    "runId": int(sys.argv[3]),
    "status": sys.argv[4],
    "result": sys.argv[5],
    "yamlPath": sys.argv[6],
    "evidenceDir": sys.argv[7]
}
with open(f"{sys.argv[7]}/summary.json", "w") as f:
    json.dump(summary, f, indent=2)
' "$PIPELINE_ID" "$PIPELINE_NAME" "$RUN_ID" "$STATUS" "$RESULT" "$REL_YAML_PATH" "$EVIDENCE_DIR"

echo "[ado-run] Execution completed. Summary written to $EVIDENCE_DIR/summary.json." >&2

# 8. Final exit code check
if [ "$STATUS" = "completed" ] && [ "$RESULT" = "succeeded" ]; then
    echo "[ado-run] Pipeline run $RUN_ID succeeded." >&2
    exit 0
else
    echo "Error: Pipeline run $RUN_ID finished with status '$STATUS' and result '$RESULT'." >&2
    exit 1
fi
