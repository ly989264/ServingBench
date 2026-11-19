#!/usr/bin/env python3
"""Panel 2 diagnostics for the cluster folder this file sits in (pack two v2.1). Read-only, except `pair`.

Run on the monitoring machine, in the cluster folder, through bin/diag.sh. Settings come from the cluster folder
(deploy.env, inventory.json, segments.json), read without writing anything there; reports go to
~/vllm-pd-diag-reports/<cluster>/. The judgements shared with `panel2_cluster.sh check` come from scripts/criteria.py.

  bin/diag.sh all | nodes | vllm | chain | monitor
  bin/diag.sh history "<start>" "<end>"        local time, e.g. "2026-11-13 10:30"
  bin/diag.sh log <file> [--save-profile]
  bin/diag.sh pair <proxy host:port> [--model M]      sends 3 short requests
Common options: --containers ADDR=NAME[;NAME],..., --stdout-file ADDR=PATH,... (they override deploy.env's
NODE_VLLM_CONTAINERS and NODE_VLLM_STDOUT_FILES).

Importable without side effects: probe_vllm(cluster, addr, containers) is what `check` uses to read the vLLM processes.
"""
import argparse
import datetime
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
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.dirname(SCRIPTS)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
import criteria as K  # noqa: E402

REPORT_ROOT = os.path.join(os.path.expanduser("~"), "vllm-pd-diag-reports")
ENV_KEYS = ("VLLM_LOGGING_CONFIG_PATH", "PYTHONPATH", "VLLM_CONFIGURE_LOGGING", "PD_DIAG_INSTANCE",
            "PD_DIAG_INSTANCE_SOURCE", "PD_DIAG_MAPPING", "PD_DIAG_LOG_DIR")
NODE_FILES = K.NODE_FILES
# F2: where the two lines go. The same wording as `bin/nodes.sh paths`.
EXPORT_WHERE = "两行 export 放在容器里执行 vllm serve 的那个脚本里、vllm serve 之前（或用 docker run/exec -e 传进去）；写在宿主机的 shell 或别的脚本里不生效"


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


# ==================== settings of the cluster ====================
class Cluster:
    """deploy.env with the names the pack derives, inventory.json, segments.json. Nothing is written in the folder."""

    def __init__(self, folder=PACK):
        self.dir = os.path.abspath(folder)
        for f in ("deploy.env", "inventory.json", "segments.json", "bin/lib.sh"):
            if not os.path.isfile(os.path.join(self.dir, f)):
                raise SystemExit("错误：%s 里没有 %s。bin/diag.sh 要在装好的集群目录里执行（panel2_cluster.sh add 建的那个目录）。" % (self.dir, f))
        self.env = self._load_env()
        self.name = self.env.get("CLUSTER") or os.path.basename(self.dir)
        self.inv = json.load(open(os.path.join(self.dir, "inventory.json"), encoding="utf-8"))
        self.segments = json.load(open(os.path.join(self.dir, "segments.json"), encoding="utf-8"))
        self.version = K.expected_version(self.dir)
        self.log_dir_in = (self.segments.get("line") or {}).get("log_dir", "/var/log/pd-diag")
        self.keep_hours = float((self.segments.get("line") or {}).get("keep_hours", 12))
        self.node_sums = K.expected_node_sums(self.dir)
        self.known_filter = K.known_filter_shas(self.dir)
        self.mode = (self.env.get("NODE_MODE") or "mock").strip()
        self.probes = {}   # addr -> (procs, facts), so that `all` reads each node's vLLM processes once
        self.instances = {}   # address -> [(name, port)]
        for side, letter in (("prefill", "P"), ("decode", "D")):
            for i, a in enumerate(self.inv[side]["api_servers"]):
                host, port = a.rsplit(":", 1)
                self.instances.setdefault(host, []).append(("%s%d" % (letter, i), port))
        self.n_d = len(self.inv["decode"]["api_servers"])
        self.version_file = open(os.path.join(self.dir, "VERSION")).read().strip() if os.path.isfile(os.path.join(self.dir, "VERSION")) else "v1.0（没有 VERSION 文件）"

    def _load_env(self):
        # The pack's own load_env, in a clean shell; Python's bytecode cache is off so nothing is written in scripts/.
        script = ('cd %s && export PYTHONDONTWRITEBYTECODE=1 && . bin/lib.sh && load_env >/dev/null && '
                  'NODE_CONF_DIR_RAW=$(raw_env NODE_CONF_DIR "$HOME/vllm-pd-diag/conf") && export NODE_CONF_DIR_RAW && env -0' % shlex.quote(self.dir))
        keep = {k: os.environ[k] for k in ("HOME", "PATH", "USER", "LOGNAME", "LANG", "VLLM_PD_REGISTRY") if k in os.environ}
        keep["PYTHONDONTWRITEBYTECODE"] = "1"
        p = subprocess.run(["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=keep, timeout=60)
        if p.returncode != 0:
            raise SystemExit("错误：读不了 %s 的 deploy.env：%s" % (self.dir, p.stderr.decode("utf-8", "replace").strip()[-300:]))
        env = {}
        for item in p.stdout.split(b"\0"):
            k, sep, v = item.partition(b"=")
            if sep:
                env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
        return env

    def get(self, key, default=""):
        return self.env.get(key) or default

    def node_dir(self, key, fallback):
        """NODE_CONF_DIR / NODE_LOG_DIR as written in deploy.env ($HOME kept for the node to expand)."""
        return self.env.get(key + "_RAW") or self.env.get(key) or fallback

    def conf_host(self):
        return self.node_dir("NODE_CONF_DIR", "$HOME/vllm-pd-diag/conf")

    def log_host(self):
        return self.node_dir("NODE_LOG_DIR", "$HOME/vllm-pd-diag/logs")

    def conf_in(self):
        """The three files' folder inside the vLLM container; v2.1's default is the host path (design 2.8)."""
        return self.get("NODE_CONF_DIR_IN_CONTAINER") or self.conf_host()

    def containers(self):
        """deploy.env's NODE_VLLM_CONTAINERS: {addr: [names]}; empty means: look for them on each node."""
        return node_containers(self.get("NODE_VLLM_CONTAINERS"))

    def stdout_files(self):
        """deploy.env's NODE_VLLM_STDOUT_FILES: {addr: path}."""
        return addr_map(self.get("NODE_VLLM_STDOUT_FILES"))


# ==================== access ====================
def run(cmd, timeout=60, stdin=None):
    try:
        p = subprocess.run(cmd, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, "", "超时（%d 秒）" % timeout
    except OSError as e:
        return 127, "", str(e)


def ssh(c, addr, script, timeout=120):
    """One read-only shell script on a node. StrictHostKeyChecking=yes: known_hosts is never written."""
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=yes", "-o", "LogLevel=ERROR"]
    cmd += shlex.split(c.get("SSH_OPTS"))
    if c.get("SSH_USER"):
        cmd += ["-l", c.get("SSH_USER")]
    return run(cmd + [addr, "sh -s"], timeout=timeout, stdin=script.encode())


def ch(c, sql, timeout=120):
    """ClickHouse as the admin, read-only for this query."""
    code, out, err = run(["docker", "exec", "-i", c.get("CLICKHOUSE_CONTAINER"), "clickhouse-client", "--password", c.get("CH_ADMIN_PASSWORD"),
                          "--readonly=1", "-q", sql + " FORMAT JSONEachRow"], timeout=timeout)
    if code != 0:
        return None, (err or out).strip().splitlines()[-1][:300] if (err or out).strip() else "退出码 %d" % code
    return [json.loads(x) for x in out.splitlines() if x.strip()], None


def http_json(url, timeout=15, auth=None, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=dict(headers or {}))
    if auth:
        import base64
        req.add_header("Authorization", "Basic " + base64.b64encode(("%s:%s" % auth).encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, (json.loads(body) if body.strip()[:1] in (b"{", b"[") else body.decode("utf-8", "replace")), None
    except urllib.error.HTTPError as e:
        return e.code, None, str(e)
    except Exception as e:  # noqa: BLE001
        return None, None, str(e)[:200]


def prom(c, query):
    url = c.get("PROMETHEUS_URL", "http://localhost:9090").rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": query})
    code, res, err = http_json(url)
    if code != 200 or not isinstance(res, dict):
        return None, err or "HTTP %s" % code
    return res["data"]["result"], None


def prom_range(c, query, start, end, step):
    q = urllib.parse.urlencode({"query": query, "start": start, "end": end, "step": step})
    code, res, err = http_json(c.get("PROMETHEUS_URL", "http://localhost:9090").rstrip("/") + "/api/v1/query_range?" + q, timeout=60)
    if code != 200 or not isinstance(res, dict):
        return None, err or "HTTP %s" % code
    return res["data"]["result"], None


def table(c):
    """v2.0 and later read the view that holds this version's rows; v1.0 has only the table."""
    rows, err = ch(c, "SELECT count() AS n FROM system.tables WHERE database = 'vllm_pd' AND name = 'phase_line_cur'")
    return "vllm_pd.phase_line_cur" if rows and int(rows[0]["n"]) else "vllm_pd.phase_line"


def parse_kv_lines(text, sep="\t"):
    """Lines KEY<TAB>VALUE..., several per key allowed: {key: [rest, ...]}."""
    out = {}
    for line in text.splitlines():
        if sep in line:
            k, _, v = line.partition(sep)
            out.setdefault(k, []).append(v)
    return out


def node_containers(spec):
    """'ADDR=NAME[;NAME],ADDR=NAME' -> {addr: [names]}"""
    out = {}
    for part in (spec or "").split(","):
        if "=" in part:
            a, _, names = part.partition("=")
            out[a.strip()] = [n.strip() for n in names.split(";") if n.strip()]
    return out


def addr_map(spec):
    """'ADDR=PATH,ADDR=PATH' -> {addr: path}"""
    return {a.strip(): v.strip() for a, _, v in (p.partition("=") for p in (spec or "").split(",")) if a.strip() and v.strip()}


def human(n):
    return K.human(n)


def write_rates(c):
    """Bytes per second each machine writes, from the puller's counter (last 30 minutes), the query `check` uses."""
    res, err = prom(c, K.log_rate_query())
    return {r["metric"].get("machine", ""): float(r["value"][1]) for r in (res or [])}, err


def prefixed(addr, v):
    """A criteria Verdict as (level, line) with the address in front."""
    return v.level, ("%s %s" % (addr, v.text)) if addr else v.text


# ==================== nodes ====================
NODE_SCRIPT = r'''
L=$(printf '%s' "$RAW_L" | sed "s|^\$HOME|$HOME|; s|^~|$HOME|")
C=$(printf '%s' "$RAW_C" | sed "s|^\$HOME|$HOME|; s|^~|$HOME|")
out() { printf '%s\t%s\n' "$1" "$2"; }
out now "$(date +%s)"
out boot "$(uptime -s 2>/dev/null)"
last -x reboot shutdown 2>/dev/null | head -n 3 | while IFS= read -r l; do [ -n "$l" ] && out last "$l"; done
for d in "$L" /data; do
  t=$d; while [ ! -e "$t" ] && [ "$t" != "/" ]; do t=$(dirname "$t"); done
  out "df $d" "$(df -Pk "$t" 2>/dev/null | awk 'NR==2 {print $2, $4, $5, $6}')"
  out "mnt $d" "$(findmnt -n -o SOURCE,FSTYPE,TARGET -T "$t" 2>/dev/null | head -n 1)"
  out "exists $d" "$([ -e "$d" ] && echo yes || echo no)"
done
out logdir "$L"
out logread "$( [ -r "$L" ] && [ -x "$L" ] && echo yes || echo no)"
out mock "$(pgrep -fc '[p]d032_mock.py [r]un-instance' 2>/dev/null || echo 0)"
out mockstart "$(stat -c %Y "$HOME/vllm-timing/run/mock.pid" 2>/dev/null)"
out bootepoch "$(awk '/^btime/ {print $2}' /proc/stat 2>/dev/null)"
pgrep -f '[p]d032_mock.py [r]un-instance' 2>/dev/null | while read -r p; do
  inst=$(tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null | sed -n 's/.*--instance \([^ ]*\).*/\1/p')
  out mockproc "$p $inst $(ps -o lstart= -p "$p" 2>/dev/null)"
done
for f in "$HOME"/vllm-timing/run/stats/*.json; do [ -f "$f" ] && out stats "$(cat "$f")"; done
mem=$(awk '/MemAvailable/ {print $2}' /proc/meminfo 2>/dev/null); out memavail "${mem:-}"
j=$( (journalctl -k --since "24 hours ago" --no-pager 2>&1 || true) | head -c 2000000)
case $j in *"No journal files"*|*"insufficient permissions"*|*"not seeing messages"*|"") j=$(dmesg -T 2>/dev/null | tail -n 20000); src=dmesg ;; *) src=journalctl ;; esac
if [ -z "$j" ]; then out syslog none; else
  out syslog "$src"
  printf '%s\n' "$j" | grep -i -E 'out of memory|killed process' | tail -n 5 | while IFS= read -r l; do out oom "$l"; done
fi
(journalctl --since "24 hours ago" --no-pager 2>/dev/null; dmesg -T 2>/dev/null) | grep -i 'No space left on device' | tail -n 3 | while IFS= read -r l; do out nospace "$l"; done
for f in pd_diag_logging.json pd_diag_logfilter.py segments.json; do
  out "sum $f" "$( (sha256sum "$C/$f" 2>/dev/null || shasum -a 256 "$C/$f" 2>/dev/null) | cut -c1-64)"
done
for d in "$L"/*/; do
  [ -d "$d" ] || continue
  f=$(ls -t "$d" 2>/dev/null | grep '\.log$' | head -n 1)
  [ -n "$f" ] && out newest "$(basename "$d") $(stat -c %Y "$d$f" 2>/dev/null) $(tail -n 1 "$d$f" | cut -c1-29) $(tail -n 2000 "$d$f" | grep -v pd_diag_logfilter.py | tail -n 1 | cut -c1-29)"
done
'''


def node_probe(c, addr):
    pre = "RAW_L=%s\nRAW_C=%s\n" % (shlex.quote(c.node_dir("NODE_LOG_DIR", "$HOME/vllm-pd-diag/logs")),
                                     shlex.quote(c.node_dir("NODE_CONF_DIR", "$HOME/vllm-pd-diag/conf")))
    t0 = time.time()
    code, out, err = ssh(c, addr, pre + NODE_SCRIPT, timeout=120)
    t1 = time.time()
    return code, parse_kv_lines(out), err, (t0 + t1) / 2


def cmd_nodes(c, r, args):
    r.section("nodes（%d 台）" % len(c.instances))
    rates, rate_err = write_rates(c)
    if c.mode == "mock":
        r.add(*prefixed("", K.judge_mock_rate(c.get("MOCK_RATE"), c.n_d)))
    mounts = {}
    for addr, names in c.instances.items():
        code, kv, err, mid = node_probe(c, addr)
        r.keep("nodes %s" % addr, "\n".join("%s\t%s" % (k, v) for k, vs in kv.items() for v in vs) or err)
        if code != 0 or "now" not in kv:
            hint = "→ 这台的 host key 不在 known_hosts：先执行一次 bin/nodes.sh check" if "Host key verification failed" in err else "→ 检查网络、SSH_USER、SSH_OPTS"
            r.add("FAIL", "%s SSH 连不上（%s）%s" % (addr, err.strip()[:80], hint))
            continue
        g = lambda k: (kv.get(k) or [""])[0]
        skew = int(g("now") or 0) + 0.5 - mid
        mem = int(g("memavail") or 0) // 1024
        boot = g("boot")
        lvl = "OK" if abs(skew) <= 1 else ("WARN" if abs(skew) <= 300 else "FAIL")
        r.add(lvl, "%s 开机 %s；时钟 %+.1f 秒；内存 available %s" % (addr, boot or "?", skew, human(mem * 1024 * 1024) if mem else "?") +
              ("" if lvl == "OK" else " → 校时（chrony/ntp）"))
        # log disk, 12 hours of logs
        for d in (g("logdir"), "/data"):
            df = g("df %s" % d).split()
            mnt = g("mnt %s" % d)
            if d == "/data" and g("exists /data") != "yes":
                continue
            mounts.setdefault(d, {})[addr] = mnt.split(" ")[0] if mnt else ""
            if len(df) < 4:
                r.add("WARN", "%s %s：读不出磁盘空间" % (addr, d))
                continue
            free = int(df[1]) * 1024
            if d == g("logdir"):
                # the same estimate and line as the upgrade-safety check (design 2.7)
                v = K.judge_log_volume(K.estimate_log_bytes(rates.get(addr), c.keep_hours), free, "日志盘 %s" % d, "%g" % c.keep_hours)
                lvl, text = prefixed(addr, v)
                r.add(lvl, text + "（%s，已用 %s）" % (mnt, df[2]) + ("" if rates.get(addr) is not None or not rate_err else "；Prometheus：%s" % rate_err[:80]))
        if g("logread") != "yes":
            r.add("FAIL", "%s SSH 用户读不了日志目录 %s → 拉取会报 Permission denied；让 SSH 用户能读" % (addr, g("logdir")))
        # mock processes
        n_mock, want = int(g("mock") or 0), len(names)
        procs = kv.get("mockproc", [])
        ms, be = int(g("mockstart") or 0), int(g("bootepoch") or 0)
        if c.mode == "mock" and n_mock == 0 and ms and be > ms:
            r.add("INFO", "%s 开机（%s）晚于上一次启动 mock（%s）：mock 和拉取程序开机不会自己起来" % (
                addr, time.strftime("%m-%d %H:%M", time.localtime(be)), time.strftime("%m-%d %H:%M", time.localtime(ms))))
        if c.mode == "mock":
            # the same count and line as the upgrade-safety check; the report keeps each process's start time
            r.add(*prefixed(addr, K.judge_mock_processes(n_mock, [n for n, _ in names])))
        elif n_mock:
            r.add("FAIL", "%s vllm 模式下还有 %d 个 mock 进程 → pkill -9 -f '[p]d032_mock.py'（在这台机器上执行）" % (addr, n_mock))
        if n_mock and procs:
            r.keep("mock processes %s" % addr, "\n".join(procs))
        stale, growing = [], []
        for s in kv.get("stats", []):
            try:
                d = json.loads(s)
            except ValueError:
                continue
            age = time.time() - float(d.get("time", 0)) - skew
            if age > 60:
                stale.append("%s %.0f 秒" % (d.get("instance"), age))
            if int(d.get("running", 0)) > 1000:
                growing.append("%s %d" % (d.get("instance"), int(d.get("running", 0))))
        if c.mode == "mock" and stale:
            r.add("FAIL", "%s mock 的主循环卡住：状态文件多久没更新 %s → 速率太大（每个 D 超过约 1.7 请求/秒会积压）：调小 deploy.env 的 MOCK_RATE（留空即每个 D 1 请求/秒），再 bin/nodes.sh start" % (addr, "，".join(stale[:8])))
        if c.mode == "mock" and growing:
            r.add("WARN", "%s mock 每个实例未完成的请求数偏大：%s（每 D %.1f 请求/秒）" % (addr, "，".join(growing[:8]),
                  K.effective_mock_rate(c.get("MOCK_RATE"), c.n_d) / max(c.n_d, 1)))
        # system logs
        if g("syslog") in ("", "none"):
            r.add("WARN", "%s 读不了系统日志（journalctl 要 root 或 adm 组），没有查内存耗尽和磁盘满 → 用有权限的用户执行 journalctl -k | grep -i -E 'out of memory|killed process'" % addr)
        for l in kv.get("oom", []):
            r.add("FAIL", "%s 内存耗尽杀过进程：%s" % (addr, l[:160]))
        for l in kv.get("nospace", []):
            r.add("FAIL", "%s 系统日志里有磁盘满：%s" % (addr, l[:160]))
        if mem and mem < 2048:
            r.add("WARN", "%s 可用内存只有 %d MiB" % (addr, mem))
        # node files
        v = K.judge_node_sums(c.node_sums, {f: g("sum %s" % f) for f in NODE_FILES})
        lvl, text = prefixed(addr, v)
        r.add(lvl, text + ("；vllm 模式下，pd_diag_logfilter.py、pd_diag_logging.json 变了要重启 vLLM" if lvl == "FAIL" and c.mode == "vllm" else ""))
    shared = mounts.get("/data", {})
    if len(shared) >= 2:
        src = set(shared.values())
        r.add("INFO", "两台的 /data 来源相同（%s），看起来是同一块盘：写满时两边一起停" % src.pop() if len(src) == 1 else "/data 各台来源不同（%s）" % "，".join("%s %s" % kv for kv in shared.items()))


# ==================== vllm ====================
# Runs inside each vLLM container (docker exec ... sh -c). Lists the vLLM processes with only the environment keys
# below; nothing else of the environment leaves the container. The last field of a proc line is the pid in the
# process's own pid namespace (NSpid), the one the filter writes into ClickHouse.
INSIDE = r'''
now=$(date +%s); up=$(cut -d' ' -f1 /proc/uptime); hz=$(getconf CLK_TCK 2>/dev/null || echo 100)
printf 'clock\t%s\t%s\t%s\n' "$now" "$up" "$hz"
dirs="$CONF_IN"
for d in /proc/[0-9]*; do
  p=${d#/proc/}
  [ -r "$d/cmdline" ] || continue
  cmd=$(tr '\0\t\n\r' '    ' < "$d/cmdline" 2>/dev/null)
  case "$cmd" in *vllm*|*VLLM*|*spawn_main*) ;; *) continue ;; esac
  case "$cmd" in *"sh -c"*|*grep*|*pgrep*) continue ;; esac
  stat=$(cat "$d/stat" 2>/dev/null) || continue
  rest=${stat##*) }; set -- $rest; ppid=$2; start=${20}
  uid=$(awk '/^Uid:/ {print $2}' "$d/status" 2>/dev/null)
  nsp=$(awk '/^NSpid:/ {print $NF}' "$d/status" 2>/dev/null)
  comm=$(cat "$d/comm" 2>/dev/null)
  if [ -r "$d/environ" ]; then
    env=$(tr '\0' '\n' < "$d/environ" | grep -E '^(VLLM_LOGGING_CONFIG_PATH|PYTHONPATH|VLLM_CONFIGURE_LOGGING|PD_DIAG_INSTANCE|PD_DIAG_INSTANCE_SOURCE|PD_DIAG_MAPPING|PD_DIAG_LOG_DIR)=' | tr '\n' '\001')
    readable=yes
    cfg=$(tr '\0' '\n' < "$d/environ" | sed -n 's/^VLLM_LOGGING_CONFIG_PATH=//p')
    [ -n "$cfg" ] && dirs="$dirs $(dirname "$cfg")"
    if [ -n "$cfg" ] && [ "${HOSTMODE:-0}" = 1 ]; then
      for f in pd_diag_logging.json pd_diag_logfilter.py segments.json; do
        s=$( (sha256sum "$d/root$(dirname "$cfg")/$f" 2>/dev/null || echo -) | cut -c1-64)
        printf 'file\t%s\t%s\t%s\n' "$(dirname "$cfg")" "$f" "$s"
      done
    fi
  else env=""; readable=no; fi
  printf 'proc\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$p" "$ppid" "$uid" "$start" "$comm" "$readable" "$(printf '%s' "$cmd" | cut -c1-300)" "$env" "${nsp:-$p}"
done
for dir in $(printf '%s\n' $dirs | sort -u); do
  for f in pd_diag_logging.json pd_diag_logfilter.py segments.json; do
    s=$( (sha256sum "$dir/$f" 2>/dev/null || echo -) | cut -c1-64)
    printf 'file\t%s\t%s\t%s\n' "$dir" "$f" "$s"
  done
done
'''

# Runs on each node over SSH. Read-only: docker version / ps / inspect / top / logs, docker exec of INSIDE (reads
# /proc) and `test -w` as each vLLM user; stat and sha256sum of the three node files.
VLLM_NODE = r'''
D=docker
if ! docker version >/dev/null 2>&1; then
  if sudo -n docker version >/dev/null 2>&1; then D="sudo -n docker"; else printf 'docker\tno\n'; D=""; fi
fi
[ -n "$D" ] && printf 'docker\t%s\n' "$D"
if [ -n "$D" ] && [ -z "$CONTAINERS" ]; then
  found=$($D ps --no-trunc --format '{{.Names}}\t{{.Command}}' 2>/dev/null | awk -F'\t' 'tolower($2) ~ /vllm/ {print $1}')
  how=a
  if [ -z "$found" ]; then
    ids=$(for p in $(pgrep -f 'vllm[ ]serve' 2>/dev/null); do grep -o -E '[0-9a-f]{64}' "/proc/$p/cgroup" 2>/dev/null | head -n 1; done | sort -u)
    [ -n "$ids" ] && found=$($D ps --no-trunc --format '{{.ID}}\t{{.Names}}' 2>/dev/null | while IFS="$(printf '\t')" read -r id name; do
      printf '%s\n' "$ids" | grep -qx "$id" && printf '%s\n' "$name"; done)
    how=b
  fi
  if [ -z "$found" ]; then
    found=$(for c in $($D ps --format '{{.Names}}' 2>/dev/null); do
      $D top "$c" -eo pid,args 2>/dev/null | grep -qE 'vllm[ ]serve|vllm[.]entrypoints|vllm[A-Za-z0-9_.-]*[.]py[ ]serve' && printf '%s\n' "$c"; done)
    how=c
  fi
  for c in $found; do printf 'found\t%s\t%s\n' "$c" "$how"; done
  CONTAINERS=$(printf '%s ' $found)
fi
printf 'hostvllm\t%s\n' "$(pgrep -fc 'vllm[ ]serve' 2>/dev/null || echo 0)"
C=$(printf '%s' "$RAW_C" | sed "s|^\$HOME|$HOME|")
# ctime: a push renames the new file into place (tar keeps the original mtime, the rename changes ctime)
printf 'pushed\t%s\n' "$(stat -c %Z "$C/segments.json" 2>/dev/null)"
for f in pd_diag_logfilter.py pd_diag_logging.json segments.json; do
  printf 'ffile\t%s\t%s\t%s\n' "$f" "$(stat -c %Z "$C/$f" 2>/dev/null)" "$( (sha256sum "$C/$f" 2>/dev/null || echo -) | cut -c1-64)"
done
if [ -z "$D" ] || [ -z "$CONTAINERS" ]; then
  # No docker, or no container found: the vLLM processes as the host sees them (needs the same uid, or root).
  HOSTMODE=1 CONF_IN="$CONF_IN" sh -c "$INSIDE" 2>&1 | while IFS= read -r l; do printf 'in\t(宿主机)\t%s\n' "$l"; done
fi
for c in $CONTAINERS; do
  [ -n "$D" ] || break
  printf 'inspect\t%s\t%s\n' "$c" "$($D inspect --format '{{.State.Status}}|{{.State.StartedAt}}|{{.RestartCount}}|{{.Config.User}}' "$c" 2>&1 | head -n 1)"
  printf 'mounts\t%s\t%s\n' "$c" "$($D inspect --format '{{json .Mounts}}' "$c" 2>/dev/null | head -c 20000)"
  printf 'logdriver\t%s\t%s\n' "$c" "$($D inspect --format '{{.HostConfig.LogConfig.Type}}' "$c" 2>/dev/null | head -n 1)"
  printf 'logtail\t%s\t%s\n' "$c" "$($D logs --tail 1 "$c" 2>&1 | wc -l | tr -d ' ')"
  $D inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$c" 2>/dev/null | grep -E '^(VLLM_LOGGING_CONFIG_PATH|PYTHONPATH|VLLM_CONFIGURE_LOGGING|PD_DIAG_INSTANCE)=' | while IFS= read -r l; do printf 'cenv\t%s\t%s\n' "$c" "$l"; done
  in=$($D exec -e CONF_IN="$CONF_IN" "$c" sh -c "$INSIDE" 2>&1)
  $D logs --tail 500 "$c" 2>&1 | grep -E 'Could not load logging config|VLLM_CONFIGURE_LOGGING evaluated|Unable to configure|No module named .pd_diag_logfilter' | tail -n 3 | while IFS= read -r l; do printf 'err\t%s\t%s\n' "$c" "$(printf '%s' "$l" | cut -c1-200)"; done
  printf '%s\n' "$in" | while IFS= read -r l; do printf 'in\t%s\t%s\n' "$c" "$l"; done
  # write access to the log folder, as each user the vLLM processes run as
  for u in $(printf '%s\n' "$in" | awk -F'\t' '$1=="proc" {print $4}' | sort -u); do
    w=$($D exec -u "$u" "$c" sh -c "test -w '$LOG_IN' && echo yes || echo no" 2>/dev/null)
    printf 'write\t%s\t%s\t%s\n' "$c" "$u" "${w:-unknown}"
  done
  # [PD_DIAG] lines on the standard output, before and after the first vLLM process of this container started
  cut=$(printf '%s\n' "$in" | awk -F'\t' '$1=="clock" {now=$2; up=$3; hz=$4} $1=="proc" && $8 ~ /serve|api_server/ {s=$5; if (min=="" || s<min) min=s} END {if (min!="") printf "%d", now-up+min/hz}')
  if [ -n "$cut" ]; then
    # 10 seconds of margin: around a restart docker stamps the old processes' last lines up to a second or two late
    iso=$(date -u -d "@$((cut + 10))" +%Y-%m-%dT%H:%M:%S 2>/dev/null)
    printf 'start\t%s\t%s\t%s\n' "$c" "$cut" "$iso"
    printf 'stdout\t%s\t%s\n' "$c" "$($D logs -t --since 24h "$c" 2>&1 | grep -F '[PD_DIAG] ' | awk -v cut="$iso" '{ if ($1 >= cut) a++; else b++; last=$1 } END { printf "%d %d %s", a, b, last }')"
    printf 'stdouttotal\t%s\t%s\n' "$c" "$($D logs --since 24h "$c" 2>&1 | head -c 10000000 | wc -l | tr -d ' ')"
  fi
done
if [ -n "$STDOUT_FILE" ] && [ -r "$STDOUT_FILE" ]; then
  # Lines carry vLLM's MM-DD HH:MM:SS (local time); counted against the start of the oldest vLLM serve seen here.
  cut=$(printf '%s\n' "$in" | awk -F'\t' '$1=="clock" {now=$2; up=$3; hz=$4} $1=="proc" && $8 ~ /serve|api_server/ {s=$5; if (min=="" || s<min) min=s} END {if (min!="") printf "%d", now-up+min/hz}')
  lc=$( [ -n "$cut" ] && date -d "@$((cut + 10))" '+%m-%d %H:%M:%S' 2>/dev/null)
  printf 'stdoutfile\t%s\t%s\n' "$STDOUT_FILE" "$(tail -c 200000000 "$STDOUT_FILE" | grep -F '[PD_DIAG] ' | awk -v cut="$lc" 'match($0, /[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]/) { t=substr($0, RSTART, RLENGTH); if (cut != "" && t >= cut) a++; else b++ } END { printf "%d %d", a, b }')"
  printf 'stdoutfilemtime\t%s\t%s\n' "$STDOUT_FILE" "$(stat -c %Y "$STDOUT_FILE" 2>/dev/null)"
elif [ -n "$STDOUT_FILE" ]; then
  printf 'stdoutfile\t%s\tunreadable\n' "$STDOUT_FILE"
fi
L=$(printf '%s' "$RAW_L" | sed "s|^\$HOME|$HOME|")
for d in "$L"/*/; do
  [ -d "$d" ] || continue
  f=$(ls -t "$d" 2>/dev/null | grep '\.log$' | head -n 1)
  [ -n "$f" ] && printf 'newest\t%s\t%s\n' "$(basename "$d")" "$(stat -c %Y "$d$f" 2>/dev/null)"
  for role in api engine worker; do
    g=$(ls -t "$d" 2>/dev/null | grep "^$role\..*\.log$" | head -n 1)
    [ -n "$g" ] && printf 'role\t%s\t%s\t%s\n' "$(basename "$d")" "$role" "$(stat -c %Y "$d$g" 2>/dev/null)"
  done
done
printf 'now\t%s\n' "$(date +%s)"
'''
# The containers named in neither deploy.env nor the command line are found the way `nodes.sh selfcheck` finds them,
# the first step that finds one wins: a. the container's command has vllm; b. the container holding a `vllm serve`
# process (its cgroup); c. a running container whose `docker top` shows vLLM's launch forms. The patterns carry
# brackets and the node script no comments, so that the shell running the script never matches itself.
FOUND_HOW = {"a": "命令含 vllm", "b": "vllm serve 进程在这个容器里", "c": "容器里有 vllm 进程"}


def host_path_in_container(mounts, host_path):
    """Where a host path is visible inside the container, through the longest bind mount that contains it."""
    best = None
    for mnt in mounts or []:
        src, dst = (mnt.get("Source") or "").rstrip("/"), (mnt.get("Destination") or "").rstrip("/")
        if src and (host_path == src or host_path.startswith(src + "/")):
            if best is None or len(src) > len(best[0]):
                best = (src, dst)
    return None if best is None else best[1] + host_path[len(best[0]):]


def parse_inside(lines):
    """The INSIDE lines of one container: (clock, procs, files)."""
    procs, files, clock = [], {}, None
    for line in lines:
        parts = line.split("\t")
        if parts[0] == "clock" and len(parts) >= 4:
            clock = parts[1:4]
        elif parts[0] == "proc" and len(parts) >= 8:
            env = dict(kv.split("=", 1) for kv in (parts[8] if len(parts) > 8 else "").split("\001") if "=" in kv)
            procs.append({"pid": parts[1], "ppid": parts[2], "uid": parts[3], "start": parts[4], "comm": parts[5],
                          "readable": parts[6] == "yes", "cmd": parts[7], "env": env,
                          "nspid": parts[9] if len(parts) > 9 and parts[9] else parts[1]})
        elif parts[0] == "file" and len(parts) >= 4:
            files.setdefault(parts[1], {})[parts[2]] = parts[3]
    return clock, procs, files


def in_pythonpath(d, pythonpath):
    """Whether folder d is an entry of PYTHONPATH, a trailing slash on either side ignored (known issue 9)."""
    want = d.rstrip("/") or "/"
    return any((x.rstrip("/") or "/") == want for x in (pythonpath or "").split(":") if x)


def is_api(p):
    return bool(re.search(r"\bserve\b|api_server", p["cmd"]))


def port_of(p):
    m = re.search(r"--port[= ](\d+)", p["cmd"])
    return m.group(1) if m else None


def to_procs(addr, names, clock, procs):
    """criteria.Proc for each vLLM process: instance from the --port of its API-server ancestor; an API server carries
    the PD_DIAG_INSTANCE_SOURCE its children see (the filter sets it in the parent's environment after exec, so
    /proc/<pid>/environ of the parent never shows it)."""
    if not clock:
        return []
    now, up, hz = float(clock[0]), float(clock[1]), float(clock[2] or 100)
    by_pid = {p["pid"]: p for p in procs}
    name_of = {port: n for n, port in names}

    def api_of(p):
        seen = 0
        while p is not None and seen < 32:
            if is_api(p):
                return p
            p = by_pid.get(p["ppid"])
            seen += 1
        return None

    src_of_api = {}
    for p in procs:
        a = api_of(p)
        if a is not None and a is not p and p["env"].get("PD_DIAG_INSTANCE_SOURCE"):
            src_of_api.setdefault(a["pid"], p["env"]["PD_DIAG_INSTANCE_SOURCE"])
    out = []
    for p in procs:
        a = api_of(p)
        inst = name_of.get(port_of(a) or "", "") if a is not None else ""
        env = {k: v for k, v in p["env"].items() if k in ENV_KEYS}
        if a is p and p["pid"] in src_of_api and not env.get("PD_DIAG_INSTANCE_SOURCE"):
            env["PD_DIAG_INSTANCE_SOURCE"] = src_of_api[p["pid"]]
        out.append(K.Proc(addr=addr, instance=inst, pid=int(p["nspid"]), start_epoch=now - up + float(p["start"]) / hz,
                          env=env, is_child=a is not p, readable=p["readable"]))
    return out


def probe_vllm(cluster, addr, containers=None, stdout_file=None):
    """Read-only look at the vLLM processes of one node. Prints nothing.
    containers: the container names to look into; None takes deploy.env's NODE_VLLM_CONTAINERS for this address, and
    when that has none, the node is searched (FOUND_HOW). stdout_file: None takes NODE_VLLM_STDOUT_FILES.
    Returns (procs, facts): procs is a list of criteria.Proc; facts holds what the node showed:
      docker            "docker" | "sudo -n docker" | None (the SSH user cannot run docker)
      containers        the vLLM containers looked into ([] when none: then the host's view, targets ["(宿主机)"])
      container_source  "命令行" | "deploy.env" | "自动" | "宿主机"
      found             [(name, how)] found on the node when neither the command line nor deploy.env named one
      unreadable_pids   processes whose environment could not be read
      filter_files      {file: (ctime, sha256)} of the three node files in NODE_CONF_DIR
      vllm_start        {container: epoch of its oldest vLLM API server}
    Raises RuntimeError when the node cannot be reached."""
    c = cluster
    names = c.instances.get(addr, [])
    if containers is None:
        containers, source = c.containers().get(addr, []), "deploy.env"
    else:
        source = "命令行"
    if stdout_file is None:
        stdout_file = c.stdout_files().get(addr, "")
    pre = "CONTAINERS=%s\nCONF_IN=%s\nLOG_IN=%s\nRAW_L=%s\nRAW_C=%s\nSTDOUT_FILE=%s\nINSIDE=%s\n" % (
        shlex.quote(" ".join(containers)), shlex.quote(c.conf_in()), shlex.quote(c.log_dir_in), shlex.quote(c.log_host()),
        shlex.quote(c.conf_host()), shlex.quote(stdout_file), shlex.quote(INSIDE))
    code, out, err = ssh(c, addr, pre + VLLM_NODE, timeout=300)
    if code != 0 and not out.strip():
        raise RuntimeError("SSH 连不上（%s）" % err.strip()[:80])
    rows = [l.split("\t") for l in out.splitlines()]
    get = lambda key: [x[1:] for x in rows if x and x[0] == key]
    dock = (get("docker") or [["no"]])[0][0]
    found = [(x[0], FOUND_HOW.get(x[1], x[1])) for x in get("found") if len(x) >= 2]
    if not containers and found:
        containers, source = [n for n, _ in found], "自动"
    host_mode = any(len(x) > 1 and x[0] == "in" and x[1] == "(宿主机)" for x in rows)
    real = containers if dock != "no" else []
    # what was looked into: the containers, or "(宿主机)" for the host's own view of the processes
    conts = real if real else (["(宿主机)"] if host_mode else [])
    if not real:
        source = "宿主机"
    facts = {"ssh_code": code, "ssh_err": err, "out": out, "rows": rows,
             "docker": None if dock == "no" else dock, "containers": list(real), "targets": conts,
             "container_source": source, "found": found,
             "stdout_file": stdout_file, "names": names,
             "now": int((get("now") or [["0"]])[0][0] or 0), "pushed": int((get("pushed") or [["0"]])[0][0] or 0),
             "filter_files": {}, "role_files": {}, "newest": {}, "vllm_start": {}, "per_container": {},
             "unreadable_pids": []}
    for x in get("ffile"):
        if len(x) >= 3:
            facts["filter_files"][x[0]] = (int(x[1] or 0), x[2] if x[2] not in ("", "-") else "")
    for x in get("role"):
        if len(x) >= 3:
            facts["role_files"].setdefault(x[0], {})[x[1]] = int(x[2] or 0)
    facts["newest"] = {x[0]: int(x[1] or 0) for x in get("newest") if len(x) >= 2}
    procs = []
    for cname in conts:
        lines = ["\t".join(x[2:]) for x in rows if len(x) > 2 and x[0] == "in" and x[1] == cname]
        clock, raw, files = parse_inside(lines)
        api = [p for p in raw if is_api(p)]
        start = None
        if clock and api:
            now, up, hz = float(clock[0]), float(clock[1]), float(clock[2] or 100)
            start = min(now - up + float(p["start"]) / hz for p in api)
            facts["vllm_start"][cname] = start
        facts["per_container"][cname] = {"clock": clock, "procs": raw, "files": files, "start": start}
        facts["unreadable_pids"] += [int(p["nspid"]) for p in raw if not p["readable"]]
        procs += to_procs(addr, names, clock, raw)
    return procs, facts


def probe_cached(c, addr, args=None):
    """probe_vllm once per node and run; (None, error text) when the node cannot be reached."""
    if addr not in c.probes:
        cli_c = node_containers(getattr(args, "containers", None) or "")
        cli_f = addr_map(getattr(args, "stdout_file", None) or "")
        try:
            c.probes[addr] = probe_vllm(c, addr, cli_c[addr] if addr in cli_c else None, cli_f.get(addr))
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            c.probes[addr] = (None, str(e))
    return c.probes[addr]


def record_states(c, args=None):
    """{addr: {instance: state}} by criteria.instance_record_state, from each node's vLLM processes and the rows since
    their start; None for an address whose processes could not be read, or when ClickHouse cannot be queried."""
    tbl = table(c)
    out = {}
    for addr in c.instances:
        procs, facts = probe_cached(c, addr, args)
        procs = [p for p in (procs or []) if p.instance]
        if not procs:
            out[addr] = None
            continue
        rows, err = ch(c, K.records_since_start_sql(tbl, procs))
        if rows is None:
            out[addr] = None
            continue
        seen = {(x["instance"], int(x["pid"])) for x in rows if int(x.get("n", 0))}
        out[addr] = K.instance_record_state(procs, seen)
    return out


def written_since_start(c, addr):
    """Instances of addr whose log folder on the node has a file changed after their vLLM API server started: the
    filter is writing there, so "no rows in ClickHouse since the start" means the rows did not arrive, not that
    vLLM had no request."""
    procs, facts = c.probes.get(addr) or (None, None)
    if procs is None:
        return set()
    start = {}
    for p in procs:
        if p.instance and not p.is_child:
            start[p.instance] = min(start.get(p.instance, p.start_epoch), p.start_epoch)
    port = {n: pt for n, pt in c.instances.get(addr, [])}
    newest = facts.get("newest") or {}
    return {n for n, t in start.items() if newest.get(port.get(n, ""), 0) > t}


def split_states(c, addr, st):
    """(states for criteria.judge_process_states, instances written on the node but without rows in ClickHouse)."""
    st = dict(st or {})
    moved = sorted(n for n in written_since_start(c, addr) if st.get(n) not in (None, "records"))
    for n in moved:
        st[n] = "records"
    return st, moved


def cmd_vllm(c, r, args):
    r.section("vllm（%s 模式）" % c.mode)
    conf_host, log_host, conf_in, log_in = c.conf_host(), c.log_host(), c.conf_in(), c.log_dir_in
    if conf_in != conf_host or log_in != log_host:
        r.add("INFO", "容器里的路径：三个文件 %s（宿主机 %s），日志 %s（宿主机 %s）；export 要写容器里的路径" % (conf_in, conf_host, log_in, log_host))
    else:
        r.add("INFO", "容器里外同路径：三个文件 %s，日志 %s" % (conf_in, log_in))
    versions = proc_versions(c)
    states = record_states(c, args)
    idle_addrs = []
    for addr, names in c.instances.items():
        procs, facts = probe_cached(c, addr, args)
        if procs is None:
            r.add("FAIL", "%s %s" % (addr, facts))
            continue
        r.keep("vllm %s" % addr, facts["out"] + ("\n" + facts["ssh_err"] if facts["ssh_err"].strip() else ""))
        rows = facts["rows"]
        get = lambda key: [x[1:] for x in rows if x and x[0] == key]
        if not facts["docker"]:
            r.add("WARN", "%s SSH 用户不能执行 docker（不在 docker 组，也不能免密 sudo docker），没有核对真实容器" % addr)
        elif facts["container_source"] == "自动":
            hows = sorted({h for _, h in facts["found"]})
            r.add("INFO", "%s 找到 vLLM 容器 %s（%s）；不对时填 deploy.env 的 NODE_VLLM_CONTAINERS" % (
                addr, " ".join(n for n, _ in facts["found"]), "、".join(hows)))
        elif facts["container_source"] == "宿主机":
            r.add("WARN", "%s 没有找到 vLLM 容器，没有核对真实容器 → 填 deploy.env 的 NODE_VLLM_CONTAINERS" % addr)
        if not facts["containers"]:
            hv = int((get("hostvllm") or [["0"]])[0][0] or 0)
            if hv:
                r.add("INFO", "%s 命令行含「vllm serve」的进程 %d 个（含容器里的）" % (addr, hv))
        ctx = {"facts": facts, "states": (states.get(addr) or {})}
        for cname in facts["targets"]:
            check_container(c, r, addr, cname, rows, conf_host, log_host, conf_in, log_in, names, ctx)
        conts = facts["targets"]
        idle = bool(conts) and len(ctx.get("idle", [])) == len(conts)
        if idle:
            idle_addrs.append(addr)
        # the filter writes: newest file per instance folder on the host
        now, newest = facts["now"], facts["newest"]
        st = split_states(c, addr, states.get(addr))[0]
        quiet = {n for n, s in st.items() if s in ("no_records_filter_created", "unknown")}
        stale = [(n, "%s %s" % (n, "没有文件" if p not in newest else "%d 分钟没有新写入" % ((now - newest[p]) // 60))) for n, p in names
                 if p not in newest or now - newest[p] > 180]
        if c.mode == "vllm":
            hard = [t for n, t in stale if not (idle or n in quiet)]
            soft = [t for n, t in stale if idle or n in quiet]
            if soft:
                r.add("WARN", "%s 这些实例的日志目录（%s/<端口>）没有新文件：%s；vLLM 这次启动以后还没有记录（见结论一行），过滤器要等第一条记录才建文件" % (addr, log_host, "，".join(soft)))
            if hard:
                r.add("FAIL", "%s 这些实例的日志目录（%s/<端口>）没有新文件：%s → 过滤器没在写，看上面几行" % (addr, log_host, "，".join(hard)))
            if not stale:
                r.add("OK", "%s %d 个实例的日志目录 3 分钟内都有新写入" % (addr, len(names)))
        sf = get("stdoutfile")
        if sf:
            path, n = sf[0][0], sf[0][1]
            if n == "unreadable":
                r.add("FAIL", "%s 标准输出文件 %s 读不了（deploy.env 的 NODE_VLLM_STDOUT_FILES）" % (addr, path))
            else:
                after, before = (n.split(" ") + ["0", "0"])[:2]
                if int(after or 0):
                    r.add("FAIL", "%s 标准输出文件 %s：vLLM 这次启动以后有 %s 行 [PD_DIAG]（按每行开头的时间算）" % (addr, path, after))
                elif int(before or 0):
                    r.add("OK", "%s 标准输出文件 %s：%s 行 [PD_DIAG] 都早于 vLLM 这次启动" % (addr, path, before))
                else:
                    r.add("OK", "%s 标准输出文件 %s 里没有 [PD_DIAG]" % (addr, path))
    # versions of segments.json the running filters report, judged as `check` judges them
    if versions is None:
        r.add("WARN", "读不了 ClickHouse 的自检行，没有核对过滤器版本")
        return
    flat = {}
    for a, st in states.items():
        flat.update(split_states(c, a, st)[0] if st else {})
    raw = versions
    alive, node_now, ages = {}, {}, []
    for a, (procs, facts) in c.probes.items():
        if procs is None or not facts.get("now"):
            continue
        alive[a] = [(p.instance, int(p.pid), float(p.start_epoch)) for p in procs]
        node_now[a] = facts["now"]
        if facts.get("pushed"):
            ages.append(facts["now"] - facts["pushed"])
    addr_of = {n: a for a, names in c.instances.items() for n, _ in names}
    versions = K.live_versions(raw, alive, addr_of)
    th = K.chain_thresholds(c.segments.get("line") or {}, c.get("PULL_INTERVAL_S", "10"))
    starting = K.starting_instances(raw, alive, addr_of, node_now, th["clickhouse"])
    now = time.time()
    pushed = now - max(0, min(ages)) if ages else 0
    vs = K.judge_versions(versions, c.version, c.mode, pushed, now, states=flat if c.mode == "vllm" else None, starting=starting)
    if not vs and c.mode == "vllm":
        rec = sorted(i for i, s in flat.items() if s == "records")
        if not flat and idle_addrs and len(idle_addrs) == len(c.instances):
            r.add("WARN", "最近 3 分钟没有任何过滤器的自检行：各台的 vLLM 这次启动以后都还没有记录，过滤器要等第一条记录才开始写自检行 → 有请求以后再跑一次")
        elif (rec or not flat) and young_starts(c):
            # the first self-check line comes 60 s after the handler is created, then up to a pull interval to ClickHouse
            r.add("WARN", "最近 3 分钟没有过滤器的自检行，但 vLLM 启动不到 2 分钟（%s）：过滤器每 60 秒写一行自检 → 2 分钟后再跑一次 bin/diag.sh vllm" % young_starts(c))
        elif rec or not flat:
            # a filter with records keeps one self-check line a minute, with or without requests
            r.add("FAIL", "最近 3 分钟没有过滤器的自检行%s（过滤器有记录以后每分钟写一行）→ 过滤器没在写，看上面几行" % (
                "：%s 这次启动以后有过记录" % " ".join(rec) if rec else ""))
    for v in vs:
        r.add(*prefixed("", v))
    for addr in c.instances:
        if c.mode != "vllm":
            break
        st, moved = split_states(c, addr, states.get(addr))
        for v in K.judge_process_states(st, "版本"):
            r.add(*prefixed(addr, v))
        if moved:
            r.add("WARN", "%s %s：节点上这次启动以后写过文件，ClickHouse 里还没有这次启动以后的行 → bin/diag.sh chain 看是哪一段没到" % (addr, " ".join(moved)))


def young_starts(c, limit=120):
    """'<addr> <container>' of the vLLM containers started less than `limit` seconds ago, by the node clock."""
    out = []
    for addr, (procs, facts) in c.probes.items():
        if procs is None:
            continue
        for cname, t in (facts.get("vllm_start") or {}).items():
            if facts.get("now") and facts["now"] - t < limit:
                out.append("%s %s" % (addr, cname))
    return "、".join(out)


def proc_versions(c):
    """[(version, instance, pid, first, last)] of the filters writing self-check lines in the last 3 minutes, as `check`
    reads them; cmd_vllm leaves out the processes that stopped (criteria.live_versions)."""
    rows, err = ch(c, "SELECT argMax(config_version, ts) AS v, instance, pid, toUnixTimestamp64Milli(min(ts)) / 1000 AS first, "
                      "toUnixTimestamp64Milli(max(ts)) / 1000 AS last FROM vllm_pd.filter_selfcheck "
                      "WHERE ts >= now64(3) - INTERVAL 3 MINUTE GROUP BY instance, pid")
    if rows is None:
        return None
    return [(x["v"], x["instance"], int(x["pid"]), float(x["first"]), float(x["last"])) for x in rows]


def check_container(c, r, addr, cname, rows, conf_host, log_host, conf_in, log_in, names, ctx=None):
    ctx = ctx or {}
    facts = ctx.get("facts") or {}

    def mine(key):
        return [x[2:] for x in rows if len(x) > 1 and x[0] == key and x[1] == cname]
    ins = mine("inspect")
    state = (ins[0][0] if ins else "").split("|")
    host_mode = cname == "(宿主机)"
    if host_mode:
        state = ["running", "", "", ""]
    seen_err = set()
    for e in mine("err"):
        msg = e[0].strip()
        if msg.startswith("raise ") or msg in seen_err:
            continue
        seen_err.add(msg)
        r.add("FAIL", "%s %s：vLLM 启动时的报错：%s" % (addr, cname, msg[:200]))
    if not state or state[0] != "running":
        r.add("FAIL", "%s 容器 %s 不在运行（%s）" % (addr, cname, "|".join(state)[:120] or "docker inspect 没有结果"))
        return
    if not host_mode:
        r.add("INFO", "%s 容器 %s 在运行，启动于 %s，重启 %s 次" % (addr, cname, state[1][:19], state[2] if len(state) > 2 else "?"))
    # mounts: the host folders seen inside
    try:
        mounts = json.loads((mine("mounts") or [["[]"]])[0][0] or "[]")
    except ValueError:
        mounts = []
    deferred = []
    for what, host, inside in ((("三个文件", conf_host, conf_in), ("日志", log_host, log_in)) if not host_mode else ()):
        seen = host_path_in_container(mounts, host) if host.startswith("/") else None
        if seen is None:
            # Not a direct bind of the host folder (a docker volume, a parent folder mounted elsewhere...): judged by
            # the evidence below, the files the container sees and the files the filter writes on the host.
            deferred.append((what, host, inside, sorted({m.get("Type", "?") for m in mounts})))
        elif seen != inside:
            r.add("FAIL", "%s %s：宿主机的 %s 在容器里是 %s，不是 %s → 改挂载，或改 deploy.env 的 %s" % (
                addr, cname, host, seen, inside, "NODE_CONF_DIR_IN_CONTAINER" if what == "三个文件" else "NODE_LOG_DIR_IN_CONTAINER"))
        else:
            r.add("OK", "%s %s：%s 目录 %s 在容器里同样是 %s" % (addr, cname, what, host, seen))
    # processes and their environment
    pc = (facts.get("per_container") or {}).get(cname)
    if pc is None:
        clock, procs, files = parse_inside(["\t".join(x) for x in mine("in")])
    else:
        clock, procs, files = pc["clock"], pc["procs"], pc["files"]
    for what, host, inside, types in deferred:
        if what == "三个文件":
            got = files.get(inside, {})
            if all(got.get(f) == c.node_sums[f] for f in NODE_FILES):
                r.add("OK", "%s %s：宿主机的 %s 不是直接挂进来的（挂载类型 %s），但容器里 %s 的三个文件和下发的相同" % (addr, cname, host, "、".join(types) or "无", inside))
            else:
                r.add("FAIL", "%s %s：容器里 %s 看不到下发的三个文件（宿主机 %s 没有挂进容器？）→ 容器加挂载 -v %s:%s" % (addr, cname, inside, host, host, inside))
        else:
            r.add("INFO", "%s %s：宿主机的 %s 不是直接挂进来的（挂载类型 %s）；过滤器写的文件有没有出现在宿主机，看「日志目录 3 分钟内都有新写入」一行" % (addr, cname, host, "、".join(types) or "无"))
    # standard output: in docker logs, or in the file NODE_VLLM_STDOUT_FILES names (design 2.2)
    if not host_mode and not facts.get("stdout_file"):
        drv = ((mine("logdriver") or [[""]])[0][0] or "?").strip()
        if int((mine("logtail") or [["0"]])[0][0] or 0):
            r.add("OK", "%s 容器 %s 的标准输出在 docker logs 里（日志驱动 %s）" % (addr, cname, drv))
        else:
            r.add("WARN", "%s 容器 %s 的标准输出不在 docker logs 里（日志驱动 %s，没有读到行）→ 填 deploy.env 的 NODE_VLLM_STDOUT_FILES" % (addr, cname, drv))
    if not procs and host_mode:
        r.add("FAIL", "%s 宿主机上 SSH 用户看到的进程里没有 vLLM 进程（命令行含 vllm 的）→ 让 SSH 用户能执行 docker，或在 deploy.env 的 NODE_VLLM_CONTAINERS 填容器名" % addr)
        return
    if not procs:
        r.add("FAIL", "%s %s：容器里没有找到 vLLM 进程（命令行含 vllm 的）" % (addr, cname))
        return
    api = [p for p in procs if is_api(p)]
    kids = [p for p in procs if p not in api]
    unread = [p for p in procs if not p["readable"]]
    if unread:
        r.add("WARN", "%s 容器 %s：读不了 vLLM 进程的环境变量（%d 个进程，docker exec 的默认用户没有权限），结论只靠文件和标准输出判断" % (addr, cname, len(unread)))
    readable = [p for p in procs if p["readable"]]
    r.add("INFO", "%s %s：vLLM 进程 %d 个（主进程 %d 个，子进程 %d 个）" % (addr, cname, len(procs), len(api), len(kids)))
    cenv = dict(l[0].split("=", 1) for l in mine("cenv") if "=" in l[0])
    # VLLM_CONFIGURE_LOGGING=0
    off = [p for p in readable if p["env"].get("VLLM_CONFIGURE_LOGGING", "").strip() == "0"]
    if off:
        r.add("FAIL", "%s %s：%d 个进程设了 VLLM_CONFIGURE_LOGGING=0，vLLM 不会读 VLLM_LOGGING_CONFIG_PATH，[PD_DIAG] 照样进标准输出 → 去掉这个变量（容器的 -e 或启动脚本里），重启 vLLM" % (addr, cname, len(off)))
    api_with = [p for p in api if p["readable"] and p["env"].get("VLLM_LOGGING_CONFIG_PATH")]
    api_without = [p for p in api if p["readable"] and not p["env"].get("VLLM_LOGGING_CONFIG_PATH")]
    if api_without:
        r.add("FAIL", "%s %s：%d 个 vLLM 主进程没有 VLLM_LOGGING_CONFIG_PATH（%s）→ %s；容器的配置里%s这个变量" % (
            addr, cname, len(api_without), " ".join("pid %s" % p["pid"] for p in api_without[:6]), EXPORT_WHERE, "有" if "VLLM_LOGGING_CONFIG_PATH" in cenv else "也没有"))
    kids_without = [p for p in kids if p["readable"] and not p["env"].get("VLLM_LOGGING_CONFIG_PATH")]
    if api_with and kids_without:
        r.add("FAIL", "%s %s：主进程有 VLLM_LOGGING_CONFIG_PATH，%d 个子进程没有（%s）→ 子进程没有继承：变量要在 vllm serve 启动前就设在它的环境里，不要在 Python 里改 os.environ 后再起子进程" % (
            addr, cname, len(kids_without), "，".join("%s pid %s" % (p["comm"], p["pid"]) for p in kids_without[:6])))
    if api_with and kids and not kids_without:
        r.add("OK", "%s %s：主进程和 %d 个子进程都有 VLLM_LOGGING_CONFIG_PATH" % (addr, cname, len(kids)))
    # the folder the variable points to
    paths = sorted({p["env"]["VLLM_LOGGING_CONFIG_PATH"] for p in readable if p["env"].get("VLLM_LOGGING_CONFIG_PATH")})
    for path in paths:
        d = os.path.dirname(path)
        got = files.get(d, {})
        miss = [f for f in NODE_FILES if got.get(f, "-") in ("-", "")]
        diff = [f for f in NODE_FILES if f not in miss and got.get(f) != c.node_sums[f]]
        if miss:
            r.add("FAIL", "%s %s：VLLM_LOGGING_CONFIG_PATH=%s，这个目录里缺 %s；下发的目录（容器里）是 %s → 启动脚本的两行 export 改成 %s，重启 vLLM" % (
                addr, cname, path, " ".join(miss), conf_in, conf_in))
        elif diff:
            r.add("FAIL", "%s %s：%s 里的 %s 和监控机的不同（旧文件？）→ 让 export 指向 %s，或 bin/nodes.sh push" % (addr, cname, d, " ".join(diff), conf_in))
        else:
            r.add("OK", "%s %s：VLLM_LOGGING_CONFIG_PATH=%s，目录里三个文件都在，和监控机相同" % (addr, cname, path))
        nopp = [p for p in readable if p["env"].get("VLLM_LOGGING_CONFIG_PATH") == path and not in_pythonpath(d, p["env"].get("PYTHONPATH"))]
        if nopp:
            r.add("FAIL", "%s %s：%d 个进程的 PYTHONPATH 里没有 %s，过滤模块 import 不了 → export PYTHONPATH=%s${PYTHONPATH:+:$PYTHONPATH}" % (addr, cname, len(nopp), d, d))
    # instance identity and ports
    fixed = {}
    for p in api:
        v = p["env"].get("PD_DIAG_INSTANCE", "")
        if v and v != "auto":
            fixed.setdefault(v, []).append(p["pid"])
    for v, pids in fixed.items():
        if len(pids) > 1:
            r.add("FAIL", "%s %s：%d 个主进程都设了 PD_DIAG_INSTANCE=%s，会写进同一个目录 → 不设，或设成 auto" % (addr, cname, len(pids), v))
    ports = {}
    for p in api:
        ports[port_of(p) or "8000（没写 --port）"] = p["pid"]
    want_ports = {port for _, port in names}
    extra = sorted(set(ports) - want_ports)
    missing = sorted(want_ports - set(ports))
    if missing:
        r.add("WARN", "%s %s：清单里这台的端口 %s 在这个容器里没有 vLLM 主进程（可能在别的容器里）" % (addr, cname, " ".join(missing)))
    if extra:
        r.add("FAIL", "%s %s：这些端口的 vLLM 不在清单里：%s → 改 inventory.json，或 vllm serve 的 --port" % (addr, cname, " ".join(extra)))
    if kids and api_with:
        src = sorted({p["env"].get("PD_DIAG_INSTANCE_SOURCE", "") for p in kids})
        if src == [""]:
            r.add("WARN", "%s %s：子进程里没有 PD_DIAG_INSTANCE_SOURCE：主进程起子进程之前没有加载过滤器（日志配置没生效？）" % (addr, cname))
        else:
            r.add("OK", "%s %s：子进程带 PD_DIAG_INSTANCE_SOURCE=%s，主进程在起子进程之前已经加载了过滤器" % (addr, cname, "/".join(s for s in src if s)))
    # write access
    for x in mine("write"):
        if len(x) >= 2 and x[1] != "yes":
            r.add("FAIL", "%s %s：容器里 uid %s 写不了日志目录 %s（%s）→ 改宿主机 %s 的属主或权限" % (addr, cname, x[0], log_in, x[1], log_host))
    # standard output
    st = mine("start")
    so = mine("stdout")
    if st and so:
        after, before, last = (so[0][0].split(" ") + ["", "", ""])[:3]
        started = time.strftime("%m-%d %H:%M:%S", time.localtime(int(st[0][0])))
        total = int((mine("stdouttotal") or [["0"]])[0][0] or 0)
        if int(after or 0):
            r.add("FAIL", "%s %s：vLLM 这次启动（%s）10 秒以后，标准输出里有 %s 行 [PD_DIAG] → 看结论一行" % (addr, cname, started, after))
        elif int(before or 0):
            r.add("OK", "%s %s：标准输出里的 %s 行 [PD_DIAG] 都是 vLLM 这次启动（%s）以前的（最后一行 %s），启动以后没有" % (
                addr, cname, before, started, last[:19].replace("T", " ") + " UTC"))
        elif total:
            r.add("OK", "%s %s：标准输出里没有 [PD_DIAG]（最近 24 小时 %d 行别的日志）" % (addr, cname, total))
    conclude(c, r, addr, cname, procs, api, kids, files, clock, so, ctx, names)


def conclude(c, r, addr, cname, procs, api, kids, files, clock, so, ctx, names):
    """One line from five signals (the advisor's table): S1 environment, S2 start against push, S3 files, S4 the filter
    wrote since start, S5 [PD_DIAG] on stdout since start."""
    if not clock or not api:
        return
    facts = ctx.get("facts") or {}
    now, up, hz = float(clock[0]), float(clock[1]), float(clock[2] or 100)
    start = min(now - up + float(p["start"]) / hz for p in api)
    ff = facts.get("filter_files") or {}
    # Design 2.10, blocker 5: "not restarted" only when pd_diag_logfilter.py or pd_diag_logging.json changed after
    # vLLM started *and* differs from every version vLLM could have started with; segments.json is reread.
    ctimes = {f: v[0] for f, v in ff.items()}
    shas = {f: v[1] for f, v in ff.items()}
    restart, changed = K.restart_needed(ctimes, shas, start + 5, c.known_filter)
    pushed = max([ctimes.get(f, 0) for f in K.FILTER_FILES] or [0])
    readable = [p for p in procs if p["readable"]]
    s1_api = any(p["env"].get("VLLM_LOGGING_CONFIG_PATH") for p in api if p["readable"])
    s1_kids_missing = [p for p in kids if p["readable"] and not p["env"].get("VLLM_LOGGING_CONFIG_PATH")]
    s1_off = any(p["env"].get("VLLM_CONFIGURE_LOGGING", "").strip() == "0" for p in readable)
    paths = {p["env"]["VLLM_LOGGING_CONFIG_PATH"] for p in readable if p["env"].get("VLLM_LOGGING_CONFIG_PATH")}
    s3 = all(all(files.get(os.path.dirname(x), {}).get(f, "-") not in ("-", "") for f in NODE_FILES) for x in paths) if paths else None
    ports = {port_of(p) for p in api if port_of(p)}
    rf = facts.get("role_files") or {}
    roles_new = {role for port in ports for role, t in rf.get(port, {}).items() if t > start}
    young = now - start < 70
    s4 = bool(roles_new)
    # The filter writes PD_DIAG_INSTANCE_SOURCE into the environment when its handler is created, before vLLM starts
    # its children; a child that has it proves the handler exists even when no record ever reached it (the filter
    # writes nothing, not even self-check lines, until a first record has opened its file).
    loaded = any(p["env"].get("PD_DIAG_INSTANCE_SOURCE") for p in kids if p["readable"])
    after = before = 0
    if so:
        a, b = (so[0][0].split(" ") + ["0", "0"])[:2]
        after, before = int(a or 0), int(b or 0)
    hm = lambda t: time.strftime("%m-%d %H:%M:%S", time.localtime(t))
    sig = "S1 主进程%s变量、%d 个子进程没有、CONFIGURE_LOGGING%s；S2 启动 %s、过滤器文件下发 %s%s；S3 %s；S4 启动后写过的文件 %s，过滤器%s创建（子进程的 PD_DIAG_INSTANCE_SOURCE）；S5 启动 10 秒后 %d 行、之前 %d 行" % (
        "有" if s1_api else "没有", len(s1_kids_missing), "=0" if s1_off else "未关", hm(start),
        hm(pushed) if pushed else "?", "，内容变了：%s" % " ".join(changed) if changed else "，内容和已知版本相同",
        {None: "没有路径", True: "文件都在", False: "缺文件"}[s3],
        "、".join(sorted(roles_new)) or "无", "已" if loaded else "未", after, before)
    pre = "%s %s 结论：" % (addr, cname)
    if restart:
        r.add("FAIL", pre + "vLLM 没有重启，新下发的节点文件没生效（vLLM %s 启动，%s %s 换过，内容和 vLLM 启动时的不同）→ 重启 vLLM" % (
            hm(start), "、".join(changed), hm(max(ctimes.get(f, 0) for f in changed))))
    elif s1_off and not s1_api:
        r.add("FAIL", pre + "日志配置被关掉（VLLM_CONFIGURE_LOGGING=0）→ 去掉这个变量，加上两行 export，重启 vLLM")
    elif not s1_api and not s4:
        r.add("FAIL", pre + "变量没有进到 vLLM 进程：export 在容器外，或不在起 vLLM 的那个 shell 里 → %s；改完重启 vLLM" % EXPORT_WHERE)
    elif s1_api and s1_kids_missing:
        r.add("FAIL", pre + "子进程没有继承变量：%d 个子进程没有 VLLM_LOGGING_CONFIG_PATH（主进程有）→ 变量要在 vllm serve 启动前设在它的环境里，不要在进程里改环境变量后再起子进程；重启 vLLM" % len(s1_kids_missing))
    elif s1_api and s3 is False:
        r.add("FAIL", pre + "变量指向的目录里没有三个文件 → export 改成 %s，重启 vLLM" % c.conf_in())
    elif s1_api and s3 and (s4 or loaded) and after:
        r.add("FAIL", pre + "日志配置加载了、过滤器已经创建%s，但 [PD_DIAG] 仍在标准输出：发这些记录的 logger 不在 vllm 之下，走不到过滤器 → 看补丁 pd_diagnostics.py 用的 logger 名，把它加进 pd_diag_logging.json 的 loggers" % (
            "（%s 文件是启动后写的）" % "、".join(sorted(roles_new)) if s4 else "（子进程带 PD_DIAG_INSTANCE_SOURCE），日志目录里却没有新文件"))
    elif s1_api and s3 and s4 and not after:
        r.add("OK", pre + "日志配置生效，过滤器在写，标准输出里没有新的 [PD_DIAG]%s" % ("（%d 行是这次启动以前的）" % before if before else ""))
    elif s1_api and s3 and not s4 and loaded and not after:
        # idle since the restart: the handler exists, but it opens its files (and starts self-checks) on a first record
        ctx.setdefault("idle", []).append(cname)
        r.add("WARN", pre + "日志配置加载了、过滤器已经创建（子进程带 PD_DIAG_INSTANCE_SOURCE），这次启动以后还没有 [PD_DIAG] 记录（日志目录里没有新文件，标准输出里也没有新行）：多半是还没有请求 → 有请求以后再跑一次 bin/diag.sh vllm")
    elif s1_api and s3 and not s4 and young:
        r.add("WARN", pre + "vLLM 启动不到 70 秒、日志目录里还没有新文件；过滤器每 60 秒写一行自检，1 分钟后再跑一次 bin/diag.sh vllm 才分得清")
    elif s1_api and s3 and not s4:
        r.add("FAIL", pre + "变量和文件都对，但这次启动以后日志目录里没有新文件：过滤器没有起来（看 vLLM 的启动日志里有没有 logging 的报错）")
    else:
        r.add("WARN", pre + "组合没见过，原样写下：" + sig)
    r.keep("signals %s %s" % (addr, cname), sig)


# ==================== chain ====================
CHAIN_NODE = r'''
L=$(printf '%s' "$RAW_L" | sed "s|^\$HOME|$HOME|")
printf 'now\t%s\n' "$(date +%s)"
for d in "$L"/*/; do
  [ -d "$d" ] || continue
  f=$(ls -t "$d" 2>/dev/null | grep '\.log$' | head -n 1)
  [ -n "$f" ] && printf 'newest\t%s\t%s\t%s\n' "$(basename "$d")" "$(stat -c %Y "$d$f" 2>/dev/null)" "$(tail -n 1 "$d$f" | cut -c1-29)"
done
'''


def iso_epoch(text):
    """2026-11-13T05:22:33.123+08:00 -> epoch seconds, or None."""
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?([+-]\d\d):?(\d\d)", text or "")
    if not m:
        return None
    t = datetime.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
    off = (int(m.group(2)) * 60 + (1 if m.group(2)[0] != "-" else -1) * int(m.group(3))) * 60
    return (t - datetime.datetime(1970, 1, 1)).total_seconds() - off


def cmd_chain(c, r, args):
    r.section("chain（节点日志 → 镜像 → 采集器 → ClickHouse）")
    tbl = table(c)
    line = c.segments.get("line") or {}
    th = K.chain_thresholds(line, c.get("PULL_INTERVAL_S", "10"))
    r.add("INFO", "阈值：节点 %.0f 秒、镜像 %.0f 秒、ClickHouse %.0f 秒以内算 OK，3 倍以内 WARN（自检每 %s 秒、flush %s 秒、拉取每 %s 秒）" % (
        th["node"], th["mirror"], th["clickhouse"], line.get("selfcheck_seconds", 60), line.get("flush_seconds", 5), c.get("PULL_INTERVAL_S", "10") or 10))
    # vllm mode: an instance whose vLLM has written no record since its start writes nothing at all, not even a
    # self-check line (fact 4). Such instances are judged by process, as `check` judges them (design 2.1), and left
    # out of the age judgements below.
    states = record_states(c, args) if c.mode == "vllm" else {}
    quiet = {}   # addr -> instances without a record since their start
    for addr in c.instances:
        # instances the filter wrote on the node since the start are judged below like any other: their rows should
        # be on their way
        st = split_states(c, addr, states.get(addr))[0]
        quiet[addr] = {n for n, s in st.items() if s != "records"}
        for v in K.judge_process_states(st, "链路通"):
            r.add(*prefixed(addr, v))
        if c.mode == "vllm" and states.get(addr) is None:
            r.add("INFO", "%s 读不到 vLLM 进程（没有 docker 权限或没有找到容器），没有按进程判；没有请求时下面几行会 FAIL，以 bin/diag.sh vllm 的结论为准" % addr)
    all_quiet = {n for s in quiet.values() for n in s}
    for addr, names in c.instances.items():
        code, out, err = ssh(c, addr, "RAW_L=%s\n" % shlex.quote(c.log_host()) + CHAIN_NODE)
        r.keep("chain node %s" % addr, out or err)
        if code != 0:
            r.add("FAIL", "%s SSH 连不上（%s）" % (addr, err.strip()[:80]))
            continue
        kv = parse_kv_lines(out)
        now = int((kv.get("now") or ["0"])[0])
        seen = {}
        for v in kv.get("newest", []):
            parts = v.split("\t")
            if len(parts) >= 3:
                t = iso_epoch(parts[2])
                seen[parts[0]] = (now - t) if t else None
        lags = [(n, seen.get(p)) for n, p in names if n not in quiet[addr]]
        if not lags:
            continue
        missing = [n for n, l in lags if l is None]
        worst = max([l for _, l in lags if l is not None] or [0])
        lvl = "FAIL" if missing else K.age_level(worst, th["node"])
        hint = "（vllm 模式：vLLM 重启以后还没有请求时过滤器一行也不写，先看 bin/diag.sh vllm 的结论）" if lvl == "FAIL" and c.mode == "vllm" and states.get(addr) is None else ""
        r.add(lvl, "%s 节点上各实例最新一行落后：最多 %.0f 秒%s%s%s" % (
            addr, worst, "；没有文件：%s" % " ".join(missing) if missing else "",
            "（不含这次启动以后还没有记录的 %s）" % " ".join(sorted(quiet[addr])) if quiet[addr] else "", hint))
    # the puller
    code, st, err = http_json("http://%s:%s/status" % (c.get("MONITOR_HOST"), c.get("PULLER_METRICS_PORT")), timeout=10)
    puller_ok = False
    if code != 200 or not isinstance(st, list):
        r.add("FAIL", "拉取程序没有运行（%s）→ bin/puller.sh start（机器重启后不会自己起来）" % (err or code))
    else:
        bad = [m for m in st if m.get("age") is None or m["age"] > 120]
        puller_ok = not bad
        worst = max([m["age"] for m in st if m.get("age") is not None] or [0])
        if bad:
            r.add("FAIL", "拉取：这些机器超过 2 分钟没有拉取成功：%s" % "；".join("%s（%s）" % (m["addr"], (m.get("error") or "")[:60]) for m in bad))
        else:
            r.add("OK" if worst <= 30 else "WARN", "拉取：%d 台，最久的 %.0f 秒前成功" % (len(st), worst))
    # the mirror on this machine
    pull = c.get("PULL_DIR")
    if pull and os.path.isdir(pull):
        for addr, names in c.instances.items():
            if names and all(n in quiet[addr] for n, _ in names):
                r.add("INFO", "镜像 %s：这台的实例这次启动以后都还没有记录，没有判镜像的新旧" % addr)
                continue
            d = os.path.join(pull, re.sub(r"[^A-Za-z0-9._-]", "_", addr))
            newest = None
            for root, _, fs in os.walk(d):
                for f in fs:
                    if f.endswith(".log"):
                        try:
                            m = os.path.getmtime(os.path.join(root, f))
                        except OSError:
                            continue
                        newest = m if newest is None or m > newest else newest
            if newest is None:
                r.add("FAIL", "镜像目录 %s 里没有这台的日志" % d)
            else:
                age = time.time() - newest
                r.add(K.age_level(age, th["mirror"]), "镜像 %s：最新文件 %.0f 秒前改过" % (addr, age))
    else:
        r.add("WARN", "镜像目录 %s 不存在或读不了" % pull)
    # ClickHouse
    rows, err = ch(c, "SELECT node, instance, dateDiff('second', max(ts), now64(3)) AS wall, dateDiff('second', maxIf(ts, part = ''), now64(3)) AS event "
                      "FROM %s WHERE ts > now() - INTERVAL 60 MINUTE GROUP BY node, instance ORDER BY node, instance" % tbl)
    if rows is None:
        r.add("FAIL", "ClickHouse 查不了（%s）→ docker ps 看 %s" % (err, c.get("CLICKHOUSE_CONTAINER")))
    else:
        inst_seen = {x["instance"]: x for x in rows}
        want = [n for names in c.instances.values() for n, _ in names if n not in all_quiet]
        miss = [n for n in want if n not in inst_seen]
        judged = [int(x["wall"]) for x in rows if x["instance"] not in all_quiet]
        worst = max(judged or [0])
        idle = None
        if c.mode == "vllm" and want and puller_ok and (miss or worst > th["clickhouse"]) \
                and all(states.get(a) is not None for a in c.instances):
            # known issue 10: rows stop with the requests, self-check lines do not; judged by those of this version
            got, _ = ch(c, "SELECT instance, dateDiff('second', max(ts), now64(3)) AS a FROM vllm_pd.filter_selfcheck WHERE ts > now64(3) - "
                           "INTERVAL 1 DAY AND config_version = '%s' GROUP BY instance" % c.version.replace("'", ""))
            if got is not None:
                ages = {x["instance"]: int(x["a"]) for x in got}
                idle = K.judge_idle_chain({n: ages.get(n) for n in want}, th["clickhouse"], int(th["clickhouse"]))
        if idle is not None and idle.level == "WARN":
            r.add("WARN", "ClickHouse：最新一行最多落后 %d 秒%s；%s" % (
                worst, "，最近 60 分钟没有行：%s" % " ".join(miss) if miss else "", idle.text.split("；", 1)[1]))
        else:
            if miss:
                r.add("FAIL", "ClickHouse：这些实例最近 60 分钟没有行：%s" % " ".join(miss))
            if want:
                r.add(K.age_level(worst, th["clickhouse"]), "ClickHouse：%d 个实例，最新一行最多落后 %d 秒（%s）%s" % (
                    len([n for n in inst_seen if n not in all_quiet]), worst, tbl, "（不含这次启动以后还没有记录的实例）" if all_quiet else ""))
            if idle is not None:
                r.add(idle.level, idle.text)
    # collector buffer and parse failures
    buf, err = prom(c, "sum by (component_id) (vector_buffer_size_events)")
    if buf is None:
        r.add("WARN", "读不到采集器的缓冲积压（%s）" % err)
    else:
        total = sum(float(x["value"][1]) for x in buf)
        r.add("OK" if total < 10000 else "WARN", "采集器缓冲：%d 个输出共 %d 条积压%s" % (len(buf), total, "" if total < 10000 else " → ClickHouse 写得慢，看 monitor"))
    drop, err = prom(c, 'sum(increase(vector_component_sent_events_total{component_id="parse",output="dropped"}[10m]))')
    n_drop = int(float(drop[0]["value"][1])) if drop else 0
    code, out, err2 = run(["docker", "exec", c.get("CENTRAL_CONTAINER"), "sh", "-c", "cat /var/lib/vector/dead-letter-*.jsonl 2>/dev/null | wc -l"])
    dead = int(out.strip() or 0) if code == 0 else None
    if n_drop or dead:
        code, sample, _ = run(["docker", "exec", c.get("CENTRAL_CONTAINER"), "sh", "-c", "tail -n 1 $(ls -t /var/lib/vector/dead-letter-*.jsonl 2>/dev/null | head -n 1) 2>/dev/null"])
        r.add("FAIL", "解析失败：最近 10 分钟 %d 行；dead-letter %s 行；一行样子：%s" % (n_drop, dead, sample.strip()[:200]))
    else:
        r.add("OK", "解析失败：最近 10 分钟 0 行；dead-letter %s 行" % (dead if dead is not None else "读不到"))
    # filter counters, requests, file heads
    rows, err = ch(c, "SELECT instance, pid, argMax(errors, ts) AS e, argMax(config_errors, ts) AS ce, argMax(empty_key_dropped, ts) AS k "
                      "FROM vllm_pd.filter_selfcheck WHERE ts >= now64(3) - INTERVAL 5 MINUTE GROUP BY instance, pid")
    if rows is not None:
        r.add("INFO", empty_key_line(rows))
        bad = [x for x in rows if int(x["e"]) or int(x["ce"])]
        if not rows and c.mode == "vllm" and all_quiet and not any(s == "records" for st in states.values() for s in (st or {}).values()):
            r.add("WARN", "最近 5 分钟没有过滤器的自检行：各实例这次启动以后都还没有记录（见上面按进程判的几行），有请求以后再查")
        elif not rows and c.mode == "vllm" and young_starts(c):
            r.add("WARN", "最近 5 分钟没有过滤器的自检行，但 vLLM 启动不到 2 分钟（%s）：过滤器每 60 秒写一行自检 → 2 分钟后再查" % young_starts(c))
        elif not rows:
            r.add("FAIL", "最近 5 分钟没有过滤器的自检行（过滤器收到第一条记录以后每分钟一行，之后没有请求也写）→ 节点上没有进程在写，看 nodes / vllm")
        elif bad:
            r.add("FAIL", "过滤器出错：%s" % "；".join("%s pid %s 出错 %s 映射出错 %s" % (x["instance"], x["pid"], x["e"], x["ce"]) for x in bad[:6]))
        else:
            r.add("OK", "过滤器：%d 个进程的出错数都是 0" % len(rows))
    rows, err = ch(c, "SELECT uniqExact(request_id) AS n FROM %s WHERE ts > now() - INTERVAL 15 MINUTE AND proc = 'api' AND part = ''" % tbl)
    if rows is not None:
        n = int(rows[0]["n"]) if rows else 0
        r.add("INFO", "最近 15 分钟 %d 个请求" % n if n else "最近 15 分钟没有请求；自检行在写时链路是通的（vllm 模式下没有请求是正常的）")
    rows, err = ch(c, "SELECT instance, max(ts) AS last FROM vllm_pd.filter_file WHERE ts > now() - INTERVAL 7 DAY GROUP BY instance")
    if rows is not None:
        have = {x["instance"] for x in rows}
        want = [n for names in c.instances.values() for n, _ in names if n not in all_quiet]
        miss = [n for n in want if n not in have]
        if miss:
            r.add("WARN", "filter_file 里这些实例没有文件头：%s（清过 filter_file、进程没重启时会这样）" % " ".join(miss))


def empty_key_line(rows):
    """The filters' empty-key counter: records dropped because the key segments.json reads (request_id, request_ids)
    is empty, per instance, as each running process counted since its start."""
    per = {}
    for x in rows:
        per[x["instance"]] = per.get(x["instance"], 0) + int(x.get("k") or 0)
    if not any(per.values()):
        return "过滤器丢掉的空键记录：0 条（各进程从启动累计）"
    return "过滤器丢掉的空键记录（取键字段为空的记录，各进程从启动累计）：%s；首次候选那一步的步骤记录不带请求号，计在这里" % (
        "，".join("%s %d 条" % kv for kv in sorted(per.items()) if kv[1]))


# ==================== monitor ====================
def df_of(path):
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize
    except OSError:
        return None, None


def mount_sources(container):
    code, out, err = run(["docker", "inspect", "--format", "{{json .Mounts}}", container])
    try:
        return [m.get("Source") for m in json.loads(out or "[]") if m.get("Source")]
    except ValueError:
        return []


def cmd_monitor(c, r, args):
    r.section("monitor（监控机）")
    rates, err = write_rates(c)
    pull = c.get("PULL_DIR")
    free, total = df_of(pull) if pull else (None, None)
    if free is None:
        r.add("WARN", "镜像目录 %s 读不了" % pull)
    else:
        # the same estimate and line as the upgrade-safety check (design 2.7): the sum of the nodes' write rates
        need = K.estimate_log_bytes(sum(rates.values()), c.keep_hours) if rates else None
        r.add(*prefixed("", K.judge_log_volume(need, free, "镜像盘 %s" % pull, "%g" % c.keep_hours)))
    for what, cont in (("ClickHouse 数据", c.get("CLICKHOUSE_CONTAINER")), ("Prometheus 数据", c.get("PROMETHEUS_CONTAINER"))):
        srcs = mount_sources(cont) if cont else []
        shown = False
        for s in srcs:
            if s.endswith((".xml", ".yml", ".yaml")):
                continue
            free, total = df_of(s)
            if free is None or not total:
                continue
            used = 100 - free * 100.0 / total
            r.add("OK" if used < 80 else ("WARN" if used < 90 else "FAIL"), "%s（%s）：已用 %.0f%%，空闲 %s" % (what, s, used, human(free)))
            shown = True
            break
        if not shown:
            r.add("INFO", "%s：没有读到它的数据目录（容器 %s）" % (what, cont))
    for cont in (c.get("CLICKHOUSE_CONTAINER"), c.get("CENTRAL_CONTAINER"), c.get("PROMETHEUS_CONTAINER"), c.get("GRAFANA_CONTAINER")):
        if not cont:
            continue
        code, out, err = run(["docker", "inspect", "--format", "{{.State.Status}}|{{.State.StartedAt}}|{{.RestartCount}}|{{.State.OOMKilled}}", cont])
        if code != 0:
            r.add("FAIL", "容器 %s 不存在" % cont)
            continue
        st, started, restarts, oom = (out.strip().split("|") + ["", "", "", ""])[:4]
        if st != "running":
            r.add("FAIL", "容器 %s %s" % (cont, st))
        elif oom == "true":
            r.add("FAIL", "容器 %s 被内存耗尽杀过（OOMKilled）" % cont)
        else:
            t = iso_epoch(started[:19] + "+00:00") or 0
            r.add("OK" if time.time() - t > 86400 or int(restarts or 0) == 0 else "WARN",
                  "容器 %s 在运行，启动于 %s UTC，重启 %s 次" % (cont, started[:19].replace("T", " "), restarts))
    code, st, err = http_json("http://%s:%s/status" % (c.get("MONITOR_HOST"), c.get("PULLER_METRICS_PORT")), timeout=10)
    r.add("OK" if code == 200 else "FAIL", "拉取程序在运行" if code == 200 else "拉取程序没有运行 → bin/puller.sh start")
    code, res, err = http_json(c.get("PROMETHEUS_URL", "http://localhost:9090").rstrip("/") + "/api/v1/targets?state=active")
    if code != 200 or not isinstance(res, dict):
        r.add("FAIL", "Prometheus 访问不了（%s）" % (err or code))
    else:
        targets = res["data"]["activeTargets"]
        down = [t for t in targets if t.get("health") != "up"]
        slow = []
        for t in targets:
            to = t.get("scrapeTimeout", "10s")
            try:
                to_s = float(to.rstrip("ms")) / (1000 if to.endswith("ms") else 1)
            except ValueError:
                to_s = 10.0
            if t.get("health") == "up" and float(t.get("lastScrapeDuration", 0)) >= to_s / 2:
                slow.append("%s %s %.1f 秒" % (t["labels"].get("job"), t["labels"].get("instance"), float(t["lastScrapeDuration"])))
        jobs = sorted({t["labels"].get("job") for t in targets})
        r.add("OK" if not down else "WARN", "Prometheus：%d 个目标（%d 个 job），%d 个 down%s" % (
            len(targets), len(jobs), len(down), "：" + "；".join("%s %s（%s）" % (t["labels"].get("job"), t["labels"].get("instance"), (t.get("lastError") or "")[:50]) for t in down[:5]) if down else ""))
        if slow:
            r.add("WARN", "Prometheus 抓取耗时超过超时的一半：%s" % "；".join(slow[:5]))
    code, res, err = http_json(c.get("GRAFANA_URL", "http://localhost:3000").rstrip("/") + "/api/health")
    r.add("OK" if code == 200 else "FAIL", "Grafana %s" % ("正常" if code == 200 else "访问不了（%s）" % (err or code)))
    mem = 0
    for line in open("/proc/meminfo") if os.path.isfile("/proc/meminfo") else []:
        if line.startswith("MemAvailable"):
            mem = int(line.split()[1]) // 1024
    if mem and mem < 2048:
        r.add("WARN", "监控机可用内存只有 %d MiB" % mem)
    r.add("INFO", "面板二 %s，节点模式 %s，过滤器版本应为 %s" % (c.version_file, c.mode, c.version))


# ==================== history ====================
def cmd_history(c, r, args):
    try:
        t0 = time.mktime(time.strptime(args.start, "%Y-%m-%d %H:%M"))
        t1 = time.mktime(time.strptime(args.end, "%Y-%m-%d %H:%M"))
    except ValueError:
        raise SystemExit("错误：时间写成 \"2026-11-13 10:30\"（本机时间）")
    step = 60 if t1 - t0 <= 6 * 3600 else 300
    r.section("history %s – %s（每 %d 秒一个点）" % (args.start, args.end, step))
    hm = lambda t: time.strftime("%m-%d %H:%M", time.localtime(t))
    pu, _ = prom_range(c, 'min(up{job=~"%s"})' % re.escape(c.get("PULLER_JOB", "vllm-timing-puller")), t0, t1, step)
    pdown = [float(t) for s in (pu or []) for t, v in s["values"] if float(v) < 1]
    puller_down = lambda a, b: any(a <= t <= b for t in pdown)
    res, err = prom_range(c, "sum by (machine) (rate(vllm_puller_transferred_bytes_total[5m]))", t0, t1, step)
    if res is None:
        r.add("FAIL", "Prometheus 查不了（%s）" % err)
        return
    if not res:
        r.add("INFO", "这段时间没有拉取程序的数据（超出 Prometheus 的保留期，或拉取程序没在运行）")
    stopped = "节点停止写日志" + ("（vllm 模式下，vLLM 重启以后没有请求时也是这样）" if c.mode == "vllm" else "")
    seen = set()
    for s in res:
        machine = s["metric"].get("machine", "?")
        seen.add(machine)
        vals = [(float(t), float(v)) for t, v in s["values"]]
        r.keep("bytes/s %s" % machine, "\n".join("%s %.0f" % (hm(t), v) for t, v in vals))
        zero_start, long_gap, short = None, False, []
        for t, v in vals + [(t1 + step, 1.0)]:
            if v == 0 and zero_start is None:
                zero_start = t
            elif v > 0 and zero_start is not None:
                if t - zero_start >= 600:
                    long_gap = True
                    why = "拉取程序那段时间没在运行（up 为 0），节点可能照常在写" if puller_down(zero_start, t) else stopped
                    r.add("WARN", "%s 拉到的字节数在 %s 降到 0，%s；%s" % (machine, hm(zero_start), "到 %s 恢复" % hm(t) if t <= t1 else "这段时间里没有恢复", why))
                else:
                    short.append((zero_start, t))
                zero_start = None
        if vals and not any(v == 0 for _, v in vals):
            r.add("OK", "%s 整段都在写（平均 %s/秒）" % (machine, human(sum(v for _, v in vals) / len(vals))))
        elif short and not long_gap:
            a, b = short[-1]
            r.add("INFO", "%s 有 %d 段拉到的字节数为 0，都短于 10 分钟（最后一段 %s 起%s）" % (
                machine, len(short), hm(a), "，到 %s 恢复" % hm(b) if b <= t1 else "，到这段时间结束还是 0"))
        elif not vals:
            r.add("WARN", "%s 这段时间没有拉取数据" % machine)
    for addr in c.instances:
        if res and addr not in seen:
            r.add("WARN", "%s 这段时间没有拉取数据（Prometheus 里没有这台的字节数）" % addr)
    for q, what in (("min by (job) (up)", "up"), ("max by (job) (scrape_duration_seconds)", "dur")):
        res, err = prom_range(c, q, t0, t1, step)
        for s in res or []:
            job = s["metric"].get("job", "?")
            vals = [(float(t), float(v)) for t, v in s["values"]]
            r.keep("%s %s" % (what, job), "\n".join("%s %g" % (hm(t), v) for t, v in vals))
            if what == "up":
                downs = [t for t, v in vals if v < 1]
                if downs:
                    r.add("WARN", "%s %s – %s 之间有 %d 个点 up 为 0" % (job, hm(downs[0]), hm(downs[-1]), len(downs)))
                elif vals:
                    r.add("OK", "%s 整段 up" % job)
            else:
                top = max(vals, key=lambda x: x[1]) if vals else None
                if top and top[1] >= 5:
                    r.add("WARN", "%s 抓取耗时最高 %.1f 秒（%s）" % (job, top[1], hm(top[0])))
    res, err = prom_range(c, 'sum(increase(vector_component_sent_events_total{component_id="parse",output="dropped"}[5m]))', t0, t1, step)
    bad = [(float(t), float(v)) for s in (res or []) for t, v in s["values"] if float(v) > 0]
    if bad:
        r.add("WARN", "采集器解析失败：%s 起，共约 %d 行" % (hm(bad[0][0]), sum(v for _, v in bad)))


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


# Record kinds pack two reads: the seven every log of the patch has, and the two of patch 02 that v3 reads, each written
# on one side only (v3 plan 1.1).
NEED_KINDS = ("api_summary[http]", "api_summary[engine_submission]", "api_summary[request]", "core_summary[request]",
              "core_summary[execution]", "worker_summary[execute]", "worker_summary[sample]")
NEW_KINDS = (("pool_task[task]", "缓存池的保存、加载任务，只在 P 侧、用了缓存池时有"),
             ("pd_transfer_summary[request_generation]", "D 从 P 读取 KV 的请求汇总，只在 D 侧有"))


def judge_kinds(prof):
    """[(level, text)]: the 9 record kinds pack two reads. The seven are FAIL when missing; the two new ones are not
    required in one file (each is on one side only), but a file with neither is WARN: it was not written by patches
    01 and 02, and the 8 segments v3 adds stay empty."""
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
    """The known sample whose record kinds are closest to prof's (shared kinds over all kinds of the two); a tie goes
    to the later file name. bases: known_profiles()."""
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
    code, out, err = run([sys.executable, "-I", tool, args.file], timeout=3600)
    r.keep("check_pd_diag_format.py", out + err)
    last = (out.strip().splitlines() or [""])[-1]
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
        r.add("WARN", "包里没有已知样本的统计（samples/profiles/）")
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
    model = args.model or c.inv.get("model", "")
    tag = "diag-%s" % time.strftime("%Y%m%d%H%M%S")
    tbl = table(c)
    rows, err = ch(c, "SELECT toString(now64(3)) AS t")
    if rows is None:
        r.add("FAIL", "ClickHouse 查不了（%s）" % err)
        return
    t0 = rows[0]["t"]
    ok = 0
    for i in range(1, n + 1):
        rid = "%s-%d" % (tag, i)
        # the id both ways: header X-Request-Id, and request_id in the body (the body is usually passed on as it is)
        body = json.dumps({"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 8, "request_id": rid}).encode()
        code, res, err = http_json("http://%s/v1/chat/completions" % args.proxy, timeout=120, data=body,
                                   headers={"Content-Type": "application/json", "X-Request-Id": rid})
        if code == 200:
            ok += 1
        else:
            r.add("FAIL", "第 %d 个请求 HTTP %s（%s）" % (i, code, (err or "")[:100]))
    if ok:
        r.add("OK", "%d 个请求返回 200，请求键 %s-1 … %s-%d（头部和请求体都带）" % (ok, tag, tag, n))
    sides = lambda xs: {s for x in xs for s in x["roles"]}
    deadline, keys, new = time.time() + args.wait, [], {}
    while True:
        keys, err = ch(c, "SELECT request_id, groupUniqArray(role) AS roles FROM %s WHERE request_id LIKE '%%%s-%%' AND ts >= toDateTime64('%s', 3) GROUP BY request_id" % (tbl, tag, t0))
        cnt, err2 = ch(c, "SELECT role, uniqExact(request_id) AS n FROM %s WHERE ts >= toDateTime64('%s', 3) AND proc = 'api' AND part = '' GROUP BY role" % (tbl, t0))
        new = {x["role"]: int(x["n"]) for x in cnt or []}
        both = [x for x in keys or [] if set(x["roles"]) >= {"prefill", "decode"}]
        if len(both) == n or time.time() >= deadline:
            break
        time.sleep(15)
    keys = keys or []
    both = [x for x in keys if set(x["roles"]) >= {"prefill", "decode"}]
    np_, nd = new.get("prefill", 0), new.get("decode", 0)
    r.keep("pair", "diag 请求键：%s\n发请求以后两侧新增的请求：P %d，D %d" % (json.dumps(keys, ensure_ascii=False), np_, nd))
    if len(both) == n:
        r.add("OK", "%d 个请求两侧都有：P、D 能配上" % n)
    elif keys and sides(keys) == {"prefill"}:
        r.add("FAIL", "诊断请求只在 P 侧有：proxy 没有把 id 传给 D → 要现场改 proxy，给同一个请求的 P、D 发相同的 X-Request-Id（或请求体里相同的 request_id）")
    elif keys and sides(keys) == {"decode"}:
        r.add("FAIL", "诊断请求只在 D 侧有：proxy 没有把 id 传给 P → 同上")
    elif not keys and np_ >= n and nd >= n:
        r.add("FAIL", "两侧都没有诊断请求的键，但发请求以后 P 新增 %d 个、D 新增 %d 个请求：proxy 对两侧都没有传 id（头部和请求体都没有）→ 要现场改 proxy" % (np_, nd))
    elif not keys and np_ >= n and nd < n:
        r.add("FAIL", "发请求以后只有 P 侧新增请求（P %d、D %d）：请求没有到 D，或 D 侧过滤器没生效 → bin/diag.sh vllm 看 D 那台" % (np_, nd))
    elif not keys:
        r.add("FAIL", "%d 秒内两侧都没有新增请求：请求没有到 vLLM，或过滤器没生效 → 先跑 bin/diag.sh chain、bin/diag.sh vllm" % args.wait)
    else:
        r.add("WARN", "只有 %d / %d 个请求两侧都有（其余：%s）" % (len(both), n, json.dumps([x for x in keys if x not in both], ensure_ascii=False)[:200]))
    r.add("INFO", "这几个请求会出现在看板上，请求键以 diag- 开头，是诊断发的，可以忽略")


# ==================== main ====================
USAGE = """在集群目录里执行。除 pair 以外只读；报告写在 ~/vllm-pd-diag-reports/<集群>/。说明见 docs/诊断工具说明.zh-CN.md。

  bin/diag.sh all                         nodes、vllm（vllm 模式）、chain、monitor 依次执行，屏幕上最多 40 行
  bin/diag.sh nodes | vllm | chain | monitor
  bin/diag.sh history "<起>" "<止>"        监控机本地时间，比如 "2026-11-13 10:30"
  bin/diag.sh log <日志文件> [--save-profile]
  bin/diag.sh pair <proxy 地址:端口> [--model 模型名] [--wait 秒]     经 proxy 发 3 个短请求

容器名、标准输出文件取 deploy.env 的 NODE_VLLM_CONTAINERS、NODE_VLLM_STDOUT_FILES，没填时在节点上自己找；
命令行的 --containers 地址=容器名[;容器名],…、--stdout-file 地址=文件,… 优先。
"""


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bin/diag.sh", description=USAGE, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--containers", default=None, help="地址=容器名[;容器名],…（优先于 deploy.env 的 NODE_VLLM_CONTAINERS）")
    ap.add_argument("--stdout-file", default=None, help="地址=文件,…（优先于 deploy.env 的 NODE_VLLM_STDOUT_FILES）")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("all", "nodes", "vllm", "chain", "monitor"):
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
    pr.add_argument("--wait", type=int, default=180, help="最多等多少秒（每 15 秒查一次）")
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    c = Cluster(PACK)
    r = Report(args.cmd)
    print("bin/diag.sh %s：集群 %s（%s，%s 模式），%s" % (args.cmd, c.name, c.dir, c.mode,
                                               "只读" if args.cmd != "pair" else "会发 %d 个请求" % args.count), flush=True)
    steps = {"nodes": cmd_nodes, "vllm": cmd_vllm, "chain": cmd_chain, "monitor": cmd_monitor,
             "history": cmd_history, "log": cmd_log, "pair": cmd_pair}
    if args.cmd == "all":
        for name in ("nodes", "vllm", "chain", "monitor"):
            if name == "vllm" and c.mode != "vllm" and not (args.containers or c.get("NODE_VLLM_CONTAINERS")):
                continue
            steps[name](c, r, args)
    else:
        steps[args.cmd](c, r, args)
    lines = r.lines
    if args.cmd == "all":
        # at most 40 lines: section titles, FAIL and WARN first, then OK and INFO while there is room
        keep = [l for l in lines if l[0] in ("", "FAIL", "WARN")]
        room = 38 - len(keep)
        rest = [l for l in lines if l[0] in ("OK", "INFO")][:max(room, 0)]
        lines = [l for l in lines if l in keep or l in rest]
    for lv, t in lines:
        print(fmt(lv, t))
    path = r.write(os.path.join(REPORT_ROOT, c.name), " ".join(sys.argv[1:] if argv is None else argv))
    cnt = r.counts()
    print("报告：%s" % path)
    res = "FAIL（%d 项）" % cnt["FAIL"] if cnt["FAIL"] else ("WARN（%d 项）" % cnt["WARN"] if cnt["WARN"] else "OK")
    print("RESULT: %s" % res)
    return 1 if cnt["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
