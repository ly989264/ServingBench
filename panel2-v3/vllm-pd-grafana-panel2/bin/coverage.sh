#!/bin/bash
# v3.1-extra: in how many of the latest requests each segment has a value (scripts/coverage.py), read-only.
# Usage: bin/coverage.sh [requests, default 200] [minutes, default 30]
. "$(dirname "$0")/lib.sh"
load_env
need_python
export CLICKHOUSE_CONTAINER CH_ADMIN_PASSWORD
exec "$PYTHON" "$PACK_DIR/scripts/coverage.py" "$@"
