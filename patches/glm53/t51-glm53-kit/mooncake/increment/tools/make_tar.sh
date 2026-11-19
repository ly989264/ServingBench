#!/bin/bash
# Build the Mooncake incremental package (guide A step 11) for glm5.3: what goes into the containers, SHA256SUMS at the
# root. Sources are taken from this kit: the replacement (mooncake/replace/lib, decompressed and checked against its
# SHA256SUMS), the precheck dist and its usage note and lab output, patch 04, the step 11 guide and the README.
# Usage: tools/make_tar.sh OUT.tar
set -euo pipefail
OUT=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
INC=$(cd "$(dirname "$0")/.." && pwd)
MC=$(cd "$INC/.." && pwd)
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
chmod 755 "$STAGE"
mkdir -p "$STAGE/lib" "$STAGE/precheck/dist" "$STAGE/precheck/lab" "$STAGE/reference" "$STAGE/guides"
xz -dc "$MC/replace/lib/libmooncake_store.so.xz" > "$STAGE/lib/libmooncake_store.so"
chmod 0755 "$STAGE/lib/libmooncake_store.so"
want=$(awk '$2 == "libmooncake_store.so" {print $1}' "$MC/replace/lib/SHA256SUMS")
got=$(shasum -a 256 "$STAGE/lib/libmooncake_store.so" | cut -c1-64)
[ "$want" = "$got" ] || { echo "replacement sha256 $got, expected $want"; exit 1; }
want=$(awk '{print $1}' "$MC/precheck/dist/SHA256SUMS")
got=$(shasum -a 256 "$MC/precheck/dist/t51_mc_precheck.py" | cut -c1-64)
[ "$want" = "$got" ] || { echo "precheck sha256 $got, expected $want"; exit 1; }
cp "$MC/precheck/dist/t51_mc_precheck.py" "$STAGE/precheck/dist/"; cp "$MC/precheck/使用说明.zh-CN.md" "$STAGE/precheck/"
cp "$MC/precheck/lab/lab_T0-T10.txt" "$MC/precheck/lab/run_lab.sh" "$STAGE/precheck/lab/"
cp "$MC/patch/04-mooncake-0.3.13-native-timing.patch" "$STAGE/reference/"
cp "$INC/guides/操作说明A_第11步_替换Mooncake库.zh-CN.md" "$STAGE/guides/"
cp "$INC/README.zh-CN.md" "$STAGE/"
find "$STAGE" \( -name __pycache__ -o -name .DS_Store \) -prune -exec rm -rf {} +
(cd "$STAGE" && find . -type f ! -name SHA256SUMS | sed 's#^\./##' | LC_ALL=C sort | while read -r f; do shasum -a 256 "$f"; done > SHA256SUMS)
(cd "$STAGE" && COPYFILE_DISABLE=1 tar --format=ustar --uid 0 --gid 0 --uname root --gname root -cf "$OUT" .)
shasum -a 256 "$OUT"
