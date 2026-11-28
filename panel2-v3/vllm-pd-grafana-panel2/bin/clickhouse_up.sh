#!/bin/bash
# Starts ClickHouse, creates tables, views and users. Safe to run again.
. "$(dirname "$0")/lib.sh"
load_env
need_python
names_check
generate >/dev/null
docker network inspect "$DOCKER_NETWORK" >/dev/null 2>&1 || docker network create "$DOCKER_NETWORK" >/dev/null || fail "建不了网络 $DOCKER_NETWORK"
# The memory cap, always mounted so that the container never needs rebuilding for it: empty when the setting is blank.
mem_mb=${CLICKHOUSE_MAX_SERVER_MEMORY_MB:-}
case $mem_mb in ""|*[!0-9]*) [ -z "$mem_mb" ] || fail "CLICKHOUSE_MAX_SERVER_MEMORY_MB=$mem_mb 不是整数（MiB）" ;; esac
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
# The earlier version (tar 59604f30) used the same database with other tables; the two must not mix.
old=$(ch_sql "SELECT count() FROM system.tables WHERE database = 'vllm_pd' AND name = 'request_summary'") || fail "查不了 ClickHouse 里的表"
[ "$old" = "0" ] || fail "ClickHouse 里还有旧版（59604f30）的表 vllm_pd.request_summary：先按教程「卸载旧版（59604f30）」卸载，U3 删数据要做（要留旧数据就加 --backup 打包），再执行这一步"
if [ "${VLLM_PD_FORCE:-}" = 1 ]; then
  echo "VLLM_PD_FORCE=1：不比对，照做：表和视图"
  ch_sql < "$PACK_DIR/generated/schema.sql" || fail "建表失败"
  ch_sql < "$PACK_DIR/generated/views.sql" || fail "建视图失败"
else
  # Only the statements that would change something (bin/compare.sh ddl): no MODIFY TTL whose TTL is the live one.
  ddl="$PACK_DIR/generated/compare/ddl.sql"
  line=$("$PACK_DIR/bin/compare.sh" ddl --sql-out "$ddl"); rc=$?
  [ "$rc" -eq 0 ] || [ "$rc" -eq 10 ] || fail "比对表和视图失败，看上面的提示"
  if [ "$rc" -eq 0 ]; then
    echo "跳过（内容不变）：表和视图"
  else
    what=${line#*：}; what=${what#有变化：}
    [ "${line%% *}" = WARN ] && echo "$line"
    if [ ! -s "$ddl" ] && [ "${line%% *}" = WARN ]; then
      # The comparison stopped before it knew any statement: do what v2.0 does.
      ch_sql < "$PACK_DIR/generated/schema.sql" || fail "建表失败"
      ch_sql < "$PACK_DIR/generated/views.sql" || fail "建视图失败"
      echo "已更新：表和视图：$what，照 v2.0 的做法全部执行"
    elif [ ! -s "$ddl" ]; then
      echo "表和视图：$what；没有要执行的 DDL"
    else
      for t in $(sed -n 's/^ALTER TABLE \([^ ]*\) MODIFY TTL .*/\1/p' "$ddl"); do
        echo "会对 $t 起一次全表的 TTL 重算"
      done
      ch_sql < "$ddl" || fail "执行表和视图的 DDL 失败（语句在 $ddl）"
      echo "已更新：表和视图：$what"
    fi
  fi
fi
ch_sql <<SQL || fail "建用户失败"
CREATE SETTINGS PROFILE IF NOT EXISTS vllm_reader_profile SETTINGS max_execution_time = 120, max_memory_usage = 6000000000;
ALTER SETTINGS PROFILE vllm_reader_profile SETTINGS max_execution_time = 120, max_memory_usage = 6000000000, max_bytes_before_external_group_by = 2000000000, join_algorithm = 'auto', max_bytes_in_join = 1000000000;
CREATE USER IF NOT EXISTS vllm_writer;
ALTER USER vllm_writer IDENTIFIED WITH sha256_password BY '$CH_WRITER_PASSWORD';
GRANT INSERT ON vllm_pd.* TO vllm_writer;
CREATE USER IF NOT EXISTS vllm_reader SETTINGS PROFILE 'vllm_reader_profile';
ALTER USER vllm_reader IDENTIFIED WITH sha256_password BY '$CH_READER_PASSWORD';
GRANT SELECT ON vllm_pd.* TO vllm_reader;
SQL
echo "ClickHouse 版本: $(ch_sql 'SELECT version()')"
echo "表: $(ch_sql "SELECT arrayStringConcat(groupArray(name), ' ') FROM (SELECT name FROM system.tables WHERE database = 'vllm_pd' ORDER BY name)")"
echo "RESULT: OK"
