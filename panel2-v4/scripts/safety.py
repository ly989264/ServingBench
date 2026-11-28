#!/usr/bin/env python3
"""The upgrade-safety check of an installed panel-2 cluster folder: six items, read-only.

    safety.py --cluster-dir D

Runs from any pack folder (the update script runs it from the new pack against the installed folder): the code and the
expectations are this pack's, the settings are D's clickhouse.env, grafana.env and ingest.json. Nothing is written
anywhere; ClickHouse queries run as the admin account with --readonly=1.

  版本和文件   D's VERSION is this pack's; every file listed in D's SHA256SUMS has that sha256 (the cluster's own
               configuration files, generated/, run/, logs/ and offline/ are not compared)
  容器         the ClickHouse container is running; the ingest process (bin/ingest.sh) is alive
  库           the tables request_doc, status_doc, segment_def, db_interface and the view request_json exist;
               db_interface has pd_diag_db with major version 1; segment_def is not empty
  Grafana      login, the board (GRAFANA_BOARD_UID) and the data source (GRAFANA_DS_UID) of grafana.env
  过滤器和粒度 from the latest status document of each instance (last 3 minutes): its segment definition version equals
               the fetcher's, and that version is in segment_def; the PD_DIAG_LEVEL each instance runs at (INFO)
  链路通       the ingest process's /metrics answers and it is not blocked; Prometheus scrapes INGEST_JOB and has the 4
               rules of RULES_GROUP; the newest status document is younger than 180 s; requests in the last 15 minutes
               (INFO)

Data accuracy (completeness, pairing) is not here: that is bin/panel2_cluster.sh quality, which never gates.

Output: one line per finding, "OK   ", "INFO ", "WARN ", "FAIL " and the text; the last line
"RESULT: OK" / "RESULT: WARN（n 项）" / "RESULT: FAIL（n 项）". Exit 1 when something FAILs, else 0.
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import criteria as C  # noqa: E402
from segdef import read_env  # noqa: E402

CONFIG_FILES = ("clickhouse.env", "grafana.env", "ingest.json")
NOT_IN_SUMS = CONFIG_FILES + ("generated/", "run/", "logs/", "offline/")
TABLES = ("request_doc", "status_doc", "segment_def", "db_interface")
VIEW = "request_json"
DB_INTERFACE = ("pd_diag_db", 1)
STATUS_WINDOW_S = 180      # the fetcher writes its status documents every minute
RULES_COUNT = 4


# ==================== facts ====================
def run(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, "", "超时（%d 秒）" % timeout
    except OSError as e:
        return 127, "", str(e)


class Cluster:
    def __init__(self, folder):
        self.dir = os.path.abspath(folder)
        for f in CONFIG_FILES + ("bin/lib.sh",):
            if not os.path.isfile(os.path.join(self.dir, f)):
                raise SystemExit("错误：%s 里没有 %s，不是装好的面板二目录" % (self.dir, f))
        self.ch_env = read_env(os.path.join(self.dir, "clickhouse.env"))
        self.gf_env = read_env(os.path.join(self.dir, "grafana.env"))
        vf = os.path.join(self.dir, "VERSION")
        self.version_file = open(vf).read().strip() if os.path.isfile(vf) else "?"

    def ch(self, k, default=""):
        return self.ch_env.get(k) or default

    def gf(self, k, default=""):
        return self.gf_env.get(k) or default


def ch(c, sql, timeout=60):
    code, out, err = run(["docker", "exec", "-i", c.ch("CLICKHOUSE_CONTAINER"), "clickhouse-client", "--password",
                          c.ch("CH_ADMIN_PASSWORD"), "--readonly=1", "-q", sql + " FORMAT JSONEachRow"], timeout=timeout)
    if code != 0:
        msg = (err or out).strip().splitlines()
        return None, msg[-1][:200] if msg else "退出码 %d" % code
    return [json.loads(x) for x in out.splitlines() if x.strip()], None


def http_get(url, timeout=10, auth=None):
    req = urllib.request.Request(url)
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(("%s:%s" % auth).encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), None
    except urllib.error.HTTPError as e:
        return e.code, b"", str(e)
    except Exception as e:  # noqa: BLE001
        return None, b"", str(e)[:160]


def http_json(url, timeout=10, auth=None):
    code, body, err = http_get(url, timeout, auth)
    try:
        return code, (json.loads(body) if body.strip()[:1] in (b"{", b"[") else None), err
    except ValueError:
        return code, None, err


def prom(c, path):
    url = c.ch("PROMETHEUS_URL", "http://localhost:9090").rstrip("/") + path
    code, res, err = http_json(url)
    if code != 200 or not isinstance(res, dict):
        return None, err or "HTTP %s" % code
    return res["data"], None


def pid_alive(path):
    try:
        pid = int(open(path).read().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def quote(s):
    return str(s).replace("\\", "").replace("'", "")


# ==================== the six items ====================
class Out:
    def __init__(self):
        self.lines = []

    def add(self, level, text):
        self.lines.append((level, text))

    def verdict(self, v, prefix=""):
        self.add(v.level, prefix + v.text)

    def counts(self):
        return {k: sum(1 for lv, _ in self.lines if lv == k) for k in ("FAIL", "WARN")}


def item_version_files(c, o):
    want = open(os.path.join(PACK, "VERSION")).read().strip()
    ok = c.version_file == want
    o.add("OK" if ok else "FAIL", "版本和文件：面板二目录是 %s%s" % (c.version_file, "" if ok else "，这个检查要 %s" % want))
    sums = os.path.join(c.dir, "SHA256SUMS")
    if not os.path.isfile(sums):
        o.add("WARN", "版本和文件：%s 里没有 SHA256SUMS，没有逐个核对文件" % c.dir)
        return
    bad = []
    for line in open(sums, encoding="utf-8"):
        parts = line.split()
        if len(parts) != 2:
            continue
        name = parts[1].lstrip("*")
        name = name[2:] if name.startswith("./") else name
        if name in NOT_IN_SUMS or name.startswith(tuple(x for x in NOT_IN_SUMS if x.endswith("/"))):
            continue
        p = os.path.join(c.dir, name)
        if not os.path.isfile(p):
            bad.append(name + "（缺）")
        elif C.file_sha256(p) != parts[0]:
            bad.append(name)
    if bad:
        o.add("FAIL", "版本和文件：%d 个文件和 SHA256SUMS 不同：%s" % (len(bad), " ".join(bad[:12]) + (" …" if len(bad) > 12 else "")))
    else:
        o.add("OK", "版本和文件：文件都和 SHA256SUMS 相同（不含集群自己的 clickhouse.env、grafana.env、ingest.json）")


def item_containers(c, o):
    name = c.ch("CLICKHOUSE_CONTAINER")
    code, out, _ = run(["docker", "inspect", "--format", "{{.State.Running}} {{.RestartCount}}", name])
    if code != 0:
        o.add("FAIL", "容器：%s 不存在 → bin/clickhouse_up.sh" % name)
    elif not out.startswith("true"):
        o.add("FAIL", "容器：%s 没在运行 → bin/clickhouse_up.sh" % name)
    else:
        o.add("OK", "容器：%s 在运行" % name)
    loop = pid_alive(os.path.join(c.dir, "run", "ingest-loop.pid"))
    proc = pid_alive(os.path.join(c.dir, "run", "ingest.pid"))
    if loop and proc:
        o.add("OK", "容器：入库进程在运行（pid %d）" % proc)
    else:
        o.add("FAIL", "容器：入库进程没在运行 → bin/ingest.sh start")


def item_db(c, o):
    rows, err = ch(c, "SELECT name, engine FROM system.tables WHERE database = 'vllm_pd'")
    if rows is None:
        o.add("FAIL", "库：ClickHouse 查不了（%s）" % err)
        return False
    have = {r["name"]: r["engine"] for r in rows}
    miss = [t for t in TABLES if t not in have] + ([VIEW] if have.get(VIEW) != "View" else [])
    if miss:
        o.add("FAIL", "库：缺 %s → bin/clickhouse_up.sh" % " ".join(miss))
        return False
    rows, err = ch(c, "SELECT name, major FROM vllm_pd.db_interface FINAL")
    got = {(r["name"], int(r["major"])) for r in rows or []}
    if rows is None:
        o.add("FAIL", "库：读不了版本表 db_interface（%s）" % err)
    elif DB_INTERFACE not in got:
        o.add("FAIL", "库：版本表 db_interface 是 %s，这一版要 %s/v%d → bin/clickhouse_up.sh" % (
            "、".join("%s/v%d" % x for x in sorted(got)) or "空", DB_INTERFACE[0], DB_INTERFACE[1]))
    else:
        o.add("OK", "库：4 张表和视图 %s 都在，版本表是 %s/v%d" % (VIEW, DB_INTERFACE[0], DB_INTERFACE[1]))
    rows, err = ch(c, "SELECT config_version AS v, count() AS n FROM vllm_pd.segment_def FINAL GROUP BY config_version")
    if rows is None:
        o.add("FAIL", "库：读不了 segment_def（%s）" % err)
    elif not rows:
        o.add("FAIL", "库：segment_def 是空的：入库进程还没有从采集进程取到段定义 → bin/ingest.sh status")
    else:
        o.add("OK", "库：segment_def 有 %d 版段定义（%s）" % (len(rows), "、".join("%s %d 段" % (r["v"], int(r["n"])) for r in rows[:4])))
    return True


def item_grafana(c, o):
    url = c.gf("GRAFANA_URL", "http://localhost:3000").rstrip("/")
    auth = (c.gf("GRAFANA_USER", "admin"), c.gf("GRAFANA_PASSWORD"))
    code, _, err = http_json(url + "/api/user", auth=auth)
    if code != 200:
        o.add("FAIL", "Grafana：登录不了（%s）" % (err or "HTTP %s" % code))
        return
    code, _, _ = http_json(url + "/api/dashboards/uid/" + c.gf("GRAFANA_BOARD_UID"), auth=auth)
    code2, _, _ = http_json(url + "/api/datasources/uid/" + c.gf("GRAFANA_DS_UID"), auth=auth)
    if code == 200 and code2 == 200:
        o.add("OK", "Grafana：能登录，看板和数据源都在")
    else:
        o.add("FAIL", "Grafana：%s → bin/grafana_board.sh" % "、".join(x for x, k in (("看板不在", code), ("数据源不在", code2)) if k != 200))


def latest_status(c, scope, seconds):
    """[{instance, doc (parsed), age}] of the latest status document per instance of `scope` in the last `seconds`."""
    rows, err = ch(c, "SELECT instance, argMax(doc, ts) AS doc, dateDiff('second', max(ts), now64(3)) AS age FROM vllm_pd.status_doc "
                      "WHERE scope = '%s' AND ts >= now64(3) - INTERVAL %d SECOND GROUP BY instance ORDER BY instance" % (scope, seconds))
    if rows is None:
        return None, err
    out = []
    for r in rows:
        try:
            doc = json.loads(r["doc"])
        except ValueError:
            doc = {}
        out.append({"instance": r["instance"], "doc": doc, "age": int(r["age"])})
    return out, None


def since_change(fetcher_doc):
    """Seconds since the fetcher's last change of segments.json (segment_changes_recent), None when unknown."""
    import datetime
    recent = fetcher_doc.get("segment_changes_recent") or []
    if not recent or not isinstance(recent[-1], dict):
        return None
    try:
        t = datetime.datetime.strptime(str(recent[-1].get("ts", "")).replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z").timestamp()
    except ValueError:
        return None
    return max(0, int(time.time() - t))


def item_filter_level(c, o):
    inst, err = latest_status(c, "instance", STATUS_WINDOW_S)
    fet, err2 = latest_status(c, "fetcher", STATUS_WINDOW_S)
    if inst is None or fet is None:
        o.add("FAIL", "过滤器：查不了 status_doc（%s）" % (err or err2))
        return
    defined, _ = ch(c, "SELECT DISTINCT config_version AS v FROM vllm_pd.segment_def")
    if not inst:
        o.add("FAIL", "过滤器：最近 %d 秒没有实例的状态文档 → bin/ingest.sh status，并核对采集进程" % STATUS_WINDOW_S)
    fdoc = fet[0]["doc"] if fet else {}
    versions = {r["instance"]: r["doc"].get("config_version") or "" for r in inst}
    if inst or not fet:
        for v in C.judge_config_versions(versions, fdoc.get("config_version") or "", {r["v"] for r in defined or []},
                                         since_change(fdoc)):
            o.verdict(v, "过滤器：")
    count, none = {}, []
    for r in inst:
        lv = r["doc"].get("diag_level")
        if not isinstance(lv, int):
            none.append(r["instance"])
            continue
        side = "P" if (r["doc"].get("side") or r["instance"])[:1].upper() in ("P",) else "D"
        count[(side, lv)] = count.get((side, lv), 0) + 1
    parts = ["%s %d 级 %d 个实例" % (s, l, n) for (s, l), n in sorted(count.items(), key=lambda kv: (kv[0][0] != "P", kv[0][1]))]
    if none:
        parts.append("%s 的状态文档没有粒度（启动以后还没有自检行）" % " ".join(none))
    if parts:
        o.add("INFO", "粒度：" + "；".join(parts))


def item_chain(c, o):
    host = c.ch("MONITOR_HOST", "127.0.0.1")
    port = c.ch("INGEST_STATUS_PORT")
    code, body, err = http_get("http://%s:%s/metrics" % (host, port))
    if code != 200:
        o.add("FAIL", "链路通：入库进程的状态端口 %s:%s 没有应答（%s）→ bin/ingest.sh start" % (host, port, err or code))
    else:
        blocked = [x for x in body.decode("utf-8", "replace").splitlines() if x.startswith("pd_ingest_blocked ")]
        if blocked and blocked[0].split()[1] not in ("0", "0.0"):
            _, st, _ = http_json("http://%s:%s/status" % (host, port))
            o.add("FAIL", "链路通：入库进程停止读取：%s → bin/ingest.sh status" % ((st or {}).get("blocked") or "?")[:200])
        else:
            o.add("OK", "链路通：入库进程的状态端口有应答，没有停止读取")
    job, group = c.ch("INGEST_JOB"), c.ch("RULES_GROUP")
    data, err = prom(c, "/api/v1/query?" + urllib.parse.urlencode({"query": 'min(up{job="%s"})' % job}))
    if data is None:
        o.add("FAIL", "链路通：Prometheus 查不了（%s）" % err)
    else:
        res = data.get("result") or []
        up = float(res[0]["value"][1]) if res else 0.0
        o.add("OK" if up >= 1 else "FAIL", "链路通：Prometheus 抓 %s %s" % (job, "正常" if up >= 1 else "不到 → bin/prometheus_job.sh"))
        data, err = prom(c, "/api/v1/rules")
        n = sum(len(g.get("rules") or []) for g in (data or {}).get("groups") or [] if g.get("name") == group)
        if data is None:
            o.add("FAIL", "链路通：读不到 Prometheus 的规则（%s）" % err)
        else:
            o.add("OK" if n == RULES_COUNT else "FAIL", "链路通：Prometheus 规则组 %s 有 %d 条规则%s" % (
                group, n, "" if n == RULES_COUNT else "，应为 %d 条 → bin/prometheus_job.sh" % RULES_COUNT))
    rows, err = ch(c, "SELECT dateDiff('second', max(ts), now64(3)) AS a, count() AS n FROM vllm_pd.status_doc "
                      "WHERE ts > now64(3) - INTERVAL 1 DAY")
    if rows is None:
        o.add("FAIL", "链路通：查不了 ClickHouse（%s）" % err)
        return
    n, age = int(rows[0]["n"]), int(rows[0]["a"])
    if not n:
        o.add("FAIL", "链路通：最近 1 天没有状态文档（采集进程每分钟写一份）")
    elif age < STATUS_WINDOW_S:
        o.add("OK", "链路通：最新的状态文档是 %d 秒前的" % age)
    else:
        o.add("FAIL", "链路通：最新的状态文档是 %d 秒前的，超过 %d 秒（采集进程每分钟写一份）→ bin/ingest.sh status" % (age, STATUS_WINDOW_S))
    rows, _ = ch(c, "SELECT uniqExact(request_id) AS n FROM vllm_pd.request_doc WHERE ts > now64(3) - INTERVAL 15 MINUTE")
    n = int((rows or [{"n": 0}])[0]["n"])
    o.add("INFO", "最近 15 分钟 %d 个请求" % n if n else "最近 15 分钟没有请求")


def check(c):
    o = Out()
    item_version_files(c, o)
    item_containers(c, o)
    db_ok = item_db(c, o)
    item_grafana(c, o)
    if db_ok:
        item_filter_level(c, o)
    item_chain(c, o)
    return o


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cluster-dir", required=True)
    a = ap.parse_args()
    c = Cluster(a.cluster_dir)
    print("升级安全检查：%s（面板二 %s），只读" % (c.dir, c.version_file), flush=True)
    o = check(c)
    for lv, t in o.lines:
        print("%-4s %s" % (lv, t))
    n = o.counts()
    print("RESULT: %s" % ("FAIL（%d 项）" % n["FAIL"] if n["FAIL"] else "WARN（%d 项）" % n["WARN"] if n["WARN"] else "OK"))
    return 1 if n["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
