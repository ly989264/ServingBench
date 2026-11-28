#!/bin/bash
# In how many of the latest requests each segment of segment_def has a value (scripts/coverage.py), read-only.
# Reads grafana.env (the read-only account of ClickHouse).
# Usage: bin/coverage.sh [requests, default 200] [minutes, default 30]
. "$(dirname "$0")/lib.sh"
load_gf_env
need_python
exec "$PYTHON" -I "$PACK_DIR/scripts/coverage.py" "$@"
