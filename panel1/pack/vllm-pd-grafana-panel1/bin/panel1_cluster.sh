#!/usr/bin/env bash
# Panel 1 for one more cluster on the same monitoring machine: its own Prometheus, its own data source and two
# dashboards in the shared Grafana (through the Grafana API, in the folder MODEL_GROUP).
#
#   bin/panel1_cluster.sh add <CLUSTER> --model-group <G> --inventory <inventory.json> [--root R] [--dir D] [--port-base B] [--env-from F]
#   bin/panel1_cluster.sh remove <CLUSTER>
#   bin/panel1_cluster.sh list
#   bin/panel1_cluster.sh check <CLUSTER>
#   bin/panel1_cluster.sh adopt <DIR> --cluster <CLUSTER> --model-group <G> [--prometheus-container N] [--prometheus-port P]
#
# add       folder ROOT/vllm-pd-grafana-panel1/<CLUSTER> (ROOT: --root R, saved in the registry for later adds, else
#           the registry's, else $HOME; --dir D instead: not there yet, empty, or this cluster's), port block (automatic, or --port-base), registry and port
#           check, prometheus.yml, data volume and container vllm-pd-prometheus-<CLUSTER>, data source, folder and
#           two dashboards through the Grafana API, registry entry, then verify_panel1.py --cluster.
#           Running it again with the same arguments updates the same cluster.
# remove    deletes the two dashboards and the data source (API), the container and its data volume, the folder,
#           and the registry entry. The Grafana folder stays. Refuses an installation whose dashboards Grafana
#           loads from files (one installed with the README steps).
# list      the registered clusters.
# check     registry and inventory check, then verify_panel1.py --cluster.
# adopt     registers an existing installation (README steps) under a cluster name; writes <DIR>/deploy.env with
#           its current names; changes no container, file or Grafana object.
# Shared values (GRAFANA_URL, GRAFANA_USER, GRAFANA_PASSWORD, GRAFANA_CONTAINER, DOCKER_NETWORK): from the
# environment, else --env-from (panel 2's deploy.env), else a registered cluster's deploy.env; DOCKER_NETWORK
# falls back to the network of the Grafana container.
set -euo pipefail
PACK=$(cd "$(dirname "$0")/.." && pwd)
NAMES=(python3 "$PACK/scripts/names.py")
GEN=(python3 "$PACK/scripts/gen_cluster.py")
PROM_IMAGE=prom/prometheus:v3.11.2
SHARED=(GRAFANA_URL GRAFANA_USER GRAFANA_PASSWORD GRAFANA_CONTAINER DOCKER_NETWORK)

die() { echo "错误：$*" >&2; exit 1; }
usage() { sed -n '4,9p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }

valid_cluster() { [[ $1 =~ ^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$ ]] || die "集群名 $1 不合规：只用小写字母、数字和 -"; }

# Shared values: the environment wins, then `names.py shared`.
shared_values() {
  local k pre
  for k in "${SHARED[@]}"; do printf -v "PRE_$k" '%s' "${!k-}"; done
  eval "$("${NAMES[@]}" shared ${1:+--env-from "$1"} --format shell)"
  for k in "${SHARED[@]}"; do
    pre=PRE_$k
    if [ -n "${!pre}" ]; then printf -v "$k" '%s' "${!pre}"; export "${k?}"; fi
  done
  [ -n "${GRAFANA_URL:-}" ] || die "没有 GRAFANA_URL：export GRAFANA_URL=http://localhost:3000，或 --env-from <面板二 deploy.env>"
  [ -n "${DOCKER_NETWORK:-}" ] || die "没有 DOCKER_NETWORK：export DOCKER_NETWORK=<Grafana 所在的容器网络>，或 --env-from <面板二 deploy.env>"
}

write_env() {  # write_env FILE KEY...  (bash-quoted, mode 600)
  local f=$1 k; shift
  ( umask 077; : >"$f.tmp"; for k in "$@"; do printf '%s=%q\n' "$k" "${!k-}" >>"$f.tmp"; done; mv "$f.tmp" "$f" )
}

registered_dir() {  # the folder the registry has for cluster $1, or nothing
  "${NAMES[@]}" list --format json | python3 -c 'import json,sys; e=json.load(sys.stdin)["clusters"].get(sys.argv[1]) or {}; print((e.get("panel1") or {}).get("dir") or "")' "$1"
}

load_cluster() {  # load_cluster CLUSTER -> DIR and the derived values in the environment
  local c=$1
  DIR=$(registered_dir "$c")
  [ -n "$DIR" ] || DIR=$("${NAMES[@]}" dir --panel panel1 --cluster "$c")
  [ -f "$DIR/deploy.env" ] || die "没有 $DIR/deploy.env：集群 $c 的面板一没有装在这里（bin/panel1_cluster.sh list 看已登记的）"
  eval "$("${NAMES[@]}" derive --panel panel1 --env-file "$DIR/deploy.env" --format shell)"
  [ "$CLUSTER" = "$c" ] || die "$DIR/deploy.env 里是 CLUSTER=${CLUSTER}，不是 $c"
}

wait_ready() {  # wait_ready URL
  local i
  for i in $(seq 1 60); do
    curl -fsS -o /dev/null "$1/-/ready" 2>/dev/null && return 0
    sleep 1
  done
  die "Prometheus $1 一分钟内没有就绪：docker logs $PROMETHEUS_CONTAINER 看最后几行"
}

wait_scraped() {  # every active target scraped once (or 90 s), then two more scrape intervals so that rate() has data
  local i si
  for i in $(seq 1 45); do
    curl -fsS "$PROMETHEUS_URL/api/v1/targets?state=active" 2>/dev/null \
      | python3 -c 'import json,sys; t=json.load(sys.stdin)["data"]["activeTargets"]; sys.exit(0 if t and all(x["health"]!="unknown" for x in t) else 1)' \
      && break
    sleep 2
  done
  si=$(python3 -c 'import json,re,sys; v=json.load(open(sys.argv[1])).get("scrape_interval","15s"); m=re.match(r"^(\d+)(s|m)$",v); print(int(m.group(1))*(60 if m.group(2)=="m" else 1) if m else 15)' "$DIR/prometheus/inventory.json")
  sleep $((2 * si + 5))
}

wait_mounted() {  # the container sees the prometheus.yml just written (a file bind mount can lag, e.g. Docker Desktop)
  local i
  for i in $(seq 1 30); do
    docker exec "$PROMETHEUS_CONTAINER" cat /etc/prometheus/prometheus.yml 2>/dev/null | cmp -s - "$DIR/prometheus/prometheus.yml" && return 0
    sleep 1
  done
  die "容器 $PROMETHEUS_CONTAINER 里的 prometheus.yml 与 $DIR/prometheus/prometheus.yml 不同（文件被换成了新文件？）：docker restart $PROMETHEUS_CONTAINER 后再执行一次"
}

copy_pack() {  # the pack's own files into the cluster folder
  local f
  mkdir -p "$DIR/prometheus" "$DIR/cluster"
  for f in README.md MANIFEST.txt SHA256SUMS bin scripts grafana patches prometheus/gen_prometheus_yml.py \
           prometheus/inventory.example.json prometheus/prometheus.example.yml; do
    [ -e "$PACK/$f" ] || continue
    rm -rf "${DIR:?}/$f"
    cp -R "$PACK/$f" "$DIR/$f"
  done
  rm -rf "$DIR/scripts/__pycache__"
}

cmd_add() {
  local c=${1:-} mg="" inv="" pb="" envf="" dir="" root=""
  [ -n "$c" ] || usage
  shift
  while [ $# -gt 0 ]; do
    case $1 in
      --model-group) mg=$2; shift 2 ;;
      --inventory) inv=$2; shift 2 ;;
      --port-base) pb=$2; shift 2 ;;
      --env-from) envf=$2; shift 2 ;;
      --dir) dir=$2; shift 2 ;;
      --root) root=$2; shift 2 ;;
      *) usage ;;
    esac
  done
  valid_cluster "$c"
  [ -n "$mg" ] && [ -n "$inv" ] || usage
  case $dir in "" | /*) ;; *) dir=$PWD/$dir ;; esac
  [ -f "$inv" ] || die "没有清单文件 $inv"
  inv=$(cd "$(dirname "$inv")" && pwd)/$(basename "$inv")
  local B="" prev="" cand prevroot="" rootv
  [ -z "$root" ] || root=$("${NAMES[@]}" root --root "$root")
  # a re-run (also after an add that failed before registering) keeps the port block and ROOT in the cluster's
  # deploy.env: the registered folder first, then --dir, then the default folder
  cand=$(registered_dir "$c")
  [ -n "$cand" ] || cand=$dir
  [ -n "$cand" ] || cand=$("${NAMES[@]}" dir --panel panel1 --cluster "$c" ${root:+--root "$root"})
  if [ -f "$cand/deploy.env" ] && grep -qx "CLUSTER=$c" "$cand/deploy.env"; then
    prev=$(sed -n 's/^PORT_BASE=//p' "$cand/deploy.env" | tail -1)
    prevroot=$(unset VLLM_PD_ROOT; set +u; . "$cand/deploy.env" >/dev/null 2>&1; printf '%s' "${VLLM_PD_ROOT-}")
  fi
  if [ -n "$prevroot" ]; then
    [ -z "$root" ] || [ "$root" = "$prevroot" ] || die "$cand/deploy.env 里是 VLLM_PD_ROOT=${prevroot}，和 --root $root 不同：要换总目录先 remove 再 add"
    rootv=$prevroot
  else
    rootv=$("${NAMES[@]}" root ${root:+--root "$root"})
  fi
  export VLLM_PD_ROOT="$rootv"
  if [ -n "$prev" ]; then
    [ -z "$pb" ] || [ "$pb" = "$prev" ] || die "$cand/deploy.env 里是 PORT_BASE=${prev}，和 --port-base $pb 不同：要换端口段先 remove 再 add"
    B=$prev
    echo "沿用 $cand/deploy.env 里的 PORT_BASE=${B}"
  else
    B=$("${NAMES[@]}" alloc --cluster "$c" --root "$rootv" ${pb:+--port-base "$pb"})
  fi
  [ -z "$dir" ] || export PANEL1_DIR="$dir"
  eval "$("${NAMES[@]}" derive --panel panel1 --cluster "$c" --port-base "$B" --model-group "$mg" --format shell)"
  DIR=$PANEL1_DIR
  # remove deletes the whole folder: --dir must be new, empty, or this cluster's own installation
  if [ -n "$dir" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ] && ! grep -qsx "CLUSTER=$c" "$DIR/deploy.env"; then
    die "$DIR 已经有别的文件（不是集群 $c 的面板一）：--dir 要给一个不存在或空的目录"
  fi
  if [ -f "$DIR/deploy.env" ] && ! grep -qx "CLUSTER=$c" "$DIR/deploy.env"; then
    die "$DIR 里已经有别的集群的 deploy.env"
  fi
  [ "$DIR" != "$PACK" ] || die "目标目录就是本部署包所在的目录 $PACK"
  shared_values "$envf"
  echo "== 集群 ${c}：PORT_BASE=${B}，总目录 ${rootv}，目录 ${DIR}，Prometheus ${PROMETHEUS_CONTAINER}（主机端口 ${PROMETHEUS_PORT}），文件夹 $GRAFANA_FOLDER"
  "${NAMES[@]}" check --panel panel1 --cluster "$c" --port-base "$B" --model-group "$mg" --inventory "$inv"
  copy_pack
  CLUSTER=$c MODEL_GROUP=$mg PORT_BASE=$B
  write_env "$DIR/deploy.env" CLUSTER MODEL_GROUP PORT_BASE VLLM_PD_ROOT PANEL1_DIR "${SHARED[@]}"
  "${GEN[@]}" generate --env-file "$DIR/deploy.env" --inventory "$inv"
  chmod -R a+rX "$DIR/prometheus"
  docker volume create "$PROMETHEUS_VOLUME" >/dev/null
  # the data volume too: panel 2's rule files live in it (/prometheus/...)
  docker run --rm -v "$DIR/prometheus:/p:ro" -v "$PROMETHEUS_VOLUME:/prometheus:ro" --entrypoint promtool "$PROM_IMAGE" \
    check config /p/prometheus.yml >/dev/null || die "promtool 认为 $DIR/prometheus/prometheus.yml 有错"
  if docker container inspect "$PROMETHEUS_CONTAINER" >/dev/null 2>&1; then
    if [ "$(docker container inspect -f '{{.State.Running}}' "$PROMETHEUS_CONTAINER")" = true ]; then
      wait_mounted
      curl -fsS -X POST "$PROMETHEUS_URL/-/reload" >/dev/null || die "Prometheus 热加载失败"
      echo "Prometheus $PROMETHEUS_CONTAINER 已在运行，已热加载"
    else
      docker start "$PROMETHEUS_CONTAINER" >/dev/null
      echo "Prometheus $PROMETHEUS_CONTAINER 已启动"
    fi
  else
    eval "$("${GEN[@]}" prometheus-cmd --env-file "$DIR/deploy.env")" >/dev/null
    echo "Prometheus $PROMETHEUS_CONTAINER 已建"
  fi
  wait_ready "$PROMETHEUS_URL"
  "${GEN[@]}" grafana-add --env-file "$DIR/deploy.env"
  # --root given: saved in the registry, later adds without --root use it
  "${NAMES[@]}" register --panel panel1 --env-file "$DIR/deploy.env" --inventory "$DIR/prometheus/inventory.json" ${root:+--root "$rootv"}
  echo "== 核验（等每个目标抓过一次）"
  wait_scraped
  python3 "$DIR/scripts/verify_panel1.py" --cluster "$c" \
    || echo "== 集群 $c 已装好；核验有未通过项，见上面的 RESULT 一行（有的 panel 本来就没数，对照 PANELS.md）"
}

cmd_remove() {
  local c=${1:-}
  [ -n "$c" ] || usage
  valid_cluster "$c"
  load_cluster "$c"
  [ "$DIR" != "$PACK" ] || die "要删的目录就是正在运行的这个部署包 ${PACK}：请用别的目录里的 bin/panel1_cluster.sh"
  "${GEN[@]}" grafana-remove --env-file "$DIR/deploy.env"
  docker rm -fv "$PROMETHEUS_CONTAINER" >/dev/null 2>&1 && echo "容器 $PROMETHEUS_CONTAINER 已删" || echo "容器 $PROMETHEUS_CONTAINER 本来就没有"
  docker volume rm "$PROMETHEUS_VOLUME" >/dev/null 2>&1 && echo "数据卷 $PROMETHEUS_VOLUME 已删" || echo "数据卷 $PROMETHEUS_VOLUME 本来就没有"
  rm -rf "${DIR:?}"
  echo "目录 $DIR 已删"
  "${NAMES[@]}" unregister --cluster "$c" --panel panel1
}

cmd_check() {
  local c=${1:-} rc=0
  [ -n "$c" ] || usage
  valid_cluster "$c"
  load_cluster "$c"
  "${NAMES[@]}" check --panel panel1 --env-file "$DIR/deploy.env" --inventory "$DIR/prometheus/inventory.json" || rc=1
  python3 "$DIR/scripts/verify_panel1.py" --cluster "$c" || rc=1
  return $rc
}

cmd_adopt() {
  local d=${1:-} c="" mg="" pc=vllm-pd-prometheus pp=9090
  [ -n "$d" ] || usage
  shift
  while [ $# -gt 0 ]; do
    case $1 in
      --cluster) c=$2; shift 2 ;;
      --model-group) mg=$2; shift 2 ;;
      --prometheus-container) pc=$2; shift 2 ;;
      --prometheus-port) pp=$2; shift 2 ;;
      --env-from) envf=$2; shift 2 ;;
      *) usage ;;
    esac
  done
  [ -n "$c" ] && [ -n "$mg" ] || usage
  valid_cluster "$c"
  d=$(cd "$d" && pwd)
  [ -f "$d/prometheus/prometheus.yml" ] || die "$d 不像面板一的安装目录（没有 prometheus/prometheus.yml）"
  if [ -f "$d/deploy.env" ] && grep -q '^CLUSTER=' "$d/deploy.env" && ! grep -qx "CLUSTER=$c" "$d/deploy.env"; then
    die "$d/deploy.env 里已经是别的集群"
  fi
  shared_values "${envf:-}"
  CLUSTER=$c MODEL_GROUP=$mg PANEL1_DIR=$d PROMETHEUS_CONTAINER=$pc PROMETHEUS_PORT=$pp
  PROMETHEUS_VOLUME=vllm-pd-prometheus-data PROMETHEUS_DS_UID=prometheus PROMETHEUS_DS_NAME=Prometheus
  PANEL1_P_UID=vllm-pd-prefill PANEL1_D_UID=vllm-pd-decode PROMETHEUS_YML=$d/prometheus/prometheus.yml
  PROMETHEUS_URL=http://localhost:$pp
  write_env "$d/deploy.env" CLUSTER MODEL_GROUP PANEL1_DIR PROMETHEUS_CONTAINER PROMETHEUS_PORT PROMETHEUS_VOLUME \
    PROMETHEUS_DS_UID PROMETHEUS_DS_NAME PANEL1_P_UID PANEL1_D_UID PROMETHEUS_YML PROMETHEUS_URL "${SHARED[@]}"
  local invarg=()
  [ -f "$d/prometheus/inventory.json" ] && invarg=(--inventory "$d/prometheus/inventory.json")
  "${NAMES[@]}" check --panel panel1 --env-file "$d/deploy.env" ${invarg[@]+"${invarg[@]}"}
  "${NAMES[@]}" register --panel panel1 --env-file "$d/deploy.env" ${invarg[@]+"${invarg[@]}"}
  echo "已登记：$d 是集群 $c 的面板一（容器、文件、Grafana 都没有改）"
}

case ${1:-} in
  add) shift; cmd_add "$@" ;;
  remove) shift; cmd_remove "$@" ;;
  list) "${NAMES[@]}" list ;;
  check) shift; cmd_check "$@" ;;
  adopt) shift; cmd_adopt "$@" ;;
  *) usage ;;
esac
