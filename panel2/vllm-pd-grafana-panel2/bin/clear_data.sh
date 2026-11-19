#!/bin/bash
# Empties panel two's request data in ClickHouse; keeps tables, views, users and the dashboard.
# Usage: bin/clear_data.sh --yes [--all] [--with-mirror]
#   default  phase_line and diag_event (the request data) and the collector's samples of unparsed lines; the filter's
#            file records (filter_file, filter_selfcheck, filter_other) stay, so nothing has to be restarted afterwards.
#   --all    all tables (switching environments; v3.1-extra's proxy_line and proxy_selfcheck too, when they exist). The mock is never started again. In mock mode the puller is not
#            either: bin/panel2_cluster.sh add starts both once deploy.env and inventory.json are changed. In vllm mode
#            (after add --mode vllm) the puller is started again as usual.
#   mock mode: stops the mock (from here, design 2.3), deletes the logs it wrote on each node (node_mock.sh clean),
#            stops the puller, waits for the collector's buffer to empty, empties the tables and the mirror (slowly),
#            then starts the collector, the puller and the mock (with --all only the collector). --with-mirror is
#            accepted and means the same.
#   vllm mode: stops the puller, waits for the buffer, empties the tables, starts the puller. Never touches the mirror,
#            the nodes or vLLM; --with-mirror is refused (the node files belong to vLLM and would be pulled back).
. "$(dirname "$0")/lib.sh"
load_env
# T-6 (t33a, mock, MOCK_RATE=8, --yes): new rows on the board after 21 s, the upgrade-safety check passing from minute 1.
# In vllm mode new rows need requests.
BOARD_BACK="约 20 秒后"
CHECK_AFTER="1 分钟以后"

# Known issue (T32): vLLM keeps writing while the puller is stopped; t33b measured about 30 s of such rows.
INFLIGHT="vllm 模式下，清空前约 30 秒内的在途行会在清完后写进来，没有请求时为 0"

yes=""; all=""; mirror=""
for a in "$@"; do
  case $a in
    --yes) yes=1 ;;
    --all) all=1 ;;
    --with-mirror) mirror=1 ;;
    *) fail "用法：bin/clear_data.sh --yes [--all] [--with-mirror]" ;;
  esac
done
mode=${NODE_MODE:-mock}
[ "$mode" = vllm ] && BOARD_BACK="有请求时$BOARD_BACK"
if [ "$mode" = vllm ] && [ -n "$mirror" ]; then
  fail "vllm 模式下不清镜像（节点上的文件属于 vLLM，镜像删了会被原样拉回）。只清请求数据：bin/clear_data.sh --yes"
fi
tables="phase_line diag_event proxy_line"
[ -n "$all" ] && tables="phase_line diag_event proxy_line filter_selfcheck filter_file filter_other proxy_selfcheck"
what="请求数据（phase_line、diag_event；v3.1-extra 有 proxy_line 时也清）和解析失败样本，保留过滤器的文件记录"
[ -n "$all" ] && what="全部表（phase_line、diag_event、filter_selfcheck、filter_file、filter_other；有 proxy_line、proxy_selfcheck 时也清）和解析失败样本"
[ -n "$yes" ] || fail "这会清空面板二的$what。确认后执行：bin/clear_data.sh --yes${all:+ --all}"
if [ "$mode" = vllm ]; then
  if [ -n "$all" ]; then echo "vllm 模式：清$what；不清镜像，不动节点和 vLLM"
  else echo "vllm 模式：只清$what；不清镜像，不动节点和 vLLM"; fi
else
  if [ -n "$all" ]; then echo "mock 模式：清$what；停 mock、删节点上 mock 写的日志、清镜像，不再启动 mock 和拉取程序（换环境，由 add 启动）"
  else echo "mock 模式：清$what；停 mock、删节点上 mock 写的日志、清镜像，之后再启动"; fi
fi

slow_delete() {
  # Every file under $1 but the marker, one at a time, 0.2 s apart, at the lowest CPU priority, progress every 5 s (T32 C1).
  local dir=$1 total kib done=0 failed=0 first="" gone=0 s t0 last now el
  renice -n 19 -p $$ >/dev/null 2>&1
  total=$(find "$dir" -mindepth 1 -type f ! -name .made-by-vllm-timing 2>/dev/null | wc -l | tr -d ' ')
  kib=$(du -sk "$dir" 2>/dev/null | cut -f1)
  t0=$(date +%s); last=$t0
  echo "删除镜像 $dir：$total 个文件，$((kib / 1024)) MiB，每个文件之间停 0.2 秒，预计 $((total / 5)) 秒"
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
  done < <(find "$dir" -mindepth 1 -type f ! -name .made-by-vllm-timing -print0 2>/dev/null)
  find "$dir" -mindepth 1 -depth -type d -empty -delete 2>/dev/null
  if [ "$failed" -gt 0 ]; then
    echo "镜像里 $failed 个文件删不了（比如 $first，属主 $(stat -c %U "$first" 2>/dev/null)），已删 $done 个"
    return 1
  fi
  echo "镜像已清空：$done 个文件、$((gone / 1024)) MiB，用时 $(( $(date +%s) - t0 )) 秒"
}

buffer_events() {
  # Events in the collector's disk buffers (Vector's vector_buffer_size_events, summed over the five ClickHouse sinks);
  # "none" when the metric cannot be read. Vector refreshes it every 5 s (scrape_interval_secs of render_vector.py).
  docker exec "$CENTRAL_CONTAINER" bash -c 'exec 3<>/dev/tcp/127.0.0.1/9598 && printf "GET /metrics HTTP/1.0\r\nHost: localhost\r\n\r\n" >&3 && cat <&3' 2>/dev/null \
    | awk '/^vector_buffer_size_events\{/ && /buffer_type="disk"/ { s += $2; n++ } END { if (n) printf "%d", s; else print "none" }'
}

# 1. mock mode (design 2.5 steps 1 and 2): the mock stopped from here (process count 0 on every node), then its logs
# deleted on each node, so that the puller cannot copy old files back into the emptied mirror.
running=$(docker inspect --format '{{.State.Running}}' "$CENTRAL_CONTAINER" 2>/dev/null)
if [ "$mode" != vllm ]; then
  out=$("$PACK_DIR/bin/nodes.sh" stop) || { echo "$out"; fail "有节点的 mock 停不掉，什么都没有清：看上面的 FAIL 行"; }
  echo "$out" | grep -v '^RESULT'
  "$PACK_DIR/bin/nodes.sh" mock-clean | grep --line-buffered -v '^RESULT'; [ "${PIPESTATUS[0]}" = 0 ] \
    || fail "有节点上 mock 的日志没有删完，表和镜像都没有清（mock 已停止）：看上面的 FAIL 行，修好后重跑 bin/clear_data.sh，它最后会启动 mock"
fi
"$PACK_DIR/bin/puller.sh" stop >/dev/null 2>&1
echo "拉取程序已停止"

# 2. Rows the collector already read are written out before it stops (T32 C4: buffer size, not a fixed wait).
if [ "$running" = true ]; then
  t0=$(date +%s); zero_since=""; n=$(buffer_events)
  if [ "$n" = none ]; then
    echo "读不到采集器的缓冲指标（vector_buffer_size_events），按固定 60 秒等"
    sleep 60
  else
    while :; do
      now=$(date +%s)
      if [ "$n" = 0 ]; then
        [ -n "$zero_since" ] || zero_since=$now
        # Zero across one refresh of the metric, so that it is not a reading from before the last lines arrived.
        [ $((now - zero_since)) -ge 6 ] && { echo "等采集器缓冲排空：$((now - t0)) 秒"; break; }
      else
        zero_since=""
      fi
      if [ $((now - t0)) -ge 120 ]; then
        echo "等了 120 秒，采集器缓冲里还有 $n 条：清空以后它们会写进表里（时间早于清空时刻）"
        break
      fi
      sleep 1; n=$(buffer_events)
    done
  fi
  docker stop -t 60 "$CENTRAL_CONTAINER" >/dev/null || fail "停不了采集器 $CENTRAL_CONTAINER；拉取程序已停止，执行 bin/puller.sh start 恢复"
else
  echo "采集器没在运行，不用等缓冲"
fi

# 3. The tables, then (mock mode) the mirror.
cleared=$(date -u '+%F %T')
for t in $tables; do
  ch_sql "TRUNCATE TABLE IF EXISTS vllm_pd.$t" || fail "清空 $t 失败；采集器和拉取程序已停止，修好后重跑"
done
echo "清空时刻 $cleared UTC；phase_line: $(ch_sql 'SELECT count() FROM vllm_pd.phase_line') 行，diag_event: $(ch_sql 'SELECT count() FROM vllm_pd.diag_event') 行"
if [ "$mode" != vllm ] && [ -d "$PULL_DIR" ]; then
  # Only a folder this pack made (it has the marker), and everything in it but the marker.
  if [ -f "$PULL_DIR/.made-by-vllm-timing" ]; then
    slow_delete "$PULL_DIR" || fail "清不空镜像目录 $PULL_DIR"
  else
    echo "镜像目录 $PULL_DIR 不是本包建的，没有清空"
  fi
fi

# 4. Start the collector, the puller and (mock mode) the mock, also when an earlier run stopped them and failed.
# --all in mock mode switches environments: only the collector; bin/panel2_cluster.sh add starts the rest with the new
# settings. In vllm mode there is no mock to keep away, and the puller runs as usual.
docker start "$CENTRAL_CONTAINER" >/dev/null || fail "采集器 $CENTRAL_CONTAINER 起不来，执行 bin/central_up.sh"
# The checks count these samples: lines from before must not show up as now.
docker exec "$CENTRAL_CONTAINER" sh -c 'rm -f /var/lib/vector/dead-letter-*.jsonl' 2>/dev/null || true
echo "采集器已启动，解析失败样本已清空"
if [ -n "$all" ] && [ "$mode" != vllm ]; then
  echo "拉取程序和 mock 没有启动。改好 deploy.env、inventory.json 后执行 bin/panel2_cluster.sh add ${CLUSTER:-<集群>} --model-group ${MODEL_GROUP:-<组>} --inventory $PACK_DIR/inventory.json --mode <mock 或 vllm>（拉取程序和 mock 由 add 启动）"
  echo "过滤器的文件记录也清了：vllm 模式下，vLLM 打开新文件以前，升级安全检查会报没有过滤器写的文件；要马上恢复就重启 vLLM"
elif [ -n "$all" ]; then
  "$PACK_DIR/bin/puller.sh" start | grep -v '^RESULT'; [ "${PIPESTATUS[0]}" = 0 ] || fail "拉取程序起不来，执行 bin/puller.sh start"
  echo "过滤器的文件记录也清了：vLLM 打开新文件以前，升级安全检查会报没有过滤器写的文件；要马上恢复就重启 vLLM"
  echo "$INFLIGHT"
else
  "$PACK_DIR/bin/puller.sh" start | grep -v '^RESULT'; [ "${PIPESTATUS[0]}" = 0 ] || fail "拉取程序起不来，执行 bin/puller.sh start"
  if [ "$mode" != vllm ]; then
    "$PACK_DIR/bin/nodes.sh" start | grep -v '^RESULT'; [ "${PIPESTATUS[0]}" = 0 ] || fail "mock 没有全部起来，看上面的 FAIL 行；修好后执行 bin/nodes.sh start"
  fi
  echo "看板${BOARD_BACK}有新数据，打开着的页面要刷新；${CHECK_AFTER}可以跑 bin/panel2_cluster.sh check ${CLUSTER:-<集群>}"
  [ "$mode" = vllm ] && echo "$INFLIGHT"
fi
echo "RESULT: OK"
