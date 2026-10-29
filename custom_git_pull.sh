#!/usr/bin/env bash
set -e

REPO="HelloMinh2122005/yaml-to-bash"
BRANCH="main"
SCRIPT_NAME="$(basename "$0")"

echo "=== 1. Cleaning workspace (preserving .git) ==="
find . -mindepth 1 -maxdepth 1 ! -name ".git" ! -name "$SCRIPT_NAME" -exec rm -rf {} +

echo "=== 2. Discovering latest file list from GitHub main ==="
FILES=$(curl -sL "https://github.com/${REPO}/tree/${BRANCH}" | python3 -c '
import sys, json, re, subprocess

html = sys.stdin.read()
m = re.search(r"<script type=\"application/json\" data-target=\"react-app\.embeddedData\">(.*?)</script>", html)
if not m:
    sys.stderr.write("Failed to find metadata in tree page\n")
    sys.exit(1)

data = json.loads(m.group(1))
oid = data["payload"]["codeViewLayoutRoute"]["refInfo"]["currentOid"]
tree_url = f"https://github.com/'"${REPO}"'/tree-list/{oid}"

out = subprocess.check_output(["curl", "-sL", "-H", "Accept: application/json", tree_url])
tree_data = json.loads(out)
for p in tree_data.get("paths", []):
    print(p)
')

TOTAL=$(echo "$FILES" | wc -l)
echo "Found $TOTAL files to pull."

echo "=== 3. Pulling files via GitHub blob URLs ==="
COUNT=0
for file in $FILES; do
  mkdir -p "$(dirname "$file")"

  curl -sL "https://github.com/${REPO}/blob/${BRANCH}/${file}" | python3 -c '
import sys, re, json
html = sys.stdin.read()
m = re.search(r"<script type=\"application/json\" data-target=\"react-app\.embeddedData\">(.*?)</script>", html)
if m:
    data = json.loads(m.group(1))
    lines = data["payload"]["codeViewBlobLayoutRoute.StyledBlob"]["rawLines"]
    sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))
else:
    sys.stderr.write("Failed to parse: " + sys.argv[1] + "\n")
    sys.exit(1)
' "$file" > "$file"

  COUNT=$((COUNT + 1))
  printf "\rProgress: [%d/%d] %s" "$COUNT" "$TOTAL" "$file"
done

echo ""
echo "=== 4. Setting permissions ==="
[ -d "bin" ] && chmod +x bin/* || true
[ -f "setup_venv.sh" ] && chmod +x setup_venv.sh || true
[ -f "tests/fake_az/az" ] && chmod +x tests/fake_az/az || true
[ -f "pull_workspace.sh" ] && chmod +x pull_workspace.sh || true
[ -f "custom_git_pull.sh" ] && chmod +x custom_git_pull.sh || true

echo "=== Pull complete! Workspace is up to date with remote main. ==="
