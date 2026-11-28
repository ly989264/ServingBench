#!/usr/bin/env python3
"""Test-only: make_inventories.py against panel 1's own generator and inventory check, and its probe against local
stand-in API servers.

    python3 test_make_inventories.py <make_inventories.py> <panel-1 pack dir> <work dir>
Prints one line per check and a RESULT line. Uses the lab glm53-1 inventory shape (8 P, 64 D) as the defaults.
"""
import json, os, re, shutil, socket, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MK, PACK, WORK = sys.argv[1:4]
GEN = os.path.join(PACK, "prometheus", "gen_prometheus_yml.py")
NAMES = os.path.join(PACK, "scripts", "names.py")
PY = os.environ.get("PY", sys.executable).split()
fails = []


def run(args, env=None):
    p = subprocess.run(PY + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def check(name, ok, detail=""):
    print(("OK   " if ok else "FAIL ") + name + ("" if ok else "  " + detail[:600]))
    if not ok:
        fails.append(name)


shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(WORK)
glm = {"vllm_ascend_patched": False,
       "prefill": {"procs_per_instance": 16, "scheduler_store_clients": True, "api_servers": ["10.0.1.%d:9000" % i for i in range(1, 9)]},
       "decode": {"procs_per_instance": 1, "api_servers": ["10.0.2.%d:%d" % (m, 9000 + k) for m in range(1, 5) for k in range(16)]},
       "mooncake_master": {"address": "10.0.3.1:9003", "node": "mc-master"},
       "npu_exporter_port": 8082, "node_exporter_port": 9100, "scrape_interval": "15s", "scrape_timeout": "10s", "retention": "15d"}
GLM = os.path.join(WORK, "glm.json")
json.dump(glm, open(GLM, "w"))


def write(name, text):
    p = os.path.join(WORK, name)
    open(p, "w", encoding="utf-8").write(text)
    return p


def build(lst, out="out", only=None):
    return run([MK, "build", lst, "--defaults", GLM, "--out", os.path.join(WORK, out)] + (["--cluster", only] if only else []))


def gen_ok(inv_path, want_targets, want_last):
    rc, out, err = run([GEN, inv_path, "--list"])
    n = len([l for l in out.splitlines() if l.strip()])
    rc2, inst, err2 = run([GEN, inv_path, "--instances"])
    names = [l.split()[0] for l in inst.splitlines() if l.strip()]
    rc3, chk, err3 = run([NAMES, "inventory-check", inv_path], env=dict(os.environ, HOME=WORK))
    return rc == 0 and n == want_targets and rc2 == 0 and names[-1:] == [want_last] and rc3 == 0, "%s %d %s %s %s" % (rc, n, names[-1:], chk[-200:], err + err2 + err3)


TMPL = os.path.join(os.path.dirname(MK), "clusters.txt")
template = open(TMPL, encoding="utf-8").read()
NAMES7 = ["glm53-0", "glm53-2"] + ["dsv41-flash-%d" % i for i in range(1, 6)]


def fill(text, name, net):
    """The template with the block of `name` filled in (machine n of a side -> 10.<net>.1.n / 10.<net>.2.n)."""
    lines, cur, out = text.splitlines(), None, []
    for line in lines:
        w = line.split()
        if w and w[0] == "集群":
            cur = w[1]
        if cur == name:
            line = line.replace("<master 的 IP>:<端口>", "10.%d.0.100:9003" % net)
            line = re.sub(r"<P 机器 (\d+) 的 IP>", lambda m: "10.%d.1.%s" % (net, m.group(1)), line)
            line = re.sub(r"<D 机器 (\d+) 的 IP>", lambda m: "10.%d.2.%s" % (net, m.group(1)), line)
            line = line.replace("<P 实例的 TP>", "4").replace("<D 实例的 TP>", "2")
        out.append(line)
    return "\n".join(out) + "\n"


# 1 the template as shipped: every cluster still has placeholders
rc, out, err = build(TMPL, "out1")
check("template, all clusters: FAIL, 61 placeholder lines, nothing written",
      rc == 1 and err.count("占位符") == 61 and "RESULT: FAIL" in err and not os.path.exists(os.path.join(WORK, "out1")), err[-300:])
check("template names the seven clusters in order", re.findall(r"^集群 (\S+)", template, re.M) == NAMES7, str(re.findall(r"^集群 (\S+)", template, re.M)))

# 2 one dsv cluster filled, the others left with placeholders
t2 = fill(template, "dsv41-flash-2", 92)
lst2 = write("one-dsv.txt", t2)
rc, out, err = build(lst2, "out2", "dsv41-flash-2")
p2 = os.path.join(WORK, "out2", "dsv41-flash-2", "inventory.json")
inv = json.load(open(p2)) if rc == 0 else {}
check("--cluster dsv41-flash-2: OK although the other six have placeholders", rc == 0 and "RESULT: OK（集群 dsv41-flash-2，6 台机器；清单文件里其他集群的行没有读）" in out, out + err)
check("--cluster writes only that cluster", sorted(os.listdir(os.path.join(WORK, "out2"))) == ["dsv41-flash-2"], str(os.listdir(os.path.join(WORK, "out2"))))
check("dsv layout: P 7100,7101 per machine in order, D 7100-7107, TP 4 / 2",
      inv.get("prefill", {}).get("api_servers") == ["10.92.1.%d:%d" % (m, p) for m in range(1, 5) for p in (7100, 7101)]
      and inv["decode"]["api_servers"] == ["10.92.2.%d:%d" % (m, p) for m in (1, 2) for p in range(7100, 7108)]
      and inv["prefill"]["procs_per_instance"] == 4 and inv["decode"]["procs_per_instance"] == 2, json.dumps(inv)[:300])
ok, d = gen_ok(p2, 8 + 16 + 12 + 1, "D15") if rc == 0 else (False, err)
check("dsv cluster: 37 targets, D15 last, inventory-check OK", ok, d)
check("dsv summary line", "集群 dsv41-flash-2：P 4 台 8 个实例（P0–P7），D 2 台 16 个实例（D0–D15），master 10.92.0.100:9003；抓取目标 37 个" in out, out)

# 3 a second cluster later: the first one's inventory is not rewritten
t3 = fill(t2, "glm53-0", 90)
lst3 = write("two.txt", t3)
before = (os.stat(p2).st_mtime_ns, open(p2).read())
time.sleep(0.05)
rc, out, err = build(lst3, "out2", "glm53-0")
p3 = os.path.join(WORK, "out2", "glm53-0", "inventory.json")
check("--cluster glm53-0 next to an earlier cluster: OK", rc == 0 and "RESULT: OK（集群 glm53-0，12 台机器" in out, out + err)
check("the earlier cluster's inventory is left as it was", (os.stat(p2).st_mtime_ns, open(p2).read()) == before)
ok, d = gen_ok(p3, 8 + 64 + 24 + 1, "D63") if rc == 0 else (False, err)
check("glm53-0 with glm53-1's defaults: 97 targets, D63 last", ok, d)
check("glm summary line", "集群 glm53-0：P 8 台 8 个实例（P0–P7），D 4 台 64 个实例（D0–D63）" in out, out)

# 4 --cluster errors
rc, out, err = build(lst2, "out4", "dsv41-flash-9")
check("--cluster unknown name: FAIL, nothing written", rc == 1 and "没有集群 dsv41-flash-9" in err and not os.path.exists(os.path.join(WORK, "out4")), err)
rc, out, err = build(TMPL, "out4", "dsv41-flash-1")
check("--cluster with that cluster's placeholders left: FAIL, 7 lines, nothing written",
      rc == 1 and err.count("占位符") == 7 and not os.path.exists(os.path.join(WORK, "out4")), err)
bad_other = t2.replace("集群 dsv41-flash-3 master=<master 的 IP>:<端口>", "集群 dsv41-flash-3 master=oops")
rc, out, err = build(write("bad-other.txt", bad_other), "out5", "dsv41-flash-2")
check("--cluster: a wrong line in another cluster is not read", rc == 0, out + err)
shared = re.sub(r"(集群 dsv41-flash-4 master=)<master 的 IP>:<端口>\nP <P 机器 1 的 IP>", r"\g<1>10.94.0.100:9003\nP 10.92.1.3", t2)
rc, out, err = build(write("shared.txt", shared), "out6", "dsv41-flash-2")
check("--cluster: a machine also on another cluster's filled-in line: FAIL", rc == 1 and "10.92.1.3 也写在集群 dsv41-flash-4" in err, err)

# 5 per-machine overrides, mixed TP, wrong lists, defaults (whole-file mode)
lst = write("over.txt", """集群 c1 master=10.8.0.1:9003
P 10.8.1.1 端口=9000,9001 TP=8
P 10.8.1.2 实例数=2 TP=8
D 10.8.2.1 端口=9500 实例数=4
D 10.8.2.2 端口=9600-9601
""")
rc, out, err = build(lst, "out7")
inv = json.load(open(os.path.join(WORK, "out7", "c1", "inventory.json"))) if rc == 0 else {}
check("overrides: ports, 实例数, TP", rc == 0 and inv["prefill"]["api_servers"] == ["10.8.1.1:9000", "10.8.1.1:9001", "10.8.1.2:9000", "10.8.1.2:9001"]
      and inv["prefill"]["procs_per_instance"] == 8 and inv["decode"]["api_servers"] == ["10.8.2.1:9500", "10.8.2.1:9501", "10.8.2.1:9502", "10.8.2.1:9503", "10.8.2.2:9600", "10.8.2.2:9601"], out + err)
lst = write("mixed.txt", "集群 c2 master=10.7.0.1:9003\nP 10.7.1.1 TP=16\nP 10.7.1.2 端口=9000,9001 TP=8\nD 10.7.2.1\n")
rc, out, err = build(lst, "out8")
p8 = os.path.join(WORK, "out8", "c2", "inventory.json")
ok, d = gen_ok(p8, 3 + 16 + 6 + 1, "D15") if rc == 0 else (False, out + err)
check("mixed TP: api_servers form", ok and "api_servers 写法" in out, d)
cases = {
    "dup machine": ("集群 a master=1.1.1.1:1\nP 10.1.1.1\nD 10.1.1.2\n集群 b master=1.1.1.2:1\nP 10.1.1.1\nD 10.1.1.3\n", "已经写过"),
    "bad name": ("集群 dsv4.1-flash master=1.1.1.1:1\nP 10.1.1.1\nD 10.1.1.2\n", "不合规"),
    "no master": ("集群 a\nP 10.1.1.1\nD 10.1.1.2\n", "集群行写成"),
    "no D": ("集群 a master=1.1.1.1:1\nP 10.1.1.1\n", "没有 D 机器"),
    "bad key": ("集群 a master=1.1.1.1:1\nP 10.1.1.1 port=9000\nD 10.1.1.2\n", "看不懂"),
    "count mismatch": ("集群 a master=1.1.1.1:1\nP 10.1.1.1 端口=9000,9001 实例数=3\nD 10.1.1.2\n", "实例数=3"),
    "ip with port": ("集群 a master=1.1.1.1:1\nP 10.1.1.1:9000\nD 10.1.1.2\n", "不带端口"),
}
for name, (text, want) in cases.items():
    rc, out, err = build(write("bad.txt", text), "outbad")
    check("wrong list: " + name, rc == 1 and want in err and not os.path.exists(os.path.join(WORK, "outbad")), err)
rc, out, err = run([MK, "defaults", GLM])
check("defaults", rc == 0 and "P 机器：每台 1 个实例，端口 9000，TP 16" in out and "保留 15d" in out, out + err)

# 6 probe against local stand-in servers
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


BODY = {}


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = BODY.get(self.server.server_address[1], "").encode()
        self.send_response(200); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


def serve(port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


P = [free_port() for _ in range(3)]
D = [free_port() for _ in range(3)]
NPU, NODE, MM = free_port(), free_port(), free_port()
for p in P + D + [NPU, NODE, MM]:
    serve(p)
BODY[NPU] = BODY[NODE] = BODY[MM] = "up 1\n"


def vllm(engine):
    return '# TYPE vllm:num_requests_running gauge\nvllm:num_requests_running{engine="%s",model_name="m"} 0.0\n' % engine


def probe_inv(name, p_ports, d_ports):
    inv = {"vllm_ascend_patched": False,
           "prefill": {"procs_per_instance": 1, "api_servers": ["127.0.0.1:%d" % p for p in p_ports]},
           "decode": {"procs_per_instance": 1, "api_servers": ["127.0.0.1:%d" % p for p in d_ports]},
           "mooncake_master": {"address": "127.0.0.1:%d" % MM, "node": "mooncake-master"},
           "npu_exporter_port": NPU, "node_exporter_port": NODE}
    d = os.path.join(WORK, "probe", name)
    os.makedirs(d, exist_ok=True)
    json.dump(inv, open(os.path.join(d, "inventory.json"), "w"))
    return os.path.join(d, "inventory.json")


def set_engines(p_eng, d_eng):
    for port, e in zip(P, p_eng):
        BODY[port] = vllm(e)
    for port, e in zip(D, d_eng):
        BODY[port] = vllm(e)


inv_path = probe_inv("ok", P, D)
set_engines("012", "012")
rc, out, err = run([MK, "probe", inv_path])
check("probe: ranks in order -> OK", rc == 0 and "vLLM API server 6/6" in out and "RESULT: OK（全部目标可访问，engine 标签与实例号一致）" in out, out + err)
set_engines("000", "012")
rc, out, err = run([MK, "probe", inv_path])
check("probe: every P reports engine 0 -> note for P only, RESULT OK, exit 0",
      rc == 0 and "提示：P 侧 3 个 API server 的 engine 标签都是 0" in out and "D 侧" not in out and "RESULT: OK（全部目标可访问；engine 标签见上面的提示）" in out, out + err)
set_engines("000", "000")
rc, out, err = run([MK, "probe", inv_path])
check("probe: both sides all engine 0 -> two notes, exit 0", rc == 0 and out.count("提示：") == 2, out + err)
set_engines("012", "021")
rc, out, err = run([MK, "probe", inv_path])
check("probe: D order wrong -> FAIL naming D1 and D2", rc == 1 and "D1 127.0.0.1:%d engine 标签是 2，应为 1" % D[1] in out and "D2" in out and "RESULT: FAIL（2 个目标有问题" in out, out + err)
set_engines("011", "012")
rc, out, err = run([MK, "probe", inv_path])
check("probe: two of three P the same -> still checked, FAIL on P2", rc == 1 and "P2 127.0.0.1:%d engine 标签是 1，应为 2" % P[2] in out, out + err)
one = probe_inv("one", P[:1], D[:1])
set_engines("5", "0")
rc, out, err = run([MK, "probe", one])
check("probe: a single P server reporting engine 5 -> FAIL (one server is checked, no note)", rc == 1 and "engine 标签是 5，应为 0" in out and "提示" not in out, out + err)
BODY[D[2]] = "process_cpu_seconds_total 1\n"
set_engines("012", "01")
rc, out, err = run([MK, "probe", inv_path])
check("probe: a server without vllm lines -> FAIL 没有", rc == 1 and "engine 标签是 没有，应为 2" in out, out + err)
dead = probe_inv("dead", P, D[:2] + [free_port()])
set_engines("012", "012")
rc, out, err = run([MK, "probe", dead])
check("probe: a server not listening -> FAIL 不通", rc == 1 and "不通" in out and "vLLM API server 5/6" in out, out + err)

print("RESULT: %s（%d 项不通过）" % ("OK" if not fails else "FAIL", len(fails)))
sys.exit(1 if fails else 0)
