#!/bin/bash
# Adds the scrape job of the ingest process's status port (clickhouse.env INGEST_STATUS_PORT) and the 4 alert rules to the
# cluster's Prometheus (panel 1's). Reads only clickhouse.env. Safe to run again: after prometheus.yml was replaced,
# running it again puts the marked block back. When the marked blocks and the rules file are already these, nothing is
# written and Prometheus is not reloaded, so panel 1 is not touched. VLLM_PD_FORCE=1 writes and reloads anyway.
# The block replaces the earlier block of the puller and the collector (same markers).
. "$(dirname "$0")/lib.sh"
load_ch_env
need_python
for k in PROMETHEUS_CONTAINER PROMETHEUS_YML PROMETHEUS_URL INGEST_JOB RULES_FILE RULES_GROUP MONITOR_HOST INGEST_STATUS_PORT; do
  [ -n "${!k:-}" ] || fail "clickhouse.env 缺 $k"
done
docker ps --format '{{.Names}}' | grep -qx "$PROMETHEUS_CONTAINER" || fail "容器 $PROMETHEUS_CONTAINER 没在运行。检查 clickhouse.env 里的 PROMETHEUS_CONTAINER。"
[ -f "$PROMETHEUS_YML" ] || fail "没有找到 ${PROMETHEUS_YML}。检查 clickhouse.env 里的 PROMETHEUS_YML。"
mkdir -p "$PACK_DIR/generated" || fail "建不了目录 $PACK_DIR/generated"
rules_local=$PACK_DIR/generated/$(basename "$RULES_FILE")
"$PYTHON" -I "$PACK_DIR/scripts/prometheus_merge.py" rules --out "$rules_local" || exit 1
chmod a+r "$rules_local"
if [ "${VLLM_PD_FORCE:-}" != 1 ]; then
  tmp=$(mktemp "${TMPDIR:-/tmp}/prom.XXXXXX") || fail "建不了临时文件"
  cp "$PROMETHEUS_YML" "$tmp" && "$PYTHON" -I "$PACK_DIR/scripts/prometheus_merge.py" add --file "$tmp" >/dev/null || { rm -f "$tmp"; exit 1; }
  same_yml=""; cmp -s "$tmp" "$PROMETHEUS_YML" && same_yml=1
  rm -f "$tmp"
  same_rules=""; docker exec "$PROMETHEUS_CONTAINER" cat "$RULES_FILE" 2>/dev/null | cmp -s - "$rules_local" && same_rules=1
  if [ -n "$same_yml" ] && [ -n "$same_rules" ]; then
    echo "跳过（内容不变）：Prometheus（不写文件、不重新加载）"
    echo "RESULT: OK"
    exit 0
  fi
fi
docker cp "$rules_local" "$PROMETHEUS_CONTAINER:$RULES_FILE" >/dev/null || fail "规则文件拷不进容器"
"$PYTHON" -I "$PACK_DIR/scripts/prometheus_merge.py" add --file "$PROMETHEUS_YML" || exit 1
out=$(docker exec "$PROMETHEUS_CONTAINER" promtool check config /etc/prometheus/prometheus.yml 2>&1) || {
  echo "$out" >&2
  "$PYTHON" -I "$PACK_DIR/scripts/prometheus_merge.py" remove --file "$PROMETHEUS_YML"
  fail "Prometheus 配置检查没过，已经撤回。"
}
http_post() { "$PYTHON" -I -c 'import sys,urllib.request as u; u.urlopen(u.Request(sys.argv[1],method="POST"),timeout=15)' "$1"; }
http_post "$PROMETHEUS_URL/-/reload" || fail "Prometheus 重新加载失败。面板一的启动命令带 --web.enable-lifecycle 才支持；也可以 docker restart ${PROMETHEUS_CONTAINER}。"
"$PYTHON" -I - "$PROMETHEUS_URL" "$INGEST_JOB" "$RULES_GROUP" <<'PY' || exit 1
import json, sys, time, urllib.request as u
url, job, group = sys.argv[1:4]
# After a reload it takes Prometheus some seconds to hand new targets to its scraper.
for _ in range(15):
    time.sleep(2)
    targets = json.load(u.urlopen(url + "/api/v1/targets?state=active", timeout=15))["data"]["activeTargets"]
    n = sum(1 for t in targets if t["labels"].get("job") == job)
    groups = json.load(u.urlopen(url + "/api/v1/rules", timeout=15))["data"]["groups"]
    r = sum(len(g["rules"]) for g in groups if g["name"] == group)
    if n and r:
        break
print("抓取目标：入库进程 %d 个；告警规则 %d 条" % (n, r))
if not n or r != 4:
    print("错误：Prometheus 里没有看到入库进程的抓取任务或 4 条规则", file=sys.stderr)
    sys.exit(1)
print("RESULT: OK")
PY
