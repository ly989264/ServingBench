#!/usr/bin/env python3
"""Generates the ClickHouse schema, the per-request JSON view and the dashboard from segments.json (version 2).

Output goes to generated/. Nothing here talks to a server. bin/apply_segments.sh runs this and applies the result.

Where the numbers live (the collector, Vector, writes both tables; see scripts/render_vector.py):
  vllm_pd.phase_line   sparse rows, one per record a segment reads and one per part of the filter's per-request sums.
                       A request has several rows per side; the queries below put them together. Every row written for
                       this segments.json carries its config_id; the queries read vllm_pd.phase_line_cur, the rows of
                       this config_id only, so rows of an earlier log format never show under this segment tree.
  vllm_pd.diag_event   the request-level events as they were logged, for looking at one request event by event.
Without the proxy patch there is no proxy line: P and D are joined by the request key X (chatcmpl-<X>), and the
official E2E / TTFT of each side are shown side by side, never added up.
"""
import copy
import json
import math
import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from segtree import PACK, Tree, range_name  # noqa: E402

TABLE = "vllm_pd.phase_line"
LINE = "vllm_pd.phase_line_cur"
PROXY = "vllm_pd.proxy_line"     # v3.1-extra: one row per request from the proxy's line, read when a proxy segment has a src
EVENT = "vllm_pd.diag_event"
SEL = "${instance:singlequote}"
REQ_FILTER = f"s.model = '$model' AND (s.p_instance IN ({SEL}) OR s.d_instance IN ({SEL}))"
WIDE_WINDOW = "ts >= $__fromTime - INTERVAL 5 MINUTE AND ts < $__toTime + INTERVAL 1 HOUR"
# A request belongs to the window by when it arrived: P's API start, or D's when there is no P row. A request that
# arrived before the window but ended inside it stays out, even when its P rows are older than the rows scanned.
IN_WINDOW = "coalesce(p_ts, d_ts, t_min) >= $__fromTime AND coalesce(p_ts, d_ts, t_min) <= $__toTime"
WINDOW_L = "$__timeFilter(ts)"
# Distributions over more than SAMPLE_TARGET requests use every n-th request (by hash of the request key), n chosen
# from a cheap approximate count, so a query's memory has an upper bound whatever the window and the request rate.
SAMPLE_TARGET = 200000
SAMPLE_N = f"(SELECT greatest(1, toUInt64(ceil(uniq(request_id) / {SAMPLE_TARGET}))) FROM {LINE} WHERE {WINDOW_L} AND model = '$model')"
SAMPLED = "cityHash64(request_id) % sample_n = 0"
GEN = PACK / "generated"
EVENT_TTL_DAYS = int(os.environ.get("EVENT_TTL_DAYS") or 15)
SUMMARY_TTL_DAYS = int(os.environ.get("SUMMARY_TTL_DAYS") or 30)

# Fields of segments.json "request" and their ClickHouse types.
REQ_TYPES = {"_ms": "Nullable(Float32)", "_tokens": "Nullable(UInt32)"}
STRING_FIELDS = ("status", "finish_reason")
# The processes every request goes through on each side; the single-request JSON lists a side that lacks one of them.
# The background save task (proc "save") is not one: a request has it only when the cache pool saved for it.
REQUEST_PROCS = ("api", "engine", "worker")


def req_numeric(tree):
    return [k for k in tree.request_fields if k not in STRING_FIELDS and k != "http_status"]


def req_type(name):
    if name == "executions":
        return "Nullable(UInt32)"
    for suffix, t in REQ_TYPES.items():
        if name.endswith(suffix):
            return t
    return "Nullable(Float32)"


def nz(col, alias=None):
    return "ifNull(%s%s, 0)" % (alias + "." if alias else "", col)


def value_expr(node, alias=None):
    """The value of a segment as SQL: the value written directly, else the sum of the children, else NULL."""
    prefix = alias + "." if alias else ""
    own = prefix + node.column
    if not node.children:
        return own
    kids = [value_expr(c, alias) for c in node.children]
    total = "+".join("ifNull(%s, 0)" % k for k in kids)
    all_null = " AND ".join("isNull(%s)" % k for k in kids)
    return "coalesce(%s, if(%s, NULL, %s))" % (own, all_null, total)


def filled_expr(node, alias=None):
    """Same as value_expr, with empty shown as 0. Only for stacked bars and time positions."""
    return "ifNull(%s, 0)" % value_expr(node, alias)


def col(alias, name):
    return (alias + "." if alias else "") + name


def side_total(tree, role, alias="s"):
    """P 合计 / D 合计: each side's API server, from receiving the request to the last byte (api_summary[http] end_ns - start_ns)."""
    own = col(alias, "p_total_ms" if role == "prefill" else "d_total_ms")
    # Segments measured by the proxy (v3.1-extra, a src on the proxy line) stay out of the total: P 合计 and D 合计 are the
    # API server's own (decision C5). Those without a src are always empty, as in v3.
    sourced = {c for c, _ in tree.proxy_sourced()}
    head = "+".join(f"ifNull({col(alias, n.column)}, 0)" for n in tree.sides[role]["proxy"] if n.column not in sourced) or "0"
    return f"if({own} > 0, {head} + {own}, NULL)"


def alias_of(node, alias):
    """Segments measured on the proxy are read from the request row (empty until the proxy patch)."""
    return "s" if node.phase == "proxy" else alias


# ---- one row per request: the anchor every panel starts from --------------------------------------------------------

# The proxy line's fields the per-request JSON reads (v3.1-extra), with the names the anchor gives them.
PROXY_JSON = (("px_client_request_id", "anyIf(client_request_id, client_request_id != '')"), ("px_retries", "max(retries)"),
              ("px_ttft_ms", "max(ttft_ms)"), ("px_e2e_ms", "max(e2e_ms)"), ("px_retry_ms", "max(retry_ms)"))


def anchor(tree, where_rows, having="1"):
    """One row per request key X, from the API rows of both sides. Columns of the proxy stay empty, except those of
    segments that read the proxy's line (v3.1-extra): they come from vllm_pd.proxy_line, joined by the request key."""
    sourced = tree.proxy_sourced()
    proxy = []
    for role in ("prefill", "decode"):
        for n in tree.sides[role]["proxy"]:
            if n.column not in proxy and n.column not in {c for c, _ in sourced}:
                proxy.append(n.column)
    nulls = "".join(f", CAST(NULL AS Nullable(Float32)) AS {c}" for c in proxy)
    inner = api_anchor(nulls, where_rows, having)
    if not sourced:
        return inner
    cols = ", ".join(f"px.{c} AS {c}" for c, _ in sourced)
    aggs = ", ".join(f"max({f}) AS {c}" for c, f in sourced)
    extra = ", ".join(f"{e} AS {n}" for n, e in PROXY_JSON)
    return (
        f"(SELECT a.*, {cols}, px.px_found AS px_found, {', '.join(f'px.{n} AS {n}' for n, _ in PROXY_JSON)} FROM {inner} AS a\n"
        f"  LEFT JOIN (SELECT request_id, {aggs}, toUInt8(1) AS px_found, {extra} FROM {PROXY} WHERE {where_rows} GROUP BY request_id) AS px"
        " ON px.request_id = a.request_id)"
    )


def api_anchor(nulls, where_rows, having):
    return (
        "(SELECT *, t_min AS ts FROM (SELECT request_id, min(ts) AS t_min, any(model) AS model,\n"
        "    anyIf(instance, role = 'prefill') AS p_instance, anyIf(instance, role = 'decode') AS d_instance,\n"
        "    coalesce(minIf(api_start_ts, role = 'prefill'), minIf(ts, role = 'prefill')) AS p_ts,\n"
        "    coalesce(minIf(api_start_ts, role = 'decode'), minIf(ts, role = 'decode')) AS d_ts,\n"
        "    maxIf(api_end_ts, role = 'prefill') AS p_end_ts, maxIf(api_end_ts, role = 'decode') AS d_end_ts,\n"
        "    countIf(role = 'prefill') > 0 AS has_p, countIf(role = 'decode') > 0 AS has_d,\n"
        "    maxIf(official_e2e_ms, role = 'prefill') AS p_e2e_ms, maxIf(official_e2e_ms, role = 'decode') AS d_e2e_ms,\n"
        "    maxIf(official_ttft_ms, role = 'prefill') AS p_ttft_ms, maxIf(official_ttft_ms, role = 'decode') AS d_ttft_ms,\n"
        "    maxIf(official_tpot_ms, role = 'decode') AS tpot_ms,\n"
        "    maxIf(api_total_ms, role = 'prefill') AS p_total_ms, maxIf(api_total_ms, role = 'decode') AS d_total_ms,\n"
        "    maxIf(official_ttft_ms, role = 'decode') AS d_first_ms,\n"
        "    coalesce(maxIf(prompt_tokens, role = 'prefill'), maxIf(prompt_tokens, role = 'decode')) AS prompt_tokens, maxIf(cached_tokens, role = 'prefill') AS cached_tokens,\n"
        "    maxIf(output_tokens, role = 'decode') AS output_tokens,\n"
        "    if(anyIf(status, role = 'decode' AND status != '') != '', anyIf(status, role = 'decode' AND status != ''), anyIf(status, status != '')) AS status,\n"
        "    if(anyIf(finish_reason, role = 'decode' AND finish_reason != '') != '', anyIf(finish_reason, role = 'decode' AND finish_reason != ''), anyIf(finish_reason, finish_reason != '')) AS finish_reason,\n"
        "    coalesce(maxIf(http_status, role = 'decode'), maxIf(http_status, role = 'prefill')) AS http_status,\n"
        "    uniqExactIf(attempt_id, role = 'prefill') AS p_attempts, uniqExactIf(attempt_id, role = 'decode') AS d_attempts"
        f"{nulls}\n"
        f"  FROM {LINE} WHERE proc = 'api' AND part = '' AND {where_rows} GROUP BY request_id HAVING {having}))"
    )


def win_anchor(tree):
    return anchor(tree, WIDE_WINDOW, IN_WINDOW)


def one_anchor(tree, rid="'${request_id}'"):
    return anchor(tree, f"request_id = {rid}")


# ---- one row per request and side: the segments ---------------------------------------------------------------------

class Row:
    def __init__(self, label, source, expr, procs, opt=False, share=True, rest=False, columns=()):
        self.label, self.source, self.expr, self.procs = label, source, expr, procs
        self.opt, self.share, self.rest, self.columns = opt, share, rest, columns


def tree_label(node):
    """Indented label for table rows: L1 plain, L2 '├ x', L3 '│ ├ y', and so on."""
    if node.depth == 1:
        return node.label
    last = node.parent.children[-1] is node
    head = "└ " if last else "├ "
    pre = ""
    p = node.parent
    while p.parent is not None:
        # Non-breaking spaces: a table cell would swallow ordinary ones and the level would look like its parent's.
        pre = ("   " if p.parent.children[-1] is p else "│ ") + pre
        p = p.parent
    return pre + head + node.label


def rows_of(tree, role):
    side = tree.sides[role]
    rows = []

    def add(node):
        procs = node.procs()
        source = "summary" if node.phase == "proxy" else "fold"
        # A proxy segment with a src is measured outside the API server's total (C5): no share of it.
        share = node.phase != "background" and not (node.phase == "proxy" and node.src)
        rows.append(Row(tree_label(node), source, value_expr(node), procs, node.optional, share, node.phase == "tpot", node.columns_below()))
        for c in node.children:
            add(c)

    for ph in ("proxy", "ttft", "tpot"):
        for top in side[ph]:
            add(top)
    rows.append(Row(side["total_label"], "summary", side_total(tree, role, None), {"proxy"}))
    for top in side["background"]:
        add(top)
    return rows


def first_cols(tree, role):
    out = []
    for n in tree.nodes(role, "ttft", "background"):
        if n.column not in out:
            out.append(n.column)
    return out + req_numeric(tree)


def rest_cols(tree, role):
    return [n.column for n in tree.nodes(role, "tpot")]


def node_value(tree, node):
    """The SQL of one segment on one request and attempt, from the part-level aggregates."""
    srcs = node.src or []
    if not srcs:
        return "CAST(NULL AS Nullable(Float32))"
    parts = []
    if any("event" in s and "ranges" not in s for s in srcs):
        # Vector adds every field source of a segment into the one column (req_resolve and render both into req_resolve).
        parts.append(f"e_{node.column}")
    for s in srcs:
        if "ranges" in s:
            name = range_name(s)
            at = f"indexOf(rx_{name}, fe)"
            if s["part"] == "first":
                parts.append(f"if({at} > 0, toFloat64(rn_{name}[{at}]) / 1e6, NULL)")
            else:
                parts.append(f"if({at} > 0 AND isNotNull(rt_{name}), toFloat64(rt_{name} - rn_{name}[{at}]) / 1e6, NULL)")
            continue
        if "event" in s:
            continue
        else:
            a, p = s["acc"], s.get("part", "total")
            if p == "total":
                parts.append(f"b_{a}_ns / 1e6")
            elif p == "first":
                parts.append(f"b_{a}_first / 1e6")
            else:
                parts.append(f"(b_{a}_ns - b_{a}_first) / 1e6")
    if len(parts) == 1:
        return f"toNullable(toFloat32({parts[0]}))"
    nulls = " AND ".join(f"isNull({x})" for x in parts)
    return f"if({nulls}, NULL, toNullable(toFloat32({'+'.join(f'ifNull({x}, 0)' for x in parts)})))"


def fold(tree, role, where):
    """One row per request on one side.
    Level 1, per part: the rows of one event group or of one part of the filter's sums (cumulative: the largest is the latest).
    Level 2, per attempt: parts of the sums add up; the first step belongs to the earliest part.
    Level 3, per request: segments before the first token come from the first attempt; later steps add up."""
    nodes = tree.nodes(role, "ttft", "tpot", "background")
    ev = []
    for n in nodes:
        if any("event" in s and "ranges" not in s for s in n.src or []) and n.column not in ev:
            ev.append(n.column)
    reqs = req_numeric(tree)
    accs = tree.acc_ids
    latest = set(tree.latest_columns())

    def pick(c):
        # A column of core_summary[request]: the record written last (largest timestamp_ns), never a sum or a maximum.
        if c in latest:
            return f"argMaxIf({c}, core_mono_ns, isNotNull(core_mono_ns) AND isNotNull({c}))"
        return f"max({c})"
    l1 = ["request_id", "attempt_id", "part", "min(ts) AS t0", "groupUniqArray(toString(proc)) AS procs", "min(first_mono_ns) AS fm",
          "argMinIf(first_exec, first_mono_ns, first_exec != '') AS l_fe"]
    l1 += [f"{pick(c)} AS max_{c}" for c in ev] + [f"{pick(c)} AS {c}" for c in reqs]
    for a in accs:
        l1 += [f"max(acc__{a}__ns) AS a_{a}_ns", f"max(acc__{a}__steps) AS a_{a}_steps", f"max(acc__{a}__first_ns) AS a_{a}_first"]
    for name in tree.ranges():
        l1 += [f"anyIf(rg__{name}__exec, notEmpty(rg__{name}__exec)) AS l_rx_{name}",
               f"anyIf(rg__{name}__ns, notEmpty(rg__{name}__ns)) AS l_rn_{name}", f"max(rg__{name}__total_ns) AS l_rt_{name}"]
    # lines: how many of the three request processes wrote a line; the background save task (proc "save", from pool_task)
    # is a fourth kind and is not one of them.
    l2 = ["request_id", "attempt_id", "min(t0) AS t0",
          "length(arrayFilter(x -> has([%s], x), arrayDistinct(arrayFlatten(groupArray(procs))))) AS lines" % (
              ", ".join("'%s'" % p for p in REQUEST_PROCS)),
          # The request's first step on this side: from the earliest part of the filter's sums.
          "argMinIf(l_fe, fm, l_fe != '') AS fe"]
    l2 += [f"max(max_{c}) AS e_{c}" for c in ev] + [f"max({c}) AS {c}" for c in reqs]
    for a in accs:
        l2 += [f"sumOrNull(a_{a}_ns) AS b_{a}_ns", f"sumOrNull(a_{a}_steps) AS b_{a}_steps",
               f"argMinIf(a_{a}_first, fm, isNotNull(a_{a}_ns)) AS b_{a}_first"]
    for name in tree.ranges():
        l2 += [f"anyIf(l_rx_{name}, notEmpty(l_rx_{name})) AS rx_{name}", f"anyIf(l_rn_{name}, notEmpty(l_rn_{name})) AS rn_{name}",
               f"max(l_rt_{name}) AS rt_{name}"]
    seen = set()
    l3 = []
    for n in nodes:
        if n.column in seen:
            continue
        seen.add(n.column)
        l3.append(f"{node_value(tree, n)} AS {n.column}")
    steps = "toUInt32(greatest(ifNull(b_forward_steps, 0), 1) - 1)" if "forward" in accs and role == "decode" else "toUInt32(0)"
    l3_sel = ["request_id", "attempt_id", "t0", "lines"] + l3 + reqs + [f"{steps} AS steps_rest"]
    first, rest = first_cols(tree, role), rest_cols(tree, role)
    outer = [f"argMin({c}, t0) AS {c}" for c in first if c not in rest] + [f"sum({c}) AS {c}" for c in rest] + ["sum(steps_rest) AS steps_rest"]
    return (
        f"SELECT request_id, {', '.join(outer)}, uniqExact(attempt_id) AS attempts, argMin(lines, t0) AS lines FROM ("
        f"SELECT {', '.join(l3_sel)} FROM ("
        f"SELECT {', '.join(l2)} FROM ("
        f"SELECT {', '.join(l1)} FROM {LINE} WHERE role = '{role}' AND {where} GROUP BY request_id, attempt_id, part"
        f") GROUP BY request_id, attempt_id)) GROUP BY request_id"
    )


def inst_filter(side):
    """Picking only P instances leaves the D rows unfiltered, and the other way round."""
    return f"(instance IN ({SEL}) OR NOT arrayExists(x -> startsWith(x, '{side}'), [{SEL}]))"


ORDERS = {"d_e2e_ms": "s.d_e2e_ms", "p_ttft_ms": "s.p_ttft_ms", "tpot_ms": "s.tpot_ms"}


def top_cte(tree, n=20):
    """The n slowest requests by the selected column."""
    ord_expr = ("multiIf(" + ", ".join(f"'${{order_by}}' = '{k}', ifNull({v}, 0)" for k, v in ORDERS.items())
                + f", '${{order_by}}' = 'p_total_ms', ifNull({side_total(tree, 'prefill')}, 0), '${{order_by}}' = 'd_total_ms', ifNull({side_total(tree, 'decode')}, 0), "
                + "ifNull(s.d_e2e_ms, 0))")
    return (
        "t AS (\n"
        f"  SELECT s.request_id AS request_id, toFloat64({ord_expr}) AS ord FROM {win_anchor(tree)} AS s\n"
        f"  WHERE {REQ_FILTER}\n"
        f"  ORDER BY ord DESC LIMIT {n})"
    )


def top_summary(tree):
    return f"{anchor(tree, WIDE_WINDOW + ' AND request_id IN (SELECT request_id FROM t)')} AS s"


def fmt_ms(x):
    return (
        f"multiIf({x} >= 60000, concat(toString(round({x} / 60000, 2)), ' min'), "
        f"{x} >= 1000, concat(toString(round({x} / 1000, 2)), ' s'), concat(toString(round({x}, 1)), ' ms'))"
    )


STEP_WORDS = ("，第一步", "，第二步起", "（第一步）", "（第二步起）")


def bar_series(tree, role):
    """(series name, top-level node) for the stacked bars and the largest-stage column: the segment's full label, the same
    name as in the distribution tables, so host-side timing and first or later steps read the same everywhere."""
    side = tree.sides[role]
    return [(n.label, n) for n in side["ttft"]] + [(n.label, n) for n in side["tpot"]]


def top_stage(tree, role, a):
    """The largest segment of one side and its share of that side's total (name only while the total is not known)."""
    series = bar_series(tree, role)
    arr = "[" + ", ".join(f"toFloat64({filled_expr(n, alias_of(n, a))})" for _, n in series) + "]"
    nm = "[" + ", ".join(f"'{name}'" for name, _ in series) + "]"
    best = f"arrayMax({arr})"
    inside = "greatest(" + ", ".join(f"toFloat64({filled_expr(n, a)})" for _, n in series) + ")"
    total = side_total(tree, role)
    share = f"if(ifNull({total}, 0) > 0, concat(' ', toString(round({best} / {total} * 100)), '%'), '')"
    return f"if({inside} > 0, concat({nm}[indexOf({arr}, {best})], {share}), '—')"


def no_nan(x):
    """A segment nobody recorded gives NaN from the aggregates. Empty is what it should show."""
    return f"if(isNaN(ifNull({x}, nan)), NULL, {x})"


def proxy_cols(tree, role):
    return ", ".join(f"s.{n.column} AS {n.column}" for n in tree.sides[role]["proxy"])


def dist_parts(tree, rows, role, s_cte="s"):
    """Quantiles per segment over the requests of the window (a sample of them for windows longer than a day).
    Only requests this side has finished (its total is known): a request still running has only part of its segments."""
    idx = "[" + ", ".join(str(i) for i in range(1, len(rows) + 1)) + "]"
    vals = "[" + ", ".join(f"toFloat64({r.expr})" for r in rows) + "]"
    opt = "[" + ", ".join("1" if r.opt else "0" for r in rows) + "]"
    rest = "[" + ", ".join("1" if r.rest else "0" for r in rows) + "]"
    total = "p_total_ms" if role == "prefill" else "d_total_ms"
    steps = "toUInt32(0)" if role == "prefill" else "steps_rest"
    per = f"if(r = 1 AND {steps} > 0, ms / {steps}, ms)"
    pc = proxy_cols(tree, role)
    f = fold(tree, role, f"{WIDE_WINDOW} AND {SAMPLED} AND request_id IN (SELECT request_id FROM {s_cte})")
    # Only the columns used here come along from the window's requests (s is sampled already, see s_window_cte).
    keep = ", ".join(["request_id", total] + [n.column for n in tree.sides[role]["proxy"]])
    return (
        f"SELECT toUInt8(idx) AS idx, count() AS n, quantilesExact(0.5, 0.9, 0.99)({per}) AS q, quantileExactIf(0.5)({per}, o = 0 OR ms > 0) AS p50, avg(ms) AS accum, avgIf({per}, r = 0 OR {steps} > 0) AS mean\n"
        f"  FROM (SELECT f.*, s.{total} AS {total}{', ' + pc if pc else ''} FROM ({f}) AS f INNER JOIN (SELECT {keep} FROM {s_cte}) AS s ON s.request_id = f.request_id)\n"
        f"  ARRAY JOIN {idx} AS idx, {vals} AS ms, {opt} AS o, {rest} AS r WHERE isNotNull(ms) AND (r = 0 OR {steps} > 0) AND ifNull({total}, 0) > 0 GROUP BY idx"
    )


def s_window_cte(tree):
    """The window's requests for the distributions: every sample_n-th one (all of them up to SAMPLE_TARGET)."""
    sampled = anchor(tree, f"{WIDE_WINDOW} AND {SAMPLED}", IN_WINDOW)
    return f"{SAMPLE_N} AS sample_n,\ns AS (SELECT * FROM {sampled} AS s WHERE {REQ_FILTER})"


def steps_sql(tree, role, label):
    """How many steps (executions) a finished request of the window took part in on one side: core_summary[request]'s
    members[].executions for the request (the record written last when there are several), added up over attempts.
    P: chunked-prefill chunks, steps redone after preemption included; D: the first step and the later ones."""
    total = "p_total_ms" if role == "prefill" else "d_total_ms"
    return (
        f"WITH {s_window_cte(tree)}\n"
        f"SELECT '{label}' AS \"项目\", if(c = 0, NULL, q[1]) AS p50, if(c = 0, NULL, q[2]) AS p90, if(c = 0, NULL, q[3]) AS p99,\n"
        "       if(c = 0, NULL, m) AS \"均值\", if(c = 0, NULL, mx) AS \"最大\"\n"
        "FROM (SELECT count() AS c, quantilesExact(0.5, 0.9, 0.99)(n) AS q, avg(n) AS m, max(n) AS mx FROM (\n"
        "  SELECT request_id, sum(steps) AS n FROM (\n"
        f"    SELECT request_id, attempt_id, argMaxIf(executions, core_mono_ns, isNotNull(core_mono_ns) AND isNotNull(executions)) AS steps FROM {LINE}\n"
        f"    WHERE {WIDE_WINDOW} AND {SAMPLED} AND role = '{role}' AND request_id IN (SELECT request_id FROM s WHERE ifNull({total}, 0) > 0)\n"
        "    GROUP BY request_id, attempt_id)\n"
        "  GROUP BY request_id HAVING n > 0))"
    )


def dist_label(r):
    """A distribution table shows a later-step row per step (quantiles and mean of ms / steps), so its name says so."""
    if not r.rest:
        return r.label
    return r.label[:-1] + "，每步）" if r.label.endswith("）") else r.label + "（每步）"


def fit_dist_names(panel, names, cols=("p50", "p90", "p99", "均值")):
    """The name column of a distribution table as wide as its longest name; the quantile and mean columns give up the
    same width together, so the table stays as wide as before."""
    def width(name):
        return next(p["value"] for p in by_name(panel, name)["properties"] if p["id"] == "custom.width")
    have, need = width("阶段"), max(header_width(n) for n in names)
    if need <= have:
        return
    cut = -(-(need - have) // len(cols))
    for c in cols:
        set_props(panel, c, custom__width=width(c) - cut)
    set_props(panel, "阶段", custom__width=need)


def dist_sql(tree, role):
    rows = rows_of(tree, role)
    labels = "[" + ", ".join("'%s'" % dist_label(r) for r in rows) + "]"
    shares = "[" + ", ".join("1" if r.share else "0" for r in rows) + "]"
    return (
        f"WITH {s_window_cte(tree)},\n"
        f"(SELECT avg({side_total(tree, role)}) FROM s) AS total_avg\n"
        f"SELECT {labels}[idx] AS \"阶段\", if(d.n = 0, NULL, q[1]) AS p50, if(d.n = 0, NULL, q[2]) AS p90, if(d.n = 0, NULL, q[3]) AS p99,\n"
        f"       if(d.n = 0, NULL, {no_nan('mean')}) AS \"均值\", if({shares}[idx] = 1 AND d.n > 0, {no_nan('accum / total_avg')}, NULL) AS \"均值占比\"\n"
        f"FROM (SELECT toUInt8(number + 1) AS idx FROM numbers({len(rows)})) AS i LEFT JOIN (\n  {dist_parts(tree, rows, role)}\n) AS d USING idx\nORDER BY idx"
    )


def request_rows(tree, rows, role):
    """One request's segments as rows; the request row gives the side total and the (empty) proxy segments."""
    total = "p_total_ms" if role == "prefill" else "d_total_ms"
    idx = "[" + ", ".join(str(i) for i in range(1, len(rows) + 1)) + "]"
    vals = ", ".join(f"toFloat64({r.expr})" for r in rows)
    steps = "toUInt32(0)" if role == "prefill" else "f.steps_rest"
    where = "request_id = '${request_id}'"
    pc = proxy_cols(tree, role)
    return (
        f"SELECT idx, ms, total_ms, steps FROM (SELECT f.*, s.{total} AS {total}{', ' + pc if pc else ''}, toFloat64({side_total(tree, role)}) AS total_ms, {steps} AS steps\n"
        f"    FROM {one_anchor(tree)} AS s\n"
        f"    LEFT JOIN ({fold(tree, role, where)}) AS f ON s.request_id = f.request_id)\n"
        f"  ARRAY JOIN {idx} AS idx, [{vals}] AS ms"
    )


def detail_p(tree):
    rows = rows_of(tree, "prefill")
    labels = "[" + ", ".join("'%s'" % r.label for r in rows) + "]"
    shares = "[" + ", ".join("1" if r.share else "0" for r in rows) + "]"
    opts = "[" + ", ".join("1" if r.opt else "0" for r in rows) + "]"
    return (
        f"WITH {s_window_cte(tree)}\n"
        f"SELECT {labels}[r.idx] AS \"阶段\", r.ms AS \"耗时\", if({shares}[r.idx] = 1, r.ms / r.total_ms, NULL) AS \"占P合计\", {no_nan('w.p50')} AS \"窗口 p50\",\n"
        f"       if({opts}[r.idx] = 1 AND r.ms = 0, NULL, {no_nan('r.ms / nullIf(w.p50, 0)')}) AS \"相对 p50\"\n"
        f"FROM (\n  {request_rows(tree, rows, 'prefill')}\n) AS r\nLEFT JOIN (\n  {dist_parts(tree, rows, 'prefill')}\n) AS w ON w.idx = r.idx\nORDER BY r.idx"
    )


def detail_d(tree):
    rows = rows_of(tree, "decode")
    labels = "[" + ", ".join("'%s'" % r.label for r in rows) + "]"
    rests = "[" + ", ".join("1" if r.rest else "0" for r in rows) + "]"
    shares = "[" + ", ".join("1" if r.share else "0" for r in rows) + "]"
    return (
        f"WITH {s_window_cte(tree)}\n"
        f"SELECT {labels}[r.idx] AS \"阶段\", r.ms AS \"耗时\", if({shares}[r.idx] = 1, r.ms / r.total_ms, NULL) AS \"占D合计\",\n"
        f"       if({rests}[r.idx] = 1 AND r.steps > 0 AND isNotNull(r.ms), r.steps, NULL) AS \"步数\",\n"
        f"       if({rests}[r.idx] = 1 AND r.steps > 0, r.ms / r.steps, NULL) AS \"每步均值\",\n"
        f"       if({rests}[r.idx] = 0, {no_nan('w.p50')}, NULL) AS \"窗口 p50\",\n"
        f"       if({rests}[r.idx] = 1, {no_nan('w.p50')}, NULL) AS \"窗口每步 p50\",\n"
        f"       {no_nan(f'if({rests}[r.idx] = 1, if(r.steps > 0, (r.ms / r.steps) / nullIf(w.p50, 0), NULL), r.ms / nullIf(w.p50, 0))')} AS \"相对 p50\"\n"
        f"FROM (\n  {request_rows(tree, rows, 'decode')}\n) AS r\nLEFT JOIN (\n  {dist_parts(tree, rows, 'decode')}\n) AS w ON w.idx = r.idx\nORDER BY r.idx"
    )


def label_short(req, rn="rn"):
    return f"concat('#', toString({rn}), ' ', substring({req}, 1, 8), '…', right({req}, 4))"


def bars(tree, role):
    cols = ",\n".join(f"       {filled_expr(n, alias_of(n, 'ph'))} AS \"{name}\"" for name, n in bar_series(tree, role))
    return (
        f"WITH {top_cte(tree)}\n"
        f"SELECT {label_short('t.request_id')} AS req, t.request_id AS request_id,\n{cols}\n"
        "FROM (SELECT request_id, ord, row_number() OVER (ORDER BY ord DESC) AS rn FROM t) AS t\n"
        f"LEFT JOIN {top_summary(tree)} ON s.request_id = t.request_id\n"
        f"LEFT JOIN ({fold(tree, role, WIDE_WINDOW + ' AND request_id IN (SELECT request_id FROM t)')}) AS ph ON ph.request_id = t.request_id\n"
        "ORDER BY t.ord DESC"
    )


TOP_TABLE_N = 100  # the big table of slowest requests; the bar charts and the detail tables keep the 20 slowest


def top_table(tree):
    return (
        f"WITH {top_cte(tree, TOP_TABLE_N)}\n"
        "SELECT row_number() OVER (ORDER BY t.ord DESC) AS \"#\",\n"
        "       t.request_id AS request_id,\n"
        "       coalesce(s.p_ts, s.d_ts, s.ts) AS \"到达时间\",\n"
        "       concat(if(s.p_instance = '', '—', s.p_instance), ' → ', if(s.d_instance = '', '—', s.d_instance)) AS \"路由 P → D\",\n"
        "       s.prompt_tokens AS \"输入 tokens\", s.cached_tokens / nullIf(s.prompt_tokens, 0) AS \"命中率\", s.output_tokens AS \"输出 tokens\",\n"
        "       s.p_ttft_ms AS \"官方 TTFT P\", s.d_ttft_ms AS \"官方 TTFT D\", s.d_e2e_ms AS \"官方 E2E D\", nullIf(s.tpot_ms, 0) AS TPOT,\n"
        f"       {side_total(tree, 'prefill')} AS \"P 合计\", coalesce(d.start_load_kv_ms, d.start_load_kv__wait_for_transfer_ms) AS \"D 等 KV\", {side_total(tree, 'decode')} AS \"D 合计\",\n"
        f"       {top_stage(tree, 'prefill', 'p')} AS \"P 最大段\",\n"
        f"       {top_stage(tree, 'decode', 'd')} AS \"D 最大段\"\n"
        "FROM t\n"
        f"LEFT JOIN {top_summary(tree)} ON s.request_id = t.request_id\n"
        f"LEFT JOIN ({fold(tree, 'prefill', WIDE_WINDOW + ' AND request_id IN (SELECT request_id FROM t)')}) AS p ON p.request_id = t.request_id\n"
        f"LEFT JOIN ({fold(tree, 'decode', WIDE_WINDOW + ' AND request_id IN (SELECT request_id FROM t)')}) AS d ON d.request_id = t.request_id\n"
        "ORDER BY t.ord DESC"
    )


def column_name(node):
    """Column header in the Top-20 detail tables: the label without the step words, which the table title carries
    (forward（host 侧）, detokenizer（全部步）, schedule.alloc as alloc, put.submit)."""
    if node.phase == "proxy" or node.depth == 1:
        name = node.label
        for w in STEP_WORDS:
            name = name.replace(w, "")
        return name
    return ".".join(node.path[1:])


def detail_columns(nodes):
    """Every segment with a column of its own, in tree order; a sum-only parent is shown through its children.
    A segment the log has nothing for (no source in it or below it) is empty in every row and gets no column."""
    return [(column_name(n), value_expr(n, alias_of(n, "ph"))) for n in nodes if any(m.src for m in n.walk())]


def top_detail(tree, total_label, role, columns):
    cols = ",\n".join(f"       {expr} AS \"{label}\"" for label, expr in columns)
    return (
        f"WITH {top_cte(tree)}\n"
        "SELECT row_number() OVER (ORDER BY t.ord DESC) AS \"#\",\n"
        f"       t.request_id AS request_id, {side_total(tree, role)} AS \"{total_label}\",\n{cols}\n"
        "FROM t\n"
        f"LEFT JOIN {top_summary(tree)} ON s.request_id = t.request_id\n"
        f"LEFT JOIN ({fold(tree, role, WIDE_WINDOW + ' AND request_id IN (SELECT request_id FROM t)')}) AS ph ON ph.request_id = t.request_id\n"
        "ORDER BY t.ord DESC"
    )


def timeline(tree):
    """One row per span, nested as the tree is. Each side is placed at its own wall-clock start (two machines, two
    clocks, synchronized only as well as NTP keeps them); inside a side, children follow each other from the parent's start.
    Nothing is clamped: with skewed clocks the gap between P's end and D's start can be negative."""
    empty = "CAST([], 'Array(Map(String, String))')"

    def span(name, parent, svc, base, off, dur, tags=empty):
        return f"tuple('{name}', '{parent}', '{svc}', toFloat64({base}) + toFloat64({off}), toFloat64({dur}), {tags})"

    spans = []
    pb, db = "toUnixTimestamp64Milli(s.p_ts)", "toUnixTimestamp64Milli(s.d_ts)"

    def lay(node, alias, svc, letter, parent_name, base, off, tag_steps=False):
        # First-level spans carry the segment's label, as every other panel does (forward（host 侧，第二步起））;
        # deeper ones their path, with the later-steps mark for D's steps after the first. Names are unique in a trace.
        name = f"{letter} {node.label}" if node.depth == 1 else f"{letter} {'.'.join(node.path)}" + ("（第二步起）" if node.phase == "tpot" else "")
        tags = empty
        if tag_steps:
            tags = (f"[map('key', 'steps', 'value', toString({alias}.steps_rest)), map('key', 'per_step_ms', 'value', "
                    f"toString(round({filled_expr(node, alias)} / greatest({alias}.steps_rest, 1), 3)))]")
        spans.append(span(name, parent_name, svc, base, off, filled_expr(node, alias), tags))
        child_off = off
        for c in node.children:
            lay(c, alias, svc, letter, name, base, child_off, tag_steps)
            child_off = f"{child_off}+{filled_expr(c, alias)}"

    start = "least(ifNull(%s, %s), ifNull(%s, %s))" % (pb, db, db, pb)
    end = "greatest(ifNull(%s + ifNull(s.p_total_ms, 0), 0), ifNull(%s + ifNull(s.d_total_ms, 0), 0))" % (pb, db)
    spans.append(span("request", "", "request", start, "0", f"{end} - {start}"))
    p, d = tree.sides["prefill"], tree.sides["decode"]
    spans.append(span("prefill", "request", "prefill", pb, "0", "s.p_total_ms"))
    off = "0"
    for top in p["ttft"]:
        lay(top, "p", "prefill", "P", "prefill", pb, off)
        off = f"{off}+{filled_expr(top, 'p')}"
    for top in p["background"]:
        if not top.parallel:
            lay(top, "p", "prefill", "P", "prefill", pb, off)

    def parallel(side, alias, svc, letter, base, title):
        # Spans that overlap the Worker segments: one parent where the Worker segments begin, every child from that same
        # point, so that they are seen side by side with what they overlap and never as a continuation of it.
        tops = side["ttft"]
        at = next((i for i, n in enumerate(tops) if n.id in ("input_prepare", "forward")), len(tops))
        w_off = "+".join(["0"] + [filled_expr(n, alias) for n in tops[:at]])
        kids = [n for n in side["background"] if n.parallel]
        if not kids:
            return
        name = f"{letter} {title}"
        spans.append(span(name, svc, svc, base, w_off, "greatest(" + ", ".join(["0"] + [filled_expr(n, alias) for n in kids]) + ")"))
        for n in kids:
            spans.append(span(f"{letter} {n.label}", name, svc, base, w_off, filled_expr(n, alias)))

    parallel(p, "p", "prefill", "P", pb, "并行（不计入合计）")
    spans.append(span("decode", "request", "decode", db, "0", "s.d_total_ms"))
    off = "0"
    for top in d["ttft"]:
        lay(top, "d", "decode", "D", "decode", db, off)
        off = f"{off}+{filled_expr(top, 'd')}"
    first = "ifNull(s.d_ttft_ms, 0)"
    spans.append(span("D 第一个 token", "decode", "decode", db, first, "0"))
    spans.append(span("D 第二步起", "decode", "decode", db, first, f"ifNull(s.d_total_ms, 0) - {first}", "[map('key', 'steps', 'value', toString(d.steps_rest))]"))
    for top in d["tpot"]:
        lay(top, "d", "decode", "D", "D 第二步起", db, first, tag_steps=True)
    parallel(d, "d", "decode", "D", db, "并行（不计入合计，所有步之和）")
    where = "request_id = '${request_id}'"
    return (
        "SELECT s.request_id AS traceID, toString(cityHash64(x.1)) AS spanID, if(x.2 = '', '', toString(cityHash64(x.2))) AS parentSpanID,\n"
        "       x.3 AS serviceName, x.1 AS operationName, x.4 AS startTime, x.5 AS duration, x.6 AS tags, CAST([], 'Array(Map(String, String))') AS serviceTags\n"
        f"FROM {one_anchor(tree)} AS s\n"
        f"LEFT JOIN ({fold(tree, 'prefill', where)}) AS p ON p.request_id = s.request_id\n"
        f"LEFT JOIN ({fold(tree, 'decode', where)}) AS d ON d.request_id = s.request_id\n"
        "ARRAY JOIN [" + ",\n".join(spans) + "] AS x\n"
        "WHERE (x.5 > 0 OR x.1 = 'D 第一个 token') AND isNotNull(x.4)\nORDER BY startTime, x.5 DESC"
    )


def events_sql():
    """One request event by event, both sides, from the event table (kept EVENT_TTL_DAYS)."""
    return (
        "SELECT ts AS \"时间\", if(side = 'prefill', 'P', 'D') AS \"侧\", instance AS \"实例\", role AS \"进程\", event AS \"事件\",\n"
        "       duration_ns / 1e6 AS \"耗时\", status AS \"状态\", execution_id AS execution_id\n"
        "FROM vllm_pd.request_events(id = '${request_id}')\nORDER BY ts, event"
    )


# ---- per-request JSON ------------------------------------------------------------------------------------------------

def json_node(node, alias, rest=False):
    """Every level has the same shape: {"level": n, "ms": x, "sub": {...}}. sub only exists when there are children.
    Segments of later steps also carry per_step_ms. A segment never written is null, not 0."""
    expr = f"toNullable(round(toFloat64({value_expr(node, alias)}), 3))"
    values = [str(node.level), expr]
    types = ["level UInt8", "ms Nullable(Float64)"]
    if rest:
        per = f"if({alias}.steps_rest > 0, toNullable(round(toFloat64({value_expr(node, alias)}) / {alias}.steps_rest, 3)), NULL)"
        values.append(per)
        types.append("per_step_ms Nullable(Float64)")
    if node.children:
        sub_v, sub_t = zip(*[json_node(c, alias, rest) for c in node.children])
        names = [c.id for c in node.children]
        values.append("tuple(" + ", ".join(sub_v) + ")")
        types.append("sub Tuple(" + ", ".join(f"{n} {t}" for n, t in zip(names, sub_t)) + ")")
    return "tuple(" + ", ".join(values) + ")", "Tuple(" + ", ".join(types) + ")"


def json_group(nodes, alias, rest=False):
    pairs = [json_node(n, alias_of(n, alias), rest) for n in nodes]
    return "tuple(" + ", ".join(p[0] for p in pairs) + ")", "Tuple(" + ", ".join(f"{n.id} {p[1]}" for n, p in zip(nodes, pairs)) + ")"


PROXY_PATHS = ["client_request_id", "route.retries", "latency_ms.e2e", "latency_ms.ttft", "latency_ms.retry_ms"]


def not_collected(tree):
    """JSON paths that are null in every document: proxy fields (only when no segment reads the proxy's line; otherwise
    they are listed per document, see views_sql), and segments the patch has no timer for."""
    out = ["session_id"] + ([] if tree.proxy_sourced() else PROXY_PATHS)
    groups = (("ttft.p", "prefill", ("proxy", "ttft")), ("ttft.d", "decode", ("proxy", "ttft")),
              ("tpot.d", "decode", ("tpot",)), ("background.p", "prefill", ("background",)), ("background.d", "decode", ("background",)))
    groups = [g for g in groups if any(tree.sides[g[1]][ph] for ph in g[2])]
    for prefix, role, phases in groups:
        for ph in phases:
            for top in tree.sides[role][ph]:
                for n in top.walk():
                    if n.children or n.src:
                        continue
                    p = prefix + "." + ".sub.".join(n.path)
                    out.append(p)
    return out


def views_sql(tree):
    idw = "request_id = {id:String}"
    p, d = tree.sides["prefill"], tree.sides["decode"]
    pv, pt = json_group(p["proxy"] + p["ttft"], "p")
    dv, dt = json_group(d["proxy"] + d["ttft"], "d")
    rv, rt = json_group(d["tpot"], "d", rest=True)
    bv, bt = json_group(p["background"], "p")
    bg = [("p", bv, bt)]
    if d["background"]:
        dbv, dbt = json_group(d["background"], "d")
        bg.append(("d", dbv, dbt))
    p_l1 = "+".join(filled_expr(x, "p") for x in p["ttft"])
    d_l1 = "+".join(filled_expr(x, "d") for x in d["ttft"])

    def recorded(role, alias):
        items = [f"if(isNotNull({alias}.{m.column}), {m.level}, 0)" for m in tree.nodes(role, "ttft", "tpot", "background")]
        return "toUInt8(greatest(" + ", ".join(items) + "))"

    n = "round(toFloat64(%s), 3)"
    happened = "nullIf(round(toFloat64(%s), 3), 0)"
    missing = "[" + ", ".join("'%s'" % m for m in not_collected(tree)) + "]"
    if tree.proxy_sourced():
        # v3.1-extra: the proxy's own numbers (C5: only here and in the proxy segments), null without a proxy line.
        px = {"cid": "if(s.px_found = 1, nullIf(toString(s.px_client_request_id), ''), NULL)",
              "retries": "if(s.px_found = 1, toNullable(toUInt8(least(ifNull(s.px_retries, 0), 255))), NULL)",
              "e2e": "if(s.px_found = 1, toNullable(round(toFloat64(s.px_e2e_ms), 3)), NULL)",
              "starts": "if(s.px_found = 1, 'proxy', 'instance')",
              "ttft": "if(s.px_found = 1, toNullable(round(toFloat64(s.px_ttft_ms), 3)), NULL)",
              "retry": "if(s.px_found = 1, toNullable(round(toFloat64(s.px_retry_ms), 3)), NULL)"}
        missing_px = "if(s.px_found = 1, [], [%s])" % ", ".join("'%s'" % m for m in PROXY_PATHS)
    else:
        px = {"cid": "CAST(NULL AS Nullable(String))", "retries": "CAST(NULL AS Nullable(UInt8))", "e2e": "CAST(NULL AS Nullable(Float64))",
              "starts": "'instance'", "ttft": "CAST(NULL AS Nullable(Float64))", "retry": "CAST(NULL AS Nullable(Float64))"}
        missing_px = None
    expr = (
        f"tuple('vllm_pd.request.v2', s.request_id, CAST(NULL AS Nullable(String)), {px['cid']}, toString(s.model), "
        "concat(replaceOne(toString(coalesce(s.p_ts, s.d_ts, s.ts)), ' ', 'T'), 'Z'), "
        f"tuple(nullIf(toString(s.p_instance), ''), nullIf(toString(s.d_instance), ''), {px['retries']}), "
        "tuple(toString(s.status), toString(s.finish_reason), s.http_status), "
        "tuple(s.prompt_tokens, s.cached_tokens, s.output_tokens), "
        f"tuple({px['e2e']}, {px['starts']}, {px['ttft']}, {happened % 's.tpot_ms'}, "
        f"{n % side_total(tree, 'prefill')}, {n % side_total(tree, 'decode')}, {px['retry']}, "
        f"tuple(tuple({n % 's.p_e2e_ms'}, {n % 's.p_ttft_ms'}), tuple({n % 's.d_e2e_ms'}, {n % 's.d_ttft_ms'}, {happened % 's.tpot_ms'}))), "
        f"tuple({pv}, {dv}), "
        f"tuple(d.steps_rest, {rv}), "
        f"tuple({', '.join(v for _, v, _ in bg)}), "
        f"tuple(if(s.p_total_ms > 0, {n % ('s.p_total_ms - (' + p_l1 + ')')}, NULL), if(s.d_first_ms > 0, {n % ('s.d_first_ms - (' + d_l1 + ')')}, NULL)), "
        f"tuple({recorded('prefill', 'p')}, {recorded('decode', 'd')}), "
        "greatest(ifNull(p.attempts, 0), ifNull(d.attempts, 0)), "
        f"arrayConcat({missing}, {missing_px + ', ' if missing_px else ''}if(ifNull(p.lines, 0) = {len(REQUEST_PROCS)}, [], [concat('prefill: ', toString(ifNull(p.lines, 0)), "
        f"' of {len(REQUEST_PROCS)} processes')]), "
        f"if(ifNull(d.lines, 0) = {len(REQUEST_PROCS)}, [], [concat('decode: ', toString(ifNull(d.lines, 0)), ' of {len(REQUEST_PROCS)} processes')])))"
    )
    typ = (
        "Tuple(schema String, request_id String, session_id Nullable(String), client_request_id Nullable(String), model String, ts String, "
        "route Tuple(p Nullable(String), d Nullable(String), retries Nullable(UInt8)), "
        "result Tuple(status String, finish_reason String, http_status Nullable(UInt16)), "
        "tokens Tuple(prompt Nullable(UInt32), cached Nullable(UInt32), output Nullable(UInt32)), "
        "latency_ms Tuple(e2e Nullable(Float64), starts_at String, ttft Nullable(Float64), tpot Nullable(Float64), p_total Nullable(Float64), d_total Nullable(Float64), retry_ms Nullable(Float64), "
        "official Tuple(p Tuple(e2e Nullable(Float64), ttft Nullable(Float64)), d Tuple(e2e Nullable(Float64), ttft Nullable(Float64), tpot Nullable(Float64)))), "
        f"ttft Tuple(p {pt}, d {dt}), "
        f"tpot Tuple(steps Nullable(UInt32), d {rt}), "
        f"background Tuple({', '.join(k + ' ' + t for k, _, t in bg)}), "
        "checks Tuple(p_unattributed_ms Nullable(Float64), d_first_unattributed_ms Nullable(Float64)), "
        "recorded_level Tuple(p UInt8, d UInt8), "
        "attempts UInt32, missing Array(String))"
    )
    cfg = tree.raw.get("config_id", "")
    return (
        f"-- The rows written for this segments.json (config_id {cfg}); every query of the dashboard reads this view.\n"
        f"CREATE OR REPLACE VIEW {LINE} AS SELECT * FROM {TABLE} WHERE config_id = '{cfg}';\n"
        "-- The per-request JSON document: SELECT doc FROM vllm_pd.request_json(id = '<request id>')\n"
        "CREATE OR REPLACE VIEW vllm_pd.request_json AS\n"
        f"SELECT s.request_id AS request_id, toJSONString(CAST({expr}, '{typ}')) AS doc\n"
        f"FROM {anchor(tree, idw)} AS s\n"
        f"LEFT JOIN ({fold(tree, 'prefill', idw)}) AS p ON s.request_id = p.request_id\n"
        f"LEFT JOIN ({fold(tree, 'decode', idw)}) AS d ON s.request_id = d.request_id;\n"
        "-- One request event by event: SELECT * FROM vllm_pd.request_events(id = '<request id>')\n"
        "CREATE OR REPLACE VIEW vllm_pd.request_events AS\n"
        # The API server's own events carry only the observation ID (x is empty); they are found through the request's
        # rows that carry both, and only within the request's time range ± 10 minutes, which the table's order (x, ts) keeps cheap.
        f"SELECT * FROM {EVENT} WHERE x = {{id:String}}\nUNION ALL\n"
        f"SELECT * FROM {EVENT} WHERE x = ''\n"
        f"  AND ts >= (SELECT min(ts) FROM {EVENT} WHERE x = {{id:String}}) - INTERVAL 10 MINUTE\n"
        f"  AND ts <= (SELECT max(ts) FROM {EVENT} WHERE x = {{id:String}}) + INTERVAL 10 MINUTE\n"
        f"  AND observation_id IN (SELECT observation_id FROM {EVENT} WHERE x = {{id:String}} AND observation_id != '');\n"
    )


# ---- schema ----------------------------------------------------------------------------------------------------------

LINE_BASE = """    ts DateTime64(3, 'UTC'),
    request_id String CODEC(ZSTD(1)),
    attempt_id String CODEC(ZSTD(1)),
    role Enum8('prefill' = 1, 'decode' = 2),
    proc Enum8('api' = 1, 'engine' = 2, 'worker' = 3, 'save' = 4),
    part String DEFAULT '' CODEC(ZSTD(1)),
    seq UInt32 DEFAULT 0,
    model LowCardinality(String),
    instance LowCardinality(String),
    node LowCardinality(String),
    status LowCardinality(String) DEFAULT '',
    finish_reason LowCardinality(String) DEFAULT '',
    http_status Nullable(UInt16),
    first_mono_ns Nullable(UInt64),
    api_start_ts Nullable(DateTime64(3, 'UTC')),
    api_end_ts Nullable(DateTime64(3, 'UTC')),
    config_id LowCardinality(String) DEFAULT '',
    first_exec String DEFAULT '' CODEC(ZSTD(1)),
    core_mono_ns Nullable(Int64)"""
EVENT_TABLE = """CREATE TABLE IF NOT EXISTS {t}
(
    ts DateTime64(3, 'UTC'),
    x String DEFAULT '' CODEC(ZSTD(1)),
    request_id String DEFAULT '' CODEC(ZSTD(1)),
    observation_id String DEFAULT '' CODEC(ZSTD(1)),
    task_id String DEFAULT '' CODEC(ZSTD(1)),
    execution_id String DEFAULT '' CODEC(ZSTD(1)),
    record_id String DEFAULT '' CODEC(ZSTD(1)),
    side Enum8('prefill' = 1, 'decode' = 2),
    instance LowCardinality(String),
    node LowCardinality(String),
    role LowCardinality(String),
    proc_name LowCardinality(String),
    pid UInt32,
    event LowCardinality(String),
    duration_ns Nullable(Int64),
    start_ns Nullable(Int64),
    end_ns Nullable(Int64),
    status LowCardinality(String) DEFAULT '',
    raw String CODEC(ZSTD(3)),
    INDEX idx_obs observation_id TYPE bloom_filter(0.001) GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toDate(ts)
ORDER BY (x, ts)
TTL toDate(ts) + INTERVAL {ttl} DAY
SETTINGS ttl_only_drop_parts = 1;"""
SELFCHECK_TABLE = """CREATE TABLE IF NOT EXISTS vllm_pd.filter_selfcheck
(
    ts DateTime64(3, 'UTC'), node LowCardinality(String), instance LowCardinality(String), role LowCardinality(String), pid UInt32,
    config_version LowCardinality(String), python LowCardinality(String),
    seen UInt64, written UInt64, dropped UInt64, no_event UInt64, rank_dropped UInt64, accumulated UInt64, empty_key_dropped UInt64,
    errors UInt64, partials_written UInt64, evicted UInt64, forced UInt64, config_reloads UInt64, config_errors UInt64, inflight UInt64,
    files Array(String), instance_source LowCardinality(String), dp_rank_argv Nullable(Int32),
    pair_unmatched UInt64, pair_negative UInt64, version_dropped UInt64
)
ENGINE = MergeTree
PARTITION BY toDate(ts)
ORDER BY (node, instance, ts)
TTL toDate(ts) + INTERVAL {ttl} DAY
SETTINGS ttl_only_drop_parts = 1;"""
FILE_TABLE = """CREATE TABLE IF NOT EXISTS vllm_pd.filter_file
(
    ts DateTime64(3, 'UTC'), node LowCardinality(String), instance LowCardinality(String), role LowCardinality(String), pid UInt32,
    file String, config_version LowCardinality(String)
)
ENGINE = MergeTree
PARTITION BY toDate(ts)
ORDER BY (node, instance, role, ts)
TTL toDate(ts) + INTERVAL {ttl} DAY
SETTINGS ttl_only_drop_parts = 1;"""


OTHER_TABLE = """CREATE TABLE IF NOT EXISTS vllm_pd.filter_other
(
    ts DateTime64(3, 'UTC'), node LowCardinality(String), instance LowCardinality(String), role LowCardinality(String), pid UInt32,
    event LowCardinality(String)
)
ENGINE = MergeTree
PARTITION BY toDate(ts)
ORDER BY (node, instance, ts)
TTL toDate(ts) + INTERVAL {ttl} DAY
SETTINGS ttl_only_drop_parts = 1;"""


# v3.1-extra: the proxy's line (proxy patch, event pd_proxy_request), one row per request; times in ms on the proxy's clock.
PROXY_TABLE = """CREATE TABLE IF NOT EXISTS vllm_pd.proxy_line
(
    ts DateTime64(3, 'UTC'), request_id String CODEC(ZSTD(1)), client_request_id String DEFAULT '' CODEC(ZSTD(1)),
    model LowCardinality(String), node LowCardinality(String), instance LowCardinality(String), pid UInt32,
    api LowCardinality(String) DEFAULT '', status LowCardinality(String) DEFAULT '', http_status Nullable(UInt16),
    stream Nullable(UInt8), include_usage Nullable(UInt8), n Nullable(UInt16), p_addr LowCardinality(String) DEFAULT '',
    d_addr LowCardinality(String) DEFAULT '', retries Nullable(UInt16), recompute_retries Nullable(UInt16),
    p_http_retries Nullable(UInt16), d_http_retries Nullable(UInt16), retry_ms Nullable(Float32), api_gateway_ms Nullable(Float32),
    proxy_to_p_ms Nullable(Float32), p_total_ms Nullable(Float32), proxy_to_d_ms Nullable(Float32), d_first_ms Nullable(Float32),
    d_total_ms Nullable(Float32), ttft_ms Nullable(Float32), e2e_ms Nullable(Float32),
    INDEX idx_request_id request_id TYPE bloom_filter(0.001) GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toDate(ts)
ORDER BY (ts, request_id)
TTL toDate(ts) + INTERVAL {ttl} DAY
SETTINGS ttl_only_drop_parts = 1;"""
PROXY_SELFCHECK_TABLE = """CREATE TABLE IF NOT EXISTS vllm_pd.proxy_selfcheck
(
    ts DateTime64(3, 'UTC'), node LowCardinality(String), instance LowCardinality(String), pid UInt32,
    queued UInt64, queue_full UInt64, written UInt64, errors UInt64, queue_len UInt64, last_error String DEFAULT ''
)
ENGINE = MergeTree
PARTITION BY toDate(ts)
ORDER BY (node, instance, ts)
TTL toDate(ts) + INTERVAL {ttl} DAY
SETTINGS ttl_only_drop_parts = 1;"""


# v3.1-extra: the filter's counts of first-candidate pairings (segments.json "candidate"), in its self-check line.
CAND_COUNTS = (("cand_maps", "UInt64"), ("cand_applied", "UInt64"), ("cand_unpaired", "UInt64"), ("cand_evicted_full", "UInt64"))
# v3.1-extra, C14: per accumulate rule whose value is a list of paths, how many records took a later path.
FALLBACK_COUNTS = (("acc_fallback", "Map(String, UInt64)"),)
# v3.1-extra: records with no execution_id (segments.json "missing_execution_id").
EXEC_COUNTS = (("exec_missing", "UInt64"),)


def acc_lists(raw):
    """Ids of the accumulate rules whose value is a list of paths (the first that is not null)."""
    return [a["id"] for a in raw.get("accumulate", []) if isinstance(a.get("value"), list)]


def line_columns(tree):
    """(name, type) of every value column of phase_line beyond the base ones."""
    out = []
    for c in tree.event_columns():
        out.append((c, "Nullable(Float32)"))
    for k in tree.request_fields:
        if k in STRING_FIELDS or k == "http_status":
            continue
        out.append((k, req_type(k)))
    for a in tree.acc_ids:
        out += [(f"acc__{a}__ns", "Nullable(UInt64)"), (f"acc__{a}__steps", "Nullable(UInt32)"), (f"acc__{a}__first_ns", "Nullable(UInt64)")]
    for name in tree.ranges():
        out += [(f"rg__{name}__exec", "Array(String)"), (f"rg__{name}__ns", "Array(Int64)"), (f"rg__{name}__total_ns", "Nullable(Int64)")]
    return out


def schema_sql(tree):
    """Tables, then ADD COLUMN for every value column and the retention. Safe to run more than once."""
    cols = line_columns(tree)
    body = "".join(f",\n    {c} {t}" for c, t in cols)
    out = [
        "-- Generated from segments.json by scripts/generate.py. Safe to run more than once.",
        "CREATE DATABASE IF NOT EXISTS vllm_pd;",
        f"""CREATE TABLE IF NOT EXISTS {TABLE}
(
{LINE_BASE}{body},
    INDEX idx_request_id request_id TYPE bloom_filter(0.001) GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toDate(ts)
ORDER BY (model, role, ts, request_id)
TTL toDate(ts) + INTERVAL {SUMMARY_TTL_DAYS} DAY
SETTINGS ttl_only_drop_parts = 1;""",
        EVENT_TABLE.format(t=EVENT, ttl=EVENT_TTL_DAYS),
        SELFCHECK_TABLE.format(ttl=SUMMARY_TTL_DAYS),
        FILE_TABLE.format(ttl=SUMMARY_TTL_DAYS),
        OTHER_TABLE.format(ttl=SUMMARY_TTL_DAYS),
    ]
    if tree.proxy_sourced():
        out += [PROXY_TABLE.format(ttl=SUMMARY_TTL_DAYS), PROXY_SELFCHECK_TABLE.format(ttl=SUMMARY_TTL_DAYS)]
    for c, t in cols + [("api_start_ts", "Nullable(DateTime64(3, 'UTC'))"), ("api_end_ts", "Nullable(DateTime64(3, 'UTC'))"),
                        ("config_id", "LowCardinality(String) DEFAULT ''"), ("first_exec", "String DEFAULT '' CODEC(ZSTD(1))"),
                        ("core_mono_ns", "Nullable(Int64)")]:
        out.append(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {c} {t};")
    # Columns added after the first version of the self-check table, for a ClickHouse that keeps an earlier install's data.
    for c, t in (("instance_source", "LowCardinality(String)"), ("dp_rank_argv", "Nullable(Int32)"),
                 ("pair_unmatched", "UInt64"), ("pair_negative", "UInt64"), ("version_dropped", "UInt64")) + CAND_COUNTS * bool(tree.raw.get("candidate")) + \
            FALLBACK_COUNTS * bool(acc_lists(tree.raw)) + EXEC_COUNTS * bool(tree.raw.get("missing_execution_id")):
        out.append(f"ALTER TABLE vllm_pd.filter_selfcheck ADD COLUMN IF NOT EXISTS {c} {t};")
    # Retention follows deploy.env every time this runs (EVENT_TTL_DAYS, SUMMARY_TTL_DAYS).
    out.append(f"ALTER TABLE {TABLE} MODIFY TTL toDate(ts) + INTERVAL {SUMMARY_TTL_DAYS} DAY;")
    out.append(f"ALTER TABLE {EVENT} MODIFY TTL toDate(ts) + INTERVAL {EVENT_TTL_DAYS} DAY;")
    out.append(f"ALTER TABLE vllm_pd.filter_selfcheck MODIFY TTL toDate(ts) + INTERVAL {SUMMARY_TTL_DAYS} DAY;")
    out.append(f"ALTER TABLE vllm_pd.filter_file MODIFY TTL toDate(ts) + INTERVAL {SUMMARY_TTL_DAYS} DAY;")
    out.append(f"ALTER TABLE vllm_pd.filter_other MODIFY TTL toDate(ts) + INTERVAL {SUMMARY_TTL_DAYS} DAY;")
    if tree.proxy_sourced():
        out.append(f"ALTER TABLE {PROXY} MODIFY TTL toDate(ts) + INTERVAL {SUMMARY_TTL_DAYS} DAY;")
        out.append(f"ALTER TABLE vllm_pd.proxy_selfcheck MODIFY TTL toDate(ts) + INTERVAL {SUMMARY_TTL_DAYS} DAY;")
    return "\n".join(out) + "\n"


# ---- dashboard -------------------------------------------------------------------------------------------------------

# Colors by segment id: COLOR up to the first token, COLOR_REST for D's later steps; GAUGE for the gauge columns of the detail tables.
COLOR = {"wait_for_put": "#FFA6B0", "req_resolve": "#8AB8FF", "tokenizer": "#1F60C4", "add_request": "#C0D8FF", "schedule": "#FF9830",
         "start_load_kv": "#CA95E5", "forward": "#56A64B", "sampling": "#96D98D", "detokenizer": "#5794F2"}
COLOR_REST = {"schedule": "#FFCB7D", "forward": "#37872D", "sampling": "#C8F2C2", "detokenizer": "#3274D9"}
GAUGE = {"schedule": "#FF9830", "start_load_kv": "#CA95E5", "forward": "#56A64B", "wait_for_save": "#8F3BB8"}


def color_of(node):
    return (COLOR_REST if node.phase == "tpot" else COLOR).get(node.id)


def color_override(name, color):
    return {"matcher": {"id": "byName", "options": name}, "properties": [{"id": "color", "value": {"fixedColor": color, "mode": "fixed"}}]}


def stage_mappings(tree):
    """Colors for the largest-stage cells: the segment name, then optionally its share."""
    names = {}
    for role in ("prefill", "decode"):
        for name, n in bar_series(tree, role):
            if color_of(n):
                names.setdefault(name, color_of(n))
    return [{"type": "regex", "options": {"pattern": "^" + re.escape(k) + "( .*)?$", "result": {"color": v, "index": i}}} for i, (k, v) in enumerate(names.items())]


def by_name(panel, name):
    for o in panel["fieldConfig"]["overrides"]:
        if o["matcher"].get("options") == name:
            return o
    return None


def set_props(panel, name, **props):
    """Sets field properties for one column, replacing earlier values of the same property."""
    o = by_name(panel, name)
    if o is None:
        o = {"matcher": {"id": "byName", "options": name}, "properties": []}
        panel["fieldConfig"]["overrides"].append(o)
    for key, value in props.items():
        pid = key.replace("__", ".")
        o["properties"] = [p for p in o["properties"] if p["id"] != pid]
        o["properties"].append({"id": pid, "value": value})


def gauge_props(color, width=None):
    props = {"unit": "ms", "custom__cellOptions": {"type": "gauge", "mode": "basic", "valueDisplayMode": "text"}, "color": {"mode": "fixed", "fixedColor": color}}
    if width:
        props["custom__width"] = width
    return props


def header_width(name):
    """Pixels a column header needs: CJK characters are about twice as wide as Latin ones."""
    return int(sum(13.5 if ord(c) > 255 else 7.2 for c in name)) + 14


def fit_columns(panel, names, gauges):
    """Fits a 20-row detail table on a 1600 px page: every header stays readable, every value too (80 px holds 0.0733 ms), what is left goes to the plain columns."""
    room = 1530 - 32 - 124 - 110
    plain = []
    for name in names:
        if name in gauges:
            width = max(88, header_width(name))
            set_props(panel, name, **gauge_props(gauges[name], width))
            room -= width
        else:
            plain.append((name, max(80, header_width(name))))
    extra = max(0, room - sum(w for _, w in plain)) // max(1, len(plain))
    for name, width in plain:
        set_props(panel, name, custom__width=width + extra)


def fit_height(panels, keys, rows):
    """A table tall enough for its rows, so that a new segment never hides behind a scroll bar. Everything below moves down."""
    want = int(math.ceil(((rows + 1) * 36 + 68) / 38.0))
    first = panels[keys[0]]["gridPos"]
    y, old = first["y"], first["h"]
    delta = want - old
    for p in panels.values():
        if p["gridPos"]["y"] >= y + old:
            p["gridPos"]["y"] += delta
    for k in keys:
        panels[k]["gridPos"]["h"] = want


STEPS_TABLE_H = 4


def add_steps_tables(panels, by_key, tree):
    """A one-row table right above each distribution table, columns as wide as the distribution table's: P 轮次 and
    D 总步数 (p50, p90, p99, 均值, 最大). Everything from the distribution tables down moves down."""
    y = by_key["dist_p"]["gridPos"]["y"]
    for p in panels:
        if p["gridPos"]["y"] >= y:
            p["gridPos"]["y"] += STEPS_TABLE_H
    next_id = max(p["id"] for p in panels) + 1
    for i, (key, src_key, role, title, label) in enumerate((("steps_p", "dist_p", "prefill", "P 侧轮次", "轮次"),
                                                            ("steps_d", "dist_d", "decode", "D 侧总步数", "总步数"))):
        src = by_key[src_key]

        def width(name):
            return next(x["value"] for x in by_name(src, name)["properties"] if x["id"] == "custom.width")

        panel = copy.deepcopy(src)
        panel.update(key=key, id=next_id + i, title=title)
        panel["gridPos"] = dict(src["gridPos"], y=y, h=STEPS_TABLE_H)
        fc = panel["fieldConfig"]
        fc["defaults"].update(unit="none", decimals=0)
        fc["overrides"] = (
            [{"matcher": {"id": "byName", "options": "项目"}, "properties": [{"id": "custom.width", "value": width("阶段")}]}]
            + [{"matcher": {"id": "byName", "options": c}, "properties": [{"id": "custom.width", "value": width(c)}]} for c in ("p50", "p90", "p99")]
            + [{"matcher": {"id": "byName", "options": "均值"}, "properties": [{"id": "custom.width", "value": width("均值")}, {"id": "decimals", "value": 1}]},
               {"matcher": {"id": "byName", "options": "最大"}, "properties": [{"id": "custom.width", "value": width("均值占比")}]}])
        panel["targets"][0]["rawSql"] = steps_sql(tree, role, label)
        panels.insert(panels.index(src), panel)
        by_key[key] = panel


TOP_TABLE_H = 30


def top_paged(panels, top):
    """The big table of slowest requests: taller, and paged, so its rows never need a scroll bar inside the panel."""
    y, old = top["gridPos"]["y"], top["gridPos"]["h"]
    for p in panels:
        if p is not top and p["gridPos"]["y"] >= y + old:
            p["gridPos"]["y"] += TOP_TABLE_H - old
    top["gridPos"]["h"] = TOP_TABLE_H
    top["options"]["footer"]["enablePagination"] = True
    top["options"]["enablePagination"] = True


def build_board(tree):
    with open(PACK / "grafana" / "board_style.json", encoding="utf-8") as f:
        board = json.load(f)
    panels = board["panels"]
    by_key = {p["key"]: p for p in panels}
    get = by_key.__getitem__

    def sql(key, text, title=None):
        p = get(key)
        t = p["targets"][0]
        t["rawSql"] = text
        if title:
            p["title"] = title

    inst_q = (
        f"SELECT DISTINCT instance AS name FROM {LINE} WHERE $__timeFilter(ts) AND proc = 'api' AND part = '' "
        "ORDER BY startsWith(name, 'D'), toUInt32OrZero(substring(name, 2))"
    )
    for v in board["templating"]["list"]:
        if v["name"] == "model":
            v["query"] = f"SELECT DISTINCT model FROM {LINE} WHERE $__timeFilter(ts) ORDER BY model"
        elif v["name"] == "instance":
            v["query"] = inst_q
        elif v["name"] == "request_id":
            v["query"] = f"WITH {top_cte(tree, TOP_TABLE_N)} SELECT request_id FROM t ORDER BY ord DESC"

    base = f"FROM {win_anchor(tree)} AS s WHERE {REQ_FILTER}"
    sql("requests", f"SELECT count() AS requests {base}")
    sql("e2e", f"SELECT quantile(0.5)(p_e2e_ms) AS \"P p50\", quantile(0.99)(p_e2e_ms) AS \"P p99\", quantile(0.5)(d_e2e_ms) AS \"D p50\", quantile(0.99)(d_e2e_ms) AS \"D p99\" {base}")
    sql("ttft", f"SELECT quantile(0.5)(p_ttft_ms) AS \"P p50\", quantile(0.99)(p_ttft_ms) AS \"P p99\", quantile(0.5)(d_ttft_ms) AS \"D p50\", quantile(0.99)(d_ttft_ms) AS \"D p99\" {base}")
    sql("tpot", f"SELECT quantileIf(0.5)(tpot_ms, tpot_ms > 0) AS p50, quantileIf(0.99)(tpot_ms, tpot_ms > 0) AS p99 {base}")
    pt, dt = side_total(tree, "prefill"), side_total(tree, "decode")
    sql("totals", f"SELECT quantile(0.5)({pt}) AS \"P p50\", quantile(0.99)({pt}) AS \"P p99\", quantile(0.5)({dt}) AS \"D p50\", quantile(0.99)({dt}) AS \"D p99\" {base}")
    sql("dist_p", dist_sql(tree, "prefill"))
    sql("dist_d", dist_sql(tree, "decode"))
    for key, role in (("dist_p", "prefill"), ("dist_d", "decode")):
        fit_dist_names(get(key), [dist_label(r) for r in rows_of(tree, role)])
    add_steps_tables(panels, by_key, tree)
    sql("top", top_table(tree), title="Top-%d 最慢请求" % TOP_TABLE_N)
    top_paged(panels, get("top"))
    get("_r1")["title"] = "最慢请求 · 排序依据：${order_by:text}"
    sql("bars_p", bars(tree, "prefill"))
    sql("bars_d", bars(tree, "decode"))
    for key, role in (("bars_p", "prefill"), ("bars_d", "decode")):
        ov = [o for o in get(key)["fieldConfig"]["overrides"] if o["matcher"].get("options") == "request_id"]
        for name, n in bar_series(tree, role):
            if color_of(n):
                ov.append(color_override(name, color_of(n)))
        get(key)["fieldConfig"]["overrides"] = ov
    for name in ("P 最大段", "D 最大段"):
        if by_name(get("top"), name):
            set_props(get("top"), name, mappings=stage_mappings(tree))

    # The detail tables of the slowest 20: each side's segments in tree order, cut into as many full-width tables as it
    # takes for every column to be readable without scrolling (a first-level segment and its children stay together).
    # The proxy segments stay out: until the proxy patch they are empty in every row.
    gauges = {column_name(n): GAUGE[n.id] for n in tree.nodes("prefill", "ttft", "background") + tree.nodes("decode", "ttft", "tpot")
              if n.depth == 1 and n.id in GAUGE}
    p, d = tree.sides["prefill"], tree.sides["decode"]
    # (side, total, title, the first-level segments, columns every table of the section starts with, whether the
    # title names the first and last segment of each table)
    sections = [
        ("prefill", "P 合计", "P 侧", [n for n in p["ttft"]], [], True),
        ("prefill", "P 合计", "P 侧：后台 wait_for_save（不计入 P 合计）", [n for n in p["background"] if not n.parallel], [], False),
        ("prefill", "P 合计", "P 侧：并行段（不计入 P 合计）", [n for n in p["background"] if n.parallel], [], False),
        ("decode", "D 合计", "D 侧：到第一个 token", [n for n in d["ttft"]], [], True),
        ("decode", "D 合计", "D 侧：第二步起", [n for n in d["tpot"]], [("步数", "ph.steps_rest")], True),
        ("decode", "D 合计", "D 侧：并行段（不计入 D 合计，所有步之和）", [n for n in d["background"] if n.parallel], [], False),
    ]
    room = 1530 - 32 - 124 - 110

    def width(cols):
        return sum(max(88 if name in gauges else 80, header_width(name)) for name, _ in cols)
    tables = []
    for role, total_label, title, tops, extra, ranged in sections:
        groups = [(top, detail_columns(list(top.walk()))) for top in tops]
        groups = [(t, c) for t, c in groups if c]
        if not groups:
            continue
        base = width(extra)
        # How many tables at least (filling each up), then the same number with the widths spread evenly.
        k, used = 1, base
        for _, cols in groups:
            if used + width(cols) > room and used > base:
                k, used = k + 1, base
            used += width(cols)
        target = sum(width(c) for _, c in groups) / k
        chunks, chunk, used = [], [], 0
        for top, cols in groups:
            w = width(cols)
            if chunk and len(chunks) < k - 1 and (used + w > room - base or used + w / 2 > target):
                chunks.append(chunk)
                chunk, used = [], 0
            chunk.append((top, cols))
            used += w
        chunks.append(chunk)
        for j, chunk in enumerate(chunks):
            name = title
            if ranged:
                ids = [t.id for t, _ in chunk]
                name += "：" + (ids[0] if len(ids) == 1 else "%s → %s" % (ids[0], ids[-1]))
            elif len(chunks) > 1:
                name += "（%d/%d）" % (j + 1, len(chunks))
            tables.append((role, total_label, name, chunk, extra))
    # One panel per table, in the place of the Top-20 detail panels of board_style.json; everything below moves down.
    old = [x for x in panels if x.get("key", "").startswith("top_") and x["key"] != "top"]
    template = old[0]
    # Each side's tables start from that side's first panel: the request_id link and the gauge on the side's total.
    templates = {"prefill": by_key["top_p1"], "decode": by_key["top_d"]}
    y0 = min(x["gridPos"]["y"] for x in old)
    y_end = max(x["gridPos"]["y"] + x["gridPos"]["h"] for x in old)
    h = template["gridPos"]["h"]
    at = panels.index(template)
    for x in old:
        panels.remove(x)
        del by_key[x["key"]]
    delta = len(tables) * h - (y_end - y0)
    for x in panels:
        if x["gridPos"]["y"] >= y_end:
            x["gridPos"]["y"] += delta
    next_id = max(x["id"] for x in panels + old) + 1
    for i, (role, total_label, name, chunk, extra) in enumerate(tables):
        cols = list(extra) + [c for _, cs in chunk for c in cs]
        panel = copy.deepcopy(templates[role])
        panel["key"] = "top_detail_%d" % i
        panel["id"] = next_id + i
        panel["gridPos"] = dict(template["gridPos"], y=y0 + i * h)
        panel["title"] = "Top-20 子段明细 · %s（ms）" % name
        panel["targets"][0]["rawSql"] = top_detail(tree, total_label, role, cols)
        panels.insert(at + i, panel)
        by_key[panel["key"]] = panel
        fit_columns(panel, [name for name, _ in cols], gauges)
        set_props(panel, total_label, custom__width=110)
        if extra:
            set_props(panel, "步数", unit="none")
    set_props(get("top"), "到达时间", unit="time:HH:mm:ss")

    one = f"FROM {one_anchor(tree)} AS s"
    sql("one_e2e", f"SELECT p_e2e_ms AS P, d_e2e_ms AS D {one}")
    sql("one_ttft", f"SELECT p_ttft_ms AS P, d_ttft_ms AS D {one}")
    sql("one_tpot", f"SELECT nullIf(tpot_ms, 0) AS TPOT {one}")
    sql("one_tokens", f"SELECT prompt_tokens AS \"输入\", cached_tokens AS \"命中\", output_tokens AS \"输出\" {one}")
    sql("one_route", f"SELECT p_instance AS P, d_instance AS D, finish_reason AS \"结束\" {one}")
    sql("timeline", timeline(tree))
    sql("detail_p", detail_p(tree))
    sql("detail_d", detail_d(tree))
    sql("events", events_sql())
    rows_p, rows_d = len(rows_of(tree, "prefill")), len(rows_of(tree, "decode"))
    fit_height(by_key, ["dist_p", "dist_d"], max(rows_p, rows_d))
    fit_height(by_key, ["detail_p", "detail_d"], max(rows_p, rows_d))
    for p_ in panels:
        p_.pop("key", None)
    return board


def apply_node_values():
    """Two values of this folder's segments.json come from deploy.env, so that an upgrade or rollback no longer resets
    them; only those values are replaced, every other byte kept (criteria.set_log_dir, criteria.set_keep_hours):
    - line.log_dir, the folder the filters write to inside the vLLM container: NODE_LOG_DIR_IN_CONTAINER (design 2.8 /
      F1 (3)); blank leaves the value as it is;
    - line.keep_hours, how long the filters keep their files on the nodes: NODE_LOG_KEEP_HOURS (known issue 14); blank
      means the pack's 12 hours.
    The nodes get the values with segments.json (bin/nodes.sh push); their filters reread it, no vLLM restart."""
    seg = PACK / "segments.json"
    if os.environ.get("SEGMENTS_FILE"):
        return
    from criteria import hours_text, keep_hours_value, set_keep_hours, set_log_dir  # noqa: E402
    text = seg.read_text(encoding="utf-8")
    new = text
    want = (os.environ.get("NODE_LOG_DIR_IN_CONTAINER") or "").strip()
    if want:
        new = set_log_dir(new, want)
    try:
        hours = keep_hours_value(os.environ.get("NODE_LOG_KEEP_HOURS"))
    except ValueError as e:
        raise SystemExit("deploy.env 有问题：%s（节点上日志保留的小时数，留空是 12）" % e) from None
    new = set_keep_hours(new, hours)
    if new != text:
        tmp = seg.with_name(".segments.json.tmp")
        tmp.write_text(new, encoding="utf-8")
        os.chmod(tmp, os.stat(seg).st_mode & 0o777)
        os.replace(tmp, seg)
        if want and set_log_dir(text, want) != text:
            print("segments.json 的 log_dir 改成 %s（deploy.env 的 NODE_LOG_DIR_IN_CONTAINER）" % want)
        if set_keep_hours(text, hours) != text:
            print("segments.json 的 keep_hours 改成 %s（deploy.env 的 NODE_LOG_KEEP_HOURS%s）" % (
                hours_text(hours), "" if (os.environ.get("NODE_LOG_KEEP_HOURS") or "").strip() else " 留空，用 12"))


def main():
    apply_node_values()
    tree = Tree()
    GEN.mkdir(exist_ok=True)
    (GEN / "schema.sql").write_text(schema_sql(tree), encoding="utf-8")
    (GEN / "views.sql").write_text(views_sql(tree), encoding="utf-8")
    board = build_board(tree)
    text = json.dumps(board, ensure_ascii=False, indent=1)
    (GEN / "vllm-pd-request.json").write_text(text + "\n", encoding="utf-8")
    (GEN / "columns.json").write_text(json.dumps({"phase_line": [c for c, _ in line_columns(tree)]}, indent=1) + "\n", encoding="utf-8")
    print("分段：P %d 个，D %d 个；phase_line 数值列 %d 个；看板 %d 个 panel；事件表保留 %d 天，汇总表保留 %d 天" % (
        len(tree.nodes("prefill", "ttft", "background")), len(tree.nodes("decode", "ttft", "tpot", "background")), len(line_columns(tree)),
        sum(1 for p in board["panels"] if p["type"] != "row"), EVENT_TTL_DAYS, SUMMARY_TTL_DAYS))


if __name__ == "__main__":
    main()
