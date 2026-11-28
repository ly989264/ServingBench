#!/usr/bin/env python3
"""Panel 2 diagnostics on the monitoring machine, for the cluster folder this file sits in. Read-only, except `pair`.

The diagnostics are split by side. This tool checks the monitoring side only: the ingest process (ingest/pd_ingest.py),
ClickHouse and the status documents in vllm_pd.status_doc. The fetcher and its output queue are checked on the cluster
side with pd-diag-fetcher/fetcher_check.py, which calls GET /pd_diag/v1/status on the proxy host.

Run through bin/diag.sh in the cluster folder. Settings: the folder's clickhouse.env, grafana.env and ingest.json, read
without writing anything there; reports go to ~/vllm-pd-diag-reports/<cluster folder name>/.

  bin/diag.sh all | monitor | chain
  bin/diag.sh history "<start>" "<end>"        local time, e.g. "2026-11-13 10:30"
  bin/diag.sh log <file> [--save-profile]
  bin/diag.sh pair <proxy host:port> [--model M] [--count N] [--wait S]
"""
import argparse
import base64
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.dont_write_bytecode = True
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.dirname(SCRIPTS)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
import criteria as K  # noqa: E402
from segdef import read_env  # noqa: E402

REPORT_ROOT = os.path.join(os.path.expanduser("~"), "vllm-pd-diag-reports")
IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
STATUS_LIMIT = 130       # the fetcher writes status documents every 60 s; the ingest process reads every poll_seconds
BEHIND_LIMIT = 150       # incomplete_idle_seconds (120 by default) plus a margin
WINDOW = 600             # the counters are compared with the status document of 10 minutes earlier
GONE_AFTER = 3600        # a source without a status document for an hour is no longer in the fetcher's configuration


# ==================== output ====================
class Report:
    """Summary lines (level, text) for the screen, raw sections for the report file."""

    def __init__(self, name):
        self.name, self.lines, self.raw = name, [], []

    def add(self, level, text):
        self.lines.append((level, text))

    def section(self, title):
        self.lines.append(("", "== " + title))
        self.raw.append("\n==== " + title)

    def keep(self, title, text):
        self.raw.append("---- %s\n%s" % (title, text.rstrip()))

    def counts(self):
        c = {"FAIL": 0, "WARN": 0}
        for lv, _ in self.lines:
            if lv in c:
                c[lv] += 1
        return c

    def write(self, out_dir, args_text):
        os.makedirs(out_dir, mode=0o700, exist_ok=True)
        path = os.path.join(out_dir, "diag-%s-%s.txt" % (self.name, datetime.datetime.now().strftime("%Y%m%d-%H%M%S")))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("bin/diag.sh %s\n%s\n\n" % (args_text, datetime.datetime.now().isoformat(timespec="seconds")))
            f.write("\n".join(fmt(lv, t) for lv, t in self.lines) + "\n")
            f.write("\n".join(self.raw) + "\n")
        return path


def fmt(level, text):
    return text if not level else "%-4s %s" % (level, text)


def worst(levels):
    order = ("OK", "INFO", "WARN", "FAIL")
    return max(levels, key=order.index, default="OK")


# ==================== settings of the cluster ====================
class Cluster:
    """clickhouse.env, grafana.env and ingest.json of the cluster folder. Nothing is written in the folder."""

    def __init__(self, folder=PACK):
        self.dir = os.path.abspath(folder)
        for f in ("clickhouse.env", "grafana.env", "ingest.json"):
            if not os.path.isfile(os.path.join(self.dir, f)):
                raise SystemExit("错误：%s 中没有 %s。bin/diag.sh 应在集群目录（bin/panel2_cluster.sh add 建立的目录）中执行。" % (self.dir, f))
        self.ch_env = read_env(os.path.join(self.dir, "clickhouse.env"))
        self.gf_env = read_env(os.path.join(self.dir, "grafana.env"))
        with open(os.path.join(self.dir, "ingest.json"), encoding="utf-8") as f:
            self.ingest = json.load(f)
        self.name = os.path.basename(self.dir)
        vf = os.path.join(self.dir, "VERSION")
        self.version_file = open(vf).read().strip() if os.path.isfile(vf) else "（没有 VERSION 文件）"

    def get(self, key, default=""):
        """clickhouse.env first: CLICKHOUSE_HTTP_PORT of grafana.env is the port Grafana uses, not this machine's."""
        return self.ch_env.get(key) or self.gf_env.get(key) or default

    def stale_limit(self):
        try:
            return float(self.get("ALERT_STALE_SECONDS", "120"))
        except ValueError:
            return 120.0

    def ingest_url(self, path):
        return "http://127.0.0.1:%s%s" % (self.get("INGEST_STATUS_PORT"), path)


# ==================== access ====================
def run(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, "", "超时（%d 秒）" % timeout
    except OSError as e:
        return 127, "", str(e)


def ch(c, sql, timeout=60):
    """ClickHouse's HTTP interface as vllm_reader (SELECT on vllm_pd only), readonly=1 for every query."""
    addr = c.get("CLICKHOUSE_ADDR")
    host = addr if IPV4.match(addr) else "127.0.0.1"
    req = urllib.request.Request("http://%s:%s/?readonly=1" % (host, c.ch_env.get("CLICKHOUSE_HTTP_PORT")),
                                 data=(sql + " FORMAT JSONEachRow").encode("utf-8"), method="POST",
                                 headers={"X-ClickHouse-User": "vllm_reader", "X-ClickHouse-Key": c.get("CH_READER_PASSWORD")})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return [json.loads(x) for x in r.read().decode("utf-8").splitlines() if x.strip()], None
    except urllib.error.HTTPError as e:
        return None, e.read().decode("utf-8", "replace").strip().splitlines()[0][:300]
    except Exception as e:  # noqa: BLE001
        return None, str(e)[:200]


def http_json(url, timeout=15, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, (json.loads(body) if body.strip()[:1] in (b"{", b"[") else body.decode("utf-8", "replace")), None
    except urllib.error.HTTPError as e:
        return e.code, None, str(e)
    except Exception as e:  # noqa: BLE001
        return None, None, str(e)[:200]


def prom_range(c, query, start, end, step):
    q = urllib.parse.urlencode({"query": query, "start": start, "end": end, "step": step})
    code, res, err = http_json(c.get("PROMETHEUS_URL", "http://localhost:9090").rstrip("/") + "/api/v1/query_range?" + q, timeout=60)
    if code != 200 or not isinstance(res, dict):
        return None, err or "HTTP %s" % code
    return res["data"]["result"], None


def iso_epoch(text):
    """2026-11-13T05:22:33.123Z or …+08:00 -> epoch seconds, or None."""
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|([+-]\d\d):?(\d\d))", text or "")
    if not m:
        return None
    t = datetime.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
    off = 0 if m.group(2) == "Z" else (int(m.group(3)) * 60 + (1 if m.group(3)[0] != "-" else -1) * int(m.group(4))) * 60
    return (t - datetime.datetime(1970, 1, 1)).total_seconds() - off


# ==================== monitor ====================
def df_of(path):
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize
    except OSError:
        return None, None


def data_dirs(c):
    """CLICKHOUSE_DATA_DIR, else the data mounts of the ClickHouse container."""
    if c.get("CLICKHOUSE_DATA_DIR"):
        return [c.get("CLICKHOUSE_DATA_DIR")]
    code, out, _ = run(["docker", "inspect", "--format", "{{json .Mounts}}", c.get("CLICKHOUSE_CONTAINER")])
    try:
        return [m["Source"] for m in json.loads(out or "[]") if m.get("Source") and not m["Source"].endswith((".xml", ".yml", ".yaml", ".sql"))]
    except ValueError:
        return []


def pid_alive(path):
    try:
        pid = int(open(path).read().strip())
        os.kill(pid, 0)
        return pid
    except PermissionError:
        return pid
    except (OSError, ValueError):
        return None


def cmd_monitor(c, r, args):
    r.section("monitor（监控机：容器、磁盘、内存、入库进程、Prometheus、Grafana）")
    for cont in (c.get("CLICKHOUSE_CONTAINER"), c.get("PROMETHEUS_CONTAINER"), c.get("GRAFANA_CONTAINER")):
        if not cont:
            continue
        code, out, _ = run(["docker", "inspect", "--format", "{{.State.Status}}|{{.State.StartedAt}}|{{.RestartCount}}|{{.State.OOMKilled}}", cont])
        if code != 0:
            r.add("FAIL", "容器 %s 不存在" % cont)
            continue
        st, started, restarts, oom = (out.strip().split("|") + ["", "", "", ""])[:4]
        if st != "running":
            r.add("FAIL", "容器 %s 的状态为 %s" % (cont, st))
        elif oom == "true":
            r.add("FAIL", "容器 %s 曾因内存耗尽被终止（OOMKilled）" % cont)
        else:
            t = iso_epoch(started[:19] + "Z") or 0
            r.add("OK" if time.time() - t > 86400 or int(restarts or 0) == 0 else "WARN",
                  "容器 %s 在运行，启动于 %s UTC，重启 %s 次" % (cont, started[:19].replace("T", " "), restarts))
    shown = False
    for d in data_dirs(c):
        free, total = df_of(d)
        if free is None or not total:
            continue
        used = 100 - free * 100.0 / total
        r.add("OK" if used < 80 else ("WARN" if used < 90 else "FAIL"), "ClickHouse 数据盘（%s）：已用 %.0f%%，可用 %s" % (d, used, K.human(free)))
        shown = True
        break
    if not shown:
        r.add("WARN", "读不到 ClickHouse 的数据目录（CLICKHOUSE_DATA_DIR 为空，容器 %s 也没有可读的挂载）" % c.get("CLICKHOUSE_CONTAINER"))
    mem = 0
    for line in open("/proc/meminfo") if os.path.isfile("/proc/meminfo") else []:
        if line.startswith("MemAvailable"):
            mem = int(line.split()[1]) // 1024
    if mem:
        r.add("OK" if mem >= 2048 else "WARN", "监控机可用内存 %d MiB" % mem)
    run_dir = os.path.join(c.dir, "run")
    loop, child = pid_alive(os.path.join(run_dir, "ingest-loop.pid")), pid_alive(os.path.join(run_dir, "ingest.pid"))
    code, st, err = http_json(c.ingest_url("/status"), timeout=10)
    if loop and child and code == 200:
        r.add("OK", "入库进程在运行（进程号 %d，守护循环 %d），状态端口 %s 有应答" % (child, loop, c.get("INGEST_STATUS_PORT")))
    elif code == 200:
        r.add("WARN", "状态端口 %s 有应答，但 %s 中没有在运行的入库进程的进程号文件：入库进程不是由 bin/ingest.sh 启动的" % (c.get("INGEST_STATUS_PORT"), run_dir))
    else:
        r.add("FAIL", "入库进程没有运行或状态端口 %s 无应答（%s）→ bin/ingest.sh start；日志 logs/ingest.log" % (c.get("INGEST_STATUS_PORT"), err or code))
    job = c.get("INGEST_JOB")
    code, res, err = http_json(c.get("PROMETHEUS_URL", "http://localhost:9090").rstrip("/") + "/api/v1/targets?state=active")
    if code != 200 or not isinstance(res, dict):
        r.add("FAIL", "Prometheus 访问不了（%s）" % (err or code))
    else:
        mine = [t for t in res["data"]["activeTargets"] if t["labels"].get("job") == job]
        if not mine:
            r.add("FAIL", "Prometheus 中没有抓取任务 %s → bin/prometheus_job.sh" % job)
        for t in mine:
            up = t.get("health") == "up"
            r.add("OK" if up else "FAIL", "Prometheus 抓取任务 %s（%s）：%s" % (
                job, t["labels"].get("instance"), "up" if up else "%s（%s）→ 核对 clickhouse.env 的 MONITOR_HOST、INGEST_STATUS_PORT" % (
                    t.get("health"), (t.get("lastError") or "")[:80])))
    code, res, err = http_json(c.get("GRAFANA_URL", "http://localhost:3000").rstrip("/") + "/api/health")
    r.add("OK" if code == 200 else "FAIL", "Grafana %s" % ("正常" if code == 200 else "访问不了（%s）" % (err or code)))
    r.add("INFO", "面板二 %s，集群目录 %s" % (c.version_file, c.dir))


# ==================== chain ====================
def num(d, *path):
    for p in path:
        d = d.get(p) if isinstance(d, dict) else None
    return d if isinstance(d, (int, float)) else 0


def delta(cur, old):
    """Increase of a counter; the current value when there is no earlier value or the counter was reset."""
    return cur - old if old is not None and cur >= old else cur


def filter_counts(doc, old_doc):
    """(errors + config_errors, dropped) of the filter processes of an instance, increase per process (role, pid)."""
    old = {(p.get("role"), p.get("pid")): p for p in (old_doc or {}).get("processes") or []}
    err = drop = 0
    for p in doc.get("processes") or []:
        o = old.get((p.get("role"), p.get("pid")))
        err += delta(num(p, "errors") + num(p, "config_errors"), None if o is None else num(o, "errors") + num(o, "config_errors"))
        drop += delta(num(p, "dropped"), None if o is None else num(o, "dropped"))
    return err, drop


def judge_source(doc, old, age, fetcher_cv):
    """[(level, text)] of one instance or proxy worker from its newest status document and the one of 10 minutes earlier."""
    out = []
    o = old or {}
    d = lambda *p: delta(num(doc, *p), num(o, *p) if old else None)
    out.append((K.age_level(age, STATUS_LIMIT), "状态文档 %d 秒前" % age))
    if d("fetch", "errors"):
        out.append(("WARN", "读取失败 %d 次、退避 %d 次" % (d("fetch", "errors"), d("fetch", "backoffs"))))
    behind = num(doc, "fetch", "behind_seconds")
    if behind > BEHIND_LIMIT:
        out.append((K.age_level(behind, BEHIND_LIMIT), "等待这个来源的请求已 %d 秒（behind_seconds）" % behind))
    lost = d("fetch", "ring_overwritten_lines")
    if lost:
        out.append(("WARN", "%d 条记录在读出之前被缓冲区覆盖（ring_overwritten_lines），相关请求不完整" % lost))
    if doc.get("scope") == "proxy" and d("buffer_overwritten"):
        out.append(("WARN", "proxy 缓冲区覆盖 %d 条（buffer_overwritten）" % d("buffer_overwritten")))
    if doc.get("scope") == "instance":
        ferr, _ = filter_counts(doc, old)
        if ferr:
            out.append(("FAIL", "过滤器处理出错 %d 次（errors、config_errors）→ 看这个实例 vLLM 日志中的过滤器报错" % ferr))
        cv = doc.get("config_version")
        if cv and fetcher_cv and cv != fetcher_cv:
            out.append(("FAIL", "段定义版本 %s，与采集进程的 %s 不同 → 把同一版 segments.json 下发到这个实例" % (cv, fetcher_cv)))
        if d("fetch", "run_changes"):
            out.append(("INFO", "运行号变化 %d 次（实例重启）" % d("fetch", "run_changes")))
    return out


def cmd_chain(c, r, args):
    r.section("chain（采集进程 → 入库进程 → ClickHouse）")
    stale = c.stale_limit()
    fetcher_url = c.ingest.get("fetcher_url", "")
    check_cmd = "在 proxy 机器上执行 python3 fetcher_check.py --url %s" % fetcher_url
    code, st, err = http_json(c.ingest_url("/status"), timeout=10)
    if code != 200 or not isinstance(st, dict):
        r.add("FAIL", "入库进程没有应答（状态端口 %s：%s）→ bin/ingest.sh start" % (c.get("INGEST_STATUS_PORT"), err or code))
    else:
        r.keep("入库进程 /status", json.dumps(st, ensure_ascii=False, indent=1))
        if st.get("blocked"):
            r.add("FAIL", "入库进程停止读取：%s → 接口版本不符时升级入库进程；集群名不符时核对 ingest.json 的 fetcher_url" % st["blocked"])
        ff = int(st.get("fetch_failures") or 0)
        r.add("FAIL" if ff >= 3 else ("WARN" if ff else "OK"), "入库进程：采集进程 %s，游标 %s，连续连接失败 %d 次%s" % (
            st.get("fetcher_url"), st.get("cursor"), ff, "；采集进程停止或网络不通 → %s" % check_cmd if ff else ""))
        gap = int(st.get("ingest_gap") or 0)
        r.add("WARN" if gap else "OK", "序号跳号 %d 份（入库进程启动以来）%s" % (
            gap, "：写出队列在读取之前删除了这些文档，入库进程或 ClickHouse 停机时间超过了写出队列可容纳的时长" if gap else ""))
        if st.get("ch_errors"):
            r.add("WARN", "写入 ClickHouse 失败 %s 次（入库进程启动以来）→ 看 logs/ingest.log" % st["ch_errors"])
        r.add("INFO", "入库进程启动以来写入请求文档 %s 份、状态文档 %s 份" % (st.get("docs_request"), st.get("docs_status")))
    rows, err = ch(c, "SELECT scope, instance, argMax(doc, ts) AS doc, dateDiff('second', max(ts), now64(3)) AS age "
                      "FROM vllm_pd.status_doc WHERE ts > now() - INTERVAL 1 DAY GROUP BY scope, instance ORDER BY scope, instance")
    if rows is None:
        r.add("FAIL", "ClickHouse 查询失败（%s）→ bin/diag.sh monitor 查看容器 %s" % (err, c.get("CLICKHOUSE_CONTAINER")))
        return
    olds, _ = ch(c, "SELECT scope, instance, argMax(doc, ts) AS doc FROM vllm_pd.status_doc WHERE ts > now() - INTERVAL 1 DAY "
                    "AND ts <= now() - INTERVAL %d SECOND GROUP BY scope, instance" % WINDOW)
    cur = {(x["scope"], x["instance"]): (json.loads(x["doc"]), int(x["age"])) for x in rows}
    old = {(x["scope"], x["instance"]): json.loads(x["doc"]) for x in olds or []}
    fdoc, fage = cur.get(("fetcher", "fetcher"), (None, None))
    fold = old.get(("fetcher", "fetcher"))
    if fold and fdoc and fold.get("started") != fdoc.get("started"):
        old = {}          # the fetcher restarted within the window: its counters started again from 0
    since = "最近 %d 分钟" % (WINDOW // 60) if old else "采集进程启动以来"
    # the newest request document
    req, err = ch(c, "SELECT count() AS n, dateDiff('second', max(ts), now64(3)) AS age, countIf(ts > now() - INTERVAL 15 MINUTE) AS n15 "
                     "FROM vllm_pd.request_doc WHERE ts > now() - INTERVAL 1 DAY")
    n, rage, n15 = (int(req[0]["n"]), int(req[0]["age"]), int(req[0]["n15"])) if req else (0, None, 0)
    fresh = fage is not None and fage <= STATUS_LIMIT
    if n and rage <= stale:
        r.add("OK", "最新的请求文档 %d 秒前（请求开始时刻），最近 15 分钟 %d 份" % (rage, n15))
    else:
        what = "最新的请求文档 %d 秒前，超过 ALERT_STALE_SECONDS（%g 秒）" % (rage, stale) if n else "最近 1 天没有请求文档"
        r.add("WARN" if fresh else "FAIL", "%s；%s" % (what, "采集进程的状态文档 %d 秒前写入：最近可能没有请求，或各实例的记录没有到达采集进程（见下面各来源的行）" % fage
                                                   if fresh else "采集进程的状态文档也已过时 → 入库进程或采集进程停止，见上面入库进程的行"))
    # the fetcher's own status document
    if fdoc is None:
        r.add("FAIL", "最近 1 天没有采集进程的状态文档（scope fetcher）→ %s" % check_cmd)
    else:
        fo = fold if old else None
        lv = K.age_level(fage, STATUS_LIMIT)
        r.add(lv, "采集进程的状态文档 %d 秒前，段定义版本 %s，在途请求 %d 个%s" % (
            fage, fdoc.get("config_version"), num(fdoc, "open_requests"), "" if lv == "OK" else " → %s" % check_cmd))
        if fdoc.get("config_mismatch"):
            r.add("FAIL", "段定义版本与采集进程不同的实例（config_mismatch）：%s → 下发与采集进程同一版的 segments.json" % " ".join(fdoc["config_mismatch"]))
        for key, text in (("log_dropped", "写出队列在读取之前删除了 %d 份文档（log_dropped）：监控端停机时间超过了写出队列可容纳的时长"),
                          ("late_dropped", "%d 条延迟到达的记录超过延迟到达窗口被丢弃（late_dropped）")):
            v = delta(num(fdoc, key), num(fo, key) if fo else None)
            r.add("WARN" if v else "OK", ("%s：" % since) + (text % v if v else "%s 为 0" % key))
    # each instance and proxy worker
    fcv = fdoc.get("config_version") if fdoc else None
    good, bad_lines, gone, removed, dropped = [], [], [], [], 0
    for (scope, inst), (doc, age) in sorted(cur.items(), key=lambda kv: (kv[0][0] != "instance", kv[0][1])):
        if scope == "fetcher":
            continue
        if scope == "proxy" and doc.get("gone"):
            gone.append(inst)
            continue
        if age > GONE_AFTER and fresh:
            removed.append(inst)
            continue
        res = judge_source(doc, old.get((scope, inst)), age, fcv)
        if scope == "proxy":        # the proxy is optional: its state is reported, never judged
            res = [("INFO", t) for _, t in res]
        if scope == "instance":
            dropped += filter_counts(doc, old.get((scope, inst)))[1]
        lv = worst(l for l, _ in res)
        if lv in ("OK", "INFO"):
            good.append((inst, age))
        else:
            bad_lines.append((lv, "%s：%s（计数为%s）" % (inst, "；".join(t for _, t in res), since)))
    if good:
        r.add("OK", "%d 个来源正常（%s）：状态文档最多 %d 秒前，%s读取无失败、无覆盖，过滤器无出错，段定义版本与采集进程相同" % (
            len(good), " ".join(i for i, _ in good[:12]) + (" 等" if len(good) > 12 else ""), max(a for _, a in good), since))
    for lv, text in bad_lines:
        r.add(lv, text)
    if not good and not bad_lines:
        r.add("FAIL", "最近 1 天没有实例和 proxy worker 的状态文档 → %s" % check_cmd)
    if not any(sc == "proxy" for sc, _ in cur):
        r.add("INFO", "没有 proxy worker 的状态文档（proxy 未打补丁、未运行或仍是旧补丁）：proxy 可选，请求按 no_proxy 写出，proxy 段为空")
    if gone:
        r.add("INFO", "已退出的 proxy worker %d 个（%s）" % (len(gone), " ".join(gone[:8])))
    if removed:
        r.add("INFO", "超过 1 小时没有状态文档、不再判断的来源：%s（已从 fetcher.json 中去掉）" % " ".join(removed[:12]))
    r.add("INFO", "%s过滤器丢弃的记录 %d 条（dropped：不属于面板二的记录，或缺少请求号的记录）" % (since, dropped))


# ==================== history ====================
def spans(vals, cond, step):
    """Runs of consecutive points (at most step apart) where cond holds: [(start, end, n)]."""
    out = []
    for t, v in vals:
        if not cond(v):
            continue
        if out and t - out[-1][1] <= step * 1.5:
            out[-1] = (out[-1][0], t, out[-1][2] + 1)
        else:
            out.append((t, t, 1))
    return out


def cmd_history(c, r, args):
    try:
        t0 = time.mktime(time.strptime(args.start, "%Y-%m-%d %H:%M"))
        t1 = time.mktime(time.strptime(args.end, "%Y-%m-%d %H:%M"))
    except ValueError:
        raise SystemExit("错误：时间写成 \"2026-11-13 10:30\"（监控机本地时间）")
    step = 60 if t1 - t0 <= 6 * 3600 else 300
    job = c.get("INGEST_JOB")
    stale = c.stale_limit()
    r.section("history %s – %s（每 %d 秒一个点，抓取任务 %s）" % (args.start, args.end, step, job))
    hm = lambda t: time.strftime("%m-%d %H:%M", time.localtime(t))
    sel = '{job="%s"}' % job.replace('"', "")
    items = (
        ("min(up%s)" % sel, lambda v: v < 1, "FAIL", "入库进程的状态端口抓取失败（up 为 0）"),
        ("max(pd_ingest_last_doc_age_seconds%s)" % sel, lambda v: v > stale, "WARN", "最近文档距今超过 %g 秒" % stale),
        ("max(pd_ingest_fetch_failures%s)" % sel, lambda v: v >= 3, "WARN", "无法连接采集进程（连续失败 3 次以上）"),
        ("max(pd_ingest_blocked%s)" % sel, lambda v: v >= 1, "FAIL", "入库进程停止读取（接口版本、集群名或不认识的文档）"),
        ("sum(increase(pd_ingest_gap_total%s[5m]))" % sel, lambda v: v > 0, "WARN", "序号跳号（写出队列在读取之前删除了文档）"),
        ("max(increase(pd_fetcher_log_dropped%s[10m]))" % sel, lambda v: v > 0, "WARN", "写出队列丢弃文档（log_dropped 增加）"),
        ('sum(rate(pd_ingest_docs_total{job="%s",table="request_doc"}[5m]))' % job.replace('"', ""), lambda v: v == 0, "INFO",
         "请求文档的写入速率为 0（没有请求，或采集、入库停止）"),
    )
    for i, (q, cond, level, what) in enumerate(items):
        res, err = prom_range(c, q, t0, t1, step)
        if res is None:
            r.add("FAIL", "Prometheus 查询失败（%s）" % err)
            return
        vals = [(float(t), float(v)) for s in res for t, v in s["values"]]
        r.keep(q, "\n".join("%s %g" % (hm(t), v) for t, v in vals))
        if not vals:
            if i == 0:
                r.add("WARN", "这段时间 Prometheus 中没有抓取任务 %s 的数据（超出保留期，或抓取任务没有加入：bin/prometheus_job.sh）" % job)
                return
            continue
        sp = spans(vals, cond, step)
        if not sp:
            r.add("OK", "整段没有出现：%s" % what)
            continue
        text = "；".join("%s–%s" % (hm(a), hm(b)) for a, b, _ in sp[-3:])
        r.add(level, "%s：%d 段，共 %d 个点（%s%s）" % (what, len(sp), sum(n for _, _, n in sp), "最后 3 段 " if len(sp) > 3 else "", text))


# ==================== log ====================
PREFIX = re.compile(r"\((?P<proc>[A-Za-z][A-Za-z0-9_:]*) pid=\d+\)")


def profile_of(path):
    """Counts and field names only, no content: process kinds, record kinds, fields, stages and how often a stage is
    missing or has no integer duration."""
    import gzip
    op = gzip.open if open(path, "rb").read(2) == b"\x1f\x8b" else open
    prof = {"lines": 0, "pd_diag": 0, "mooncake": 0, "procs": {}, "kinds": {}, "fields": {}, "stages": {}, "bad_json": 0,
            "shared_pool_tasks": 0}
    with op(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            prof["lines"] += 1
            if "[BatchPutTiming]" in line or "[BatchUpsertTiming]" in line:
                prof["mooncake"] += 1
            i = line.find("[PD_DIAG] ")
            if i < 0:
                continue
            prof["pd_diag"] += 1
            m = PREFIX.search(line[:i])
            proc = re.sub(r"\d+", "N", m.group("proc")) if m else "?"
            prof["procs"][proc] = prof["procs"].get(proc, 0) + 1
            try:
                o = json.loads(line[i + 10:])
            except ValueError:
                prof["bad_json"] += 1
                continue
            kind = o.get("event", "?") + ("[%s]" % (o.get("scope") or o.get("phase")) if (o.get("scope") or o.get("phase")) else "")
            if o.get("event") == "pool_task" and not o.get("request_id") and len(o.get("request_ids") or []) > 1:
                prof["shared_pool_tasks"] += 1
            prof["kinds"][kind] = prof["kinds"].get(kind, 0) + 1
            fs = prof["fields"].setdefault(kind, {})
            for k in o:
                fs[k] = fs.get(k, 0) + 1
            st = prof["stages"].setdefault(kind, {})
            for name, v in (o.get("stages") or {}).items():
                ok = isinstance(v, dict) and isinstance(v.get("duration_ns"), int)
                cur = st.setdefault(name, [0, 0])
                cur[0] += 1
                cur[1] += 0 if ok else 1
    return prof


# Record kinds pack two reads: the seven every log of the patch has, and the two of patch 02, each written on one side only.
NEED_KINDS = ("api_summary[http]", "api_summary[engine_submission]", "api_summary[request]", "core_summary[request]",
              "core_summary[execution]", "worker_summary[execute]", "worker_summary[sample]")
NEW_KINDS = (("pool_task[task]", "缓存池的保存、加载任务，只在 P 侧、用了缓存池时有"),
             ("pd_transfer_summary[request_generation]", "D 从 P 读取 KV 的请求汇总，只在 D 侧有"))


def judge_kinds(prof):
    """[(level, text)]: the 9 record kinds pack two reads. The seven are FAIL when missing; the two new ones are not
    required in one file (each is on one side only), but a file with neither is WARN."""
    out = []
    miss = [k for k in NEED_KINDS if not prof["kinds"].get(k)]
    out.append(("FAIL" if miss else "OK", "包二读的 9 种记录：原有的 7 种%s" % ("缺 %s" % " ".join(miss) if miss else "都在")))
    if not any(prof["kinds"].get(k) for k, _ in NEW_KINDS):
        out.append(("WARN", "包二读的 9 种记录：新的 2 种（%s）都没有：这份日志不是补丁 01、02 写的，v3 新加的 8 个段会是空的" % (
            "、".join(k for k, _ in NEW_KINDS))))
    else:
        for k, what in NEW_KINDS:
            n = prof["kinds"].get(k, 0)
            out.append(("OK" if n else "INFO", "包二读的 9 种记录：%s %s（%s）" % (k, "%d 条" % n if n else "没有", what)))
    return out


def known_profiles(base_dir):
    """[(file name, profile)] of the known samples in base_dir, sorted by file name."""
    if not os.path.isdir(base_dir):
        return []
    out = []
    for f in sorted(os.listdir(base_dir)):
        if f.endswith(".json") and not f.startswith("."):
            try:
                out.append((f, json.load(open(os.path.join(base_dir, f), encoding="utf-8"))))
            except (OSError, ValueError):
                continue
    return out


def pick_profile(prof, bases):
    """The known sample whose record kinds are closest to prof's; a tie goes to the later file name."""
    have = set(prof["kinds"])

    def score(item):
        kinds = set(item[1].get("kinds") or {})
        both = have | kinds
        return (len(have & kinds) / len(both) if both else 0.0, item[0])
    return max(bases, key=score)


def cmd_log(c, r, args):
    r.section("log %s" % args.file)
    if not os.path.isfile(args.file):
        raise SystemExit("错误：没有这个文件 %s" % args.file)
    tool = os.path.join(PACK, "tools", "check_pd_diag_format.py")
    if os.path.isfile(tool):
        code, out, err = run([sys.executable, "-I", tool, args.file], timeout=3600)
        r.keep("check_pd_diag_format.py", out + err)
        last = (out.strip().splitlines() or [""])[-1]
    else:
        # The format checker belongs to the cluster side and is not shipped with the monitoring-side pack.
        r.add("INFO", "格式校验不适用：本目录不含集群侧的日志格式检查工具 tools/check_pd_diag_format.py")
        last = "不适用"
    prof = profile_of(args.file)
    r.keep("profile", json.dumps(prof, ensure_ascii=False, indent=1))
    base_dir = os.path.join(PACK, "samples", "profiles")
    if prof["bad_json"]:
        r.add("FAIL", "%d 行 [PD_DIAG] 后面不是 JSON" % prof["bad_json"])
    for lv, text in judge_kinds(prof):
        r.add(lv, text)
    r.add("INFO", "共用缓存池任务（只带 request_ids、两个以上请求共用的 pool_task）%d 条：这种记录不归到任何一个请求上" % prof["shared_pool_tasks"])
    bases = known_profiles(base_dir)
    if not bases:
        # The known samples belong to the cluster side and are not shipped with the monitoring-side pack.
        r.add("INFO", "与已知样本的比较不适用：本目录不含已知样本的统计（samples/profiles/）")
        return
    bname, base = pick_profile(prof, bases)
    r.add("INFO", "格式校验：%s；和已知样本 %s 比（记录种类最接近的一份）" % (last, base.get("name", bname)))
    extra = {k: v for k, v in prof["kinds"].items() if k not in base["kinds"]}
    if extra:
        r.add("WARN", "和已知样本不同：多了 %s（过滤器丢弃并计数，看板上对应的段为空）" % "、".join("%s %d 条" % kv for kv in sorted(extra.items())))
    gone = [k for k in base["kinds"] if k not in prof["kinds"]]
    if gone:
        r.add("WARN", "和已知样本不同：少了 %s" % "、".join(gone))
    for kind, st in sorted(prof["stages"].items()):
        b = (base.get("stages") or {}).get(kind, {})
        for name, (n, bad) in sorted(st.items()):
            bn, bbad = b.get(name, [0, 0])
            if n and bad / n > (bbad / bn if bn else 0) + 0.05:
                r.add("WARN", "和已知样本不同：%s 有 %d 条里 %d 条没有可用的 stages.%s（已知样本 %d 条里 %d 条）" % (kind, n, bad, name, bn, bbad))
    newp = sorted(p for p in prof["procs"] if p not in base["procs"])
    if newp:
        r.add("WARN", "和已知样本不同：进程多了 %s（已知样本只有 %s）" % ("、".join(newp), "、".join(sorted(base["procs"]))))
    if prof["mooncake"] != base.get("mooncake", 0):
        r.add("INFO", "Mooncake 计时行 %d 行（已知样本 %d 行）" % (prof["mooncake"], base.get("mooncake", 0)))
    if args.save_profile:
        d = os.path.join(REPORT_ROOT, c.name, "profiles")
        os.makedirs(d, mode=0o700, exist_ok=True)
        prof["name"] = os.path.basename(args.file)
        p = os.path.join(d, os.path.basename(args.file) + ".json")
        json.dump(prof, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        r.add("INFO", "统计存成 %s（只有计数和字段名，不含日志内容），可以发回" % p)


# ==================== pair ====================
def cmd_pair(c, r, args):
    n = args.count
    r.section("pair %s（会发 %d 个请求）" % (args.proxy, n))
    model = args.model
    if not model:
        rows, err = ch(c, "SELECT model FROM vllm_pd.request_doc WHERE ts > now() - INTERVAL 1 DAY ORDER BY ts DESC LIMIT 1")
        if rows is None:
            r.add("FAIL", "ClickHouse 查询失败（%s）" % err)
            return
        model = rows[0]["model"] if rows else ""
        if not model:
            r.add("FAIL", "最近 1 天的请求文档中没有模型名 → 用 --model <模型名> 指定")
            return
    tag = "diag-%s" % time.strftime("%Y%m%d%H%M%S")
    ok = 0
    for i in range(1, n + 1):
        rid = "%s-%d" % (tag, i)
        # the id both ways: header X-Request-Id, and request_id in the body (client_request_id of the request document)
        body = json.dumps({"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 8, "request_id": rid}).encode()
        code, res, err = http_json("http://%s/v1/chat/completions" % args.proxy, timeout=120, data=body,
                                   headers={"Content-Type": "application/json", "X-Request-Id": rid})
        if code == 200:
            ok += 1
        else:
            r.add("FAIL", "第 %d 个请求 HTTP %s（%s）" % (i, code, (err or "")[:100]))
    if not ok:
        return
    r.add("OK", "%d 个请求返回 200，请求体的 request_id 为 %s-1 … %s-%d（同时带 X-Request-Id 头）" % (ok, tag, tag, n))
    sql = ("SELECT request_id, client_request_id, closed, route_p, route_d, missing FROM vllm_pd.request_doc FINAL "
           "WHERE ts > now() - INTERVAL 1 HOUR AND (client_request_id LIKE '%s-%%' OR request_id LIKE '%s-%%')" % (tag, tag))
    deadline, docs = time.time() + args.wait, []
    while True:
        docs, err = ch(c, sql)
        docs = docs or []
        if (len(docs) >= ok and all(d["closed"] in ("complete", "no_proxy") for d in docs)) or time.time() >= deadline:
            break
        time.sleep(10)
    r.keep("pair", json.dumps(docs, ensure_ascii=False, indent=1))
    if not docs:
        r.add("FAIL", "%d 秒内 request_doc 中没有这几个请求 → bin/diag.sh chain；在 proxy 机器上执行 fetcher_check.py" % args.wait)
        return
    both = [d for d in docs if d["route_p"] and d["route_d"]]
    for d in docs:
        key = d.get("client_request_id") or d["request_id"]
        if d not in both:
            side = "P" if d["route_p"] else ("D" if d["route_d"] else "")
            r.add("FAIL", "%s 只有 %s 侧（closed=%s，缺 %s）→ 核对 proxy 发给 P、D 的 X-Request-Id 是否相同" % (
                key, side or "proxy", d["closed"], " ".join(d.get("missing") or []) or "—") if side else
                "%s 两侧都没有（closed=%s）" % (key, d["closed"]))
        elif d["closed"] == "no_proxy":
            r.add("INFO", "%s P、D 两侧都在（%s → %s），closed=no_proxy：没有 proxy 的记录（proxy 可选），proxy 段为空" % (
                key, d["route_p"], d["route_d"]))
        elif d["closed"] != "complete":
            r.add("WARN", "%s P、D 两侧都在（%s → %s），但 closed=%s" % (key, d["route_p"], d["route_d"], d["closed"]))
    if len(both) == ok and all(d["closed"] in ("complete", "no_proxy") for d in docs):
        r.add("OK", "%d 个请求都有请求文档，P、D 两侧都在（%s），closed=%s" % (ok, "、".join("%s → %s" % (d["route_p"], d["route_d"]) for d in both),
                                                                    "、".join(sorted(set(d["closed"] for d in docs)))))
    elif len(docs) < ok:
        r.add("FAIL", "%d 个请求中只有 %d 个有请求文档" % (ok, len(docs)))
    r.add("INFO", "这几个请求会出现在看板上，请求体的 request_id 以 diag- 开头，是诊断发的，可以忽略")


# ==================== main ====================
USAGE = """在监控机上、集群目录中执行。除 pair 以外只读；报告写在 ~/vllm-pd-diag-reports/<集群>/。说明见 docs/诊断工具说明.zh-CN.md。
采集进程和写出队列在集群端检查：在 proxy 机器上执行 python3 fetcher_check.py --url http://<proxy 机器>:9300。

  bin/diag.sh all                         依次执行 monitor、chain，屏幕上最多 40 行
  bin/diag.sh monitor | chain
  bin/diag.sh history "<起>" "<止>"        监控机本地时间，例如 "2026-11-13 10:30"
  bin/diag.sh log <日志文件> [--save-profile]
  bin/diag.sh pair <proxy 地址:端口> [--model 模型名] [--count 个数] [--wait 秒]
"""


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bin/diag.sh", description=USAGE, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    for name in ("all", "monitor", "chain"):
        sub.add_parser(name)
    h = sub.add_parser("history")
    h.add_argument("start")
    h.add_argument("end")
    lg = sub.add_parser("log")
    lg.add_argument("file")
    lg.add_argument("--save-profile", action="store_true")
    pr = sub.add_parser("pair")
    pr.add_argument("proxy")
    pr.add_argument("--model", default=None)
    pr.add_argument("--count", type=int, default=3)
    pr.add_argument("--wait", type=int, default=180, help="最多等待的秒数（每 10 秒查询一次）")
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    c = Cluster(PACK)
    r = Report(args.cmd)
    print("bin/diag.sh %s：集群 %s（%s），%s" % (args.cmd, c.name, c.dir, "只读" if args.cmd != "pair" else "会发 %d 个请求" % args.count), flush=True)
    steps = {"monitor": cmd_monitor, "chain": cmd_chain, "history": cmd_history, "log": cmd_log, "pair": cmd_pair}
    for name in (("monitor", "chain") if args.cmd == "all" else (args.cmd,)):
        steps[name](c, r, args)
    lines = r.lines
    if args.cmd == "all":
        # at most 40 lines: section titles, FAIL and WARN first, then OK and INFO while there is room
        keep = [l for l in lines if l[0] in ("", "FAIL", "WARN")]
        rest = [l for l in lines if l[0] in ("OK", "INFO")][:max(38 - len(keep), 0)]
        lines = [l for l in lines if l in keep or l in rest]
    for lv, t in lines:
        print(fmt(lv, t))
    path = r.write(os.path.join(REPORT_ROOT, c.name), " ".join(sys.argv[1:] if argv is None else argv))
    cnt = r.counts()
    print("报告：%s" % path)
    print("RESULT: %s" % ("FAIL（%d 项）" % cnt["FAIL"] if cnt["FAIL"] else ("WARN（%d 项）" % cnt["WARN"] if cnt["WARN"] else "OK")))
    return 1 if cnt["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
