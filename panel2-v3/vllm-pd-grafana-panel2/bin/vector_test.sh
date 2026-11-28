#!/bin/bash
# Generates the collector's config from segments.json and has Vector check it, without starting anything.
# Use it after changing segments.json, before bin/apply_segments.sh.
. "$(dirname "$0")/lib.sh"
load_env
need_python
WORK=$(mktemp -d)
trap 'find "$WORK" -mindepth 1 -delete; rmdir "$WORK"' EXIT
"$PYTHON" "$PACK_DIR/scripts/inventory_tool.py" "${INVENTORY:-$PACK_DIR/inventory.example.json}" csv > "$WORK/inventory.csv" 2>/dev/null \
  || "$PYTHON" "$PACK_DIR/scripts/inventory_tool.py" "$PACK_DIR/inventory.example.json" csv > "$WORK/inventory.csv"
CLICKHOUSE_ADDR=localhost CH_WRITER_PASSWORD=x MODEL_NAME=test "$PYTHON" "$PACK_DIR/scripts/render_vector.py" "$WORK/central.yaml" /tmp /etc/vector || exit 1
chmod -R a+rX "$WORK"
docker run --rm -v "$WORK:/etc/vector:ro" "$CENTRAL_IMAGE" validate --no-environment /etc/vector/central.yaml && echo "RESULT: OK"
