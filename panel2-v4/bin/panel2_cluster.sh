#!/bin/bash
# One cluster of 面板二 on this monitoring machine, in one command. ~/.vllm-pd-clusters.json lists the clusters installed
# here (scripts/names.py; shared with panel 1, which checks that no two clusters use the same name, port or folder).
#   bin/panel2_cluster.sh add <部署名> --model-group <组> [--port-base B] [--fetcher-url http://<proxy 机器>:<端口>]
#                             [--env-from <已装集群的目录>] [--root <总目录>] [--dir <安装目录>] [--set KEY=VALUE ...]
#                             [--no-ingest] [--force]
#       makes <总目录>/vllm-pd-grafana-panel2/<部署名> and writes ingest.json, clickhouse.env and grafana.env there, with
#       the container names, ports and UIDs written out (the deployment name and the port base are given here only);
#       then starts ClickHouse with the tables, installs the Grafana plugin, adds the ingest process's scrape job and
#       alert rules, starts the ingest process and, once it has the segment definitions, imports the board.
#       Run it again after a failed step or after editing one of the three files: every step can be run again, unchanged
#       steps are skipped (--force redoes them); the folder's scripts are refreshed from the pack the command runs from.
#       Without a fetcher address in ingest.json (or with --no-ingest) it stops before the ingest process and the board.
#   bin/panel2_cluster.sh remove <部署名> [--backup <目录>]   removes the cluster, its data and its folder
#   bin/panel2_cluster.sh list                              the registered clusters
#   bin/panel2_cluster.sh check <部署名> [--dir <目录>]      the upgrade-safety check, read-only (scripts/safety.py)
#   bin/panel2_cluster.sh quality <部署名>                   data accuracy (verify.sh, postdeploy_check.sh); never gates
. "$(dirname "$0")/lib.sh"
unset PYTHONHOME PYTHONPATH LD_PRELOAD LD_LIBRARY_PATH
need_python
NAMES=("$PYTHON" -I "$PACK_DIR/scripts/names.py")
FILES=("$PYTHON" -I "$PACK_DIR/scripts/cluster_files.py")

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }

# Runs a step with only the basic environment, so that nothing of this shell or of another cluster's files gets in.
clean() {
  env -i HOME="$HOME" PATH="$PATH" USER="${USER:-}" LOGNAME="${LOGNAME:-}" LANG="${LANG:-C.UTF-8}" TERM="${TERM:-dumb}" \
    ${TMPDIR:+TMPDIR="$TMPDIR"} ${VLLM_PD_REGISTRY:+VLLM_PD_REGISTRY="$VLLM_PD_REGISTRY"} ${VLLM_PD_FORCE:+VLLM_PD_FORCE="$VLLM_PD_FORCE"} \
    ${PYTHON:+PYTHON="$PYTHON"} "$@"
}
step() {
  # step <number> <what> <command...>: runs it in the cluster's folder; on failure names the manual step to look at.
  local n=$1 what=$2
  shift 2
  echo
  echo "==== 步骤 ${n}：$what"
  ( cd "$D" && clean "$@" ) || fail "步骤 ${n}（${what}）没过。按操作手册的「步骤 ${n}」一节处理，改好后重跑同一条命令；已经做完的步骤会再检查一遍，不会重复建。"
}
registered_dir() {
  "${NAMES[@]}" list --format json | "$PYTHON" -I -c 'import json,sys; e=json.load(sys.stdin)["clusters"].get(sys.argv[1]) or {}; print((e.get("panel2") or {}).get("dir") or "")' "$1"
}
copy_pack() {
  # This pack's files into $1, without the cluster's three files and what the steps generate; offline/ as hard links.
  local to=$1
  mkdir -p "$to" || fail "建不了目录 $to"
  ( cd "$PACK_DIR" && tar -cf - --exclude=./clickhouse.env --exclude=./grafana.env --exclude=./ingest.json --exclude=./generated \
      --exclude=./offline --exclude=./run --exclude=./logs --exclude='./deploy.env*' --exclude='*/__pycache__' . ) \
    | ( cd "$to" && tar -xf - ) || fail "拷不了部署包到 $to"
  if [ -d "$PACK_DIR/offline" ] && [ ! -e "$to/offline" ]; then
    cp -al "$PACK_DIR/offline" "$to/offline" 2>/dev/null || cp -a "$PACK_DIR/offline" "$to/offline" || fail "拷不了 offline 目录到 $to"
  fi
}
fetcher_url_filled() {
  "$PYTHON" -I -c 'import json,sys; u=json.load(open(sys.argv[1])).get("fetcher_url",""); sys.exit(0 if u.startswith("http://") and "<" not in u else 1)' "$D/ingest.json"
}

cmd_add() {
  local c=${1:-}; [ -n "$c" ] || usage; shift
  local group="" base="" env_from="" dir="" root="" url="" sets=() noingest=""
  while [ $# -gt 0 ]; do
    case $1 in
      --model-group) group=${2:-}; shift 2 ;;
      --port-base) base=${2:-}; shift 2 ;;
      --fetcher-url) url=${2:-}; shift 2 ;;
      --env-from) env_from=${2:-}; shift 2 ;;
      --dir) dir=${2:-}; shift 2 ;;
      --root) root=${2:-}; shift 2 ;;
      --set) sets+=(--set "${2:-}"); shift 2 ;;
      --no-ingest) noingest=1; shift ;;
      --force) export VLLM_PD_FORCE=1; shift ;;
      *) usage ;;
    esac
  done
  case $c in *[!a-z0-9-]*|-*) fail "部署名只用小写字母、数字和 -：$c" ;; esac
  local rootv; rootv=$("${NAMES[@]}" root ${root:+--root "$root"}) || exit 1
  if [ -n "$dir" ]; then D=$dir; else D=$("${NAMES[@]}" dir --panel panel2 --cluster "$c" --root "$rootv") || exit 1; fi
  mkdir -p "$D" || fail "建不了目录 $D"
  D=$(cd "$D" && pwd)
  local reg; reg=$(registered_dir "$c")
  [ -z "$reg" ] || [ "$reg" = "$D" ] || fail "集群 $c 已经登记在 ${reg}：在那个目录里重跑，或先 bin/panel2_cluster.sh remove $c"
  if [ -f "$D/clickhouse.env" ]; then
    local had; had=$(sed -n 's/^CLUSTER=//p' "$D/generated/registry.env" 2>/dev/null | head -n 1)
    [ -z "$had" ] || [ "$had" = "$c" ] || fail "$D 已经是集群「${had}」的目录：换一个 --dir"
    [ -n "$base" ] || base=$(sed -n 's/^PORT_BASE=//p' "$D/generated/registry.env" 2>/dev/null | head -n 1)
    [ -n "$group" ] || group=$(raw_value "$D/grafana.env" MODEL_GROUP "")
    [ "$D" = "$PACK_DIR" ] || copy_pack "$D"
    echo "沿用 $D 的 clickhouse.env、grafana.env、ingest.json（要改参数就改它们，再重跑这条命令）"
  else
    [ -n "$group" ] || usage
    [ -f "$D/deploy.env" ] && fail "$D 里是迁移前的 deploy.env：先执行 bin/migrate_deploy_env.sh split $D"
    [ -z "$(ls -A "$D" 2>/dev/null)" ] || [ "$D" = "$PACK_DIR" ] || fail "$D 不是空目录，也不是集群 $c 的目录：换一个 --dir"
    [ "$D" = "$PACK_DIR" ] || copy_pack "$D"
    if [ -n "$base" ]; then
      "${NAMES[@]}" alloc --cluster "$c" --port-base "$base" --root "$rootv" >/dev/null || exit 1
    else
      base=$("${NAMES[@]}" alloc --cluster "$c" --root "$rootv") || exit 1
    fi
  fi
  "${FILES[@]}" write --dir "$D" --cluster "$c" --model-group "$group" ${base:+--port-base "$base"} --root "$rootv" \
    ${env_from:+--env-from "$env_from"} ${url:+--fetcher-url "$url"} "${sets[@]+"${sets[@]}"}" || exit 1
  local todo; todo=$("${FILES[@]}" missing --dir "$D") || fail "还有要填写的项（改好后重跑这条命令）：
$todo"

  echo
  echo "==== 集群 ${c}：查注册表和端口"
  clean "${NAMES[@]}" check --panel panel2 --env-file "$D/generated/registry.env" --dir "$D" --no-ports \
    || fail "集群 $c 和这台机器上别的集群冲突，见上面的 FAIL 行。用 --set 改对应的项后重跑，什么都没有改动。"
  clean "${NAMES[@]}" register --panel panel2 --env-file "$D/generated/registry.env" --dir "$D" >/dev/null || fail "登记失败，见上面的输出"
  [ -z "$root" ] || "${NAMES[@]}" root --root "$rootv" --save >/dev/null || exit 1

  step 1 "导入 ClickHouse 镜像（已有就跳过）" bash -c '. bin/lib.sh; load_ch_env
    docker image inspect "$CLICKHOUSE_IMAGE" >/dev/null 2>&1 && { echo "已有镜像 $CLICKHOUSE_IMAGE"; exit 0; }
    [ -f offline/clickhouse-image.tar ] || fail "没有镜像 ${CLICKHOUSE_IMAGE}，offline 目录里也没有 clickhouse-image.tar"
    docker load -i offline/clickhouse-image.tar'
  step 2 "预检监控机" bin/check_cpu.sh
  step 3 "启动 ClickHouse，建账号和表" bin/clickhouse_up.sh
  step 4 "给 Grafana 装 ClickHouse 插件（已装就跳过）" bin/grafana_plugin.sh
  step 5 "让本集群的 Prometheus 抓取入库进程的状态端口，加 4 条告警规则" bin/prometheus_job.sh
  if [ -n "$noingest" ] || ! fetcher_url_filled; then
    echo
    if [ -n "$noingest" ]; then echo "==== 步骤 6、7：不做（--no-ingest）"; else echo "==== 步骤 6、7：不做（ingest.json 的 fetcher_url 还没有填写）"; fi
    echo "RESULT: OK（集群 $c 的 ClickHouse 侧已装好。采集进程装好后，执行 bin/panel2_cluster.sh add $c --fetcher-url http://<proxy 机器>:9300 启动入库进程、导入看板）"
    return 0
  fi
  step 6 "启动入库进程" bin/ingest.sh start
  step 7 "等入库进程取到段定义，生成并导入看板" bash -c '. bin/lib.sh; load_ch_env
    for _ in $(seq 1 45); do
      n=$(ch_sql "SELECT count() FROM vllm_pd.segment_def" 2>/dev/null) && [ "${n:-0}" -gt 0 ] && break
      sleep 2
    done
    [ "${n:-0}" -gt 0 ] || fail "90 秒内 segment_def 仍是空的：入库进程没有从采集进程取到段定义。执行 bin/ingest.sh status，看 logs/ingest.log"
    exec bin/grafana_board.sh'
  echo
  echo "RESULT: OK（集群 $c 装好了：看板在 Grafana 的「${group}」文件夹里）"
}

cmd_remove() {
  local c=${1:-}; [ -n "$c" ] || usage; shift
  local backup=()
  while [ $# -gt 0 ]; do
    case $1 in
      --backup) backup=(--backup "${2:-}"); shift 2 ;;
      *) usage ;;
    esac
  done
  D=$(registered_dir "$c")
  [ -n "$D" ] || fail "注册表里没有集群 $c 的面板二。bin/panel2_cluster.sh list 列出已登记的集群。"
  [ -f "$D/clickhouse.env" ] || fail "$D 里没有 clickhouse.env，没法知道要删什么（迁移前的目录用旧包的工具卸载）"
  local last=()
  [ -n "$("${NAMES[@]}" others --cluster "$c" --panel panel2)" ] || last=(--last)
  ( cd "$D" && clean bin/monitor_down.sh --purge "${backup[@]+"${backup[@]}"}" "${last[@]+"${last[@]}"}" ) || fail "卸载没过，见上面的输出。处理后重跑 bin/panel2_cluster.sh remove $c"
  "${NAMES[@]}" unregister --cluster "$c" --panel panel2
  case $D in
    "$HOME"|/|"") fail "不删目录 $D" ;;
  esac
  find "$D" -mindepth 1 -delete && rmdir "$D" && echo "已删除目录 $D"
  echo "RESULT: OK（集群 $c 的面板二已卸载）"
}

cmd_check() {
  # The upgrade-safety check, read-only. Runs this pack's code against the installed folder.
  local c=${1:-}; [ -n "$c" ] || usage; shift
  local dir=""
  while [ $# -gt 0 ]; do
    case $1 in
      --dir) dir=${2:-}; shift 2 ;;
      *) usage ;;
    esac
  done
  if [ -n "$dir" ]; then D=$(cd "$dir" 2>/dev/null && pwd) || fail "没有这个目录：$dir"; else D=$(registered_dir "$c"); fi
  [ -n "$D" ] || fail "注册表里没有集群 $c 的面板二"
  ( cd "$D" && clean "$PYTHON" -I "$PACK_DIR/scripts/safety.py" --cluster-dir "$D" )
}

cmd_quality() {
  # Data accuracy: the chain and the panels, then what has reached ClickHouse. Reports only; gates nothing.
  local c=${1:-}; [ -n "$c" ] || usage
  D=$(registered_dir "$c")
  [ -n "$D" ] || fail "注册表里没有集群 $c 的面板二"
  echo "==== 集群 ${c}：数据准确性检查（只报告，不影响升级和 check）"
  local rc=0
  echo; echo "==== 链路和看板（bin/verify.sh）"
  ( cd "$D" && clean bin/verify.sh ) || rc=1
  echo; echo "==== 部署后核验（bin/postdeploy_check.sh）"
  ( cd "$D" && clean bin/postdeploy_check.sh ) || rc=1
  [ $rc -eq 0 ] && echo "RESULT: OK（集群 $c 的数据检查）" || echo "RESULT: FAIL（集群 $c 的数据检查有项没过，见上面；不影响升级）"
  return $rc
}

case "${1:-}" in
  add) shift; cmd_add "$@" ;;
  remove) shift; cmd_remove "$@" ;;
  list) "${NAMES[@]}" list ;;
  check) shift; cmd_check "$@" ;;
  quality) shift; cmd_quality "$@" ;;
  *) usage ;;
esac
