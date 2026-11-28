#!/usr/bin/env python3
"""Generates the request board (Grafana) from the segment definitions in vllm_pd.segment_def and the columns of
vllm_pd.request_doc (interface two). Output: generated/vllm-pd-request.json; bin/grafana_board.sh imports it.

Before anything is generated the version table vllm_pd.db_interface is checked against grafana.env's DB_INTERFACE; on a
mismatch nothing is written and the board in Grafana stays as it is.

  vllm_pd.request_doc   one document per request, written by the fetcher (the largest doc_version counts, FINAL); every
                        column the panels read is taken out of the document by ClickHouse.
  vllm_pd.status_doc    the status documents of the instances, the proxy workers and the fetcher, one a minute each.
  vllm_pd.segment_def   the segments of each config_version: code, group, side, phase, order, depth, level, label, column.
Without the proxy patch there is no proxy record: the documents join P and D by the request key, and the official E2E /
TTFT of each side are shown side by side, never added up.
"""
import copy
import json
import math
import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from segdef import PACK, Reader, Tree, check_interface, contract_rows, grafana_env, load_tree  # noqa: E402

DOC = "vllm_pd.request_doc"
STATUS = "vllm_pd.status_doc"
# T58: the dashboard variable "层级" (1, 2, 3): rows, spans and Top-20 detail columns deeper than it are not shown.
LEVEL = "${level}"
# Top-20 detail columns: an L2 name ends with one zero-width space, an L3 name with two (column_name); a column whose name
# has ${level} or more of them is left out by the panel's "filter fields by name" (checked on Grafana 12.4.2).
LEVEL_COL_RE = "^(?!.*(\\u200b){${level},}).*$"
SEL = "${instance:singlequote}"
REQ_FILTER = f"s.model = '$model' AND (s.p_instance IN ({SEL}) OR s.d_instance IN ({SEL}))"
# A request belongs to the window by its document's ts: when it arrived (the proxy's start, else P's or D's API start).
WIDE_WINDOW = "$__timeFilter(ts)"
IN_WINDOW = "1"
WINDOW_L = "$__timeFilter(ts)"
# Distributions over more than SAMPLE_TARGET requests use every n-th request (by hash of the request key), n chosen
# from a cheap approximate count, so a query's memory has an upper bound whatever the window and the request rate.
SAMPLE_TARGET = 200000
SAMPLE_N = f"(SELECT greatest(1, toUInt64(ceil(uniq(request_id) / {SAMPLE_TARGET}))) FROM {DOC} WHERE {WINDOW_L} AND model = '$model')"
SAMPLED = "cityHash64(request_id) % sample_n = 0"
GEN = PACK / "generated"


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
    """P 合计 / D 合计, as the request document has them (latency_ms.p_total, d_total): P's includes the proxy segments
    measured on the proxy (T64 item 5), D's is its API server's own (C5). Empty when the side has none."""
    own = col(alias, "p_total_ms" if role == "prefill" else "d_total_ms")
    return f"if({own} > 0, {own}, NULL)"


def alias_of(node, alias):
    """Segments measured on the proxy are read from the request row (empty until the proxy patch)."""
    return "s" if node.phase == "proxy" else alias


# ---- one row per request: the anchor every panel starts from --------------------------------------------------------


def anchor(tree, where_rows, having="1"):
    """One row per request: its document's columns in request_doc (the largest doc_version), under the names the panels
    use. The segments of the proxy are columns of the same row."""
    proxy = []
    for role in ("prefill", "decode"):
        for n in tree.sides[role]["proxy"]:
            if n.column not in proxy:
                proxy.append(n.column)
    extra = "".join(f", {c}" for c in proxy)
    return (
        "(SELECT request_id, ts, ts AS t_min, model, route_p AS p_instance, route_d AS d_instance, p_start AS p_ts, d_start AS d_ts,\n"
        "    p_e2e_ms, d_e2e_ms, p_ttft_ms, d_ttft_ms, tpot_ms, p_total_ms, d_total_ms, d_ttft_ms AS d_first_ms,\n"
        "    prompt_tokens, cached_tokens, output_tokens, status, finish_reason, http_status, closed, attempts"
        f"{extra}\n"
        f"  FROM {DOC} FINAL WHERE {where_rows} AND ({having}))"
    )


def win_anchor(tree):
    return anchor(tree, WIDE_WINDOW, IN_WINDOW)


def one_anchor(tree, rid="'${request_id}'"):
    return anchor(tree, f"request_id = {rid}")


# ---- one row per request and side: the segments ---------------------------------------------------------------------

class Row:
    def __init__(self, label, source, expr, procs, opt=False, share=True, rest=False, columns=(), depth=1, lvl=1, kind="seg"):
        self.label, self.source, self.expr, self.procs = label, source, expr, procs
        self.opt, self.share, self.rest, self.columns = opt, share, rest, columns
        self.depth, self.lvl = depth, lvl      # T58: the segment's depth (层级) and from_level (the lowest PD_DIAG_LEVEL that records it)
        # T64: "seg", "else", "total", or "header" (a group's title row in the two segment tables: a name, blank cells)
        self.kind = kind


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


def seg_share(node):
    """Whether a segment's row takes a share of its side's total: not the background, comparison rows or segments outside
    the total; a proxy segment with a src only on P (T64 item 5: P 合计 includes it; D 合计 is the API server's own, C5)."""
    if node.phase == "background" or node.outside_total:
        return False
    if node.phase == "proxy" and node.src:
        return node.role == "prefill"
    return True


# T64 (items 7, 10): the segment tables group a side's rows under title rows; the group's segments sit one level in.
GROUP_BACK, GROUP_FIRST, GROUP_REST = "后台任务", "第一步", "第二步起 · 每步"
# The step words of the D segments' names (forward（第一步）): a table row under its group's title row goes without them.
STEP_SUFFIX = re.compile(r"（(第一步|第二步起)）$")


def table_name(node):
    return STEP_SUFFIX.sub("", node.label.replace("设备侧，", ""))


def grouped_label(node):
    """The row name of a segment inside a group: one level in under the title row (the group's last first-level segment
    takes '└ ', the others '├ '; the levels below follow on, as tree_label draws them)."""
    top = node
    while top.parent is not None:
        top = top.parent
    siblings = top.role_tops
    last_top = siblings[-1] is top
    if node.depth == 1:
        return ("└ " if last_top else "├ ") + table_name(node)
    return ("\u00a0" * 3 if last_top else "│ ") + tree_label(node)


def else_expr(tree, role, alias=None):
    """T64 item 2: per request, the side's total minus its first-level segments that take a share (empty counts 0); no
    sub-segments, comparison rows, background or segments outside the total. Empty when the side has no
    core_summary[request] for the request (rlv = 0, levels_sql); below 0 it is 0."""
    kids = [n for ph in ("proxy", "ttft", "tpot") for n in tree.sides[role][ph] if seg_share(n)]
    tot = f"toFloat64({side_total(tree, role, alias)})"
    rest = " + ".join(f"ifNull(toFloat64({value_expr(n, alias_of(n, alias) if alias else None)}), 0)" for n in kids) or "0"
    return f"if(rlv = 0 OR isNull({tot}), NULL, greatest(0, {tot} - ({rest})))"


def rows_of(tree, role, with_else=True):
    """The rows of a side's segment tables. P: the proxy and first-token segments, else, P 合计, then the background under
    「后台任务」. D: the proxy segment, the first step under 「第一步」, the later steps under 「第二步起 · 每步」, else, D 合计.
    The single-request tables leave else out (with_else=False)."""
    side = tree.sides[role]
    rows = []

    def add(node, grouped=False):
        source = "summary" if node.phase == "proxy" else "fold"
        label = grouped_label(node) if grouped else (table_name(node) if node.depth == 1 else tree_label(node))
        rows.append(Row(label, source, value_expr(node), node.procs(), node.optional, seg_share(node), node.phase == "tpot",
                        node.columns_below(), node.depth, node.from_level))
        for c in node.children:
            add(c, grouped)

    def group(title, tops):
        if not tops:
            return
        for t in tops:
            t.role_tops = tops
        rows.append(Row(title, "header", "CAST(NULL AS Nullable(Float64))", set(), share=False, depth=0, kind="header"))
        for top in tops:
            add(top, True)

    for top in side["proxy"]:
        add(top)
    if role == "prefill":
        for top in side["ttft"]:
            add(top)
    else:
        group(GROUP_FIRST, side["ttft"])
        group(GROUP_REST, side["tpot"])
    if with_else:
        rows.append(Row("else", "summary", else_expr(tree, role), {"proxy"}, kind="else"))
    rows.append(Row(side["total_label"], "summary", side_total(tree, role, None), {"proxy"}, kind="total"))
    group(GROUP_BACK, side["background"])
    return rows


def fold(tree, role, where):
    """One row per request with one side's segments: the columns of its document in request_doc. The merging of a
    request's records (parts, attempts, first and later steps) is done by the fetcher; the document holds the result."""
    cols = []
    for n in tree.nodes(role, "ttft", "tpot", "background"):
        if n.column not in cols:
            cols.append(n.column)
    steps = "ifNull(steps, 0)" if role == "decode" else "toUInt32(0)"
    return (f"SELECT request_id, {', '.join(cols)}, toUInt32({steps}) AS steps_rest, attempts FROM {DOC} FINAL WHERE {where}")


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
    # T56: comparison rows and segments outside the total overlap the others; a bar of them would count time twice.
    return [(n.label, n) for n in side["ttft"] + side["tpot"] if not n.outside_total]


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


def levels_sql(where):
    """T58: each request's PD_DIAG_LEVEL per side, as its document has them (diag_level: 0 for a side the request has no
    instance on, 3 for a side whose records carry no level). 0 also for a side whose core_summary[request] did not
    arrive (an incomplete document lists it in missing), as before; level_of() takes 0 as 3. One row per request."""
    side_lv = "toUInt8(if(has(missing, 'required: {s} core_summary[request]'), 0, {c}))"
    return (
        f"SELECT request_id, {side_lv.format(s='prefill', c='p_level')} AS p_level, {side_lv.format(s='decode', c='d_level')} AS d_level "
        f"FROM {DOC} FINAL WHERE {where}"
    )


def level_of(alias, role):
    """The side's level of a request joined from levels_sql (0, a request with no row, is 3)."""
    c = f"{alias}.{'p_level' if role == 'prefill' else 'd_level'}"
    return f"if({c} = 0, toUInt8(3), {c})"


def arr(values):
    return "[" + ", ".join(str(v) for v in values) + "]"


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
    lv = levels_sql(f"{WIDE_WINDOW} AND request_id IN (SELECT request_id FROM {s_cte})")
    # Only the columns used here come along from the window's requests (s is sampled already, see s_window_cte).
    keep = ", ".join(["request_id", total] + [n.column for n in tree.sides[role]["proxy"]])
    lcol = "p_level" if role == "prefill" else "d_level"
    # n: the requests with a value in the segment; n_all (T55): the requests the segment could have (finished on this side;
    # for a later-step row, with at least one step after the first). The aggregates skip NULL, so they read as before.
    # T58: a request whose side ran below the segment's from_level cannot have it: it counts in n_skip ("未采"), not in
    # n_all, and skip_lv is the highest such level.
    # T64 (乙): the share is the segment's time over those n_all requests (empty counts 0) divided by their total
    # (ssum / tsum) over the same requests on every row, later-step rows included (a request without later steps counts 0
    # there), so the first-level rows and else add up to the total; rlv is the side's level as levels_sql has it (0: no
    # core_summary[request]), for the else row.
    ok = f"(r = 0 OR {steps} > 0)"
    return (
        f"SELECT toUInt8(idx) AS idx, countIf({ok} AND isNotNull(ms) AND sl >= fl) AS n, countIf({ok} AND sl >= fl) AS n_all, countIf({ok} AND sl < fl) AS n_skip, maxIf(sl, {ok} AND sl < fl) AS skip_lv, "
        f"quantilesExactIf(0.5, 0.9, 0.99)({per}, {ok}) AS q, quantileExactIf(0.5)({per}, {ok} AND (o = 0 OR ms > 0)) AS p50, avgIf({per}, {ok}) AS mean, "
        f"sumIf(ifNull(ms, 0), sl >= fl) AS ssum, sumIf(tot, sl >= fl) AS tsum\n"
        f"  FROM (SELECT f.*, s.{total} AS {total}{', ' + pc if pc else ''}, {level_of('dl', role)} AS sl, ifNull(dl.{lcol}, 0) AS rlv,"
        f" toFloat64({side_total(tree, role, None)}) AS tot FROM ({f}) AS f INNER JOIN (SELECT {keep} FROM {s_cte}) AS s ON s.request_id = f.request_id\n"
        f"        LEFT JOIN ({lv}) AS dl ON dl.request_id = f.request_id)\n"
        f"  ARRAY JOIN {idx} AS idx, {vals} AS ms, {opt} AS o, {rest} AS r, {arr(r.lvl for r in rows)} AS fl\n"
        f"  WHERE ifNull({total}, 0) > 0 GROUP BY idx"
    )


def s_window_cte(tree):
    """The window's requests for the distributions: every sample_n-th one (all of them up to SAMPLE_TARGET)."""
    sampled = anchor(tree, f"{WIDE_WINDOW} AND {SAMPLED}", IN_WINDOW)
    return f"{SAMPLE_N} AS sample_n,\ns AS (SELECT * FROM {sampled} AS s WHERE {REQ_FILTER})"


def steps_sql(tree, role, label):
    """How many steps (executions) a finished request of the window took part in on one side: its document's
    sides.<p|d>.executions (core_summary[request] members[].executions, added up over the attempts by the fetcher).
    P: chunked-prefill chunks, steps redone after preemption included; D: the first step and the later ones."""
    total = "p_total_ms" if role == "prefill" else "d_total_ms"
    ex = "p_executions" if role == "prefill" else "d_executions"
    return (
        f"WITH {s_window_cte(tree)}\n"
        f"SELECT '{label}' AS \"项目\", if(c = 0, NULL, q[1]) AS p50, if(c = 0, NULL, q[2]) AS p90, if(c = 0, NULL, q[3]) AS p99,\n"
        "       if(c = 0, NULL, m) AS \"均值\", if(c = 0, NULL, mx) AS \"最大\"\n"
        "FROM (SELECT count() AS c, quantilesExact(0.5, 0.9, 0.99)(n) AS q, avg(n) AS m, max(n) AS mx FROM (\n"
        f"  SELECT request_id, {ex} AS n FROM {DOC} FINAL\n"
        f"  WHERE {WIDE_WINDOW} AND {SAMPLED} AND request_id IN (SELECT request_id FROM s WHERE ifNull({total}, 0) > 0) AND ifNull({ex}, 0) > 0))"
    )


HDR = -1   # T64: what a title row's cells hold; the segment tables map it to a blank (style_segment_table)


def hdr_of(rows, idx):
    return f"{arr(1 if r.kind == 'header' else 0 for r in rows)}[{idx}]"


def cell(rows, idx, expr):
    """A segment table's cell: blank on a title row, else the expression."""
    return f"if({hdr_of(rows, idx)} = 1, {HDR}, {expr})"


DIST_GAUGE_MIN = 100     # T56: the share gauge still readable
NUM_W, NUM_MIN = 98, 88  # T64 item 4: the quantile and mean columns; 88 px still holds 0.0663 ms
TABLE_W = 772            # what a half-width table holds on the board's 1600 px page (Grafana 12.4.2, the lab's measurement)
HAS_VALUE = "有值 / 请求"   # T55: how many of the window's requests have a value in the row, out of those that could
HAS_VALUE_W = 143      # T64: room for "999 / 999，未采 99"
# T64 item 3: the column is hidden unless grafana.env says BOARD_HAS_VALUE=1 (bin/has_value.sh show | hide)
SHOW_HAS_VALUE = False   # main() sets it from grafana.env
GROUP_BG = "rgba(87, 148, 242, 0.22)"


def text_w(s):
    """Pixels a cell's text needs at the table's font (measured on Grafana 12.4.2), with the cell's padding."""
    return int(sum(13.5 if ord(c) > 0x2fff else 9 if 0x2500 <= ord(c) < 0x2600 else 3.5 if c in "  " else 7.1 for c in s)) + 20


def width_of(panel, name):
    o = by_name(panel, name)
    return next((p["value"] for p in o["properties"] if p["id"] == "custom.width"), None) if o else None


def take(panel, need, name, floor):
    """Takes up to `need` px from a column, down to `floor`; returns what is still missing."""
    w = width_of(panel, name)
    if need <= 0 or not w:
        return need
    t = min(need, max(0, w - floor))
    if t:
        set_props(panel, name, custom__width=w - t)
    return need - t


def style_segment_table(panel, rows, dist):
    """T64: a segment table's title rows (one background over the row, blank cells) and its widths. A distribution table:
    p50, p90, p99, 均值 at NUM_W and 有值 / 请求 wide enough for its text (hidden unless SHOW_HAS_VALUE); the room comes from
    the name column (down to its longest name), then the share gauge (down to DIST_GAUGE_MIN), then the four number
    columns (down to NUM_MIN). A single-request table keeps its widths, the name column as wide as its longest name."""
    fc = panel["fieldConfig"]
    fc["defaults"].setdefault("mappings", []).append({"type": "value", "options": {str(HDR): {"text": " ", "color": "transparent", "index": 0}}})
    heads = [r.label for r in rows if r.kind == "header"]
    set_props(panel, "阶段", custom__cellOptions={"type": "color-background", "mode": "basic", "applyToRow": True},
              color={"mode": "fixed", "fixedColor": "transparent"},
              mappings=[{"type": "value", "options": {x: {"color": GROUP_BG, "index": i} for i, x in enumerate(heads)}}])
    need = max(text_w(r.label) for r in rows)
    if not dist:
        set_props(panel, "阶段", custom__width=max(need, width_of(panel, "阶段")))
        return
    nums = ("p50", "p90", "p99", "均值")
    for c in nums:
        set_props(panel, c, custom__width=NUM_W)
    if SHOW_HAS_VALUE:
        set_props(panel, HAS_VALUE, custom__width=HAS_VALUE_W, custom__align="right", custom__hidden=False)
    else:
        set_props(panel, HAS_VALUE, custom__hidden=True)
    set_props(panel, "阶段", custom__width=max(need, width_of(panel, "阶段")))
    shown = ("阶段", "均值占比") + nums + ((HAS_VALUE,) if SHOW_HAS_VALUE else ())
    over = sum(width_of(panel, c) or 0 for c in shown) - TABLE_W
    over = take(panel, over, "阶段", need)
    over = take(panel, over, "均值占比", DIST_GAUGE_MIN)
    if over > 0:
        cut = -(-over // len(nums))
        for c in nums:
            set_props(panel, c, custom__width=max(NUM_MIN, NUM_W - cut))
        over -= min(cut, NUM_W - NUM_MIN) * len(nums)
    if over > 0:
        print("注意：%s 比半宽面板宽 %d 像素，右边要横向滚动" % (panel.get("title"), over))


def has_value_expr(d, hdr="0"):
    """T55 + T58 + T64 item 8: "k / n"; "k / n，未采 m" when m requests ran below the segment's level; "未采 m" when all did.
    「未采」 is written only here, every number in it a count of requests; the number cells show 「—」 as for no value."""
    return (f"multiIf({hdr} = 1, '{HDR}', ifNull({d}.n_all, 0) = 0 AND ifNull({d}.n_skip, 0) > 0, concat('未采 ', toString({d}.n_skip)), "
            f"ifNull({d}.n_all, 0) = 0, NULL, "
            f"ifNull({d}.n_skip, 0) > 0, concat(toString({d}.n), ' / ', toString({d}.n_all), '，未采 ', toString({d}.n_skip)), "
            f"concat(toString({d}.n), ' / ', toString({d}.n_all)))")


def dist_sql(tree, role):
    rows = rows_of(tree, role)
    labels = "[" + ", ".join("'%s'" % r.label for r in rows) + "]"
    shares = "[" + ", ".join("1" if r.share else "0" for r in rows) + "]"
    return (
        f"WITH {s_window_cte(tree)}\n"
        f"SELECT {labels}[idx] AS \"阶段\", {cell(rows, 'idx', 'if(d.n = 0, NULL, q[1])')} AS p50, {cell(rows, 'idx', 'if(d.n = 0, NULL, q[2])')} AS p90, "
        f"{cell(rows, 'idx', 'if(d.n = 0, NULL, q[3])')} AS p99,\n"
        f"       {cell(rows, 'idx', 'if(d.n = 0, NULL, ' + no_nan('mean') + ')')} AS \"均值\", "
        f"{cell(rows, 'idx', f'if({shares}[idx] = 1 AND d.n > 0, ' + no_nan('ssum / tsum') + ', NULL)')} AS \"均值占比\",\n"
        f"       {has_value_expr('d', hdr_of(rows, 'idx'))} AS \"{HAS_VALUE}\"\n"
        f"FROM (SELECT toUInt8(number + 1) AS idx FROM numbers({len(rows)})) AS i LEFT JOIN (\n  {dist_parts(tree, rows, role)}\n) AS d USING idx\n"
        f"WHERE {arr(r.depth for r in rows)}[idx] <= {LEVEL}\nORDER BY idx"
    )


def request_rows(tree, rows, role):
    """One request's segments as rows; the request row gives the side total and the proxy segments."""
    total = "p_total_ms" if role == "prefill" else "d_total_ms"
    idx = "[" + ", ".join(str(i) for i in range(1, len(rows) + 1)) + "]"
    vals = ", ".join(f"toFloat64({r.expr})" for r in rows)
    steps = "toUInt32(0)" if role == "prefill" else "f.steps_rest"
    where = "request_id = '${request_id}'"
    pc = proxy_cols(tree, role)
    lvq = levels_sql("request_id = '${request_id}'")
    return (
        f"SELECT idx, ms, total_ms, steps, sl FROM (SELECT f.*, s.{total} AS {total}{', ' + pc if pc else ''}, toFloat64({side_total(tree, role)}) AS total_ms, {steps} AS steps, {level_of('dl', role)} AS sl\n"
        f"    FROM {one_anchor(tree)} AS s\n"
        f"    LEFT JOIN ({fold(tree, role, where)}) AS f ON s.request_id = f.request_id\n"
        f"    LEFT JOIN ({lvq}) AS dl ON dl.request_id = s.request_id)\n"
        f"  ARRAY JOIN {idx} AS idx, [{vals}] AS ms"
    )


def detail_ms(rows):
    """T64 item 8: a segment the request's side did not record at its level is empty, its name as always."""
    return f"if(r.sl < {arr(x.lvl for x in rows)}[r.idx], NULL, r.ms)"


def detail_p(tree):
    rows = rows_of(tree, "prefill", with_else=False)
    labels = "[" + ", ".join("'%s'" % r.label for r in rows) + "]"
    shares = "[" + ", ".join("1" if r.share else "0" for r in rows) + "]"
    opts = "[" + ", ".join("1" if r.opt else "0" for r in rows) + "]"
    ms, H = detail_ms(rows), "r.idx"
    return (
        f"WITH {s_window_cte(tree)}\n"
        f"SELECT {labels}[r.idx] AS \"阶段\", {cell(rows, H, ms)} AS \"耗时\", {cell(rows, H, f'if({shares}[r.idx] = 1, {ms} / r.total_ms, NULL)')} AS \"占P合计\", "
        f"{cell(rows, H, no_nan('w.p50'))} AS \"窗口 p50\",\n"
        f"       {cell(rows, H, f'if({opts}[r.idx] = 1 AND r.ms = 0, NULL, ' + no_nan(f'{ms} / nullIf(w.p50, 0)') + ')')} AS \"相对 p50\"\n"
        f"FROM (\n  {request_rows(tree, rows, 'prefill')}\n) AS r\nLEFT JOIN (\n  {dist_parts(tree, rows, 'prefill')}\n) AS w ON w.idx = r.idx\n"
        f"WHERE {arr(x.depth for x in rows)}[r.idx] <= {LEVEL}\nORDER BY r.idx"
    )


def detail_d(tree):
    rows = rows_of(tree, "decode", with_else=False)
    labels = "[" + ", ".join("'%s'" % r.label for r in rows) + "]"
    rests = "[" + ", ".join("1" if r.rest else "0" for r in rows) + "]"
    shares = "[" + ", ".join("1" if r.share else "0" for r in rows) + "]"
    ms, H = detail_ms(rows), "r.idx"
    return (
        f"WITH {s_window_cte(tree)}\n"
        f"SELECT {labels}[r.idx] AS \"阶段\", {cell(rows, H, ms)} AS \"耗时\", {cell(rows, H, f'if({shares}[r.idx] = 1, {ms} / r.total_ms, NULL)')} AS \"占D合计\",\n"
        f"       {cell(rows, H, f'if({rests}[r.idx] = 1 AND r.steps > 0 AND isNotNull({ms}), r.steps, NULL)')} AS \"步数\",\n"
        f"       {cell(rows, H, f'if({rests}[r.idx] = 1 AND r.steps > 0, {ms} / r.steps, NULL)')} AS \"每步均值\",\n"
        f"       {cell(rows, H, f'if({rests}[r.idx] = 0, ' + no_nan('w.p50') + ', NULL)')} AS \"窗口 p50\",\n"
        f"       {cell(rows, H, f'if({rests}[r.idx] = 1, ' + no_nan('w.p50') + ', NULL)')} AS \"窗口每步 p50\",\n"
        f"       {cell(rows, H, no_nan(f'if({rests}[r.idx] = 1, if(r.steps > 0, ({ms} / r.steps) / nullIf(w.p50, 0), NULL), {ms} / nullIf(w.p50, 0))'))} AS \"相对 p50\"\n"
        f"FROM (\n  {request_rows(tree, rows, 'decode')}\n) AS r\nLEFT JOIN (\n  {dist_parts(tree, rows, 'decode')}\n) AS w ON w.idx = r.idx\n"
        f"WHERE {arr(x.depth for x in rows)}[r.idx] <= {LEVEL}\nORDER BY r.idx"
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


def wait_kv(tree):
    """D 等 KV in the Top table: the decode segment start_load_kv, else its sub-segment wait_for_transfer."""
    top = next((n for n in tree.sides["decode"]["ttft"] if n.id == "start_load_kv"), None)
    if top is None:
        return "CAST(NULL AS Nullable(Float64))"
    kid = next((c for c in top.children if c.id == "wait_for_transfer"), None)
    return f"coalesce(d.{top.column}, d.{kid.column})" if kid else f"d.{top.column}"


def top_table(tree):
    return (
        f"WITH {top_cte(tree, TOP_TABLE_N)}\n"
        "SELECT row_number() OVER (ORDER BY t.ord DESC) AS \"#\",\n"
        "       t.request_id AS request_id,\n"
        "       coalesce(s.p_ts, s.d_ts, s.ts) AS \"到达时间\",\n"
        "       concat(if(s.p_instance = '', '—', s.p_instance), ' → ', if(s.d_instance = '', '—', s.d_instance)) AS \"路由 P → D\",\n"
        "       s.prompt_tokens AS \"输入 tokens\", s.cached_tokens / nullIf(s.prompt_tokens, 0) AS \"命中率\", s.output_tokens AS \"输出 tokens\",\n"
        "       s.p_ttft_ms AS \"官方 TTFT P\", s.d_ttft_ms AS \"官方 TTFT D\", s.d_e2e_ms AS \"官方 E2E D\", nullIf(s.tpot_ms, 0) AS TPOT,\n"
        f"       {side_total(tree, 'prefill')} AS \"P 合计\", {wait_kv(tree)} AS \"D 等 KV\", {side_total(tree, 'decode')} AS \"D 合计\",\n"
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
    # T58: one zero-width space per level below L1, for the panel's filter on the 层级 variable (LEVEL_COL_RE)
    return ".".join(node.path[1:]) + "\u200b" * (node.depth - 1)


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

    def span(name, parent, svc, base, off, dur, tags=empty, depth=1):
        # T58: the 7th element is the segment's depth; the spans below the 层级 variable are left out
        return f"tuple('{name}', '{parent}', '{svc}', toFloat64({base}) + toFloat64({off}), toFloat64({dur}), {tags}, toUInt8({depth}))"

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
        spans.append(span(name, parent_name, svc, base, off, filled_expr(node, alias), tags, node.depth))
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
        # T56: comparison rows are not spans of the request; a segment outside the total is drawn where it starts but does
        # not move the next one, as it overlaps the segments beside it.
        if top.compare:
            continue
        lay(top, "d", "decode", "D", "decode", db, off)
        if not top.outside_total:
            off = f"{off}+{filled_expr(top, 'd')}"
    first = "ifNull(s.d_ttft_ms, 0)"
    spans.append(span("D 第一个 token", "decode", "decode", db, first, "0"))
    spans.append(span("D 第二步起", "decode", "decode", db, first, f"ifNull(s.d_total_ms, 0) - {first}", "[map('key', 'steps', 'value', toString(d.steps_rest))]"))
    for top in d["tpot"]:
        if top.compare:
            continue
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
        f"WHERE (x.5 > 0 OR x.1 = 'D 第一个 token') AND isNotNull(x.4) AND x.7 <= {LEVEL}\nORDER BY startTime, x.5 DESC"
    )


def events_sql():
    """T62: the request's document as the fetcher wrote it (the largest doc_version), one row per top-level field."""
    return ("SELECT kv.1 AS \"字段\", kv.2 AS \"内容\" FROM vllm_pd.request_json(id = '${request_id}')\n"
            "ARRAY JOIN JSONExtractKeysAndValuesRaw(doc) AS kv")


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


def build_board(tree, stale_red=60):
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
        f"SELECT DISTINCT name FROM (SELECT arrayJoin([route_p, route_d]) AS name FROM {DOC} WHERE $__timeFilter(ts)) WHERE name != '' "
        "ORDER BY startsWith(name, 'D'), toUInt32OrZero(substring(name, 2))"
    )
    tl = board["templating"]["list"]
    if not any(v["name"] == "level" for v in tl):
        # T58: 层级 (1, 2, 3), default 3 (the user's choice): how deep the distribution tables, the single-request page and
        # the Top-20 detail tables go
        at_v = next((i + 1 for i, v in enumerate(tl) if v["name"] == "instance"), len(tl))
        tl.insert(at_v, {"name": "level", "label": "层级", "type": "custom", "query": "1,2,3", "hide": 0, "multi": False, "includeAll": False,
                         "current": {"selected": True, "text": "3", "value": "3"},
                         "options": [{"selected": v == "3", "text": v, "value": v} for v in ("1", "2", "3")]})
    for v in board["templating"]["list"]:
        if v["name"] == "model":
            v["query"] = f"SELECT DISTINCT model FROM {DOC} WHERE $__timeFilter(ts) ORDER BY model"
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
        style_segment_table(get(key), rows_of(tree, role), True)
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
        panel["transformations"] = [x for x in panel.get("transformations", []) if x.get("id") != "filterFieldsByName"] + [
            {"id": "filterFieldsByName", "options": {"include": {"pattern": LEVEL_COL_RE}}}]
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
    style_segment_table(get("detail_p"), rows_of(tree, "prefill", with_else=False), False)
    style_segment_table(get("detail_d"), rows_of(tree, "decode", with_else=False), False)
    sql("events", events_sql(), title="请求文档")
    style_doc_panel(get("events"))
    rows_p, rows_d = len(rows_of(tree, "prefill")), len(rows_of(tree, "decode"))
    fit_height(by_key, ["dist_p", "dist_d"], max(rows_p, rows_d))
    rows_p, rows_d = len(rows_of(tree, "prefill", with_else=False)), len(rows_of(tree, "decode", with_else=False))
    fit_height(by_key, ["detail_p", "detail_d"], max(rows_p, rows_d))
    add_status_row(panels, stale_red)
    for p_ in panels:
        p_.pop("key", None)
    return board


def style_doc_panel(panel):
    """The document panel: field name, then its value as JSON (a nested value opens in the cell)."""
    d = panel["fieldConfig"]["defaults"]
    d["custom"]["cellOptions"] = {"type": "auto"}
    d["custom"]["inspect"] = True
    panel["fieldConfig"]["overrides"] = [
        {"matcher": {"id": "byName", "options": "字段"}, "properties": [{"id": "custom.width", "value": 170}]},
        {"matcher": {"id": "byName", "options": "内容"}, "properties": [{"id": "custom.cellOptions", "value": {"type": "json-view"}}]}]


STATUS_H = 5


def stat_age_sql():
    """Seconds since the newest document's ts in request_doc (the last two days first, so only their parts are read)."""
    recent = f"(SELECT max(ts) FROM {DOC} WHERE ts >= now() - INTERVAL 2 DAY)"
    every = f"(SELECT max(ts) FROM {DOC})"
    return (f"SELECT dateDiff('second', ifNull(nullIf({recent}, toDateTime64(0, 3, 'UTC')), {every}), now64(3, 'UTC')) AS \"最近文档距今秒数\"")


def seg_changes_sql():
    """The segment definition changes the fetcher reported in the window (status_doc, scope fetcher)."""
    return (
        "SELECT DISTINCT parseDateTime64BestEffort(JSONExtractString(c, 'ts'), 3, 'UTC') AS \"时间\",\n"
        "       JSONExtractString(c, 'from') AS \"原段定义版本\", JSONExtractString(c, 'to') AS \"新段定义版本\",\n"
        "       arrayStringConcat(JSONExtract(c, 'added', 'Array(String)'), '，') AS \"新增段\",\n"
        "       arrayStringConcat(JSONExtract(c, 'removed', 'Array(String)'), '，') AS \"删去段\"\n"
        f"FROM {STATUS} ARRAY JOIN JSONExtractArrayRaw(doc, 'segment_changes_recent') AS c\n"
        "WHERE $__timeFilter(ts) AND scope = 'fetcher'\nORDER BY \"时间\" DESC LIMIT 20"
    )


def add_status_row(panels, stale_red):
    """The status row at the top: seconds since the newest document (red above STALE_RED_SECONDS) and the segment
    definition changes of the window. Everything else moves down."""
    for x in panels:
        x["gridPos"]["y"] += 1 + STATUS_H
    next_id = max(x["id"] for x in panels) + 1
    ds = {"type": "grafana-clickhouse-datasource", "uid": "${clickhouse}"}

    def target(text):
        return [{"refId": "A", "rawSql": text, "editorType": "sql", "format": 1, "queryType": "table", "pluginVersion": "4.23.4"}]
    row = {"type": "row", "id": next_id, "title": "采集状态", "collapsed": False, "gridPos": {"h": 1, "w": 24, "x": 0, "y": 0}, "panels": []}
    age = {"type": "stat", "id": next_id + 1, "title": "最近文档距今秒数", "datasource": ds, "targets": target(stat_age_sql()),
           "fieldConfig": {"defaults": {"unit": "s", "decimals": 0, "color": {"mode": "thresholds"}, "noValue": "—",
                                        "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None},
                                                                                     {"color": "red", "value": stale_red}]}},
                           "overrides": []},
           "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "orientation": "auto",
                       "textMode": "value", "colorMode": "background", "graphMode": "none", "justifyMode": "auto",
                       "wideLayout": True, "showPercentChange": False},
           "gridPos": {"h": STATUS_H, "w": 4, "x": 0, "y": 1}}
    seg = {"type": "table", "id": next_id + 2, "title": "段定义变化", "datasource": ds, "targets": target(seg_changes_sql()),
           "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}, "inspect": True},
                                        "noValue": "无"},
                           "overrides": [{"matcher": {"id": "byName", "options": "时间"},
                                          "properties": [{"id": "unit", "value": "time:YYYY-MM-DD HH:mm:ss"}, {"id": "custom.width", "value": 170}]}]},
           "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False, "reducer": ["sum"], "countRows": False, "fields": ""}},
           "gridPos": {"h": STATUS_H, "w": 20, "x": 4, "y": 1}}
    panels[0:0] = [row, age, seg]


def main():
    import argparse
    ap = argparse.ArgumentParser(description="按 segment_def 生成面板二看板")
    ap.add_argument("--env", help="grafana.env（默认 GRAFANA_ENV，或包目录下的 grafana.env）")
    ap.add_argument("--contract", help="不连库：用保存下来的段定义端点响应生成（只用于检查）")
    ap.add_argument("--config-version", help="用这一版段定义（默认最近写入 segment_def 的一版）")
    ap.add_argument("--out", default=str(GEN / "vllm-pd-request.json"))
    a = ap.parse_args()
    global SHOW_HAS_VALUE
    env = {}
    if a.contract:
        if a.env or os.environ.get("GRAFANA_ENV"):
            env = grafana_env(a.env)
        with open(a.contract, encoding="utf-8") as f:
            contract = json.load(f)
        tree = Tree(contract_rows(contract), contract["config_version"])
    else:
        env = grafana_env(a.env)
        reader = Reader(env)
        reason = check_interface(reader, env.get("DB_INTERFACE"))
        if reason:
            print("未生成看板，Grafana 中的看板保持原样：" + reason, file=sys.stderr)
            sys.exit(2)
        tree = load_tree(reader, a.config_version)
    SHOW_HAS_VALUE = (env.get("BOARD_HAS_VALUE") or "").strip() == "1"
    stale_red = int(env.get("STALE_RED_SECONDS") or 60)
    board = build_board(tree, stale_red)
    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(board, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("段定义 %s：P %d 段，D %d 段；看板 %d 个 panel，写到 %s" % (
        tree.config_version, len(tree.nodes("prefill", "proxy", "ttft", "background")),
        len(tree.nodes("decode", "proxy", "ttft", "tpot", "background")), sum(1 for p in board["panels"] if p["type"] != "row"), out.name))


if __name__ == "__main__":
    main()
