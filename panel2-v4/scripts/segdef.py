#!/usr/bin/env python3
"""The segment tree of the board generator, built from the rows of vllm_pd.segment_def (interface two): one row per
segment, with its code (path joined by "."), group, side, phase, order, depth, level, label, flags and its column in
request_doc. The generator reads nothing else about the segments; the column names come from segment_def as they are.

Also the reading of grafana.env (the panel side's only configuration) and of ClickHouse with the read-only account:
CLICKHOUSE_ADDR a container name on this machine (docker exec into it) or an IPv4 address (HTTP to CLICKHOUSE_HTTP_PORT).
"""
import base64
import ipaddress
import json
import os
import pathlib
import re
import shlex
import subprocess
import urllib.error
import urllib.parse
import urllib.request

PACK = pathlib.Path(__file__).resolve().parent.parent
PHASES = ("proxy", "ttft", "tpot", "background")
PROCS = ("api", "engine", "worker", "save", "proxy")


class Node:
    def __init__(self, row, role, parent=None):
        self.path = row["code"].split(".")
        self.id = self.path[-1]
        self.role, self.phase, self.parent = role, row["phase"], parent
        self.depth = int(row["depth"])
        self.label = row["label"]
        self.short = row.get("short") or self.id
        self.proc = row.get("proc") or None
        self.optional = bool(int(row.get("optional") or 0))
        self.compare = bool(int(row.get("compare") or 0))
        self.outside_total = bool(int(row.get("outside_total") or 0))
        self.parallel = bool(int(row.get("parallel") or 0))
        self.before_proxy = bool(int(row.get("before_proxy") or 0))
        self.from_level = int(row.get("from_level") or 1)
        # Whether the segment has a source in the cluster's segments.json (a segment without one is empty everywhere).
        self.src = [{}] if int(row.get("has_src") or 0) else None
        self.column = row["column_name"]
        self.children = []

    @property
    def level(self):
        return self.depth

    def procs(self):
        out = {self.proc}
        for c in self.children:
            out |= c.procs()
        return out

    def columns_below(self):
        out = [self.column]
        for c in self.children:
            out += c.columns_below()
        return out

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


class Tree:
    """sides[role] = {name, total_label, proxy, ttft, tpot, background}, as the generator has always read them."""

    def __init__(self, rows, config_version=""):
        if not rows:
            raise SystemExit("segment_def 里没有段定义%s：入库进程还没有从采集进程取到段定义" % ("（%s）" % config_version if config_version else ""))
        self.config_version = config_version
        self.sides = {}
        for role, sk in (("prefill", "p"), ("decode", "d")):
            first = next((r for r in rows if r["side"] == sk), {})
            self.sides[role] = {"name": first.get("side_name") or role, "total_label": first.get("side_total_label") or role,
                                "proxy": [], "ttft": [], "tpot": [], "background": []}
        by_key = {}
        for r in sorted(rows, key=lambda r: int(r["ord"])):
            role = "prefill" if r["side"] == "p" else "decode"
            key = (r["grp"], r["side"], r["code"])
            parent = by_key.get((r["grp"], r["side"], r["parent"])) if r.get("parent") else None
            n = Node(r, role, parent)
            by_key[key] = n
            if parent is None:
                if r["phase"] not in PHASES:
                    raise SystemExit("segment_def：%s 的 phase %s 不认识" % (r["code"], r["phase"]))
                self.sides[role][r["phase"]].append(n)
            else:
                parent.children.append(n)
        self.request_fields = {}
        self.acc_ids = []

    def nodes(self, role, *phases):
        out = []
        for ph in phases:
            for top in self.sides[role][ph]:
                out += list(top.walk())
        return out

    def proxy_sourced(self):
        return [(n.column, None) for role in ("prefill", "decode") for n in self.nodes(role, "proxy") if n.src]

    def columns(self):
        return [n.column for role in ("prefill", "decode") for n in self.nodes(role, *PHASES)]


def contract_rows(contract):
    """segment_def rows from a segment definition endpoint response (what the ingest process writes), for generating
    from a saved response instead of the database."""
    sides = contract.get("sides") or {}
    out = []
    for s in contract["segments"]:
        role = "prefill" if s["side"] == "p" else "decode"
        out.append({"config_version": contract["config_version"], "ord": s["order"], "code": s["code"], "grp": s["group"],
                    "side": s["side"], "phase": s["phase"], "parent": s.get("parent") or "", "depth": s["depth"],
                    "from_level": s["from_level"], "unit": s["unit"], "column_name": s["column"], "doc_path": s["doc_path"],
                    "label": s["label"], "short": s.get("short") or "", "proc": s.get("proc") or "",
                    "optional": int(bool(s.get("optional"))), "compare": int(bool(s.get("compare"))),
                    "outside_total": int(bool(s.get("outside_total"))), "parallel": int(bool(s.get("parallel"))),
                    "before_proxy": int(bool(s.get("before_proxy"))), "has_src": int(bool(s.get("has_src"))),
                    "side_name": (sides.get(role) or {}).get("name", ""), "side_total_label": (sides.get(role) or {}).get("total_label", "")})
    return out


# ---- grafana.env and the database ---------------------------------------------------------------------------------------

def read_env(path):
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if not re.match(r"^[A-Z_][A-Z0-9_]*$", k):
                continue
            try:
                parts = shlex.split(v, comments=True)
            except ValueError:
                parts = [v]
            out[k] = parts[0] if parts else ""
    return out


def grafana_env(path=None):
    """grafana.env of the cluster folder (GRAFANA_ENV, else the pack folder's)."""
    path = path or os.environ.get("GRAFANA_ENV") or str(PACK / "grafana.env")
    if not os.path.exists(path):
        raise SystemExit("没有 %s：先用 bin/panel2_cluster.sh add 写出这个集群的 grafana.env" % path)
    return read_env(path)


class Reader:
    """ClickHouse with the read-only account vllm_reader."""

    def __init__(self, env):
        self.addr = env.get("CLICKHOUSE_ADDR") or ""
        self.port = env.get("CLICKHOUSE_HTTP_PORT") or "8123"
        self.password = env.get("CH_READER_PASSWORD") or ""
        try:
            ipaddress.IPv4Address(self.addr)
            self.http = True
        except ValueError:
            self.http = False
        if not self.addr:
            raise SystemExit("grafana.env 缺 CLICKHOUSE_ADDR")

    def query(self, sql):
        if self.http:
            req = urllib.request.Request("http://%s:%s/" % (self.addr, self.port), data=sql.encode("utf-8"), method="POST",
                                         headers={"Authorization": "Basic " + base64.b64encode(("vllm_reader:" + self.password).encode()).decode()})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return r.read().decode("utf-8")
            except urllib.error.HTTPError as e:
                raise SystemExit("ClickHouse（%s:%s）：%s" % (self.addr, self.port, e.read().decode("utf-8", "replace")[:400])) from None
            except OSError as e:
                raise SystemExit("连不上 ClickHouse %s:%s：%s" % (self.addr, self.port, e)) from None
        r = subprocess.run(["docker", "exec", "-i", self.addr, "clickhouse-client", "--user", "vllm_reader", "--password", self.password,
                            "-q", sql], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if r.returncode:
            raise SystemExit("ClickHouse（容器 %s）：%s" % (self.addr, r.stderr.strip()[-400:]))
        return r.stdout

    def rows(self, sql):
        return [json.loads(x) for x in self.query(sql + " FORMAT JSONEachRow").splitlines() if x.strip()]


def check_interface(reader, want):
    """The version table against grafana.env's DB_INTERFACE (pd_diag_db/v1). Returns a reason, or ''."""
    m = re.match(r"^([a-z_]+)/v(\d+)$", want or "")
    if not m:
        return "grafana.env 的 DB_INTERFACE 应为 <名称>/v<主版本>，现在是 %r" % want
    try:
        got = reader.rows("SELECT name, major FROM vllm_pd.db_interface FINAL")
    except SystemExit as e:
        return "读不了版本表 vllm_pd.db_interface：%s" % e
    names = {(r["name"], int(r["major"])) for r in got}
    if (m.group(1), int(m.group(2))) not in names:
        return "接口二的版本不符：库内版本表为 %s，看板生成器依赖 %s" % (
            "、".join("%s/v%d" % x for x in sorted(names)) or "空", want)
    return ""


def load_tree(reader, config_version=None):
    """The segment tree of one config_version: the one given, else the one written into segment_def last."""
    if not config_version:
        got = reader.rows("SELECT config_version FROM vllm_pd.segment_def GROUP BY config_version ORDER BY max(written_at) DESC LIMIT 1")
        if not got:
            raise SystemExit("segment_def 是空的：入库进程还没有从采集进程取到段定义")
        config_version = got[0]["config_version"]
    cv = config_version.replace("'", "")
    rows = reader.rows("SELECT * FROM vllm_pd.segment_def FINAL WHERE config_version = '%s' ORDER BY ord" % cv)
    return Tree(rows, config_version)
