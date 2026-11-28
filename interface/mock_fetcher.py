#!/usr/bin/env python3
"""Mock fetcher: serves interface one (pd_diag/v1) from the samples in pd_diag_v1/samples/, so that the ingest process,
ClickHouse and the board can be run without a cluster.

The three endpoints of the specification (pd_diag_v1/接口一_采集接口规范.zh-CN.md, section 2):
  GET /pd_diag/v1/status                          the status endpoint (from samples/status_api.json)
  GET /pd_diag/v1/contract                        the segment definition endpoint (samples/contract.json)
  GET /pd_diag/v1/docs?after=<seq>&max_bytes=<n>  the output queue as NDJSON, with the X-PD-* response headers

The output queue starts with the 11 sample documents (8 request documents, 3 status documents), each renumbered and
stamped with the current time. Then a copy of a request sample (a new request_id) is added every 1/--rate seconds, and
the three status samples every --status-interval seconds, so that the board shows current data. --rate 0 serves the
11 samples only. Sequence numbers start at the start time in microseconds and go up by one per document, as in the
specification (section 3). The documents are the samples as they are: only seq, cluster, ts and, for the copies,
request_id change.

  python3 mock_fetcher.py [--port 9300] [--cluster lab] [--rate 1] [--status-interval 60] [--max-docs 100000]

Standard library only; Python 3.9 and later. Listens on all addresses; read-only; no authentication, as the real one.
"""
import argparse
import copy
import datetime
import json
import os
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(HERE, "pd_diag_v1", "samples")
INTERFACE = "pd_diag/v1"
PREFIX = "/pd_diag/v1/"
ENDPOINTS = ["/pd_diag/v1/docs", "/pd_diag/v1/contract", "/pd_diag/v1/status"]
MAX_BYTES_DEFAULT, MAX_BYTES_LIMIT = 4194304, 67108864
REQUESTS = ["complete", "no_proxy", "incomplete", "evicted", "level1", "level2", "level3", "empty_fields"]
STATUSES = ["instance", "proxy", "fetcher"]


def now_ts():
    """UTC, milliseconds: YYYY-MM-DDTHH:MM:SS.mmmZ (specification section 8)."""
    t = datetime.datetime.now(datetime.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (t.microsecond // 1000)


def load(name):
    with open(os.path.join(SAMPLES, name), encoding="utf-8") as f:
        return json.load(f)


class Queue:
    """The output queue: documents in seq order, as encoded lines."""

    def __init__(self, cluster, max_docs):
        self.cluster, self.max_docs = cluster, max_docs
        self.lock = threading.Lock()
        self.lines = []                         # [(seq, bytes)]
        self.next_seq = int(time.time() * 1e6)
        self.started = now_ts()
        self.closed = {"complete": 0, "no_proxy": 0, "incomplete": 0, "evicted": 0}
        self.docs_written = 0
        self.req = {n: load("request.%s.json" % n) for n in REQUESTS}
        self.status = {n: load("status.%s.json" % n) for n in STATUSES}
        self.contract = load("contract.json")
        self.contract["cluster"] = cluster
        self.api = load("status_api.json")
        self.n_copies = 0

    def append(self, doc):
        with self.lock:
            doc["seq"] = self.next_seq
            doc["cluster"] = self.cluster
            doc["ts"] = now_ts()
            self.next_seq += 1
            if doc.get("schema", "").startswith("vllm_pd.request."):
                self.docs_written += 1
                c = doc.get("closed")
                if c in self.closed:
                    self.closed[c] += 1
            if doc.get("schema", "").startswith("pd_diag.status.") and doc.get("scope") == "fetcher":
                doc["docs_written"] = self.docs_written
                doc["closed"] = dict(self.closed)
                doc["started"] = self.started
                doc["open_requests"] = 0
            self.lines.append((doc["seq"], json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"))
            if len(self.lines) > self.max_docs:
                del self.lines[: len(self.lines) - self.max_docs]

    def add_samples(self):
        for n in REQUESTS:
            self.append(copy.deepcopy(self.req[n]))
        self.add_status()

    def add_copy(self):
        name = REQUESTS[self.n_copies % len(REQUESTS)]
        self.n_copies += 1
        doc = copy.deepcopy(self.req[name])
        doc["request_id"] = str(uuid.uuid4())
        self.append(doc)

    def add_status(self):
        for n in STATUSES:
            self.append(copy.deepcopy(self.status[n]))

    def read(self, after, max_bytes):
        """(lines, first, head, last) for GET docs: documents with seq > after, at least one, at most max_bytes."""
        with self.lock:
            first = self.lines[0][0] if self.lines else after
            head = self.lines[-1][0] if self.lines else after
            out, size, last = [], 0, after
            for seq, raw in self.lines:
                if seq <= after:
                    continue
                if out and size + len(raw) > max_bytes:
                    break
                out.append(raw)
                size += len(raw)
                last = seq
            return out, first, head, last

    def status_api(self):
        with self.lock:
            st = copy.deepcopy(self.api)
            st["cluster"] = self.cluster
            st["config_version"] = self.contract["config_version"]
            st["head_seq"] = self.lines[-1][0] if self.lines else 0
            st["oldest_seq"] = self.lines[0][0] if self.lines else 0
            st["open_requests"] = 0
            for s in st.get("sources") or []:
                s["last_ok_seconds"] = 0.3 if not s.get("gone") else s.get("last_ok_seconds")
                s["behind_seconds"] = 0
            f = st.get("fetcher") or {}
            f.update({"ts": now_ts(), "config_version": st["config_version"], "docs_written": self.docs_written,
                      "closed": dict(self.closed), "open_requests": 0, "started": self.started})
            st["fetcher"] = f
            return st


class Handler(BaseHTTPRequestHandler):
    queue = None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code, obj):
        self.send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = self.queue
        if u.path == PREFIX + "status":
            return self.send_json(200, q.status_api())
        if u.path == PREFIX + "contract":
            return self.send_json(200, q.contract)
        if u.path == PREFIX + "docs":
            params = urllib.parse.parse_qs(u.query)
            try:
                after = int((params.get("after") or ["0"])[0])
                max_bytes = int((params.get("max_bytes") or [str(MAX_BYTES_DEFAULT)])[0])
            except ValueError:
                return self.send_json(400, {"error": "after 和 max_bytes 要是整数"})
            if after < 0 or not 1 <= max_bytes <= MAX_BYTES_LIMIT:
                return self.send_json(400, {"error": "after 要 ≥ 0，max_bytes 要在 1 到 %d 之间" % MAX_BYTES_LIMIT})
            lines, first, head, last = q.read(after, max_bytes)
            return self.send(200, b"".join(lines), "application/x-ndjson", {
                "X-PD-Interface": INTERFACE, "X-PD-Cluster": q.cluster, "X-PD-First-Seq": first,
                "X-PD-Head-Seq": head, "X-PD-Last-Seq": last})
        return self.send_json(404, {"error": "没有这个端点：%s" % u.path, "endpoints": ENDPOINTS})


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser(description="模拟采集进程：按 pd_diag_v1/samples/ 的样例提供接口一的三个端点")
    ap.add_argument("--port", type=int, default=9300, help="监听端口，默认 9300（与采集进程的 listen_port 缺省值相同）")
    ap.add_argument("--cluster", default="lab", help="集群名，写进全部文档和状态接口，默认 lab")
    ap.add_argument("--rate", type=float, default=1.0, help="每秒新增几份请求文档，默认 1；0 表示只提供 11 份样例")
    ap.add_argument("--status-interval", type=float, default=60.0, help="每隔多少秒新增一组状态文档，默认 60")
    ap.add_argument("--max-docs", type=int, default=100000, help="写出队列最多保留多少份文档，默认 100000")
    a = ap.parse_args()
    q = Queue(a.cluster, a.max_docs)
    q.add_samples()
    Handler.queue = q

    def produce():
        next_req = time.monotonic() + (1.0 / a.rate if a.rate > 0 else 0)
        next_st = time.monotonic() + a.status_interval
        while True:
            t = time.monotonic()
            if a.rate > 0 and t >= next_req:
                q.add_copy()
                next_req += 1.0 / a.rate
            if t >= next_st:
                q.add_status()
                next_st += a.status_interval
            time.sleep(0.05)

    threading.Thread(target=produce, daemon=True).start()
    srv = Server(("0.0.0.0", a.port), Handler)
    print("模拟采集进程：集群 %s，端口 %d，接口 %s，已有 %d 份文档，每秒新增 %s 份请求文档" % (
        a.cluster, a.port, INTERFACE, len(q.lines), a.rate), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
