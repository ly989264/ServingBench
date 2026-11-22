#!/bin/bash
# t53drive.sh: panel two plus the T58 patches for a whole glm5.3 cluster, one stop and one start, driven from the
# monitor machine over  ssh root@<machine>.  The script never stops or starts vLLM or the proxy: it stops and asks.
#
#   t53drive.sh run      <list> [--dry-run]   go on from where it stopped, up to the next stop or failure
#   t53drive.sh status   <list>               where it stands, the last result of each machine, what to do next
#   t53drive.sh rollback <list>               put the files of stage ② back (also stops for the stop and the start)
#   t53drive.sh remove-panel <list>           remove CLUSTER's panel two (the rehearsal's first step); keeps its
#                                             deploy.env for ENV_FROM; says so first when it is the only panel-two cluster
#
# Stages: ① precheck  ② backup, p2d, panel two's add  ③ (user stops the cluster)  ④ swap files
#         ⑤ (user starts the cluster) checks  ⑥ boards and checks, a second time about 30 minutes later.
# Full output of every step goes to <STATE>/<CLUSTER>/log/; the screen gets one line per stage and machine.
set -u -o pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
L=$HERE/lib
CMD=${1:-}; LIST=${2:-}; DRY=0
[ "${3:-}" = --dry-run ] && DRY=1
case $CMD in run|status|rollback|remove-panel) ;; *) sed -n '3,12p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;; esac
[ -f "$LIST" ] || { echo "清单 $LIST 不存在"; exit 2; }
PY="python3 -I"
out=$($PY "$L/plan.py" parse "$LIST") || { echo "$out"; exit 2; }
eval "$out"
. "$L/expect.sh"
[ -n "${T53_EXPECT:-}" ] && . "$T53_EXPECT"     # the lab's own values; never set on site
SD=$STATE/$CLUSTER
mkdir -p "$SD"/log "$SD"/kv "$SD"/res "$SD"/back "$SD"/push && chmod 700 "$STATE" "$SD"
$PY "$L/plan.py" machines "$LIST" > "$SD/machines"
SSHO=(-o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=15 -o ServerAliveCountMax=4)
T=/tmp/t53
NTOT=$((P_TOTAL + D_TOTAL))
TO=${T53_TIMEOUT:-1200}

say() { echo "$*" | tee -a "$SD/screen.log"; }
st_get() { cat "$SD/$1" 2>/dev/null || echo "${2:-}"; }
st_set() { echo "$2" > "$SD/$1"; }
mf() { awk -v n="$1" -v f="$2" '$1 == n { split("name addr role ports container file", k, " "); for (i in k) if (k[i] == f) print $i }' "$SD/machines"; }
names() { awk -v r="${1:-PD}" 'index(r, $3) { print $1 }' "$SD/machines"; }
kvget() { awk -F= -v k="$2" '$1 == k { v = substr($0, length(k) + 2) } END { print v }' "$SD/kv/$1" 2>/dev/null; }
kvset() { echo "$2=$3" >> "$SD/kv/$1"; }
hm() { date -d "@$1" +%H:%M 2>/dev/null || date -r "$1" +%H:%M; }

# ---- the files every machine gets in /tmp/t53 ----------------------------------------------------------------
push_dir() {
  rm -rf "$SD/push" && mkdir -p "$SD/push/lib" "$SD/push/kits"
  cp "$L"/node.sh "$L"/c_probe.py "$L"/c_act.py "$SD/push/lib/"
  cp "$KITS/t58-glm53-patch-kit.tar" "$SD/push/kits/"
  if [ "$MOONCAKE" = replace ]; then cp "$KITS/t58-glm53-mooncake-kit.tar" "$SD/push/kits/"; cp -r "$HERE/t53-mc-exception" "$SD/push/kits/"; fi
  if [ "$PROXY" = on ]; then tar -xzf "$KITS/t51-glm53-proxy-kit.tar.gz" -C "$SD/push/kits/"; cp "$PROXY_NEW" "$SD/push/kits/proxy_new.py"; fi
  (cd "$SD/push" && find . -type f | sort | xargs sha256sum) > "$SD/push.sums"
  sha256sum "$SD/push.sums" | cut -c1-16 > "$SD/push/kits/.id"
}

# ---- one node.sh command on a list of machines, in parallel ---------------------------------------------------
node_args() {   # NA=(...) for machine $1
  local n=$1 v pair
  NA=(NAME="$n" ROLE="$(mf "$n" role)" PORTS="$(mf "$n" ports)" MOONCAKE="$MOONCAKE" MC_NEW_SHA="$MC_NEW_SHA")
  v=$(kvget "$n" container); [ -n "$v" ] || v=$(mf "$n" container); NA+=(C="$v")
  if [ "$(mf "$n" role)" = proxy ]; then
    NA+=(PXFILE="$(mf "$n" file)" PX_ORIG="$PROXY_ORIG_SHA256" PX_NEW="$PROXY_NEW_SHA256"); return
  fi
  NA+=(START_GIVEN="$(mf "$n" file)")
  for pair in START:start LAUNCH:launch TMPL:tmpl LOGS:logs MCJSON:mcjson STARTMC:startmc BASELINE:baseline_token BK:bk \
              S58:s58 INSERT:insert ANCHOR:anchor MCLIB:mclib OFFSETS:offsets PURELIB:purelib; do
    v=$(kvget "$n" "${pair#*:}"); [ -n "$v" ] && NA+=("${pair%%:*}=$v")
  done
  v=$(kvget "$n" logs); NA+=(LOG1="${v%%;*}")
}

job() {   # machine stage command [extra KEY=VALUE...]: push, run node.sh, keep the log
  local n=$1 stg=$2 c=$3; shift 3
  local a; a=$(mf "$n" addr); node_args "$n"; NA+=("$@")
  local log=$SD/log/$stg.$n.log
  rm -f "$log.ssh"
  {
    ssh "${SSHO[@]}" "root@$a" "mkdir -p $T/back; cat $T/kits/.id 2>/dev/null; exit 0" > "$SD/log/.id.$n" 2>> "$log.ssh" || { cat "$log.ssh"; echo "ssh_exit=255"; return; }
    scp -q -r "${SSHO[@]}" "$SD/push/lib" "root@$a:$T/" || { echo "ssh_exit=255"; return; }
    if ! cmp -s "$SD/log/.id.$n" "$SD/push/kits/.id"; then
      scp -q -r "${SSHO[@]}" "$SD/push/kits" "root@$a:$T/" || { echo "ssh_exit=255"; return; }
    fi
    if [ "$c" = rb_restore ] && [ -d "$SD/back/$n" ]; then scp -q -r "${SSHO[@]}" "$SD/back/$n" "root@$a:$T/back/" ; fi
    timeout "$TO" ssh "${SSHO[@]}" "root@$a" "bash $T/lib/node.sh $c $(printf '%q ' "${NA[@]}")"
    echo "ssh_exit=$?"
  } > "$log" 2>&1
}

judge_one() {   # machine stage judge-stage -> res file
  local n=$1 stg=$2 j=$3 log=$SD/log/$2.$1.log e
  e=$(sed -n 's/^ssh_exit=//p' "$log" | tail -n 1)
  if [ "$e" = 255 ] || [ -z "$e" ]; then
    if grep -q 'Host key verification failed' "$log"; then echo "FAIL 监控机还不认识这台的 host key：先在监控机上 ssh root@这台 一次，回答 yes" > "$SD/res/$stg.$n"
    elif grep -q 'Permission denied' "$log"; then echo "FAIL ssh root 不能免密登录这台" > "$SD/res/$stg.$n"
    else echo "FAIL ssh 连不上（$(grep -m1 -E 'ssh:|Connection' "$log" | cut -c1-60)）" > "$SD/res/$stg.$n"; fi
    return
  fi
  if [ "$e" = 124 ]; then echo "FAIL 超过 $TO 秒没做完" > "$SD/res/$stg.$n"; return; fi
  ROLE=$(mf "$n" role) PORTS=$(mf "$n" ports) NAME=$n D_TOTAL=$D_TOTAL MOONCAKE=$MOONCAKE PROXY_ORIG_SHA256=$PROXY_ORIG_SHA256 \
  PROXY_NEW_SHA256=$PROXY_NEW_SHA256 MC_SWAPPED=$(st_get mc_swapped 0) FIRST=${FIRST:-0} EXP_P_TP=$EXP_P_TP EXP_CONN=$EXP_CONN \
  EXP_SITE_KV=$EXP_SITE_KV EXP_SITE_PREFILL=$EXP_SITE_PREFILL EXP_P2D_ALT=$EXP_P2D_ALT EXP_P2D_EXTRA=$EXP_P2D_EXTRA \
  EXP_MC_VERDICT_PRE=$EXP_MC_VERDICT_PRE EXP_MC_COMMENT=$EXP_MC_COMMENT EXP_D_STAGES=$EXP_D_STAGES MIN_FREE_GB=$MIN_FREE_GB LAB_NO_IMPORT=${LAB_NO_IMPORT:-0} \
    $PY "$L/judge.py" "$j" "$log" "$SD/kv/$n" > "$SD/res/$stg.$n" 2>&1 || echo "FAIL 判定出错，见 $log" > "$SD/res/$stg.$n"
}

stage_on() {   # stage judge node-command machines... [-- extra KEY=VALUE]: run, judge, print; returns 1 on any FAIL
  local stg=$1 j=$2 c=$3; shift 3
  local ms=() extra=() n pids=()
  while [ $# -gt 0 ] && [ "$1" != -- ]; do ms+=("$1"); shift; done
  [ "${1:-}" = -- ] && { shift; extra=("$@"); }
  for n in "${ms[@]}"; do rm -f "$SD/res/$stg.$n"; job "$n" "$stg" "$c" "${extra[@]+"${extra[@]}"}" & pids+=($!); done
  for p in "${pids[@]}"; do wait "$p"; done
  for n in "${ms[@]}"; do judge_one "$n" "$stg" "$j"; done
  report "$stg" "${ms[@]}"
}

report() {   # one line per machine; all-OK machines of one role with the same message go on one line
  local stg=$1 n bad=0 r; shift
  for n in "$@"; do grep -q '^FAIL' "$SD/res/$stg.$n" && bad=1; done
  for r in P D proxy; do
    local ok_ms=() msgs=()
    for n in "$@"; do
      [ "$(mf "$n" role)" = $r ] || continue
      if grep -q '^FAIL' "$SD/res/$stg.$n"; then say "  $n  $(cat "$SD/res/$stg.$n")"; else ok_ms+=("$n"); msgs+=("$(cut -c6- "$SD/res/$stg.$n")"); fi
    done
    [ ${#ok_ms[@]} = 0 ] && continue
    if [ ${#ok_ms[@]} -gt 1 ] && [ "$(printf '%s\n' "${msgs[@]}" | sort -u | wc -l)" = 1 ]; then
      say "  $r ${#ok_ms[@]} 台  OK   ${msgs[0]}"
    else
      for n in "${ok_ms[@]}"; do say "  $n  OK   $(cut -c6- "$SD/res/$stg.$n")"; done
    fi
  done
  return $bad
}

mon_line() { local r; r=$("$@" 2>&1); local e=$?; say "  监控机  $(echo "$r" | tail -n 1)"; return $e; }

p2dir() { $PY "$L/mon.py" reg "$CLUSTER" panel2 dir; }
chq() {   # a read-only query to this cluster's ClickHouse, SQL on stdin
  (cd "$(p2dir)" && bash -c '. bin/lib.sh && load_env >/dev/null && curl -s -u "vllm_reader:$CH_READER_PASSWORD" "http://localhost:$CLICKHOUSE_HTTP_PORT/" --data-binary @-')
}
sqlf() { sed -e 's/FORMAT PrettyCompactMonoBlock/FORMAT TSVWithNames/' "$L/sql/$1.sql"; }

fail_stop() { say "结论：$1 没过，停在这里。完整输出在 $SD/log/。"; say "下一步：$2"; exit 1; }

# ==== ① precheck =================================================================================================
s_pre() {
  say "== ① 预检（集群照常运行，只读）"
  local f bad=0
  for f in "$KITS/t58-glm53-patch-kit.tar:$EXP_KIT_PATCH" \
           $( [ "$MOONCAKE" = replace ] && echo "$KITS/t58-glm53-mooncake-kit.tar:$EXP_KIT_MC $HERE/t53-mc-exception/SHA256SUMS:$EXP_MC_EXC_SUMS" ) \
           $( [ "$PROXY" = on ] && echo "$KITS/t51-glm53-proxy-kit.tar.gz:$EXP_KIT_PROXY $PROXY_NEW:$PROXY_NEW_SHA256" ); do
    [ "$(sha256sum "${f%%:*}" 2>/dev/null | cut -c1-64)" = "${f##*:}" ] || { say "  监控机  FAIL $(basename "${f%%:*}") 不在或 sha256 不对"; bad=1; }
  done
  [ "$MOONCAKE" = replace ] && { (cd "$HERE/t53-mc-exception" && sha256sum -c --quiet SHA256SUMS >/dev/null 2>&1) || { say "  监控机  FAIL t53-mc-exception 的文件 sha256 不对"; bad=1; }; }
  if [ "$(cat "$PANEL_PACK/VERSION" 2>/dev/null)" != "$EXP_PANEL_VERSION" ] || ! (cd "$PANEL_PACK" && sha256sum -c --quiet SHA256SUMS >/dev/null 2>&1); then
    say "  监控机  FAIL 面板包 $PANEL_PACK 不是解开的 $EXP_PANEL_VERSION，或者 sha256 不对"; bad=1
  fi
  [ -f "$ARRANGE_DIR/arrange_boards.py" ] || { say "  监控机  FAIL $ARRANGE_DIR 里没有 arrange_boards.py"; bad=1; }
  local miss; miss=$($PY "$L/mon.py" env "$ENV_FROM" GRAFANA_URL GRAFANA_USER GRAFANA_PASSWORD GRAFANA_CONTAINER DOCKER_NETWORK MONITOR_HOST 2>&1)
  [ -z "$miss" ] || { say "  监控机  FAIL ENV_FROM 里缺 $(echo "$miss" | sed 's/^missing //' | tr '\n' ' ')"; bad=1; }
  [ $bad = 0 ] || fail_stop "① 监控机上的包" "按上面的 FAIL 行放好文件，再执行 run"
  if [ "$(st_get added 0)" = 0 ]; then   # T64 on site; and this cluster has no panel two yet
    if [ "$($PY "$L/mon.py" reg "$CLUSTER" panel2 has)" = yes ]; then
      say "  监控机  FAIL 注册表里已经有 $CLUSTER 的面板二"; fail_stop "①" "先做 T64 现场升级（演练时：先删 $CLUSTER 的面板二）"
    fi
  fi
  local p1=-
  if [ "$PANEL1_CLUSTER" != - ]; then
    p1=$($PY "$L/mon.py" reg "$CLUSTER" panel1 dir)
    [ "$p1" != - ] || { say "  监控机  FAIL 注册表里没有 $CLUSTER 的面板一"; fail_stop "①" "有面板一时清单的 CLUSTER 写面板一的集群名；没有面板一的集群写 PANEL1_CLUSTER=- 和它自己的 PROMETHEUS_* 几行"; }
  fi
  (cd "$PANEL_PACK" && $PY scripts/names.py alloc --cluster "$CLUSTER") > "$SD/port_base" 2> "$SD/log/alloc.log" || { say "  监控机  FAIL 取不到端口基数：$(tail -n 1 "$SD/log/alloc.log")"; fail_stop "①" "带回这一行"; }
  $PY "$L/plan.py" inventory "$LIST" "$SD/inventory.json" "$( [ "$p1" != - ] && echo "$p1/prometheus/inventory.json" )"
  (cd "$PANEL_PACK" && $PY scripts/inventory_tool.py "$SD/inventory.json" csv > "$SD/inv-panel2.csv") 2> "$SD/log/inv.log" \
    || { say "  监控机  FAIL 清单转不成 csv：$(tail -n 1 "$SD/log/inv.log")"; fail_stop "①" "带回这一行"; }
  if [ "$p1" != - ]; then
    (cd "$PANEL_PACK" && $PY scripts/inventory_tool.py "$p1/prometheus/inventory.json" csv > "$SD/inv-panel1.csv") 2>> "$SD/log/inv.log" \
      || { say "  监控机  FAIL 面板一的清单转不成 csv"; fail_stop "①" "带回 $SD/log/inv.log 的最后一行"; }
    local cmp; cmp=$($PY "$L/plan.py" compare "$LIST" "$SD/inv-panel2.csv" "$SD/inv-panel1.csv" | sed 's/^/  监控机  /')
    say "$cmp"
    case $cmp in *FAIL*) fail_stop "① 实例和面板一的比对" "按面板一改清单的顺序或端口" ;; esac
  else
    say "  监控机  INFO 这个集群没有面板一（PANEL1_CLUSTER=-）：不比对实例，面板二写进 $PROMETHEUS_YML"
  fi
  # a fresh snapshot each time ① runs (also after a rollback: other clusters may have changed since the first run)
  $PY "$ARRANGE_DIR/arrange_boards.py" state --env-from "$ENV_FROM" > "$SD/state-before" 2> "$SD/log/state-before.log" \
    || { rm -f "$SD/state-before"; say "  监控机  FAIL arrange_boards.py state 没过"; fail_stop "①" "带回 $SD/log/state-before.log 的最后几行"; }
  say "  监控机  OK   包、面板包、端口基数 $(cat "$SD/port_base")、面板一的状态都记下了"
  push_dir
  local ms; ms=$(names PD)
  if ! stage_on pre pre pre $ms; then fail_stop "①" "按每台的原因处理，再执行 run"; fi
  if [ "$MOONCAKE" = replace ] && grep -qx 'mc_pre=comment_only' $(for n in $ms; do echo "$SD/kv/$n"; done) 2>/dev/null; then
    # every container reported the .comment-only STOP (or ok_replace): the two read-only commands on one P and one D
    local x; x="$(names P | head -n 1) $(names D | head -n 1)"
    say "== ① Mooncake 例外：挑 $x 跑两条只读命令"
    stage_on mcexc mcexc mcexc $x || fail_stop "① Mooncake 例外" "清单改 MOONCAKE=skip 再执行 run（P 后台 put 下 3 段为空），或者带回这几行"
  fi
  if [ "$PROXY" = on ]; then stage_on pxpre pxpre pxpre proxy || fail_stop "① proxy" "按原因处理，再执行 run"; fi
  local n; for n in $ms; do say "  $n 启动文件 $(basename "$(kvget "$n" start)")：$( [ "$(kvget "$n" exports)" = absent ] && echo "第 $(kvget "$n" insert) 行前加两行 export" || echo "两行 export 已在")"; done | awk '!seen[substr($0, index($0, " 启动文件"))]++' | head -n 4
  st_set stage prep
}

# ==== ② backup, p2d, the panel's add ============================================================================
s_prep() {
  say "== ② 备份、p2d 重放、装面板二（集群照常运行）"
  [ -d "$SD/push/lib" ] || push_dir
  local ms; ms=$(names PD)
  stage_on prep prep prep $ms || fail_stop "②" "按原因处理，再执行 run"
  local n s0 bad=0
  s0=$(kvget "$(names D | head -n 1)" s58)
  for n in $ms; do [ "$(kvget "$n" s58)" = "$s0" ] || { say "  $n  FAIL p2d 结果和 $(names D | head -n 1) 不同"; bad=1; }; done
  for n in $ms; do   # the monitor's copy of every backup
    rm -rf "$SD/back/$n" && scp -q -r "${SSHO[@]}" "root@$(mf "$n" addr):$T/out/$n" "$SD/back/$n" && (cd "$SD/back/$n" && sha256sum -c --quiet SHA256SUMS >/dev/null 2>&1) \
      || { say "  $n  FAIL 备份拷不回监控机，或 sha256 不对"; bad=1; }
  done
  [ $bad = 0 ] || fail_stop "②" "带回这几行"
  say "  监控机  OK   $(echo $ms | wc -w) 份备份都拷回来了，p2d 结果都是 ${s0:0:16}"
  local sets=(--set SEGMENTS_PROFILE=glm53 --set SSH_USER=root --set NODE_CONF_DIR=$NODE_DIR/conf --set NODE_LOG_DIR=$NODE_DIR/logs)
  # T53_ADD_SETS: the lab's small disk floors (the lab VM has no 1000 GB); never set on site
  [ -n "${T53_ADD_SETS:-}" ] && for x in $T53_ADD_SETS; do sets+=(--set "$x"); done
  for k in PROMETHEUS_URL PROMETHEUS_CONTAINER PROMETHEUS_YML; do [ -n "${!k}" ] && sets+=(--set "$k=${!k}"); done
  [ "$PROXY" = on ] && sets+=(--set PROXY_COLLECT=1 --set "PROXY_MACHINE=$(mf proxy addr)" --set "PROXY_LOG_DIR=$PROXY_LOG_DIR" --set "PULL_RSYNC_FLAGS=--append --exclude=/proxy/")
  st_set added 1
  (cd "$PANEL_PACK" && bin/panel2_cluster.sh add "$CLUSTER" --model-group "$MODEL_GROUP" --mode vllm --port-base "$(cat "$SD/port_base")" \
     --inventory "$SD/inventory.json" --env-from "$ENV_FROM" "${sets[@]}") > "$SD/log/add.log" 2>&1
  mon_line $PY "$L/mon.py" add "$SD/log/add.log" || fail_stop "② 装面板二" "带回 $SD/log/add.log 里的 FAIL 行；处理后再执行 run（add 可以重跑）"
  stage_on prep2 prep2 prep2 $ms || fail_stop "② 节点文件和日志目录" "按原因处理，再执行 run"
  # the proxy check sees /data/pd-diag/logs only after the add made it
  if [ "$PROXY" = on ]; then
    stage_on pxprep pxprep pxprep proxy || fail_stop "② proxy" "按原因处理，再执行 run"
    rm -rf "$SD/back/proxy" && mkdir -p "$SD/back/proxy" && scp -q "${SSHO[@]}" "root@$(mf proxy addr):$T/out/proxy/orig.py" "$SD/back/proxy/orig.py" \
      && [ "$(sha256sum "$SD/back/proxy/orig.py" | cut -c1-64)" = "$PROXY_ORIG_SHA256" ] || fail_stop "② proxy" "proxy 原文件拷不回监控机"
  fi
  st_set stage stopped
  say "结论：备份和准备都好了。"
  [ "$PROXY" = on ] && say "整启前：在你们起 proxy 的启动脚本里加两个环境变量 PD_PROXY_DIAG=1、PD_PROXY_DIAG_DIR=$PROXY_LOG_DIR（脚本不改它；⑤ 核对 proxy 进程带没带）。"
  say "下一步：请整停集群（vLLM$( [ "$PROXY" = on ] && echo " 和 proxy")；不要 docker rm 容器），停完再执行 run。"
  exit 0
}

# ==== ③ stopped? ④ swap ==========================================================================================
s_stopped() {
  say "== ③ 核对集群已停"
  local ms; ms=$(names PD)
  stage_on stopped stopped stopped $ms || fail_stop "③" "停完再执行 run；容器被重建过的：执行 rollback 不行，带回这几行"
  if [ "$PROXY" = on ]; then
    job proxy pxstop pxpre; grep -q '^proxy_procs=0$' "$SD/log/pxstop.proxy.log" || { say "  proxy  FAIL proxy 主进程还在"; fail_stop "③" "停掉 proxy 再执行 run"; }
    say "  proxy  OK   proxy 已停"
  fi
  st_set stop_epoch "$(date +%s)"
  say "== ④ 换文件（先第一个 D，再其余并行）"
  local d1 rest; d1=$(names D | head -n 1); rest=$(names PD | grep -vx "$d1")
  st_set stage swapping
  FIRST=1 stage_on swap swap swap "$d1" -- FIRST=1 || { st_set rb_stage stopped; fail_stop "④ 第一个 D" "其余容器没动。执行 rollback（集群停着，直接还原）"; }
  [ "$MOONCAKE" = replace ] && st_set mc_swapped 1
  stage_on swap swap swap $rest || { st_set rb_stage stopped; fail_stop "④" "执行 rollback（集群停着，直接还原）"; }
  if [ "$PROXY" = on ]; then stage_on pxswap pxswap pxswap proxy || { st_set rb_stage stopped; fail_stop "④ proxy" "执行 rollback"; }; fi
  st_set stage started
  say "结论：文件都换好了。"
  say "下一步：请整启集群（启动方式和平时相同，不加 MC_LOG_DIR、PD_DIAG_LEVEL$( [ "$PROXY" = on ] && echo "；proxy 用已加 PD_PROXY_DIAG=1、PD_PROXY_DIAG_DIR=$PROXY_LOG_DIR 的启动脚本起")），起来以后再执行 run。"
  exit 0
}

# ==== ⑤ started ===================================================================================================
wait_health() {   # stage judge cmd machines: retry while only /health is short, up to 10 minutes
  local stg=$1 j=$2 c=$3; shift 3
  local t0; t0=$(date +%s)
  while :; do
    : > "$SD/screen.tmp"
    if stage_on "$stg" "$j" "$c" "$@" > "$SD/screen.tmp"; then cat "$SD/screen.tmp"; return 0; fi
    if grep -q 'FAIL' "$SD/screen.tmp" && ! grep 'FAIL' "$SD/screen.tmp" | grep -vq '/health'; then
      if [ $(( $(date +%s) - t0 )) -lt 600 ]; then sleep 30; continue; fi
    fi
    cat "$SD/screen.tmp"; return 1
  done
}

s_started() {
  say "== ⑤ 整启以后的核对"
  wait_health started started started $(names PD) || fail_stop "⑤" "要回滚就执行 rollback"
  if [ "$PROXY" = on ]; then stage_on pxstarted pxstarted pxstarted proxy || fail_stop "⑤ proxy" "proxy 按原来的方式重启，带上两个变量，再执行 run"; fi
  st_set start_epoch "$(date +%s)"
  st_set ch_t0 "$(echo "SELECT toString(now64(3, 'UTC'))" | chq)"
  say "  监控机  OK   数据库时间 $(st_get ch_t0) UTC 记为拉起时刻"
  st_set stage final1
}

# ==== ⑥ boards and checks ========================================================================================
s_final1() {
  local d; d=$(p2dir)
  local w=$(( $(st_get start_epoch 0) + 300 - $(date +%s) ))
  [ $w -gt 0 ] && { say "== ⑥ 等到拉起 5 分钟（$(hm $(( $(date +%s) + w )))）"; sleep $w; }
  say "== ⑥ 看板归位和核对（监控机）"
  local bad=0 e
  $PY "$ARRANGE_DIR/arrange_boards.py" plan --env-from "$d/deploy.env" > "$SD/log/arrange-plan.log" 2>&1
  $PY "$L/mon.py" arrange "$SD/log/arrange-plan.log" "$CLUSTER" > "$SD/res/arrange" ; e=$?
  if [ $e = 0 ]; then
    $PY "$ARRANGE_DIR/arrange_boards.py" apply --env-from "$d/deploy.env" > "$SD/log/arrange-apply.log" 2>&1
    grep -q '^RESULT: OK' "$SD/log/arrange-apply.log" && say "  监控机  $(cat "$SD/res/arrange")，已改" || { say "  监控机  FAIL 看板归位 apply 没过"; bad=1; }
  elif [ $e = 3 ]; then say "  监控机  OK   看板已在位"; else say "  监控机  $(cat "$SD/res/arrange")"; bad=1; fi
  (cd "$d" && bin/panel2_cluster.sh check "$CLUSTER") > "$SD/log/check.log" 2>&1
  mon_line $PY "$L/mon.py" check "$SD/log/check.log" "$NTOT" || bad=1
  sqlf selfcheck | chq > "$SD/log/selfcheck.tsv"
  mon_line $PY "$L/mon.py" selfcheck "$SD/log/selfcheck.tsv" "$NTOT" || bad=1
  (cd "$d" && bin/coverage.sh 200 30) > "$SD/log/coverage.log" 2>&1
  mon_line $PY "$L/mon.py" coverage "$SD/log/coverage.log" "$PROXY" "$MOONCAKE" "${T53_LAB_COVERAGE_EMPTY:-}" || bad=1
  stage_on late late late $(names PD) || bad=1
  local n; for n in $(names PD); do kvset "$n" disk0 "$(sed -n 's/^now \([0-9]*\) logs_bytes \([0-9]*\)$/\1:\2/p' "$SD/log/late.$n.log")"; done
  $PY "$ARRANGE_DIR/arrange_boards.py" state --env-from "$d/deploy.env" > "$SD/state-after" 2> "$SD/log/state-after.log"
  mon_line $PY "$L/mon.py" statediff "$SD/state-before" "$SD/state-after" "$CLUSTER" "$PANEL1_CLUSTER" "$(sed -n 's/^GRAFANA_CONTAINER=//p' "$ENV_FROM" | tail -n 1)" || bad=1
  local t0 inst; t0=$(st_get ch_t0)
  inst=$(echo "SELECT instance FROM vllm_pd.phase_line_cur WHERE role = 'decode' AND ts >= toDateTime64('$t0', 3, 'UTC') GROUP BY instance ORDER BY uniqExact(request_id) DESC LIMIT 1 FORMAT TSV" | chq)
  sqlf first_batch | sed -e "s/'<D 实例>'/'$inst'/" -e "s/'<开始时刻>'/'$t0'/" \
    -e 's/countIf(sp1 > 0 AND six1 > sp1)/countIf(sp1 > 0 AND six1 > sp1 * 1.02)/' \
    -e 's/countIf(sp > sp1 AND six - six1 > sp - sp1)/countIf(sp > sp1 AND six - six1 > (sp - sp1) * 1.02)/' | chq > "$SD/log/first.tsv"
  say "  监控机  $($PY "$L/mon.py" first "$SD/log/first.tsv" "${inst:-?}")"
  sqlf empty1 | chq > "$SD/log/empty1.tsv"
  st_set t10 "$(awk -F'\t' 'NR == 1 { for (i = 1; i <= NF; i++) if ($i == "t10_utc") c = i } NR == 2 { print $c }' "$SD/log/empty1.tsv")"
  st_set t10_epoch "$(date +%s)"
  [ -n "$(st_get t10)" ] || { say "  监控机  FAIL 留空比例第一次没有结果"; bad=1; }
  [ $bad = 0 ] || fail_stop "⑥" "带回上面几行；要回滚就执行 rollback"
  st_set stage final2
  say "结论：看板和核对都过了；留空比例第一次已记下（$(st_get t10) UTC）。"
  say "下一步：请在 $(hm $(( $(st_get t10_epoch) + 1800 ))) 以后再执行 run（留空比例和磁盘第二次）。"
  exit 0
}

promq() {   # panel one's Prometheus: one instant value (seconds) of $1 at epoch $2
  local port; port=$($PY "$L/mon.py" reg "$PANEL1_CLUSTER" panel1 port)
  $PY -c '
import json, sys, urllib.parse, urllib.request
o = urllib.request.build_opener(urllib.request.ProxyHandler({}))
q = urllib.parse.urlencode({"query": sys.argv[2], "time": sys.argv[3]})
try:
    r = json.load(o.open("http://localhost:%s/api/v1/query?%s" % (sys.argv[1], q), timeout=10))["data"]["result"]
    v = float(r[0]["value"][1]); print("-" if v != v else "%.0f" % (v * 1000))
except Exception:
    print("-")' "$port" "$1" "$2"
}

s_final2() {
  local due=$(( $(st_get t10_epoch 0) + 1800 ))
  [ "$(date +%s)" -ge $due ] || { say "还没到 $(hm $due)：留空比例要隔约 30 分钟。到时再执行 run。"; exit 0; }
  say "== ⑥ 第二次：留空比例、磁盘"
  local bad=0
  sed -e "s/'<第一次的 t10_utc>'/'$(st_get t10)'/" "$L/sql/empty2.sql" | sed 's/FORMAT PrettyCompactMonoBlock/FORMAT TSVWithNames/' | chq > "$SD/log/empty2.tsv"
  mon_line $PY "$L/mon.py" empty "$SD/log/empty1.tsv" "$SD/log/empty2.tsv" "${T53_LAB_EMPTY_REPORT_ONLY:-0}" || bad=1
  stage_on late2 late late $(names PD) || bad=1
  local n r g=""; for n in $(names PD); do
    r=$(kvget "$n" disk0); local t1 b1; read -r t1 b1 < <(sed -n 's/^now \([0-9]*\) logs_bytes \([0-9]*\)$/\1 \2/p' "$SD/log/late2.$n.log")
    [ -n "$r" ] && [ -n "$t1" ] && g="$g $n:$(awk -v t=$((t1 - ${r%%:*})) -v b=$((b1 - ${r##*:})) 'BEGIN { printf "%.1f", b / t * 86400 / 1e9 }')"
  done
  say "  磁盘    节点目录每天 GB：$(echo $g | tr ' ' '\n' | sort -t: -k2 -n -r | head -n 4 | tr '\n' ' ')（最多的 4 台，只记）"
  local now; now=$(date +%s)
  [ "$PANEL1_CLUSTER" = - ] && { [ $bad = 0 ] || fail_stop "⑥ 第二次" "带回上面几行；要回滚就执行 rollback"; st_set stage done; say "结论：全部做完。面板二 $CLUSTER 在用，$NTOT 个实例都是 T58（没有面板一，不看 TPOT）。"; exit 0; }
  local s0; s0=$(st_get stop_epoch)
  say "  面板一  TPOT P50（D，30 分钟）：停机前 $(promq 'histogram_quantile(0.5, sum by (le) (increase(vllm:request_time_per_output_token_seconds_bucket{role="decode", pd_instance=~".+"}[30m])))' "$s0") ms，现在 $(promq 'histogram_quantile(0.5, sum by (le) (increase(vllm:request_time_per_output_token_seconds_bucket{role="decode", pd_instance=~".+"}[30m])))' "$now") ms（观测，不判）"
  [ $bad = 0 ] || fail_stop "⑥ 第二次" "带回上面几行；要回滚就执行 rollback"
  st_set stage done
  say "结论：全部做完。面板二 $CLUSTER 在用，$NTOT 个实例都是 T58。"
  exit 0
}

# ==== rollback ======================================================================================================
r_main() {
  local rs; rs=$(st_get rb_stage "")
  local stg; stg=$(st_get stage pre)
  case $stg in pre|prep) say "还没有备份（停在 $stg），不用回滚。"; exit 0 ;; esac
  push_dir
  case $rs in
  ""|done)
    say "== 回滚 1：核对备份（集群照常运行）"
    local n bad=0
    for n in $(names PD); do (cd "$SD/back/$n" && sha256sum -c --quiet SHA256SUMS >/dev/null 2>&1) || { say "  $n  FAIL 监控机上的备份不对"; bad=1; }; done
    [ $bad = 0 ] || fail_stop "回滚 1" "带回这几行"
    stage_on rb_check rb_check rb_check $(names PD) || fail_stop "回滚 1" "带回这几行"
    st_set rb_stage stopped
    say "下一步：请整停集群（不要 docker rm 容器），停完再执行 rollback。"; exit 0 ;;
  stopped)
    say "== 回滚 2：还原文件（集群停着）"
    stage_on rb_restore rb_restore rb_restore $(names PD) || fail_stop "回滚 2" "停完再执行 rollback；带回这几行"
    if [ "$PROXY" = on ] && [ -f "$SD/back/proxy/orig.py" ]; then
      scp -q "${SSHO[@]}" "$SD/back/proxy/orig.py" "root@$(mf proxy addr):$T/out/proxy/orig.py"
      stage_on pxrestore pxrestore pxrestore proxy || fail_stop "回滚 2 proxy" "带回这几行"
    fi
    st_set mc_swapped 0
    st_set rb_stage started
    say "下一步：请整启集群（和打补丁以前的启动方式相同$( [ "$PROXY" = on ] && echo "；proxy 不带 PD_PROXY_DIAG 两个变量")），起来以后再执行 rollback。"; exit 0 ;;
  started)
    say "== 回滚 3：整启以后的核对"
    wait_health rb_started rb_started rb_started $(names PD) || fail_stop "回滚 3" "带回这几行"
    if [ "$PROXY" = on ]; then stage_on rbpx pxpre pxpre proxy || fail_stop "回滚 3 proxy" "proxy 要用原文件、不带 PD_PROXY_DIAG 变量起来；起好再执行 rollback"; fi
    st_set rb_stage done; st_set stage pre
    say "结论：回到打补丁以前。面板二 $CLUSTER 留着（不影响 vLLM）。要重新打补丁就再执行 run，从 ① 开始。"; exit 0 ;;
  esac
}

# ==== status, dry run ===============================================================================================
s_status() {
  local stg; stg=$(st_get stage pre)
  say "集群 $CLUSTER：P $N_P 台 $P_TOTAL 个实例，D $N_D 台 $D_TOTAL 个实例；MOONCAKE=$MOONCAKE PROXY=$PROXY"
  say "停在：$stg$( [ -n "$(st_get rb_stage "")" ] && echo "；回滚：$(st_get rb_stage)")"
  local last; last=$(ls -t "$SD/res" 2>/dev/null | head -n 1 | cut -d. -f1)
  [ -n "$last" ] && { say "上一段 $last："; for n in $(names PD) proxy; do [ -f "$SD/res/$last.$n" ] && say "  $n  $(cat "$SD/res/$last.$n")"; done; }
  case $stg in
    pre|prep) say "下一步：执行 run" ;; stopped) say "下一步：整停集群，再执行 run" ;; swapping) say "下一步：执行 rollback" ;;
    started) say "下一步：整启集群，再执行 run" ;; final1) say "下一步：执行 run" ;;
    final2) say "下一步：$(hm $(( $(st_get t10_epoch 0) + 1800 ))) 以后执行 run" ;; done) say "已全部做完" ;;
  esac
}

s_dry() {
  local stg; stg=$(st_get stage pre)
  say "（只列出接下来要做的事，不连任何机器）集群 $CLUSTER：P $N_P 台 $P_TOTAL 个实例，D $N_D 台 $D_TOTAL 个实例，MOONCAKE=$MOONCAKE，PROXY=$PROXY；停在 $stg"
  case $stg in pre) say "① 预检：监控机核对包、面板包、端口基数、和面板一比实例；$((N_P + N_D)) 台机器的容器里只读核对$( [ "$MOONCAKE" = replace ] && echo "、换库前核对")$( [ "$PROXY" = on ] && echo "；proxy 那台核对原文件")" ;; esac
  case $stg in pre|prep) say "② 每个容器备份 23+10 个文件和启动文件$( [ "$MOONCAKE" = replace ] && echo "、Mooncake 库")，p2d 重放；监控机装面板二（端口基数 $(cat "$SD/port_base" 2>/dev/null || echo 待取)）；然后停下，请你整停集群" ;; esac
  case $stg in pre|prep|stopped) say "③④ 核对都停了；先第一个 D 换文件和导入核对，再其余 $((N_P + N_D - 1)) 台；每个启动文件加两行 export；然后停下，请你整启集群" ;; esac
  case $stg in pre|prep|stopped|started) say "⑤ /health、命令行、检测脚本、启动行；⑥ 看板归位、check、自检、各段有值、首批数据、留空比例第一次；约 30 分钟后再 run 一次" ;; esac
  exit 0
}

# ==== remove-panel (rehearsal: the old test-glm53 panel two goes first) ===========================================
s_remove() {
  local d; d=$(p2dir)
  [ "$d" != - ] || { say "注册表里没有 $CLUSTER 的面板二，不用删。"; exit 0; }
  local others; others=$($PY "$L/mon.py" reg "$CLUSTER" panel2 others)
  say "这台监控机上另外的面板二集群：$others"
  [ "$others" = - ] && say "$CLUSTER 是唯一的面板二集群：删它时面板包删掉 Grafana 的 ClickHouse 插件并重启 Grafana（面板一的看板断几十秒，数据不丢），装 $CLUSTER 的新面板二时再装回插件、再重启一次。"
  local keep; keep=$STATE/removed-$CLUSTER-$(date +%Y%m%d-%H%M%S)
  mkdir -p "$keep" && cp -p "$d/deploy.env" "$d/inventory.json" "$keep/" || { say "FAIL deploy.env 拷不出来，没删"; exit 1; }
  (cd "$d" && bin/panel2_cluster.sh remove "$CLUSTER") > "$keep/remove.log" 2>&1
  if ! grep -q "^RESULT: OK（集群 $CLUSTER 的面板二已卸载）" "$keep/remove.log" && [ -d "$d" ] \
     && grep -q '^OK    Grafana 插件 .*Grafana 重启了一次' "$keep/remove.log"; then
    # Removing the last panel two drops the ClickHouse plugin and restarts Grafana; the pack's check then runs at once and
    # can find Grafana still starting (HTTP 0). Wait for Grafana, run the pack's read-only check again, and when it
    # passes do what panel2_cluster.sh remove does after it: unregister the cluster's panel two and delete its folder.
    local g; g=$(sed -n 's/^GRAFANA_URL=//p' "$keep/deploy.env" | tail -n 1)
    for _ in $(seq 1 36); do curl -s -m 3 --noproxy '*' "$g/api/health" 2>/dev/null | grep -q '"database": *"ok"' && break; sleep 5; done
    sleep 10
    (cd "$d" && bin/uninstall_old.sh check "$d") > "$keep/check2.log" 2>&1
    if [ "$(grep '^RESULT:' "$keep/check2.log" | tail -n 1)" = "RESULT: OK" ]; then
      (cd "$d" && $PY scripts/names.py unregister --cluster "$CLUSTER" --panel panel2) >> "$keep/check2.log" 2>&1 \
        && find "$d" -mindepth 1 -delete && rmdir "$d" && echo "RESULT: OK（集群 $CLUSTER 的面板二已卸载）" >> "$keep/remove.log"
    else
      grep '^RESULT:' "$keep/check2.log" | tail -n 1 >> "$keep/remove.log"
    fi
  fi
  local r; r=$(grep '^RESULT:' "$keep/remove.log" | tail -n 1)
  case $r in "RESULT: OK（集群 $CLUSTER 的面板二已卸载）"*) ;; *) say "FAIL remove 没过：${r:-没有 RESULT 行}（全文 $keep/remove.log）"; exit 1 ;; esac
  say "OK   $CLUSTER 的面板二已卸载。它的 deploy.env 留在 $keep/deploy.env：演练清单的 ENV_FROM 写这个路径。"
  if [ "$($PY "$L/mon.py" reg "$CLUSTER" panel1 has)" != yes ]; then
    say "$CLUSTER 没有面板一：演练清单里写这几行（它自己的 Prometheus 留着，remove 不动它）"
    say "PANEL1_CLUSTER=-"
    grep -E '^PROMETHEUS_(URL|CONTAINER|YML)=.' "$keep/deploy.env" | tee -a "$SD/screen.log"
  fi
  [ -d "$SD" ] && mv "$SD" "$SD.before-remove-$(date +%Y%m%d-%H%M%S)"
  exit 0
}

[ "$CMD" = status ] && { s_status; exit 0; }
[ "$CMD" = rollback ] && { r_main; exit 0; }
[ "$CMD" = remove-panel ] && s_remove
[ $DRY = 1 ] && s_dry
: > "$SD/screen.log"
[ -n "$(st_get rb_stage "")" ] && [ "$(st_get rb_stage)" != done ] && { say "回滚做到一半（$(st_get rb_stage)）：接着执行 rollback。"; exit 1; }
while :; do
  case $(st_get stage pre) in
    pre) s_pre ;; prep) s_prep ;; stopped) s_stopped ;; swapping) say "④ 换文件做到一半：执行 rollback。"; exit 1 ;;
    started) s_started ;; final1) s_final1 ;; final2) s_final2 ;; done) say "已全部做完。"; exit 0 ;;
  esac
done
