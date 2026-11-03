# ==============================================================================
# Setup steps template for __TOOL__
# Handles Ubuntu PEP 668 externally-managed venv & exports PATH
# ==============================================================================
steps:
  - bash: |
      set -euo pipefail
      VENV="$(Agent.TempDirectory)/y2venv"
      python3 -m venv "$VENV"
      "$VENV/bin/pip" install --quiet 'PyYAML>=6.0'
      echo "##vso[task.prependpath]$VENV/bin"
      echo "##vso[task.prependpath]$(Build.SourcesDirectory)/bin"
      python3 --version; bash --version | head -1; az version -o tsv --query '"azure-cli"'
    displayName: Set up __TOOL__

