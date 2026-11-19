#!/bin/bash
# Read-only personal-data scan of the manual deliverables (placeholder manual, walk records, criteria check) with the
# patch kit's scanner; the filled version is checked only for what it is meant to contain (USER_DIR and ROOT).
# Local names come from the environment at run time.  Usage: scan_manual.sh OUT_DIR SCAN_DIR
set -u
OUT=$1; S=$2; HERE=$(cd "$(dirname "$0")" && pwd); PY=${PY:-/opt/homebrew/bin/python3.12}
SCANNER=$HERE/../../mooncake/increment/lab/scan_pii.py
rm -rf "$S"; mkdir -p "$S/x"
cp "$OUT/glm5.3测试集群打补丁操作手册.zh-CN.md" "$OUT/criteria_check.txt" "$S/x/"; cp "$HERE"/walk_*.txt "$S/x/"
echo "== placeholder manual, walk records, criteria check"; "$PY" -I "$SCANNER" "$S/x"
echo "== local names (this account, home directory, work directories)"; k=0
for n in "$(id -un)" "$(basename "$HOME")" Desktop scratchpad /private/tmp claude-501; do
  k=$((k + 1)); echo "name #$k: files with it $(grep -rIl -F -- "$n" "$S/x" | wc -l)"
done
F="$OUT/glm5.3测试集群打补丁操作手册_已填路径.zh-CN.md"
echo "== filled version: lines that differ from the placeholder version"; diff "$OUT/glm5.3测试集群打补丁操作手册.zh-CN.md" "$F" | grep -c '^>'
