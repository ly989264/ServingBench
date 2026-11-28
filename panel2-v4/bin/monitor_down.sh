#!/bin/bash
# Removes this cluster's 面板二 from the monitoring machine. Panel 1's dashboards and Prometheus jobs stay as they are.
#   bin/monitor_down.sh [--purge [--backup <目录>]] [--last]
# Default: stops the ingest process; removes the board, the data source, the scrape job and alert rules, and the
# ClickHouse container; keeps the data. The documents not yet read stay in the fetcher's output queue on the proxy host.
# --purge also deletes the ClickHouse data (--backup <目录> first saves it there as a tar.gz).
# --last also removes the Grafana plugin (Grafana restarts once): only when no other cluster on this machine uses it
# (bin/panel2_cluster.sh remove passes it after checking the registry).
. "$(dirname "$0")/lib.sh"
purge="" backup="" last=""
while [ $# -gt 0 ]; do
  case $1 in
    --purge) purge=1; shift ;;
    --backup) backup=${2:-}; shift 2 ;;
    --last) last=1; shift ;;
    *) fail "用法：bin/monitor_down.sh [--purge [--backup <目录>]] [--last]" ;;
  esac
done
[ -z "$backup" ] || [ -n "$purge" ] || fail "--backup 只和 --purge 一起用"
need_python
"$PACK_DIR/bin/ingest.sh" stop
( load_gf_env; "$PYTHON" -I "$PACK_DIR/scripts/grafana_api.py" uninstall ) || echo "Grafana 里的看板和数据源没有删掉，确认 Grafana 在运行后重试"
load_ch_env
if [ -f "${PROMETHEUS_YML:-}" ]; then
  "$PYTHON" -I "$PACK_DIR/scripts/prometheus_merge.py" remove --file "$PROMETHEUS_YML"
  docker exec -u root "$PROMETHEUS_CONTAINER" rm -f "$RULES_FILE" 2>/dev/null
  "$PYTHON" -I -c 'import sys,urllib.request as u; u.urlopen(u.Request(sys.argv[1],method="POST"),timeout=15)' "$PROMETHEUS_URL/-/reload" \
    || echo "Prometheus 没有重新加载，执行 docker restart $PROMETHEUS_CONTAINER"
fi
docker stop -t 60 "$CLICKHOUSE_CONTAINER" >/dev/null 2>&1
if [ -n "$purge" ] && [ -n "$backup" ]; then
  mkdir -p "$backup" || fail "建不了 $backup"
  out=$(cd "$backup" && pwd)/$CLICKHOUSE_CONTAINER-$(date +%Y%m%d-%H%M%S).tar.gz
  if [ -n "${CLICKHOUSE_DATA_DIR:-}" ]; then src=(-v "$CLICKHOUSE_DATA_DIR/data:/x:ro"); else src=(-v "$CLICKHOUSE_CONTAINER-data:/x:ro"); fi
  docker run --rm "${src[@]}" -v "$(dirname "$out"):/out" --entrypoint sh "$CLICKHOUSE_IMAGE" -c "tar -czf /out/$(basename "$out") -C /x ." \
    || fail "备份失败，什么都没有删；ClickHouse 容器已停止，执行 bin/clickhouse_up.sh 可以恢复"
  echo "已备份 ClickHouse 数据到 $out"
fi
docker rm -fv "$CLICKHOUSE_CONTAINER" >/dev/null 2>&1
if [ -n "$purge" ]; then
  if [ -n "${CLICKHOUSE_DATA_DIR:-}" ]; then
    # The files belong to the container's user, so remove them from a container. Only this pack's two folders.
    docker run --rm -v "$CLICKHOUSE_DATA_DIR:/x" --entrypoint sh "$CLICKHOUSE_IMAGE" -c 'rm -rf /x/data /x/logs' || echo "$CLICKHOUSE_DATA_DIR 下的 data、logs 没有删掉，手工删除"
  else
    docker volume rm "$CLICKHOUSE_CONTAINER-data" "$CLICKHOUSE_CONTAINER-logs" >/dev/null 2>&1
  fi
fi
if [ -n "$last" ]; then
  ( load_gf_env
    docker exec -u root "$GRAFANA_CONTAINER" rm -rf /var/lib/grafana/plugins/grafana-clickhouse-datasource 2>/dev/null
    docker restart "$GRAFANA_CONTAINER" >/dev/null 2>&1 )
  plugin="、Grafana 插件（Grafana 已重启）"
else
  plugin=""
fi
if [ -n "$purge" ]; then
  echo "已停止入库进程，删除看板、数据源、Prometheus 抓取任务和告警规则、ClickHouse 容器和数据$plugin"
else
  echo "已停止入库进程，删除看板、数据源、Prometheus 抓取任务和告警规则、ClickHouse 容器${plugin}。数据还在"
fi
echo "RESULT: OK"
