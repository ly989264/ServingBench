#!/bin/bash
# Empties panel two's documents in ClickHouse; keeps the tables, the view, the accounts, the segment definitions
# (segment_def), the version table (db_interface) and the dashboard.
# Usage: bin/clear_data.sh --yes [--all]
#   default  request_doc (the request documents)
#   --all    request_doc and status_doc (the status documents)
# The ingest process is stopped first (bin/ingest.sh stop) and, when it was running, started again afterwards.
# The ingest process's cursor is the largest seq in request_doc and status_doc. After clearing it starts again from the
# oldest document still in the fetcher's output queue: the documents the queue still holds are written once more, those
# of requests from before the clearing included; with only request_doc cleared, status_doc keeps the cursor, and only
# documents with a larger seq are read.
. "$(dirname "$0")/lib.sh"
load_ch_env

yes=""; all=""
for a in "$@"; do
  case $a in
    --yes) yes=1 ;;
    --all) all=1 ;;
    *) fail "用法：bin/clear_data.sh --yes [--all]" ;;
  esac
done
tables="request_doc"
what="请求文档（request_doc）"
if [ -n "$all" ]; then
  tables="request_doc status_doc"
  what="请求文档（request_doc）和状态文档（status_doc）"
fi
[ -n "$yes" ] || fail "这会清空面板二的${what}；段定义（segment_def）和版本表（db_interface）保留。确认后执行：bin/clear_data.sh --yes${all:+ --all}"

was_running=""
running_pid "$PACK_DIR/run/ingest-loop.pid" >/dev/null && was_running=1
"$PACK_DIR/bin/ingest.sh" stop || fail "入库进程停不下来，什么都没有清"

cleared=$(date -u '+%F %T')
for t in $tables; do
  ch_sql "TRUNCATE TABLE IF EXISTS vllm_pd.$t" || fail "清空 $t 失败；入库进程已停止，修好后重跑"
done
echo "清空时刻 $cleared UTC；request_doc: $(ch_sql 'SELECT count() FROM vllm_pd.request_doc') 行，status_doc: $(ch_sql 'SELECT count() FROM vllm_pd.status_doc') 行"
if [ -n "$all" ]; then
  echo "游标归零：入库进程从采集进程写出队列中最早的文档重新读取，队列里清空前的文档会再写入一次"
else
  echo "游标取 status_doc 的最大序号：入库进程只读取序号更大的文档"
fi

if [ -n "$was_running" ]; then
  "$PACK_DIR/bin/ingest.sh" start | grep -v '^RESULT'; [ "${PIPESTATUS[0]}" = 0 ] || fail "入库进程起不来，执行 bin/ingest.sh start"
else
  echo "入库进程清空前没有在运行，没有启动"
fi
echo "RESULT: OK"
