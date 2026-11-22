#!/bin/bash
# Build the T56 patch kit for glm5.3 (D-side additions, upgrade from T51 on D only).
# Everything is taken from commits, not working trees:
#   vLLM          t51-glm53-vllm   VLLM_COMMIT    patches/vllm/t56/ (lab/ left out)
#   vllm-ascend   t51-glm53-ascend ASCEND_COMMIT  patches/vllm-ascend/ (optional/ left out) + tools/apply_check.sh
#   p2d           t51-glm53-p2d    P2D_COMMIT     patches/vllm-ascend-p2d/ (tests/ kept) + field_check/
#   T51 after-sums from the T51 kit tar (the T56 upgrade checks the T51 state before replacing)
#   detector      this kit, detect/dist7/ (glm53-7) with its SHA256SUMS; usage note and baseline list
#   README from t56kit/ (the upgrade manual stays outside the kit: it quotes the kit's sha256)
# Usage: tools/make_kit.sh OUT.tar
set -euo pipefail
OUT=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
KIT=$(cd "$(dirname "$0")/../.." && pwd); T56=$KIT/t56kit
VLLM_REPO=${VLLM_REPO:?set VLLM_REPO}; VLLM_COMMIT=${VLLM_COMMIT:-796927a}
ASC_REPO=${ASC_REPO:?set ASC_REPO}; ASCEND_COMMIT=${ASCEND_COMMIT:-3380187}
P2D_REPO=${P2D_REPO:?set P2D_REPO}; P2D_COMMIT=${P2D_COMMIT:-d7d26f3}
T51_TAR=${T51_TAR:?set T51_TAR}
STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT; chmod 755 "$STAGE"
mkdir -p "$STAGE/patches/vllm" "$STAGE/patches/vllm-ascend/tools" "$STAGE/patches/p2d" "$STAGE/patches/mooncake" "$STAGE/detect"
# T51 kit, for the T51 after-sums
echo "d21cc3196e8c3430ff851c9810cf8ed6ddc6b619a41c3a510c0af9f211c24815  $T51_TAR" | shasum -a 256 -c - >/dev/null
T51X=$(mktemp -d); trap 'rm -rf "$STAGE" "$T51X"' EXIT
tar -xf "$T51_TAR" -C "$T51X" ./patches/vllm/SHA256SUMS.after ./patches/vllm-ascend/SHA256SUMS.after
# vLLM
(cd "$VLLM_REPO" && git archive "$VLLM_COMMIT" patches/vllm/t56) | (cd "$STAGE/patches/vllm" && tar -x --strip-components 3 -f -)
rm -rf "$STAGE/patches/vllm/lab"
cp "$T51X/patches/vllm/SHA256SUMS.after" "$STAGE/patches/vllm/SHA256SUMS.t51"
# vllm-ascend
(cd "$ASC_REPO" && git archive "$ASCEND_COMMIT" patches/vllm-ascend) | (cd "$STAGE/patches/vllm-ascend" && tar -x --strip-components 2 -f -)
rm -rf "$STAGE/patches/vllm-ascend/optional"
(cd "$ASC_REPO" && git show "$ASCEND_COMMIT:tools/apply_check.sh") > "$STAGE/patches/vllm-ascend/tools/apply_check.sh"
cp "$T51X/patches/vllm-ascend/SHA256SUMS.after" "$STAGE/patches/vllm-ascend/SHA256SUMS.t51"
cp "$T56/SHA256SUMS.d4ad49796-unchanged" "$STAGE/patches/vllm-ascend/"
# p2d
(cd "$P2D_REPO" && git archive "$P2D_COMMIT" patches/vllm-ascend-p2d) | (cd "$STAGE/patches/p2d" && tar -x --strip-components 2 -f -)
mkdir -p "$STAGE/patches/p2d/field_check"
(cd "$P2D_REPO" && git archive "$P2D_COMMIT" field_check) | (cd "$STAGE/patches/p2d/field_check" && tar -x --strip-components 1 -f -)
# mooncake: note only
cp "$T56/mooncake_README.zh-CN.md" "$STAGE/patches/mooncake/README.zh-CN.md"
# detector
want=$(awk '{print $1}' "$KIT/detect/dist7/SHA256SUMS"); got=$(shasum -a 256 "$KIT/detect/dist7/t51_glm53_check.py" | cut -c1-64)
[ "$want" = "$got" ] || { echo "detector sha256 $got, expected $want"; exit 1; }
cp "$KIT/detect/dist7/t51_glm53_check.py" "$STAGE/detect/"; (cd "$STAGE/detect" && shasum -a 256 t51_glm53_check.py > SHA256SUMS)
cp "$T56/detect_使用说明.zh-CN.md" "$STAGE/detect/使用说明.zh-CN.md"; cp "$T56/PORT_BASELINE.txt" "$STAGE/detect/"
# readme, guides
cp "$T56/README.zh-CN.md" "$STAGE/"
find "$STAGE" \( -name __pycache__ -o -name .DS_Store \) -prune -exec rm -rf {} +
# the three after-sums must hold on the replacement files
(cd "$STAGE/patches/vllm/files" && shasum -a 256 -c ../SHA256SUMS.after | grep -v ': OK$') && { echo "vllm files differ from SHA256SUMS.after"; exit 1; } || true
(cd "$STAGE/patches/vllm-ascend/files" && shasum -a 256 -c ../SHA256SUMS.after | grep -v ': OK$') && { echo "ascend files differ from SHA256SUMS.after"; exit 1; } || true
(cd "$STAGE" && find . -type f | sed 's#^\./##' | grep -v '^SHA256SUMS$' | LC_ALL=C sort | while read -r f; do shasum -a 256 "$f"; done > "$T51X/sums" && mv "$T51X/sums" SHA256SUMS)
# reproducible: fixed times and modes, files in sorted order (the manual quotes this tar's sha256)
find "$STAGE" -type d -exec chmod 755 {} + ; find "$STAGE" -type f -exec chmod 644 {} + ; chmod 755 "$STAGE/patches/vllm-ascend/tools/apply_check.sh"
find "$STAGE" -exec touch -h -t 202611200000 {} +
(cd "$STAGE" && find . -type f | LC_ALL=C sort > "$T51X/list" && COPYFILE_DISABLE=1 tar --format=ustar --uid 0 --gid 0 --uname root --gname root -cf "$OUT" -T "$T51X/list")
shasum -a 256 "$OUT"
