#!/usr/bin/env python3
"""Mirrors the log folder of every machine to this machine with rsync over SSH. The collector (Vector, file source) reads the mirror.

Offsets, new files and reading from where it stopped are Vector's job; copying is rsync's. This is only the small loop around them: a thread per
machine so that one machine cannot hold up another, a timeout on each run, no second run while one is going, and the time of the last success.
Settings come from the environment (bin/lib.sh loads deploy.env).
v3.1-extra adds two optional sources, both off by default:
  PROXY_COLLECT=1     the proxy's log folder PROXY_LOG_DIR on PROXY_MACHINE, mirrored the same way into PULL_DIR/_proxy/<machine>;
  MOONCAKE_LOG_COLLECT=1  Mooncake's timing lines from MOONCAKE_LOG_FILES, fetched and paired with the pool's save records
                      (scripts/native_put.py) into PULL_DIR/_native/<machine>/<instance>.
"""
import atexit
import http.server
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from inventory_tool import load as load_inventory, node_machines  # noqa: E402
from names import Names, NamesError  # noqa: E402
import native_put  # noqa: E402

ENV = os.environ
INTERVAL = float(ENV.get("PULL_INTERVAL_S") or 10)
LIMIT = threading.Semaphore(int(ENV.get("PULL_CONCURRENCY") or 16))
TIMEOUT = float(ENV.get("PULL_TIMEOUT_S") or 300)
PULL_DIR = pathlib.Path(ENV["PULL_DIR"])
STATE = pathlib.Path(ENV["PULLER_STATE_DIR"])
# Named by cluster, so that removing one cluster closes only its own shared connections (scripts/uninstall_old.py).
try:
    SOCKETS = tempfile.mkdtemp(prefix=Names(ENV).get("SSH_SOCKET_PREFIX"))
except NamesError as e:
    raise SystemExit("错误：%s" % e)
atexit.register(shutil.rmtree, SOCKETS, True)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
NAMES_LOCK = threading.Lock()
NAMES_FILE = STATE / "names.json"


def ssh_options():
    opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2",
            "-o", "ControlMaster=auto", "-o", "ControlPersist=120", "-o", "ControlPath=%s/%%C" % SOCKETS, "-o", "StrictHostKeyChecking=accept-new", "-o", "LogLevel=ERROR"]
    opts += shlex.split(ENV.get("SSH_OPTS") or "")
    if ENV.get("SSH_USER"):
        opts += ["-l", ENV["SSH_USER"]]
    return opts


def run(cmd, timeout):
    """stderr goes to a file: the connection ssh leaves behind for reuse keeps it open, and a pipe would never end."""
    with tempfile.TemporaryFile() as err:
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "", "超时（%d 秒）" % timeout
        err.seek(0)
        return p.returncode, p.stdout.decode("utf-8", "replace"), err.read().decode("utf-8", "replace").strip()


def folder_name(addr):
    """The folder under PULL_DIR, which is also the node column: the machine's address as in inventory.json. The collector
    looks an instance folder named by port up as <address>:<port>. (Earlier versions used the host name; names.json keeps those.)"""
    if os.environ.get("PULL_FOLDER_BY_HOSTNAME") != "1":
        return re.sub(r"[^A-Za-z0-9._-]", "_", addr)
    with NAMES_LOCK:
        names = json.loads(NAMES_FILE.read_text()) if NAMES_FILE.exists() else {}
        if addr in names:
            return names[addr]
    code, out, _ = run(["ssh"] + ssh_options() + [addr, "hostname"], 30)
    if code != 0:
        return ""
    with NAMES_LOCK:
        names = json.loads(NAMES_FILE.read_text()) if NAMES_FILE.exists() else {}
        name = re.sub(r"[^A-Za-z0-9._-]", "_", out.strip()) or addr
        if name in names.values():
            name = addr
        names[addr] = name
        tmp = NAMES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(names))
        tmp.replace(NAMES_FILE)
    return name


class Machine(threading.Thread):
    def __init__(self, addr, sources, base=None, kind="node"):
        super().__init__(daemon=True)
        self.addr, self.sources = addr, sources
        self.base, self.kind = base or PULL_DIR, kind
        self.name_ = ""
        self.state_file = STATE / (("proxy-" if kind == "proxy" else "") + addr.replace(":", "_") + ".json")
        self.last_ok = json.loads(self.state_file.read_text()).get("last_ok", 0.0) if self.state_file.exists() else 0.0
        self.last_error = ""
        self.bytes = self.errors = 0

    def rsync(self, source, target):
        cmd = ["rsync", "-rt", "--delete", "--timeout=30", "--stats"] + shlex.split(ENV.get("PULL_RSYNC_FLAGS") or "--append")
        for pattern in (ENV.get("LOG_INCLUDE") or "*.log").split(","):
            cmd += ["--include=*/", "--include=" + pattern.strip()]
        cmd += ["--exclude=*", "--prune-empty-dirs", "-e", " ".join(["ssh"] + [shlex.quote(o) for o in ssh_options()])]
        if ENV.get("RSYNC_PATH"):
            cmd += ["--rsync-path=" + ENV["RSYNC_PATH"]]
        cmd += ["%s:%s/" % (self.addr, source.rstrip("/")), str(target) + "/"]
        return run(cmd, TIMEOUT)

    def pull(self):
        if not self.name_:
            self.name_ = folder_name(self.addr)
            if not self.name_:
                raise RuntimeError("SSH 连不上")
        multi = len(self.sources) > 1
        for i, source in enumerate(self.sources):
            target = self.base / self.name_ / ("d%d" % i if multi else "")
            target.mkdir(parents=True, exist_ok=True)
            code, out, err = self.rsync(source, target)
            if code == 23 and "No such file" in err and ("change_dir" in err or "link_stat" in err):
                # The folder does not exist on the machine (the mock has not started, or was cleaned). The mirror follows it.
                for child in target.iterdir():
                    shutil.rmtree(child) if child.is_dir() else child.unlink()
                continue
            if code not in (0, 24):   # 24: a file disappeared during the run, which rotation does; the next run sorts it out
                raise RuntimeError("rsync 退出码 %d：%s" % (code, (err.splitlines() or [""])[0][:160]))
            m = re.search(r"Literal data: ([\d,.]+)", out)
            if m:
                self.bytes += int(re.sub(r"[,.]", "", m.group(1)))
        self.last_ok = time.time()
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"last_ok": self.last_ok}))
        tmp.replace(self.state_file)

    def run(self):
        while True:
            started = time.time()
            with LIMIT:
                try:
                    self.pull()
                    self.last_error = ""
                except Exception as e:  # noqa: BLE001
                    self.errors += 1
                    self.last_error = str(e)[:200]
            time.sleep(max(1.0, INTERVAL - (time.time() - started)))


class Handler(http.server.BaseHTTPRequestHandler):
    machines = []
    native = None
    native_ok = [0.0]

    def log_message(self, *args):
        pass

    def do_GET(self):
        now = time.time()
        if self.path.startswith("/status"):
            rows = [{"addr": m.addr, "node": m.name_, "kind": m.kind, "last_ok": m.last_ok, "age": round(now - m.last_ok, 1) if m.last_ok else None,
                     "bytes": m.bytes, "error": m.last_error} for m in self.machines]
            if self.native is not None:
                n = self.native.status()
                rows.append({"addr": "mooncake", "node": "_native", "kind": "mooncake", "last_ok": self.native_ok[0],
                             "age": round(now - self.native_ok[0], 1) if self.native_ok[0] else None, "bytes": 0,
                             "error": "; ".join("%s: %s" % kv for kv in sorted(n["errors"].items()))[:200], "counts": n["counts"]})
            body = json.dumps(rows, ensure_ascii=False)
            kind = "application/json"
        else:
            out = []
            for metric, kind_, value in (("vllm_puller_last_success_seconds", "gauge", lambda m: m.last_ok), ("vllm_puller_transferred_bytes_total", "counter", lambda m: m.bytes),
                                         ("vllm_puller_errors_total", "counter", lambda m: m.errors)):
                out.append("# TYPE %s %s" % (metric, kind_))
                for m in self.machines:
                    out.append('%s{machine="%s",node="%s"} %s' % (metric, m.addr, m.name_, value(m)))
            if self.native is not None:
                for k, v in sorted(self.native.status()["counts"].items()):
                    out.append("# TYPE vllm_puller_mooncake_%s_total counter" % k)
                    out.append("vllm_puller_mooncake_%s_total %s" % (k, v))
            body = "\n".join(out) + "\n"
            kind = "text/plain; version=0.0.4"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.end_headers()
        self.wfile.write(body.encode())


def native_loop(nat, ok):
    """Mooncake's timing lines: fetch from every source (at most PULL_CONCURRENCY machines at a time), then pair them."""
    last_clean = 0.0
    while True:
        started = time.time()
        threads = []
        for addr in nat.sources:
            def one(a=addr):
                with LIMIT:
                    try:
                        nat.fetch(a)
                    except Exception as e:  # noqa: BLE001
                        nat.errors[a] = str(e)[:200]
            t = threading.Thread(target=one, daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(TIMEOUT)
        try:
            nat.run_once()
            ok[0] = time.time()
            if time.time() - last_clean > 600:
                nat.clean()
                last_clean = time.time()
        except Exception as e:  # noqa: BLE001
            nat.errors["associate"] = str(e)[:200]
        time.sleep(max(1.0, INTERVAL - (time.time() - started)))


def main():
    inv = load_inventory(ENV["INVENTORY"])
    STATE.mkdir(parents=True, exist_ok=True)
    PULL_DIR.mkdir(parents=True, exist_ok=True)
    sources = [s.strip() for s in (ENV.get("LOG_DIRS") or "").split(",") if s.strip()]
    if not sources:
        raw = ENV.get("NODE_LOG_DIR_RAW") or "$HOME/vllm-pd-diag/logs"
        sources = ["~/" + raw[6:] if raw.startswith("$HOME/") else raw]
    ms = [Machine(a, sources) for a in node_machines(inv)]
    if ENV.get("PROXY_COLLECT") == "1":
        if not ENV.get("PROXY_MACHINE") or not ENV.get("PROXY_LOG_DIR"):
            raise SystemExit("错误：PROXY_COLLECT=1 时要填 PROXY_MACHINE 和 PROXY_LOG_DIR")
        raw = ENV["PROXY_LOG_DIR"]
        ms.append(Machine(ENV["PROXY_MACHINE"], ["~/" + raw[6:] if raw.startswith("$HOME/") else raw], PULL_DIR / "_proxy", "proxy"))
    Handler.machines = ms
    for m in ms:
        m.start()
    if ENV.get("MOONCAKE_LOG_COLLECT") == "1":
        try:
            nat = native_put.Puller(ENV, str(PULL_DIR), str(STATE), folder_name, ssh_options, run)
        except ValueError as e:
            raise SystemExit("错误：%s" % e)
        if not nat.sources:
            raise SystemExit("错误：MOONCAKE_LOG_COLLECT=1 时要填 MOONCAKE_LOG_FILES")
        Handler.native = nat
        threading.Thread(target=native_loop, args=(nat, Handler.native_ok), daemon=True).start()
    addr = ("0.0.0.0", int(ENV.get("PULLER_METRICS_PORT") or 9598))
    print("拉取程序已启动：%d 台机器，每 %g 秒一轮，读 %s，镜像到 %s%s%s" % (
        len(ms), INTERVAL, ",".join(sources), PULL_DIR, "；proxy %s:%s" % (ENV.get("PROXY_MACHINE"), ENV.get("PROXY_LOG_DIR")) if ENV.get("PROXY_COLLECT") == "1" else "",
        "；Mooncake 计时行 %d 个来源" % sum(len(v) for v in Handler.native.sources.values()) if Handler.native else ""), flush=True)
    http.server.ThreadingHTTPServer(addr, Handler).serve_forever()


if __name__ == "__main__":
    main()
