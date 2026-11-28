#!/bin/bash
# Removes 面板二 from the monitoring machine. Panel 1 dashboards stay as they are.
# Default: removes the dashboard, data source, Prometheus jobs, the collector and the ClickHouse container, keeps the data.
# --purge also deletes the ClickHouse data; the Grafana plugin too (Grafana restarts once), but only when no other cluster
# on this machine is registered (scripts/names.py): the plugin and Grafana are shared.
. "$(dirname "$0")/lib.sh"
load_env
need_python
"$PACK_DIR/bin/puller.sh" stop
"$PYTHON" "$PACK_DIR/scripts/grafana_api.py" uninstall || echo "Grafana 里的看板和数据源没有删掉，确认 Grafana 在运行后重试"
if [ -f "$PROMETHEUS_YML" ]; then
  "$PYTHON" "$PACK_DIR/scripts/prometheus_merge.py" remove --file "$PROMETHEUS_YML"
  docker exec -u root "$PROMETHEUS_CONTAINER" rm -f "/prometheus/$RULES_FILE" 2>/dev/null
  "$PYTHON" -c 'import sys,urllib.request as u; u.urlopen(u.Request(sys.argv[1],method="POST"),timeout=15)' "$PROMETHEUS_URL/-/reload" || echo "Prometheus 没有重新加载，执行 docker restart $PROMETHEUS_CONTAINER"
fi
# The collector first, stopped so that it finishes what it holds (see bin/central_up.sh).
docker stop -t 60 "$CENTRAL_CONTAINER" >/dev/null 2>&1
docker rm -f "$CENTRAL_CONTAINER" "$CLICKHOUSE_CONTAINER" >/dev/null 2>&1
if [ "${1:-}" = "--purge" ]; then
  if [ -n "${CLICKHOUSE_DATA_DIR:-}" ]; then
    # The files belong to the container's user, so remove them from a container. Only this pack's two folders.
    docker run --rm -v "$CLICKHOUSE_DATA_DIR:/x" --entrypoint sh "$CLICKHOUSE_IMAGE" -c 'rm -rf /x/data /x/logs /x/vector' || echo "$CLICKHOUSE_DATA_DIR 下的 data、logs、vector 没有删掉，手工删除"
  else
    docker volume rm "$CLICKHOUSE_CONTAINER-data" "$CLICKHOUSE_CONTAINER-logs" "$CENTRAL_CONTAINER-data" >/dev/null 2>&1
  fi
  others=$("$PYTHON" "$PACK_DIR/scripts/names.py" others --cluster "$CLUSTER" --panel panel2 | tr '\n' ' ')
  if [ -z "${others// /}" ]; then
    docker exec -u root "$GRAFANA_CONTAINER" rm -rf /var/lib/grafana/plugins/grafana-clickhouse-datasource 2>/dev/null
  fi
  # The mirror and the puller's state. Only what this pack made.
  if [ -f "$PULL_DIR/.made-by-vllm-timing" ]; then
    find "$PULL_DIR" -mindepth 1 -delete && rmdir "$PULL_DIR"
  elif [ -d "$PULL_DIR" ]; then
    echo "镜像目录 $PULL_DIR 不是本包建的，没有删除"
  fi
  [ -d "$TIMING_HOME/puller" ] && find "$TIMING_HOME/puller" -mindepth 1 -delete && rmdir "$TIMING_HOME/puller"
  if [ -z "${others// /}" ]; then
    docker restart "$GRAFANA_CONTAINER" >/dev/null 2>&1
    echo "已删除看板、数据源、Prometheus 配置、采集器和 ClickHouse 的容器和数据、Grafana 插件"
  else
    echo "已删除看板、数据源、Prometheus 配置、采集器和 ClickHouse 的容器和数据。Grafana 插件保留：集群 ${others% } 还在用，Grafana 没有重启"
  fi
else
  echo "已删除看板、数据源、Prometheus 配置、采集器和 ClickHouse 容器。数据还在"
fi
