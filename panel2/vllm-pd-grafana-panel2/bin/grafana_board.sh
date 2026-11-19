#!/bin/bash
# Adds the ClickHouse data source and the dashboard to the existing Grafana. Does not touch other dashboards.
# The data source and the dashboard are each written only when they differ from what Grafana has (bin/compare.sh);
# VLLM_PD_FORCE=1 writes both anyway.
. "$(dirname "$0")/lib.sh"
load_env
need_python
names_check
generate >/dev/null
if [ "${VLLM_PD_FORCE:-}" = 1 ]; then
  echo "VLLM_PD_FORCE=1：不比对，照做：数据源、看板"
  exec "$PYTHON" "$PACK_DIR/scripts/grafana_api.py" install
fi
args=()
for item in datasource board; do
  case $item in datasource) label=数据源 ;; board) label=看板 ;; esac
  line=$("$PACK_DIR/bin/compare.sh" "$item"); rc=$?
  [ "$rc" -eq 0 ] || [ "$rc" -eq 10 ] || fail "比对${label}失败，看上面的提示"
  if [ "$rc" -eq 0 ]; then
    args+=("--skip-$item")
  else
    [ "${line%% *}" = WARN ] && echo "$line"
    what=${line#*：}
    args+=("--$item-change" "${what#有变化：}")
  fi
done
"$PYTHON" "$PACK_DIR/scripts/grafana_api.py" install "${args[@]}"
