#!/usr/bin/env python3
"""Names, ports and folders of one cluster, and the registry of the clusters on this monitoring machine.

One rule for both panels: a value written in deploy.env is used as written. A blank one is derived from
CLUSTER and PORT_BASE: names and UIDs get "-<CLUSTER>", titles get "（<CLUSTER>）", host ports are
PORT_BASE plus a fixed offset (ClickHouse +0, puller +1, Prometheus +2), folders are $HOME/<name>-<CLUSTER>.

The registry (~/.vllm-pd-clusters.json) keeps every container, port, folder, data volume and UID of each
installed cluster. Each step that makes a container, takes a port or writes to Grafana runs `check` first:
a value that another cluster has registered stops it, and so does a port that something else holds.
The cluster's own containers and puller do not count, so any step can be run again.

Panel 1 and panel 2 carry the same copy of this file.

Usage:
  names.py derive   [--panel panel2|panel1] [--env-file F] [--cluster C --port-base B --model-group G] [--format shell|json]
  names.py check    [--panel P] [--env-file F] [--dir D] [--inventory I] [--no-ports]
  names.py register [--panel P] [--env-file F] [--dir D] [--inventory I]
  names.py unregister --cluster C --panel P
  names.py alloc    --cluster C [--port-base B]
  names.py shared   [--env-from F] [--format shell|json]
  names.py inventory-check I [--cluster C]
  names.py others   --cluster C [--panel P]
  names.py list     [--format text|json]
  names.py adopt-values --env-file F
Without --env-file the values come from the environment (bin/lib.sh sources deploy.env first).
"""
import argparse
import contextlib
import datetime
import json
import os
import re
import shlex
import socket
import subprocess
import sys

CLUSTER_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
REGISTRY_FORMAT, REGISTRY_VERSION = "vllm-pd-clusters", 1
# Port blocks handed out by `alloc`: PORT_BASE is one of these, the cluster uses PORT_BASE .. PORT_BASE+2
# and the rest of the block stays free for later additions.
PORT_FIRST, PORT_LAST, PORT_STEP = 18100, 19990, 10
PANELS = ("panel2", "panel1")

# How each key is derived when deploy.env leaves it blank. c = CLUSTER, B = PORT_BASE.
#   name    base-c           title   base（c）        port   B + offset
#   home    ROOT/base/c      file    base-c.yml       mark   "base c"         prefix base + "c-"
# ROOT is the monitoring machine's folder for clusters: VLLM_PD_ROOT in deploy.env, else the registry's "root"
# (set by add --root), else $HOME.
RULES = {
    # panel 2: its own containers, ports, folders, Grafana objects, and its part of the cluster's Prometheus
    "PANEL2_DIR": ("panel2", "home", "vllm-pd-grafana-panel2"),
    "CLICKHOUSE_CONTAINER": ("panel2", "name", "vllm-pd-clickhouse"),
    "CLICKHOUSE_HTTP_PORT": ("panel2", "port", 0),
    "CENTRAL_CONTAINER": ("panel2", "name", "vllm-pd-vector"),
    "PULLER_METRICS_PORT": ("panel2", "port", 1),
    "TIMING_HOME": ("panel2", "home", "vllm-timing"),
    "GRAFANA_DS_UID": ("panel2", "name", "vllm-pd-clickhouse"),
    "GRAFANA_DS_NAME": ("panel2", "title", "vLLM ClickHouse"),
    "GRAFANA_BOARD_UID": ("panel2", "name", "vllm-pd-request"),
    "GRAFANA_BOARD_TITLE": ("panel2", "title", "面板二：请求时延拆解（P / D）"),
    "PROMETHEUS_MARK": ("panel2", "mark", "面板二"),
    "PULLER_JOB": ("panel2", "name", "vllm-timing-puller"),
    "CENTRAL_JOB": ("panel2", "name", "vllm-timing-central"),
    "RULES_FILE": ("panel2", "file", "vllm-timing-rules"),
    "RULES_GROUP": ("panel2", "name", "vllm-timing"),
    "SSH_SOCKET_PREFIX": ("panel2", "prefix", "vllm-ssh-"),
    # panel 1: the cluster's Prometheus, its data source and the two dashboards
    "PANEL1_DIR": ("panel1", "home", "vllm-pd-grafana-panel1"),
    "PROMETHEUS_CONTAINER": ("panel1", "name", "vllm-pd-prometheus"),
    "PROMETHEUS_PORT": ("panel1", "port", 2),
    "PROMETHEUS_VOLUME": ("panel1", "name", "vllm-pd-prometheus-data"),
    "PROMETHEUS_DS_UID": ("panel1", "name", "prometheus"),
    "PROMETHEUS_DS_NAME": ("panel1", "title", "Prometheus"),
    "PANEL1_P_UID": ("panel1", "name", "vllm-pd-prefill"),
    "PANEL1_D_UID": ("panel1", "name", "vllm-pd-decode"),
    "PANEL1_TITLE_SUFFIX": ("panel1", "title", ""),
}
# Derived from other values (after those are resolved), not from CLUSTER directly.
DEPENDENT = ("CLICKHOUSE_ADDR", "PULL_DIR", "PROMETHEUS_URL", "PROMETHEUS_YML", "GRAFANA_FOLDER")
PATH_KEYS = {"PANEL2_DIR", "TIMING_HOME", "PULL_DIR", "CLICKHOUSE_DATA_DIR", "PANEL1_DIR", "PROMETHEUS_YML"}
PORT_KEYS = {k for k, (_, kind, _) in RULES.items() if kind == "port"}
# What a panel's scripts read: its own keys plus the other panel's keys it refers to.
OUTPUT = {
    "panel2": [k for k, r in RULES.items() if r[0] == "panel2"]
    + ["CLICKHOUSE_ADDR", "PULL_DIR", "GRAFANA_FOLDER", "PROMETHEUS_CONTAINER", "PROMETHEUS_URL", "PROMETHEUS_YML",
       "PANEL1_P_UID", "PANEL1_D_UID"],
    "panel1": [k for k, r in RULES.items() if r[0] == "panel1"] + ["PROMETHEUS_URL", "PROMETHEUS_YML", "GRAFANA_FOLDER"],
}
ROOT_KEY = "VLLM_PD_ROOT"
INPUTS = ("CLUSTER", "MODEL_GROUP", "PORT_BASE", "CLICKHOUSE_DATA_DIR", ROOT_KEY)
# What a panel registers. Values of the same kind must differ between clusters.
KIND = {
    "PANEL2_DIR": "目录", "TIMING_HOME": "目录", "PULL_DIR": "目录", "CLICKHOUSE_DATA_DIR": "目录", "PANEL1_DIR": "目录",
    "PROMETHEUS_YML": "文件",
    "CLICKHOUSE_CONTAINER": "容器", "CENTRAL_CONTAINER": "容器", "PROMETHEUS_CONTAINER": "容器",
    "CLICKHOUSE_HTTP_PORT": "主机端口", "PULLER_METRICS_PORT": "主机端口", "PROMETHEUS_PORT": "主机端口",
    "VOLUMES": "数据卷", "PROMETHEUS_VOLUME": "数据卷",
    "GRAFANA_DS_UID": "Grafana 数据源 UID", "PROMETHEUS_DS_UID": "Grafana 数据源 UID",
    "GRAFANA_DS_NAME": "Grafana 数据源名", "PROMETHEUS_DS_NAME": "Grafana 数据源名",
    "GRAFANA_BOARD_UID": "Grafana 看板 UID", "PANEL1_P_UID": "Grafana 看板 UID", "PANEL1_D_UID": "Grafana 看板 UID",
    "SSH_SOCKET_PREFIX": "SSH 复用目录前缀",
}
REGISTERED = {
    "panel2": ["PANEL2_DIR", "CLICKHOUSE_CONTAINER", "CENTRAL_CONTAINER", "CLICKHOUSE_HTTP_PORT", "PULLER_METRICS_PORT",
               "TIMING_HOME", "PULL_DIR", "CLICKHOUSE_DATA_DIR", "VOLUMES", "GRAFANA_DS_UID", "GRAFANA_DS_NAME",
               "GRAFANA_BOARD_UID", "SSH_SOCKET_PREFIX", "PROMETHEUS_CONTAINER", "PROMETHEUS_YML", "PANEL1_P_UID", "PANEL1_D_UID"],
    "panel1": ["PANEL1_DIR", "PROMETHEUS_CONTAINER", "PROMETHEUS_PORT", "PROMETHEUS_VOLUME", "PROMETHEUS_DS_UID",
               "PROMETHEUS_DS_NAME", "PANEL1_P_UID", "PANEL1_D_UID", "PROMETHEUS_YML"],
}
OWN_CONTAINERS = {"panel2": ["CLICKHOUSE_CONTAINER", "CENTRAL_CONTAINER"], "panel1": ["PROMETHEUS_CONTAINER"]}
OWN_PORTS = {"panel2": ["CLICKHOUSE_HTTP_PORT", "PULLER_METRICS_PORT"], "panel1": ["PROMETHEUS_PORT"]}
# The values the packs used before clusters existed, as deploy.env writes them. `adopt-values` writes them into an
# existing deployment's deploy.env so that its names stay what they are; the uninstaller reads them for a folder
# whose deploy.env has no CLUSTER. They are not used to derive anything.
EARLIER = {
    "CLICKHOUSE_CONTAINER": "vllm-pd-clickhouse", "CLICKHOUSE_HTTP_PORT": "8123", "CLICKHOUSE_ADDR": "vllm-pd-clickhouse",
    "CENTRAL_CONTAINER": "vllm-pd-vector", "PULLER_METRICS_PORT": "9598", "TIMING_HOME": "$HOME/vllm-timing",
    "GRAFANA_DS_UID": "vllm-pd-clickhouse", "GRAFANA_DS_NAME": "vLLM ClickHouse", "GRAFANA_BOARD_UID": "vllm-pd-request",
    "GRAFANA_BOARD_TITLE": "面板二：请求时延拆解（P / D）", "PROMETHEUS_MARK": "面板二",
    "PULLER_JOB": "vllm-timing-puller", "CENTRAL_JOB": "vllm-timing-central", "RULES_FILE": "vllm-timing-rules.yml",
    "RULES_GROUP": "vllm-timing", "SSH_SOCKET_PREFIX": "vllm-ssh-",
    "PROMETHEUS_CONTAINER": "vllm-pd-prometheus", "PROMETHEUS_URL": "http://localhost:9090",
    "PANEL1_P_UID": "vllm-pd-prefill", "PANEL1_D_UID": "vllm-pd-decode",
    "GRAFANA_CONTAINER": "vllm-pd-grafana", "GRAFANA_URL": "http://localhost:3000", "DOCKER_NETWORK": "vllm-pd-monitor",
}
# Values every cluster on one monitoring machine shares: one Grafana, one container network, one machine.
SHARED_KEYS = ["GRAFANA_URL", "GRAFANA_USER", "GRAFANA_PASSWORD", "GRAFANA_CONTAINER", "DOCKER_NETWORK", "MONITOR_HOST"]


class NamesError(Exception):
    pass


def home_dir(env=None):
    return (env or os.environ).get("HOME") or os.path.expanduser("~")


def expand_home(value, home):
    for p in ("${HOME}", "$HOME"):
        if value == p or value.startswith(p + "/"):
            return home + value[len(p):]
    if value == "~" or value.startswith("~/"):
        return home + value[1:]
    return value


def norm_path(p):
    return os.path.normpath(p) if p else p


class Names:
    """The resolved values of one cluster. `env` holds deploy.env as the shell sees it (blank = derive)."""

    def __init__(self, env):
        self.env = env
        self.home = home_dir(env)
        self.cluster = self.given("CLUSTER")
        if not self.cluster:
            raise NamesError("deploy.env 里没有 CLUSTER。新集群用 bin/panel2_cluster.sh add，已经装好的用 bin/panel2_cluster.sh adopt 补上。")
        if not CLUSTER_RE.match(self.cluster):
            raise NamesError("CLUSTER=%s 不合规：只用小写字母、数字和 -，以字母或数字开头和结尾，最长 40 个字符。" % self.cluster)
        self.model_group = self.given("MODEL_GROUP")
        b = self.given("PORT_BASE")
        self.port_base = None
        if b:
            if not b.isdigit() or not 1024 <= int(b) <= 65535 - PORT_STEP:
                raise NamesError("PORT_BASE=%s 不是 1024–%d 之间的整数。" % (b, 65535 - PORT_STEP))
            self.port_base = int(b)
        r = self.given(ROOT_KEY) or default_root()
        self.root = norm_path(expand_home(r, self.home)) if r else self.home
        if not os.path.isabs(self.root):
            raise NamesError("%s=%s 不是绝对路径。" % (ROOT_KEY, r))
        self._cache = {}

    def given(self, key):
        v = self.env.get(key)
        return v.strip() if v else ""

    def get(self, key):
        if key not in self._cache:
            self._cache[key] = self._resolve(key)
        return self._cache[key]

    def _resolve(self, key):
        v = self.given(key)
        if v:
            if key in PATH_KEYS:
                v = norm_path(expand_home(v, self.home))
            if key in PORT_KEYS and (not v.isdigit() or not 1 <= int(v) <= 65535):
                raise NamesError("%s=%s 不是端口号。" % (key, v))
            return v
        c = self.cluster
        if key in RULES:
            _, kind, base = RULES[key]
            if kind == "name":
                return "%s-%s" % (base, c)
            if kind == "title":
                return "%s（%s）" % (base, c)
            if kind == "home":
                return os.path.join(self.root, base, c)
            if kind == "file":
                return "%s-%s.yml" % (base, c)
            if kind == "mark":
                return "%s %s" % (base, c)
            if kind == "prefix":
                return "%s%s-" % (base, c)
            if kind == "port":
                if self.port_base is None:
                    raise NamesError("PORT_BASE 和 %s 都是空的：用 bin/panel2_cluster.sh add 自动分配，或者在 deploy.env 里填 PORT_BASE。" % key)
                return str(self.port_base + base)
        if key == "CLICKHOUSE_ADDR":
            return self.get("CLICKHOUSE_CONTAINER")
        if key == "PULL_DIR":
            return os.path.join(self.get("CLICKHOUSE_DATA_DIR") or self.get("TIMING_HOME"), "pull")
        if key == "PROMETHEUS_URL":
            return "http://localhost:%s" % self.get("PROMETHEUS_PORT")
        if key == "PROMETHEUS_YML":
            return os.path.join(self.get("PANEL1_DIR"), "prometheus", "prometheus.yml")
        if key == "GRAFANA_FOLDER":
            if not self.model_group:
                raise NamesError("deploy.env 里没有 MODEL_GROUP（Grafana 里的模型文件夹名，比如 GLM-5.3）。")
            return self.model_group
        if key == "CLICKHOUSE_DATA_DIR":
            return ""
        if key == "VOLUMES":
            if self.get("CLICKHOUSE_DATA_DIR"):
                return []
            ch, central = self.get("CLICKHOUSE_CONTAINER"), self.get("CENTRAL_CONTAINER")
            return [ch + "-data", ch + "-logs", central + "-data"]
        raise KeyError(key)

    def output(self, panel):
        out = {"CLUSTER": self.cluster, "MODEL_GROUP": self.model_group,
               "PORT_BASE": "" if self.port_base is None else str(self.port_base), ROOT_KEY: self.root}
        for k in OUTPUT[panel]:
            out[k] = self.get(k)
        if panel == "panel2":
            out["CLICKHOUSE_DATA_DIR"] = self.get("CLICKHOUSE_DATA_DIR")
        return out

    def resources(self, panel, pack_dir=None):
        """What this panel of the cluster registers: {key: value or list}. Empty values are left out."""
        res = {}
        for k in REGISTERED[panel]:
            v = self.get(k)
            if k == "PANEL2_DIR" and pack_dir:
                v = norm_path(pack_dir)
            if v:
                res[k] = v
        return res


def env_from_file(path, keys=None):
    """deploy.env as bash sees it after `set -a; . deploy.env`, in an empty environment. Only `keys` when given."""
    want = list(keys) if keys else sorted(set(RULES) | set(DEPENDENT) | set(INPUTS) | set(SHARED_KEYS) | set(EARLIER))
    script = 'set -a; . "$1" >/dev/null 2>&1; shift; for k in "$@"; do printf "%s=%s\\0" "$k" "${!k-}"; done'
    env = {"HOME": home_dir(), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    r = subprocess.run(["bash", "-c", script, "_", path] + want, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise NamesError("读不了 %s：%s" % (path, r.stderr.decode(errors="replace").strip()))
    out = {"HOME": env["HOME"]}
    for item in r.stdout.decode().split("\0"):
        if "=" in item:
            k, v = item.split("=", 1)
            out[k] = v
    return out


def raw_lines(path):
    """KEY -> the value as written in the file (the last assignment wins), without shell expansion."""
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line.rstrip("\n"))
            if m:
                v = m.group(2).strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                out[m.group(1)] = v
    return out


# ---------------- registry ----------------

def registry_path():
    return os.environ.get("VLLM_PD_REGISTRY") or os.path.join(home_dir(), ".vllm-pd-clusters.json")


def default_root(path=None):
    """The registry's "root" (the folder add --root set), or "" when there is none."""
    try:
        return load_registry(path).get("root") or ""
    except (NamesError, OSError, ValueError):
        return ""


def empty_registry():
    return {"format": REGISTRY_FORMAT, "version": REGISTRY_VERSION, "clusters": {}}


def load_registry(path=None):
    path = path or registry_path()
    if not os.path.exists(path):
        return empty_registry()
    with open(path, encoding="utf-8") as f:
        reg = json.load(f)
    if reg.get("format") != REGISTRY_FORMAT:
        raise NamesError("%s 不是集群注册表（format 不是 %s）" % (path, REGISTRY_FORMAT))
    reg.setdefault("clusters", {})
    return reg


@contextlib.contextmanager
def registry(write=False, path=None):
    """The registry under an exclusive lock; written back atomically when write is true."""
    import fcntl
    path = path or registry_path()
    with open(path + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        reg = load_registry(path)
        yield reg
        if write:
            tmp = "%s.tmp.%d" % (path, os.getpid())
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(reg, f, ensure_ascii=False, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp, path)


def entry_values(entry):
    """(kind, value, key, panel) for everything a registry entry holds, ports and paths normalised."""
    for panel in PANELS:
        sec = entry.get(panel) or {}
        for k, v in (sec.get("values") or {}).items():
            for x in (v if isinstance(v, list) else [v]):
                yield KIND.get(k, k), norm_value(k, x), k, panel
        for h in sec.get("hosts") or []:
            yield "集群机器", h, "inventory", panel
    if entry.get("port_base"):
        b = int(entry["port_base"])
        for p in range(b, b + PORT_STEP):
            yield "端口段", str(p), "PORT_BASE", "-"


def norm_value(key, v):
    """For comparing: folders through symbolic links, so that one folder under two spellings is still one folder."""
    v = str(v)
    return os.path.realpath(v) if key in PATH_KEYS else v


def conflicts(reg, cluster, panel, resources, hosts=()):
    """Lines naming each value this cluster wants that another cluster has registered."""
    mine = []
    for k, v in resources.items():
        for x in (v if isinstance(v, list) else [v]):
            mine.append((KIND.get(k, k), norm_value(k, x), k))
    mine += [("集群机器", h, "inventory") for h in hosts]
    lines = []
    for other, entry in sorted(reg["clusters"].items()):
        if other == cluster:
            continue
        theirs = {}
        for kind, v, k, p in entry_values(entry):
            theirs.setdefault((kind, v), (k, p))
        for kind, v, k in mine:
            if (kind, v) in theirs:
                ok, op = theirs[(kind, v)]
                lines.append("%s %s 已经登记给集群 %s（它的 %s），本集群的 %s 不能再用" % (kind, v, other, ok, k))
            if kind == "主机端口" and ("端口段", v) in theirs:
                lines.append("主机端口 %s 在集群 %s 的端口段里（PORT_BASE=%s），本集群的 %s 不能用" % (v, other, entry.get("port_base"), k))
    own = reg["clusters"].get(cluster, {}).get(panel) or {}
    if own.get("dir") and resources.get("PANEL2_DIR" if panel == "panel2" else "PANEL1_DIR") \
            and os.path.realpath(own["dir"]) != os.path.realpath(resources.get("PANEL2_DIR" if panel == "panel2" else "PANEL1_DIR")):
        lines.append("集群 %s 的%s已经登记在 %s，不是这个目录" % (cluster, "面板二" if panel == "panel2" else "面板一", own["dir"]))
    return lines


def register(reg, names, panel, pack_dir=None, hosts=(), env_file=None):
    c = names.cluster
    entry = reg["clusters"].setdefault(c, {})
    if entry.get("model_group") and names.model_group and entry["model_group"] != names.model_group:
        raise NamesError("集群 %s 已经登记的模型文件夹是 %s，这里是 %s：两个面板要用同一个 MODEL_GROUP" % (c, entry["model_group"], names.model_group))
    if names.model_group:
        entry["model_group"] = names.model_group
    if names.port_base is not None:
        if entry.get("port_base") and int(entry["port_base"]) != names.port_base:
            raise NamesError("集群 %s 已经登记的 PORT_BASE 是 %s，这里是 %s" % (c, entry["port_base"], names.port_base))
        entry["port_base"] = names.port_base
    res = names.resources(panel, pack_dir)
    sec = {"values": res, "hosts": list(hosts), "registered": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
    if pack_dir:
        sec["dir"] = norm_path(pack_dir)
    elif panel == "panel1":
        sec["dir"] = res.get("PANEL1_DIR")
    if env_file:
        sec["env_file"] = os.path.abspath(env_file)
    entry[panel] = sec
    return entry


def unregister(reg, cluster, panel):
    entry = reg["clusters"].get(cluster)
    if not entry:
        return False
    entry.pop(panel, None)
    if not any(entry.get(p) for p in PANELS):
        del reg["clusters"][cluster]
    return True


def others(reg, cluster, panel=None):
    return sorted(c for c, e in reg["clusters"].items() if c != cluster and (panel is None or e.get(panel)))


# ---------------- ports ----------------

def docker_publishers():
    """{host port: [container names]} of running containers; None when docker cannot be asked."""
    try:
        r = subprocess.run(["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    out = {}
    for line in r.stdout.decode(errors="replace").splitlines():
        name, _, ports = line.partition("\t")
        for m in re.finditer(r":(\d+)(?:-(\d+))?->", ports):
            lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
            for p in range(lo, hi + 1):
                out.setdefault(p, [])
                if name not in out[p]:
                    out[p].append(name)
    return out


def port_bindable(port):
    """True when a process on this machine could listen on the port now."""
    for fam, addr in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
        try:
            s = socket.socket(fam, socket.SOCK_STREAM)
        except OSError:
            continue
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((addr, port))
        except OSError:
            return False
        finally:
            s.close()
    return True


def own_puller_running(names):
    pid_file = os.path.join(names.get("TIMING_HOME"), "puller", "loop.pid")
    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def port_problems(names, panel, publishers=None, bindable=port_bindable, puller_running=None):
    """Ports of this cluster that something other than its own containers or its own puller holds."""
    if publishers is None:
        publishers = docker_publishers()
    own = {names.get(k) for k in OWN_CONTAINERS[panel]}
    lines = []
    for k in OWN_PORTS[panel]:
        p = int(names.get(k))
        holders = (publishers or {}).get(p, [])
        foreign = [h for h in holders if h not in own]
        if foreign:
            lines.append("主机端口 %d（%s）被容器 %s 占用" % (p, k, " ".join(foreign)))
            continue
        if holders:
            continue
        if bindable(p):
            continue
        if k == "PULLER_METRICS_PORT":
            running = own_puller_running(names) if puller_running is None else puller_running
            if running:
                continue
        lines.append("主机端口 %d（%s）已经被这台机器上的别的程序占用（ss -ltnp | grep :%d 查是谁）" % (p, k, p))
    if publishers is None:
        lines.append("问不了 docker（docker ps 失败），没有检查容器占用的端口")
    return lines


def alloc_port_base(reg, cluster, wanted=None, publishers=None, bindable=port_bindable, puller_running=None, root=None):
    """A free port block for the cluster: the one it has registered, `wanted`, or the next one nobody holds.
    As in port_problems, a port held by the cluster's own containers or its own puller is free for it: an `add` that
    stopped after making its containers and before registering gets the same block when it is run again."""
    entry = reg["clusters"].get(cluster) or {}
    if entry.get("port_base"):
        # Its own block: whatever holds those ports now is its own (bin/*.sh check that with the exemptions).
        if wanted is None or int(wanted) == int(entry["port_base"]):
            return int(entry["port_base"])
        raise NamesError("集群 %s 已经登记的 PORT_BASE 是 %s，不能改成 %s" % (cluster, entry["port_base"], wanted))
    taken = {}
    for other, e in reg["clusters"].items():
        if other == cluster:
            continue
        for kind, v, k, _ in entry_values(e):
            if kind in ("主机端口", "端口段"):
                taken[int(v)] = other
    if publishers is None:
        publishers = docker_publishers() or {}
    candidates = [int(wanted)] if wanted is not None else range(PORT_FIRST, PORT_LAST + 1, PORT_STEP)
    for b in candidates:
        if not 1024 <= b <= 65535 - PORT_STEP:
            raise NamesError("--port-base %d 不在 1024–%d 之间" % (b, 65535 - PORT_STEP))
        clash = [p for p in range(b, b + PORT_STEP) if p in taken]
        own = Names(dict({"HOME": home_dir(), "CLUSTER": cluster, "PORT_BASE": str(b)}, **({ROOT_KEY: root} if root else {})))
        own_containers = {own.get(k) for k in OWN_CONTAINERS["panel2"] + OWN_CONTAINERS["panel1"]}

        def held(p):
            holders = publishers.get(p) or []
            if holders:
                return any(h not in own_containers for h in holders)
            if bindable(p):
                return False
            if p == int(own.get("PULLER_METRICS_PORT")):
                return not (own_puller_running(own) if puller_running is None else puller_running)
            return True
        busy = [p for p in (b, b + 1, b + 2) if held(p)]
        if not clash and not busy:
            return b
        if wanted is not None:
            why = ["%d 已登记给集群 %s" % (p, taken[p]) for p in clash] + ["%d 已被占用" % p for p in busy]
            raise NamesError("--port-base %d 用不了：%s" % (b, "；".join(why)))
    raise NamesError("%d–%d 之间没有空闲的端口段，用 --port-base 指定一个" % (PORT_FIRST, PORT_LAST))


# ---------------- inventory ----------------

def inventory_hosts(inv):
    hosts = []
    for side in ("prefill", "decode"):
        for a in (inv.get(side) or {}).get("api_servers") or []:
            h = a.rsplit(":", 1)[0]
            if h not in hosts:
                hosts.append(h)
    return hosts


def inventory_problems(inv, reg=None, cluster=None):
    """Exporter ports that a vLLM API server on the same machine uses, and machines another cluster registered."""
    lines = []
    ports = {}
    for side in ("prefill", "decode"):
        for a in (inv.get(side) or {}).get("api_servers") or []:
            h, _, p = a.rpartition(":")
            if p.isdigit():
                ports.setdefault(h, set()).add(int(p))
    exporters = [("npu_exporter_port", int(inv.get("npu_exporter_port", 8082))), ("node_exporter_port", int(inv.get("node_exporter_port", 9100)))]
    if exporters[0][1] == exporters[1][1]:
        lines.append("清单里 npu_exporter_port 和 node_exporter_port 都是 %d" % exporters[0][1])
    for h in sorted(ports):
        for field, p in exporters:
            if p in ports[h]:
                lines.append("机器 %s 上 vLLM API server 用了端口 %d，和清单的 %s 相同：在清单里把 %s 改成别的值" % (h, p, field, field))
    if reg is not None:
        mine = set(inventory_hosts(inv))
        for other, e in sorted(reg["clusters"].items()):
            if other == cluster:
                continue
            for panel in PANELS:
                for h in sorted(mine & set((e.get(panel) or {}).get("hosts") or [])):
                    lines.append("机器 %s 已经登记给集群 %s：一台机器只能属于一个集群" % (h, other))
    return sorted(set(lines), key=lines.index)


# ---------------- shared values ----------------

def docker_network_of(container):
    try:
        r = subprocess.run(["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", container],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None, "问不了 docker"
    if r.returncode != 0:
        return None, "docker inspect %s 失败" % container
    nets = [n for n in json.loads(r.stdout.decode() or "{}") if n not in ("bridge", "host", "none")]
    if len(nets) == 1:
        return nets[0], ""
    return None, "Grafana 容器 %s 在 %d 个自建网络里（%s），分不出用哪个" % (container, len(nets), " ".join(nets) or "无")


def shared_values(env_from=None, reg=None, network_of=docker_network_of):
    """The values all clusters share, from `env_from` (a deploy.env or its folder), else from the deploy.env of a
    registered cluster. DOCKER_NETWORK falls back to the network of the Grafana container."""
    source = None
    if env_from:
        source = os.path.join(env_from, "deploy.env") if os.path.isdir(env_from) else env_from
        if not os.path.isfile(source):
            raise NamesError("没有这个文件：%s" % source)
    else:
        reg = reg if reg is not None else load_registry()
        found = []
        for c, e in reg["clusters"].items():
            for panel in PANELS:
                f = (e.get(panel) or {}).get("env_file")
                if f and os.path.isfile(f):
                    found.append(((e.get(panel) or {}).get("registered", ""), c, f))
        if found:
            source = sorted(found)[0][2]
    vals = {k: "" for k in SHARED_KEYS}
    if source:
        got = env_from_file(source, SHARED_KEYS)
        vals.update({k: got.get(k, "") for k in SHARED_KEYS})
    note = "共用值来自 %s" % source if source else "没有可读的已装集群 deploy.env"
    if not vals["DOCKER_NETWORK"] and vals["GRAFANA_CONTAINER"]:
        net, why = network_of(vals["GRAFANA_CONTAINER"])
        if net:
            vals["DOCKER_NETWORK"] = net
            note += "；DOCKER_NETWORK 取自 Grafana 容器所在的网络"
        else:
            note += "；" + why
    return vals, source, note


def adopt_values(path):
    """Lines to give an existing deployment its earlier names explicitly: KEY -> value, for keys it leaves blank."""
    raw = raw_lines(path)
    return {k: v for k, v in EARLIER.items() if not raw.get(k)}


# ---------------- command line ----------------

def abs_root(value):
    return norm_path(os.path.abspath(expand_home(value, home_dir())))


def shell_lines(d):
    return "".join("%s=%s\nexport %s\n" % (k, shlex.quote(str(v)), k) for k, v in d.items())


def build_env(args):
    if getattr(args, "env_file", None):
        env = env_from_file(args.env_file)
    else:
        env = dict(os.environ)
    for k, a in (("CLUSTER", "cluster"), ("PORT_BASE", "port_base"), ("MODEL_GROUP", "model_group"), (ROOT_KEY, "root")):
        v = getattr(args, a, None)
        if v is not None:
            env[k] = str(v)
    return env


def load_inventory(path):
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, panel=True):
        if panel:
            p.add_argument("--panel", choices=PANELS, default="panel2")
        p.add_argument("--env-file")
        p.add_argument("--cluster")
        p.add_argument("--port-base", type=int)
        p.add_argument("--model-group")
        p.add_argument("--root", type=abs_root, help="总目录（不给就用 deploy.env 的 VLLM_PD_ROOT、注册表里的、$HOME）")

    p = sub.add_parser("derive"); common(p); p.add_argument("--format", choices=["shell", "json"], default="json")
    for name in ("check", "register"):
        p = sub.add_parser(name); common(p)
        p.add_argument("--dir", help="这一套的安装目录（面板二是解包目录）")
        p.add_argument("--inventory")
        if name == "check":
            p.add_argument("--no-ports", action="store_true", help="只查注册表，不查端口是否空闲")
    p = sub.add_parser("unregister"); p.add_argument("--cluster", required=True); p.add_argument("--panel", choices=PANELS, required=True)
    p = sub.add_parser("alloc"); p.add_argument("--cluster", required=True); p.add_argument("--port-base", type=int); p.add_argument("--root", type=abs_root)
    p = sub.add_parser("root", help="总目录：--root 给的、注册表里的，或 $HOME")
    p.add_argument("--root", type=abs_root); p.add_argument("--save", action="store_true", help="把 --root 写进注册表，以后的 add 默认用它")
    p = sub.add_parser("dir", help="一个面板的安装目录")
    p.add_argument("--panel", choices=PANELS, required=True); p.add_argument("--cluster", required=True); p.add_argument("--root", type=abs_root)
    p = sub.add_parser("shared"); p.add_argument("--env-from"); p.add_argument("--format", choices=["shell", "json"], default="json")
    p = sub.add_parser("inventory-check"); p.add_argument("inventory"); p.add_argument("--cluster")
    p = sub.add_parser("others"); p.add_argument("--cluster", required=True); p.add_argument("--panel", choices=PANELS)
    p = sub.add_parser("list"); p.add_argument("--format", choices=["text", "json"], default="text")
    p = sub.add_parser("adopt-values"); p.add_argument("--env-file", required=True)
    args = ap.parse_args(argv)

    try:
        if args.cmd == "derive":
            out = Names(build_env(args)).output(args.panel)
            sys.stdout.write(shell_lines(out) if args.format == "shell" else json.dumps(out, ensure_ascii=False, indent=2) + "\n")
            return 0
        if args.cmd in ("check", "register"):
            names = Names(build_env(args))
            inv = load_inventory(args.inventory)
            hosts = inventory_hosts(inv) if inv else []
            pack_dir = args.dir or (os.environ.get("PACK_DIR") if args.panel == "panel2" else None)
            if args.cmd == "check":
                reg = load_registry()
                res = names.resources(args.panel, pack_dir)
                lines = conflicts(reg, names.cluster, args.panel, res, hosts)
                if inv:
                    lines += inventory_problems(inv, reg, names.cluster)
                if not args.no_ports:
                    lines += port_problems(names, args.panel)
                for line in lines:
                    print("FAIL " + line)
                if lines:
                    print("RESULT: FAIL（集群 %s 的名字、端口或目录和别的集群重复，或者端口被占用；什么都没有改）" % names.cluster)
                    return 1
                print("RESULT: OK（集群 %s：注册表里没有重复，端口空闲或属于本集群）" % names.cluster)
                return 0
            with registry(write=True) as reg:
                lines = conflicts(reg, names.cluster, args.panel, names.resources(args.panel, pack_dir), hosts)
                if lines:
                    for line in lines:
                        print("FAIL " + line)
                    return 1
                env_file = args.env_file or (os.path.join(pack_dir, "deploy.env") if pack_dir and os.path.isfile(os.path.join(pack_dir, "deploy.env")) else None)
                register(reg, names, args.panel, pack_dir, hosts, env_file)
                if args.root:
                    reg["root"] = args.root
            print("已登记集群 %s 的%s（%s）" % (names.cluster, "面板二" if args.panel == "panel2" else "面板一", registry_path()))
            return 0
        if args.cmd == "unregister":
            with registry(write=True) as reg:
                done = unregister(reg, args.cluster, args.panel)
            print("已从注册表删除集群 %s 的%s" % (args.cluster, "面板二" if args.panel == "panel2" else "面板一") if done else "注册表里没有集群 %s" % args.cluster)
            return 0
        if args.cmd == "alloc":
            if not CLUSTER_RE.match(args.cluster):
                raise NamesError("集群名 %s 不合规：只用小写字母、数字和 -" % args.cluster)
            print(alloc_port_base(load_registry(), args.cluster, args.port_base, root=args.root))
            return 0
        if args.cmd == "root":
            if args.save:
                if not args.root:
                    raise NamesError("--save 要和 --root 一起用")
                with registry(write=True) as reg:
                    reg["root"] = args.root
            print(args.root or default_root() or home_dir())
            return 0
        if args.cmd == "dir":
            env = {"HOME": home_dir(), "CLUSTER": args.cluster}
            if args.root:
                env[ROOT_KEY] = args.root
            print(Names(env).get("PANEL2_DIR" if args.panel == "panel2" else "PANEL1_DIR"))
            return 0
        if args.cmd == "shared":
            vals, _, note = shared_values(args.env_from)
            print(note, file=sys.stderr)
            sys.stdout.write(shell_lines(vals) if args.format == "shell" else json.dumps(vals, ensure_ascii=False, indent=2) + "\n")
            return 0
        if args.cmd == "inventory-check":
            lines = inventory_problems(load_inventory(args.inventory), load_registry(), args.cluster)
            for line in lines:
                print("FAIL " + line)
            print("RESULT: %s" % ("FAIL" if lines else "OK（清单端口不冲突，机器没有登记给别的集群）"))
            return 1 if lines else 0
        if args.cmd == "others":
            print("\n".join(others(load_registry(), args.cluster, args.panel)))
            return 0
        if args.cmd == "list":
            reg = load_registry()
            if args.format == "json":
                print(json.dumps(reg, ensure_ascii=False, indent=2))
                return 0
            print("%-14s %-12s %-9s %-26s %-26s %s" % ("集群", "模型文件夹", "端口基数", "面板二看板 UID", "面板一看板 UID", "目录"))
            for c, e in sorted(reg["clusters"].items()):
                v2 = (e.get("panel2") or {}).get("values") or {}
                v1 = (e.get("panel1") or {}).get("values") or {}
                print("%-14s %-12s %-9s %-26s %-26s %s" % (c, e.get("model_group", ""), e.get("port_base", "") or "-",
                                                         v2.get("GRAFANA_BOARD_UID", "-"),
                                                         " ".join(x for x in (v1.get("PANEL1_P_UID"), v1.get("PANEL1_D_UID")) if x) or "-",
                                                         " ".join(x for x in ((e.get("panel2") or {}).get("dir"), (e.get("panel1") or {}).get("dir")) if x)))
            return 0
        if args.cmd == "adopt-values":
            for k, v in adopt_values(args.env_file).items():
                print("%s=%s" % (k, v))
            return 0
    except NamesError as e:
        print("错误：%s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
