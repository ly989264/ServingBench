#!/bin/bash
# Checks the whole chain on the monitoring machine: the ingest process in Prometheus, the documents in ClickHouse,
# every instance, every panel (scripts/verify_panel2.py). Reads clickhouse.env and grafana.env.
# Usage: bin/verify.sh [--minutes N] [--no-prometheus] [--all-orders]
. "$(dirname "$0")/lib.sh"
load_ch_env
load_gf_env
need_python
generate >/dev/null
"$PYTHON" -I "$PACK_DIR/scripts/verify_panel2.py" "$@"
