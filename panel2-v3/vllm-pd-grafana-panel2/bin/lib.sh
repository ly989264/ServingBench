#!/bin/bash
# Shared by the scripts in bin/. Loads deploy.env from the pack root.
set -u
PACK_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
INVENTORY="$PACK_DIR/inventory.json"

fail() { echo "错误：$*" >&2; exit 1; }

raw_env() {
  # The value of $1 in deploy.env as written, before the shell expands it; $2 when it is not set.
  local v
  v=$(grep -E "^$1=" "$PACK_DIR/deploy.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")
  echo "${v:-$2}"
}
load_env() {
  [ -f "$PACK_DIR/deploy.env" ] || fail "没有找到 deploy.env。先执行 cp deploy.env.example deploy.env，再按说明改好。"
  # shellcheck disable=SC1091
  set -a; . "$PACK_DIR/deploy.env"; set +a
  # PYTHON as deploy.env writes it, before need_python fills it in for this machine: the nodes get this one (bin/nodes.sh).
  DEPLOY_PYTHON=${PYTHON:-}
  # A vLLM shell may carry these. They must not reach this pack's Python or Vector.
  unset PYTHONHOME PYTHONPATH LD_PRELOAD LD_LIBRARY_PATH
  if [ -f "$PACK_DIR/scripts/names.py" ]; then
    # The monitoring machine: names, ports, folders and UIDs left blank follow CLUSTER and PORT_BASE (scripts/names.py).
    # The nodes get only bin/lib.sh and a short deploy.env, so this part does not run there.
    local derived
    need_python
    derived=$("$PYTHON" "$PACK_DIR/scripts/names.py" derive --panel panel2 --format shell) || fail "deploy.env 的集群设置有问题，看上面的提示"
    eval "$derived"
  fi
  TIMING_HOME=${TIMING_HOME:-$HOME/vllm-timing}
  LOG_DIR=$TIMING_HOME/logs
  # Node folders as written in deploy.env: $HOME there means the SSH user's home on the node, so it is kept unexpanded for the nodes.
  NODE_CONF_DIR_RAW=$(raw_env NODE_CONF_DIR '$HOME/vllm-pd-diag/conf')
  NODE_LOG_DIR_RAW=$(raw_env NODE_LOG_DIR '$HOME/vllm-pd-diag/logs')
  export NODE_CONF_DIR_RAW NODE_LOG_DIR_RAW
  # v2.1 (design 2.8): in vllm mode the container sees the node folders at the same paths unless deploy.env says
  # otherwise; in mock mode the container paths are not used, and segments.json keeps the pack's log_dir.
  # A host path written with $HOME is not a path inside a container: then the pack's values stay.
  if [ "${NODE_MODE:-mock}" = vllm ]; then
    case $NODE_CONF_DIR_RAW in /*) NODE_CONF_DIR_IN_CONTAINER=${NODE_CONF_DIR_IN_CONTAINER:-$NODE_CONF_DIR_RAW} ;; esac
    case $NODE_LOG_DIR_RAW in /*) NODE_LOG_DIR_IN_CONTAINER=${NODE_LOG_DIR_IN_CONTAINER:-$NODE_LOG_DIR_RAW} ;; esac
    NODE_CONF_DIR_IN_CONTAINER=${NODE_CONF_DIR_IN_CONTAINER:-/opt/vllm-pd-diag}
    NODE_LOG_DIR_IN_CONTAINER=${NODE_LOG_DIR_IN_CONTAINER:-}
  else
    NODE_CONF_DIR_IN_CONTAINER=${NODE_CONF_DIR_IN_CONTAINER:-/opt/vllm-pd-diag}
    NODE_LOG_DIR_IN_CONTAINER=${NODE_LOG_DIR_IN_CONTAINER:-}
  fi
  # vLLM container names and stdout files per machine (ADDR=NAME[;NAME],...); blank: bin/diag.sh and selfcheck find them.
  NODE_VLLM_CONTAINERS=${NODE_VLLM_CONTAINERS:-}
  NODE_VLLM_STDOUT_FILES=${NODE_VLLM_STDOUT_FILES:-}
  export NODE_CONF_DIR_IN_CONTAINER NODE_LOG_DIR_IN_CONTAINER NODE_VLLM_CONTAINERS NODE_VLLM_STDOUT_FILES
  PULL_DIR=${PULL_DIR:-${CLICKHOUSE_DATA_DIR:-$TIMING_HOME}/pull}
  export PULL_DIR
  export TIMING_HOME LOG_DIR
}

names_check() {
  # Before a step makes a container, takes a port or writes to Grafana: stops when another cluster on this machine
  # registered one of this cluster's names, ports, folders or machines, or when something else holds one of its ports.
  local inv=()
  [ -f "$INVENTORY" ] && inv=(--inventory "$INVENTORY")
  "$PYTHON" "$PACK_DIR/scripts/names.py" check --panel panel2 --dir "$PACK_DIR" "${inv[@]+"${inv[@]}"}" "$@" \
    || fail "集群 $CLUSTER 和这台机器上别的集群冲突，见上面的 FAIL 行。改 deploy.env 里对应的项（留空就按集群名派生），什么都没有改动。"
}

machine_arch() {
  case $(uname -m) in
    aarch64|arm64) echo aarch64 ;;
    x86_64|amd64) echo x86_64 ;;
    *) fail "不支持的架构 $(uname -m)，只有 aarch64 和 x86_64 的离线文件。" ;;
  esac
}

need_inventory() {
  [ -f "$INVENTORY" ] || fail "没有找到 inventory.json。先执行 cp inventory.example.json inventory.json，再改成你的地址。"
  "$PYTHON" "$PACK_DIR/scripts/inventory_tool.py" "$INVENTORY" summary >/dev/null || exit 1
}

python_usable() {
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null
}

need_python() {
  # In this order: PYTHON from deploy.env, any Python 3.9+ on PATH, the one in the offline folder.
  # The offline one is unpacked into TIMING_HOME; on the monitoring machine, before TIMING_HOME is derived, into offline/.
  local c tarball home=${TIMING_HOME:-$PACK_DIR/offline}
  for c in "${PYTHON:-}" python3 python3.13 python3.12 python3.11 python3.10 python3.9 python; do
    if [ -n "$c" ] && python_usable "$c"; then PYTHON=$c; export PYTHON; return 0; fi
  done
  if ! python_usable "$home/python/bin/python3"; then
    tarball=$(ls "$PACK_DIR"/offline/python-*-"$(machine_arch)"-*.tar.gz 2>/dev/null | head -n 1)
    [ -n "$tarball" ] || fail "这台机器没有 Python 3.9 及以上，offline 目录里也没有自带的 Python。把离线包解开后重新做一次。"
    mkdir -p "$home" || fail "建不了目录 $home。改 deploy.env 里的 TIMING_HOME。"
    tar -xzf "$tarball" -C "$home" || fail "解不开 $tarball"
    [ "$home" = "${TIMING_HOME:-}" ] && touch "$home/.vllm-timing-home"
    python_usable "$home/python/bin/python3" || fail "自带的 Python 运行不了（glibc 太旧？）。这台机器要自己装一个 Python 3.9 及以上，路径写进 deploy.env 的 PYTHON。"
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
  mkdir -p "$dir/data" "$dir/logs" 2>/dev/null || { echo "建不了 $dir/data（只读盘或没有权限）→ 换一个目录，或用 root 执行 mkdir -p $dir。"; return 1; }
  docker run --rm -v "$dir/data:/x" --entrypoint sh "$CLICKHOUSE_IMAGE" -c 'mkdir -p /x/.w && chown 101:101 /x /x/.w && su -s /bin/sh clickhouse -c "touch /x/.w/t" && rm -rf /x/.w' >/dev/null 2>&1 \
    || { echo "$dir 容器里写不了（只读、NFS root_squash 或权限不对）→ 换本地盘上的目录，或执行 chown -R 101:101 $dir。"; return 1; }
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
  "$PYTHON" "$PACK_DIR/scripts/generate.py" || fail "segments.json 有问题，看上面的提示"
}

make_pull_dir() {
  # A marker only if this pack made the folder, so that uninstalling never empties a folder someone else filled.
  case $PULL_DIR in
    /*) ;;
    *) fail "PULL_DIR=$PULL_DIR 不是绝对路径。改 deploy.env。" ;;
  esac
  if [ ! -d "$PULL_DIR" ] || [ -z "$(ls -A "$PULL_DIR" 2>/dev/null)" ]; then
    mkdir -p "$PULL_DIR" && touch "$PULL_DIR/.made-by-vllm-timing" || fail "建不了 $PULL_DIR。改 deploy.env 的 PULL_DIR。"
  fi
}

running_pid() {
  # Prints the pid in file $1 if that process is alive.
  [ -f "$1" ] || return 1
  local p; p=$(cat "$1")
  kill -0 "$p" 2>/dev/null && echo "$p"
}
