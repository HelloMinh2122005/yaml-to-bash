#!/usr/bin/env bash
set -e

BASE_URL="https://github.com/HelloMinh2122005/yaml-to-bash/blob/main"

FILES=(
  ".gitignore"
  "CLAUDE.md"
  "IMPLEMENT_PLAN_v2.md"
  "IMPLEMENT_PLAN_v3.md"
  "README.md"
  "USER_GUIDE.md"
  "pull_workspace.sh"
  "custom_git_pull.sh"
  "bin/y2b"
  "bin/y2p"
  "examples/linux/job.yaml"
  "examples/linux/list_vm.csv"
  "examples/linux/scripts/audit_hardware.sh"
  "examples/linux/scripts/calc_compliance.sh"
  "examples/windows/job.yaml"
  "examples/windows/list_vm.csv"
  "examples/windows/scripts/check_tls.ps1"
  "requirements.txt"
  "setup_venv.sh"
  "tests/fake_az/az"
  "tests/golden/hip_compliance_remote.sh"
  "tests/golden/windows_baseline_remote.ps1"
  "tests/test_collectlist.py"
  "tests/test_compiler.py"
  "tests/test_config.py"
  "tests/test_hostenv.py"
  "tests/test_inputcsv.py"
  "tests/test_m1b_step_var_removal.py"
  "tests/test_m2_collector_parts_merger.py"
  "tests/test_m3_precheck.py"
  "tests/test_m4_dispatcher.py"
  "tests/test_m5_runner.py"
  "tests/test_m6_fire_and_forget.py"
  "tests/test_validate.py"
  "tests/test_y2p_container.py"
  "tests/test_y2p_integration.py"
  "tests/test_y2p_unit.py"
  "y2b/__init__.py"
  "y2b/__main__.py"
  "y2b/compiler.py"
  "y2b/profile.py"
  "y2b/templates/collect.sh.tpl"
  "y2b/templates/remote.sh.tpl"
  "y2b/validate.py"
  "y2p/__init__.py"
  "y2p/__main__.py"
  "y2p/compiler.py"
  "y2p/profile.py"
  "y2p/templates/collect.ps1.tpl"
  "y2p/templates/remote.ps1.tpl"
  "y2p/validate.py"
  "y2shared/__init__.py"
  "y2shared/cli.py"
  "y2shared/collectlist.py"
  "y2shared/collector.py"
  "y2shared/config.py"
  "y2shared/dispatcher.py"
  "y2shared/hostenv.py"
  "y2shared/inputcsv.py"
  "y2shared/merger.py"
  "y2shared/parts.py"
  "y2shared/precheck.py"
  "y2shared/profile.py"
  "y2shared/runner.py"
  "y2shared/scan.py"
  "y2shared/shell.py"
  "y2shared/validate.py"
  "y2shared/vmlog.py"
)

echo "Pulling ${#FILES[@]} files via GitHub blob URLs..."

for file in "${FILES[@]}"; do
  mkdir -p "$(dirname "$file")"
  echo "Downloading $file..."

  curl -sL "${BASE_URL}/${file}" | python3 -c '
import sys, re, json
html = sys.stdin.read()
m = re.search(r"<script type=\"application/json\" data-target=\"react-app\.embeddedData\">(.*?)</script>", html)
if m:
    data = json.loads(m.group(1))
    lines = data["payload"]["codeViewBlobLayoutRoute.StyledBlob"]["rawLines"]
    sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))
else:
    sys.stderr.write("Failed to parse embedded data for: " + sys.argv[1] + "\n")
    sys.exit(1)
' "$file" > "$file"

done

# Apply executable bits
chmod +x bin/y2b bin/y2p setup_venv.sh tests/fake_az/az pull_workspace.sh custom_git_pull.sh 2>/dev/null || true

echo "All ${#FILES[@]} files successfully pulled and directory structure recreated."
