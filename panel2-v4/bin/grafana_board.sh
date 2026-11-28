#!/bin/bash
# Generates the board from segment_def (the version table is checked first: on a mismatch nothing is generated and the
# board in Grafana stays as it is) and adds the ClickHouse data source and the board to the existing Grafana. Other
# dashboards are not touched. Reads only grafana.env.
. "$(dirname "$0")/lib.sh"
load_gf_env
need_python
generate
exec "$PYTHON" -I "$PACK_DIR/scripts/grafana_api.py" install
