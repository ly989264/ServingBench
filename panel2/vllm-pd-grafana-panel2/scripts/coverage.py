#!/usr/bin/env python3
"""v3.1-extra: how many of the latest requests have a value in each segment of this cluster's segments.json, read-only.

  bin/coverage.sh [请求数，默认 200] [分钟，默认 30]

Takes the latest requests of the window (by the API server's rows of the current config_id), reads each one's
per-request JSON (vllm_pd.request_json, the same document the dashboard's single-request panel shows) and counts, per
segment, the requests where it is not empty. A segment is "有值" when at least one request has it. The last line:
RESULT: OK when every segment that has a source in segments.json has a value, else the empty ones. Segments without a
source (P's api gateway) are listed apart. A segment that only some requests go through is told in the list: the cache
pool's load only for requests that hit it, the save and Mooncake's three only for requests whose KV P saved.
With segment definitions that count a shared cache-pool task for each member (shared_members, C9 in the manual 3.6.4),
a block before the summary line tells, for each of P's segments from those records, in how many requests the value
comes from a task shared by several requests (members[] has more than one request): the dashboard keeps a request's
largest record per column, and a shared task is often larger than the request's own. Only here, not on the dashboard.
Runs ClickHouse queries as the admin account inside the cluster's container (bin/lib.sh ch_sql), like the other checks.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from segtree import Tree  # noqa: E402

ONLY_SOME = {
    "p.start_load_kv": "只在命中缓存池的请求上有值", "p.start_load_kv.submit": "只在命中缓存池的请求上有值",
    "p.start_load_kv.wait_for_transfer": "只在命中缓存池的请求上有值",
    "b.wait_for_save": "只在 P 保存了 KV 的请求上有值",
}


def ch(sql):
    r = subprocess.run(["docker", "exec", "-i", os.environ["CLICKHOUSE_CONTAINER"], "clickhouse-client", "--password",
                        os.environ["CH_ADMIN_PASSWORD"], "-q", sql], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True)
    if r.returncode:
        raise SystemExit("查不了 ClickHouse：%s" % r.stderr.strip()[:300])
    return r.stdout


def paths(nodes, prefix):
    for n in nodes:
        yield prefix + n.id, n
        yield from paths(n.children, prefix + n.id + ".")


def value(doc, key):
    grp, path = key.split(".", 1)
    if grp == "r":
        node = doc["tpot"]["d"]
    elif grp in ("b", "bd"):
        node = doc["background"]["p" if grp == "b" else "d"]
    else:
        node = doc["ttft"][grp]
    try:
        parts = path.split(".")
        n = node[parts[0]]
        for p in parts[1:]:
            n = n["sub"][p]
        return n["ms"]
    except (KeyError, TypeError):
        return None


def shared_report(tree, seg, ids, minutes):
    """C9: for P's segments whose source is a record kind of shared_members, the requests whose value comes from a shared
    task. The collector writes a shared task as one row per member, identical but for the request: rows of one instance
    with the same time and the same values under several requests are one shared task. A request's value comes from a
    shared task when the largest of its shared rows is at least the largest of its own rows (the dashboard keeps the
    largest). Prints nothing for segment definitions without shared_members."""
    kinds = set(tree.raw.get("shared_members") or [])
    picked = []
    for key, node in seg:
        if key[0] in "pb" and node.src and any(isinstance(x, dict) and x.get("event") in kinds for x in node.src):
            picked.append((key, node.column))
    if not kinds or not picked or not ids:
        return
    cols = [c for _, c in picked]
    inner = ("SELECT request_id, %s, uniqExact(request_id) OVER (PARTITION BY ts, instance, node, toString(tuple(%s))) > 1 AS sh "
             "FROM vllm_pd.phase_line_cur WHERE role = 'prefill' AND ts > now() - INTERVAL %d MINUTE AND (%s)") % (
        ", ".join(cols), ", ".join(cols), minutes + 10, " OR ".join("isNotNull(%s)" % c for c in cols))
    per_req = ", ".join("isNotNull(max({c})) AS v{i}, isNotNull(maxIf({c}, sh)) AND (isNull(maxIf({c}, NOT sh)) OR "
                        "maxIf({c}, sh) >= maxIf({c}, NOT sh)) AS s{i}".format(c=c, i=i) for i, c in enumerate(cols))
    outer = ", ".join("countIf(v{i}), countIf(s{i})".format(i=i) for i in range(len(cols)))
    sql = "SELECT %s, countIf(%s), countIf(%s) FROM (SELECT request_id, %s FROM (%s) WHERE request_id IN (%s) GROUP BY request_id)" % (
        outer, " OR ".join("v%d" % i for i in range(len(cols))), " OR ".join("s%d" % i for i in range(len(cols))), per_req, inner,
        ", ".join("'%s'" % r.replace("'", "") for r in ids))
    vals = [int(x) for x in ch(sql + " FORMAT TSV").split()]
    if len(vals) != 2 * len(cols) + 2:
        print("共用任务的统计没有算出来（查询结果 %d 项）" % len(vals))
        return
    pct = lambda a, b: "（%d%%）" % round(100.0 * a / b) if b else ""
    print("P 缓存池各段的值来自共用任务（members[] 不止一个请求）的请求数 / 有值的请求数（C9；只在这里报，不进看板）：")
    for i, (key, _) in enumerate(picked):
        name = "P %s%s" % ("后台 " if key.startswith("b.") else "", key.split(".", 1)[1].replace(".", "/"))
        print("  %-48s %4d / %d%s" % (name, vals[2 * i + 1], vals[2 * i], pct(vals[2 * i + 1], vals[2 * i])))
    print("  %-48s %4d / %d%s" % ("这几段里任一段", vals[-1], vals[-2], pct(vals[-1], vals[-2])))


def main():
    n_req = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    minutes = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    tree = Tree()
    seg = []
    for role, grp_phases in (("prefill", (("p", ("proxy", "ttft")), ("b", ("background",)))),
                             ("decode", (("d", ("proxy", "ttft")), ("r", ("tpot",)), ("bd", ("background",))))):
        for grp, phases in grp_phases:
            for ph in phases:
                for key, node in paths(tree.sides[role][ph], grp + "."):
                    seg.append((key, node))
    ids = [x for x in ch("SELECT request_id FROM vllm_pd.phase_line_cur WHERE proc = 'api' AND part = '' AND ts > now() - INTERVAL %d MINUTE "
                         "GROUP BY request_id ORDER BY max(ts) DESC LIMIT %d" % (minutes, n_req)).split() if x]
    docs = []
    for rid in ids:
        out = ch("SELECT doc FROM vllm_pd.request_json(id = '%s') FORMAT TSVRaw" % rid.replace("'", "")).strip()
        if out:
            docs.append(json.loads(out))
    print("段定义 %s：最近 %d 分钟里 %d 个请求（最多取 %d 个）" % (tree.raw.get("config_id"), minutes, len(docs), n_req))
    if not docs:
        print("RESULT: FAIL（这段时间没有请求：先发一批请求再跑）")
        return 1
    have, empty, nosrc = [], [], []
    for key, node in seg:
        sourced = any(m.src for m in node.walk())
        k = sum(1 for d in docs if isinstance(value(d, key), (int, float)))
        side = "P" if key[0] in "pb" else "D"
        name = "%s %s%s" % (side, {"b": "后台 ", "bd": "后台 ", "r": "第二步起 "}.get(key.split(".")[0], ""), key.split(".", 1)[1].replace(".", "/"))
        print("  %-48s %4d / %d%s" % (name, k, len(docs), ("（%s）" % ONLY_SOME[key]) if key in ONLY_SOME else ""))
        if not sourced:
            nosrc.append(name)
        elif k:
            have.append(name)
        else:
            empty.append(name)
    shared_report(tree, seg, ids, minutes)
    total = len(seg)
    print("%d 段里 %d 段有值；没有来源、按设计为空的 %d 段：%s" % (total, len(have), len(nosrc), "、".join(nosrc) or "无"))
    if empty:
        print("RESULT: FAIL（有来源但这些请求里都为空的 %d 段：%s）" % (len(empty), "、".join(empty)))
        return 1
    print("RESULT: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
