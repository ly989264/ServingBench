#!/bin/bash
# Shared by the scripts in bin/. The cluster folder (the folder this pack copy sits in) holds three files, written by
# bin/panel2_cluster.sh add with the concrete names, ports and UIDs; nothing is derived from the deployment name at run time:
#   clickhouse.env  the ClickHouse side: container, ports, passwords, retention, the ingest process's status port, alerts
#   ingest.json     the ingest process: the fetcher's address (interface one)
#   grafana.env     the panel side: Grafana, the data source and the board, and the read-only access to ClickHouse
set -u
PACK_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CH_ENV_FILE=$PACK_DIR/clickhouse.env
GF_ENV_FILE=$PACK_DIR/grafana.env
INGEST_JSON=$PACK_DIR/ingest.json
export CLICKHOUSE_ENV=$CH_ENV_FILE GRAFANA_ENV=$GF_ENV_FILE

fail() { echo "错误：$*" >&2; exit 1; }

source_env() {
  # set -a; . <file>; with the variables a vLLM shell may carry taken out, so they never reach this pack's Python.
  [ -f "$1" ] || fail "没有找到 $1。先执行 bin/panel2_cluster.sh add <部署名>（已装的旧集群用 bin/migrate_deploy_env.sh）。"
  # shellcheck disable=SC1090
  set -a; . "$1"; set +a
  unset PYTHONHOME PYTHONPATH LD_PRELOAD LD_LIBRARY_PATH
}
load_ch_env() { source_env "$CH_ENV_FILE"; }
load_gf_env() { source_env "$GF_ENV_FILE"; }
raw_value() {
  # The value of $2 in file $1 as written, before the shell expands it; $3 when it is not set.
  local v
  v=$(grep -E "^$2=" "$1" 2>/dev/null | tail -n 1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")
  echo "${v:-${3:-}}"
}

machine_arch() {
  case $(uname -m) in
    aarch64|arm64) echo aarch64 ;;
    x86_64|amd64) echo x86_64 ;;
    *) fail "不支持的架构 $(uname -m)，只有 aarch64 和 x86_64 的离线文件。" ;;
  esac
}

python_usable() {
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null
}

need_python() {
  # In this order: PYTHON from the environment, any Python 3.9+ on PATH, the one in the offline folder (unpacked there).
  local c tarball home=$PACK_DIR/offline
  for c in "${PYTHON:-}" python3 python3.13 python3.12 python3.11 python3.10 python3.9 python; do
    if [ -n "$c" ] && python_usable "$c"; then PYTHON=$c; export PYTHON; return 0; fi
  done
  if ! python_usable "$home/python/bin/python3"; then
    tarball=$(ls "$PACK_DIR"/offline/python-*-"$(machine_arch)"-*.tar.gz 2>/dev/null | head -n 1)
    [ -n "$tarball" ] || fail "这台机器没有 Python 3.9 及以上，offline 目录里也没有自带的 Python。把离线包解开后重新做一次。"
    tar -xzf "$tarball" -C "$home" || fail "解不开 $tarball"
    python_usable "$home/python/bin/python3" || fail "自带的 Python 运行不了（glibc 太旧？）。这台机器要自己装一个 Python 3.9 及以上，执行前 export PYTHON=<它的路径>。"
  fi
  PYTHON=$home/python/bin/python3
  export PYTHON
}

# ---- preflight output: one line per item, a summary at the end ----
CHECK_TOTAL=0
CHECK_FAILS=0
pass() { CHECK_TOTAL=$((CHECK_TOTAL + 1)); echo "OK   $*"; }
miss() { CHECK_TOTAL=$((CHECK_TOTAL + 1)); CHECK_FAILS=$((CHECK_FAILS + 1)); echo "FAIL $*"; }
check_summary() {
  if [ "$CHECK_FAILS" -eq 0 ]; then echo "RESULT: OK（$CHECK_TOTAL 项全过）"; return 0; fi
  echo "RESULT: FAIL（$CHECK_TOTAL 项里 $CHECK_FAILS 项没过）"
  return 1
}
free_gb() {
  # Free space in GB on the disk that holds $1, or on the closest parent that exists.
  local d=$1
  while [ ! -e "$d" ] && [ "$d" != "/" ]; do d=$(dirname "$d"); done
  df -Pk "$d" | awk 'NR == 2 { printf "%d", $4 / 1048576 }'
}
check_page_size() {
  local ps
  ps=$(getconf PAGESIZE 2>/dev/null || echo unknown)
  if [ "$ps" = 4096 ]; then pass "页大小 4096"; else miss "页大小 $ps → 测试只覆盖了 4096，这台机器上能不能跑没有验证过。"; fi
}
check_python() {
  pass "Python $("$PYTHON" -c 'import platform; print(platform.python_version())') $(command -v "$PYTHON")"
}

data_dir_check() {
  # Makes sure ClickHouse (uid 101 in the image) can write under CLICKHOUSE_DATA_DIR. Prints the reason when it cannot.
  local dir=$CLICKHOUSE_DATA_DIR
  case $dir in
    /*) ;;
    *) echo "CLICKHOUSE_DATA_DIR=$dir 不是绝对路径 → 写成 /开头的路径。"; return 1 ;;
  esac
  mkdir -p "$dir/data" "$dir/logs" 2>/dev/null || { echo "建不了 $dir/data（只读盘或没有权限）→ 换一个目录，或用 root 执行 mkdir -p ${dir}。"; return 1; }
  docker run --rm -v "$dir/data:/x" --entrypoint sh "$CLICKHOUSE_IMAGE" -c 'mkdir -p /x/.w && chown 101:101 /x /x/.w && su -s /bin/sh clickhouse -c "touch /x/.w/t" && rm -rf /x/.w' >/dev/null 2>&1 \
    || { echo "$dir 容器里写不了（只读、NFS root_squash 或权限不对）→ 换本地盘上的目录，或执行 chown -R 101:101 ${dir}。"; return 1; }
}

ch_sql() {
  # Runs SQL from stdin or $1 as the ClickHouse admin, inside the container.
  if [ $# -gt 0 ]; then
    docker exec "$CLICKHOUSE_CONTAINER" clickhouse-client --password "$CH_ADMIN_PASSWORD" -q "$1"
  else
    docker exec -i "$CLICKHOUSE_CONTAINER" clickhouse-client --password "$CH_ADMIN_PASSWORD" --multiquery
  fi
}

generate() {
  # The board from segment_def (scripts/generate.py checks the version table first; on a mismatch nothing is written).
  "$PYTHON" "$PACK_DIR/scripts/generate.py" --env "$GF_ENV_FILE" || fail "看板没有生成，看上面的提示"
}

running_pid() {
  # Prints the pid in file $1 if that process is alive.
  [ -f "$1" ] || return 1
  local p; p=$(cat "$1")
  kill -0 "$p" 2>/dev/null && echo "$p"
}
