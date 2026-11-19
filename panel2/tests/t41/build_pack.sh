#!/bin/bash
# Lab only (T41-D): build vllm-pd-grafana-panel2.tar.gz from a commit of this repository, the way the demo repository's
# tools/build_panel2_pack.sh builds a delivery tar (MANIFEST.txt and SHA256SUMS inside, root-owned, no macOS metadata).
# The pack is taken from `git archive`, so untracked files of the working tree never get in.
#   tests/t41/build_pack.sh <out dir> [commit, default HEAD]
set -euo pipefail
OUT=${1:?out dir}
REV=${2:-HEAD}
REPO=$(cd "$(dirname "$0")/../.." && pwd)
PACK=vllm-pd-grafana-panel2
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
git -C "$REPO" archive --format=tar "$REV" "$PACK" | tar -xf - -C "$WORK"
python3 -I "$WORK/$PACK/scripts/generate.py" >/dev/null   # fails here if segments.json is broken
rm -rf "$WORK/$PACK/generated" "$WORK/$PACK/.gitignore"
find "$WORK/$PACK" -name __pycache__ -prune -exec rm -rf {} +
chmod 755 "$WORK/$PACK"/bin/*.sh "$WORK/$PACK"/scripts/*.py "$WORK/$PACK"/mock/*.py
cd "$WORK/$PACK"
files=$(find . -type f | sed 's|^\./||' | LC_ALL=C sort)
: > SHA256SUMS
for f in $files; do shasum -a 256 "$f" >> SHA256SUMS; done
{
  echo "vllm-pd-grafana-panel2 — 面板二（请求时延拆解）deployment pack: mock logs, Vector, ClickHouse, Grafana dashboard"
  echo "version: $(cat VERSION)"
  echo "built: $(date -u +%Y-%m-%dT%H:%M:%SZ) from $(git -C "$REPO" rev-parse --short "$REV")"
  echo "versions: vector 0.61.0, clickhouse/clickhouse-server:26.8.36.3, grafana-clickhouse-datasource 4.23.4, Grafana 12.4.2, Prometheus 3.11.2"
  echo
  printf '%-10s  %-64s  %s\n' bytes sha256 path
  for f in $files; do printf '%-10s  %-64s  %s\n' "$(wc -c < "$f" | tr -d ' ')" "$(shasum -a 256 "$f" | cut -d' ' -f1)" "$f"; done
} > MANIFEST.txt
mkdir -p "$OUT"
cd "$WORK"
COPYFILE_DISABLE=1 tar --no-mac-metadata --no-xattrs --uid 0 --gid 0 --uname root --gname root -czf "$OUT/$PACK.tar.gz" "$PACK"
cd "$OUT"
shasum -a 256 "$PACK.tar.gz" > "$PACK.tar.gz.sha256"
cat "$PACK.tar.gz.sha256"
