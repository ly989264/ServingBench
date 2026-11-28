#!/bin/bash
# Makes the deployment pack from this folder: a copy of it named panel2-v4-<VERSION>, with SHA256SUMS (the file list
# that scripts/safety.py checks after installation) added to the copy, packed into <output>.tar.gz. This folder is not changed.
# Usage: bin/make_pack.sh <output.tar.gz>
# Not in SHA256SUMS and not in the pack: the cluster's own clickhouse.env, grafana.env and ingest.json, and generated/,
# run/, logs/. offline/ goes into the pack when present but is not in SHA256SUMS (the same exclusions as safety.py).
set -eu
PACK_DIR=$(cd "$(dirname "$0")/.." && pwd)
[ $# -eq 1 ] || { echo "用法：bin/make_pack.sh <输出文件.tar.gz>" >&2; exit 1; }
out=$1
case $out in /*) ;; *) out=$PWD/$out ;; esac
version=$(cat "$PACK_DIR/VERSION")
if command -v sha256sum >/dev/null 2>&1; then sum() { sha256sum "$@"; }; else sum() { shasum -a 256 "$@"; }; fi
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
dst=$stage/panel2-v4-$version
mkdir "$dst"
( cd "$PACK_DIR" && tar -cf - --exclude=./clickhouse.env --exclude=./grafana.env --exclude=./ingest.json --exclude=./generated \
  --exclude=./run --exclude=./logs --exclude=./SHA256SUMS --exclude=.DS_Store . ) | ( cd "$dst" && tar -xf - )
( cd "$dst" && find . -type f ! -name SHA256SUMS ! -path './offline/*' | LC_ALL=C sort | while read -r f; do sum "$f"; done > SHA256SUMS )
echo "SHA256SUMS：$(wc -l < "$dst/SHA256SUMS" | tr -d ' ') 个文件"
tar -czf "$out" -C "$stage" "panel2-v4-$version"
echo "部署包：$out（目录 panel2-v4-$version）"
