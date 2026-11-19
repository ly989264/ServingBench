#!/bin/bash
# Build the delivery tar: what goes into the glm5.3 containers, with SHA256SUMS at the root.
# Usage: tools/make_tar.sh OUT.tar
set -euo pipefail
OUT=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
KIT=$(cd "$(dirname "$0")/.." && pwd)
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/patches" "$STAGE/guides"
for d in vllm vllm-ascend p2d mooncake; do
  cp -R "$KIT/patches/$d" "$STAGE/patches/"
done
cp -R "$KIT/detect" "$STAGE/"
cp "$KIT/README.zh-CN.md" "$STAGE/"
cp "$KIT/guides/操作说明A_glm53_容器内打补丁.zh-CN.md" "$STAGE/guides/"
find "$STAGE" \( -name __pycache__ -o -name .DS_Store \) -prune -exec rm -rf {} +
(cd "$STAGE" && find . -type f ! -name SHA256SUMS | sed 's#^\./##' | LC_ALL=C sort | while read -r f; do shasum -a 256 "$f"; done > SHA256SUMS)
(cd "$STAGE" && COPYFILE_DISABLE=1 tar --format=ustar --uid 0 --gid 0 --uname root --gname root -cf "$OUT" .)
shasum -a 256 "$OUT"
