#!/usr/bin/env python3
"""The upgrade-safety check of an installed panel-2 cluster (design 2.1; T32 B1): nine items, read-only.

    safety.py --cluster-dir D [--pre-upgrade] [--wait SECONDS]

Runs from any pack folder (the update script runs it from the new pack against the installed folder): the code and the
expectations are this pack's, the settings are D's deploy.env (loaded by D's own bin/lib.sh in a clean shell, then the
v2.1 defaults of design 2.8). Nothing is written anywhere; ClickHouse queries carry --readonly=1, the nodes only get
read-only commands over SSH.

Data accuracy (completeness, pairing, gaps) is not here: that is bin/panel2_cluster.sh quality, which never gates.

Output: one line per finding, "OK   ", "INFO ", "WARN ", "FAIL " and the text; the last line
"RESULT: OK" / "RESULT: WARN（n 项）" / "RESULT: FAIL（n 项）". Exit 1 when something FAILs, else 0.
--pre-upgrade: D may still be v2.1; its files differing from its SHA256SUMS are WARN (the upgrade replaces them).
--wait S: repeat the filter and chain items for up to S seconds until they pass (after a mock start).
"""
import argparse
import hashlib
import json
import os
import re
import shlex
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

PACK_LOG_DIR = "/var/log/pd-diag"   # line.log_dir of the pack's segments.json, before deploy.env replaces it
UPGRADE_FROM = ("v2.1",)            # --pre-upgrade: the versions this pack upgrades from (and rolls back to)
TABLES = ("phase_line", "diag_event", "filter_selfcheck", "filter_file", "filter_other")
NOT_IN_SUMS = ("deploy.env", "inventory.json", "segments.json")


# ==================== facts ====================
def run(cmd, timeout=60, stdin=None, env=None):
    try:
        p = subprocess.run(cmd, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=env)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, "", "超时（%d 秒）" % timeout
    except OSError as e:
        return 127, "", str(e)


class Cluster:
    def __init__(self, folder):
        self.dir = os.path.abspath(folder)
        for f in ("deploy.env", "inventory.json", "segments.json", "bin/lib.sh"):
            if not os.path.isfile(os.path.join(self.dir, f)):
                raise SystemExit("错误：%s 里没有 %s，不是装好的面板二目录" % (self.dir, f))
        self.env = self._load_env()
        self.mode = (self.env.get("NODE_MODE") or "mock").strip()
        # A folder whose lib.sh is still v2.0 leaves this unset; v2.0 used /opt/vllm-pd-diag until run pins the value.
        self.env.setdefault("NODE_CONF_DIR_IN_CONTAINER", "/opt/vllm-pd-diag")
        self.inv = json.load(open(os.path.join(self.dir, "inventory.json"), encoding="utf-8"))
        self.segments = json.load(open(os.path.join(self.dir, "segments.json"), encoding="utf-8"))
        self.line = self.segments.get("line") or {}
        self.version = C.expected_version(self.dir)
        self.instances = {}
        for side, letter in (("prefill", "P"), ("decode", "D")):
            for i, a in enumerate(self.inv[side]["api_servers"]):
                host, port = a.rsplit(":", 1)
                self.instances.setdefault(host, []).append(("%s%d" % (letter, i), port))
        self.n_d = len(self.inv["decode"]["api_servers"])
        vf = os.path.join(self.dir, "VERSION")
        self.version_file = open(vf).read().strip() if os.path.isfile(vf) else "v1.0"

    def get(self, k, default=""):
        return self.env.get(k) or default

    def _load_env(self):
        script = ('cd %s && export PYTHONDONTWRITEBYTECODE=1 && . bin/lib.sh && load_env >/dev/null && env -0'
                  % shlex.quote(self.dir))
        keep = {k: os.environ[k] for k in ("HOME", "PATH", "USER", "LOGNAME", "LANG", "VLLM_PD_REGISTRY") if k in os.environ}
        keep["PYTHONDONTWRITEBYTECODE"] = "1"
        code, out, err = run(["bash", "-c", script], timeout=60, env=keep)
        if code != 0:
            raise SystemExit("错误：读不了 %s 的 deploy.env：%s" % (self.dir, err.strip()[-300:]))
        env = {}
        for item in out.split("\0"):
            k, sep, v = item.partition("=")
            if sep:
                env[k] = v
        return env


def ssh(c, addr, script, timeout=60):
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=yes", "-o", "LogLevel=ERROR"]
    cmd += shlex.split(c.get("SSH_OPTS"))
    if c.get("SSH_USER"):
        cmd += ["-l", c.get("SSH_USER")]
    return run(cmd + [addr, "sh -s"], timeout=timeout, stdin=script.encode())


def ch(c, sql, timeout=60):
    code, out, err = run(["docker", "exec", "-i", c.get("CLICKHOUSE_CONTAINER"), "clickhouse-client", "--password",
                          c.get("CH_ADMIN_PASSWORD"), "--readonly=1", "-q", sql + " FORMAT JSONEachRow"], timeout=timeout)
    if code != 0:
        msg = (err or out).strip().splitlines()
        return None, msg[-1][:200] if msg else "退出码 %d" % code
    return [json.loads(x) for x in out.splitlines() if x.strip()], None


def http_json(url, timeout=10, auth=None):
    req = urllib.request.Request(url)
    if auth:
        import base64
        req.add_header("Authorization", "Basic " + base64.b64encode(("%s:%s" % auth).encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, (json.loads(body) if body.strip()[:1] in (b"{", b"[") else None), None
    except urllib.error.HTTPError as e:
        return e.code, None, str(e)
    except Exception as e:  # noqa: BLE001
        return None, None, str(e)[:160]


def prom(c, query):
    url = c.get("PROMETHEUS_URL", "http://localhost:9090").rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": query})
    code, res, err = http_json(url)
    if code != 200 or not isinstance(res, dict):
        return None, err or "HTTP %s" % code
    return res["data"]["result"], None


# Known issue 7: the running mock processes (pid, start in clock ticks since boot, instance) and the node clock, so
# that the version item counts only processes that are still writing. In vllm mode diag.probe_vllm gives the processes.
NODE_PROBE = r'''
C=$(printf '%s' "$RAW_C" | sed "s|^\$HOME|$HOME|"); L=$(printf '%s' "$RAW_L" | sed "s|^\$HOME|$HOME|")
printf 'now\t%s\n' "$(date +%s)"
printf 'clock\t%s\t%s\n' "$(cut -d' ' -f1 /proc/uptime 2>/dev/null)" "$(getconf CLK_TCK 2>/dev/null || echo 100)"
for p in $(pgrep -f '[p]d032_mock.py [r]un-instance' 2>/dev/null); do
  s=$(cat "/proc/$p/stat" 2>/dev/null) || continue
  r=${s##*) }; set -- $r
  i=$(tr '\0' '\n' < "/proc/$p/cmdline" 2>/dev/null | awk 'f { print; exit } $0 == "--instance" { f = 1 }')
  printf 'alive\t%s\t%s\t%s\n' "$p" "${20}" "$i"
done
for f in pd_diag_logfilter.py pd_diag_logging.json segments.json; do
  printf 'sum\t%s\t%s\n' "$f" "$(sha256sum "$C/$f" 2>/dev/null | cut -d' ' -f1)"
done
printf 'pushed\t%s\n' "$(stat -c %Z "$C/segments.json" 2>/dev/null || echo 0)"
printf 'mock\t%s\n' "$(pgrep -fc '[p]d032_mock.py [r]un-instance' 2>/dev/null || echo 0)"
d=$L; while [ ! -e "$d" ] && [ "$d" != / ]; do d=$(dirname "$d"); done
printf 'free\t%s\n' "$(df -Pk "$d" 2>/dev/null | awk 'NR == 2 { printf "%.0f", $4 * 1024 }')"
'''


def probe_node(c, addr):
    pre = "RAW_C=%s\nRAW_L=%s\n" % (shlex.quote(c.get("NODE_CONF_DIR_RAW", "$HOME/vllm-pd-diag/conf")),
                                     shlex.quote(c.get("NODE_LOG_DIR_RAW", "$HOME/vllm-pd-diag/logs")))
    code, out, err = ssh(c, addr, pre + NODE_PROBE)
    if code != 0 and not out.strip():
        return None, err.strip()[:120] or "退出码 %d" % code
    f = {"sums": {}}
    clock, alive = None, []
    for line in out.splitlines():
        parts = line.split("\t")
        if parts[0] == "sum" and len(parts) >= 3:
            f["sums"][parts[1]] = parts[2]
        elif parts[0] in ("pushed", "mock", "free", "now") and len(parts) >= 2:
            f[parts[0]] = int(parts[1] or 0) if parts[1].strip().isdigit() else 0
        elif parts[0] == "clock" and len(parts) >= 3:
            clock = parts[1:3]
        elif parts[0] == "alive" and len(parts) >= 3:
            alive.append(parts[1:4] + [""] * (4 - len(parts)))
    f["alive"] = mock_alive(f.get("now"), clock, alive)
    return f, None


def mock_alive(now, clock, alive):
    """[(instance, pid, start epoch by the node clock)] of the running mock processes; None when the node clock or
    uptime could not be read (the version item then keeps the rule without processes)."""
    try:
        up, hz = float(clock[0]), float(clock[1] or 100)
    except (TypeError, ValueError, IndexError):
        return None
    if not now:
        return None
    out = []
    for pid, ticks, inst in alive:
        try:
            out.append((inst.strip(), int(pid), now - up + float(ticks) / hz))
        except ValueError:
            continue
    return out


def alive_processes(c, facts):
    """({addr: [(instance, pid, start epoch)]}, {addr: node now}) of the machines whose probe gave the running filter
    processes: the mock processes in mock mode (probe_node), the vLLM processes in vllm mode (vllm_states)."""
    alive, now = {}, {}
    for addr, f in facts.items():
        if not f or not f.get("now"):
            continue
        if c.mode == "vllm":
            procs = f.get("procs")
            if procs is None:
                continue
            alive[addr] = [(p.instance, int(p.pid), float(p.start_epoch)) for p in procs]
        else:
            if f.get("alive") is None:
                continue
            alive[addr] = f["alive"]
        now[addr] = f["now"]
    return alive, now


def push_age(facts):
    """Seconds since segments.json last changed on a node, by each node's own clock (the youngest of the machines);
    None when no node told."""
    ages = [f["now"] - f["pushed"] for f in facts.values() if f and f.get("now") and f.get("pushed")]
    return max(0, min(ages)) if ages else None


def probe_vllm(c, addr):
    """The vLLM processes of one machine through diag.probe_vllm (v2.1's diag), or None when it is not available."""
    try:
        import diag  # noqa: E402
        dc = diag.Cluster(c.dir)
        return diag.probe_vllm(dc, addr)
    except Exception as e:  # noqa: BLE001
        return None, {"error": "%s: %s" % (type(e).__name__, str(e)[:120])}


# ==================== the nine items ====================
class Out:
    def __init__(self):
        self.lines = []

    def add(self, level, text):
        self.lines.append((level, text))

    def verdict(self, v, prefix=""):
        self.add(v.level, prefix + v.text)

    def counts(self):
        return {k: sum(1 for lv, _ in self.lines if lv == k) for k in ("FAIL", "WARN")}


def item_version_files(c, o, pre):
    want = open(os.path.join(PACK, "VERSION")).read().strip()
    ok = c.version_file in (UPGRADE_FROM + (want,) if pre else (want,))
    o.add("OK" if ok else "FAIL", "版本和文件：面板二目录是 %s%s" % (c.version_file, "" if ok else "，这个检查要 %s" % (
        " 或 ".join(UPGRADE_FROM + (want,)) if pre else want)))
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
        if name in ("deploy.env", "inventory.json") or name.startswith(("generated/", "offline/")):
            continue
        p = os.path.join(c.dir, name)
        if not os.path.isfile(p):
            bad.append(name + "（缺）")
            continue
        if name == "segments.json":
            # log_dir and keep_hours come from deploy.env (design 2.8, known issue 14): the pack's values go back first
            try:
                text = C.set_keep_hours(C.set_log_dir(open(p, encoding="utf-8").read(), PACK_LOG_DIR), C.PACK_KEEP_HOURS)
            except ValueError as e:
                bad.append("%s（%s）" % (name, e))
                continue
            got = hashlib.sha256(text.encode("utf-8")).hexdigest()
        else:
            got = C.file_sha256(p)
        if got != parts[0]:
            bad.append(name)
    if bad:
        o.add("WARN" if pre else "FAIL", "版本和文件：%d 个文件和 SHA256SUMS 不同%s：%s" % (
            len(bad), "，升级会换成包里的" if pre else "", " ".join(bad[:12]) + (" …" if len(bad) > 12 else "")))
    else:
        o.add("OK", "版本和文件：文件都和 SHA256SUMS 相同（segments.json 只差 log_dir 和 keep_hours）")


def item_containers(c, o):
    for name in (c.get("CLICKHOUSE_CONTAINER"), c.get("CENTRAL_CONTAINER")):
        code, out, _ = run(["docker", "inspect", "--format", "{{.State.Running}} {{.RestartCount}}", name])
        if code != 0:
            o.add("FAIL", "容器：%s 不存在" % name)
        elif not out.startswith("true"):
            o.add("FAIL", "容器：%s 没在运行 → bin/central_up.sh 或 bin/clickhouse_up.sh" % name)
        else:
            o.add("OK", "容器：%s 在运行" % name)
    code, out, err = run(["docker", "exec", c.get("CENTRAL_CONTAINER"), "vector", "validate", "--no-environment",
                          "--skip-healthchecks", "/etc/vector/central.yaml"], timeout=60)
    o.add("OK" if code == 0 else "FAIL", "容器：采集器配置 %s" % ("Vector 自己检查过" if code == 0 else
                                                          "Vector 检查没过：%s" % (err or out).strip()[-160:]))


def item_db(c, o):
    rows, err = ch(c, "SELECT name, engine FROM system.tables WHERE database = 'vllm_pd'")
    if rows is None:
        o.add("FAIL", "库：ClickHouse 查不了（%s）" % err)
        return
    have = {r["name"] for r in rows}
    miss = [t for t in TABLES + ("phase_line_cur",) if t not in have]
    if miss:
        o.add("FAIL", "库：缺 %s → bin/clickhouse_up.sh" % " ".join(miss))
        return
    cid = c.segments.get("config_id", "")
    rows, err = ch(c, "SELECT create_table_query AS q FROM system.tables WHERE database = 'vllm_pd' AND name = 'phase_line_cur'")
    q = (rows or [{}])[0].get("q", "")
    if cid and cid in q:
        o.add("OK", "库：表和视图都在，视图读的是 %s" % cid)
    else:
        o.add("FAIL", "库：视图 phase_line_cur 读的不是这一版的 config_id %s → bin/clickhouse_up.sh" % cid)


def item_grafana(c, o):
    url = c.get("GRAFANA_URL", "http://localhost:3000").rstrip("/")
    auth = (c.get("GRAFANA_USER", "admin"), c.get("GRAFANA_PASSWORD"))
    code, _, err = http_json(url + "/api/user", auth=auth)
    if code != 200:
        o.add("FAIL", "Grafana：登录不了（%s）" % (err or "HTTP %s" % code))
        return
    code, _, _ = http_json(url + "/api/dashboards/uid/" + c.get("GRAFANA_BOARD_UID"), auth=auth)
    code2, _, _ = http_json(url + "/api/datasources/uid/" + c.get("GRAFANA_DS_UID"), auth=auth)
    if code == 200 and code2 == 200:
        o.add("OK", "Grafana：能登录，看板和数据源都在")
    else:
        o.add("FAIL", "Grafana：%s → bin/grafana_board.sh" % "、".join(x for x, k in (("看板不在", code), ("数据源不在", code2)) if k != 200))


def item_nodes(c, o, facts):
    expected = C.expected_node_sums(c.dir)
    for addr, names in c.instances.items():
        f, err = probe_node(c, addr)
        facts[addr] = f
        if f is None:
            o.add("FAIL", "节点：%s SSH 连不上（%s）" % (addr, err))
            continue
        o.verdict(C.judge_node_sums(expected, f["sums"]), "节点：%s " % addr)
        if c.mode == "mock":
            o.verdict(C.judge_mock_processes(f.get("mock", 0), [n for n, _ in names]), "节点：%s " % addr)
        elif f.get("mock"):
            o.add("FAIL", "节点：%s vllm 模式下还有 %d 个 mock 进程 → 在那台执行 pkill -9 -f '[p]d032_mock.py'" % (addr, f["mock"]))


def vllm_states(c, o, facts):
    """vllm mode: {instance: state} of all machines (design 2.1), from diag.probe_vllm; None when not available."""
    procs, unavailable = [], []
    for addr in c.instances:
        ps, info = probe_vllm(c, addr)
        if ps is None:
            unavailable.append("%s（%s）" % (addr, (info or {}).get("error", "?")))
            continue
        procs += ps
        if facts.get(addr) is not None:
            facts[addr]["procs"] = ps
        for name, how in (info or {}).get("found") or []:
            o.add("INFO", "节点：%s 找到 vLLM 容器 %s（%s）；不对时填 deploy.env 的 NODE_VLLM_CONTAINERS" % (addr, name, how))
        info = info or {}
        if info.get("docker") is None:
            o.add("WARN", "节点：%s SSH 用户不能执行 docker（不在 docker 组，也不能免密 sudo docker），没有核对真实容器" % addr)
        elif not info.get("containers"):
            o.add("WARN", "节点：%s 没有找到 vLLM 容器，没有核对真实容器 → 填 deploy.env 的 NODE_VLLM_CONTAINERS" % addr)
    if unavailable:
        o.add("WARN", "节点：没有核对 vLLM 进程：%s" % "；".join(unavailable))
    if not procs:
        return None
    sql = C.records_since_start_sql("vllm_pd.phase_line", procs)
    rows, err = ch(c, sql) if sql else ([], None)
    if rows is None:
        o.add("WARN", "过滤器：查不了这次启动以后的记录（%s）" % err)
        return None
    seen = {(r["instance"], int(r["pid"])) for r in rows if int(r.get("n", 0))}
    return C.instance_record_state(procs, seen)


def item_filter(c, o, facts, states):
    rows, err = ch(c, "SELECT argMax(config_version, ts) AS v, instance, pid, toUnixTimestamp64Milli(min(ts)) / 1000 AS f, "
                      "toUnixTimestamp64Milli(max(ts)) / 1000 AS l FROM vllm_pd.filter_selfcheck "
                      "WHERE ts >= now64(3) - INTERVAL 3 MINUTE GROUP BY instance, pid")
    if rows is None:
        o.add("FAIL", "过滤器：查不了自检表（%s）" % err)
        return
    raw = [(r["v"], r["instance"], int(r["pid"]), float(r["f"]), float(r["l"])) for r in rows]
    alive, node_now = alive_processes(c, facts)
    addr_of = {n: addr for addr, names in c.instances.items() for n, _ in names}
    versions = C.live_versions(raw, alive, addr_of)
    th = C.chain_thresholds(c.line, c.get("PULL_INTERVAL_S", "10"))
    starting = C.starting_instances(raw, alive, addr_of, node_now, th["clickhouse"])
    if states is not None:
        for v in C.judge_process_states(states, "过滤器"):
            o.verdict(v)
    elif c.mode == "vllm":
        have = {i for _, i, _ in versions}
        quiet = [n for names in c.instances.values() for n, _ in names if n not in have]
        if quiet:
            o.add("WARN", "过滤器：%s 最近 3 分钟没有自检行；分不清是这次启动以后没有请求，还是过滤器没起来 → bin/diag.sh vllm" % " ".join(quiet))
    now = time.time()
    age = push_age(facts)
    for v in C.judge_versions(versions, c.version, c.mode, now - age if age is not None else 0, now, states, starting):
        o.verdict(v)
    if states is not None:
        for v in C.judge_process_states(states, "版本"):
            o.verdict(v)


def item_chain(c, o, states):
    th = C.chain_thresholds(c.line, c.get("PULL_INTERVAL_S", "10"))
    url = "http://%s:%s/status" % (c.get("MONITOR_HOST"), c.get("PULLER_METRICS_PORT"))
    code, st, err = http_json(url)
    puller_ok = False
    if code != 200 or not isinstance(st, list):
        o.add("FAIL", "链路通：拉取程序没有运行（%s）→ bin/puller.sh start" % (err or code))
    else:
        bad = [m for m in st if m.get("age") is None or m["age"] > 120]
        puller_ok = not bad
        if bad:
            o.add("FAIL", "链路通：这些机器超过 2 分钟没有拉取成功：%s" % "；".join(
                "%s（%s）" % (m.get("addr"), (m.get("error") or "")[:60]) for m in bad))
        else:
            o.add("OK", "链路通：拉取 %d 台，最久的 %.0f 秒前成功" % (len(st), max([m["age"] for m in st] or [0])))
    for job in (c.get("PULLER_JOB"), c.get("CENTRAL_JOB")):
        res, err = prom(c, 'min(up{job="%s"})' % job)
        if res is None:
            o.add("FAIL", "链路通：Prometheus 查不了（%s）" % err)
            break
        up = float(res[0]["value"][1]) if res else 0.0
        o.add("OK" if up >= 1 else "FAIL", "链路通：Prometheus 抓 %s %s" % (job, "正常" if up >= 1 else "不到"))
    win = int(th["clickhouse"])
    rows, err = ch(c, "SELECT (SELECT count() FROM vllm_pd.filter_selfcheck WHERE ts > now64(3) - INTERVAL %d SECOND "
                      "AND config_version = '%s') AS s, (SELECT count() FROM vllm_pd.phase_line_cur WHERE ts > now64(3) - "
                      "INTERVAL %d SECOND) AS l" % (win, c.version.replace("'", ""), win))
    if rows is None:
        o.add("FAIL", "链路通：查不了 ClickHouse（%s）" % err)
        return
    s, l = int(rows[0]["s"]), int(rows[0]["l"])
    if s or l:
        o.add("OK", "链路通：最近 %d 秒这一版的自检行 %d 行、汇总行 %d 行" % (win, s, l))
    elif states is not None:
        vs = C.judge_process_states(states, "链路通")
        for v in vs:
            o.verdict(v)
        if not vs and puller_ok:
            # known issue 10: every instance has records and the puller works; idle unless the self-check lines stopped
            o.verdict(C.judge_idle_chain(selfcheck_ages(c), th["clickhouse"], win))
        elif not vs:
            o.add("FAIL", "链路通：最近 %d 秒没有这一版的行" % win)
    else:
        o.add("FAIL" if c.mode == "mock" else "WARN", "链路通：最近 %d 秒没有这一版（%s）的行%s" % (
            win, c.version, "" if c.mode == "mock" else "；vllm 模式下重启以后没有请求时也是这样 → bin/diag.sh vllm"))
    rows, _ = ch(c, "SELECT uniqExact(request_id) AS n FROM vllm_pd.phase_line_cur WHERE ts > now64(3) - INTERVAL 15 MINUTE")
    n = int((rows or [{"n": 0}])[0]["n"])
    o.add("INFO", "最近 15 分钟 %d 个请求" % n if n else "最近 15 分钟没有请求")


def selfcheck_ages(c):
    """{instance: seconds since its newest self-check line of this version (last day), None when it has none}."""
    rows, _ = ch(c, "SELECT instance, dateDiff('second', max(ts), now64(3)) AS a FROM vllm_pd.filter_selfcheck "
                    "WHERE ts > now64(3) - INTERVAL 1 DAY AND config_version = '%s' GROUP BY instance" % c.version.replace("'", ""))
    got = {r["instance"]: int(r["a"]) for r in rows or []}
    return {n: got.get(n) for names in c.instances.values() for n, _ in names}


def item_rate(c, o):
    if c.mode == "mock":
        o.verdict(C.judge_mock_rate(c.get("MOCK_RATE"), c.n_d), "节点上的数据量：")


def item_volume(c, o, facts):
    keep = float(c.line.get("keep_hours", 12))
    res, err = prom(c, C.log_rate_query())
    rates = {r["metric"].get("machine"): float(r["value"][1]) for r in (res or [])}
    total = 0.0
    for addr in c.instances:
        f = facts.get(addr) or {}
        est = C.estimate_log_bytes(rates.get(addr), keep) if res is not None else None
        total += est or 0.0
        if "free" not in f:
            o.add("WARN", "日志量和空闲空间：%s 读不出节点日志盘的空闲空间" % addr)
            continue
        o.verdict(C.judge_log_volume(est, f["free"], "%s 日志盘" % addr, int(keep)), "日志量和空闲空间：")
    pull = c.get("PULL_DIR")
    d = pull
    while d and not os.path.exists(d) and d != "/":
        d = os.path.dirname(d)
    try:
        s = os.statvfs(d or "/")
        o.verdict(C.judge_log_volume(total if res is not None else None, s.f_bavail * s.f_frsize, "镜像盘 %s" % pull, int(keep)),
                  "日志量和空闲空间：")
    except OSError as e:
        o.add("WARN", "日志量和空闲空间：读不了镜像盘 %s（%s）" % (pull, e))


def check(c, pre, wait):
    o = Out()
    facts = {}
    item_version_files(c, o, pre)
    item_containers(c, o)
    item_db(c, o)
    item_grafana(c, o)
    item_nodes(c, o, facts)
    states = vllm_states(c, o, facts) if c.mode == "vllm" else None
    deadline = time.time() + wait
    while True:
        part = Out()
        item_filter(c, part, facts, states)
        item_chain(c, part, states)
        if not part.counts()["FAIL"] or time.time() >= deadline:
            break
        print("（自检行或链路还没过，%d 秒后再查）" % 10, flush=True)
        time.sleep(10)
    o.lines += part.lines
    item_rate(c, o)
    item_volume(c, o, facts)
    return o


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cluster-dir", required=True)
    ap.add_argument("--pre-upgrade", action="store_true")
    ap.add_argument("--wait", type=int, default=0)
    a = ap.parse_args()
    c = Cluster(a.cluster_dir)
    print("升级安全检查：集群 %s（%s，%s 模式，面板二 %s），只读" % (c.get("CLUSTER", "?"), c.dir, c.mode, c.version_file), flush=True)
    o = check(c, a.pre_upgrade, a.wait)
    for lv, t in o.lines:
        print("%-4s %s" % (lv, t))
    n = o.counts()
    print("RESULT: %s" % ("FAIL（%d 项）" % n["FAIL"] if n["FAIL"] else "WARN（%d 项）" % n["WARN"] if n["WARN"] else "OK"))
    return 1 if n["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
