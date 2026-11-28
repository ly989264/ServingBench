#!/bin/bash
# Post-deployment check on the monitoring machine: whether the status and request documents in ClickHouse look the way
# this pack expects (scripts/postdeploy.py). Run it after the cluster has served requests for a few minutes.
# Reads grafana.env (the read-only account of ClickHouse). Usage: bin/postdeploy_check.sh [--minutes N]
. "$(dirname "$0")/lib.sh"
load_gf_env
need_python
exec "$PYTHON" -I "$PACK_DIR/scripts/postdeploy.py" "$@"
