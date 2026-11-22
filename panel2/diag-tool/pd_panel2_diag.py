#!/usr/bin/env python3
"""面板二诊断：数据采齐了没有、每一段的值和「有值 / 请求」对不对、看板显示的和库里的是否一致。

在监控机上，用装面板的账号执行（只读，见文末「对现场零影响」）：

    python3 -I -B pd_panel2_diag.py <集群目录> [--from "2026-11-21 15:00"] [--to "2026-11-21 16:00"]

<集群目录> 是面板二这个集群的目录（里面有 deploy.env 和 segments.json）。时间按北京时间写，缺省是最近 1 小时。
屏幕上 40 行以内，最后一行 RESULT；完整明细写进 ~/vllm-pd-diag-reports/<集群>/<时间>.txt。

只用 Python 3 标准库，不 import 面板包的模块、不调用面板包的脚本。读的是集群目录里的 deploy.env、segments.json，
注册表 ~/.vllm-pd-clusters.json（deploy.env 留空的几项从这里取），拉取目录里文件的修改时间。

对现场零影响：ClickHouse 只发 SELECT，经 HTTP 接口、用只读账号 vllm_reader，每条都带 readonly=2、max_execution_time、
max_memory_usage；Grafana 只用 GET。不 SSH、不进容器、不发信号、不改任何服务。只写自己的报告文件。
"""
import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request

TZ = datetime.timezone(datetime.timedelta(hours=8))
SAMPLE_TARGET = 200000          # the board samples the requests of a window above this many; the tool then stops at section 3
LIMITS = {"readonly": "2", "max_execution_time": "60", "max_memory_usage": "1000000000",
          "output_format_json_quote_64bit_integers": "0", "output_format_json_quote_denormals": "1"}
# The board's rules follow its version, told by its rows: a board with an else row (pack v3.4-extra, T64) has P 合计 with
# the proxy segments added (a request without a proxy line counts 0), a share of P/D LB proxy on P, the share as the
# segment's time over the requests that could have it (empty counts 0) divided by their totals, and the else row (per
# request: the side's total minus the first-level segments that take a share, below 0 → 0, empty without the side's
# core_summary[request]). A board without it (v3.3-extra) has P 合计 the API server's own (C5) and the share as the mean
# of the requests with a value divided by the mean total.
ELSE_LABEL = "else"
HDR = -1                        # v3.4-extra: a group's title row holds this in its number cells
STEP_SUFFIX = re.compile(r"（(第一步|第二步起)(，每步)?）$|（每步）$")
DIST_TITLES = {"prefill": "P 侧分段时延分布", "decode": "D 侧分段时延分布"}
REQUEST_PROCS = ("api", "engine", "worker")
STRING_FIELDS = ("status", "finish_reason", "http_status")
TREE_CHARS = "│├└    "
SCREEN, REPORT, COUNTS = [], [], {"OK": 0, "WARN": 0, "FAIL": 0}


def say(level, text, detail=None):
    """One screen line (level OK / WARN / FAIL / INFO, or "" for a plain line); the report gets it and its detail."""
    line = ("%-4s %s" % (level, text)) if level else text
    SCREEN.append(line)
    REPORT.append(line)
    if level in COUNTS:
        COUNTS[level] += 1
    for d in detail or []:
        REPORT.append("     " + d)


def note(text):
    REPORT.append(text)


def ids(xs, n=3):
    xs = sorted(xs)
    return "，".join(xs[:n]) + ("……" if len(xs) > n else "")


# ---- configuration ------------------------------------------------------------------------------------------------

def read_env(path):
    out = {}
    for line in open(path, encoding="utf-8"):
        m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if not m:
            continue
        v = m.group(2).strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        elif " #" in v:
            v = v.split(" #", 1)[0].rstrip()
        out[m.group(1)] = v.replace("${HOME}", os.path.expanduser("~")).replace("$HOME", os.path.expanduser("~"))
    return out


class Conf:
    def __init__(self, cdir):
        self.dir = os.path.abspath(cdir)
        envf = os.path.join(self.dir, "deploy.env")
        if not os.path.isfile(envf):
            raise SystemExit("%s 里没有 deploy.env：这里要写面板二这个集群的目录" % self.dir)
        self.env = read_env(envf)
        self.cluster = self.env.get("CLUSTER", "")
        reg = {}
        rp = os.environ.get("VLLM_PD_REGISTRY") or os.path.expanduser("~/.vllm-pd-clusters.json")
        try:
            entry = json.load(open(rp, encoding="utf-8")).get("clusters", {}).get(self.cluster, {})
            reg = (entry.get("panel2") or {}).get("values") or {}
            self.port_base = entry.get("port_base")
        except (OSError, ValueError):
            self.port_base = None
        self.reg = reg
        if self.env.get("PORT_BASE"):
            self.port_base = int(self.env["PORT_BASE"])

    def get(self, key, derive=None):
        v = self.env.get(key) or str(self.reg.get(key) or "")
        if not v and derive is not None:
            v = derive()
        return v

    def name(self, base):
        return "%s-%s" % (base, self.cluster) if self.cluster else base


# ---- ClickHouse and Grafana, read only -----------------------------------------------------------------------------

class CH:
    def __init__(self, url, user, password):
        self.url, self.auth = url, base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
        self.queries = 0

    def rows(self, sql, fmt="JSONEachRow"):
        head = re.sub(r"^(\s|--[^\n]*\n)*", "", sql)
        if not re.match(r"(?i)(SELECT|WITH)\b", head):
            raise RuntimeError("只发 SELECT：%s" % head[:60])
        req = urllib.request.Request(self.url + "/?" + urllib.parse.urlencode(LIMITS),
                                     data=(sql + "\nFORMAT " + fmt).encode("utf-8"), method="POST",
                                     headers={"Authorization": "Basic " + self.auth})
        self.queries += 1
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                body = r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            raise RuntimeError("ClickHouse：%s" % e.read().decode("utf-8", "replace").strip().splitlines()[0][:300])
        if fmt == "JSON":
            return json.loads(body)
        return [json.loads(x) for x in body.splitlines() if x.strip()]


def grafana_get(conf, path):
    url = conf.get("GRAFANA_URL") or "http://localhost:3000"
    auth = base64.b64encode(("%s:%s" % (conf.get("GRAFANA_USER") or "admin", conf.get("GRAFANA_PASSWORD"))).encode()).decode()
    req = urllib.request.Request(url.rstrip("/") + path, method="GET", headers={"Authorization": "Basic " + auth})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


# ---- segments.json: the segments and the table rows, as the board builds them ----------------------------------------

class Node:
    def __init__(self, raw, role, phase, parent=None):
        self.id, self.role, self.phase, self.parent = raw.get("id", ""), role, phase, parent
        self.depth = 1 if parent is None else parent.depth + 1
        self.path = [self.id] if parent is None else parent.path + [self.id]
        self.proc = "proxy" if phase == "proxy" else (raw.get("proc") or (parent.proc if parent else None))
        self.optional = bool(raw.get("optional"))
        self.label = raw.get("label", self.id)
        self.own_column = raw.get("column")
        self.outside_total = bool(raw.get("outside_total")) or bool(raw.get("compare"))
        self.from_level = raw.get("from_level", parent.from_level if parent is not None else 1)
        self.src = raw.get("src")
        self.children = [Node(c, role, phase, self) for c in raw.get("children", [])]

    @property
    def column(self):
        return self.own_column or "__".join(self.path) + ("_rest" if self.phase == "tpot" else "") + "_ms"

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def tree_label(self):
        if self.depth == 1:
            return self.label
        return ("└ " if self.parent.children[-1] is self else "├ ") + self.label


class Tree:
    def __init__(self, path):
        raw = open(path, "rb").read()
        self.raw = json.loads(raw.decode("utf-8"))
        self.version = "%s@%s" % (self.raw.get("config_id", "?"), hashlib.sha1(raw).hexdigest()[:8])
        self.acc_ids = []
        for a in self.raw.get("accumulate", []) + (self.raw.get("candidate") or {}).get("accumulate", []):
            if a["id"] not in self.acc_ids:
                self.acc_ids.append(a["id"])
        self.request_fields = self.raw.get("request", {})
        self.sides = {}
        for role in ("prefill", "decode"):
            s = self.raw["sides"][role]
            self.sides[role] = {ph: [Node(x, role, ph) for x in s.get(ph, [])] for ph in ("proxy", "ttft", "tpot", "background")}
            self.sides[role]["total_label"] = s["total_label"]

    def nodes(self, role, *phases):
        return [n for ph in phases for top in self.sides[role][ph] for n in top.walk()]

    def reqs(self):
        return [k for k in self.request_fields if k not in STRING_FIELDS]

    def event_cols(self, role):
        out = []
        for n in self.nodes(role, "ttft", "tpot", "background"):
            if any("event" in s and "ranges" not in s for s in n.src or []) and n.column not in out:
                out.append(n.column)
        return out

    def latest(self):
        out = set()
        for role in ("prefill", "decode"):
            for n in self.nodes(role, "ttft", "tpot", "background"):
                if any(s.get("event") == "core_summary" for s in n.src or []):
                    out.add(n.column)
        return out | {k for k, s in self.request_fields.items() if s.get("event") == "core_summary"}

    def ranges(self):
        out = []
        for role in ("prefill", "decode"):
            for n in self.nodes(role, "ttft", "tpot", "background"):
                for s in n.src or []:
                    if "ranges" in s:
                        name = s["ranges"].split(".")[-2] if "." in s["ranges"] else s["ranges"]
                        if name not in out:
                            out.append(name)
        return out

    def proxy_sourced(self):
        out = []
        for role in ("prefill", "decode"):
            for n in self.nodes(role, "proxy"):
                for s in n.src or []:
                    if (n.column, s["proxy"]) not in out:
                        out.append((n.column, s["proxy"]))
        return out

    def rows(self, role, v34=False):
        """The distribution table's rows in order: (label, kind, node, share, opt, rest, from_level)."""
        out = []

        def add(n):
            share = n.phase != "background" and not (n.phase == "proxy" and n.src and not (v34 and role == "prefill")) and not n.outside_total
            out.append({"label": n.tree_label(), "node": n, "share": share, "opt": n.optional, "rest": n.phase == "tpot", "lvl": n.from_level,
                        "depth": n.depth})
            for c in n.children:
                add(c)
        for ph in ("proxy", "ttft", "tpot"):
            for top in self.sides[role][ph]:
                add(top)
        if v34:
            out.append({"label": ELSE_LABEL, "node": None, "else": True, "share": True, "opt": False, "rest": False, "lvl": 1, "depth": 1})
        out.append({"label": self.sides[role]["total_label"], "node": None, "share": True, "opt": False, "rest": False, "lvl": 1, "depth": 1})
        for top in self.sides[role]["background"]:
            add(top)
        for r in out:
            lab = r["label"].replace("设备侧，", "")
            if r["rest"]:
                lab = lab[:-1] + "，每步）" if lab.endswith("）") else lab + "（每步）"
            r["dist"] = lab
        return out


def bare(label):
    """A row's name without the tree lines in front and the step words at the end (the board's groups carry those)."""
    return STEP_SUFFIX.sub("", (label or "").lstrip(TREE_CHARS).strip().replace("设备侧，", ""))


# ---- the per-request values, computed here from the rows (the same definition as the board, not the board's SQL) ----

def f32(x):
    return None if x is None else struct.unpack("f", struct.pack("f", float(x)))[0]


def arg_min(pairs):
    """argMin(value, key) skipping a NULL value or key; pairs: (key, value)."""
    best = None
    for k, v in pairs:
        if v is None or k is None:
            continue
        if best is None or k < best[0]:
            best = (k, v)
    return None if best is None else best[1]


def sum_or_null(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) if xs else None


def fold(tree, role, lines):
    """One dict per request on one side: part → attempt → request, as generate.py fold() describes it."""
    ev, reqs, latest = tree.event_cols(role), tree.reqs(), tree.latest()
    accs, rgs = tree.acc_ids, tree.ranges()
    parts = {}
    for x in lines:
        parts.setdefault((x["request_id"], x["attempt_id"], x["part"]), []).append(x)
    l1 = {}
    for (rid, att, part), xs in parts.items():
        d = {"t0": min(x["t_ms"] for x in xs), "fm": min((x["first_mono_ns"] for x in xs if x["first_mono_ns"] is not None), default=None),
             "fe": arg_min((x["first_mono_ns"], x["first_exec"] or None) for x in xs)}
        for c in ev + reqs:
            if c in latest:
                d[c] = arg_min((-x["core_mono_ns"] if x["core_mono_ns"] is not None else None, x[c]) for x in xs)
            else:
                d[c] = max((x[c] for x in xs if x[c] is not None), default=None)
        for a in accs:
            for k in ("ns", "steps", "first_ns"):
                d["a_%s_%s" % (a, k)] = max((x["acc__%s__%s" % (a, k)] for x in xs if x["acc__%s__%s" % (a, k)] is not None), default=None)
        for g in rgs:
            d["rx_" + g] = next((x["rg__%s__exec" % g] for x in xs if x["rg__%s__exec" % g]), [])
            d["rn_" + g] = next((x["rg__%s__ns" % g] for x in xs if x["rg__%s__ns" % g]), [])
            d["rt_" + g] = max((x["rg__%s__total_ns" % g] for x in xs if x["rg__%s__total_ns" % g] is not None), default=None)
        l1.setdefault((rid, att), []).append(d)
    nodes = tree.nodes(role, "ttft", "tpot", "background")
    rest_cols = {n.column for n in tree.nodes(role, "tpot")}
    first_cols = [n.column for n in tree.nodes(role, "ttft", "background")]
    per_att = {}
    for (rid, att), ds in l1.items():
        b = {"t0": min(d["t0"] for d in ds), "fe": arg_min((d["fm"], d["fe"]) for d in ds)}
        for c in ev + reqs:
            b["e_" + c] = max((d[c] for d in ds if d[c] is not None), default=None)
        for a in accs:
            b["b_%s_ns" % a] = sum_or_null(d["a_%s_ns" % a] for d in ds)
            b["b_%s_steps" % a] = sum_or_null(d["a_%s_steps" % a] for d in ds)
            b["b_%s_first" % a] = arg_min((d["fm"], d["a_%s_first_ns" % a]) for d in ds if d["a_%s_ns" % a] is not None)
        for g in rgs:
            b["rx_" + g] = next((d["rx_" + g] for d in ds if d["rx_" + g]), [])
            b["rn_" + g] = next((d["rn_" + g] for d in ds if d["rn_" + g]), [])
            b["rt_" + g] = max((d["rt_" + g] for d in ds if d["rt_" + g] is not None), default=None)
        v = {"t0": b["t0"]}
        seen = set()
        for n in nodes:
            if n.column not in seen:
                seen.add(n.column)
                v[n.column] = node_value(n, b)
        for c in reqs:
            v[c] = b["e_" + c]
        fw = b.get("b_forward_steps")
        v["steps_rest"] = max(fw or 0, 1) - 1 if role == "decode" and "forward" in accs else 0
        per_att.setdefault(rid, []).append(v)
    out = {}
    for rid, vs in per_att.items():
        r = {}
        for c in first_cols + reqs:
            if c not in rest_cols:
                r[c] = arg_min((v["t0"], v.get(c)) for v in vs)
        for c in rest_cols:
            r[c] = sum_or_null(v.get(c) for v in vs)
        r["steps_rest"] = sum(v["steps_rest"] for v in vs)
        out[rid] = r
    return out


def node_value(n, b):
    srcs = n.src or []
    if not srcs:
        return None
    parts = []
    if any("event" in s and "ranges" not in s for s in srcs):
        parts.append(b.get("e_" + n.column))
    for s in srcs:
        if "ranges" in s:
            g = s["ranges"].split(".")[-2] if "." in s["ranges"] else s["ranges"]
            rx, rn, rt = b["rx_" + g], b["rn_" + g], b["rt_" + g]
            at = rx.index(b["fe"]) if b["fe"] in rx else -1
            if s["part"] == "first":
                parts.append(rn[at] / 1e6 if at >= 0 and at < len(rn) else None)
            else:
                parts.append((rt - rn[at]) / 1e6 if at >= 0 and at < len(rn) and rt is not None else None)
        elif "acc" in s:
            a, p = s["acc"], s.get("part", "total")
            tot, first = b.get("b_%s_ns" % a), b.get("b_%s_first" % a)
            if p == "total":
                parts.append(None if tot is None else tot / 1e6)
            elif p == "first":
                parts.append(None if first is None else first / 1e6)
            else:
                parts.append(None if tot is None or first is None else (tot - first) / 1e6)
    if len(parts) == 1:
        return f32(parts[0])
    if all(x is None for x in parts):
        return None
    return f32(sum(x or 0 for x in parts))


def value_of(n, r):
    """value_expr: the value written directly, else the sum of the children, else empty."""
    own = r.get(n.column)
    if own is not None or not n.children:
        return own
    kids = [value_of(c, r) for c in n.children]
    if all(k is None for k in kids):
        return None
    return sum(k or 0 for k in kids)


def quantile(xs, q):
    """ClickHouse quantileExact: the element at floor(q * n) of the sorted values."""
    return xs[min(int(q * len(xs)), len(xs) - 1)]


# ---- the checks --------------------------------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cluster_dir")
    ap.add_argument("--from", dest="t_from", help="北京时间，比如 2026-11-21 15:00；缺省是 --to 前 1 小时")
    ap.add_argument("--to", dest="t_to", help="北京时间；缺省是现在")
    ap.add_argument("--report-dir", default=os.path.expanduser("~/vllm-pd-diag-reports"))
    a = ap.parse_args()

    def when(s):
        for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.datetime.strptime(s, f).replace(tzinfo=TZ)
            except ValueError:
                pass
        raise SystemExit("时间写成 2026-11-21 15:00 这样（北京时间）：%s" % s)
    now = datetime.datetime.now(TZ).replace(microsecond=0)
    t_to = when(a.t_to) if a.t_to else now
    t_from = when(a.t_from) if a.t_from else t_to - datetime.timedelta(hours=1)
    F, T = int(t_from.timestamp()), int(t_to.timestamp())
    conf = Conf(a.cluster_dir)
    tree = Tree(os.path.join(conf.dir, "segments.json"))
    port = conf.get("CLICKHOUSE_HTTP_PORT", lambda: str(conf.port_base) if conf.port_base else "8123")
    ch = CH("http://127.0.0.1:%s" % port, "vllm_reader", conf.get("CH_READER_PASSWORD"))
    stamp = now.strftime("%Y%m%d-%H%M%S")
    rdir = os.path.join(a.report_dir, conf.cluster or "cluster")
    rfile = os.path.join(rdir, stamp + ".txt")
    try:
        run(conf, tree, ch, F, T, t_from, t_to)
    except Exception as e:  # report what was found so far, then the failure
        say("FAIL", "诊断没有做完：%s" % str(e)[:200])
    os.makedirs(rdir, mode=0o700, exist_ok=True)
    fails, warns = COUNTS["FAIL"], COUNTS["WARN"]
    tail = "RESULT: %s" % ("FAIL（%d 项）" % fails if fails else "OK") + ("，WARN %d 项" % warns if warns else "")
    with open(rfile, "w", encoding="utf-8") as f:
        f.write("\n".join(REPORT + ["", "ClickHouse 查询 %d 条（都是 SELECT）" % ch.queries, tail]) + "\n")
    shown = rfile.replace(os.path.expanduser("~"), "~", 1)
    print("\n".join(SCREEN + ["明细：" + shown, tail]))
    return 1 if fails else 0


def run(conf, tree, ch, F, T, t_from, t_to):
    WIDE = "ts >= toDateTime(%d) - INTERVAL 5 MINUTE AND ts < toDateTime(%d) + INTERVAL 1 HOUR" % (F, T)
    IN = "ts >= toDateTime(%d) AND ts <= toDateTime(%d)" % (F, T)
    bj = lambda ms: datetime.datetime.fromtimestamp(ms / 1000, TZ).strftime("%H:%M:%S") if ms else "—"
    # ---- the window's requests, one row each (the anchor the board starts from)
    api = ch.rows("SELECT request_id, toString(role) AS role, attempt_id, model, toString(instance) AS instance, toUnixTimestamp64Milli(ts) AS t_ms, "
                  "toUnixTimestamp64Milli(api_start_ts) AS st, toUnixTimestamp64Milli(api_end_ts) AS en, api_total_ms, official_ttft_ms "
                  "FROM vllm_pd.phase_line_cur WHERE proc = 'api' AND part = '' AND " + WIDE)
    req = {}
    for x in api:
        r = req.setdefault(x["request_id"], {"t_min": x["t_ms"], "model": x["model"], "p": [], "d": []})
        r["t_min"] = min(r["t_min"], x["t_ms"])
        r["p" if x["role"] == "prefill" else "d"].append(x)
    for rid, r in req.items():
        for s in ("p", "d"):
            xs = r[s]
            r[s + "_inst"] = xs[0]["instance"] if xs else ""
            r[s + "_ts"] = min((x["st"] for x in xs if x["st"] is not None), default=None) or min((x["t_ms"] for x in xs), default=None)
            r[s + "_end"] = max((x["en"] for x in xs if x["en"] is not None), default=None)
            r[s + "_total"] = max((x["api_total_ms"] for x in xs if x["api_total_ms"] is not None), default=None)
        r["d_first"] = max((x["official_ttft_ms"] for x in r["d"] if x["official_ttft_ms"] is not None), default=None)
        r["start"] = r["p_ts"] or r["d_ts"] or r["t_min"]
    win = {rid: r for rid, r in req.items() if F * 1000 <= r["start"] <= T * 1000}
    models = sorted({r["model"] for r in win.values()})
    insts = sorted({x["instance"] for x in api if F * 1000 <= x["t_ms"] <= T * 1000})
    model = models[0] if models else ""
    s = {rid: r for rid, r in win.items() if r["model"] == model and (r["p_inst"] in insts or r["d_inst"] in insts)}
    sc = ch.rows("SELECT toString(instance) AS instance, toString(role) AS role, config_version, diag_level, errors, config_errors, "
                 "toUnixTimestamp64Milli(ts) AS t_ms FROM vllm_pd.filter_selfcheck WHERE ts <= toDateTime(%d) AND ts >= toDateTime(%d) - INTERVAL 1 DAY "
                 "ORDER BY ts DESC LIMIT 1 BY instance, role" % (T, F))
    cids = sorted({x["config_version"] for x in sc})
    say("", "范围：集群 %s，%s 至 %s（北京时间），模型 %s，过滤器版本 %s，请求 %d 个（P 已结束 %d，D 已结束 %d）" % (
        conf.cluster, t_from.strftime("%Y-%m-%d %H:%M:%S"), t_to.strftime("%H:%M:%S"), model or "（没有）", "、".join(cids) or "（没有自检行）",
        len(s), sum(1 for r in s.values() if (r["p_total"] or 0) > 0), sum(1 for r in s.values() if (r["d_total"] or 0) > 0)))
    if len(models) > 1:
        say("INFO", "时间段里有 %d 个模型（%s），按看板的缺省只看 %s" % (len(models), "、".join(models), model))
    collection(conf, tree, ch, sc, IN, F, T, bj)
    if not s:
        say("WARN", "时间段里没有请求：后两节不做。换一个有流量的时间段")
        return
    per_request(conf, tree, ch, s, WIDE, bj)
    board(conf, tree, ch, s, WIDE, F, T, model, insts)


def collection(conf, tree, ch, sc, IN, F, T, bj):
    say("", "== 一、采集")
    rows = ch.rows("SELECT toString(role) AS role, toString(instance) AS instance, toString(proc) AS proc, toString(node) AS node, count() AS n, "
                   "toUnixTimestamp64Milli(max(ts)) AS last FROM vllm_pd.phase_line_cur WHERE %s GROUP BY role, instance, proc, node" % IN)
    for role, tag in (("prefill", "P"), ("decode", "D")):
        xs = [x for x in rows if x["role"] == role]
        inst = sorted({x["instance"] for x in xs})
        procs = {}
        for x in xs:
            procs[x["proc"]] = procs.get(x["proc"], 0) + x["n"]
        last = max((x["last"] for x in xs), default=None)
        for x in sorted(xs, key=lambda y: (y["instance"], y["proc"])):
            note("     %s %s %s（%s）：%d 行，最后一条 %s" % (tag, x["instance"], x["proc"], x["node"], x["n"], bj(x["last"])))
        silent = sorted({x["instance"] for x in sc if x["role"] in REQUEST_PROCS and x["instance"].startswith(tag)} - set(inst))
        level = "WARN" if not xs or silent else "OK"
        say(level, "%s：实例 %d 个，%s，最后一条 %s%s" % (tag, len(inst), "，".join("%s %d 行" % (p, n) for p, n in sorted(procs.items())) or "没有记录",
                                                  bj(last), "；有自检、没有记录的实例：%s" % "、".join(silent) if silent else ""))
    want = tree.version
    bad = [x for x in sc if x["config_version"] != want]
    errs = [x for x in sc if (x["errors"] or 0) > 0 or (x["config_errors"] or 0) > 0]
    lv = sorted({x["diag_level"] if x["diag_level"] is not None else 3 for x in sc})
    for x in sc:
        note("     自检 %s %s：版本 %s，粒度 %s，errors %s，config_errors %s，%s" % (x["instance"], x["role"], x["config_version"], x["diag_level"],
                                                                         x["errors"], x["config_errors"], bj(x["t_ms"])))
    if not sc:
        say("WARN", "过滤器自检：时间段结束前一天里没有自检行")
    else:
        say("WARN" if bad or errs else "OK", "过滤器自检：%d 个进程，%s；粒度 %s；%s" % (
            len(sc), "版本和集群目录的 segments.json（%s）相同" % want if not bad else "%d 个进程的版本不是 %s（%s）" % (len(bad), want, ids({"%s %s" % (x["instance"], x["role"]) for x in bad})),
            "、".join("%d 级" % v for v in lv), "errors 都是 0" if not errs else "%d 个进程 errors 不是 0" % len(errs)))
    if tree.proxy_sourced():
        px = ch.rows("SELECT toString(instance) AS instance, max(queued) - min(queued) AS queued, max(written) - min(written) AS written, "
                     "max(queue_full) - min(queue_full) AS full, max(errors) - min(errors) AS errors, argMax(last_error, ts) AS last_error, count() AS n "
                     "FROM vllm_pd.proxy_selfcheck WHERE %s GROUP BY instance" % IN)
        pl = ch.rows("SELECT count() AS n, toUnixTimestamp64Milli(max(ts)) AS last FROM vllm_pd.proxy_line WHERE %s" % IN)[0]
        bad = [x for x in px if x["full"] or x["errors"]]
        for x in px:
            note("     proxy 自检 %s：%d 行，入队 %d，已写 %d，队列满丢弃 %d，出错 %d，最后的错误 %s" % (x["instance"], x["n"], x["queued"], x["written"], x["full"], x["errors"], x["last_error"] or "—"))
        say("WARN" if bad or not pl["n"] else ("OK" if px else "INFO"), "proxy：时间段里 proxy 行 %d 条，最后一条 %s；%s" % (
            pl["n"], bj(pl["last"]), ("自检 %d 个 proxy，%s" % (len(px), "队列满和出错都是 0" if not bad else "%s 有丢弃或出错" % ids({x["instance"] for x in bad})))
            if px else "时间段里没有 proxy 自检行"))
    pull = conf.get("PULL_DIR", lambda: os.path.join(conf.get("TIMING_HOME", lambda: os.path.join(os.path.expanduser("~"), "vllm-timing")), "pull"))
    if not os.path.isdir(pull):
        say("INFO", "拉取目录 %s 不在，不比文件时间" % pull)
        return
    last_db = {}
    for x in rows:
        last_db[x["node"]] = max(last_db.get(x["node"], 0), x["last"])
    stale, behind, seen = [], [], 0
    for top in sorted(os.listdir(pull)):
        p = os.path.join(pull, top)
        if top.startswith(".") or not os.path.isdir(p) or top in ("_proxy", "_native", "_mooncake"):
            continue
        newest, budget = 0, 20000
        for dp, dn, fn in os.walk(p):
            if dp.count(os.sep) - p.count(os.sep) >= 3:
                dn[:] = []
            for f in fn:
                if f.endswith(".log"):
                    budget -= 1
                    try:
                        newest = max(newest, int(os.stat(os.path.join(dp, f)).st_mtime * 1000))
                    except OSError:
                        pass
            if budget <= 0:
                break
        seen += 1
        db = last_db.get(top)
        note("     拉取目录 %s：最新的文件 %s，库里这台在时间段里最后一条 %s" % (top, bj(newest), bj(db)))
        # A file newer than the window's end with no row of the window from that machine: the collector is behind.
        if newest > T * 1000 + 120000 and db is None and newest and F * 1000 <= newest:
            behind.append(top)
        if T * 1000 > int(datetime.datetime.now(TZ).timestamp() * 1000) - 600000 and newest < T * 1000 - 600000:
            stale.append(top)
    say("WARN" if stale or behind else "OK", "拉取目录：%d 台%s%s" % (
        seen, "；%d 台的文件 10 分钟没有更新（%s）" % (len(stale), ids(stale)) if stale else "",
        "；%d 台有新文件、库里没有这一段的记录（%s）" % (len(behind), ids(behind)) if behind else "" if stale else "，文件时间和库里的记录对得上"))


def per_request(conf, tree, ch, s, WIDE, bj):
    say("", "== 二、每个请求")
    ev = ch.rows("SELECT x, side, event, JSONExtract(raw, 'diag_level', 'Nullable(UInt8)') AS lv FROM vllm_pd.diag_event "
                 "WHERE event IN ('core_summary[request]', 'pool_task[task]', 'pd_transfer_summary[request_generation]') AND x != '' AND " + WIDE)
    kinds = {}
    for e in ev:
        if e["x"] in s:
            kinds.setdefault((e["x"], e["side"]), {}).setdefault(e["event"], []).append(e["lv"])
    s_levels = {}
    for rid in s:
        for side in ("prefill", "decode"):
            k = kinds.get((rid, side), {})
            core = k.get("core_summary[request]")
            s_levels[(rid, side)] = 3 if not core else min((v for v in core if v is not None), default=3)
    for side, tag in (("prefill", "P"), ("decode", "D")):
        done = [rid for rid, r in s.items() if (r["%s_total" % tag.lower()] or 0) > 0]
        miss = [rid for rid in done if "core_summary[request]" not in kinds.get((rid, side), {})]
        note("     %s 已结束 %d 个；缺 core_summary[request] 的：%s" % (tag, len(done), "，".join(sorted(miss)) or "没有"))
        say("WARN" if miss else "OK", "%s 已结束 %d 个，%s" % (tag, len(done), "都有 core_summary[request]" if not miss else
                                                         "缺 core_summary[request] %d 个（看板按 3 级算）：%s" % (len(miss), ids(miss))))
    lv = {}
    for (rid, side), v in s_levels.items():
        if "core_summary[request]" in kinds.get((rid, side), {}):
            lv.setdefault(side, {}).setdefault(v, 0)
            lv[side][v] += 1
    say("INFO", "粒度（按 core_summary[request] 的 diag_level）：%s" % "；".join("%s %s" % (t, "，".join("%d 级 %d 个" % (k, n) for k, n in sorted(lv.get(sd, {}).items())) or "没有")
                                                                       for sd, t in (("prefill", "P"), ("decode", "D"))))
    pool = sum(1 for rid in s if "pool_task[task]" in kinds.get((rid, "prefill"), {}))
    kv = sum(1 for rid in s if "pd_transfer_summary[request_generation]" in kinds.get((rid, "decode"), {}))
    note("     有 pool_task[task]（P 写缓存池）的请求 %d 个；有 pd_transfer_summary[request_generation]（D 读 KV）的 %d 个" % (pool, kv))
    if tree.proxy_sourced():
        px = ch.rows("SELECT request_id, toUnixTimestamp64Milli(min(ts)) AS t_ms FROM vllm_pd.proxy_line WHERE %s GROUP BY request_id" % WIDE)
        have = {x["request_id"]: x["t_ms"] for x in px}
        last = max(have.values(), default=0)
        gap = int(conf.get("PULL_INTERVAL_S") or 10) * 1000
        no = [rid for rid in s if rid not in have]
        # The proxy writes a request's line after D's last byte; one that ended after the newest line in the table may not be in yet.
        late = [rid for rid in no if (s[rid]["d_end"] or s[rid]["p_end"] or 0) > last - gap]
        gone = [rid for rid in no if rid not in late]
        note("     缺 proxy 行、请求在库里最后一条 proxy 行（%s）之前结束的：%s" % (bj(last), "，".join(sorted(gone)) or "没有"))
        note("     缺 proxy 行、请求结束晚于那一条减一轮拉取的：%s" % ("，".join(sorted(late)) or "没有"))
        if not no:
            say("OK", "%d 个请求都有 proxy 行" % len(s))
        if gone:
            say("WARN", "缺 proxy 行 %d 个（请求在库里最后一条 proxy 行之前就结束了）：%s" % (len(gone), ids(gone)))
        if late:
            say("WARN", "proxy 行可能还没到库 %d 个（请求结束晚于库里最后一条 proxy 行 %s 减一轮拉取）：%s" % (len(late), bj(last), ids(late)))
    put = [n for n in tree.nodes("prefill", "background") if n.column == "wait_for_save__put_ms"]
    if put and put[0].children:
        lines = ch.rows("SELECT request_id, max(wait_for_save__put_ms) AS put, " + ", ".join("max(%s) AS %s" % (c.column, c.column) for c in put[0].children) +
                        " FROM vllm_pd.phase_line_cur WHERE role = 'prefill' AND " + WIDE + " GROUP BY request_id")
        got = {x["request_id"]: x for x in lines}
        no_put = [rid for rid in s if s_levels[(rid, "prefill")] >= 3 and got.get(rid, {}).get("put") is not None
                  and all(got.get(rid, {}).get(c.column) is None for c in put[0].children)]
        if no_put:
            say("WARN", "P 3 级、put 有值，但 put 下的 3 段都是空的 %d 个（Mooncake 自己的文件没有进库？）：%s" % (len(no_put), ids(no_put)))
        note("     P 3 级、put 有值、put 下 3 段都空的：%s" % ("，".join(sorted(no_put)) or "没有"))
    return s_levels


def mine(tree, ch, s, WIDE, role, F, T, v34=False):
    """The distribution table, computed here: {row index: dict}."""
    tag = "p" if role == "prefill" else "d"
    ev, reqs, accs, rgs = tree.event_cols(role), tree.reqs(), tree.acc_ids, tree.ranges()
    cols = ["request_id", "attempt_id", "part", "toString(proc) AS proc", "toUnixTimestamp64Milli(ts) AS t_ms", "first_mono_ns", "first_exec", "core_mono_ns"]
    cols += ev + [c for c in reqs if c not in ev]
    cols += ["acc__%s__%s" % (a, k) for a in accs for k in ("ns", "steps", "first_ns")]
    cols += ["rg__%s__%s" % (g, k) for g in rgs for k in ("exec", "ns", "total_ns")]
    lines = ch.rows("SELECT %s FROM vllm_pd.phase_line_cur WHERE role = '%s' AND %s" % (", ".join(cols), role, WIDE))
    lines = [x for x in lines if x["request_id"] in s]
    fr = fold(tree, role, lines)
    lv = ch.rows("SELECT x, side, JSONExtract(raw, 'diag_level', 'Nullable(UInt8)') AS lv FROM vllm_pd.diag_event "
                 "WHERE event = 'core_summary[request]' AND x != '' AND " + WIDE)
    lev = {}
    for e in lv:
        if e["x"] in s and e["side"] == role:
            lev.setdefault(e["x"], []).append(e["lv"])
    proxy_vals = {}
    sourced = tree.proxy_sourced()
    if sourced:
        fields = sorted({f for _, f in sourced})
        for x in ch.rows("SELECT request_id, %s FROM vllm_pd.proxy_line WHERE %s GROUP BY request_id" % (
                ", ".join("max(%s) AS %s" % (f, f) for f in fields), WIDE)):
            proxy_vals[x["request_id"]] = {c: x[f] for c, f in sourced}
    proxy_nodes = tree.sides[role]["proxy"]
    sourced_cols = {c for c, _ in sourced}

    def total(r):
        own = r["%s_total" % tag]
        if not own or own <= 0:
            return None
        extra = sum(proxy_vals.get(r["rid"], {}).get(n.column) or 0 for n in proxy_nodes
                    if v34 or n.column not in sourced_cols) if role == "prefill" else 0
        return extra + own
    for rid, r in s.items():
        r["rid"] = rid
    totals = [total(r) for r in s.values()]
    totals = [t for t in totals if t is not None]
    total_avg = sum(totals) / len(totals) if totals else None
    rows = tree.rows(role, v34)
    l1 = [r["node"] for r in rows if r["node"] is not None and r["depth"] == 1 and r["share"]]
    acc = [{"ms": [], "per": [], "n": 0, "n_all": 0, "n_skip": 0, "skip_lv": None, "ssum": 0.0, "tsum": 0.0} for _ in rows]
    for rid, f in fr.items():
        r = s[rid]
        t = total(r)
        if not t or t <= 0:
            continue
        ls = lev.get(rid)
        sl = 3 if not ls else min((v for v in ls if v is not None), default=3)

        def seg_ms(n):
            if n.phase == "proxy":
                v = proxy_vals.get(rid, {}).get(n.column) if n.column in sourced_cols else None
                return None if v is None else f32(v)
            return value_of(n, f)
        steps = f["steps_rest"] if role == "decode" else 0
        for i, row in enumerate(rows):
            if row["rest"] and steps <= 0:
                if v34 and sl >= row["lvl"] and row["node"] is not None:   # the share counts it as 0, as the board does
                    acc[i]["ssum"] += seg_ms(row["node"]) or 0
                    acc[i]["tsum"] += t
                continue
            n = row["node"]
            if row.get("else"):
                ms = None if not ls else max(0.0, t - sum(seg_ms(x) or 0 for x in l1))
            elif n is None:
                ms = t
            else:
                ms = seg_ms(n)
            a = acc[i]
            if sl >= row["lvl"]:
                a["n_all"] += 1
                a["ssum"] += ms or 0
                a["tsum"] += t
                if ms is not None:
                    a["n"] += 1
            else:
                a["n_skip"] += 1
                a["skip_lv"] = sl if a["skip_lv"] is None else max(a["skip_lv"], sl)
            if ms is not None:
                a["ms"].append(ms)
                a["per"].append(ms / steps if row["rest"] and steps > 0 else ms)
    out = []
    for row, a in zip(rows, acc):
        if row["depth"] > 3:          # the board's level variable at its highest (3) shows rows down to depth 3
            continue
        per = sorted(a["per"])
        q = [quantile(per, x) for x in (0.5, 0.9, 0.99)] if per else [None] * 3
        accum = sum(a["ms"]) / len(a["ms"]) if a["ms"] else None
        mean = sum(per) / len(per) if per else None
        has = a["n"] > 0
        out.append({"label": row["dist"], "p50": q[0] if has else None, "p90": q[1] if has else None, "p99": q[2] if has else None,
                    "mean": mean if has else None,
                    "share": (None if not (row["share"] and has) else (a["ssum"] / a["tsum"] if a["tsum"] else None) if v34
                              else (accum / total_avg if accum is not None and total_avg else None)),
                    "k": a["n"], "n": a["n_all"], "m": a["n_skip"], "skip_lv": a["skip_lv"]})
    return out


def substitute(sql, F, T, model, insts, level="3"):
    sql = re.sub(r"\$__timeFilter\((\w+)\)", lambda m: "%s >= toDateTime(%d) AND %s <= toDateTime(%d)" % (m.group(1), F, m.group(1), T), sql)
    sql = sql.replace("$__fromTime", "toDateTime(%d)" % F).replace("$__toTime", "toDateTime(%d)" % T)
    sql = sql.replace("${instance:singlequote}", ", ".join("'%s'" % i.replace("'", "\\'") for i in insts) or "''")
    sql = sql.replace("${level}", level).replace("$model", model.replace("'", "\\'"))
    left = sorted(set(re.findall(r"\$\{?[A-Za-z_][A-Za-z0-9_:]*\}?", sql)))
    return sql, left


def ints(text):
    return [int(x) for x in re.findall(r"\d+", text or "")]


def close(a, b):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= max(1e-3, 1e-4 * abs(b))


def board(conf, tree, ch, s, WIDE, F, T, model, insts):
    say("", "== 三、各段和看板")
    if len(s) > SAMPLE_TARGET * 3 // 4:
        say("WARN", "时间段里请求 %d 个，看板会抽样，逐行对比不做。把时间段缩短到 %d 个请求以内" % (len(s), SAMPLE_TARGET * 3 // 4))
        return
    uid = conf.get("GRAFANA_BOARD_UID", lambda: conf.name("vllm-pd-request"))
    try:
        b = grafana_get(conf, "/api/dashboards/uid/" + urllib.parse.quote(uid))["dashboard"]
    except Exception as e:
        say("FAIL", "Grafana 取不到看板 %s：%s" % (uid, str(e)[:120]))
        return
    panels = []
    for p in b.get("panels", []):
        panels += [p] + p.get("panels", [])
    for role, tag in (("prefill", "P"), ("decode", "D")):
        title = DIST_TITLES[role]
        p = next((x for x in panels if x.get("title") == title), None)
        if p is None or not (p.get("targets") or [{}])[0].get("rawSql"):
            say("FAIL", "看板 %s 里没有「%s」这张表" % (uid, title))
            continue
        sql, left = substitute(p["targets"][0]["rawSql"], F, T, model, insts)
        if left:
            say("FAIL", "「%s」的查询里有认不出的变量：%s" % (title, "、".join(left)))
            continue
        try:
            got = ch.rows(sql, fmt="JSON")
        except RuntimeError as e:
            say("FAIL", "「%s」的查询在库里执行失败：%s" % (title, str(e)[:160]))
            continue
        names = [c["name"] for c in got["meta"]]
        brows = got["data"]
        hv = next((n for n in names if "有值" in n), None)
        share = next((n for n in names if "占比" in n), None)
        lab = next((n for n in names if n in ("阶段", "项目")), names[0])
        v34 = any(bare(x.get(lab)) == ELSE_LABEL for x in brows)
        exp = mine(tree, ch, s, WIDE, role, F, T, v34)
        bad, extra, missing, lines = compare(brows, exp, lab, hv, share)
        note("     「%s」看板 %d 行，本工具 %d 行；列：%s" % (title, len(brows), len(exp), "、".join(names)))
        # The board takes a request's start from its P side; a request with no P record there starts in 1970 (minIf of an
        # empty set) and drops out of the window. When that alone explains the difference, say so in one line.
        donly = sorted(rid for rid, r in s.items() if not r["p"])
        if (bad or missing) and donly and role == "decode":
            s2 = {rid: r for rid, r in s.items() if r["p"]}
            b2, _, m2, lines2 = compare(brows, mine(tree, ch, s2, WIDE, role, F, T, v34), lab, hv, share)
            if not b2 and not m2:
                REPORT.extend(lines2)
                note("       只有 D 记录的请求（%d 个）：%s" % (len(donly), "，".join(donly)))
                say("FAIL", "%s：看板没有算只有 D 记录的 %d 个请求（看板取请求起点时先看 P 侧，P 侧没有记录就取到 1970 年，落在时间段外）；"
                            "去掉它们以后 %d 行都对得上：%s" % (title, len(donly), len(exp), ids(donly, 2)))
                continue
        REPORT.extend(lines)
        if bad or missing:
            say("FAIL", "%s：%d 行和本工具算的对不上%s：%s" % (title, len(bad) + len(missing), "（另有看板缺的行 %d 个）" % len(missing) if missing else "",
                                                     "；".join("%s（%s）" % (bare(l), d) for l, d in bad[:2]) or "、".join(missing[:3])))
        else:
            say("OK", "%s：%d 行，看板和本工具从库里算的一致（p50、p90、p99、均值、均值占比%s）%s" % (
                title, len(exp) - len(missing), "、有值 / 请求" if hv else "；「有值 / 请求」列隐藏着，不比",
                "；看板另有 %d 行不在段定义里（%s），不比" % (len(extra), ids(extra)) if extra else ""))


def compare(brows, exp, lab, hv, share):
    """Board rows against the tool's, matched by name in order (a group's title row, its number cells HDR, is skipped)."""
    bad, extra, lines, used, i = [], [], [], set(), 0
    for br in brows:
        name = bare(br.get(lab))
        if br.get("p50") == HDR and br.get("均值") == HDR:
            lines.append("       标题行：%s" % name)
            continue
        j = next((k for k in range(i, len(exp)) if k not in used and bare(exp[k]["label"]) == name), None)
        if j is None:
            extra.append(name)
            lines.append("       看板多出的行：%s" % name)
            continue
        used.add(j)
        i = j + 1
        e, diffs = exp[j], []
        for col, key in (("p50", "p50"), ("p90", "p90"), ("p99", "p99"), ("均值", "mean"), (share, "share")):
            if col and col in br and not close(br[col], e[key]):
                diffs.append("%s 看板 %s，本工具 %s" % (col, fmt(br[col]), fmt(e[key])))
        if hv:
            want = [[]] if e["n"] == 0 and e["m"] == 0 else []
            if e["n"] > 0:
                want.append([e["k"], e["n"], e["m"]] if e["m"] else [e["k"], e["n"]])
            if e["n"] == 0 and e["m"] > 0:
                want += [[e["skip_lv"]], [e["m"]]]
            if ints(br.get(hv)) not in want:
                diffs.append("%s 看板「%s」，本工具 k=%d n=%d 未采=%d" % (hv, br.get(hv) or "空", e["k"], e["n"], e["m"]))
        lines.append("       %s %s：%s" % ("≠" if diffs else "=", e["label"], "；".join(diffs) if diffs else "p50 %s，均值 %s，有值 %d / %d，未采 %d" % (
            fmt(e["p50"]), fmt(e["mean"]), e["k"], e["n"], e["m"])))
        if diffs:
            bad.append((e["label"], diffs[0]))
    missing = [bare(exp[k]["label"]) for k in range(len(exp)) if k not in used]
    lines += ["       看板缺的行：%s" % m for m in missing]
    return bad, extra, missing, lines


def fmt(x):
    return "空" if x is None else ("%.4g" % x if isinstance(x, float) else str(x))


if __name__ == "__main__":
    sys.exit(main())
