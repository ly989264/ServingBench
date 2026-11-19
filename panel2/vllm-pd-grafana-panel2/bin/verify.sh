#!/bin/bash
# Checks the whole chain on the monitoring machine: Prometheus targets, ClickHouse rows, every instance, every panel.
. "$(dirname "$0")/lib.sh"
load_env
need_python
need_inventory
generate >/dev/null
"$PYTHON" "$PACK_DIR/scripts/verify_panel2.py" "$@"
