#!/bin/bash
# Build both versions of the glm5.3 patching manual into OUT_DIR:
#   glm5.3测试集群打补丁操作手册.zh-CN.md            placeholder version (shareable)
#   glm5.3测试集群打补丁操作手册_已填路径.zh-CN.md    USER_DIR and ROOT filled from the v3 filled update script (private Caching only)
# Commands come byte for byte from the sources in src/ (build_manual.py); the criteria check must pass.
# Usage: tools/make_manual.sh OUT_DIR V3_FILLED_UPDATE_SCRIPT
set -euo pipefail
OUT=$1; V3=$2; M=$(cd "$(dirname "$0")/.." && pwd); PY=${PY:-/opt/homebrew/bin/python3.12}
mkdir -p "$OUT"
P="$OUT/glm5.3测试集群打补丁操作手册.zh-CN.md"; F="$OUT/glm5.3测试集群打补丁操作手册_已填路径.zh-CN.md"
SRCS=("$M/src/操作说明A_207d7a4.zh-CN.md" "$M/src/第11步_6d70978c.zh-CN.md")
SRCS+=("$M/src/面板手册_37d4595.zh-CN.md" "$M/src/面板手册交付说明_37d4595.zh-CN.md")
"$PY" -I "$M/build_manual.py" "$M/template.zh-CN.md" "$M/parts" "$P" "${SRCS[@]}"
"$PY" -I "$M/lab/criteria_check.py" "$P" "$M/src/操作说明A_207d7a4.zh-CN.md" "$M/src/第11步_6d70978c.zh-CN.md" > "$OUT/criteria_check.txt" || { tail -3 "$OUT/criteria_check.txt"; echo "criteria check failed"; exit 1; }
"$PY" -I "$M/tools/fill_guide.py" "$V3" "$P" "$F" >/dev/null
grep -q '<用户目录>\|<总目录>' "$F" && { echo "filled version still has <用户目录>/<总目录>"; exit 1; }
tail -1 "$OUT/criteria_check.txt"
(cd "$OUT" && shasum -a 256 "$(basename "$P")" "$(basename "$F")")
