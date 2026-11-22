#!/bin/bash
# Build the Mooncake incremental package v2 (T58, patch 04b): what goes into the containers, SHA256SUMS at the root.
# Sources are taken from this kit: replacement v2 (mooncake/replace/lib_v2, decompressed and checked against its
# SHA256SUMS), the precheck glm53mc-6 dist with its usage note and lab output, patches 04 and 04b, the v2 lab results
# and the README. Reproducible: fixed times and modes, sorted file list.
# Usage: tools/make_tar.sh OUT.tar
set -euo pipefail
OUT=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
INC=$(cd "$(dirname "$0")/.." && pwd)
MC=$(cd "$INC/.." && pwd)
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
chmod 755 "$STAGE"
mkdir -p "$STAGE/lib" "$STAGE/precheck/dist" "$STAGE/precheck/lab" "$STAGE/reference" "$STAGE/lab"
xz -dc "$MC/replace/lib_v2/libmooncake_store.so.xz" > "$STAGE/lib/libmooncake_store.so"
want=$(awk '$2 == "libmooncake_store.so" {print $1}' "$MC/replace/lib_v2/SHA256SUMS")
got=$(shasum -a 256 "$STAGE/lib/libmooncake_store.so" | cut -c1-64)
[ "$want" = "$got" ] || { echo "replacement v2 sha256 $got, expected $want"; exit 1; }
want=$(awk '{print $1}' "$MC/precheck/dist6/SHA256SUMS")
got=$(shasum -a 256 "$MC/precheck/dist6/t51_mc_precheck.py" | cut -c1-64)
[ "$want" = "$got" ] || { echo "precheck sha256 $got, expected $want"; exit 1; }
cp "$MC/precheck/dist6/t51_mc_precheck.py" "$STAGE/precheck/dist/"; cp "$MC/precheck/使用说明.zh-CN.md" "$STAGE/precheck/"
cp "$MC/precheck/lab/lab_v6.txt" "$MC/precheck/lab/run_lab_v6.sh" "$STAGE/precheck/lab/"
cp "$MC/patch/04-mooncake-0.3.13-native-timing.patch" "$MC/patch/04b-mooncake-timing-files.patch" "$STAGE/reference/"
cp "$MC/replace/lab_v2/"*.txt "$STAGE/lab/"
cp "$INC/README.zh-CN.md" "$STAGE/"
find "$STAGE" \( -name __pycache__ -o -name .DS_Store \) -prune -exec rm -rf {} +
find "$STAGE" -type d -exec chmod 0755 {} +
find "$STAGE" -type f -exec chmod 0644 {} +
chmod 0755 "$STAGE/lib/libmooncake_store.so"
(cd "$STAGE" && find . -type f ! -name SHA256SUMS | sed 's#^\./##' | LC_ALL=C sort | while read -r f; do shasum -a 256 "$f"; done > SHA256SUMS)
chmod 0644 "$STAGE/SHA256SUMS"
find "$STAGE" -exec touch -h -t 202611200000 {} +
(cd "$STAGE" && find . -mindepth 1 | sed 's#^\./##' | LC_ALL=C sort > "$STAGE.list" && \
  COPYFILE_DISABLE=1 tar --format=ustar --uid 0 --gid 0 --uname root --gname root --no-recursion -cf "$OUT" -T "$STAGE.list")
rm -f "$STAGE.list"
shasum -a 256 "$OUT"
