#!/bin/bash
# Build the delivery tar: only what goes into the intranet containers, with SHA256SUMS at the root.
# Usage: tools/make_tar.sh OUT.tar
set -euo pipefail
OUT=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
KIT=$(cd "$(dirname "$0")/.." && pwd)
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/detect" "$STAGE/patches" "$STAGE/guides"
cp "$KIT/detect/dist/t51_dsv41_check.py" "$KIT/detect/使用说明.zh-CN.md" "$STAGE/detect/"
for d in vllm vllm-ascend; do
  mkdir -p "$STAGE/patches/$d"
  cp -R "$KIT/patches/$d/files" "$STAGE/patches/$d/"
  cp "$KIT/patches/$d"/SHA256SUMS.before "$KIT/patches/$d"/SHA256SUMS.after "$KIT/patches/$d"/NEW_FILES.txt "$KIT/patches/$d"/README.zh-CN.md "$STAGE/patches/$d/"
done
# the colleague's patch 01 stays out of the package (it carries a personal intranet path; the replacement
# files are its result); the vllm-ascend patch is ours and goes in
cp "$KIT/patches/vllm-ascend"/*.patch "$STAGE/patches/vllm-ascend/"
mkdir -p "$STAGE/patches/mooncake"
cp "$KIT/patches/mooncake"/*.patch "$KIT/patches/mooncake/README.zh-CN.md" "$STAGE/patches/mooncake/"
cp -R "$KIT/patches/mooncake/lib" "$STAGE/patches/mooncake/"
cp "$KIT/guides/操作说明A_容器内打补丁.zh-CN.md" "$STAGE/guides/"
(cd "$STAGE" && find . -type f ! -name SHA256SUMS | sed 's#^\./##' | LC_ALL=C sort | while read -r f; do shasum -a 256 "$f"; done > SHA256SUMS)
(cd "$STAGE" && COPYFILE_DISABLE=1 tar --format=ustar -cf "$OUT" .)
shasum -a 256 "$OUT"
