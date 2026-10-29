#!/usr/bin/env bash
# ==============================================================================
# Setup and activation helper for yaml_to_bash virtual environment (.venv)
# Usage:
#   source setup_venv.sh   # Creates venv (if missing), installs deps & activates
#   ./setup_venv.sh        # Creates venv & installs dependencies
# ==============================================================================

# Find script directory regardless of where it is called from
if [ -n "$BASH_SOURCE" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
elif [ -n "$ZSH_VERSION" ]; then
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
else
  SCRIPT_DIR="$(pwd)"
fi

VENV_DIR="${SCRIPT_DIR}/.venv"
REQ_FILE="${SCRIPT_DIR}/requirements.txt"

# 1. Check for Python 3.9+
if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 not found in PATH. Please install Python 3.9 or higher." >&2
  return 1 2>/dev/null || exit 1
fi

# 2. Create virtual environment if it doesn't exist
if [ ! -d "$VENV_DIR" ]; then
  echo "Creating virtual environment in: $VENV_DIR ..."
  python3 -m venv "$VENV_DIR"
  if [ $? -ne 0 ]; then
    echo "ERROR: Failed to create virtual environment." >&2
    return 1 2>/dev/null || exit 1
  fi
fi

# 3. Install/upgrade requirements
if [ -f "$REQ_FILE" ]; then
  echo "Installing / verifying dependencies from requirements.txt ..."
  "${VENV_DIR}/bin/pip" install --quiet --upgrade pip
  "${VENV_DIR}/bin/pip" install --quiet -r "$REQ_FILE"
  if [ $? -ne 0 ]; then
    echo "ERROR: Failed to install requirements." >&2
    return 1 2>/dev/null || exit 1
  fi
fi

# 4. Check if this script is being sourced or executed directly
is_sourced() {
  if [ -n "$ZSH_VERSION" ]; then
    case $ZSH_EVAL_CONTEXT in *:file) return 0;; esac
    return 1
  elif [ -n "$BASH_VERSION" ]; then
    [ "${BASH_SOURCE[0]}" != "${0}" ] && return 0
    return 1
  fi
  return 1
}

if is_sourced; then
  # Sourced: activate in current shell
  source "${VENV_DIR}/bin/activate"
  # Add bin/y2b to PATH for current session if not present
  if [[ ":$PATH:" != *":${SCRIPT_DIR}/bin:"* ]]; then
    export PATH="${SCRIPT_DIR}/bin:${PATH}"
  fi
  echo "=================================================="
  echo "✓ Virtual environment activated: (.venv)"
  echo "✓ 'y2b' command is now available in your PATH."
  echo "=================================================="
  echo "Quick check: try running 'y2b --help'"
else
  # Executed directly in subshell
  echo "=================================================="
  echo "✓ Virtual environment created and dependencies installed."
  echo "=================================================="
  echo "To activate it in your current terminal session, run:"
  echo ""
  echo "    source setup_venv.sh"
  echo "    # or: source .venv/bin/activate"
  echo ""
fi
