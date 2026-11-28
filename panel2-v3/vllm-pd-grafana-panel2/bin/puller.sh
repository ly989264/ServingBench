#!/bin/bash
# The puller on the monitoring machine: mirrors the log folder of every machine with rsync over SSH, for the collector to read.
# Usage: bin/puller.sh start|stop|restart|status
. "$(dirname "$0")/lib.sh"
load_env
node_home=$(grep -E '^TIMING_HOME=' "$PACK_DIR/deploy.env" | cut -d= -f2- | sed 's|^\$HOME|~|')
need_python
need_inventory
RUN="$TIMING_HOME/puller"
LOOP="$RUN/loop.pid"
stop_puller() {
  # The loop, the puller and the rsync copies it started (they write into PULL_DIR, so bin/clear_data.sh needs them gone):
  # SIGTERM, up to 10 s, then SIGKILL to what is left.
  local p pids=() i=0 left
  p=$(running_pid "$LOOP") && pids+=("$p")
  p=$(running_pid "$RUN/puller.pid") && pids+=("$p")
  [ ${#pids[@]} -gt 0 ] && kill "${pids[@]}" 2>/dev/null
  alive() { local q; for q in "${pids[@]+"${pids[@]}"}"; do kill -0 "$q" 2>/dev/null && return 0; done; pgrep -f "rsync .*$PULL_DIR/" >/dev/null 2>&1; }
  while [ "$i" -lt 10 ] && alive; do sleep 1; i=$((i + 1)); done
  if alive; then
    [ ${#pids[@]} -gt 0 ] && kill -9 "${pids[@]}" 2>/dev/null
    pkill -9 -f "rsync .*$PULL_DIR/" 2>/dev/null
    sleep 1
  fi
  rm -f "$LOOP" "$RUN/puller.pid"
}
start_puller() {
  command -v rsync >/dev/null 2>&1 || fail "这台机器没有 rsync。先安装（yum install rsync 或 apt install rsync）。"
  names_check
  make_pull_dir
  mkdir -p "$RUN" || fail "建不了 $RUN"
  export INVENTORY PULL_DIR NODE_TIMING_HOME="$node_home" PULLER_STATE_DIR="$RUN/state"
  # Keeps it running if it crashes.
  nohup bash -c 'while true; do "$0" "$1" & p=$!; echo $p > "$2"; wait $p; sleep 5; done' "$PYTHON" "$PACK_DIR/scripts/puller.py" "$RUN/puller.pid" >>"$RUN/puller.out" 2>&1 &
  echo $! > "$LOOP"
  sleep 2
  running_pid "$LOOP" >/dev/null && running_pid "$RUN/puller.pid" >/dev/null || { tail -n 10 "$RUN/puller.out" >&2; fail "拉取程序没有在运行，看上面的输出"; }
  echo "拉取程序已启动，每 ${PULL_INTERVAL_S:-10} 秒拉一轮，镜像到 $PULL_DIR"
  echo "RESULT: OK"
}
case "${1:-}" in
  start|restart) stop_puller; start_puller ;;
  stop) stop_puller; echo "拉取程序已停止" ;;
  status)
    # One line per machine, then RESULT; exit 1 when the puller does not answer or a machine was not pulled for 2 minutes.
    st=0
    "$PYTHON" - "$MONITOR_HOST" "$PULLER_METRICS_PORT" 2>/dev/null <<'PY' || st=1
import json, sys, urllib.request as u
try:
    rows = json.load(u.urlopen("http://%s:%s/status" % (sys.argv[1], sys.argv[2]), timeout=5))
except Exception:  # noqa: BLE001
    print("FAIL 拉取程序没有运行（%s:%s 没有应答）→ bin/puller.sh start" % (sys.argv[1], sys.argv[2]))
    sys.exit(1)
bad = 0
for m in rows:
    ok = m["age"] is not None and m["age"] < 120
    bad += 0 if ok else 1
    print("%-4s %-16s %-14s 上次成功 %s 秒前  %s" % ("OK" if ok else "FAIL", m["addr"], m["node"], m["age"], m["error"][:80]))
sys.exit(1 if bad else 0)
PY
    du -sh "$PULL_DIR" 2>/dev/null | cut -f1 | sed 's/^/镜像占用 /'
    if [ "$st" = 0 ]; then echo "RESULT: OK"; else echo "RESULT: FAIL"; exit 1; fi
    ;;
  *) fail "用法：bin/puller.sh start|stop|restart|status" ;;
esac
