#!/usr/bin/env python3
"""Tests of the proxy patch against fake P and D servers on this machine (no vLLM, no NPU).

Usage: python run_tests.py <work dir> [--base <file>] [--patched <file>]
       (needs fastapi, httpx and uvicorn; the work dir is created and filled). The two files default to this kit's
       base/ and patched file; --base and --patched test another pair, such as a site's own file and its patched copy.
       --base none: no original file; run 1 and the comparisons with it are skipped (printed as SKIP), the rest runs.

Runs, each with a fresh proxy process:
  1. off-base     the unpatched file (base/), the reference;
  2. off-unset    the patched file, PD_PROXY_DIAG unset;
  3. off-zero     the patched file, PD_PROXY_DIAG=0;
  4. on           the patched file, PD_PROXY_DIAG=1, small files (rotation) and a 1 s self-check;
  5. on-workers2  the patched file, PD_PROXY_DIAG=1, --workers 2.
Runs 1-3 send the same requests; what the client gets and what P and D receive must be the same in all three, after
random request ids are numbered in order of appearance; runs 2-3 must write no file. Run 4 checks every case's line.
Exit status 0 when every check passes; each check prints PASS or FAIL.
"""
import json
import os
import pathlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import time

import argparse
import hashlib

import httpx

HERE = pathlib.Path(__file__).resolve().parent
PATCHED = HERE.parent / "load_balance_proxy_server_example.py"
BASE = HERE.parent / "base" / "load_balance_proxy_server_example.py"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# The line format of the panel-2 node files, as the collector parses it (scripts/render_vector.py).
LINE = re.compile(r'^(?P<ts>[0-9T:.+-]+) \((?P<proc>[^ ]+) pid=(?P<pid>[0-9]+)\) (?P<level>[A-Z]+) \[[^\]]*\] \[PD_DIAG\] (?P<json>\{.*\})$')
FILE = re.compile(r"^proxy\.\d{8}T\d{6}\.(\d+)\.(\d{4})\.log$")
FAILS = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (("  -- " + detail) if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_http(url, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if httpx.get(url, timeout=1).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise SystemExit("not up: " + url)


# The requests. Each case: (name, request options, fake-server behaviour).
CASES = [
    ("stream_ok", dict(stream=True, include_usage=True, client_rid="cli-1"), {}),
    ("nonstream_ok", dict(stream=False, n=2), {}),
    ("completions_ok", dict(path="/v1/completions", stream=True), {}),
    ("client_disconnect", dict(stream=True, disconnect=True), dict(d_chunks=20, d_chunk_ms=100)),
    ("d_error_500", dict(stream=True), dict(d_status=[500])),
    ("d_error_400", dict(stream=True), dict(d_status=[400])),
    ("d_drop_mid_stream", dict(stream=True), dict(d_chunks=5, d_drop_after=1)),
    ("recomputed", dict(stream=True, client_rid="cli-r"), dict(d_recompute=True)),
    ("p_error_500", dict(stream=True), dict(p_status=[500])),
    ("p_retry_then_ok", dict(stream=False), dict(p_status=[503, 200])),
]


def send(port, case, opts, fake):
    path = opts.get("path", "/v1/chat/completions")
    body = {"model": "fake-model", "max_tokens": 8, "stream": opts.get("stream", False), "fake": dict(case=case, **fake)}
    if path.endswith("chat/completions"):
        body["messages"] = [{"role": "user", "content": "hello " + case}]
    else:
        body["prompt"] = "hello " + case
    if opts.get("include_usage") is not None:
        body["stream_options"] = {"include_usage": opts["include_usage"]}
    if opts.get("n") is not None:
        body["n"] = opts["n"]
    headers = {"X-Request-Id": opts["client_rid"]} if opts.get("client_rid") else {}
    with httpx.Client(timeout=30) as c:
        with c.stream("POST", "http://127.0.0.1:%d%s" % (port, path), json=body, headers=headers) as r:
            if opts.get("disconnect"):
                first = next(r.iter_raw())
                return r.status_code, first, True
            return r.status_code, r.read(), False


class Numbering:
    """Random request ids numbered in order of appearance, per case, so two runs compare equal."""

    def __init__(self):
        self.seen = {}

    def __call__(self, text):
        def sub(m):
            return "<id%d>" % self.seen.setdefault(m.group(0), len(self.seen) + 1)
        return UUID.sub(sub, text)


def run(label, script, env_extra, work, workers=1):
    pport, dport, xport = free_port(), free_port(), free_port()
    log = work / (label + ".fake.jsonl")
    procs = []
    for role, port in (("P", pport), ("D", dport)):
        procs.append(subprocess.Popen([sys.executable, str(HERE / "fake_pd.py"), str(port)],
                                      env=dict(os.environ, FAKE_ROLE=role, FAKE_LOG=str(log))))
    for port in (pport, dport):
        wait_http("http://127.0.0.1:%d/v1/models" % port)
    env = dict(os.environ, OPENAI_API_KEY="test-key")
    env.pop("PD_PROXY_DIAG", None)
    env.update(env_extra)
    out = open(work / (label + ".proxy.out"), "w")
    proxy = subprocess.Popen([sys.executable, str(script), "--host", "127.0.0.1", "--port", str(xport),
                              "--prefiller-hosts", "127.0.0.1", "--prefiller-ports", str(pport),
                              "--decoder-hosts", "127.0.0.1", "--decoder-ports", str(dport),
                              "--max-retries", "3", "--retry-delay", "0.001", "--workers", str(workers)],
                             env=env, stdout=out, stderr=subprocess.STDOUT, cwd=str(work))
    wait_http("http://127.0.0.1:%d/healthcheck" % xport)
    results = {}
    try:
        cases = CASES if workers == 1 else [("w2_%d" % i, dict(stream=True), {}) for i in range(12)]
        for i, (name, opts, fake) in enumerate(cases):
            results[name] = send(xport, name, opts, fake)
            if name == "client_disconnect":
                time.sleep(1.0)
            if label == "on" and i == 4:
                time.sleep(2.5)    # one self-check line at least
        time.sleep(1.5)
    finally:
        proxy.send_signal(signal.SIGINT)
        try:
            proxy.wait(15)
        except subprocess.TimeoutExpired:
            proxy.kill()
        for p in procs:
            p.terminate()
            p.wait(10)
        out.close()
    seen = [json.loads(x) for x in log.read_text().splitlines()]
    return results, seen, dict(p=pport, d=dport, x=xport)


def received_by_case(seen):
    by = {}
    for row in seen:
        case = (row["body"].get("fake") or {}).get("case")
        by.setdefault(case, []).append(row)
    return by


def normalised_view(results, seen):
    """What the client got and what P and D received, per case, with random ids numbered."""
    by = received_by_case(seen)
    view = {}
    for name, (code, content, partial) in results.items():
        num = Numbering()
        rows = []
        for r in by.get(name, []):
            b = dict(r["body"])
            rows.append(num(json.dumps([r["role"], r["path"], r["x_request_id"], r["authorization"], b], sort_keys=True)))
        view[name] = {"status": code, "content": None if partial else num(content.decode("utf-8", "replace")), "received": rows}
    return view


def read_proxy_files(d):
    files = {}
    if not d.exists():
        return files
    for p in sorted(d.rglob("*")):
        if p.is_file():
            files[p] = p.read_text().splitlines()
    return files


def unit_checks(work):
    """The writer on its own: old files are deleted, other files are left alone; a full queue drops and counts."""
    import importlib.util
    import threading

    d = work / "logs-unit"
    os.environ["PD_PROXY_DIAG_DIR"] = str(d)
    os.environ["PD_PROXY_DIAG_KEEP_HOURS"] = "1"
    os.environ["PD_PROXY_DIAG_QUEUE"] = "1"
    spec = importlib.util.spec_from_file_location("patched_proxy", PATCHED)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    w = mod._PdWriter(9999)
    inst = d / "proxy-9999"
    inst.mkdir(parents=True, exist_ok=True)
    old = inst / "proxy.20260101T000000.1.0000.log"
    keep = inst / "proxy.20260101T000000.1.0001.log"
    other = inst / "notes.log"
    for p in (old, keep, other):
        p.write_text("x\n")
    two_hours_ago = time.time() - 7200
    os.utime(old, (two_hours_ago, two_hours_ago))
    os.utime(other, (two_hours_ago, two_hours_ago))
    w._clean_old()
    check("unit: a file older than keep_hours is deleted", not old.exists())
    check("unit: a recent file and a file of another name are kept", keep.exists() and other.exists())

    # Another worker's file: deleted only when its pid has stopped or it was last written before this process
    # started. The writer is made to look 20 h old; all files below are older than keep_hours.
    live = subprocess.Popen(["sleep", "60"])
    dead = subprocess.Popen(["true"])
    dead.wait()
    w.started = time.time() - 20 * 3600
    after_start = time.time() - 3 * 3600
    before_start = time.time() - 21 * 3600
    sibling = inst / f"proxy.20260101T000000.{live.pid}.0000.log"
    stopped = inst / f"proxy.20260101T000000.{dead.pid}.0000.log"
    stale = inst / f"proxy.20260101T000000.{live.pid}.0001.log"
    own = inst / f"proxy.20260101T000000.{w.pid}.0000.log"
    for p, t in ((sibling, after_start), (stopped, after_start), (stale, before_start), (own, after_start)):
        p.write_text("x\n")
        os.utime(p, (t, t))
    w._clean_old()
    live.kill()
    live.wait()
    check("unit: an old file of a live worker written after this process started is kept", sibling.exists())
    check("unit: an old file whose pid has stopped is deleted", not stopped.exists())
    check("unit: an old file written before this process started is deleted", not stale.exists())
    check("unit: an old file of this process (not the current one) is deleted", not own.exists())
    with w.lock:     # the writer thread cannot write while this is held
        end = time.time() + 2
        while w.counts["queue_full"] == 0 and time.time() < end:
            w.put({"event": "pd_proxy_request"})
    check("unit: a full queue drops the record and counts it", w.counts["queue_full"] >= 1, str(w.counts))
    check("unit: the writer thread is named pd-proxy-diag",
          any(t.name == "pd-proxy-diag" for t in threading.enumerate()))


def main():
    global BASE, PATCHED
    ap = argparse.ArgumentParser()
    ap.add_argument("work")
    ap.add_argument("--base")
    ap.add_argument("--patched")
    a = ap.parse_args()
    BASE = None if a.base == "none" else pathlib.Path(a.base).resolve() if a.base else BASE
    PATCHED = pathlib.Path(a.patched).resolve() if a.patched else PATCHED
    for label, f in (("base", BASE), ("patched", PATCHED)):
        if f is None:
            print("base=none (no original file: run 1 and the comparisons with it are skipped)", flush=True)
            continue
        print("%s=%s sha256=%s" % (label, f, hashlib.sha256(f.read_bytes()).hexdigest()), flush=True)
    work = pathlib.Path(a.work).resolve()
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    # ---- off: three runs, same requests ----
    views = {}
    for label, script, extra in ((("off-base", BASE, {}),) if BASE is not None else ()) + (
                                 ("off-unset", PATCHED, {"PD_PROXY_DIAG_DIR": str(work / "logs-off-unset")}),
                                 ("off-zero", PATCHED, {"PD_PROXY_DIAG": "0", "PD_PROXY_DIAG_DIR": str(work / "logs-off-zero")})):
        res, seen, _ = run(label, script, extra, work)
        views[label] = normalised_view(res, seen)
        (work / (label + ".view.json")).write_text(json.dumps(views[label], indent=1, ensure_ascii=False))
    for label in ("off-unset", "off-zero"):
        if BASE is None:
            print("SKIP %s: same as unpatched (no original file)" % label)
        for name in views.get("off-base", {}):
            a, b = views["off-base"][name], views[label][name]
            check("%s %s: same as unpatched (client status, body; P/D headers and bodies)" % (label, name), a == b,
                  json.dumps({"base": a, label: b}, ensure_ascii=False)[:600])
        check("%s: no proxy file written" % label, not (work / ("logs-" + label)).exists())

    # ---- on ----
    logdir = work / "logs-on"
    res, seen, ports = run("on", PATCHED, {"PD_PROXY_DIAG": "1", "PD_PROXY_DIAG_DIR": str(logdir),
                                           "PD_PROXY_DIAG_ROTATE_MIB": "0.002", "PD_PROXY_DIAG_SELFCHECK_S": "1"}, work)
    files = read_proxy_files(logdir)
    records, selfchecks, heads, bad_lines = [], [], [], []
    names_ok = True
    for path, lines in files.items():
        m = FILE.match(path.name)
        names_ok &= bool(m) and path.parent.name == "proxy-%d" % ports["x"]
        for k, line in enumerate(lines):
            lm = LINE.match(line)
            if not lm:
                bad_lines.append(line[:120])
                continue
            obj = json.loads(lm.group("json"))
            if k == 0:
                heads.append((path.name, obj))
            if obj["event"] == "pd_proxy_request":
                records.append(obj)
            elif obj["event"] == "pd_proxy_selfcheck":
                selfchecks.append(obj)
    check("on: file names <dir>/proxy-<port>/proxy.<start>.<pid>.<seq>.log", names_ok and len(files) > 0, str(list(files)))
    check("on: every line in the node line format", not bad_lines, str(bad_lines[:3]))
    check("on: first line of each file is pd_file_open naming the file",
          len(heads) == len(files) and all(o["event"] == "pd_file_open" and o["file"] == n for n, o in heads), str(heads[:2]))
    check("on: rotation made several files with seq 0000, 0001, ...",
          len(files) >= 2 and sorted(int(FILE.match(p.name).group(2)) for p in files) == list(range(len(files))), str(list(files)))
    check("on: self-check lines written", len(selfchecks) >= 1 and selfchecks[-1]["queue_full"] == 0 and selfchecks[-1]["errors"] == 0,
          str(selfchecks[-1:]))
    check("on: one line per request", len(records) == len(CASES), "%d records for %d cases" % (len(records), len(CASES)))
    by_rid = {r["request_id"]: r for r in records}
    by_case = received_by_case(seen)
    rec = {}
    for name in res:
        rids = {r["x_request_id"] for r in by_case.get(name, [])}
        rec[name] = by_rid.get(next(iter(rids))) if len(rids) == 1 else None
        check("on %s: P and D got one request id, the line's own" % name, len(rids) == 1 and rec[name] is not None, str(rids))
    for name, r in rec.items():
        if r is None:
            continue
        rows = by_case[name]
        check("on %s: the body's request_id is the proxy's id" % name, all(x["body"].get("request_id") == r["request_id"] for x in rows))
        check("on %s: request id is a fresh UUID" % name, bool(UUID.fullmatch(r["request_id"] or "")))
        t = [r[k] for k in ("t_enter_ns", "t_p_send_ns", "t_p_return_ns", "t_d_send_ns", "t_d_first_ns", "t_end_ns")]
        known = [x for x in t if x is not None]
        check("on %s: times in order" % name, known == sorted(known), str(t))
        if r["ttft_ms"] is not None:
            s = r["proxy_to_p_ms"] + r["p_total_ms"] + r["proxy_to_d_ms"] + r["d_first_ms"]
            check("on %s: ttft = proxy_to_p + p_total + proxy_to_d + d_first" % name, abs(s - r["ttft_ms"]) < 0.01, "%s vs %s" % (s, r["ttft_ms"]))
        if r["d_total_ms"] is not None:
            s = r["proxy_to_p_ms"] + r["p_total_ms"] + r["proxy_to_d_ms"] + r["d_total_ms"]
            check("on %s: e2e = proxy_to_p + p_total + proxy_to_d + d_total" % name, abs(s - r["e2e_ms"]) < 0.01, "%s vs %s" % (s, r["e2e_ms"]))
        check("on %s: api_gateway_ms empty" % name, "api_gateway_ms" in r and r["api_gateway_ms"] is None)

    def has(name, **want):
        r = rec.get(name) or {}
        bad = {k: (r.get(k), v) for k, v in want.items() if r.get(k) != v}
        check("on %s: %s" % (name, ", ".join("%s=%s" % kv for kv in want.items())), not bad, str(bad))

    p, d = "127.0.0.1:%d" % ports["p"], "127.0.0.1:%d" % ports["d"]
    has("stream_ok", status="ok", http_status=200, stream=True, include_usage=True, n=None, client_request_id="cli-1",
        p_addr=p, d_addr=d, retries=0, api="/chat/completions")
    has("nonstream_ok", status="ok", http_status=200, stream=False, n=2, client_request_id=None, retries=0)
    has("completions_ok", status="ok", api="/completions", stream=True)
    has("client_disconnect", status="abort")
    r = rec.get("client_disconnect") or {}
    check("on client_disconnect: first chunk seen, ended well before the stream would have",
          r.get("t_d_first_ns") is not None and (r.get("e2e_ms") or 1e9) < 1500, str({k: r.get(k) for k in ("d_first_ms", "e2e_ms")}))
    has("d_error_500", status="error", http_status=500, d_http_retries=2, retries=2, t_d_first_ns=None)
    check("on d_error_500: client got 500", res["d_error_500"][0] == 500)
    has("d_error_400", status="error", http_status=400, d_http_retries=0, retries=0)
    has("d_drop_mid_stream", status="error")
    has("recomputed", status="ok", recompute_retries=1, retries=1, client_request_id="cli-r", p_addr=p, d_addr=d)
    r = rec.get("recomputed") or {}
    check("on recomputed: retry_ms recorded", isinstance(r.get("retry_ms"), float) and r["retry_ms"] > 0, str(r.get("retry_ms")))
    check("on recomputed: P and D each got two requests with the same id",
          [x["role"] for x in by_case["recomputed"]].count("P") == 2 and [x["role"] for x in by_case["recomputed"]].count("D") == 2)
    has("p_error_500", status="error", http_status=500, p_http_retries=2, t_p_return_ns=None, t_d_send_ns=None)
    has("p_retry_then_ok", status="ok", p_http_retries=1, retries=1)

    # ---- on, two workers ----
    logdir2 = work / "logs-on-workers2"
    res2, seen2, _ = run("on-workers2", PATCHED, {"PD_PROXY_DIAG": "1", "PD_PROXY_DIAG_DIR": str(logdir2)}, work, workers=2)
    recs2, pids = [], set()
    for path, lines in read_proxy_files(logdir2).items():
        pids.add(FILE.match(path.name).group(1))
        for line in lines:
            obj = json.loads(LINE.match(line).group("json"))
            if obj["event"] == "pd_proxy_request":
                recs2.append(obj)
    check("on-workers2: one line per request", len(recs2) == len(res2), "%d lines, %d requests" % (len(recs2), len(res2)))
    check("on-workers2: each worker process writes its own file", len(pids) == len({r["pid"] for r in recs2}) and len(pids) >= 1,
          str(pids))
    print("on-workers2: %d worker processes wrote lines" % len(pids))

    unit_checks(work)
    print("\n%d checks failed" % len(FAILS) if FAILS else "\nall checks passed")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
