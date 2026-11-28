#!/bin/bash
# Installs the ClickHouse plugin into the existing Grafana from the offline folder, without internet.
# Usage: grafana_plugin.sh [zip]. Without a zip it picks the one that fits the Grafana container.
# Grafana restarts once. Its data volume and dashboards stay as they are. Does nothing when the plugin is already there.
. "$(dirname "$0")/lib.sh"
load_gf_env
need_python
docker ps --format '{{.Names}}' | grep -qx "$GRAFANA_CONTAINER" || fail "容器 $GRAFANA_CONTAINER 没在运行。检查 grafana.env 里的 GRAFANA_CONTAINER。"
if "$PYTHON" "$PACK_DIR/scripts/grafana_api.py" plugin-check 2>/dev/null; then
  echo "插件已经装好，没有重启 Grafana"
  exit 0
fi
zip=${1:-}
if [ -z "$zip" ]; then
  go=$(docker image inspect --format '{{.Architecture}}' "$(docker inspect --format '{{.Image}}' "$GRAFANA_CONTAINER")")
  zip=$(ls "$PACK_DIR"/offline/grafana-clickhouse-datasource-*.linux_"$go".zip 2>/dev/null | head -n 1)
  [ -n "$zip" ] || fail "offline 目录里没有 $go 的插件 zip。Grafana 容器是 ${go}，离线包要拿对应架构的那一份。"
fi
[ -f "$zip" ] || fail "没有这个文件：$zip"
docker cp "$zip" "$GRAFANA_CONTAINER:/tmp/clickhouse-plugin.zip" >/dev/null || fail "拷不进容器"
docker exec "$GRAFANA_CONTAINER" grafana cli --pluginUrl /tmp/clickhouse-plugin.zip plugins install grafana-clickhouse-datasource || fail "插件安装失败，看上面的输出"
docker exec -u root "$GRAFANA_CONTAINER" rm -f /tmp/clickhouse-plugin.zip
start=$(date +%s)
docker restart "$GRAFANA_CONTAINER" >/dev/null || fail "Grafana 重启失败"
ok=""
for _ in $(seq 1 60); do
  if "$PYTHON" "$PACK_DIR/scripts/grafana_api.py" plugin-check >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
[ -n "$ok" ] || fail "Grafana 2 分钟内没有恢复或没有加载插件，看 docker logs $GRAFANA_CONTAINER"
echo "Grafana 重启用了 $(( $(date +%s) - start )) 秒"
"$PYTHON" "$PACK_DIR/scripts/grafana_api.py" plugin-check
