#!/usr/bin/env python3
"""Post-deployment check, on the monitoring machine, from the documents in ClickHouse (bin/postdeploy_check.sh).

For the first day in a new environment: it finds the differences between the real cluster and what this pack expects.
  1. instances: every instance with status documents (the fetcher's instance list) has had filter self-check lines read
     by the fetcher; every instance named in route.p / route.d of a request document has status documents;
  2. each instance has its processes: api, engine, worker (the processes of its latest status document);
  3. the filters' self-check lines report 0 errors, and every instance uses the fetcher's segment definition version,
     which is in segment_def;
  4. P and D are paired: the share of requests whose document names both route.p and route.d, by closing reason;
  5. clocks: the gap from P's end (sides.p.start + P's official E2E) to D's start (negative means D's clock is behind
     P's, or the pairing is wrong);
  6. counted, not a failure: requests whose document was written more than once (the queries read the latest version).
Status documents: the latest one of each instance in the last 3 minutes (the fetcher writes one a minute).
Runs the ClickHouse queries with the read-only account of grafana.env. Prints RESULT: OK or RESULT: FAIL at the end.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import criteria as C  # noqa: E402
from segdef import Reader, grafana_env  # noqa: E402

DOC = "vllm_pd.request_doc FINAL"
REQUEST_PROCS = ("api", "engine", "worker")
problems = []


def ok(msg):
    print("OK    " + msg)


def fail(msg):
    problems.append(msg)
    print("FAIL  " + msg)


def latest_status(reader, scope):
    rows = reader.rows("SELECT instance, argMax(doc, ts) AS doc FROM vllm_pd.status_doc WHERE scope = '%s' "
                       "AND ts >= now64(3) - INTERVAL 3 MINUTE GROUP BY instance ORDER BY instance" % scope)
    out = {}
    for r in rows:
        try:
            out[r["instance"]] = json.loads(r["doc"])
        except ValueError:
            out[r["instance"]] = {}
    return out


def since_change(fdoc):
    import datetime
    recent = fdoc.get("segment_changes_recent") or []
    if not recent or not isinstance(recent[-1], dict):
        return None
    try:
        t = datetime.datetime.strptime(str(recent[-1].get("ts", "")).replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z").timestamp()
    except ValueError:
        return None
    return max(0, int(time.time() - t))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=int, default=10)
    a = ap.parse_args()
    reader = Reader(grafana_env())
    win = "ts >= now64(3) - INTERVAL %d MINUTE" % a.minutes

    # 1. instances
    known = {r["instance"] for r in reader.rows("SELECT DISTINCT instance FROM vllm_pd.status_doc WHERE scope = 'instance' "
                                                 "AND ts >= now64(3) - INTERVAL 7 DAY")}
    status = latest_status(reader, "instance")
    routed = {r["name"] for r in reader.rows("SELECT DISTINCT name FROM (SELECT arrayJoin([route_p, route_d]) AS name FROM %s "
                                              "WHERE %s) WHERE name != ''" % (DOC, win))}
    if not known:
        fail("最近 7 天没有实例的状态文档 → 入库进程和采集进程是否在运行（bin/ingest.sh status）")
    gone = sorted(known - set(status))
    if gone:
        fail("这些实例最近 3 分钟没有状态文档（采集进程每分钟写一份）：%s" % " ".join(gone))
    quiet = sorted(n for n, d in status.items() if not d.get("processes"))
    if quiet:
        fail("采集进程还没有读到这些实例的过滤器自检行：%s → 看采集进程配置里这些实例的地址，以及 vLLM 是否加载了过滤器" % " ".join(quiet))
    elif status:
        ok("%d 个实例都有状态文档，采集进程都读到了过滤器的自检行" % len(status))
    strange = sorted(routed - known)
    if strange:
        fail("这些实例出现在请求文档的 route 里，但没有状态文档：%s → 采集进程配置的实例列表是否漏了它们" % " ".join(strange))

    # 2. processes per instance
    bad = []
    for n, d in sorted(status.items()):
        have = {p.get("role") for p in d.get("processes") or []}
        if d.get("processes") and not set(REQUEST_PROCS) <= have:
            bad.append("%s 缺 %s" % (n, "、".join(sorted(set(REQUEST_PROCS) - have))))
    if bad:
        fail("这些实例缺进程的自检行：%s → 子进程没有用到 logging 配置，或这类进程还没有写过记录（先发几个请求）" % "；".join(bad[:10]))
    elif status and not quiet:
        ok("每个实例的进程都有自检行：api、engine、worker 三类")

    # 3. self-check errors and the segment definition version
    if status:
        errs = []
        for n, d in sorted(status.items()):
            for p in d.get("processes") or []:
                if int(p.get("errors") or 0) or int(p.get("config_errors") or 0):
                    errs.append("%s %s pid %s 出错 %s 映射出错 %s" % (n, p.get("role"), p.get("pid"), p.get("errors", 0), p.get("config_errors", 0)))
        if errs:
            fail("这些进程的自检行出错数不为 0：%s" % "；".join(errs[:10]))
        else:
            ok("%d 个进程的自检行出错数都是 0" % sum(len(d.get("processes") or []) for d in status.values()))
        fet = latest_status(reader, "fetcher")
        fdoc = next(iter(fet.values()), {})
        defined = {r["v"] for r in reader.rows("SELECT DISTINCT config_version AS v FROM vllm_pd.segment_def")}
        versions = {n: d.get("config_version") or "" for n, d in status.items()}
        for v in C.judge_config_versions(versions, fdoc.get("config_version") or "", defined, since_change(fdoc)):
            if v.level == "FAIL":
                fail(v.text)
            elif v.level == "OK":
                ok(v.text)
            else:
                print("      " + v.text)

    # 4. pairing P and D, 5. the gap from P's end to D's start
    # Requests from 2 to N + 15 minutes ago: their documents are closed by then (a request still waiting on a side is
    # written when the fetcher gives up on it).
    span = "ts >= now64(3) - INTERVAL %d MINUTE AND ts < now64(3) - INTERVAL 2 MINUTE" % (a.minutes + 15)
    rows = reader.rows("SELECT countIf(route_p != '') AS p, countIf(route_d != '') AS d, countIf(route_p != '' AND route_d != '') AS both "
                       "FROM %s WHERE %s" % (DOC, span))
    closed = {(x["closed"] or "—"): int(x["n"]) for x in reader.rows("SELECT closed, count() AS n FROM %s WHERE %s GROUP BY closed" % (DOC, span))}
    r = rows[0] if rows else {}
    p, d, both = int(r.get("p", 0)), int(r.get("d", 0)), int(r.get("both", 0))
    if not p and not d:
        fail("2 到 %d 分钟前没有请求（没有 route.p 或 route.d 的请求文档）" % (a.minutes + 15))
    else:
        sp = both / p if p else 0.0
        sd = both / d if d else 0.0
        line = "P、D 配对：P 侧 %d 个请求里 %.1f%% 在 D 侧也有，D 侧 %d 个里 %.1f%% 在 P 侧也有（关闭原因：%s）" % (
            p, sp * 100, d, sd * 100, "，".join("%s %d" % kv for kv in sorted(closed.items())))
        (ok if min(sp, sd) >= 0.95 else fail)(line + ("" if min(sp, sd) >= 0.95 else
                                                    " → 不到 95%：采集进程的实例列表是否漏了 P 或 D、proxy 发给两侧的 X-Request-Id 是否相同"))
        if both:
            q = reader.rows(
                "SELECT quantiles(0.01, 0.5, 0.99)(gap) AS q, min(gap) AS lo, countIf(gap < 0) AS neg FROM ("
                "SELECT toFloat64(dateDiff('millisecond', p_start, d_start)) - p_e2e_ms AS gap FROM %s "
                "WHERE %s AND route_p != '' AND route_d != '' AND isNotNull(p_start) AND isNotNull(d_start) AND isNotNull(p_e2e_ms))" % (DOC, span))
            g = q[0] if q else {}
            if g.get("q") and g.get("lo") is not None:
                qq = g["q"]
                line = "P 末到 D 首的间隔：最小 %.1f ms，p1 %.1f ms，p50 %.1f ms，p99 %.1f ms；为负的 %d 个" % (
                    float(g["lo"]), qq[0], qq[1], qq[2], int(g["neg"]))
                (ok if int(g["neg"]) == 0 else fail)(line + ("" if int(g["neg"]) == 0 else
                                                             " → 为负说明 D 机器的钟比 P 慢，或者配对错了：先校时（chrony/ntp）"))

    # 6. counted: documents written more than once
    rows = reader.rows("SELECT countIf(n > 1) AS dup, count() AS reqs FROM (SELECT request_id, count() AS n FROM vllm_pd.request_doc "
                       "WHERE %s GROUP BY request_id)" % win)
    r = rows[0] if rows else {"dup": 0, "reqs": 0}
    ok("文档写过不止一版的请求：%s 个（共 %s 个请求；查询取 doc_version 最大的一版）" % (r["dup"], r["reqs"]))
    print("RESULT: %s" % ("FAIL（%d 项）" % len(problems) if problems else "OK"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
