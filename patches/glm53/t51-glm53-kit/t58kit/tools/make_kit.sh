#!/bin/bash
# Build the T58 patch kit for glm5.3: the granularity switch PD_DIAG_LEVEL (vLLM, vllm-ascend, p2d); level 3, the
# default, writes exactly what T56 writes. Upgrade paths: glm53-test from T56, glm53-1 from the site's original files.
# Everything is taken from commits, not working trees:
#   vLLM          t51-glm53-vllm   VLLM_COMMIT    patches/vllm/t58/ (lab/ left out)
#   vllm-ascend   t51-glm53-ascend ASCEND_COMMIT  patches/vllm-ascend/ (optional/ left out) + tools/apply_check.sh
#   p2d           t51-glm53-p2d    P2D_COMMIT     patches/vllm-ascend-p2d/ (tests/ kept) + field_check/
#   T56 after-sums from the T56 kit tar (glm53-test checks the T56 state before replacing): SHA256SUMS.t56
#   the site-original vLLM lists from the T51 kit tar (glm53-1 checks the original state): SHA256SUMS.before, NEW_FILES.txt
#   detector      this kit, detect/dist8/ (glm53-8) with its SHA256SUMS; usage note and baseline list
#   README from t58kit/ (the manuals stay outside the kit: they quote the kit's sha256)
# Usage: VLLM_COMMIT=… ASCEND_COMMIT=… P2D_COMMIT=… tools/make_kit.sh OUT.tar
set -euo pipefail
OUT=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
KIT=$(cd "$(dirname "$0")/../.." && pwd); T58=$KIT/t58kit
VLLM_REPO=${VLLM_REPO:?set VLLM_REPO}; VLLM_COMMIT=${VLLM_COMMIT:?}
ASC_REPO=${ASC_REPO:?set ASC_REPO}; ASCEND_COMMIT=${ASCEND_COMMIT:?}
P2D_REPO=${P2D_REPO:?set P2D_REPO}; P2D_COMMIT=${P2D_COMMIT:?}
VLLM_DIR=${VLLM_DIR:-patches/vllm/t58}; P2D_DIR=${P2D_DIR:-patches/vllm-ascend-p2d}
T51_TAR=${T51_TAR:?set T51_TAR}
T56_TAR=${T56_TAR:?set T56_TAR}
STAGE=$(mktemp -d); X=$(mktemp -d); trap 'rm -rf "$STAGE" "$X"' EXIT; chmod 755 "$STAGE"
mkdir -p "$STAGE/patches/vllm" "$STAGE/patches/vllm-ascend/tools" "$STAGE/patches/p2d" "$STAGE/patches/mooncake" "$STAGE/detect"
echo "d21cc3196e8c3430ff851c9810cf8ed6ddc6b619a41c3a510c0af9f211c24815  $T51_TAR" | shasum -a 256 -c - >/dev/null
echo "0d2bc9a86a3950460e88c9262cf203c79888d08189150798edbd388882022006  $T56_TAR" | shasum -a 256 -c - >/dev/null
mkdir -p "$X/t51" "$X/t56"
tar -xf "$T51_TAR" -C "$X/t51" ./patches/vllm/SHA256SUMS.before ./patches/vllm/NEW_FILES.txt
tar -xf "$T56_TAR" -C "$X/t56" ./patches/vllm/SHA256SUMS.after ./patches/vllm-ascend/SHA256SUMS.after
# vLLM
(cd "$VLLM_REPO" && git archive "$VLLM_COMMIT" "$VLLM_DIR") | (cd "$STAGE/patches/vllm" && tar -x --strip-components $(echo "$VLLM_DIR" | awk -F/ '{print NF}') -f -)
rm -rf "$STAGE/patches/vllm/lab"
cp "$X/t56/patches/vllm/SHA256SUMS.after" "$STAGE/patches/vllm/SHA256SUMS.t56"
cp "$X/t51/patches/vllm/SHA256SUMS.before" "$STAGE/patches/vllm/SHA256SUMS.before"
cp "$X/t51/patches/vllm/NEW_FILES.txt" "$STAGE/patches/vllm/NEW_FILES.txt"
# vllm-ascend
(cd "$ASC_REPO" && git archive "$ASCEND_COMMIT" patches/vllm-ascend) | (cd "$STAGE/patches/vllm-ascend" && tar -x --strip-components 2 -f -)
rm -rf "$STAGE/patches/vllm-ascend/optional"
(cd "$ASC_REPO" && git show "$ASCEND_COMMIT:tools/apply_check.sh") > "$STAGE/patches/vllm-ascend/tools/apply_check.sh"
cp "$X/t56/patches/vllm-ascend/SHA256SUMS.after" "$STAGE/patches/vllm-ascend/SHA256SUMS.t56"
cp "$T58/SHA256SUMS.d4ad49796-unchanged" "$STAGE/patches/vllm-ascend/"
# p2d
(cd "$P2D_REPO" && git archive "$P2D_COMMIT" "$P2D_DIR") | (cd "$STAGE/patches/p2d" && tar -x --strip-components $(echo "$P2D_DIR" | awk -F/ '{print NF}') -f -)
mkdir -p "$STAGE/patches/p2d/field_check"
(cd "$P2D_REPO" && git archive "$P2D_COMMIT" field_check) | (cd "$STAGE/patches/p2d/field_check" && tar -x --strip-components 1 -f -)
# mooncake: note only (the replacement library is in the Mooncake incremental package v2)
cp "$T58/mooncake_README.zh-CN.md" "$STAGE/patches/mooncake/README.zh-CN.md"
# detector
want=$(awk '{print $1}' "$KIT/detect/dist8/SHA256SUMS"); got=$(shasum -a 256 "$KIT/detect/dist8/t51_glm53_check.py" | cut -c1-64)
[ "$want" = "$got" ] || { echo "detector sha256 $got, expected $want"; exit 1; }
cp "$KIT/detect/dist8/t51_glm53_check.py" "$STAGE/detect/"; (cd "$STAGE/detect" && shasum -a 256 t51_glm53_check.py > SHA256SUMS)
cp "$T58/detect_使用说明.zh-CN.md" "$STAGE/detect/使用说明.zh-CN.md"; cp "$T58/PORT_BASELINE.txt" "$STAGE/detect/"
cp "$T58/README.zh-CN.md" "$STAGE/"
find "$STAGE" \( -name __pycache__ -o -name .DS_Store \) -prune -exec rm -rf {} +
(cd "$STAGE/patches/vllm/files" && shasum -a 256 -c ../SHA256SUMS.after | grep -v ': OK$') && { echo "vllm files differ from SHA256SUMS.after"; exit 1; } || true
(cd "$STAGE/patches/vllm-ascend/files" && shasum -a 256 -c ../SHA256SUMS.after | grep -v ': OK$') && { echo "ascend files differ from SHA256SUMS.after"; exit 1; } || true
# the site-original lists name the same 25 vLLM files as the T58 after-sums
diff <( { awk '{print $2}' "$STAGE/patches/vllm/SHA256SUMS.before"; cat "$STAGE/patches/vllm/NEW_FILES.txt"; } | LC_ALL=C sort) <(awk '{print $2}' "$STAGE/patches/vllm/SHA256SUMS.after" | LC_ALL=C sort) >/dev/null || { echo "vLLM file lists differ"; exit 1; }
(cd "$STAGE" && find . -type f | sed 's#^\./##' | grep -v '^SHA256SUMS$' | LC_ALL=C sort | while read -r f; do shasum -a 256 "$f"; done > "$X/sums" && mv "$X/sums" SHA256SUMS)
find "$STAGE" -type d -exec chmod 755 {} + ; find "$STAGE" -type f -exec chmod 644 {} + ; chmod 755 "$STAGE/patches/vllm-ascend/tools/apply_check.sh"
find "$STAGE" -exec touch -h -t 202611200000 {} +
(cd "$STAGE" && find . -type f | LC_ALL=C sort > "$X/list" && COPYFILE_DISABLE=1 tar --format=ustar --uid 0 --gid 0 --uname root --gname root -cf "$OUT" -T "$X/list")
shasum -a 256 "$OUT"
