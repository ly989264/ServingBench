#!/usr/bin/env python3
"""The ingest process: reads the documents of one cluster's fetcher through interface one and writes them into this
cluster's ClickHouse (interface two). One process per cluster, on the monitoring machine.

  python3 pd_ingest.py run  --dir <集群目录>      the process (bin/ingest.sh starts it)
  python3 pd_ingest.py init --dir <集群目录>      the tables, the view and the version table (bin/clickhouse_up.sh)
  python3 pd_ingest.py check --dir <集群目录>     read-only: the tables against clickhouse/schema.sql and segment_def
  python3 pd_ingest.py drop-old --dir <集群目录>  drops the 9 tables and views of the earlier version (bin/migrate_deploy_env.sh)

<集群目录> holds ingest.json (the fetcher's address) and clickhouse.env; nothing else is read. The cursor is the largest
seq in request_doc and status_doc, so no state is kept in files.

  1. At start, and while it fails: GET <fetcher_url>/pd_diag/v1/status. The interface must be one this process supports
     (SUPPORTED) and the cluster the same as the cluster of the data already in the tables; else nothing is read, the
     reason is logged and the check is repeated every poll_seconds.
  2. The segment definitions: GET /pd_diag/v1/contract at start and whenever a status document (or the status endpoint,
     every minute) gives a config_version not yet in segment_def. The rows go into segment_def under that
     config_version, a column per new segment into request_doc (computed from doc), then every column of segment_def is
     checked to exist.
  3. GET /pd_diag/v1/docs?after=<cursor>&max_bytes=<max_bytes>; the documents by schema into request_doc and status_doc,
     as they came (the columns are computed from doc). A document of a schema this process does not know stops the
     reading: the documents before it are written, the cursor does not move past it, and the reason is logged.
  4. A full response is followed at once by the next request; otherwise poll_seconds of waiting.
  5. A sequence gap (the fetcher's output queue dropped documents before they were read) is counted (ingest_gap).
Failures to reach the fetcher or ClickHouse are logged once per failure streak and retried every poll_seconds.
The status port (clickhouse.env INGEST_STATUS_PORT) serves /metrics for Prometheus and /status as JSON.
Standard library only; Python 3.9 and later.
"""
import argparse
import base64
import ipaddress
import json
import os
import re
import shlex
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.dirname(HERE)
SCHEMA_SQL = os.path.join(PACK, "clickhouse", "schema.sql")
INTERFACE = "pd_diag/v1"
SUPPORTED = {"pd_diag": {1}}                                   # interface one: name -> major versions read
DOC_SCHEMAS = {"vllm_pd.request": ({2}, "request_doc"), "pd_diag.status": ({1}, "status_doc")}
DB_INTERFACE = ("pd_diag_db", 1)                              # interface two, written into the version table
CONTRACT_SCHEMA = "pd_diag.contract.v1"
STATUS_CHECK_S = 60.0
SEQ_RE = re.compile(rb'"seq":(\d+)')
COLUMN_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def log(msg):
    sys.stderr.write("%s pd_ingest: %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg))
    sys.stderr.flush()


def read_env(path):
    """KEY=VALUE lines of a shell file (the values as the shell would read them, without expansion)."""
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


def load_config(d):
    ing_path = os.path.join(d, "ingest.json")
    env_path = os.path.join(d, "clickhouse.env")
    try:
        with open(ing_path, encoding="utf-8") as f:
            ing = json.load(f)
    except (OSError, ValueError) as e:
        raise SystemExit("读不了 %s：%s" % (ing_path, e))
    if not os.path.exists(env_path):
        raise SystemExit("没有 %s" % env_path)
    env = read_env(env_path)
    ing.setdefault("interface", INTERFACE)
    ing.setdefault("poll_seconds", 5)
    ing.setdefault("max_bytes", 4194304)
    if not str(ing.get("fetcher_url", "")).startswith("http://"):
        raise SystemExit("ingest.json 的 fetcher_url 要填采集进程的地址：http://<proxy 机器>:<listen_port>")
    for k in ("CLICKHOUSE_ADDR", "CLICKHOUSE_HTTP_PORT", "CH_WRITER_PASSWORD", "INGEST_STATUS_PORT", "SUMMARY_TTL_DAYS"):
        if not env.get(k):
            raise SystemExit("clickhouse.env 缺 %s" % k)
    return ing, env


def interface_major(name):
    m = re.match(r"^([a-z_]+)/v(\d+)$", str(name or ""))
    return (m.group(1), int(m.group(2))) if m else (None, None)


def schema_major(name):
    m = re.match(r"^(.*)\.v(\d+)$", str(name or ""))
    return (m.group(1), int(m.group(2))) if m else (None, None)


class ClickHouse:
    """ClickHouse's HTTP interface. CLICKHOUSE_ADDR is the container name (this machine: its published port
    CLICKHOUSE_HTTP_PORT on 127.0.0.1) or an IPv4 address (that machine's CLICKHOUSE_HTTP_PORT)."""

    def __init__(self, env, user, password):
        addr = env["CLICKHOUSE_ADDR"]
        try:
            ipaddress.IPv4Address(addr)
            host = addr
        except ValueError:
            host = "127.0.0.1"
        self.url = "http://%s:%s/" % (host, env["CLICKHOUSE_HTTP_PORT"])
        self.auth = "Basic " + base64.b64encode(("%s:%s" % (user, password)).encode()).decode()

    def query(self, sql, data=None, timeout=60):
        if data is None:
            body, params = sql.encode("utf-8"), {}
        else:
            body, params = data, {"query": sql}
        params["date_time_input_format"] = "best_effort"
        req = urllib.request.Request(self.url + "?" + urllib.parse.urlencode(params), data=body, method="POST",
                                     headers={"Authorization": self.auth})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            raise RuntimeError("ClickHouse：%s" % e.read().decode("utf-8", "replace")[:600]) from None

    def rows(self, sql):
        out = self.query(sql + " FORMAT JSONEachRow")
        return [json.loads(x) for x in out.splitlines() if x.strip()]

    def scalar(self, sql):
        return self.query(sql + " FORMAT TSV").strip()


# ---- the fixed tables ---------------------------------------------------------------------------------------------------

def schema_statements(ttl):
    text = open(SCHEMA_SQL, encoding="utf-8").read().replace("{SUMMARY_TTL_DAYS}", str(int(ttl)))
    text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("--"))
    return [s.strip() for s in text.split(";\n") if s.strip().rstrip(";")]


def fixed_columns():
    """{table: [(column, type)]} of the fixed columns, from schema.sql."""
    text = open(SCHEMA_SQL, encoding="utf-8").read()
    out = {}
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS vllm_pd\.(\w+)\s*\((.*?)\n\)\nENGINE", text, re.S):
        cols = []
        for line in m.group(2).splitlines():
            line = line.strip().rstrip(",")
            mm = re.match(r"^(\w+) (.+?)(?: MATERIALIZED .*| DEFAULT .*| CODEC\(.*\))?$", line)
            if mm:
                cols.append((mm.group(1), mm.group(2)))
        out[m.group(1)] = cols
    return out


def init(ch, env):
    """Creates what is missing; changes the retention only when it differs (a TTL change recomputes the table)."""
    ttl = int(env["SUMMARY_TTL_DAYS"])
    stmts = schema_statements(ttl)
    for s in stmts:
        if s.startswith("ALTER TABLE") and "MODIFY TTL" in s:
            continue
        ch.query(s)
    for t in ("request_doc", "status_doc"):
        q = ch.scalar("SELECT create_table_query FROM system.tables WHERE database = 'vllm_pd' AND name = '%s'" % t)
        if "INTERVAL %d DAY" % ttl not in q:
            ch.query("ALTER TABLE vllm_pd.%s MODIFY TTL toDate(ts) + toIntervalDay(%d)" % (t, ttl))
            print("已改 vllm_pd.%s 的保留天数为 %d（会对整张表重算一次 TTL）" % (t, ttl))
    if ch.scalar("SELECT count() FROM vllm_pd.db_interface FINAL WHERE name = '%s'" % DB_INTERFACE[0]) == "0":
        ch.query("INSERT INTO vllm_pd.db_interface (name, major) VALUES ('%s', %d)" % DB_INTERFACE)
    return check_tables(ch)


# The earlier version's tables and views (views first). request_json is not among them: init replaces it.
OLD_OBJECTS = ("phase_line_cur", "request_events", "phase_line", "diag_event", "filter_selfcheck", "filter_file", "filter_other",
               "proxy_line", "proxy_selfcheck")


def drop_old(ch):
    rows = {r["name"]: r for r in ch.rows("SELECT name, engine, total_rows, total_bytes FROM system.tables WHERE database = 'vllm_pd' "
                                          "AND name IN (%s)" % ", ".join("'%s'" % n for n in OLD_OBJECTS))}
    if not rows:
        print("没有旧版的表和视图，什么都没有删")
        return []
    for n in OLD_OBJECTS:
        r = rows.get(n)
        if not r:
            continue
        ch.query("DROP TABLE IF EXISTS vllm_pd.%s SYNC" % n)
        print("已删除 vllm_pd.%s（%s，%s 行，%s 字节）" % (n, r["engine"], r.get("total_rows") or 0, r.get("total_bytes") or 0))
    left = ch.scalar("SELECT count() FROM system.tables WHERE database = 'vllm_pd' AND name IN (%s)" % ", ".join("'%s'" % n for n in OLD_OBJECTS))
    return [] if left == "0" else ["还剩 %s 个旧版的表或视图没有删掉" % left]


def check_tables(ch):
    """The fixed columns against schema.sql, and every column of segment_def in request_doc. Returns the problems."""
    have = {}
    for r in ch.rows("SELECT table, name, type FROM system.columns WHERE database = 'vllm_pd'"):
        have.setdefault(r["table"], {})[r["name"]] = r["type"]
    bad = []
    for t, cols in fixed_columns().items():
        if t not in have:
            bad.append("没有表 vllm_pd.%s" % t)
            continue
        for c, typ in cols:
            if c not in have[t]:
                bad.append("vllm_pd.%s 缺列 %s" % (t, c))
            elif have[t][c].replace(" ", "") != typ.replace(" ", ""):
                bad.append("vllm_pd.%s.%s 的类型是 %s，建表语句是 %s" % (t, c, have[t][c], typ))
    if "segment_def" in have and "request_doc" in have:
        for r in ch.rows("SELECT DISTINCT column_name FROM vllm_pd.segment_def"):
            if r["column_name"] not in have["request_doc"]:
                bad.append("segment_def 的列 %s 在 request_doc 中不存在" % r["column_name"])
    if ch.scalar("SELECT count() FROM system.tables WHERE database = 'vllm_pd' AND name = 'request_json'") == "0":
        bad.append("没有视图 vllm_pd.request_json")
    v = ch.rows("SELECT name, major FROM vllm_pd.db_interface FINAL")
    if [(x["name"], int(x["major"])) for x in v] != [DB_INTERFACE]:
        bad.append("版本表 vllm_pd.db_interface 是 %s，应为 %s/v%d" % (v, DB_INTERFACE[0], DB_INTERFACE[1]))
    return bad


def doc_path_expr(path):
    """JSONExtract of the document path of a segment (group.side.seg[.sub.child...].ms)."""
    keys = path.split(".")
    if not keys or not all(re.match(r"^[a-z0-9_]+$", k) for k in keys):
        raise ValueError("段的文档路径不合法：%s" % path)
    return "JSONExtract(doc, %s, 'Nullable(Float64)')" % ", ".join("'%s'" % k for k in keys)


# ---- the process --------------------------------------------------------------------------------------------------------

class Ingest:
    def __init__(self, d):
        self.dir = d
        self.cfg, self.env = load_config(d)
        self.ch = ClickHouse(self.env, "vllm_writer", self.env["CH_WRITER_PASSWORD"])
        self.base = self.cfg["fetcher_url"].rstrip("/")
        self.poll = float(self.cfg["poll_seconds"])
        self.max_bytes = int(self.cfg["max_bytes"])
        self.lock = threading.Lock()
        self.m = {"docs_request": 0, "docs_status": 0, "ingest_gap": 0, "fetch_errors": 0, "fetch_failures": 0,
                  "ch_errors": 0, "contracts": 0, "columns_added": 0, "cursor": 0, "last_doc_ts": None,
                  "fetcher_log_dropped": None, "blocked": "", "started": time.time()}
        self.cluster = None
        self.known_cv = set()
        self.last_status = 0.0
        self.fail_logged = {}

    # ---- fetcher ------------------------------------------------------------------------------------------------------

    def get(self, path, timeout=30):
        with urllib.request.urlopen(self.base + path, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()

    def _failed(self, what, err):
        if not self.fail_logged.get(what):
            log("%s失败，每 %g 秒重试：%s" % (what, self.poll, err))
            self.fail_logged[what] = True

    def _ok(self, what):
        if self.fail_logged.pop(what, None):
            log("%s已恢复" % what)

    def check_status(self):
        """The fetcher's status endpoint: interface, cluster, config_version. Returns a reason to wait, or ''."""
        try:
            _, _, body = self.get("/pd_diag/v1/status", timeout=10)
            st = json.loads(body)
        except (OSError, ValueError) as e:
            self.m["fetch_errors"] += 1
            self.m["fetch_failures"] += 1
            self._failed("连接采集进程", e)
            return "fetcher"
        self._ok("连接采集进程")
        self.m["fetch_failures"] = 0
        self.last_status = time.monotonic()
        name, major = interface_major(st.get("interface"))
        mine, my_major = interface_major(self.cfg["interface"])
        if name != mine or major not in SUPPORTED.get(name, ()) or major != my_major:
            return "接口版本不符：采集进程提供 %s，入库进程依赖 %s（支持 %s 的主版本 %s）" % (
                st.get("interface"), self.cfg["interface"], mine, sorted(SUPPORTED.get(mine, ())))
        cluster = st.get("cluster")
        if self.cluster is None:
            have = self.ch.scalar("SELECT any(cluster) FROM (SELECT cluster FROM vllm_pd.request_doc LIMIT 1 UNION ALL "
                                  "SELECT cluster FROM vllm_pd.status_doc LIMIT 1)")
            if have and have != cluster:
                return "集群名不符：采集进程是 %s，库中已有数据的集群是 %s；拒绝写入（是否连到了别的集群的采集进程？）" % (cluster, have)
            self.cluster = cluster
        elif cluster != self.cluster:
            return "采集进程的集群名从 %s 变成了 %s；拒绝写入" % (self.cluster, cluster)
        cv = st.get("config_version")
        if cv and cv not in self.known_cv:
            self.sync_contract()
        return ""

    def sync_contract(self):
        _, _, body = self.get("/pd_diag/v1/contract", timeout=30)
        c = json.loads(body)
        if c.get("schema") != CONTRACT_SCHEMA:
            raise RuntimeError("段定义接口的 schema 是 %s，不是 %s" % (c.get("schema"), CONTRACT_SCHEMA))
        cv = c["config_version"]
        if self.ch.scalar("SELECT count() FROM vllm_pd.segment_def WHERE config_version = '%s'" % cv.replace("'", "")) == "0":
            sides = c.get("sides") or {}
            rows = []
            for s in c["segments"]:
                if not COLUMN_RE.match(s["column"]):
                    raise RuntimeError("段定义的列名不合法：%s" % s["column"])
                role = "prefill" if s["side"] == "p" else "decode"
                rows.append(json.dumps({
                    "config_version": cv, "written_at": int(time.time()), "ord": s["order"], "code": s["code"],
                    "grp": s["group"], "side": s["side"], "phase": s["phase"], "parent": s.get("parent") or "",
                    "depth": s["depth"], "from_level": s["from_level"], "unit": s["unit"], "column_name": s["column"],
                    "doc_path": s["doc_path"], "label": s["label"], "short": s.get("short") or "", "proc": s.get("proc") or "",
                    "optional": int(bool(s.get("optional"))), "compare": int(bool(s.get("compare"))),
                    "outside_total": int(bool(s.get("outside_total"))), "parallel": int(bool(s.get("parallel"))),
                    "before_proxy": int(bool(s.get("before_proxy"))), "has_src": int(bool(s.get("has_src"))),
                    "side_name": (sides.get(role) or {}).get("name", ""), "side_total_label": (sides.get(role) or {}).get("total_label", ""),
                }, ensure_ascii=False))
            have = {r["name"] for r in self.ch.rows("SELECT name FROM system.columns WHERE database = 'vllm_pd' AND table = 'request_doc'")}
            added = []
            for s in c["segments"]:
                if s["column"] not in have:
                    self.ch.query("ALTER TABLE vllm_pd.request_doc ADD COLUMN IF NOT EXISTS %s Nullable(Float64) MATERIALIZED %s"
                                  % (s["column"], doc_path_expr(s["doc_path"])))
                    added.append(s["column"])
                    have.add(s["column"])
            self.ch.query("INSERT INTO vllm_pd.segment_def FORMAT JSONEachRow", data="\n".join(rows).encode("utf-8"))
            self.m["columns_added"] += len(added)
            log("段定义 %s：%d 段写入 segment_def，request_doc 新增 %d 列%s" % (
                cv, len(rows), len(added), "（%s）" % "、".join(added[:8]) + ("等" if len(added) > 8 else "") if added else ""))
            bad = check_tables(self.ch)
            if bad:
                raise RuntimeError("表结构核对没过：" + "；".join(bad[:5]))
        self.m["contracts"] += 1
        self.known_cv.add(cv)

    # ---- one round ----------------------------------------------------------------------------------------------------

    def cursor(self):
        v = self.ch.scalar("SELECT greatest((SELECT max(seq) FROM vllm_pd.request_doc), (SELECT max(seq) FROM vllm_pd.status_doc))")
        return int(v or 0)

    def round(self):
        """One request to the fetcher; True when there is more to read at once."""
        cur = self.cursor()
        self.m["cursor"] = cur
        try:
            _, h, body = self.get("/pd_diag/v1/docs?after=%d&max_bytes=%d" % (cur, self.max_bytes))
        except (OSError, ValueError) as e:
            self.m["fetch_errors"] += 1
            self.m["fetch_failures"] += 1
            self._failed("连接采集进程", e)
            return False
        self._ok("连接采集进程")
        self.m["fetch_failures"] = 0
        lines = [x for x in body.split(b"\n") if x.strip()]
        batch = {"request_doc": [], "status_doc": []}
        expect, stop, new_cv, last_ts, dropped = cur + 1, "", set(), None, None
        for raw in lines:
            m = SEQ_RE.search(raw)
            try:
                doc = json.loads(raw)
                seq = int(doc["seq"])
            except (ValueError, KeyError, TypeError):
                stop = "读不了的文档（序号 %s 之后）：%s" % (expect - 1, raw[:200])
                break
            if seq <= cur:
                continue        # already in the tables (cannot happen while the cursor is the tables' max(seq))
            if seq > expect and cur > 0:
                self.m["ingest_gap"] += seq - expect
                log("序号跳号：%d 之后是 %d，跳过 %d 份（写出队列已删除）" % (expect - 1, seq, seq - expect))
            expect = seq + 1
            base, major = schema_major(doc.get("schema"))
            known = DOC_SCHEMAS.get(base)
            if known is None or major not in known[0]:
                stop = "不认识的文档 schema %s（序号 %d）；支持 %s。游标停在这里，升级入库进程后继续" % (
                    doc.get("schema"), seq, "、".join("%s.v%s" % (b, "/".join(map(str, sorted(v[0])))) for b, v in DOC_SCHEMAS.items()))
                break
            if self.cluster is not None and doc.get("cluster") != self.cluster:
                stop = "文档的集群名 %s 与采集进程的 %s 不同（序号 %d）；拒绝写入" % (doc.get("cluster"), self.cluster, seq)
                break
            batch[known[1]].append(raw)
            if known[1] == "status_doc":
                cv = doc.get("config_version")
                if cv and cv not in self.known_cv and doc.get("scope") == "fetcher":
                    new_cv.add(cv)
                if doc.get("scope") == "fetcher" and isinstance(doc.get("log_dropped"), int):
                    dropped = doc["log_dropped"]
            if isinstance(doc.get("ts"), str) and (last_ts is None or doc["ts"] > last_ts):
                last_ts = doc["ts"]        # same format everywhere (UTC, milliseconds): the largest string is the latest
        if new_cv:
            self.sync_contract()
        for table in ("request_doc", "status_doc"):
            if batch[table]:
                self.ch.query("INSERT INTO vllm_pd.%s (doc) FORMAT JSONAsString" % table, data=b"\n".join(batch[table]) + b"\n")
                self.m["docs_request" if table == "request_doc" else "docs_status"] += len(batch[table])
        with self.lock:
            if last_ts and (self.m["last_doc_ts"] is None or last_ts > self.m["last_doc_ts"]):
                self.m["last_doc_ts"] = last_ts
            if dropped is not None:
                self.m["fetcher_log_dropped"] = dropped
        if stop:
            self.m["blocked"] = stop
            log(stop)
            return False
        try:
            last, head = int(h.get("x-pd-last-seq", "0")), int(h.get("x-pd-head-seq", "0"))
        except ValueError:
            last, head = 0, 0
        return bool(lines) and last < head

    def run(self):
        start_status_server(self, int(self.env["INGEST_STATUS_PORT"]))
        log("开始运行：采集进程 %s，接口 %s，状态端口 %s" % (self.base, self.cfg["interface"], self.env["INGEST_STATUS_PORT"]))
        ready = False
        while True:
            try:
                if not ready or time.monotonic() - self.last_status >= STATUS_CHECK_S:
                    why = self.check_status()
                    if why:
                        if why != "fetcher" and self.m["blocked"] != why:
                            log("不读取文档：" + why)
                        self.m["blocked"] = "" if why == "fetcher" else why
                        ready = False
                        time.sleep(self.poll)
                        continue
                    if not ready:
                        log("核对通过：集群 %s，接口 %s" % (self.cluster, self.cfg["interface"]))
                    ready = True
                    self.m["blocked"] = ""
                more = self.round()
                self._ok("写 ClickHouse")
                if self.m["blocked"]:
                    ready = False
                    time.sleep(self.poll)
                elif not more:
                    time.sleep(self.poll)
            except (RuntimeError, OSError, ValueError) as e:
                self.m["ch_errors"] += 1
                self._failed("写 ClickHouse", e)
                time.sleep(self.poll)

    # ---- the status port ----------------------------------------------------------------------------------------------

    def last_doc_age(self):
        with self.lock:
            ts = self.m["last_doc_ts"]
        if ts:
            try:
                import datetime
                t = datetime.datetime.strptime(ts.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z").timestamp()
                return max(0.0, time.time() - t)
            except ValueError:
                pass
        return time.time() - self.m["started"]

    def metrics(self):
        m = self.m
        out = [
            "# HELP pd_ingest_up 入库进程在运行", "# TYPE pd_ingest_up gauge", "pd_ingest_up 1",
            "# HELP pd_ingest_blocked 因接口版本、集群名或不认识的文档而停止读取", "# TYPE pd_ingest_blocked gauge",
            "pd_ingest_blocked %d" % (1 if m["blocked"] else 0),
            "# HELP pd_ingest_last_doc_age_seconds 最近一份入库文档的时刻距今的秒数", "# TYPE pd_ingest_last_doc_age_seconds gauge",
            "pd_ingest_last_doc_age_seconds %.1f" % self.last_doc_age(),
            "# HELP pd_ingest_fetch_failures 连续连接采集进程失败的次数", "# TYPE pd_ingest_fetch_failures gauge",
            "pd_ingest_fetch_failures %d" % m["fetch_failures"],
            "# TYPE pd_ingest_fetch_errors_total counter", "pd_ingest_fetch_errors_total %d" % m["fetch_errors"],
            "# TYPE pd_ingest_clickhouse_errors_total counter", "pd_ingest_clickhouse_errors_total %d" % m["ch_errors"],
            "# TYPE pd_ingest_docs_total counter",
            'pd_ingest_docs_total{table="request_doc"} %d' % m["docs_request"],
            'pd_ingest_docs_total{table="status_doc"} %d' % m["docs_status"],
            "# HELP pd_ingest_gap_total 序号跳号跳过的文档数", "# TYPE pd_ingest_gap_total counter", "pd_ingest_gap_total %d" % m["ingest_gap"],
            "# TYPE pd_ingest_cursor gauge", "pd_ingest_cursor %d" % m["cursor"],
        ]
        if m["fetcher_log_dropped"] is not None:
            out += ["# HELP pd_fetcher_log_dropped 采集进程状态文档中的 log_dropped", "# TYPE pd_fetcher_log_dropped gauge",
                    "pd_fetcher_log_dropped %d" % m["fetcher_log_dropped"]]
        return "\n".join(out) + "\n"

    def status(self):
        s = {k: v for k, v in self.m.items()}
        s.update({"cluster": self.cluster, "fetcher_url": self.base, "interface": self.cfg["interface"],
                  "last_doc_age_seconds": round(self.last_doc_age(), 1), "config_versions": sorted(self.known_cv)})
        return s


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_status_server(ing, port):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/metrics"):
                body, ctype = ing.metrics().encode(), "text/plain; version=0.0.4; charset=utf-8"
            elif self.path.startswith("/status"):
                body, ctype = (json.dumps(ing.status(), ensure_ascii=False) + "\n").encode(), "application/json; charset=utf-8"
            else:
                body, ctype = b"/metrics /status\n", "text/plain"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    srv = _Server(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    ap = argparse.ArgumentParser(description="T62 入库进程")
    ap.add_argument("action", choices=("run", "init", "check", "drop-old"))
    ap.add_argument("--dir", required=True, help="集群目录：ingest.json 和 clickhouse.env 所在的目录")
    a = ap.parse_args()
    d = os.path.abspath(a.dir)
    if a.action == "run":
        Ingest(d).run()
        return
    env = read_env(os.path.join(d, "clickhouse.env"))
    ch = ClickHouse(env, "default", env.get("CH_ADMIN_PASSWORD", ""))
    try:
        bad = init(ch, env) if a.action == "init" else drop_old(ch) if a.action == "drop-old" else check_tables(ch)
    except (RuntimeError, OSError) as e:
        raise SystemExit("错误：%s" % e)
    for b in bad:
        print("FAIL " + b)
    print("RESULT: %s" % ("OK" if not bad else "FAIL"))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
