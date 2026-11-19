#!/bin/bash
# Post-deployment check on the monitoring machine: whether the nodes' logs reach ClickHouse the way this pack expects.
# Run it after the nodes have served requests for a few minutes. Usage: bin/postdeploy_check.sh [--minutes N]
. "$(dirname "$0")/lib.sh"
load_env
need_python
need_inventory
exec "$PYTHON" "$PACK_DIR/scripts/postdeploy.py" "$@"
