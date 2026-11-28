#!/bin/bash
# Run on the monitoring machine after editing segments.json (or the retention in deploy.env). Adds columns, rebuilds the views,
# re-imports the dashboard, regenerates and restarts the collector, and pushes segments.json to the nodes (their filters reread it).
# No data migration: new columns read as empty for old requests.
# --drop-removed also drops the columns of segments that are no longer in segments.json (their data is gone for good).
. "$(dirname "$0")/lib.sh"
load_env
need_python
start=$(date +%s)
generate
ch_sql < "$PACK_DIR/generated/schema.sql" || fail "加列失败"
ch_sql < "$PACK_DIR/generated/views.sql" || fail "重建视图失败"
extra=$("$PYTHON" "$PACK_DIR/scripts/columns_diff.py" "$(ch_sql "SELECT arrayStringConcat(groupArray(concat(table, '.', name)), ',') FROM system.columns WHERE database = 'vllm_pd' AND table = 'phase_line' AND startsWith(type, 'Nullable(')")")
if [ -n "$extra" ]; then
  if [ "${1:-}" = "--drop-removed" ]; then
    for c in $extra; do
      ch_sql "ALTER TABLE vllm_pd.${c%%.*} DROP COLUMN IF EXISTS ${c#*.}" || fail "删列失败：$c"
      echo "已删除列 $c"
    done
  else
    echo "定义里没有、表里还留着的列（数据还在，要删就加 --drop-removed）：$extra"
  fi
fi
"$PYTHON" "$PACK_DIR/scripts/grafana_api.py" board-only || exit 1
"$PACK_DIR/bin/central_up.sh" || fail "采集器没有用新规则重启"
"$PACK_DIR/bin/nodes.sh" push || fail "有节点没有收到新的 segments.json，看上面哪一台；修好后执行 bin/nodes.sh push"
echo "用时 $(( $(date +%s) - start )) 秒；vLLM 不用重启（过滤器按文件修改时间重读映射）"
echo "RESULT: OK"
