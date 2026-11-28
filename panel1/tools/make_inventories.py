#!/usr/bin/env python3
"""Turn one list file (clusters.txt) into one panel-1 inventory.json per cluster.

Usage:
    python3 make_inventories.py defaults <inventory.json of glm53-1>
    python3 make_inventories.py build <clusters.txt> --defaults <inventory.json of glm53-1> --out <folder> [--cluster <name>]
    python3 make_inventories.py probe <inventory.json> [<inventory.json> ...]

defaults  prints the values a machine line gets when it leaves them out: the ports of the first machine of each
          side of glm53-1, the TP of each side, and the exporter ports, scrape interval and retention. Read-only.
build     checks the list file first and writes nothing if any line is wrong. Then it writes
          <folder>/<cluster>/inventory.json and prints one summary line per cluster. Running it again overwrites.
          With --cluster only that cluster's lines are checked and only its inventory is written: the lines of the
          other clusters may still hold placeholders, and their inventories are left as they are. Its machines must
          not appear on another cluster's filled-in machine line.
probe     read-only, from the monitoring machine: GET /metrics of every scrape target an inventory gives (each vLLM
          API server, the npu-exporter and node_exporter of each machine, the Mooncake master), 3 s timeout each.
          For an API server it also reads the engine label of its vllm: lines, which is its data-parallel rank, and
          compares it with the instance number the inventory gives it (P<n> / D<n> must have engine="n"). When all
          API servers of one side report the same engine value (instances without data parallelism), the order cannot
          be checked from the label: a note is printed for that side and it is not counted as a failure.

The list file (see clusters.txt for a filled-in example of the format):
    集群 <cluster> master=<IP>:<port>              starts a cluster; the Mooncake master of this cluster
    P <IP> [端口=<ports>] [实例数=<n>] [TP=<n>]     a P machine of the cluster above
    D <IP> [端口=<ports>] [实例数=<n>] [TP=<n>]     a D machine of the cluster above
  <ports>: 9000, 9000-9015 or 9000,9002. One port is one instance (one API server).
  实例数 with a single port: that many consecutive ports from it. 实例数 alone: consecutive ports from the first
  default port. Left out: the ports and TP of glm53-1's side.
  Machines of a side are listed in DP rank order, and the ports of a machine too: the n-th API server of a side is
  instance P<n> / D<n>.

The inventory is the one panel 1's gen_prometheus_yml.py reads (pack README appendix A): vllm_ascend_patched false,
one Mooncake master, the exporter ports, scrape interval, scrape timeout and retention of glm53-1. When the machines
of one side have different TP, the inventory uses the api_servers form of README appendix B, one API server per
instance, so that each instance keeps its own TP.
Standard library only, Python 3.6 or later.
"""
import io
import json
import os
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

CLUSTER_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")  # the rule of panel 1's names.py
HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
ROLES = (("P", "prefill"), ("D", "decode"))
SHARED = (("npu_exporter_port", 8082), ("node_exporter_port", 9100),
          ("scrape_interval", "15s"), ("scrape_timeout", "10s"), ("retention", "15d"))
SCHEDULER_SLOT_OFFSET = 1000  # gen_prometheus_yml.py's default: global ranks must stay below it


class Bad(Exception):
    pass


def utf8_stdout():
    """Chinese output also when the locale is C (Python 3.6 then writes ASCII)."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
            setattr(sys, name, io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace", line_buffering=True))


def load_defaults(path):
    """Per side: (ports of its first machine, TP); plus the shared values. From glm53-1's inventory.json."""
    try:
        with open(path, encoding="utf-8") as f:
            inv = json.load(f)
    except (OSError, ValueError) as e:
        raise Bad("读不了 glm53-1 的清单 %s：%s" % (path, e))
    sides = {}
    for letter, role in ROLES:
        side = inv.get(role)
        if not isinstance(side, dict) or not side.get("api_servers") or not isinstance(side.get("procs_per_instance"), int):
            raise Bad("%s 里没有 %s.api_servers 和 %s.procs_per_instance（不是两个列表的写法）："
                      "清单文件里每台机器都要写 端口= 和 TP=" % (path, role, role))
        first = side["api_servers"][0].rpartition(":")[0]
        ports = [int(a.rpartition(":")[2]) for a in side["api_servers"] if a.rpartition(":")[0] == first]
        sides[letter] = (ports, side["procs_per_instance"])
    shared = {}
    for key, fallback in SHARED:
        shared[key] = inv.get(key, fallback)
    return sides, shared


def parse_ports(text, where):
    ports = []
    for part in text.split(","):
        part = part.strip()
        m = re.match(r"^(\d+)(?:-(\d+))?$", part)
        if not m:
            raise Bad("%s：端口写法不对：%r（写 9000、9000-9015 或 9000,9002）" % (where, text))
        a = int(m.group(1))
        b = int(m.group(2) or a)
        if b < a:
            raise Bad("%s：端口段 %s 的结尾比开头小" % (where, part))
        ports.extend(range(a, b + 1))
    for p in ports:
        if not 1 <= p <= 65535:
            raise Bad("%s：端口 %d 不在 1–65535 之间" % (where, p))
    if len(set(ports)) != len(ports):
        raise Bad("%s：同一台机器上有重复的端口" % where)
    return ports


def parse_int(text, what, where):
    if not re.match(r"^[1-9]\d*$", text):
        raise Bad("%s：%s 要写正整数，写的是 %r" % (where, what, text))
    return int(text)


def parse(path, sides, only=None):
    """The list file as [(cluster, master, [(letter, host, ports, tp, line)])]. Raises Bad listing every wrong line.
    With `only`, the lines of the other clusters are not checked; their filled-in machine addresses are only compared
    with the machines of `only`."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError) as e:
        raise Bad("读不了清单文件 %s：%s" % (path, e))
    clusters, errors, seen_host, seen_name = [], [], {}, {}
    current, others = None, []  # the cluster the line belongs to; (host, line, cluster) of the other clusters
    for no, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        where = "第 %d 行" % no
        words = line.split()
        if words[0] == "集群":
            current = words[1] if len(words) > 1 else None
        if only is not None and current != only:
            if words[0] in ("P", "D") and len(words) > 1 and HOST_RE.match(words[1]):
                others.append((words[1], no, current))
            continue
        try:
            if "<" in line or ">" in line:
                raise Bad("%s还有没填的占位符：%s" % (where, line))
            words = line.split()
            if words[0] == "集群":
                if len(words) != 3 or not words[2].startswith("master="):
                    raise Bad("%s：集群行写成 集群 <集群名> master=<IP>:<端口>" % where)
                name, master = words[1], words[2][len("master="):]
                if not CLUSTER_RE.match(name):
                    raise Bad("%s：集群名 %s 不合规：只用小写字母、数字和 -，以字母或数字开头和结尾，最长 40 个字符" % (where, name))
                if name in seen_name:
                    raise Bad("%s：集群 %s 在第 %d 行已经写过" % (where, name, seen_name[name]))
                seen_name[name] = no
                host, _, port = master.rpartition(":")
                if not HOST_RE.match(host or "-") or not port.isdigit() or not 1 <= int(port) <= 65535:
                    raise Bad("%s：master 写成 IP:端口，写的是 %r" % (where, master))
                clusters.append((name, master, []))
                continue
            if words[0] not in ("P", "D"):
                raise Bad("%s：行首要是 集群、P 或 D，写的是 %r" % (where, words[0]))
            if not clusters:
                raise Bad("%s：机器行前面要先有一行 集群 …" % where)
            letter, host = words[0], (words[1] if len(words) > 1 else "")
            if not HOST_RE.match(host or "-"):
                raise Bad("%s：机器地址写 IP（不带端口），写的是 %r" % (where, host))
            opts = {}
            for w in words[2:]:
                key, eq, val = w.partition("=")
                if not eq or key not in ("端口", "实例数", "TP") or not val:
                    raise Bad("%s：看不懂 %r（只能写 端口=、实例数=、TP=）" % (where, w))
                if key in opts:
                    raise Bad("%s：%s 写了两次" % (where, key))
                opts[key] = val
            default_ports, default_tp = sides[letter]
            n = parse_int(opts["实例数"], "实例数", where) if "实例数" in opts else None
            if "端口" in opts:
                ports = parse_ports(opts["端口"], where)
                if n is not None and len(ports) == 1 and n > 1:
                    ports = list(range(ports[0], ports[0] + n))
                elif n is not None and n != len(ports):
                    raise Bad("%s：实例数=%d，但 端口= 写了 %d 个端口" % (where, n, len(ports)))
            elif n is not None:
                ports = list(range(default_ports[0], default_ports[0] + n))
            else:
                ports = list(default_ports)
            if ports[-1] > 65535:
                raise Bad("%s：端口超过 65535" % where)
            tp = parse_int(opts["TP"], "TP", where) if "TP" in opts else default_tp
            if host in seen_host:
                raise Bad("%s：机器 %s 在第 %d 行已经写过：各套集群的机器互不相同，P 和 D 也不共用机器" % (where, host, seen_host[host]))
            seen_host[host] = no
            clusters[-1][2].append((letter, host, ports, tp, no))
        except Bad as e:
            errors.append(str(e))
    for name, _, machines in clusters:
        for letter, role in ROLES:
            if not any(m[0] == letter for m in machines):
                errors.append("集群 %s 没有 %s 机器" % (name, letter))
    for host, no, name in others:
        if host in seen_host:
            errors.append("第 %d 行：机器 %s 也写在集群 %s 的第 %d 行：各套集群的机器互不相同" % (seen_host[host], host, name, no))
    if only is not None and not clusters and not errors:
        errors.append("清单文件里没有集群 %s（集群行写成 集群 %s master=<IP>:<端口>）" % (only, only))
    if not clusters and not errors:
        errors.append("清单文件里没有集群行")
    if errors:
        raise Bad("\n".join(errors))
    return clusters


def inventory(machines, master, shared):
    """The inventory.json of one cluster."""
    inv = {"vllm_ascend_patched": False}
    tps = {letter: sorted({m[3] for m in machines if m[0] == letter}) for letter, _ in ROLES}
    if all(len(v) == 1 for v in tps.values()):
        for letter, role in ROLES:
            inv[role] = {"procs_per_instance": tps[letter][0],
                         "api_servers": ["%s:%d" % (m[1], p) for m in machines if m[0] == letter for p in m[2]]}
    else:
        servers = []
        for letter, role in ROLES:
            hosts, n = [], 0
            for m in machines:
                if m[0] != letter:
                    continue
                hosts.append(m[1])
                for p in m[2]:
                    first, last = n * m[3], n * m[3] + m[3] - 1
                    if last >= SCHEDULER_SLOT_OFFSET:
                        raise Bad("第 %d 行：%s 侧第 %d 个实例的全局 rank 到了 %d，超过 %d" % (m[4], letter, n, last, SCHEDULER_SLOT_OFFSET - 1))
                    servers.append({"name": "%s-API-%d" % (letter, n), "role": role, "address": "%s:%d" % (m[1], p),
                                    "procs_per_dp": m[3],
                                    "nodes": [{"node": "%s-node-%d" % (role[0], len(hosts) - 1), "host": m[1],
                                               "ranks": "%d-%d" % (first, last)}]})
                    n += 1
        inv["api_servers"] = servers
    inv["mooncake_master"] = {"address": master, "node": "mooncake-master"}
    inv.update(shared)
    return inv


def targets_of(inv):
    """(kind, instance or node name, address, expected engine) for every scrape target of an inventory."""
    servers, hosts = [], []
    if "api_servers" in inv:
        count = {"prefill": 0, "decode": 0}
        for s in inv["api_servers"]:
            letter = "P" if s["role"] == "prefill" else "D"
            servers.append((letter, count[s["role"]], s["address"]))
            count[s["role"]] += 1
            hosts.extend(nd["host"] for nd in s["nodes"])
    else:
        for letter, role in ROLES:
            for n, a in enumerate(inv[role]["api_servers"]):
                servers.append((letter, n, a))
                hosts.append(a.rpartition(":")[0])
    out = [("vLLM API server", "%s%d" % (letter, n), a, str(n)) for letter, n, a in servers]
    seen = []
    for h in hosts:
        if h not in seen:
            seen.append(h)
    for kind, key in (("npu-exporter", "npu_exporter_port"), ("node_exporter", "node_exporter_port")):
        out.extend((kind, "", "%s:%s" % (h, inv.get(key, dict(SHARED)[key])), None) for h in seen)
    mm = inv.get("mooncake_master")
    if mm:
        out.append(("Mooncake master", mm.get("node", "mooncake-master"), mm["address"], None))
    return out


def fetch(target):
    """(target, problem or None, engine values of an API server or None)."""
    kind, name, address, want = target
    try:
        with urllib.request.urlopen("http://%s/metrics" % address, timeout=3) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception as e:  # any failure is reported, never raised: the probe only reads
        return target, "不通：%s" % (getattr(e, "reason", None) or e), None
    if want is None:
        return target, None, None
    return target, None, sorted(set(re.findall(r'^vllm:[^{\n]*\{[^}\n]*\bengine="(\d+)"', body, re.M)), key=int)


def engine_check(results):
    """The engine label against the instance number, one side at a time. A side whose API servers (more than one)
    all report the same engine values is a note, not a failure: the label then says nothing about the order
    (scripts/verify_panel1.py of the deploy pack does the same). Returns (results with problems, notes)."""
    notes, same = [], set()
    for letter in ("P", "D"):
        side = [r for r in results if r[0][0] == "vLLM API server" and r[0][1].startswith(letter) and r[2]]
        if len(side) > 1 and len({tuple(r[2]) for r in side}) == 1:
            same.add(letter)
            notes.append("提示：%s 侧 %d 个 API server 的 engine 标签都是 %s，用它核对不了顺序（实例不开 DP 时是这样）："
                         "这一侧的顺序按附录 A 的 A1 核对" % (letter, len(side), ",".join(side[0][2])))
    out = []
    for target, problem, engines in results:
        if problem is None and engines is not None and target[1][:1] not in same and engines != [target[3]]:
            problem = "engine 标签是 %s，应为 %s（清单里的顺序和 DP rank 不一致）" % (",".join(engines) or "没有", target[3])
        out.append((target, problem))
    return out, notes


def cmd_probe(args):
    if not args:
        raise Bad("用法：make_inventories.py probe <inventory.json> [<inventory.json> ...]")
    bad_total, note_total = 0, 0
    for path in args:
        try:
            with open(path, encoding="utf-8") as f:
                inv = json.load(f)
        except (OSError, ValueError) as e:
            raise Bad("读不了 %s：%s" % (path, e))
        targets = targets_of(inv)
        with ThreadPoolExecutor(max_workers=16) as pool:
            results, notes = engine_check(list(pool.map(fetch, targets)))
        name = os.path.basename(os.path.dirname(os.path.abspath(path)))
        parts, bad = [], []
        for kind in ("vLLM API server", "npu-exporter", "node_exporter", "Mooncake master"):
            rs = [r for r in results if r[0][0] == kind]
            if rs:
                parts.append("%s %d/%d" % (kind, sum(1 for r in rs if r[1] is None), len(rs)))
            bad.extend("  " + " ".join(x for x in (kind, r[0][1], r[0][2], r[1]) if x) for r in rs if r[1] is not None)
        print("%s：%s" % (name, "，".join(parts)))
        for line in bad + notes:
            print(line)
        bad_total += len(bad)
        note_total += len(notes)
    if bad_total:
        print("RESULT: FAIL（%d 个目标有问题，见上面缩进的行）" % bad_total)
    elif note_total:
        print("RESULT: OK（全部目标可访问；engine 标签见上面的提示）")
    else:
        print("RESULT: OK（全部目标可访问，engine 标签与实例号一致）")
    return 1 if bad_total else 0


def cmd_defaults(args):
    if len(args) != 1:
        raise Bad("用法：make_inventories.py defaults <glm53-1 的 inventory.json>")
    sides, shared = load_defaults(args[0])
    for letter, _ in ROLES:
        ports, tp = sides[letter]
        print("%s 机器：每台 %d 个实例，端口 %s，TP %d" % (letter, len(ports), ",".join(str(p) for p in ports), tp))
    print("npu-exporter 端口 %s，node_exporter 端口 %s，抓取间隔 %s，超时 %s，保留 %s" % tuple(shared[k] for k, _ in SHARED))


def cmd_build(args):
    only = None
    if len(args) == 7 and args[5] == "--cluster":
        only, args = args[6], args[:5]
    if len(args) != 5 or args[1] != "--defaults" or args[3] != "--out":
        raise Bad("用法：make_inventories.py build <clusters.txt> --defaults <glm53-1 的 inventory.json> --out <目录> [--cluster <集群名>]")
    list_file, defaults, out = args[0], args[2], args[4]
    sides, shared = load_defaults(defaults)
    clusters = parse(list_file, sides, only)
    built = [(name, inventory(machines, master, shared), machines, master) for name, master, machines in clusters]
    for name, inv, machines, master in built:
        d = os.path.join(out, name)
        if not os.path.isdir(d):
            os.makedirs(d)
        tmp = os.path.join(d, "inventory.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(inv, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, os.path.join(d, "inventory.json"))
        parts = []
        for letter, _ in ROLES:
            ms = [m for m in machines if m[0] == letter]
            k = sum(len(m[2]) for m in ms)
            parts.append("%s %d 台 %d 个实例（%s0–%s%d）" % (letter, len(ms), k, letter, letter, k - 1))
        targets = sum(len(m[2]) for m in machines) + 2 * len(machines) + 1
        form = "两个列表的写法" if "prefill" in inv else "api_servers 写法（同一侧 TP 不同）"
        print("集群 %s：%s，%s，master %s；抓取目标 %d 个；%s → %s" % (
            name, parts[0], parts[1], master, targets, form, os.path.join(d, "inventory.json")))
    if only is not None:
        print("RESULT: OK（集群 %s，%d 台机器；清单文件里其他集群的行没有读）" % (only, len(built[0][2])))
    else:
        print("RESULT: OK（%d 套集群，%d 台机器）" % (len(built), sum(len(b[2]) for b in built)))


def main():
    utf8_stdout()
    cmds = {"defaults": cmd_defaults, "build": cmd_build, "probe": cmd_probe}
    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    try:
        return cmds[sys.argv[1]](sys.argv[2:]) or 0
    except Bad as e:
        print("错误：" + str(e).replace("\n", "\n错误："), file=sys.stderr)
        print("RESULT: FAIL（没有写任何文件）" if sys.argv[1] == "build" else "RESULT: FAIL", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
