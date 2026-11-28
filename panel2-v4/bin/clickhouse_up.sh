#!/bin/bash
# Starts this cluster's ClickHouse and creates what interface two needs: the two accounts (clickhouse/users.sql), the
# tables request_doc, status_doc, segment_def, the version table and the view request_json (pd_ingest.py init).
# Reads only clickhouse.env. Safe to run again: what exists is kept; the retention changes only when SUMMARY_TTL_DAYS does.
. "$(dirname "$0")/lib.sh"
load_ch_env
need_python
for k in CLICKHOUSE_CONTAINER CLICKHOUSE_IMAGE CLICKHOUSE_HTTP_PORT DOCKER_NETWORK CH_ADMIN_PASSWORD CH_WRITER_PASSWORD CH_READER_PASSWORD SUMMARY_TTL_DAYS; do
  [ -n "${!k:-}" ] || fail "clickhouse.env 缺 $k"
done
for k in CH_ADMIN_PASSWORD CH_WRITER_PASSWORD CH_READER_PASSWORD; do
  case ${!k} in *[!A-Za-z0-9]*) fail "clickhouse.env 的 $k 只能用字母和数字" ;; change-me-*) fail "clickhouse.env 的 $k 还是示例值，先填写" ;; esac
done
# The memory cap, always mounted so that the container never needs rebuilding for it: empty when the setting is blank.
mem_mb=${CLICKHOUSE_MAX_SERVER_MEMORY_MB:-}
case $mem_mb in ""|*[!0-9]*) [ -z "$mem_mb" ] || fail "CLICKHOUSE_MAX_SERVER_MEMORY_MB=$mem_mb 不是整数（MiB）" ;; esac
[ -z "$mem_mb" ] || [ "$mem_mb" -ge 1500 ] || fail "CLICKHOUSE_MAX_SERVER_MEMORY_MB=$mem_mb 太小，至少 1500（MiB）；留空表示不设上限"
docker network inspect "$DOCKER_NETWORK" >/dev/null 2>&1 || docker network create "$DOCKER_NETWORK" >/dev/null || fail "建不了网络 $DOCKER_NETWORK"
mkdir -p "$PACK_DIR/generated" || fail "建不了目录 $PACK_DIR/generated"
if [ -n "$mem_mb" ]; then
  printf '<clickhouse>\n    <max_server_memory_usage>%d</max_server_memory_usage>\n</clickhouse>\n' $((mem_mb * 1048576)) > "$PACK_DIR/generated/clickhouse_memory.xml"
else
  printf '<clickhouse>\n</clickhouse>\n' > "$PACK_DIR/generated/clickhouse_memory.xml"
fi
chmod 644 "$PACK_DIR/generated/clickhouse_memory.xml"
# A container that was made but never started (docker run failed, for example on CLICKHOUSE_DOCKER_ARGS) has no data yet:
# remake it, so that running this again picks up the corrected settings.
if [ "$(docker inspect --format '{{.State.Status}}' "$CLICKHOUSE_CONTAINER" 2>/dev/null)" = created ]; then
  docker rm -fv "$CLICKHOUSE_CONTAINER" >/dev/null || fail "删不掉没有启动过的容器 $CLICKHOUSE_CONTAINER"
fi
if docker ps -a --format '{{.Names}}' | grep -qx "$CLICKHOUSE_CONTAINER"; then
  docker start "$CLICKHOUSE_CONTAINER" >/dev/null || fail "容器 $CLICKHOUSE_CONTAINER 启动失败，看 docker logs $CLICKHOUSE_CONTAINER"
else
  mount=()
  if [ -n "${CLICKHOUSE_COMPAT_BINARY:-}" ]; then
    [ -f "$CLICKHOUSE_COMPAT_BINARY" ] || fail "CLICKHOUSE_COMPAT_BINARY 指向的文件不存在：$CLICKHOUSE_COMPAT_BINARY"
    chmod a+rx "$CLICKHOUSE_COMPAT_BINARY" 2>/dev/null
    mount=(-v "$CLICKHOUSE_COMPAT_BINARY:/usr/bin/clickhouse:ro")
  fi
  chmod -R a+rX "$PACK_DIR/clickhouse"
  if [ -n "${CLICKHOUSE_DATA_DIR:-}" ]; then
    reason=$(data_dir_check) || fail "$reason"
    vols=(-v "$CLICKHOUSE_DATA_DIR/data:/var/lib/clickhouse" -v "$CLICKHOUSE_DATA_DIR/logs:/var/log/clickhouse-server")
  else
    vols=(-v "$CLICKHOUSE_CONTAINER-data:/var/lib/clickhouse" -v "$CLICKHOUSE_CONTAINER-logs:/var/log/clickhouse-server")
  fi
  # shellcheck disable=SC2086
  docker run -d --name "$CLICKHOUSE_CONTAINER" --network "$DOCKER_NETWORK" --restart unless-stopped \
    --ulimit nofile=262144:262144 -p "$CLICKHOUSE_HTTP_PORT:8123" ${CLICKHOUSE_DOCKER_ARGS:-} \
    -e CLICKHOUSE_PASSWORD="$CH_ADMIN_PASSWORD" -e CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1 \
    "${vols[@]}" \
    -v "$PACK_DIR/clickhouse/config.d/vllm_pd.xml:/etc/clickhouse-server/config.d/vllm_pd.xml:ro" \
    -v "$PACK_DIR/generated/clickhouse_memory.xml:/etc/clickhouse-server/config.d/vllm_pd_memory.xml:ro" \
    "${mount[@]+"${mount[@]}"}" "$CLICKHOUSE_IMAGE" >/dev/null || fail "容器启动失败"
fi
ok=""
for _ in $(seq 1 60); do
  if ch_sql 'SELECT 1' >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
[ -n "$ok" ] || { docker logs --tail 15 "$CLICKHOUSE_CONTAINER" >&2; fail "ClickHouse 2 分钟内没有起来。退出码 132 或日志里有 Illegal instruction 时，回到 CPU 检查那一步。"; }
# The earliest version (tar 59604f30) used the same database with other tables; the two must not mix.
old=$(ch_sql "SELECT count() FROM system.tables WHERE database = 'vllm_pd' AND name = 'request_summary'") || fail "查不了 ClickHouse 里的表"
[ "$old" = "0" ] || fail "ClickHouse 里还有旧版（59604f30）的表 vllm_pd.request_summary：先用 bin/uninstall_old.sh plan 与 remove 卸载旧版，再执行这一步"
ch_sql 'CREATE DATABASE IF NOT EXISTS vllm_pd' || fail "建不了库 vllm_pd"
sed -e "s/{CH_WRITER_PASSWORD}/$CH_WRITER_PASSWORD/" -e "s/{CH_READER_PASSWORD}/$CH_READER_PASSWORD/" "$PACK_DIR/clickhouse/users.sql" | ch_sql \
  || fail "建账号失败"
# The ingest process connects over HTTP: to 127.0.0.1:CLICKHOUSE_HTTP_PORT when CLICKHOUSE_ADDR is the container name.
"$PYTHON" -I "$PACK_DIR/ingest/pd_ingest.py" init --dir "$PACK_DIR" || fail "建表没过，看上面的 FAIL 行"
echo "ClickHouse 版本: $(ch_sql 'SELECT version()')"
echo "表: $(ch_sql "SELECT arrayStringConcat(groupArray(name), ' ') FROM (SELECT name FROM system.tables WHERE database = 'vllm_pd' ORDER BY name)")"
echo "RESULT: OK"
