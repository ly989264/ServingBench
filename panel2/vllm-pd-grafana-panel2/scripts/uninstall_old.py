#!/usr/bin/env python3
"""Removes an earlier 面板二 (tar 59604f30, or tar a87f4d74) from the monitoring machine and the nodes (bin/uninstall_old.sh).
The version is told by the folder: mock/mockgen.py is 59604f30, mock/pd032_mock.py is a87f4d74.

  plan        lists what the earlier version left on this machine, in Grafana, Prometheus and on the nodes, and what stays;
              keeps a note of panel 1's dashboards and Prometheus targets for `check`
  remove      stops and removes it; the data stays (ClickHouse data, the mirror, the mock logs on the nodes)
  purge-data  deletes that data; cannot be undone. --backup <folder> first writes ClickHouse's data into that folder
  check       the checklist: nothing of it is left, panel 1 works as before

Everything is read from the earlier version's own deploy.env and inventory.json, in the folder given.
Panel 1 is not touched: its Prometheus and Grafana keep running, its dashboards, data sources and the shared folder
"vLLM P/D" stay, prometheus.yml only loses the block between the 面板二 markers.

A folder whose deploy.env has CLUSTER is one cluster of this version (bin/panel2_cluster.sh remove): its names come from
scripts/names.py, and only that cluster's puller, SSH connections and Grafana objects are touched. The Grafana plugin is
removed only when no other cluster is registered.
"""
import argparse
import base64
import hashlib
import json
import os
import pathlib
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PACK = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PACK / "scripts"))
import names as names_mod  # noqa: E402

PLUGIN = "grafana-clickhouse-datasource"
NODE_DIR = "vllm-timing-node"
RESULTS = {"ok": 0, "fail": 0}


def out(kind, msg):
    if kind == "FAIL":
        RESULTS["fail"] += 1
    elif kind == "OK":
        RESULTS["ok"] += 1
    print("%-5s %s" % (kind, msg))


def run(argv, input_text=None, timeout=120):
    try:
        p = subprocess.run(argv, input=input_text, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


# ---- the earlier version's settings ----------------------------------------------------------------------------------

class Old:
    def __init__(self, folder):
        self.dir = pathlib.Path(folder).resolve()
        envf = self.dir / "deploy.env"
        if not envf.is_file():
            raise SystemExit("%s 里没有 deploy.env：这里要写旧版的目录（解开旧 tar 的那个目录）" % self.dir)
        if (self.dir / "mock" / "mockgen.py").is_file():
            self.version, self.mock_pattern = "59604f30", "mock/mockgen.py"
        elif (self.dir / "mock" / "pd032_mock.py").is_file():
            self.version, self.mock_pattern = "a87f4d74", "mock/pd032_mock.py"
        else:
            raise SystemExit("%s 不像上一版面板二的目录：没有 mock/mockgen.py（59604f30）或 mock/pd032_mock.py（a87f4d74）" % self.dir)
        # In an empty environment: names another cluster's scripts exported must not fill what this deploy.env leaves blank.
        code, text, err = run_env(["bash", "-c", 'set -a; . "$1" >/dev/null 2>&1; set +a; env -0', "_", str(envf)],
                                  {"HOME": os.path.expanduser("~"), "PATH": os.environ.get("PATH", "/usr/bin:/bin")})
        if code != 0:
            raise SystemExit("读不了 %s：%s" % (envf, err.strip()))
        self.env = dict(x.split("=", 1) for x in text.split("\0") if "=" in x)
        raw = dict(re.findall(r"^([A-Z_0-9]+)=(.*)$", envf.read_text(encoding="utf-8"), re.M))
        g = self.env.get
        self.cluster = (g("CLUSTER") or "").strip()
        if self.cluster:
            try:
                n = names_mod.Names(self.env)
                n.get("TIMING_HOME")
            except names_mod.NamesError as e:
                raise SystemExit("错误：%s" % e)
            v = n.get
        else:
            if self.dir == PACK.resolve():
                raise SystemExit("%s 是这一版自己的目录：这里要写上一版改名后的目录" % self.dir)
            # A folder from before clusters: what it left blank had the names that version used.
            home = names_mod.home_dir(self.env)
            v = lambda k: g(k) or names_mod.expand_home(names_mod.EARLIER.get(k, ""), home)  # noqa: E731
        self.ds_uid, self.board_uid = v("GRAFANA_DS_UID"), v("GRAFANA_BOARD_UID")
        self.mark_b, self.mark_rb = "# BEGIN vllm-timing (%s)" % v("PROMETHEUS_MARK"), "# BEGIN vllm-timing rules (%s)" % v("PROMETHEUS_MARK")
        self.jobs = (v("PULLER_JOB"), v("CENTRAL_JOB"))
        self.rules_path, self.rules_group = "/prometheus/" + v("RULES_FILE"), v("RULES_GROUP")
        self.ssh_prefix = v("SSH_SOCKET_PREFIX")
        self.timing_home = v("TIMING_HOME")
        # On the nodes, $HOME in TIMING_HOME is the SSH user's home there.
        self.node_home = (raw.get("TIMING_HOME", "").strip("'\"") or "$HOME/vllm-timing").replace("${HOME}", "$HOME")
        self.data_dir = g("CLICKHOUSE_DATA_DIR") or ""
        self.pull_dir = g("PULL_DIR") or os.path.join(self.data_dir or self.timing_home, "pull")
        self.ch = v("CLICKHOUSE_CONTAINER")
        self.central = v("CENTRAL_CONTAINER")
        self.ch_image = g("CLICKHOUSE_IMAGE") or "clickhouse/clickhouse-server:26.8.36.3"
        self.central_image = g("CENTRAL_IMAGE") or "timberio/vector:0.61.0-debian"
        self.ports = [("ClickHouse HTTP", int(v("CLICKHOUSE_HTTP_PORT"))), ("拉取程序状态", int(v("PULLER_METRICS_PORT")))]
        self.grafana = (g("GRAFANA_URL") or "http://localhost:3000").rstrip("/")
        self.grafana_auth = "Basic " + base64.b64encode(("%s:%s" % (g("GRAFANA_USER") or "admin", g("GRAFANA_PASSWORD") or "")).encode()).decode()
        self.grafana_container = g("GRAFANA_CONTAINER") or "vllm-pd-grafana"
        self.prom = v("PROMETHEUS_URL").rstrip("/")
        self.prom_container = v("PROMETHEUS_CONTAINER")
        self.prom_yml = v("PROMETHEUS_YML") or ""
        self.ssh_user = g("SSH_USER") or ""
        self.ssh_opts = shlex.split(g("SSH_OPTS") or "")
        inv = json.loads((self.dir / "inventory.json").read_text(encoding="utf-8"))
        hosts = [inv.get("proxy", {}).get("host")] + [a.rsplit(":", 1)[0] for a in inv["prefill"]["api_servers"] + inv["decode"]["api_servers"]]
        self.nodes = []
        for h in hosts:
            if h and h not in self.nodes:
                self.nodes.append(h)
        self.note = self.dir / "uninstall-panel1-before.json"
        # a87f4d74 on the nodes: the three files in NODE_CONF_DIR, the filter's (or the mock's) files in NODE_LOG_DIR.
        self.node_mode = (g("NODE_MODE") or "mock").strip()
        self.node_conf = (raw.get("NODE_CONF_DIR", "").strip("'\"") or "$HOME/vllm-pd-diag/conf").replace("${HOME}", "$HOME")
        self.node_log = (raw.get("NODE_LOG_DIR", "").strip("'\"") or "$HOME/vllm-pd-diag/logs").replace("${HOME}", "$HOME")

    def merge_env(self):
        """The environment for scripts/prometheus_merge.py: this folder's settings, with its names written out."""
        env = dict(self.env)
        env.update(CLUSTER=self.cluster or "earlier", PROMETHEUS_MARK=self.mark_b[len("# BEGIN vllm-timing ("):-1],
                   RULES_FILE=self.rules_path[len("/prometheus/"):], PULLER_JOB=self.jobs[0], CENTRAL_JOB=self.jobs[1])
        return env

    def volumes(self):
        return [] if self.data_dir else [self.ch + "-data", self.ch + "-logs", self.central + "-data"]

    def data_paths(self):
        return [os.path.join(self.data_dir, x) for x in ("data", "logs", "vector")] if self.data_dir else []


# ---- helpers ---------------------------------------------------------------------------------------------------------

def docker_names(kind):
    code, text, _ = run(["docker", "ps", "-a", "--format", "{{.Names}}"] if kind == "container" else ["docker", "volume", "ls", "-q"])
    return set(text.split()) if code == 0 else set()


def run_env(argv, env, timeout=120):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def http(o, method, url, auth=None, body=None, timeout=10):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None)
    if auth:
        req.add_header("Authorization", auth)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw.strip().startswith((b"{", b"[")) else raw.decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def listening(port):
    s = socket.socket()
    s.settimeout(1)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def ssh(o, addr, script, timeout=60):
    who = "%s@%s" % (o.ssh_user, addr) if o.ssh_user else addr
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new", "-o", "LogLevel=ERROR"] + o.ssh_opts + [who, "bash -s"]
    return run(argv, input_text=script, timeout=timeout)


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def puller_pids(o):
    """The puller's restart loop and the puller itself: from its pid files, and any process still running its script."""
    pids = []
    for f in ("loop.pid", "puller.pid"):
        p = pathlib.Path(o.timing_home, "puller", f)
        if p.is_file():
            v = p.read_text().strip()
            if v.isdigit() and pid_alive(v):
                pids.append(int(v))
    for pid, cmd in puller_processes():
        if pid in pids or pid == os.getpid():
            continue
        if o.cluster:
            # Only the puller started from this cluster's folder.
            if not re.search(r"(^| )%s/scripts/puller\.py( |$)" % re.escape(str(o.dir)), cmd):
                continue
        else:
            # An earlier version, whose folder was renamed after it started: any panel 2 puller, but none of a registered cluster.
            if not re.search(r"vllm-pd-grafana-panel2[^ ]*/scripts/puller\.py", cmd) or any(
                    re.search(r"(^| )%s/scripts/puller\.py( |$)" % re.escape(d), cmd) for d in cluster_dirs()):
                continue
        pids.append(pid)
    return pids


def puller_processes():
    code, text, _ = run(["pgrep", "-u", str(os.getuid()), "-af", "/scripts/puller.py"])
    out = []
    for line in text.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if pid.isdigit():
            out.append((int(pid), cmd))
    return out


def cluster_dirs():
    try:
        reg = names_mod.load_registry()
    except (names_mod.NamesError, ValueError, OSError):
        return []
    return [e["panel2"]["dir"] for e in reg["clusters"].values() if (e.get("panel2") or {}).get("dir")]


def ssh_masters(o):
    """SSH connection sharing left by the puller and bin/nodes.sh: this cluster's folders <prefix>XXXXXXXX in the temp folder
    (XXXXXXXX: the 8 characters mkdtemp adds) and their master processes. Other clusters' prefixes are longer, so they never match."""
    tmp = tempfile.gettempdir()
    own = re.compile(r"^%s[a-z0-9_]{8}$" % re.escape(o.ssh_prefix))
    dirs = [str(p) for p in pathlib.Path(tmp).glob(o.ssh_prefix + "*")
            if own.match(p.name) and p.is_dir() and p.owner() == pathlib.Path.home().owner()] if os.path.isdir(tmp) else []
    code, text, _ = run(["pgrep", "-u", str(os.getuid()), "-af", "^ssh: "])
    master = re.compile(r"^ssh: %s/%s[a-z0-9_]{8}/" % (re.escape(tmp), re.escape(o.ssh_prefix)))
    pids = []
    for line in text.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if pid.isdigit() and master.match(cmd):
            pids.append(int(pid))
    return dirs, pids


NODE_STATE = r'''
H={home}
m=$(pgrep -u "$(id -u)" -f '{pattern}' | tr '\n' ' ')
echo "mock=$m"
[ -d ~/{nd} ] && echo "prog=yes" || echo "prog=no"
[ -d {conf} ] && echo "conf=yes" || echo "conf=no"
n=$(find {log} -mindepth 2 -maxdepth 2 -type f -name '*.log' 2>/dev/null | wc -l | tr -d ' '); echo "logs=$n"
if [ -d "$H" ]; then echo "home=yes $(du -sh "$H" 2>/dev/null | cut -f1) $(find "$H" -name '*.jsonl*' 2>/dev/null | wc -l | tr -d ' ')"; else echo "home=no"; fi
[ -f "$H/.vllm-timing-home" ] && echo "marker=yes" || echo "marker=no"
'''


def node_state(o, addr):
    code, text, err = ssh(o, addr, NODE_STATE.format(home=o.node_home, nd=NODE_DIR, pattern=o.mock_pattern, conf=o.node_conf, log=o.node_log))
    if code != 0:
        return None, (err or text).strip()[:120]
    st = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    return st, ""


def panel1_snapshot(o):
    """Panel 1 as it is now: every dashboard but panel 2's with its version, and Prometheus's targets but panel 2's."""
    code, found = http(o, "GET", o.grafana + "/api/search?type=dash-db&limit=5000", o.grafana_auth)
    boards = {}
    if code == 200:
        for b in found:
            if b.get("uid") == o.board_uid:
                continue
            c, full = http(o, "GET", o.grafana + "/api/dashboards/uid/" + b["uid"], o.grafana_auth)
            boards[b["uid"]] = {"title": b.get("title"), "version": (full.get("dashboard", {}) if c == 200 else {}).get("version")}
    code, t = http(o, "GET", o.prom + "/api/v1/targets?state=active")
    targets = {}
    if code == 200:
        for x in t["data"]["activeTargets"]:
            job = x["labels"].get("job", "")
            if job not in o.jobs:
                targets["%s %s" % (job, x["labels"].get("instance", ""))] = x.get("health")
    code, ds = http(o, "GET", o.grafana + "/api/datasources", o.grafana_auth)
    sources = sorted(d["uid"] for d in ds if d.get("uid") != o.ds_uid) if code == 200 else None
    return {"grafana_ok": bool(boards), "boards": boards, "targets": targets, "datasources": sources}


# ---- plan -------------------------------------------------------------------------------------------------------------

def plan(o):
    print("旧版目录：%s；节点 %d 台：%s" % (o.dir, len(o.nodes), " ".join(o.nodes)))
    print("\n[监控机：会在 remove 一步删除]")
    names = docker_names("container")
    for c in (o.central, o.ch):
        out("INFO", "容器 %s：%s" % (c, "有" if c in names else "没有"))
    pids = puller_pids(o)
    out("INFO", "拉取程序：%s" % ("运行中，进程 %s" % " ".join(map(str, pids)) if pids else "没有运行"))
    dirs, masters = ssh_masters(o)
    out("INFO", "SSH 连接复用：目录 %d 个，主连接进程 %d 个（%s）" % (len(dirs), len(masters), tempfile.gettempdir()))
    for label, port in o.ports:
        out("INFO", "端口 %d（%s）：%s" % (port, label, "在监听" if listening(port) else "空闲"))
    out("INFO", "拉取程序的状态目录 %s：%s" % (os.path.join(o.timing_home, "puller"), "有" if os.path.isdir(os.path.join(o.timing_home, "puller")) else "没有"))
    print("\n[Grafana、Prometheus：只删面板二的部分，会在 remove 一步删除]")
    code, _ = http(o, "GET", o.grafana + "/api/dashboards/uid/" + o.board_uid, o.grafana_auth)
    out("INFO", "Grafana 看板 %s：%s" % (o.board_uid, {200: "有", 404: "没有"}.get(code, "查不了（HTTP %s）" % code)))
    code, _ = http(o, "GET", o.grafana + "/api/datasources/uid/" + o.ds_uid, o.grafana_auth)
    out("INFO", "Grafana 数据源 %s：%s" % (o.ds_uid, {200: "有", 404: "没有"}.get(code, "查不了（HTTP %s）" % code)))
    if o.prom_yml and os.path.isfile(o.prom_yml):
        text = open(o.prom_yml, encoding="utf-8").read()
        out("INFO", "%s 里面板二的抓取任务和规则：%s" % (o.prom_yml, "有" if o.mark_b in text or o.mark_rb in text else "没有"))
    else:
        out("INFO", "PROMETHEUS_YML=%s 不存在" % o.prom_yml)
    code, _, _ = run(["docker", "exec", o.prom_container, "test", "-f", o.rules_path])
    out("INFO", "Prometheus 容器里的规则文件 %s：%s" % (o.rules_path, "有" if code == 0 else "没有"))
    print("\n[数据：只在 purge-data 一步删除，删了不能恢复]")
    vols = docker_names("volume")
    for v in o.volumes():
        out("INFO", "数据卷 %s：%s" % (v, "有" if v in vols else "没有"))
    for d in o.data_paths():
        out("INFO", "目录 %s：%s" % (d, "有" if os.path.isdir(d) else "没有"))
    out("INFO", "拉取镜像 %s：%s" % (o.pull_dir, "有" if os.path.isdir(o.pull_dir) else "没有"))
    print("\n[节点：mock 在 remove 一步停掉并删程序，日志在 purge-data 一步删]")
    for a in o.nodes:
        st, err = node_state(o, a)
        if st is None:
            out("FAIL", "%s：SSH 连不上（%s）" % (a, err))
            continue
        home = st.get("home", "no").split()
        out("INFO", "%s：mock 进程 %s；程序目录 ~/%s %s；%s %s" % (
            a, st.get("mock", "").strip() or "没有", NODE_DIR, "有" if st.get("prog") == "yes" else "没有", o.node_home,
            ("有，%s，日志 %s 个文件" % (home[1], home[2])) if home[0] == "yes" else "没有"))
    print("\n[保留，不动]")
    for line in ("面板一的 Prometheus、Grafana 容器和它们的配置、看板、数据源：面板二只在它们里面加过自己的东西",
                 "Grafana 目录「vLLM P/D」：面板一的看板也在里面",
                 "Grafana 的 ClickHouse 插件 %s：新版也用它；删除要重启 Grafana（remove 加 --remove-plugin）" % PLUGIN,
                 "docker 网络 %s：是面板一的" % (o.env.get("DOCKER_NETWORK") or "vllm-pd-monitor"),
                 "%s.before-panel2：第一次装面板二之前的 prometheus.yml 备份，新版沿用" % o.prom_yml,
                 "镜像 %s、%s：新版用同样的两个；要删由你决定（见教程）" % (o.ch_image, o.central_image),
                 "%s：自带的 Python，新版也用；整个卸载时再删" % os.path.join(o.timing_home, "python"),
                 "SSH 密钥、节点上 ~/.ssh/authorized_keys 和监控机 ~/.ssh/known_hosts 里的记录：新版用同样的 SSH 登录"):
        print("      " + line)
    snap = panel1_snapshot(o)
    o.note.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n面板一现在的状态记在 %s：看板 %d 个，Prometheus 目标 %d 个（check 一步拿它比较）" % (o.note, len(snap["boards"]), len(snap["targets"])))
    return 0


# ---- remove -------------------------------------------------------------------------------------------------------------

def remove(o, remove_plugin=False):
    if not o.note.is_file():
        raise SystemExit("先执行 plan：它记下面板一现在的状态，check 一步要用")
    # 1. the puller, then the SSH masters it shared connections through
    for p in puller_pids(o):
        try:
            os.kill(p, 15)
        except OSError:
            pass
    time.sleep(2)
    left = puller_pids(o)
    for p in left:
        try:
            os.kill(p, 9)
        except OSError:
            pass
    for f in ("loop.pid", "puller.pid"):
        try:
            pathlib.Path(o.timing_home, "puller", f).unlink()
        except OSError:
            pass
    out("OK", "拉取程序已停止")
    dirs, masters = ssh_masters(o)
    for d in dirs:
        for s in pathlib.Path(d).iterdir():
            run(["ssh", "-o", "ControlPath=%s" % s, "-O", "exit", "x"], timeout=10)
    for p in ssh_masters(o)[1]:
        try:
            os.kill(p, 15)
        except OSError:
            pass
    for d in dirs:
        run(["rm", "-rf", d])
    out("OK", "SSH 连接复用已关闭：目录 %d 个，主连接 %d 个" % (len(dirs), len(masters)))
    # 2. the nodes: stop the mock and remove its program; its logs stay until purge-data
    for a in o.nodes:
        script = ('H={home}\nif [ -f "$H/run/mock.pid" ]; then kill "$(cat "$H/run/mock.pid")" 2>/dev/null; fi\n'
                  '[ -x ~/{nd}/bin/node_mock.sh ] && (cd ~/{nd} && bin/node_mock.sh stop) >/dev/null 2>&1\n'
                  'pkill -u "$(id -u)" -f "{pat}" 2>/dev/null; sleep 1; pkill -9 -u "$(id -u)" -f "{pat}" 2>/dev/null\n'
                  'rm -rf ~/{nd} "$H/run"\n').format(home=o.node_home, nd=NODE_DIR, pat=o.mock_pattern)
        if o.version == "a87f4d74" and o.node_mode == "mock":
            # In the intranet the three node files are this pack's own; on a vLLM machine they belong to vLLM's launch.
            script += "rm -rf %s\n" % o.node_conf
        script += "echo done\n"
        code, text, err = ssh(o, a, script)
        out("OK" if code == 0 and "done" in text else "FAIL", "%s：mock 已停止，程序目录 ~/%s 已删除%s" % (a, NODE_DIR, "" if code == 0 else "（%s）" % (err or text).strip()[:100]))
    # 3. Grafana: panel 2's dashboard and data source; the folder stays (panel 1's dashboards are in it)
    for path, what in (("/api/dashboards/uid/" + o.board_uid, "看板"), ("/api/datasources/uid/" + o.ds_uid, "数据源")):
        code, body = http(o, "DELETE", o.grafana + path, o.grafana_auth)
        out("OK" if code in (200, 404) else "FAIL", "Grafana %s：%s" % (what, "已删除" if code == 200 else "本来就没有" if code == 404 else "删不掉（HTTP %s %s）" % (code, str(body)[:80])))
    # 4. Prometheus: the marked block in prometheus.yml, the rules file, then a reload (no restart)
    prom_running = o.prom_container in running_containers()
    if o.prom_yml and os.path.isfile(o.prom_yml):
        # prometheus_merge.py takes the markers from the environment: give it the old folder's settings.
        code, text, err = run_env([sys.executable, str(PACK / "scripts" / "prometheus_merge.py"), "remove", "--file", o.prom_yml], o.merge_env())
        out("OK" if code == 0 else "FAIL", "prometheus.yml：%s" % (text.strip() or err.strip())[:120])
    if prom_running:
        run(["docker", "exec", "-u", "root", o.prom_container, "rm", "-f", o.rules_path])
        code, body = http(o, "POST", o.prom + "/-/reload")
        out("OK" if code == 200 else "FAIL", "Prometheus 重新加载配置%s" % ("" if code == 200 else "失败（HTTP %s）→ 确认面板一的 Prometheus 带 --web.enable-lifecycle，或在没人看的时候执行 docker restart %s" % (code, o.prom_container)))
    elif o.cluster:
        out("INFO", "本集群的 Prometheus 容器 %s 不在运行（面板一已经先卸了），跳过规则文件和重新加载" % o.prom_container)
    else:
        out("FAIL", "Prometheus 容器 %s 不在运行，规则文件没有删、配置没有重新加载" % o.prom_container)
    # 5. the collector first, stopped so it finishes what it holds, then ClickHouse; -v removes anonymous volumes only, the named data volumes stay
    for c in (o.central, o.ch):
        if c in docker_names("container"):
            run(["docker", "stop", "-t", "60", c], timeout=90)
            code, _, err = run(["docker", "rm", "-fv", c])
            out("OK" if code == 0 else "FAIL", "容器 %s 已删除%s" % (c, "" if code == 0 else "：" + err.strip()[:80]))
        else:
            out("OK", "容器 %s 本来就没有" % c)
    others = names_mod.others(names_mod.load_registry(), o.cluster, "panel2") if remove_plugin else []
    if remove_plugin and others:
        out("FAIL", "Grafana 插件 %s 没有删：集群 %s 还在用它（插件和 Grafana 是共用的，最后一个集群卸载时才删）" % (PLUGIN, " ".join(others)))
    elif remove_plugin:
        run(["docker", "exec", "-u", "root", o.grafana_container, "rm", "-rf", "/var/lib/grafana/plugins/" + PLUGIN])
        code, _, err = run(["docker", "restart", o.grafana_container], timeout=120)
        out("OK" if code == 0 else "FAIL", "Grafana 插件 %s 已删除，Grafana 重启了一次" % PLUGIN)
    print("RESULT: %s" % ("OK" if RESULTS["fail"] == 0 else "FAIL（%d 项）" % RESULTS["fail"]))
    print("数据还在：ClickHouse 的数据、拉取镜像 %s、节点上 %s 里的日志。要删执行 purge-data，要留着就跳过那一步。" % (
        o.pull_dir, o.node_log if o.version == "a87f4d74" else o.node_home))
    return 1 if RESULTS["fail"] else 0


# ---- purge-data -----------------------------------------------------------------------------------------------------------

def purge(o, backup=None):
    names = docker_names("container")
    if o.ch in names or o.central in names:
        raise SystemExit("容器 %s 或 %s 还在：先执行 remove" % (o.ch, o.central))
    vols = [v for v in o.volumes() if v in docker_names("volume")]
    paths = [d for d in o.data_paths() if os.path.isdir(d)]
    if backup:
        b = pathlib.Path(backup).resolve()
        b.mkdir(parents=True, exist_ok=True)
        # The files belong to ClickHouse's user inside the container, so the archive is written from a container of the same image.
        sources = [("volume", v, "%s:/x:ro" % v) for v in vols if not v.startswith(o.central)] + \
                  [("folder", d, "%s:/x:ro" % d) for d in paths if not d.endswith("/vector")]
        for kind, src, mount in sources:
            name = "%s.tar.gz" % (src if kind == "volume" else "clickhouse-" + os.path.basename(src))
            code, _, err = run(["docker", "run", "--rm", "-v", mount, "-v", "%s:/b" % b, "--entrypoint", "tar", o.ch_image,
                                "-czf", "/b/" + name, "-C", "/x", "."], timeout=7200)
            if code != 0:
                raise SystemExit("备份 %s 失败，什么都没有删：%s" % (src, err.strip()[:200]))
            h = hashlib.sha256()
            with open(b / name, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            (b / (name + ".sha256")).write_text("%s  %s\n" % (h.hexdigest(), name))
            out("OK", "已备份 %s 到 %s（%.1f MB）" % (src, b / name, (b / name).stat().st_size / 1e6))
    for v in vols:
        code, _, err = run(["docker", "volume", "rm", v])
        out("OK" if code == 0 else "FAIL", "数据卷 %s 已删除%s" % (v, "" if code == 0 else "：" + err.strip()[:80]))
    if paths:
        code, _, err = run(["docker", "run", "--rm", "-v", "%s:/x" % o.data_dir, "--entrypoint", "sh", o.ch_image, "-c", "rm -rf /x/data /x/logs /x/vector"], timeout=1800)
        out("OK" if code == 0 else "FAIL", "%s 下的 data、logs、vector 已删除%s" % (o.data_dir, "" if code == 0 else "：" + err.strip()[:80]))
    if os.path.isdir(o.pull_dir):
        if os.path.isfile(os.path.join(o.pull_dir, ".made-by-vllm-timing")):
            code, _, err = run(["rm", "-rf", o.pull_dir], timeout=1800)
            out("OK" if code == 0 else "FAIL", "拉取镜像 %s 已删除" % o.pull_dir)
        else:
            out("FAIL", "%s 不是旧版建的目录（没有 .made-by-vllm-timing），没有删" % o.pull_dir)
    run(["rm", "-rf", os.path.join(o.timing_home, "puller")])
    out("OK", "拉取程序的状态目录 %s 已删除" % os.path.join(o.timing_home, "puller"))
    for a in o.nodes:
        script = 'H={home}\nif [ -d "$H" ]; then [ -f "$H/.vllm-timing-home" ] || {{ echo notours; exit 0; }}; rm -rf "$H"; fi\n'.format(home=o.node_home)
        if o.version == "a87f4d74" and o.node_mode == "vllm":
            # Known issue 11 (3): vLLM keeps writing the newest file of each process (the filter opens a new one only
            # when there is none or it is full), so of each <instance>/<role>.<start>.<pid> only the files before the
            # highest <seq> go; the one being written stays and has to be removed by hand after the filter is unhooked.
            script = 'L={log}\n'.format(log=o.node_log) + VLLM_LOG_CLEAN
            what = "%s 里过滤器写的旧文件" % o.node_log
        elif o.version == "a87f4d74":
            # Only the files the filter or the mock wrote (<instance>/<role>.<start>.<pid>.<seq>.log), then the empty folders.
            script = ('L={log}\nif [ -d "$L" ]; then find "$L" -mindepth 2 -maxdepth 2 -type f \\( -name "api.*.log" -o -name "engine.*.log" '
                      '-o -name "worker.*.log" -o -name "pool.*.log" \\) -delete; find "$L" -mindepth 1 -maxdepth 1 -type d -empty -delete; '
                      '{rmlog}fi\n').format(log=o.node_log, rmlog='rmdir "$L" 2>/dev/null; ' if o.node_mode == "mock" else "")
            what = "%s 里过滤器和 mock 写的文件" % o.node_log
        else:
            what = "%s（mock 日志和自带的 Python）" % o.node_home
        script += "echo done\n"
        code, text, err = ssh(o, a, script)
        ok = code == 0 and "done" in text
        kept = re.findall(r"^kept\t(\d+)\t(\d+)$", text, re.M)
        if ok and kept:
            what += "（删了 %s 个；留下正在写的 %s 个：vLLM 还开着它们，摘掉过滤器、重启 vLLM 以后再手删）" % (kept[-1][0], kept[-1][1])
        out("OK" if ok else "FAIL", "%s：%s" % (a, "%s已删除" % what if ok else
                                          "%s 不是旧版建的目录，没有删" % o.node_home if "notours" in text else "SSH 失败：%s" % (err or text).strip()[:100]))
    print("RESULT: %s" % ("OK" if RESULTS["fail"] == 0 else "FAIL（%d 项）" % RESULTS["fail"]))
    return 1 if RESULTS["fail"] else 0


# vllm mode (known issue 11 (3)): of each process's files <instance>/<role>.<start>.<pid>.<seq>.log only those before the
# highest <seq> are deleted; the last one is the file the filter is writing. Prints "kept <deleted> <kept>".
VLLM_LOG_CLEAN = r'''
n_del=0; n_keep=0
if [ -d "$L" ]; then
  T=$(mktemp)
  find "$L" -mindepth 2 -maxdepth 2 -type f \( -name "api.*.log" -o -name "engine.*.log" -o -name "worker.*.log" -o -name "pool.*.log" \) \
    | awk '{ k = $0; sub(/\.[0-9]+\.log$/, "", k); s = $0; sub(/\.log$/, "", s); sub(/.*\./, "", s)
             if (!(k in best) || s + 0 > best[k] + 0) { if (k in path) print "del\t" path[k]; best[k] = s; path[k] = $0 }
             else print "del\t" $0 }
           END { for (k in path) print "keep\t" path[k] }' > "$T"
  while IFS="$(printf '\t')" read -r what f; do
    if [ "$what" = del ]; then rm -f "$f" && n_del=$((n_del + 1)); else n_keep=$((n_keep + 1)); fi
  done < "$T"
  rm -f "$T"
  find "$L" -mindepth 1 -maxdepth 1 -type d -empty -delete
fi
printf 'kept\t%s\t%s\n' "$n_del" "$n_keep"
'''


# ---- check ----------------------------------------------------------------------------------------------------------------

def check(o):
    names = docker_names("container")
    for c in (o.central, o.ch):
        out("OK" if c not in names else "FAIL", "没有容器 %s" % c)
    pids = puller_pids(o)
    out("OK" if not pids else "FAIL", "拉取程序没有在运行" + ("" if not pids else "（进程 %s）" % " ".join(map(str, pids))))
    dirs, masters = ssh_masters(o)
    out("OK" if not dirs and not masters else "FAIL", "没有留下 SSH 连接复用的目录和主连接（%s/%s*）" % (tempfile.gettempdir(), o.ssh_prefix))
    for label, port in o.ports:
        out("OK" if not listening(port) else "FAIL", "端口 %d（%s）没有程序在监听" % (port, label))
    out("OK" if not os.path.isdir(os.path.join(o.timing_home, "puller")) or not any(pathlib.Path(o.timing_home, "puller").glob("*.pid")) else "FAIL",
        "没有拉取程序的进程文件（%s）" % os.path.join(o.timing_home, "puller"))
    vols = [v for v in o.volumes() if v in docker_names("volume")]
    paths = [d for d in o.data_paths() if os.path.isdir(d)]
    kept = vols + paths + ([o.pull_dir] if os.path.isdir(o.pull_dir) else [])
    out("OK" if not kept else "INFO", "旧数据：%s" % ("已删除" if not kept else "还在 %s（没有执行 purge-data 时是这样）" % " ".join(kept)))
    code, _ = http(o, "GET", o.grafana + "/api/dashboards/uid/" + o.board_uid, o.grafana_auth)
    out("OK" if code == 404 else "FAIL", "Grafana 里没有面板二的看板（HTTP %s）" % code)
    code, _ = http(o, "GET", o.grafana + "/api/datasources/uid/" + o.ds_uid, o.grafana_auth)
    out("OK" if code == 404 else "FAIL", "Grafana 里没有面板二的数据源（HTTP %s）" % code)
    code, plugins = http(o, "GET", o.grafana + "/api/plugins?type=datasource", o.grafana_auth)
    has_plugin = code == 200 and any(p.get("id") == PLUGIN for p in plugins)
    out("INFO", "ClickHouse 插件：%s" % ("保留（新版要用）" if has_plugin else "已删除"))
    if o.prom_yml and os.path.isfile(o.prom_yml):
        text = open(o.prom_yml, encoding="utf-8").read()
        out("OK" if o.mark_b not in text and o.mark_rb not in text else "FAIL", "prometheus.yml 里没有面板二的抓取任务和规则")
    prom_running = o.prom_container in running_containers()
    if not prom_running and o.cluster:
        out("INFO", "本集群的 Prometheus 容器 %s 不在运行（面板一已经先卸了），不查 Prometheus" % o.prom_container)
    else:
        check_prometheus(o)
    check_panel1(o, prom_running or not o.cluster)
    code, _ = http(o, "GET", o.grafana + "/api/health")
    out("OK" if code == 200 else "FAIL", "Grafana 正常（/api/health）")
    check_nodes(o)
    print("RESULT: %s" % ("OK" if RESULTS["fail"] == 0 else "FAIL（%d 项）" % RESULTS["fail"]))
    return 1 if RESULTS["fail"] else 0


def running_containers():
    code, text, _ = run(["docker", "ps", "--format", "{{.Names}}"])
    return set(text.split()) if code == 0 else set()


def check_prometheus(o):
    code, _, _ = run(["docker", "exec", o.prom_container, "test", "-f", o.rules_path])
    out("OK" if code != 0 else "FAIL", "Prometheus 容器里没有 %s" % o.rules_path)
    code, t = http(o, "GET", o.prom + "/api/v1/targets?state=active")
    jobs = sorted({x["labels"].get("job") for x in t["data"]["activeTargets"]}) if code == 200 else None
    out("OK" if jobs is not None and not set(o.jobs) & set(jobs) else "FAIL", "Prometheus 不再抓面板二的两个任务（现在的任务：%s）" % (" ".join(jobs) if jobs else "查不了"))
    code, r = http(o, "GET", o.prom + "/api/v1/rules")
    groups = [g["name"] for g in r["data"]["groups"]] if code == 200 else None
    out("OK" if groups is not None and o.rules_group not in groups else "FAIL", "Prometheus 里没有面板二的告警规则")
    code, _ = http(o, "GET", o.prom + "/-/ready")
    out("OK" if code == 200 else "FAIL", "Prometheus 正常（/-/ready）")


def check_panel1(o, with_targets):
    # Panel 1 as noted by plan
    before = json.loads(o.note.read_text(encoding="utf-8")) if o.note.is_file() else None
    now = panel1_snapshot(o)
    if before is None:
        out("FAIL", "没有 %s：plan 一步没有执行，没法和卸载之前比" % o.note)
    else:
        same = {k: v for k, v in before["boards"].items() if now["boards"].get(k) == v}
        out("OK" if len(same) == len(before["boards"]) and before["boards"] else "FAIL",
            "面板一的看板 %d 个都在，版本号没变" % len(before["boards"]) if len(same) == len(before["boards"]) else
            "面板一的看板有变化：%s" % " ".join(sorted(set(before["boards"]) - set(same))))
        out("OK" if now["datasources"] == before["datasources"] else "FAIL", "面板一的数据源没变（%s）" % " ".join(now["datasources"] or []))
        if with_targets:
            miss = [k for k in before["targets"] if k not in now["targets"]]
            down = [k for k, h in now["targets"].items() if h != "up" and before["targets"].get(k) == "up"]
            out("OK" if not miss and not down else "FAIL", "Prometheus 抓面板一的目标和卸载之前一样（%d 个），之前是 up 的现在也是 up%s" % (
                len(now["targets"]), "" if not miss and not down else "（少了 %s；变成不是 up 的 %s）" % (" ".join(miss[:5]), " ".join(down[:5]))))


def check_nodes(o):
    for a in o.nodes:
        st, err = node_state(o, a)
        if st is None:
            out("FAIL", "%s：SSH 连不上（%s）" % (a, err))
            continue
        bad = []
        if st.get("mock", "").strip():
            bad.append("mock 还在运行")
        if st.get("prog") == "yes":
            bad.append("还有 ~/%s" % NODE_DIR)
        if o.version == "a87f4d74" and o.node_mode == "mock" and st.get("conf") == "yes":
            bad.append("还有 %s" % o.node_conf)
        out("OK" if not bad else "FAIL", "%s：没有 mock 进程，没有程序目录%s" % (a, "" if not bad else "（%s）" % "，".join(bad)))
        if o.version == "a87f4d74" and st.get("logs", "0") != "0":
            out("INFO", "%s：%s 里还有 %s 个日志文件（%s）" % (a, o.node_log, st.get("logs"), "vllm 模式下 purge-data 留下各进程正在写的文件，没有执行 purge-data 时全部都在"
                                                       if o.node_mode == "vllm" else "没有执行 purge-data 时是这样"))
        if st.get("home", "no").startswith("yes"):
            out("INFO", "%s：%s 还在（mock 日志，没有执行 purge-data 时是这样）" % (a, o.node_home))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("plan", "remove", "purge-data", "check"))
    ap.add_argument("old_dir", help="上一版改名后的目录，比如 ~/vllm-pd-grafana-panel2-a87f4d74")
    ap.add_argument("--remove-plugin", action="store_true", help="remove：连 Grafana 的 ClickHouse 插件一起删（Grafana 重启一次）")
    ap.add_argument("--backup", help="purge-data：先把 ClickHouse 的数据打包到这个目录")
    a = ap.parse_args()
    o = Old(os.path.expanduser(a.old_dir))
    if a.action == "plan":
        return plan(o)
    if a.action == "remove":
        return remove(o, a.remove_plugin)
    if a.action == "purge-data":
        return purge(o, a.backup)
    return check(o)


if __name__ == "__main__":
    sys.exit(main())
