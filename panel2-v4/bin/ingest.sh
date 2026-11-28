#!/bin/bash
# The ingest process of this cluster (ingest/pd_ingest.py): reads the documents from the fetcher (ingest.json fetcher_url)
# and writes them into ClickHouse. Reads ingest.json and clickhouse.env. It keeps no state of its own: the cursor is the
# largest seq in the tables, so a restart continues where the tables end.
#   bin/ingest.sh start | stop | restart | status
# A loop restarts it 5 s after it exits. Output: logs/ingest.log in the cluster folder.
. "$(dirname "$0")/lib.sh"
load_ch_env
need_python
RUN=$PACK_DIR/run
LOG=$PACK_DIR/logs/ingest.log
LOOP=$RUN/ingest-loop.pid
stop_ingest() {
  local p pids=() i=0
  p=$(running_pid "$LOOP") && pids+=("$p")
  p=$(running_pid "$RUN/ingest.pid") && pids+=("$p")
  [ ${#pids[@]} -gt 0 ] || return 0
  kill "${pids[@]}" 2>/dev/null
  alive() { local q; for q in "${pids[@]}"; do kill -0 "$q" 2>/dev/null && return 0; done; return 1; }
  while [ "$i" -lt 10 ] && alive; do sleep 1; i=$((i + 1)); done
  alive && kill -9 "${pids[@]}" 2>/dev/null
  rm -f "$LOOP" "$RUN/ingest.pid"
}
start_ingest() {
  [ -f "$INGEST_JSON" ] || fail "没有 $INGEST_JSON"
  "$PYTHON" -I -c 'import json,sys; u=json.load(open(sys.argv[1])).get("fetcher_url",""); sys.exit(0 if u.startswith("http://") and "<" not in u else 1)' "$INGEST_JSON" \
    || fail "ingest.json 的 fetcher_url 还没有填写：http://<proxy 机器>:<listen_port>"
  mkdir -p "$RUN" "$(dirname "$LOG")" || fail "建不了 $RUN"
  nohup bash -c 'while true; do "$0" -I "$1" run --dir "$2" & p=$!; echo $p > "$3"; wait $p; sleep 5; done' \
    "$PYTHON" "$PACK_DIR/ingest/pd_ingest.py" "$PACK_DIR" "$RUN/ingest.pid" >>"$LOG" 2>&1 &
  echo $! > "$LOOP"
  sleep 2
  running_pid "$LOOP" >/dev/null && running_pid "$RUN/ingest.pid" >/dev/null || { tail -n 10 "$LOG" >&2; fail "入库进程没有在运行，看上面的输出"; }
  echo "入库进程已启动，日志 ${LOG}，状态端口 $INGEST_STATUS_PORT"
  echo "RESULT: OK"
}
case "${1:-}" in
  start)
    if running_pid "$LOOP" >/dev/null; then echo "入库进程已在运行"; echo "RESULT: OK"; exit 0; fi
    start_ingest ;;
  restart) stop_ingest; start_ingest ;;
  stop) stop_ingest; echo "入库进程已停止" ;;
  status)
    "$PYTHON" -I - "$INGEST_STATUS_PORT" <<'PY'
import json, sys, urllib.request as u
try:
    s = json.load(u.urlopen("http://127.0.0.1:%s/status" % sys.argv[1], timeout=5))
except Exception:  # noqa: BLE001
    print("FAIL 入库进程没有应答（状态端口 %s）→ bin/ingest.sh start" % sys.argv[1])
    print("RESULT: FAIL")
    sys.exit(1)
bad = bool(s.get("blocked")) or s.get("fetch_failures", 0) > 0
print("%s 集群 %s，采集进程 %s，游标 %s，最近文档距今 %s 秒，连续连接失败 %s 次%s" % (
    "FAIL" if bad else "OK  ", s.get("cluster") or "—", s.get("fetcher_url"), s.get("cursor"), s.get("last_doc_age_seconds"),
    s.get("fetch_failures"), "，停止读取：%s" % s["blocked"] if s.get("blocked") else ""))
print("已写入 request_doc %s 份、status_doc %s 份，跳号 %s" % (s.get("docs_request"), s.get("docs_status"), s.get("ingest_gap")))
print("RESULT: %s" % ("FAIL" if bad else "OK"))
sys.exit(1 if bad else 0)
PY
    ;;
  *) fail "用法：bin/ingest.sh start|stop|restart|status" ;;
esac
