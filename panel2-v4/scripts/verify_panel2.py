#!/usr/bin/env python3
"""Checks the whole chain: the ingest process in Prometheus, documents in ClickHouse, every instance, every panel.

Settings come from the cluster folder's clickhouse.env (Prometheus, the ingest job and its rule group) and grafana.env
(Grafana and the data source); every ClickHouse query goes through Grafana's data source (/api/ds/query), as the board's
own queries do. Prints RESULT: OK or RESULT: FAIL at the end.

  Prometheus      the ingest job (INGEST_JOB) is up; the rule group RULES_GROUP is loaded
  数据            request documents in the window; every instance named in a status document or in route.p / route.d of
                  a request document has both; the share of requests with both P and D and all three request processes
  时钟            the newest side start of each instance and the newest status document of the fetcher against this
                  machine's clock
  请求 JSON       20 random request documents: schema vllm_pd.request.v2, request_id, latency_ms.official
  panel           every panel query of generated/vllm-pd-request.json returns rows
"""
import argparse
import base64
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

PACK = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PACK / "scripts"))
from segdef import read_env  # noqa: E402

CH_ENV = read_env(os.environ.get("CLICKHOUSE_ENV") or str(PACK / "clickhouse.env"))
GF_ENV = read_env(os.environ.get("GRAFANA_ENV") or str(PACK / "grafana.env"))
SCHEMA = "vllm_pd.request.v2"
GRAFANA = GF_ENV.get("GRAFANA_URL", "http://localhost:3000").rstrip("/")
AUTH = base64.b64encode(("%s:%s" % (GF_ENV.get("GRAFANA_USER", "admin"), GF_ENV.get("GRAFANA_PASSWORD", ""))).encode()).decode()
DOC = "vllm_pd.request_doc FINAL"
# A request has its three request processes on a side unless missing[] names that side ("prefill: 2 of 3 processes").
PROCS_OK = "NOT arrayExists(x -> x LIKE '%of 3 processes', missing)"
# Panels whose empty result is the normal state: the segment definition changes of the window (usually none).
EMPTY_OK = {"段定义变化": "时间窗口内没有更换段定义"}
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
        "queries": [{"refId": "A", "datasource": {"uid": GF_ENV.get("GRAFANA_DS_UID")}, "rawSql": sql, "format": fmt, "queryType": query_type,
                     "editorType": "sql", "pluginVersion": "4.23.4",
                     "meta": {"builderOptions": {"queryType": query_type, "mode": "list", "columns": [], "filters": [], "limit": 0}}}],
    }
    req = urllib.request.Request(GRAFANA + "/api/ds/query", data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", "Basic " + AUTH)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            res = json.loads(e.read() or b"{}")
        except ValueError:
            res = {}
        if not res.get("results"):
            return None, "HTTP %d %s" % (e.code, res.get("message", ""))
    except (OSError, ValueError) as e:
        return None, "连不上 Grafana %s：%s" % (GRAFANA, e)
    a = res.get("results", {}).get("A", {})
    if a.get("error"):
        return None, a["error"]
    frames = a.get("frames") or []
    if not frames or not frames[0]["data"]["values"]:
        return [], None
    names = [f["name"] for f in frames[0]["schema"]["fields"]]
    return [dict(zip(names, row)) for row in zip(*frames[0]["data"]["values"])], None


def check_prometheus():
    url = CH_ENV.get("PROMETHEUS_URL", "http://localhost:9090").rstrip("/")
    try:
        with urllib.request.urlopen(url + "/api/v1/targets?state=active", timeout=15) as r:
            targets = json.load(r)["data"]["activeTargets"]
    except Exception as e:  # noqa: BLE001
        fail("连不上 Prometheus %s：%s" % (url, e))
        return
    mine = [t for t in targets if t["labels"].get("job") == CH_ENV.get("INGEST_JOB")]
    if mine and all(t["health"] == "up" for t in mine):
        ok("Prometheus 抓到入库进程")
    else:
        fail("Prometheus 抓不到入库进程。先确认 bin/ingest.sh status 正常；再检查 Prometheus 容器能不能访问 %s:%s。" % (
            CH_ENV.get("MONITOR_HOST"), CH_ENV.get("INGEST_STATUS_PORT")))
    try:
        with urllib.request.urlopen(url + "/api/v1/rules", timeout=15) as r:
            groups = json.load(r)["data"]["groups"]
        n = sum(len(g["rules"]) for g in groups if g["name"] == CH_ENV.get("RULES_GROUP"))
        if n:
            ok("Prometheus 加载了 %d 条告警规则" % n)
        else:
            fail("Prometheus 没有加载告警规则")
    except Exception as e:  # noqa: BLE001
        fail("读不到 Prometheus 规则：%s" % e)


def check_data(minutes):
    rows, err = ds_query(
        "SELECT (SELECT uniqExact(request_id) FROM %s WHERE $__timeFilter(ts)) AS s, "
        "(SELECT count() FROM vllm_pd.status_doc WHERE $__timeFilter(ts)) AS st, "
        "(SELECT dateDiff('second', max(ts), now64(3)) FROM vllm_pd.request_doc WHERE $__timeFilter(ts)) AS age" % DOC, minutes=minutes)
    if err is not None:
        fail("Grafana 查不了 ClickHouse：%s" % err[:300])
        return False
    r = rows[0] if rows else {"s": 0, "st": 0, "age": None}
    if not r["s"] or not r["st"]:
        fail("最近 %d 分钟 ClickHouse 里没有数据（请求文档 %s 个，状态文档 %s 份）。在监控机上执行 bin/ingest.sh status。" % (minutes, r["s"], r["st"]))
        return False
    ok("最近 %d 分钟：请求文档 %d 个，状态文档 %d 份，最新请求距现在 %s 秒" % (minutes, r["s"], r["st"], r["age"]))
    rows, _ = ds_query("SELECT DISTINCT instance FROM vllm_pd.status_doc WHERE $__timeFilter(ts) AND scope = 'instance'", minutes=minutes)
    by_status = {x["instance"] for x in rows or []}
    rows, _ = ds_query("SELECT DISTINCT name FROM (SELECT arrayJoin([route_p, route_d]) AS name FROM %s WHERE $__timeFilter(ts)) "
                       "WHERE name != ''" % DOC, minutes=minutes)
    by_route = {x["name"] for x in rows or []}
    names = sorted(by_status | by_route, key=lambda n: (not n.startswith("P"), n))
    no_req = [n for n in names if n not in by_route]
    no_status = [n for n in names if n not in by_status]
    if no_req:
        fail("这些实例有状态文档，但没有请求经过：%s" % " ".join(no_req))
    if no_status:
        fail("这些实例出现在请求文档的 route 里，但没有状态文档（不在采集进程的实例列表中？）：%s" % " ".join(no_status))
    if names and not no_req and not no_status:
        ok("全部 %d 个实例都有数据（P %d 个、D %d 个）" % (len(names), sum(1 for n in names if n.startswith("P")),
                                                     sum(1 for n in names if not n.startswith("P"))))
    # Requests older than 3 minutes (their documents are closed by then), not the first minute after the data starts.
    rows, _ = ds_query(
        "SELECT count() AS n, countIf(route_p != '' AND route_d != '' AND %s) AS full FROM %s WHERE $__timeFilter(ts) "
        "AND ts < now64(3) - INTERVAL 3 MINUTE AND ts >= (SELECT min(ts) FROM vllm_pd.request_doc) + INTERVAL 60 SECOND" % (PROCS_OK, DOC),
        minutes=minutes)
    clock(r["age"], minutes)
    json_check(minutes)
    if rows and rows[0]["n"]:
        share = rows[0]["full"] / rows[0]["n"]
        msg = "3 到 %d 分钟前的 %d 个请求里，%.1f%% 的 P、D 两侧都有，且两侧三类进程（api、engine、worker）都有记录" % (minutes, rows[0]["n"], share * 100)
        (ok if share > 0.9 else fail)(msg)
    return True


def clock(age, minutes):
    """Whether a machine's clock is far off: the newest side start of each instance (when the request data is fresh) and
    the newest status document of the fetcher (written every minute), against this machine's clock."""
    rows = []
    if age is not None and age <= 60:
        got, _ = ds_query(
            "SELECT name, dateDiff('second', now64(3), max(t)) AS d FROM (SELECT route_p AS name, p_start AS t FROM %s WHERE route_p != '' "
            "AND isNotNull(p_start) UNION ALL SELECT route_d AS name, d_start AS t FROM %s WHERE route_d != '' AND isNotNull(d_start)) "
            "WHERE t > now() - INTERVAL 2 HOUR AND t < now() + INTERVAL 2 HOUR GROUP BY name" % (DOC, DOC), minutes=120)
        rows += got or []
    got, _ = ds_query("SELECT '采集进程' AS name, dateDiff('second', now64(3), max(ts)) AS d FROM vllm_pd.status_doc WHERE scope = 'fetcher' "
                      "AND ts > now() - INTERVAL 2 HOUR AND ts < now() + INTERVAL 2 HOUR HAVING count() > 0", minutes=120)
    rows += got or []
    if not rows:
        return
    rows.sort(key=lambda x: x["d"])
    low, high = rows[0], rows[-1]
    line = "各实例最新请求和采集进程最新状态文档的时间和监控机相差：最慢 %s %+d 秒，最快 %s %+d 秒（含几秒到几十秒的写入延迟）" % (
        low["name"], low["d"], high["name"], high["d"])
    if low["d"] < -300 or high["d"] > 300:
        fail(line + "。超过 5 分钟：先校时，或者确认那台机器还在写记录。")
    else:
        ok(line)


def json_check(minutes):
    """The request documents of 20 random requests that reached both sides: request_id, schema, both sides' official numbers."""
    rows, err = ds_query("SELECT request_id FROM %s WHERE $__timeFilter(ts) AND ts < now64(3) - INTERVAL 30 SECOND "
                         "AND route_p != '' AND route_d != '' ORDER BY rand() LIMIT 20" % DOC, minutes=minutes)
    if not rows:
        return
    bad = 0
    for row in rows:
        docs, err = ds_query("SELECT doc FROM vllm_pd.request_json(id='%s')" % row["request_id"].replace("'", ""), minutes=minutes)
        doc = json.loads(docs[0]["doc"]) if docs else {}
        lat = doc.get("latency_ms", {})
        if doc.get("request_id") != row["request_id"] or doc.get("schema") != SCHEMA or "official" not in lat:
            bad += 1
    if bad:
        fail("抽查 %d 个请求的 JSON，%d 个不是 %s 或没有 request_id、latency_ms.official" % (len(rows), bad, SCHEMA))
    else:
        ok("抽查 %d 个请求的 JSON：都是 %s，request_id 对得上，两侧官方 E2E/TTFT 在 latency_ms.official" % (len(rows), SCHEMA))


def subst(sql, values):
    def one(m):
        return values[m.group(1)]
    sql = sql.replace("${instance:singlequote}", values["instance"]).replace("${model}", values["model"]).replace("$model", values["model"])
    return re.sub(r"\$\{(order_by|request_id|level)\}", one, sql)


def panels_of(board):
    for p in board["panels"]:
        yield p
        for q in p.get("panels") or []:
            yield q


def queried(board):
    """The panels that run a query (rows and text panels do not)."""
    return [p for p in panels_of(board) if p.get("type") != "row" and any(t.get("rawSql") for t in p.get("targets") or [])]


def check_panels(minutes, all_orders=False):
    with open(PACK / "generated" / "vllm-pd-request.json", encoding="utf-8") as f:
        board = json.load(f)
    variables = {v["name"]: v for v in board["templating"]["list"]}
    values = {"order_by": "d_e2e_ms", "level": "3"}
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
    for p in queried(board):
        n += 1
        t = next(t for t in p["targets"] if t.get("rawSql"))
        rows, err = ds_query(subst(t["rawSql"], values), t.get("format", 1), t.get("queryType", "table"), minutes)
        if err:
            fail("panel %-44s 查询出错：%s" % (p.get("title", ""), err[:400]))
        elif not rows and p.get("title") in EMPTY_OK:
            good += 1
            print("OK    panel %-44s 0 行（%s）" % (p["title"], EMPTY_OK[p["title"]]))
        elif not rows:
            fail("panel %-44s 没有数据" % p.get("title", ""))
        else:
            good += 1
            print("OK    panel %-44s %d 行" % (p.get("title", ""), len(rows)))
    print("      %d 个 panel，%d 个有数据" % (n, good))
    if all_orders:
        for order in ("p_ttft_ms", "p_total_ms", "d_total_ms", "tpot_ms"):
            vals = dict(values, order_by=order)
            bad = 0
            for p in queried(board):
                t = next(t for t in p["targets"] if t.get("rawSql"))
                if "${order_by}" not in t["rawSql"]:
                    continue
                rows, err = ds_query(subst(t["rawSql"], vals), minutes=minutes)
                if err or not rows:
                    bad += 1
                    fail("按 %s 排序时 panel %s：%s" % (order, p.get("title", ""), (err or "没有数据")[:300]))
            if not bad:
                ok("按 %s 排序，Top-20 的 panel 都有数据" % order)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=int, default=15)
    ap.add_argument("--no-prometheus", action="store_true")
    ap.add_argument("--all-orders", action="store_true", help="also try every sort order of the Top-20 panels")
    args = ap.parse_args()
    if not args.no_prometheus:
        check_prometheus()
    if check_data(args.minutes):
        check_panels(args.minutes, args.all_orders)
    print("RESULT: %s" % ("FAIL (%d 项)" % len(problems) if problems else "OK"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
