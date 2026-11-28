#!/bin/bash
# Starts or stops the mock writer on this machine (intranet without a model). Normally called over SSH by bin/nodes.sh on the monitoring machine.
# The writer loads the three node files the monitoring machine pushed to NODE_CONF_DIR and writes, through them, to NODE_LOG_DIR,
# one process per instance of this machine, at the current time.
# Usage: bin/node_mock.sh start <address> | stop | clean (stop, delete its logs) | purge (stop, delete TIMING_HOME) | status
. "$(dirname "$0")/lib.sh"
load_env
RUN="$TIMING_HOME/run"
PIDFILE="$RUN/mock.pid"
CONF=${NODE_CONF_DIR:-$HOME/vllm-pd-diag/conf}
LOGS=${NODE_LOG_DIR:-$HOME/vllm-pd-diag/logs}
# Written as pd032_mock[.]py so that they never match a shell whose command line holds them.
ALL='pd032_mock[.]py run'
INST='pd032_mock[.]py run-instance'

count() { pgrep -f "$1" 2>/dev/null | wc -l | tr -d ' '; }

stop_mock() {
  # SIGTERM to the parent and every run-instance, a count each second for up to 10 s, SIGKILL to what is left, 2 s,
  # and a last count (the same as scripts/nodes.py on the monitoring machine). Returns 1 when processes are left.
  local n pids left i=0 forced=0 p
  n=$(count "$INST")
  pids=$(pgrep -f "$ALL" 2>/dev/null)
  if [ -n "$pids" ]; then
    # shellcheck disable=SC2086
    kill -TERM $pids 2>/dev/null
    while [ "$i" -lt 10 ] && [ -n "$(pgrep -f "$ALL" 2>/dev/null)" ]; do sleep 1; i=$((i + 1)); done
    left=$(pgrep -f "$ALL" 2>/dev/null)
    if [ -n "$left" ]; then
      forced=$(count "$INST")
      # shellcheck disable=SC2086
      kill -KILL $left 2>/dev/null
      sleep 2
    fi
  fi
  left=$(pgrep -f "$ALL" 2>/dev/null)
  if [ -n "$left" ]; then
    for p in $left; do echo "还在：pid $p 启动 $(ps -o lstart= -p "$p" 2>/dev/null) 用户 $(ps -o user= -p "$p" 2>/dev/null)" >&2; done
    echo "错误：还有 $(count "$ALL") 个 mock 进程停不掉 → kill -9 $(echo $left)；是别的用户的进程时 sudo kill -9 $(echo $left)" >&2
    return 1
  fi
  rm -f "$PIDFILE"
  if [ "$n" = 0 ] && [ -z "$pids" ]; then STOPPED="没有 mock 进程在运行"
  elif [ "$forced" -gt 0 ]; then STOPPED="已停止：$n 个进程，其中 $forced 个强制结束（最后几秒的数据会少）"
  else STOPPED="已停止：$n 个进程，都正常退出"; fi
}

slow_delete() {
  # Every file under $1, one at a time, 0.2 s apart, at the lowest CPU priority, with progress every 5 s (T32 C1).
  # Returns 1 when a file could not be deleted (another owner in a sticky folder, read-only disk...).
  local dir=$1 total kib done=0 failed=0 first="" gone=0 s t0 last now el
  [ -d "$dir" ] || { DELETED="0 个文件（$dir 不存在）"; return 0; }
  renice -n 19 -p $$ >/dev/null 2>&1
  total=$(find "$dir" -type f 2>/dev/null | wc -l | tr -d ' ')
  kib=$(du -sk "$dir" 2>/dev/null | cut -f1)
  t0=$(date +%s); last=$t0
  echo "删除 $dir：$total 个文件，$((kib / 1024)) MiB，每个文件之间停 0.2 秒，预计 $((total / 5)) 秒"
  while IFS= read -r -d '' f; do
    s=$(stat -c %s "$f" 2>/dev/null || echo 0)
    if rm -f -- "$f" 2>/dev/null && [ ! -e "$f" ]; then
      done=$((done + 1)); gone=$((gone + s / 1024))
    else
      failed=$((failed + 1)); [ -n "$first" ] || first=$f
    fi
    sleep 0.2
    now=$(date +%s)
    if [ $((now - last)) -ge 5 ]; then
      last=$now; el=$((now - t0))
      echo "进度 $((done + failed))/$total 个文件，$((gone / 1024))/$((kib / 1024)) MiB，预计还要 $((el * (total - done - failed) / (done + failed))) 秒"
    fi
  done < <(find "$dir" -type f -print0 2>/dev/null)
  find "$dir" -mindepth 1 -depth -type d -empty -delete 2>/dev/null
  DELETED="$done 个文件、$((gone / 1024)) MiB，用时 $(( $(date +%s) - t0 )) 秒"
  if [ "$failed" -gt 0 ]; then
    echo "错误：$dir 里 $failed 个文件删不了（比如 $first，属主 $(stat -c %U "$first" 2>/dev/null)，这里是 $(id -un)）→ 用文件的属主或 root 删掉，再重跑" >&2
    return 1
  fi
}

case "${1:-}" in
  stop)
    stop_mock || exit 1
    echo "mock $STOPPED"
    ;;
  clean)
    stop_mock || fail "mock 没有停干净，日志没有删"
    slow_delete "$LOGS" || fail "mock 的日志没有删完（已删 $DELETED）"
    echo "mock 的日志已删除：$DELETED"
    ;;
  purge)
    stop_mock || fail "mock 没有停干净，什么都没有删"
    slow_delete "$LOGS" || fail "mock 的日志没有删完（已删 $DELETED），$TIMING_HOME 没有删"
    if [ -f "$TIMING_HOME/.vllm-timing-home" ]; then find "$TIMING_HOME" -mindepth 1 -delete && rmdir "$TIMING_HOME"; fi
    echo "mock 的日志已删除：$DELETED；$TIMING_HOME 已删除"
    ;;
  status)
    echo "mock 进程 $(count "$INST") 个"
    ;;
  start)
    need_python
    target=${2:-}
    [ -n "$target" ] || fail "用法：bin/node_mock.sh start <本机在清单里的地址>"
    for f in pd_diag_logging.json pd_diag_logfilter.py segments.json; do
      [ -f "$CONF/$f" ] || fail "$CONF 里没有 $f。先在监控机上执行 bin/nodes.sh push"
    done
    stop_mock || fail "旧的 mock 进程没有停干净，没有启动"
    # The whole model's requests a second; blank means one per D instance of the inventory (bin/nodes.sh sends the value).
    rate=${MOCK_RATE:-$("$PYTHON" -c 'import json, sys; print(len(json.load(open(sys.argv[1]))["decode"]["api_servers"]))' "$INVENTORY")}
    mkdir -p "$RUN" "$LOGS" || fail "建不了目录 $RUN 或 $LOGS"
    touch "$TIMING_HOME/.vllm-timing-home"
    extra=()
    [ -n "${MOCK_P_RANKS:-}" ] && extra+=(--p-ranks "$MOCK_P_RANKS")
    [ -n "${MOCK_STEP_EVENTS:-}" ] && extra+=(--step-events "$MOCK_STEP_EVENTS")
    [ -n "${MOCK_CLOCK_OFFSET_S:-}" ] && extra+=(--clock-offset-s "$MOCK_CLOCK_OFFSET_S")
    [ -n "${MOCK_SECONDS:-}" ] && extra+=(--seconds "$MOCK_SECONDS")
    [ -n "${MOCK_TRUTH_DIR:-}" ] && extra+=(--truth-dir "$MOCK_TRUTH_DIR")
    nohup nice -n 19 "$PYTHON" "$PACK_DIR/mock/pd032_mock.py" run --inventory "$INVENTORY" --host "$target" --node-dir "$CONF" --log-dir "$LOGS" \
      --rate "$rate" --seed "${MOCK_SEED:-20261111}" --stats-dir "$RUN/stats" "${extra[@]+"${extra[@]}"}" >"$RUN/mock.out" 2>&1 &
    echo $! > "$PIDFILE"
    sleep 2
    running_pid "$PIDFILE" >/dev/null || [ -n "${MOCK_SECONDS:-}" ] || { cat "$RUN/mock.out" >&2; fail "mock 没有在运行，看上面的输出"; }
    echo "mock 已启动：$target，$(head -n 1 "$RUN/mock.out" 2>/dev/null)，日志 $LOGS"
    ;;
  *) fail "用法：bin/node_mock.sh start <地址> | stop | clean | purge | status" ;;
esac
