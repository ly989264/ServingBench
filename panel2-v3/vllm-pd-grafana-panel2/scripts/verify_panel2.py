#!/usr/bin/env python3
"""Checks the whole chain: the puller and collector in Prometheus, rows in ClickHouse, every instance, every panel.

Settings come from the environment (bin/lib.sh loads deploy.env). Prints RESULT: OK or RESULT: FAIL at the end.
"""
import argparse
import base64
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ENV = os.environ
PACK = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PACK / "scripts"))
from inventory_tool import load as load_inventory, node_machines  # noqa: E402
from segtree import Tree  # noqa: E402
from names import Names, NamesError  # noqa: E402

try:
    N = Names(ENV)
except NamesError as e:
    raise SystemExit("错误：%s" % e)

GRAFANA = ENV.get("GRAFANA_URL", "http://localhost:3000").rstrip("/")
AUTH = base64.b64encode(("%s:%s" % (ENV.get("GRAFANA_USER", "admin"), ENV.get("GRAFANA_PASSWORD", ""))).encode()).decode()
problems = []


def fail(msg):
    problems.append(msg)
    print("FAIL  " + msg)


def ok(msg):
    print("OK    " + msg)


def ds_query(sql, fmt=1, query_type="table", minutes=15):
    body = {
        "from": "now-%dm" % minutes,
        "to": "now",
        "queries": [{"refId": "A", "datasource": {"uid": N.get("GRAFANA_DS_UID")}, "rawSql": sql, "format": fmt, "queryType": query_type, "editorType": "sql", "pluginVersion": "4.23.4",
                     "meta": {"builderOptions": {"queryType": query_type, "mode": "list", "columns": [], "filters": [], "limit": 0}}}],
    }
    req = urllib.request.Request(GRAFANA + "/api/ds/query", data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", "Basic " + AUTH)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        res = json.loads(e.read() or b"{}")
    a = res.get("results", {}).get("A", {})
    if a.get("error"):
        return None, a["error"]
    frames = a.get("frames") or []
    if not frames or not frames[0]["data"]["values"]:
        return [], None
    names = [f["name"] for f in frames[0]["schema"]["fields"]]
    return [dict(zip(names, row)) for row in zip(*frames[0]["data"]["values"])], None


def check_prometheus(inv):
    url = N.get("PROMETHEUS_URL").rstrip("/")
    try:
        with urllib.request.urlopen(url + "/api/v1/targets?state=active", timeout=15) as r:
            targets = json.load(r)["data"]["activeTargets"]
    except Exception as e:  # noqa: BLE001
        fail("连不上 Prometheus %s：%s" % (url, e))
        return
    mine = [t for t in targets if t["labels"].get("job") == N.get("PULLER_JOB")]
    if mine and all(t["health"] == "up" for t in mine):
        ok("Prometheus 抓到拉取程序")
    else:
        fail("Prometheus 抓不到拉取程序。先确认 bin/puller.sh status 正常；再检查 Prometheus 容器能不能访问 %s:%s。" % (ENV.get("MONITOR_HOST"), ENV.get("PULLER_METRICS_PORT")))
    check_pull(inv)
    central = [t for t in targets if t["labels"].get("job") == N.get("CENTRAL_JOB")]
    if central and all(t["health"] == "up" for t in central):
        ok("Prometheus 抓到采集器")
    else:
        fail("Prometheus 抓不到采集器。在监控机上看 docker ps 里有没有 %s，重新执行 bin/central_up.sh 和 bin/prometheus_job.sh。" % N.get("CENTRAL_CONTAINER"))
    try:
        q = urllib.parse.quote('sum(vector_component_sent_events_total{component_id="parse",output="dropped"})')
        with urllib.request.urlopen(url + "/api/v1/query?query=" + q, timeout=15) as r:
            res = json.load(r)["data"]["result"]
        print("      采集器解析失败的行：%d" % (int(float(res[0]["value"][1])) if res else 0))
    except Exception:  # noqa: BLE001
        pass
    try:
        with urllib.request.urlopen(url + "/api/v1/rules", timeout=15) as r:
            groups = json.load(r)["data"]["groups"]
        n = sum(len(g["rules"]) for g in groups if g["name"] == N.get("RULES_GROUP"))
        if n:
            ok("Prometheus 加载了 %d 条告警规则" % n)
        else:
            fail("Prometheus 没有加载告警规则")
    except Exception as e:  # noqa: BLE001
        fail("读不到 Prometheus 规则：%s" % e)


def check_data(inv, minutes):
    rows, err = ds_query(
        "SELECT (SELECT uniqExact(request_id) FROM vllm_pd.phase_line_cur WHERE $__timeFilter(ts) AND proc = 'api') AS s, "
        "(SELECT count() FROM vllm_pd.phase_line_cur WHERE $__timeFilter(ts)) AS l, "
        "(SELECT count() FROM vllm_pd.diag_event WHERE $__timeFilter(ts)) AS e, "
        "(SELECT dateDiff('second', max(ts), now64(3)) FROM vllm_pd.phase_line_cur WHERE $__timeFilter(ts)) AS age", minutes=minutes)
    if err is not None:
        fail("Grafana 查不了 ClickHouse：%s" % err[:300])
        return False
    r = rows[0] if rows else {"s": 0, "l": 0, "e": 0, "age": None}
    if not r["s"] or not r["l"] or not r["e"]:
        fail("最近 %d 分钟 ClickHouse 里没有数据（请求 %s，汇总行 %s，事件行 %s）。在监控机上执行 bin/nodes.sh status 和 bin/puller.sh status。" % (minutes, r["s"], r["l"], r["e"]))
        return False
    ok("最近 %d 分钟：请求 %d 个，汇总行 %d，事件行 %d，最新一条距现在 %s 秒" % (minutes, r["s"], r["l"], r["e"], r["age"]))
    names = ["P%d" % i for i in range(len(inv["prefill"]["api_servers"]))] + ["D%d" % i for i in range(len(inv["decode"]["api_servers"]))]
    rows, err = ds_query("SELECT DISTINCT instance FROM vllm_pd.phase_line_cur WHERE $__timeFilter(ts)", minutes=minutes)
    seen = {x["instance"] for x in rows or []}
    missing = [n for n in names if n not in seen]
    if missing:
        fail("这些实例没有数据：%s" % " ".join(missing))
    else:
        ok("全部 %d 个实例都有数据（P %d 个、D %d 个）" % (len(names), len(inv["prefill"]["api_servers"]), len(inv["decode"]["api_servers"])))
    rows, err = ds_query("SELECT node, count() AS c FROM vllm_pd.phase_line_cur WHERE $__timeFilter(ts) GROUP BY node ORDER BY node", minutes=minutes)
    print("      node 列：%s" % " ".join(x["node"] for x in rows or []))
    if len(rows or []) != len(node_machines(inv)):
        fail("汇总行来自 %d 台机器，清单上 P、D 一共 %d 台。" % (len(rows or []), len(node_machines(inv))))
    rows, err = ds_query(
        # Requests chosen by P's own clock, their rows looked up in a wider window. Not the first minute after the data starts.
        "SELECT count() AS n, countIf(a.p = 3 AND a.d = 3) AS full FROM (SELECT request_id FROM vllm_pd.phase_line_cur WHERE proc = 'api' AND role = 'prefill' "
        "AND ts >= now() - INTERVAL 10 MINUTE AND ts < now() - INTERVAL 3 MINUTE AND ts >= (SELECT min(ts) FROM vllm_pd.phase_line_cur) + INTERVAL 60 SECOND GROUP BY request_id) AS s "
        # the three request processes only: the background save task (proc save) is a fourth kind a request may or may not have
        "LEFT JOIN (SELECT request_id, uniqExactIf(proc, role = 'prefill' AND proc IN ('api', 'engine', 'worker')) AS p, "
        "uniqExactIf(proc, role = 'decode' AND proc IN ('api', 'engine', 'worker')) AS d FROM vllm_pd.phase_line_cur "
        "WHERE ts >= now() - INTERVAL 20 MINUTE GROUP BY request_id) AS a ON a.request_id = s.request_id", minutes=minutes)
    clock(r["age"])
    json_check(minutes)
    if rows and rows[0]["n"]:
        share = rows[0]["full"] / rows[0]["n"]
        msg = "3 到 10 分钟前 P 侧的 %d 个请求里，%.1f%% 的 P、D 两侧三类进程（api、engine、worker）都有行（mock 造了中止和失败）" % (rows[0]["n"], share * 100)
        (ok if share > 0.9 else fail)(msg)
    return True


def check_pull(inv):
    """When each machine was last pulled."""
    try:
        with urllib.request.urlopen("http://%s:%s/status" % (ENV.get("MONITOR_HOST"), ENV.get("PULLER_METRICS_PORT")), timeout=10) as r:
            rows = json.load(r)
    except Exception as e:  # noqa: BLE001
        fail("拉取程序没有运行（%s）。执行 bin/puller.sh start" % e)
        return
    stale = [m for m in rows if m["age"] is None or m["age"] > 120]
    if stale:
        fail("这些机器超过 2 分钟没有拉取成功：%s" % "; ".join("%s（%s）" % (m["addr"], m["error"][:60]) for m in stale))
    else:
        ok("%d 台机器都在拉取，最久的 %.0f 秒前" % (len(rows), max(m["age"] for m in rows)))


def clock(age):
    """Whether a machine's clock is far off, judged from the newest line of each machine: that is the time the logs carry, and it needs no extra port."""
    if age is None or age > 60:
        return
    rows, err = ds_query(
        "SELECT node, dateDiff('second', now64(3), max(ts)) AS d FROM vllm_pd.phase_line_cur WHERE ts > now() - INTERVAL 2 HOUR AND ts < now() + INTERVAL 2 HOUR "
        "GROUP BY node ORDER BY d", minutes=120)
    if not rows:
        return
    low, high = rows[0], rows[-1]
    line = "各机器最新日志的时间和监控机相差：最慢 %s %+d 秒，最快 %s %+d 秒（含几秒到几十秒的写入延迟）" % (low["node"], low["d"], high["node"], high["d"])
    if low["d"] < -300 or high["d"] > 300:
        fail(line + "。超过 5 分钟：先校时，或者确认那台机器还在写日志。")
    else:
        ok(line)


def json_check(minutes):
    """The per-request JSON of 20 random requests: request_id, schema, both sides' official numbers."""
    rows, err = ds_query("SELECT request_id FROM vllm_pd.phase_line_cur WHERE $__timeFilter(ts) AND proc = 'api' AND ts < now() - INTERVAL 30 SECOND "
                         "GROUP BY request_id HAVING uniqExact(role) = 2 ORDER BY rand() LIMIT 20", minutes=minutes)
    if not rows:
        return
    bad = 0
    for row in rows:
        docs, err = ds_query("SELECT doc FROM vllm_pd.request_json(id='%s')" % row["request_id"], minutes=minutes)
        doc = json.loads(docs[0]["doc"]) if docs else {}
        lat = doc.get("latency_ms", {})
        if doc.get("request_id") != row["request_id"] or doc.get("schema") != "vllm_pd.request.v2" or "official" not in lat:
            bad += 1
    if bad:
        fail("抽查 %d 个请求的 JSON，%d 个不是 vllm_pd.request.v2 或没有 request_id、latency_ms.official" % (len(rows), bad))
    else:
        ok("抽查 %d 个请求的 JSON：都是 vllm_pd.request.v2，request_id 对得上，两侧官方 E2E/TTFT 在 latency_ms.official" % len(rows))


def subst(sql, values):
    def one(m):
        return values[m.group(1)]
    sql = sql.replace("${instance:singlequote}", values["instance"]).replace("$model", values["model"])
    return re.sub(r"\$\{(order_by|request_id)\}", one, sql)


def check_panels(minutes, all_orders=False):
    with open(PACK / "generated" / "vllm-pd-request.json", encoding="utf-8") as f:
        board = json.load(f)
    variables = {v["name"]: v for v in board["templating"]["list"]}
    values = {"order_by": "d_e2e_ms"}
    rows, err = ds_query(variables["model"]["query"], minutes=minutes)
    if err or not rows:
        fail("模型变量没有值：%s" % err)
        return
    values["model"] = rows[0]["model"]
    rows, err = ds_query(variables["instance"]["query"], minutes=minutes)
    if err or not rows:
        fail("实例变量没有值：%s" % err)
        return
    values["instance"] = ",".join("'%s'" % x["name"] for x in rows)
    rows, err = ds_query(subst(variables["request_id"]["query"], dict(values, request_id="")), minutes=minutes)
    if err or not rows:
        fail("request_id 变量没有值：%s" % err)
        return
    values["request_id"] = rows[0]["request_id"]
    n = good = 0
    for p in board["panels"]:
        if p["type"] == "row":
            continue
        n += 1
        t = p["targets"][0]
        rows, err = ds_query(subst(t["rawSql"], values), t.get("format", 1), t.get("queryType", "table"), minutes)
        if err:
            fail("panel %-44s 查询出错：%s" % (p["title"], err[:400]))
        elif not rows:
            fail("panel %-44s 没有数据" % p["title"])
        else:
            good += 1
            print("OK    panel %-44s %d 行" % (p["title"], len(rows)))
    print("      %d 个 panel，%d 个有数据" % (n, good))
    if all_orders:
        for order in ("p_ttft_ms", "p_total_ms", "d_total_ms", "tpot_ms"):
            vals = dict(values, order_by=order)
            bad = 0
            for p in board["panels"]:
                if p["type"] == "row" or "${order_by}" not in p["targets"][0]["rawSql"]:
                    continue
                rows, err = ds_query(subst(p["targets"][0]["rawSql"], vals), minutes=minutes)
                if err or not rows:
                    bad += 1
                    fail("按 %s 排序时 panel %s：%s" % (order, p["title"], (err or "没有数据")[:300]))
            if not bad:
                ok("按 %s 排序，Top-20 的 panel 都有数据" % order)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--minutes", type=int, default=15)
    ap.add_argument("--no-prometheus", action="store_true")
    ap.add_argument("--all-orders", action="store_true", help="also try every sort order of the Top-20 panels")
    args = ap.parse_args()
    inv = load_inventory(PACK / "inventory.json")
    if not args.no_prometheus:
        check_prometheus(inv)
    if check_data(inv, args.minutes):
        check_panels(args.minutes, args.all_orders)
    print("RESULT: %s" % ("FAIL (%d 项)" % len(problems) if problems else "OK"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
