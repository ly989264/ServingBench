#!/usr/bin/env python3
"""How many of the latest requests have a value in each segment of segment_def, read-only.

  bin/coverage.sh [请求数，默认 200] [分钟，默认 30]

Takes the latest requests of the window from vllm_pd.request_doc (the latest document version of each request, the
same document the dashboard's single-request panel shows) and counts, per segment of the segment definitions written
into segment_def last (scripts/segdef.py load_tree), the requests where it is not empty. A segment is "有值" when at
least one request has it. The last line: RESULT: OK when every segment that has a source in the segment definitions
has a value, else the empty ones. Segments without a source (P's api gateway) are listed apart. A segment that only
some requests go through is told in the list: the cache pool's load only for requests that hit it, the save and
Mooncake's three only for requests whose KV P saved.
Every segment has a from_level, the lowest PD_DIAG_LEVEL that records it; each request document carries diag_level per
side (3 when its records carry none). A segment is counted only over the requests whose side ran at its level or
above; a request below it is "未采" (told on the segment's line), and a segment that no request of the window could
have recorded is listed as "按粒度不记", not as FAIL.
Runs the ClickHouse queries with the read-only account of grafana.env.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from segdef import Reader, grafana_env, load_tree  # noqa: E402

ONLY_SOME = {
    "p.start_load_kv": "只在命中缓存池的请求上有值", "p.start_load_kv.submit": "只在命中缓存池的请求上有值",
    "p.start_load_kv.wait_for_transfer": "只在命中缓存池的请求上有值",
    "b.wait_for_save": "只在 P 保存了 KV 的请求上有值",
    # D side: the maxima the connector writes on request_generation, and the scheduler's wait.
    "d.start_load_kv.receive_queue": "只在从 P 读 KV 的请求上有值；各任务中的最大值，任务数 = P 的 TP 端口数 × shard 数",
    "d.start_load_kv.prepare_read": "只在从 P 读 KV 的请求上有值；取对端信息、组读表各自在各任务中的最大值之和",
    "d.kv_ready_wait": "只在走过异步加载的请求上有值；从调度器得知 KV 到齐算起，比字节到齐晚一步；字节到齐在 start_load_kv（接收线程的墙钟包络）里",
}


READER = None     # segdef.Reader with grafana.env's read-only account, set in main()


def ch(sql):
    return READER.query(sql)


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


def main():
    global READER
    READER = Reader(grafana_env())
    n_req = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    minutes = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    tree = load_tree(READER)
    seg = []
    for role, grp_phases in (("prefill", (("p", ("proxy", "ttft")), ("b", ("background",)))),
                             ("decode", (("d", ("proxy", "ttft")), ("r", ("tpot",)), ("bd", ("background",))))):
        for grp, phases in grp_phases:
            for ph in phases:
                for key, node in paths(tree.sides[role][ph], grp + "."):
                    seg.append((key, node))
    ids = [x for x in ch("SELECT request_id FROM vllm_pd.request_doc FINAL WHERE ts > now64(3) - INTERVAL %d MINUTE "
                         "ORDER BY ts DESC LIMIT %d FORMAT TSV" % (minutes, n_req)).split() if x]
    docs = []
    if ids:
        out = ch("SELECT doc FROM vllm_pd.request_doc FINAL WHERE request_id IN (%s) FORMAT JSONEachRow"
                 % ", ".join("'%s'" % r.replace("'", "").replace("\\", "") for r in ids))
        docs = [json.loads(json.loads(x)["doc"]) for x in out.splitlines() if x.strip()]
    print("段定义 %s：最近 %d 分钟里 %d 个请求（最多取 %d 个）" % (tree.config_version, minutes, len(docs), n_req))
    if not docs:
        print("RESULT: FAIL（这段时间没有请求：先发一批请求再跑）")
        return 1
    have, empty, nosrc, by_level, proxy_empty = [], [], [], [], []
    compare = []

    def side_level(doc, key):
        # the request's PD_DIAG_LEVEL on the segment's side (3 when the document has none); 0: the request has no
        # instance on that side (sent to D directly)
        lv = doc.get("diag_level")
        if not isinstance(lv, dict):
            return 3
        return lv.get("p" if key[0] in "pb" else "d", 3)

    def can_have(doc, key, node):
        # a request with no record on the side counts for the level-1 segments, never for the others
        lv = side_level(doc, key)
        return lv >= node.from_level or (lv == 0 and node.from_level == 1)

    def name_of(key):
        side = "P" if key[0] in "pb" else "D"
        return "%s %s%s" % (side, {"b": "后台 ", "bd": "后台 ", "r": "第二步起 "}.get(key.split(".")[0], ""), key.split(".", 1)[1].replace(".", "/"))

    for key, node in seg:
        k = sum(1 for d in docs if isinstance(value(d, key), (int, float)))
        if node.compare:
            compare.append((key, node, k))   # a comparison row is not a segment; listed apart below
            continue
        sourced = any(m.src for m in node.walk())
        name = name_of(key)
        able = [d for d in docs if can_have(d, key, node)]
        skip = len(docs) - len(able)
        k = sum(1 for d in able if isinstance(value(d, key), (int, float)))
        notes = ([ONLY_SOME[key]] if key in ONLY_SOME else []) + (
            ["未采 %d：这些请求那一侧的粒度低于 %d 级" % (skip, node.from_level)] if skip and able else [])
        if able or not sourced:
            print("  %-48s %4d / %d%s" % (name, k, len(able), ("（%s）" % "；".join(notes)) if notes else ""))
        else:
            print("  %-48s 未采（粒度 %d 级；这一段从 %d 级起记）" % (name, max(side_level(d, key) for d in docs), node.from_level))
        if not sourced:
            nosrc.append(name)
        elif not able:
            by_level.append(name)
        elif k:
            have.append(name)
        elif getattr(node, "phase", "") == "proxy":
            proxy_empty.append(name)      # the proxy is optional: without its records these are empty by design
        else:
            empty.append(name)
    if compare:
        print("对照行（不是段，不计入合计）：")
        for key, node, k in compare:
            print("  %-48s %4d / %d（%s）" % (name_of(key), k, len(docs), node.label))
            if not k:
                empty.append(name_of(key))
    total = len(seg) - len(compare)
    print("%d 段里 %d 段有值；没有来源、按设计为空的 %d 段：%s" % (total, len(have), len(nosrc), "、".join(nosrc) or "无"))
    if proxy_empty:
        print("proxy 段为空的 %d 段（proxy 可选：proxy 未打补丁、未运行或仍是旧补丁时没有 proxy 的记录，不算失败）：%s" % (
            len(proxy_empty), "、".join(proxy_empty)))
    if by_level:
        print("按粒度不记的 %d 段（这些请求的粒度低于这几段的起记级别，不算失败）：%s" % (len(by_level), "、".join(by_level)))
    if empty:
        print("RESULT: FAIL（有来源但这些请求里都为空的 %d 段：%s）" % (len(empty), "、".join(empty)))
        return 1
    print("RESULT: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
