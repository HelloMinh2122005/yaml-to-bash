#!/usr/bin/env bash
# lab/run-lab.sh: Lab runner for y2 squad with SP login, lab-only CSV validation,
# budget enforcement, and fail-safe VM deallocation.

set -eu

ORIG_PATH="$PATH"
LAB_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$LAB_DIR/.." && pwd)"

Y2_LAB_CONFIG_DIR="${Y2_LAB_CONFIG_DIR:-$HOME/.config/y2-lab}"
Y2_LAB_BUDGET_SECONDS="${Y2_LAB_BUDGET_SECONDS:-3600}"
Y2_LAB_ENV="${Y2_LAB_ENV:-$LAB_DIR/lab.env}"

usage() {
    echo "Usage: $0 [--csv FILE]... [--evidence DIR] -- <command> [args...]" >&2
    exit 64
}

CSV_FILES=()
EVIDENCE_DIR=""

while [ $# -gt 0 ]; do
    case "$1" in
        --)
            shift
            break
            ;;
        --csv)
            if [ $# -lt 2 ]; then usage; fi
            CSV_FILES+=("$2")
            shift 2
            ;;
        --evidence)
            if [ $# -lt 2 ]; then usage; fi
            EVIDENCE_DIR="$2"
            shift 2
            ;;
        *)
            usage
            ;;
    esac
done

if [ $# -eq 0 ]; then
    usage
fi

if [ -z "$EVIDENCE_DIR" ]; then
    TIMESTAMP="$(date -u +%Y%m%d_%H%M%SZ)"
    EVIDENCE_DIR="$LAB_DIR/lab-evidence/$TIMESTAMP"
fi

# 1. Acquire lock
LOCK_DIR="$Y2_LAB_CONFIG_DIR/run.lock"
mkdir -p "$Y2_LAB_CONFIG_DIR"

if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo $$ > "$LOCK_DIR/pid"
else
    # Lock exists, check if process is alive
    if [ -f "$LOCK_DIR/pid" ]; then
        LOCK_PID="$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")"
        if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
            echo "Lab busy: locked by active PID $LOCK_PID" >&2
            exit 75
        fi
    fi
    # Reclaim stale lock
    rm -rf "$LOCK_DIR"
    if mkdir "$LOCK_DIR" 2>/dev/null; then
        echo $$ > "$LOCK_DIR/pid"
    else
        echo "Lab busy: failed to acquire lock" >&2
        exit 75
    fi
fi

early_cleanup() {
    rm -rf "$LOCK_DIR"
}
trap early_cleanup EXIT INT TERM

# 2. Check service principal config (fallback to active az login if not present)
SP_ENV="$Y2_LAB_CONFIG_DIR/sp.env"

# Resolve real az CLI before putting shim on PATH
if [ -n "${Y2_REAL_AZ:-}" ]; then
    REAL_AZ="$Y2_REAL_AZ"
else
    REAL_AZ="$(command -v az 2>/dev/null || true)"
fi

if [ -z "$REAL_AZ" ] || [ ! -x "$REAL_AZ" ]; then
    echo "Config error: az CLI not found or not executable" >&2
    exit 78
fi
export Y2_REAL_AZ="$REAL_AZ"

# Check timeout binary
TIMEOUT_BIN=""
if command -v timeout >/dev/null 2>&1; then
    TIMEOUT_BIN="timeout"
elif command -v gtimeout >/dev/null 2>&1; then
    TIMEOUT_BIN="gtimeout"
else
    echo "Config error: neither timeout nor gtimeout utility found" >&2
    exit 78
fi

mkdir -p "$EVIDENCE_DIR"
RUN_LOG="$EVIDENCE_DIR/run.log"
touch "$RUN_LOG"

if [ -f "$SP_ENV" ]; then
    AZURE_CLIENT_ID=""
    AZURE_TENANT_ID=""
    AZURE_CLIENT_SECRET=""

    while IFS='=' read -r key val || [ -n "$key" ]; do
        key=$(printf '%s\n' "$key" | tr -d ' \t\r\n')
        val=$(printf '%s\n' "$val" | sed -e 's/^[ \t]*//' -e 's/[ \t\r\n]*$//' -e 's/^["'\x27']//' -e 's/["'\x27']$//')
        case "$key" in
            AZURE_CLIENT_ID) AZURE_CLIENT_ID="$val" ;;
            AZURE_TENANT_ID) AZURE_TENANT_ID="$val" ;;
            AZURE_CLIENT_SECRET) AZURE_CLIENT_SECRET="$val" ;;
        esac
    done < "$SP_ENV"

    if [ -n "$AZURE_CLIENT_ID" ] && [ "$AZURE_CLIENT_ID" != "<appId>" ]; then
        export AZURE_CONFIG_DIR="$Y2_LAB_CONFIG_DIR/azure"
        mkdir -p "$AZURE_CONFIG_DIR"

        echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Authenticating service principal $AZURE_CLIENT_ID" >> "$RUN_LOG"
        if ! "$Y2_REAL_AZ" login --service-principal \
            -u "$AZURE_CLIENT_ID" \
            -p "$AZURE_CLIENT_SECRET" \
            --tenant "$AZURE_TENANT_ID" \
            -o none >/dev/null 2>&1; then
            echo "Login error: az login --service-principal failed" >&2
            exit 78
        fi
    else
        echo "Using active Azure CLI login" >> "$RUN_LOG"
    fi
else
    echo "Config error: service principal file not found at $SP_ENV" >&2
    exit 78
fi

# 4. Account set
# shellcheck source=lab.env
source "$Y2_LAB_ENV"
if ! "$Y2_REAL_AZ" account set --subscription "$LAB_SUBSCRIPTION_ID" >> "$RUN_LOG" 2>&1; then
    echo "Login error: az account set to lab subscription failed" >&2
    exit 78
fi

# 5. CSV validation
if [ ${#CSV_FILES[@]} -gt 0 ]; then
    if ! python3 "$LAB_DIR/labenv.py" check-csv "${CSV_FILES[@]}" >> "$RUN_LOG" 2>&1; then
        echo "CSV validation failed: non-lab target detected" >&2
        exit 65
    fi
fi

# 6. Install fail-safe deallocate trap
CLEANED_UP=0
FINAL_EXIT=0
CHILD_PID=""

cleanup() {
    local sig="${1:-EXIT}"
    if [ "$CLEANED_UP" -eq 1 ]; then
        return
    fi
    CLEANED_UP=1
    trap - EXIT INT TERM

    if [ -n "$CHILD_PID" ] && kill -0 "$CHILD_PID" 2>/dev/null; then
        kill -TERM "$CHILD_PID" 2>/dev/null || true
        wait "$CHILD_PID" 2>/dev/null || true
    fi

    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Deallocating all lab VMs (signal: $sig)..." >> "$RUN_LOG"
    export PATH="$LAB_DIR/az-shim:$ORIG_PATH"
    export Y2_REAL_AZ="$REAL_AZ"
    export Y2_LAB_ENV

    local dealloc_rc=0
    if ! bash "$LAB_DIR/toogle_vm.sh" stop --wait >> "$RUN_LOG" 2>&1; then
        dealloc_rc=70
    fi
    bash "$LAB_DIR/toogle_vm.sh" status > "$EVIDENCE_DIR/post-state.txt" 2>> "$RUN_LOG" || true

    rm -rf "$LOCK_DIR"

    if [ "$dealloc_rc" -ne 0 ]; then
        exit 70
    fi
    if [ "$FINAL_EXIT" -ne 0 ]; then
        exit "$FINAL_EXIT"
    fi
    if [ "$sig" = "INT" ] || [ "$sig" = "TERM" ]; then
        exit 130
    fi
    exit 0
}

trap 'cleanup EXIT' EXIT
trap 'FINAL_EXIT=130; cleanup INT' INT
trap 'FINAL_EXIT=143; cleanup TERM' TERM

# 7. Record pre-state and execute command with shim on PATH
export PATH="$LAB_DIR/az-shim:$ORIG_PATH"
export Y2_LAB_ENV

echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Recording pre-state..." >> "$RUN_LOG"
bash "$LAB_DIR/toogle_vm.sh" status > "$EVIDENCE_DIR/pre-state.txt" 2>> "$RUN_LOG" || true

echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Starting command under budget ${Y2_LAB_BUDGET_SECONDS}s: $*" >> "$RUN_LOG"

set +e
"$TIMEOUT_BIN" --kill-after=30 "$Y2_LAB_BUDGET_SECONDS" "$@" &
CHILD_PID=$!
wait "$CHILD_PID" 2>/dev/null
CMD_RC=$?
set -e

if [ "$CMD_RC" -eq 124 ]; then
    FINAL_EXIT=124
else
    FINAL_EXIT=$CMD_RC
fi

echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Command completed with exit code $CMD_RC" >> "$RUN_LOG"
exit "$FINAL_EXIT"
