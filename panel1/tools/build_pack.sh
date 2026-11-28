#!/bin/bash
# Build dist/vllm-pd-grafana-panel1.tar.gz from pack/vllm-pd-grafana-panel1 (dashboard 1 deployment pack). Run from panel1/.
# Regenerates the example prometheus.yml, then writes MANIFEST.txt and SHA256SUMS
# inside the pack and the tarball's own .sha256 next to it. Nothing is pushed or published.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PACK=vllm-pd-grafana-panel1
SRC="$ROOT/pack/$PACK"
DIST="$ROOT/dist"

# Since T26 the dashboards are the merged baseline in grafana/dashboards/, not the demo export: NO_EXPORT=1 keeps them.
# This repository does not carry the demo export script, so the build refuses to run without NO_EXPORT=1.
if [ "${NO_EXPORT:-}" != 1 ]; then
  echo "run as: NO_EXPORT=1 tools/build_pack.sh (the dashboards in grafana/dashboards/ are the source; there is no export step)" >&2
  exit 2
fi
# git does not keep empty directories; the delivered tar has these two empty provisioning directories.
mkdir -p "$SRC/grafana/provisioning/alerting" "$SRC/grafana/provisioning/plugins"
python3 -I "$SRC/prometheus/gen_prometheus_yml.py" "$SRC/prometheus/inventory.example.json" > "$SRC/prometheus/prometheus.example.yml"
find "$SRC" \( -name .DS_Store -o -name __pycache__ -o -name '*.pyc' \) -prune -exec rm -rf {} +
chmod 755 "$SRC/prometheus/gen_prometheus_yml.py" "$SRC"/scripts/*.py "$SRC"/bin/*.sh

cd "$SRC"
files=$(find . -type f ! -name MANIFEST.txt ! -name SHA256SUMS | sed 's|^\./||' | LC_ALL=C sort)
: > SHA256SUMS
for f in $files; do shasum -a 256 "$f" >> SHA256SUMS; done
{
  echo "vllm-pd-grafana-panel1 — dashboards 面板一·P and 面板一·D (vLLM P/D service view) deployment pack v1.2"
  echo "built: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "images: grafana/grafana:12.4.2, prom/prometheus:v3.11.2 (dashboard schemaVersion 42)"
  echo "patch base: vllm-ascend releases/v0.32.0rc 3b1da3fe9ac5a40ec6a200f31a20d33f67e2f02c"
  echo
  printf '%-10s  %-64s  %s\n' bytes sha256 path
  for f in $files; do printf '%-10s  %-64s  %s\n' "$(wc -c < "$f" | tr -d ' ')" "$(shasum -a 256 "$f" | cut -d' ' -f1)" "$f"; done
} > MANIFEST.txt

mkdir -p "$DIST"
cd "$ROOT/pack"
COPYFILE_DISABLE=1 tar --no-mac-metadata --uid 0 --gid 0 --uname root --gname root -czf "$DIST/$PACK.tar.gz" "$PACK"
cd "$DIST"
shasum -a 256 "$PACK.tar.gz" > "$PACK.tar.gz.sha256"
cp "$SRC/MANIFEST.txt" "$DIST/MANIFEST.txt"
echo "$DIST/$PACK.tar.gz"
cat "$PACK.tar.gz.sha256"
tar -tzf "$PACK.tar.gz"
