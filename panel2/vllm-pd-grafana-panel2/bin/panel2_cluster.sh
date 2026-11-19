#!/bin/bash
# One cluster of 面板二 on this monitoring machine, in one command. Names, ports and folders follow scripts/names.py;
# ~/.vllm-pd-clusters.json lists the clusters installed here.
#   bin/panel2_cluster.sh add <集群> --model-group <组> --inventory <清单> [--port-base B] [--mode mock|vllm]
#                             [--env-from <已装集群的 deploy.env 或目录>] [--root <总目录>] [--dir <安装目录>] [--set KEY=VALUE ...]
#                             [--force] [--ignore-check] [--no-safety-check]
#       makes <总目录>/vllm-pd-grafana-panel2/<集群>, writes its deploy.env and runs steps 3–18 of the tutorial there.
#       Steps whose content is unchanged are skipped (Prometheus not reloaded, collector not rebuilt, board not
#       re-imported); --force redoes them. Mock mode ends with the upgrade-safety check; --ignore-check reports its FAILs
#       without stopping; --no-safety-check (the update script) leaves it out.
#       <总目录>: --root (also kept in the registry for later adds), else the registry's, else $HOME. --dir wins over it.
#       Run it again after a failed step or after editing that deploy.env: every step can be run again; the folder's scripts
#       are refreshed from the pack the command runs from, deploy.env and inventory.json are kept.
#   bin/panel2_cluster.sh remove <集群> [--backup <目录>]   removes the cluster and its data, then its folder (uninstall_old.sh)
#   bin/panel2_cluster.sh list                              the registered clusters
#   bin/panel2_cluster.sh check <集群> [--dir <目录>] [--pre-upgrade] [--wait 秒]   the upgrade-safety check, read-only
#   bin/panel2_cluster.sh quality <集群>                   data accuracy (verify.sh, postdeploy_check.sh); never gates
#   bin/panel2_cluster.sh adopt <目录> --cluster <集群> --model-group <组>
#       registers a deployment made before clusters: writes CLUSTER, MODEL_GROUP and its current names into its deploy.env,
#       brings its scripts to this version (old ones kept in pack-before-adopt.tar.gz). Its containers are not touched.
. "$(dirname "$0")/lib.sh"
unset PYTHONHOME PYTHONPATH LD_PRELOAD LD_LIBRARY_PATH TIMING_HOME
need_python
NAMES=("$PYTHON" "$PACK_DIR/scripts/names.py")

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }

# Runs a step with only the basic environment, so that nothing of this shell or of another cluster's deploy.env gets in.
clean() {
  env -i HOME="$HOME" PATH="$PATH" USER="${USER:-}" LOGNAME="${LOGNAME:-}" LANG="${LANG:-C.UTF-8}" TERM="${TERM:-dumb}" \
    ${SSH_AUTH_SOCK:+SSH_AUTH_SOCK="$SSH_AUTH_SOCK"} ${TMPDIR:+TMPDIR="$TMPDIR"} ${VLLM_PD_REGISTRY:+VLLM_PD_REGISTRY="$VLLM_PD_REGISTRY"} \
    ${VLLM_PD_FORCE:+VLLM_PD_FORCE="$VLLM_PD_FORCE"} "$@"
}
step() {
  # step <number> <what> <command...>: runs it in the cluster's folder; on failure names the tutorial step to look at.
  local n=$1 what=$2
  shift 2
  echo
  echo "==== 步骤 $n：$what"
  # Tests only (T-7): VLLM_PD_TEST_FAIL_STEP=<n> makes step n fail at its start, as if it had gone wrong. Unset: no effect.
  if [ "${VLLM_PD_TEST_FAIL_STEP:-}" = "$n" ]; then
    fail "步骤 $n（$what）没过（测试用的 VLLM_PD_TEST_FAIL_STEP=$n，这一步没有执行）。按 README 的「步骤 $n」一节处理，改好后重跑同一条命令"
  fi
  ( cd "$D" && clean "$@" ) || fail "步骤 $n（$what）没过。按 README 的「步骤 $n」一节处理，改好后重跑同一条命令；已经做完的步骤会再检查一遍，不会重复建。"
}
in_cluster() {
  # Runs shell code with the cluster's deploy.env loaded (bin/lib.sh), in a clean environment.
  ( cd "$D" && clean bash -c '. bin/lib.sh; load_env; '"$1" )
}
registered_dir() {
  "${NAMES[@]}" list --format json | "$PYTHON" -c 'import json,sys; e=json.load(sys.stdin)["clusters"].get(sys.argv[1]) or {}; print((e.get("panel2") or {}).get("dir") or "")' "$1"
}
set_env() {
  # set_env <deploy.env> KEY=VALUE...: replaces each KEY's line, or adds it at the end. deploy.env is sourced by bash, so
  # every value that is not plain is written in single quotes (a quote inside is escaped): a password with $ stays as it is.
  # Only $HOME/<plain path> is left unquoted, so that it is expanded where the file is read (the nodes get TIMING_HOME so).
  "$PYTHON" - "$@" <<'PY' || fail "改不了 $1"
import re, shlex, sys
path, pairs = sys.argv[1], sys.argv[2:]
lines = open(path, encoding="utf-8").read().splitlines()
added = []
plain = r"[A-Za-z0-9_./:@,+-]*"
for pair in pairs:
    key, _, value = pair.partition("=")
    if not re.match(r"^[A-Z_][A-Z0-9_]*$", key):
        raise SystemExit("不是 KEY=VALUE：%s" % pair)
    raw = re.fullmatch(plain, value) or re.fullmatch(r"(\$HOME|\$\{HOME\})/" + plain, value)
    text = "%s=%s" % (key, value if raw else shlex.quote(value))
    idx = [i for i, l in enumerate(lines) if l.startswith(key + "=")]
    if idx:
        lines[idx[-1]] = text
    else:
        added.append(text)
if added:
    lines += ["", "# ---- bin/panel2_cluster.sh 写入 ----"] + added
open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
PY
}
random_password() { "$PYTHON" -c 'import secrets, string; print("".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24)))'; }
copy_pack() {
  # This pack's files into $1, without deploy.env, inventory.json and what steps generate; offline/ as hard links when possible.
  local to=$1
  mkdir -p "$to" || fail "建不了目录 $to"
  ( cd "$PACK_DIR" && tar -cf - --exclude=./deploy.env --exclude=./inventory.json --exclude=./generated --exclude=./offline \
      --exclude='./pack-before-adopt.tar.gz' --exclude='./deploy.env.before-adopt' --exclude='./deploy.env.adopt-new' --exclude='*/__pycache__' \
      --exclude='./uninstall-panel1-before.json' . ) \
    | ( cd "$to" && tar -xf - ) || fail "拷不了部署包到 $to"
  if [ -d "$PACK_DIR/offline" ] && [ ! -e "$to/offline" ]; then
    cp -al "$PACK_DIR/offline" "$to/offline" 2>/dev/null || cp -a "$PACK_DIR/offline" "$to/offline" || fail "拷不了 offline 目录到 $to"
  fi
}

cmd_add() {
  local c=${1:-}; [ -n "$c" ] || usage; shift
  local group="" inv="" base="" mode="" env_from="" dir="" root="" sets=() ignore="" nocheck=""
  while [ $# -gt 0 ]; do
    case $1 in
      --model-group) group=${2:-}; shift 2 ;;
      --inventory) inv=${2:-}; shift 2 ;;
      --port-base) base=${2:-}; shift 2 ;;
      --mode) mode=${2:-}; shift 2 ;;
      --env-from) env_from=${2:-}; shift 2 ;;
      --dir) dir=${2:-}; shift 2 ;;
      --root) root=${2:-}; shift 2 ;;
      --set) sets+=("${2:-}"); shift 2 ;;
      --force) export VLLM_PD_FORCE=1; shift ;;
      --ignore-check) ignore=1; shift ;;
      --no-safety-check) nocheck=1; shift ;;
      *) usage ;;
    esac
  done
  [ -n "$group" ] && [ -n "$inv" ] || usage
  case $mode in ""|mock|vllm) ;; *) fail "--mode 只能是 mock 或 vllm" ;; esac
  [ -f "$inv" ] || fail "没有这个清单文件：$inv"
  inv=$(cd "$(dirname "$inv")" && pwd)/$(basename "$inv")
  "$PYTHON" "$PACK_DIR/scripts/inventory_tool.py" "$inv" summary >/dev/null || exit 1
  local rootv; rootv=$("${NAMES[@]}" root ${root:+--root "$root"}) || exit 1
  if [ -n "$dir" ]; then D=$dir; else D=$("${NAMES[@]}" dir --panel panel2 --cluster "$c" --root "$rootv") || exit 1; fi
  mkdir -p "$D" || fail "建不了目录 $D"
  D=$(cd "$D" && pwd)
  local reg; reg=$(registered_dir "$c")
  [ -z "$reg" ] || [ "$reg" = "$D" ] || fail "集群 $c 已经登记在 $reg：在那个目录里重跑，或先 bin/panel2_cluster.sh remove $c"
  if [ -f "$D/deploy.env" ]; then
    local had; had=$(bash -c 'set -a; . "$1" >/dev/null 2>&1; echo "${CLUSTER:-}"' _ "$D/deploy.env")
    [ "$had" = "$c" ] || fail "$D 里已经有 deploy.env，它的 CLUSTER 是「$had」，不是 $c：换一个 --dir"
    echo "沿用 $D/deploy.env（要改参数就改它，再重跑这条命令）"
    if [ -n "$root" ]; then
      local had_root; had_root=$(bash -c 'set -a; . "$1" >/dev/null 2>&1; echo "${VLLM_PD_ROOT:-}"' _ "$D/deploy.env")
      [ "$had_root" = "$rootv" ] || fail "集群 $c 已经装在总目录「${had_root:-$HOME}」下，不是 $rootv：要换总目录先 bin/panel2_cluster.sh remove $c"
    fi
    # The scripts come from the pack this command runs from, so that a corrected pack takes effect on the rerun.
    [ "$D" = "$PACK_DIR" ] || copy_pack "$D"
  else
    [ -z "$(ls -A "$D" 2>/dev/null)" ] || [ "$D" = "$PACK_DIR" ] || fail "$D 不是空目录，也不是集群 $c 的目录：换一个 --dir"
    [ "$D" = "$PACK_DIR" ] || copy_pack "$D"
    if [ -n "$base" ]; then
      "${NAMES[@]}" alloc --cluster "$c" --port-base "$base" --root "$rootv" >/dev/null || exit 1
    else
      base=$("${NAMES[@]}" alloc --cluster "$c" --root "$rootv") || exit 1
    fi
    local shared
    shared=$("${NAMES[@]}" shared ${env_from:+--env-from "$env_from"} --format json) || exit 1
    cp "$D/deploy.env.example" "$D/deploy.env.new" || fail "没有 $D/deploy.env.example"
    chmod 600 "$D/deploy.env.new"
    local kv=()
    while IFS= read -r line; do kv+=("$line"); done < <("$PYTHON" -c 'import json,sys; [print("%s=%s" % (k, v)) for k, v in json.load(sys.stdin).items() if v]' <<<"$shared")
    set_env "$D/deploy.env.new" "CLUSTER=$c" "MODEL_GROUP=$group" "PORT_BASE=$base" "VLLM_PD_ROOT=$rootv" "NODE_MODE=${mode:-mock}" \
      "CH_ADMIN_PASSWORD=$(random_password)" "CH_WRITER_PASSWORD=$(random_password)" "CH_READER_PASSWORD=$(random_password)" \
      "${kv[@]+"${kv[@]}"}"
    mv "$D/deploy.env.new" "$D/deploy.env"
    echo "已写 $D/deploy.env：集群 $c，模型文件夹 $group，端口基数 $base（ClickHouse $base、拉取程序 $((base + 1))、Prometheus $((base + 2))）"
    for k in GRAFANA_URL GRAFANA_PASSWORD DOCKER_NETWORK MONITOR_HOST; do
      grep -q "^$k=" <(printf '%s\n' "${kv[@]+"${kv[@]}"}") || echo "注意：没有取到共用值 $k，用的是模板里的值。用 --env-from 指定一个已装集群的 deploy.env，或改 $D/deploy.env 后重跑。"
    done
  fi
  [ ${#sets[@]} -eq 0 ] || set_env "$D/deploy.env" "${sets[@]}"
  [ -z "$mode" ] || set_env "$D/deploy.env" "NODE_MODE=$mode"
  cmp -s "$inv" "$D/inventory.json" || cp "$inv" "$D/inventory.json" || fail "拷不了清单到 $D/inventory.json"
  # v3.1-extra: the cluster's own segment definitions (deploy.env SEGMENTS_PROFILE, a folder of profiles/), else the pack's.
  local prof; prof=$(bash -c 'set -a; . "$1" >/dev/null 2>&1; echo "${SEGMENTS_PROFILE:-}"' _ "$D/deploy.env")
  if [ -n "$prof" ]; then
    case $prof in *[!a-z0-9_-]*) fail "deploy.env 的 SEGMENTS_PROFILE=$prof 只能用小写字母、数字、- 和 _" ;; esac
    [ -f "$PACK_DIR/profiles/$prof/segments.json" ] || fail "deploy.env 的 SEGMENTS_PROFILE=$prof：包里没有 profiles/$prof/segments.json"
    cmp -s "$PACK_DIR/profiles/$prof/segments.json" "$D/segments.json" || cp "$PACK_DIR/profiles/$prof/segments.json" "$D/segments.json" \
      || fail "拷不了 profiles/$prof/segments.json 到 $D"
    echo "段定义：profiles/$prof/segments.json（deploy.env 的 SEGMENTS_PROFILE）"
  fi

  echo
  echo "==== 集群 $c：查注册表、端口和清单"
  in_cluster 'names_check && "$PYTHON" "$PACK_DIR/scripts/names.py" register --panel panel2 --dir "$PACK_DIR" --inventory "$INVENTORY"' || exit 1
  [ -z "$root" ] || "${NAMES[@]}" root --root "$rootv" --save >/dev/null || exit 1
  local m; m=$(in_cluster 'echo "$NODE_MODE"')

  step 3 "导入镜像（已有就跳过）" bash -c '. bin/lib.sh; load_env
    for x in "$CLICKHOUSE_IMAGE:clickhouse-image.tar" "$CENTRAL_IMAGE:vector-image.tar"; do
      img=${x%:*}; tar=${x##*:}
      docker image inspect "$img" >/dev/null 2>&1 && { echo "已有镜像 $img"; continue; }
      [ -f "offline/$tar" ] || fail "没有镜像 $img，offline 目录里也没有 $tar"
      docker load -i "offline/$tar" || exit 1
    done'
  step 6 "预检监控机" bin/check_cpu.sh
  step 7 "预检所有节点" bin/nodes.sh check
  step 9 "启动 ClickHouse" bin/clickhouse_up.sh
  step 10 "启动采集器" bin/central_up.sh
  step 11 "给 Grafana 装 ClickHouse 插件（已装就跳过）" bin/grafana_plugin.sh
  step 12 "导入数据源和看板" bin/grafana_board.sh
  step 13 "让本集群的 Prometheus 抓采集链路的指标" bin/prometheus_job.sh
  step 14 "下发三个节点文件" bin/nodes.sh push
  step 15 "节点自检" bin/nodes.sh selfcheck
  if [ "$m" = mock ]; then
    step 16 "在所有节点上启动 mock 写入程序" bin/nodes.sh start
  fi
  step 17 "启动拉取程序" bin/puller.sh start
  if [ "$m" = mock ] && [ -n "$nocheck" ]; then
    # The update script (v3): the data flow never gates an update; the check stays a separate, optional step.
    echo; echo "==== 步骤 18：升级安全检查不做（--no-safety-check）；要核对时执行 bin/panel2_cluster.sh check $c"
  elif [ "$m" = mock ]; then
    # v2.1 (design 2.1): the upgrade-safety check, waiting at most 2 minutes for the filters' self-check lines.
    # Data accuracy is bin/panel2_cluster.sh quality and gates nothing.
    if [ -n "$ignore" ]; then
      echo; echo "==== 步骤 18：升级安全检查（--ignore-check：FAIL 只报不停）"
      ( cd "$D" && clean "$PYTHON" "$PACK_DIR/scripts/safety.py" --cluster-dir "$D" --wait 120 ) || echo "步骤 18 有 FAIL，按 --ignore-check 继续"
    else
      step 18 "升级安全检查" "$PYTHON" "$PACK_DIR/scripts/safety.py" --cluster-dir "$D" --wait 120
    fi
  fi
  echo
  echo "==== 每台集群机器要加的目录、挂载和环境变量（bin/nodes.sh paths）"
  ( cd "$D" && clean bin/nodes.sh paths )
  echo
  if [ "$m" = mock ]; then
    echo "数据准不准另看：bin/panel2_cluster.sh quality $c（不影响升级）"
    echo "RESULT: OK（集群 $c 装好了：看板在 Grafana 的「$group」文件夹里）"
  else
    echo "RESULT: OK（集群 $c 的监控机部分装好了。还没接上的 vLLM 实例按上面的输出加挂载和环境变量、重启 vLLM；已经接好的不用改。然后执行 bin/panel2_cluster.sh check $c）"
  fi
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
  [ -f "$D/deploy.env" ] || fail "$D 里没有 deploy.env，没法知道要删什么"
  local plugin=() th
  [ -n "$("${NAMES[@]}" others --cluster "$c" --panel panel2)" ] || plugin=(--remove-plugin)
  # The cluster's TIMING_HOME on this machine: purge-data empties it, the folder itself is removed at the end.
  th=$(in_cluster 'echo "$TIMING_HOME"') || th=""
  local u=$PACK_DIR/bin/uninstall_old.sh
  for a in plan remove purge-data check; do
    echo; echo "==== uninstall_old.sh $a $D"
    case $a in
      remove) clean "$u" remove "$D" "${plugin[@]+"${plugin[@]}"}" || fail "remove 没过，见上面的 FAIL 行。处理后重跑 bin/panel2_cluster.sh remove $c" ;;
      purge-data) clean "$u" purge-data "$D" "${backup[@]+"${backup[@]}"}" || fail "purge-data 没过，见上面的 FAIL 行" ;;
      *) clean "$u" "$a" "$D" || fail "$a 没过，见上面的 FAIL 行" ;;
    esac
  done
  "${NAMES[@]}" unregister --cluster "$c" --panel panel2
  case $D in
    "$HOME"|/|"") fail "不删目录 $D" ;;
  esac
  find "$D" -mindepth 1 -delete && rmdir "$D" && echo "已删除目录 $D"
  if [ -n "$th" ] && [ -d "$th" ] && [ -z "$(ls -A "$th" 2>/dev/null)" ]; then rmdir "$th" && echo "已删除空目录 $th"; fi
  echo "RESULT: OK（集群 $c 的面板二已卸载）"
}

cmd_check() {
  # The upgrade-safety check (design 2.1), read-only. Runs this pack's code against the installed folder: the update
  # script runs it from the new pack with --dir <installed folder> --pre-upgrade before anything changes.
  local c=${1:-}; [ -n "$c" ] || usage; shift
  local dir="" args=()
  while [ $# -gt 0 ]; do
    case $1 in
      --dir) dir=${2:-}; shift 2 ;;
      --pre-upgrade) args+=(--pre-upgrade); shift ;;
      --wait) args+=(--wait "${2:-0}"); shift 2 ;;
      *) usage ;;
    esac
  done
  if [ -n "$dir" ]; then D=$(cd "$dir" 2>/dev/null && pwd) || fail "没有这个目录：$dir"; else D=$(registered_dir "$c"); fi
  [ -n "$D" ] || fail "注册表里没有集群 $c 的面板二"
  ( cd "$D" && clean "$PYTHON" "$PACK_DIR/scripts/safety.py" --cluster-dir "$D" "${args[@]+"${args[@]}"}" )
}

cmd_quality() {
  # Data accuracy: completeness, pairing, gaps, panels (the former steps 18 and 19). Reports only; gates nothing.
  local c=${1:-}; [ -n "$c" ] || usage
  D=$(registered_dir "$c")
  [ -n "$D" ] || fail "注册表里没有集群 $c 的面板二"
  echo "==== 集群 $c：数据准确性检查（只报告，不影响升级、check 和 run）"
  local rc=0
  echo; echo "==== 链路和看板（bin/verify.sh）"
  ( cd "$D" && clean bin/verify.sh ) || rc=1
  echo; echo "==== 部署后核验（bin/postdeploy_check.sh）"
  ( cd "$D" && clean bin/postdeploy_check.sh ) || rc=1
  [ $rc -eq 0 ] && echo "RESULT: OK（集群 $c 的数据检查）" || echo "RESULT: FAIL（集群 $c 的数据检查有项没过，见上面；不影响升级）"
  return $rc
}

cmd_adopt() {
  local dir=${1:-}; [ -n "$dir" ] || usage; shift
  local c="" group=""
  while [ $# -gt 0 ]; do
    case $1 in
      --cluster) c=${2:-}; shift 2 ;;
      --model-group) group=${2:-}; shift 2 ;;
      *) usage ;;
    esac
  done
  [ -n "$c" ] && [ -n "$group" ] || usage
  D=$(cd "$dir" 2>/dev/null && pwd) || fail "没有这个目录：$dir"
  [ -f "$D/deploy.env" ] && [ -f "$D/inventory.json" ] || fail "$D 里没有 deploy.env 和 inventory.json：写已经装好的面板二的目录"
  local had; had=$(bash -c 'set -a; . "$1" >/dev/null 2>&1; echo "${CLUSTER:-}"' _ "$D/deploy.env")
  [ -z "$had" ] || [ "$had" = "$c" ] || fail "$D 已经是集群 $had"
  local reg; reg=$(registered_dir "$c")
  [ -z "$reg" ] || [ "$reg" = "$D" ] || fail "集群名 $c 已经登记给 $reg"
  # 1. The new deploy.env as a separate file, checked against the registry and the ports before anything changes.
  local new="$D/deploy.env.adopt-new" kv=()
  cp -p "$D/deploy.env" "$new" || fail "写不了 $new"
  while IFS= read -r line; do kv+=("$line"); done < <("${NAMES[@]}" adopt-values --env-file "$D/deploy.env")
  set_env "$new" "CLUSTER=$c" "MODEL_GROUP=$group" "${kv[@]+"${kv[@]}"}"
  echo "==== 集群 $c：查注册表、端口和清单"
  if ! clean "${NAMES[@]}" check --panel panel2 --env-file "$new" --dir "$D" --inventory "$D/inventory.json"; then
    rm -f "$new"
    fail "没有登记，$D 里什么都没有改。处理上面的冲突后重跑。"
  fi
  # 2. Keep what is there now: deploy.env, and the scripts as an archive (written outside the folder, then moved in).
  [ -f "$D/deploy.env.before-adopt" ] || cp -p "$D/deploy.env" "$D/deploy.env.before-adopt" || fail "备份不了 deploy.env"
  if [ "$D" != "$PACK_DIR" ] && [ ! -f "$D/pack-before-adopt.tar.gz" ]; then
    local tmp; tmp=$(mktemp "${TMPDIR:-/tmp}/pack-before-adopt.XXXXXX") || fail "建不了临时文件"
    ( cd "$D" && tar -czf "$tmp" --exclude=./offline --exclude=./generated --exclude=./deploy.env --exclude=./deploy.env.before-adopt \
        --exclude=./deploy.env.adopt-new --exclude='*/__pycache__' . ) || { rm -f "$tmp" "$new"; fail "打包不了 $D 原来的脚本，什么都没有改"; }
    mv "$tmp" "$D/pack-before-adopt.tar.gz" || { rm -f "$tmp" "$new"; fail "放不进 $D/pack-before-adopt.tar.gz"; }
  fi
  # 3. Then change: the deploy.env, the scripts, the registry.
  mv "$new" "$D/deploy.env" || fail "换不了 $D/deploy.env"
  echo "已在 $D/deploy.env 写入 CLUSTER=$c、MODEL_GROUP=$group，以及原来留空、现在写明的 ${#kv[@]} 项（值和原来一样；改前的文件是 deploy.env.before-adopt）"
  if [ "$D" != "$PACK_DIR" ]; then
    copy_pack "$D"
    echo "$D 的脚本已换成这一版（原来的在 pack-before-adopt.tar.gz）；deploy.env、inventory.json、offline 没动"
  fi
  in_cluster '"$PYTHON" "$PACK_DIR/scripts/names.py" register --panel panel2 --dir "$PACK_DIR" --inventory "$INVENTORY"' || fail "登记失败，见上面的输出"
  echo "RESULT: OK（集群 $c 已登记，容器没有动。要把看板挪进「$group」文件夹：cd $D && bin/grafana_board.sh）"
}

case "${1:-}" in
  add) shift; cmd_add "$@" ;;
  remove) shift; cmd_remove "$@" ;;
  list) "${NAMES[@]}" list ;;
  check) shift; cmd_check "$@" ;;
  quality) shift; cmd_quality "$@" ;;
  adopt) shift; cmd_adopt "$@" ;;
  *) usage ;;
esac
