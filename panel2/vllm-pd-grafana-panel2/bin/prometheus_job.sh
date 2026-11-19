#!/bin/bash
# Adds the scrape jobs of the puller and of the collector, and the alert rules, to the cluster's Prometheus. Safe to run again:
# after prometheus.yml was replaced, running it again puts the marked block back.
# When the marked blocks, the rules file and what Prometheus has loaded are already this pack's (bin/compare.sh prometheus),
# nothing is written and Prometheus is not reloaded, so panel 1 is not touched. VLLM_PD_FORCE=1 writes and reloads anyway.
. "$(dirname "$0")/lib.sh"
load_env
need_python
names_check
docker ps --format '{{.Names}}' | grep -qx "$PROMETHEUS_CONTAINER" || fail "容器 $PROMETHEUS_CONTAINER 没在运行。检查 deploy.env 里的 PROMETHEUS_CONTAINER。"
[ -f "$PROMETHEUS_YML" ] || fail "没有找到 $PROMETHEUS_YML。检查 deploy.env 里的 PROMETHEUS_YML。"
updated=""
if [ "${VLLM_PD_FORCE:-}" = 1 ]; then
  echo "VLLM_PD_FORCE=1：不比对，照做：Prometheus"
else
  line=$("$PACK_DIR/bin/compare.sh" prometheus); rc=$?
  [ "$rc" -eq 0 ] || [ "$rc" -eq 10 ] || fail "比对 Prometheus 失败，看上面的提示"
  if [ "$rc" -eq 0 ]; then
    echo "跳过（内容不变）：Prometheus（不写文件、不重新加载）"
    echo "RESULT: OK"
    exit 0
  fi
  [ "${line%% *}" = WARN ] && echo "$line"
  what=${line#*：}; updated="已更新：Prometheus：${what#有变化：}"
fi
mkdir -p "$PACK_DIR/generated" || fail "建不了目录 $PACK_DIR/generated"
"$PYTHON" "$PACK_DIR/scripts/prometheus_merge.py" rules --out "$PACK_DIR/generated/$RULES_FILE" || exit 1
chmod a+r "$PACK_DIR/generated/$RULES_FILE"
docker cp "$PACK_DIR/generated/$RULES_FILE" "$PROMETHEUS_CONTAINER:/prometheus/$RULES_FILE" >/dev/null || fail "规则文件拷不进容器"
"$PYTHON" "$PACK_DIR/scripts/prometheus_merge.py" add --file "$PROMETHEUS_YML" --puller "$MONITOR_HOST:$PULLER_METRICS_PORT" --central "$CENTRAL_CONTAINER" || exit 1
out=$(docker exec "$PROMETHEUS_CONTAINER" promtool check config /etc/prometheus/prometheus.yml 2>&1) || {
  echo "$out" >&2
  "$PYTHON" "$PACK_DIR/scripts/prometheus_merge.py" remove --file "$PROMETHEUS_YML"
  fail "Prometheus 配置检查没过，已经撤回。"
}
http_post() { "$PYTHON" -c 'import sys,urllib.request as u; u.urlopen(u.Request(sys.argv[1],method="POST"),timeout=15)' "$1"; }
http_post "$PROMETHEUS_URL/-/reload" || fail "Prometheus 重新加载失败。面板一的启动命令带 --web.enable-lifecycle 才支持；也可以 docker restart $PROMETHEUS_CONTAINER。"
"$PYTHON" - "$PROMETHEUS_URL" "$PULLER_JOB" "$CENTRAL_JOB" "$RULES_GROUP" "$updated" <<'PY' || exit 1
import json, sys, time, urllib.request as u
url, puller_job, central_job, group, updated = sys.argv[1:6]
# After a reload it takes Prometheus some seconds to hand new targets to its scraper.
for _ in range(15):
    time.sleep(2)
    targets = json.load(u.urlopen(url + "/api/v1/targets?state=active", timeout=15))["data"]["activeTargets"]
    n = sum(1 for t in targets if t["labels"].get("job") == puller_job)
    c = sum(1 for t in targets if t["labels"].get("job") == central_job)
    groups = json.load(u.urlopen(url + "/api/v1/rules", timeout=15))["data"]["groups"]
    r = sum(len(g["rules"]) for g in groups if g["name"] == group)
    if n and c and r:
        break
print("抓取目标: %d 个拉取程序、%d 个采集器，告警规则: %d 条" % (n, c, r))
if not n or not c or not r:
    print("错误：Prometheus 里没有看到新增的 job 或规则", file=sys.stderr)
    sys.exit(1)
if updated:
    print(updated)
print("RESULT: OK")
PY
