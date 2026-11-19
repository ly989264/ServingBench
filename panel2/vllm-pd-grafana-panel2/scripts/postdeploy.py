#!/usr/bin/env python3
"""Post-deployment check, on the monitoring machine, from what has reached ClickHouse (bin/postdeploy_check.sh).

For the first day in a new environment: it finds the differences between the real logs and what this pack expects.
  1. new-format lines: every instance of inventory.json has files named by the filter, and the collector parsed every line;
     no instance folder holds two DP ranks (two vllm serve sharing one --port folder);
  2. only request-level events and the filter's own lines arrive (anything else lands in vllm_pd.filter_other);
  3. each instance has its processes: api, engine, worker on P and on D;
  4. the filters' self-check lines report 0 errors and the segments.json the monitoring machine pushed;
  5. P and D are paired: the share of requests whose key X appears on both sides;
  6. clocks: the gap from P's end to D's start (negative means D's clock is behind P's, or the pairing is wrong);
  7. the filter's cumulative sums never decrease within a part;
  8. counted, not a failure: requests with more than one core_summary[request] (the one written last counts), and D
     requests whose first-step detokenize range is not among the ranges kept (both of D's detokenizer segments empty).
Requests are read from vllm_pd.phase_line_cur, the rows of this segments.json's config_id.
Settings come from the environment (bin/lib.sh loads deploy.env). Prints RESULT: OK or RESULT: FAIL at the end.
"""
import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import urllib.request

ENV = os.environ
PACK = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PACK / "scripts"))
from inventory_tool import load as load_inventory  # noqa: E402
from segtree import Tree  # noqa: E402
from names import Names, NamesError  # noqa: E402

try:
    N = Names(ENV)
except NamesError as e:
    raise SystemExit("错误：%s" % e)
problems = []


def ok(msg):
    print("OK    " + msg)


def fail(msg):
    problems.append(msg)
    print("FAIL  " + msg)


def info(msg):
    print("      " + msg)


def ch(sql):
    cmd = ["docker", "exec", "-i", N.get("CLICKHOUSE_CONTAINER"), "clickhouse-client",
           "--password", ENV.get("CH_ADMIN_PASSWORD", ""), "-q", sql + " FORMAT JSONEachRow"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:400])
    return [json.loads(x) for x in out.stdout.splitlines() if x.strip()]


def expected(inv):
    out = {}
    for side, letter in (("prefill", "P"), ("decode", "D")):
        for i, a in enumerate(inv[side]["api_servers"]):
            out["%s%d" % (letter, i)] = a
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--minutes", type=int, default=10)
    a = ap.parse_args()
    inv = load_inventory(str(PACK / "inventory.json"))
    tree = Tree()
    names = expected(inv)
    win = "ts >= now64(3) - INTERVAL %d MINUTE" % a.minutes

    # 1. files and parsing
    rows = ch("SELECT instance, groupUniqArray(role) AS roles, max(ts) AS last FROM vllm_pd.filter_file WHERE ts >= now64(3) - INTERVAL 7 DAY GROUP BY instance")
    seen = {r["instance"]: r for r in rows}
    missing = [n for n in names if n not in seen]
    strange = [n for n in seen if n not in names]
    if missing:
        fail("清单上的这些端口没有过滤器写的文件：%s → 看 bin/nodes.sh paths 里它的目录、启动脚本的环境变量、挂载和 vllm serve 的 --port" % " ".join(
            "%s（%s）" % (n, names[n]) for n in missing))
    else:
        ok("清单上的 %d 个实例都有过滤器写的文件" % len(names))
    if strange:
        fail("这些实例名不在清单里：%s → vllm serve 的 --port 要和清单里这台机器的端口一致；设了 PD_DIAG_INSTANCE 的话要写清单算出的名字（P0、D17）、端口号或 auto" % " ".join(strange))
    # One folder, one DP rank, among the processes running now: those with a self-check line in the last 3 minutes (the
    # filter writes one a minute), so that processes stopped before a fix drop out about 3 minutes after the restart.
    # The rank: --data-parallel-rank of the command line (self-check line), else the EngineCore's process name
    # (EngineCore_DP17) or its dp_rank.
    live = ("SELECT DISTINCT node, instance, pid FROM vllm_pd.filter_selfcheck WHERE ts >= now64(3) - INTERVAL 3 MINUTE")
    rows = ch("SELECT instance, arraySort(groupUniqArray(r)) AS ranks FROM ("
              "SELECT instance, toString(dp_rank_argv) AS r FROM vllm_pd.filter_selfcheck "
              "WHERE ts >= now64(3) - INTERVAL 3 MINUTE AND dp_rank_argv IS NOT NULL "
              "UNION ALL SELECT instance, if(match(proc_name, '^EngineCore_DP[0-9]+$'), replaceRegexpOne(proc_name, '^EngineCore_DP', ''), "
              "toString(JSONExtractInt(raw, 'dp_rank'))) AS r FROM vllm_pd.diag_event "
              "WHERE event = 'core_summary[request]' AND ts >= now64(3) - INTERVAL %d MINUTE AND (node, instance, pid) IN (%s)) "
              "GROUP BY instance HAVING length(ranks) > 1" % (a.minutes + 15, live))
    if rows:
        fail("这些实例目录里有不止一个 DP rank：%s → 两个 vllm serve 用了同一个 --port 目录，或 PD_DIAG_INSTANCE 写成了同一个值；不设它，或设成 auto，"
             "重启 vLLM，3 分钟后再查（只算还在运行的进程）" % "；".join(
                 "%s（rank %s）" % (r["instance"], "、".join(r["ranks"])) for r in rows[:10]))
    else:
        ok("每个实例目录里只有一个 DP rank")
    rows = ch("SELECT instance, any(node) AS node, arraySort(groupUniqArray(pid)) AS pids FROM vllm_pd.filter_selfcheck "
              "WHERE ts >= now64(3) - INTERVAL 5 MINUTE AND instance_source = 'default' GROUP BY instance")
    if rows:
        fail("这些实例的身份是缺省值（命令行上没有 --port，按 vLLM 默认的 8000 算）：%s → 在 vllm serve 的命令行上写 --port；"
             "端口只写在 --config 文件里时，命令行上也要写一次" % "；".join(
                 "%s（%s，pid %s）" % (r["instance"], r["node"], " ".join(str(x) for x in r["pids"][:5])) for r in rows[:10]))
    else:
        ok("每个实例的身份都来自 vllm serve 的 --port 或手工设的 PD_DIAG_INSTANCE")
    dead = subprocess.run(["docker", "exec", N.get("CENTRAL_CONTAINER"), "sh", "-c",
                           "cat /var/lib/vector/dead-letter-*.jsonl 2>/dev/null | wc -l"], capture_output=True, text=True)
    n_dead = int((dead.stdout or "0").strip() or 0)
    if dead.returncode == 0 and n_dead == 0:
        ok("采集器没有解析失败的行（行前缀和 JSON 都是新格式）")
    elif dead.returncode == 0:
        sample = subprocess.run(["docker", "exec", N.get("CENTRAL_CONTAINER"), "sh", "-c",
                                 "tail -n 1 $(ls -t /var/lib/vector/dead-letter-*.jsonl | head -n 1)"], capture_output=True, text=True).stdout
        fail("采集器有解析失败的行（抽样 %d 行，在 /var/lib/vector/dead-letter-*.jsonl）。一行样子：%s" % (n_dead, sample.strip()[:300]))
    else:
        fail("读不到采集器的 dead-letter 文件：%s" % dead.stderr.strip()[:200])

    # 2. nothing but request-level events and the filter's own lines, from the processes running now (a self-check line in
    # the last 2 minutes; the filter writes one a minute): right after an update, processes of the earlier version wrote
    # its lines until they were restarted, and drop out of this check 2 minutes after the restart.
    alive = "SELECT DISTINCT node, instance, pid FROM vllm_pd.filter_selfcheck WHERE ts >= now64(3) - INTERVAL 2 MINUTE"
    rows = ch("SELECT event, count() AS n FROM vllm_pd.filter_other WHERE %s AND (node, instance, pid) IN (%s) GROUP BY event ORDER BY n DESC LIMIT 10" % (win, alive))
    if rows:
        fail("节点上有白名单以外的事件到了监控机：%s → 过滤器没有生效（看自检行），或 segments.json 的白名单要加" % ", ".join("%s %s 行" % (r["event"], r["n"]) for r in rows))
    else:
        ok("最近 %d 分钟只有请求级记录和过滤器自己的行" % a.minutes)

    # 3. processes per instance
    bad = []
    for n in names:
        want = {"api", "engine", "worker"}
        have = set(seen.get(n, {}).get("roles", []))
        if n in seen and not want <= have:
            bad.append("%s 缺 %s" % (n, "、".join(sorted(want - have))))
    if bad:
        fail("这些实例缺进程的文件：%s → 子进程没有用到 logging 配置，或这类进程还没有写过行（先发几个请求）" % "；".join(bad[:10]))
    elif not missing:
        ok("每个实例的进程都有文件：api、engine、worker 三类")

    # 4. self-check lines
    raw = (PACK / "segments.json").read_bytes()
    want_version = "%s@%s" % (json.loads(raw).get("config_id", "?"), hashlib.sha1(raw).hexdigest()[:8])
    # The processes running now (their latest self-check line within 2 minutes), as in 2.
    rows = ch("SELECT instance, pid, argMax(errors, ts) AS errors, argMax(config_errors, ts) AS cerr, argMax(config_version, ts) AS ver, "
              "argMax(inflight, ts) AS inflight, max(ts) AS last FROM vllm_pd.filter_selfcheck WHERE ts >= now64(3) - INTERVAL 5 MINUTE "
              "GROUP BY instance, pid HAVING last >= now64(3) - INTERVAL 2 MINUTE")
    if not rows:
        fail("最近 2 分钟没有过滤器的自检行 → 过滤器每分钟写一行；没有就是过滤器没有加载或进程没有运行")
    else:
        errs = [r for r in rows if int(r["errors"]) or int(r["cerr"])]
        vers = sorted({r["ver"] for r in rows})
        if errs:
            fail("这些进程的自检行出错数不为 0：%s" % "；".join("%s pid %s 出错 %s 映射出错 %s" % (r["instance"], r["pid"], r["errors"], r["cerr"]) for r in errs[:10]))
        else:
            ok("%d 个进程的自检行出错数都是 0" % len(rows))
        if vers != [want_version]:
            fail("节点上的 segments.json 版本是 %s，监控机上是 %s → 执行 bin/nodes.sh push；过滤器本身也换了时（换包），vLLM 要重启才用上新的" % (" ".join(vers), want_version))
        else:
            ok("节点上的 segments.json 和监控机一致（%s）" % want_version)

    # 5. pairing P and D by X, 6. the gap from P's end to D's start
    # Only requests that started a minute or more after every instance switched to this segments.json (its first file
    # with this version): a request in flight while the nodes switched has one side written by the earlier version,
    # whose rows this version's view does not read, and would count as unpaired. Restarting the same version (add
    # restarts the mock every time) does not move this point.
    # Per instance: its first self-check line of this version after its last line of any other version (a rollback and
    # a second update switch it twice). Self-check lines, not file headers: a filter rereads segments.json without
    # reopening its files, so a file's header keeps the version it was opened with (v2.1 design 1.2, limit 2).
    w = want_version.replace("'", "")
    t = ch("SELECT toString(max(sw)) AS t FROM (SELECT node, instance, minIf(ts, config_version = '%s' AND ts > other) AS sw FROM ("
           "SELECT node, instance, ts, config_version, maxIf(ts, config_version != '%s') OVER (PARTITION BY node, instance) AS other "
           "FROM vllm_pd.filter_selfcheck WHERE ts >= now64(3) - INTERVAL 7 DAY) GROUP BY node, instance HAVING countIf(config_version = '%s') > 0)" % (w, w, w))
    since = (t[0]["t"] if t and t[0].get("t") and not t[0]["t"].startswith("1970") else "1970-01-01 00:00:00")
    # A request counts on a side if any row of that side arrived: D's API row only comes when the request ends, its engine rows within seconds.
    # Requests are taken from 2 to N minutes ago by P's API row, so that D has had time to start; a request seen only on D
    # counts when its D row is 2 minutes old too, so that a P row still on its way (pulled in a later round) is not missed.
    rows = ch("SELECT countIf(has_p) AS p, countIf(has_d) AS d, countIf(has_p AND has_d) AS both, "
              "quantilesIf(0.01, 0.5, 0.99)(gap, has_p AND has_d AND isNotNull(gap)) AS q, minIf(gap, has_p AND has_d AND isNotNull(gap)) AS lo, "
              "countIf(has_p AND has_d AND gap < 0) AS neg FROM ("
              "SELECT request_id, countIf(role = 'prefill' AND proc = 'api' AND part = '') > 0 AS has_p, countIf(role = 'decode') > 0 AS has_d, "
              "minIf(ts, role = 'prefill' AND proc = 'api' AND part = '') AS p_ts, minIf(ts, role = 'decode') AS d_ts, "
              "toFloat64(dateDiff('millisecond', maxIf(api_end_ts, role = 'prefill'), minIf(api_start_ts, role = 'decode'))) AS gap "
              "FROM vllm_pd.phase_line_cur WHERE ts >= now64(3) - INTERVAL %d MINUTE "
              "GROUP BY request_id HAVING ((has_p AND p_ts < now64(3) - INTERVAL 2 MINUTE) OR (NOT has_p AND has_d AND d_ts < now64(3) - INTERVAL 2 MINUTE)) "
              "AND least(if(has_p, p_ts, d_ts), if(has_d, d_ts, p_ts)) >= toDateTime64('%s', 3) + INTERVAL 1 MINUTE)" % (a.minutes + 15, since))
    r = rows[0] if rows else {}
    p, d, both = int(r.get("p", 0)), int(r.get("d", 0)), int(r.get("both", 0))
    if not p and not d:
        n = ch("SELECT uniqExact(request_id) AS n FROM vllm_pd.phase_line_cur WHERE ts >= now64(3) - INTERVAL %d MINUTE AND proc = 'api' AND part = ''" % a.minutes)
        if n and int(n[0]["n"]):
            ok("节点 %s 换到这一版 segments.json，之后开始、已过 2 分钟的请求还没有：配对和间隔下次核验再看（最近 %d 分钟有 %s 个请求）"
               % (since, a.minutes, n[0]["n"]))
        else:
            fail("最近 %d 分钟没有请求（P、D 两侧的 API 行都没有）" % a.minutes)
    else:
        sp = both / p if p else 0.0
        sd = both / d if d else 0.0
        line = "按 X 配对：P 侧 %d 个请求里 %.1f%% 在 D 侧也有，D 侧 %d 个里 %.1f%% 在 P 侧也有" % (p, sp * 100, d, sd * 100)
        (ok if min(sp, sd) >= 0.95 else fail)(line + ("" if min(sp, sd) >= 0.95 else " → 不到 95%：清单里是否漏了 P 或 D、proxy 发给两侧的 X-Request-Id 是否相同（教程「已知限制」）"))
        if both:
            q = r.get("q") or [None, None, None]
            line = "P 末到 D 首的间隔：最小 %.1f ms，p1 %.1f ms，p50 %.1f ms，p99 %.1f ms；为负的 %d 个" % (float(r["lo"]), q[0], q[1], q[2], int(r["neg"]))
            (ok if int(r["neg"]) == 0 else fail)(line + ("" if int(r["neg"]) == 0 else " → 为负说明 D 机器的钟比 P 慢，或者配对错了：先校时（chrony/ntp），再看已知限制"))

    # 7. cumulative sums never decrease
    checks = " + ".join("toUInt64(argMax(acc__%s__ns, seq) < max(acc__%s__ns))" % (x, x) for x in tree.acc_ids)
    rows = ch("SELECT count() AS parts, countIf(bad > 0) AS bad FROM (SELECT request_id, part, %s AS bad FROM vllm_pd.phase_line_cur "
              "WHERE part != '' AND %s GROUP BY request_id, part)" % (checks, win))
    r = rows[0] if rows else {"parts": 0, "bad": 0}
    if int(r["bad"]):
        fail("%s 个累计段后写的行比先写的小（共 %s 个）→ 把这台机器的日志和 segments.json 发给维护者" % (r["bad"], r["parts"]))
    else:
        ok("%s 个累计段都没有变小" % r["parts"])

    # 8. counted: several core_summary[request] for one request; D's first-step detokenize range not kept
    rows = ch("SELECT countIf(n > 1) AS dup, count() AS reqs FROM (SELECT request_id, attempt_id, role, countIf(isNotNull(core_mono_ns)) AS n "
              "FROM vllm_pd.phase_line_cur WHERE %s GROUP BY request_id, attempt_id, role HAVING n > 0)" % win)
    r = rows[0] if rows else {"dup": 0, "reqs": 0}
    ok("core_summary[request] 多于一条的请求：%s 个（共 %s 个请求·侧；多条时取 timestamp_ns 最晚的一条）" % (r["dup"], r["reqs"]))
    ranges = list(Tree().ranges())
    if ranges:
        name = ranges[0]
        rows = ch("SELECT countIf(notEmpty(rx) AND fe != '' AND indexOf(rx, fe) = 0) AS miss, countIf(notEmpty(rx) AND fe != '') AS n FROM ("
                  "SELECT request_id, argMinIf(first_exec, first_mono_ns, first_exec != '') AS fe, anyIf(rg__%s__exec, notEmpty(rg__%s__exec)) AS rx "
                  "FROM vllm_pd.phase_line_cur WHERE role = 'decode' AND %s GROUP BY request_id, attempt_id)" % (name, name, win))
        r = rows[0] if rows else {"miss": 0, "n": 0}
        ok("D 第一步的 %s 区间不在 ranges 里的请求：%s 个（共 %s 个；这些请求的第一步和第二步起都留空）" % (name, r["miss"], r["n"]))

    # the puller
    try:
        with urllib.request.urlopen("http://%s:%s/status" % (ENV.get("MONITOR_HOST"), ENV.get("PULLER_METRICS_PORT")), timeout=10) as resp:
            ms = json.load(resp)
        stale = [m for m in ms if m["age"] is None or m["age"] > 120]
        if stale:
            fail("这些机器超过 2 分钟没有拉取成功：%s" % "；".join("%s（%s）" % (m["addr"], m["error"][:60]) for m in stale))
        else:
            ok("%d 台机器都在拉取" % len(ms))
    except Exception as e:  # noqa: BLE001
        fail("拉取程序没有运行（%s）→ bin/puller.sh start" % e)
    print("RESULT: %s" % ("FAIL（%d 项）" % len(problems) if problems else "OK"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
