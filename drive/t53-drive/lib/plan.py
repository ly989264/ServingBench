#!/usr/bin/env python3
"""Monitor-side helper of t53drive.sh: the machine list, the inventory, expected values, and judging what each
machine sent back. Standard library only; run as python3 -I.

  plan.py parse <list>                         check the list, print shell assignments of the cluster values
  plan.py machines <list>                      one line per machine: index name addr role ports container
  plan.py inventory <list> <out.json> [p1.json]  panel two's inventory.json from the list (exporter ports from panel one)
  plan.py compare <list> <new.csv> <p1.csv>    the list against panel one's instances (old step 10; both csv
                                               files are inventory_tool.py csv output)
"""
import json
import os
import re
import sys

KEYS = ("CLUSTER", "MODEL_GROUP", "PANEL1_CLUSTER", "PROMETHEUS_URL", "PROMETHEUS_CONTAINER", "PROMETHEUS_YML",
        "ENV_FROM", "MODEL_NAME", "MOONCAKE", "PROXY", "PROXY_NEW",
        "PROXY_NEW_SHA256", "PROXY_ORIG_SHA256", "KITS", "PANEL_PACK", "ARRANGE_DIR", "STATE")
REQUIRED = ("CLUSTER", "MODEL_GROUP", "ENV_FROM", "KITS", "ARRANGE_DIR")
DEFAULTS = {"MOONCAKE": "replace", "PROXY": "on"}   # the user, 11-22
NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


def die(msg):
    print("清单有问题：" + msg)
    sys.exit(2)


def ports_of(text):
    out = []
    for part in text.split(","):
        if re.fullmatch(r"\d+-\d+", part):
            a, b = map(int, part.split("-"))
            if b < a or b - a > 255:
                die("端口段 %s 不对" % part)
            out += list(range(a, b + 1))
        elif re.fullmatch(r"\d+", part):
            out.append(int(part))
        else:
            die("端口 %s 不对：写成 9900、9100-9115 或 9100,9102" % part)
    if len(set(out)) != len(out):
        die("端口 %s 有重复" % text)
    return out


def load(path):
    vals, machines = {}, []
    for no, raw in enumerate(open(path, encoding="utf-8"), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.fullmatch(r"([A-Z0-9_]+)=(.*)", line)
        if m:
            if m.group(1) not in KEYS:
                die("第 %d 行：不认识的项 %s" % (no, m.group(1)))
            vals[m.group(1)] = m.group(2).strip().strip("'\"")
            continue
        f = line.split()
        if len(f) < 3:
            die("第 %d 行：机器一行至少写 地址 角色 端口" % no)
        addr, role = f[0], f[1]
        if not NAME_RE.match(addr):
            die("第 %d 行：地址 %s 不对" % (no, addr))
        if role in ("P", "D"):
            if len(f) > 5:
                die("第 %d 行：P、D 一行是 地址 角色 端口 [容器] [启动文件]" % no)
            cont = f[3] if len(f) >= 4 else "-"
            start = f[4] if len(f) == 5 else "-"
            if start != "-" and not start.startswith("/"):
                die("第 %d 行：启动文件要写容器里的绝对路径" % no)
            machines.append({"addr": addr, "role": role, "ports": ports_of(f[2]), "container": cont, "file": start, "line": no})
        elif role == "proxy":
            if len(f) != 5:
                die("第 %d 行：proxy 一行是 地址 proxy 端口 容器 容器里的proxy文件" % no)
            machines.append({"addr": addr, "role": "proxy", "ports": ports_of(f[2]), "container": f[3], "file": f[4], "line": no})
        else:
            die("第 %d 行：角色 %s 不对，只能是 P、D、proxy" % (no, role))
    for k, v in DEFAULTS.items():
        vals.setdefault(k, v)
        vals[k] = vals[k] or v
    for k in REQUIRED:
        if not vals.get(k):
            die("缺 %s=" % k)
    if vals["MOONCAKE"] not in ("replace", "skip"):
        die("MOONCAKE 只能是 replace 或 skip")
    if vals["PROXY"] not in ("on", "off"):
        die("PROXY 只能是 on 或 off")
    px = [m for m in machines if m["role"] == "proxy"]
    if vals["PROXY"] == "on":
        if len(px) != 1:
            die("PROXY=on 时要正好一行 proxy")
        for k in ("PROXY_NEW", "PROXY_NEW_SHA256", "PROXY_ORIG_SHA256"):
            if not re.fullmatch(r"[0-9a-f]{64}", vals.get(k, "")) and k != "PROXY_NEW":
                die("PROXY=on 时 %s= 要写 64 位 sha256" % k)
        if not vals.get("PROXY_NEW"):
            die("PROXY=on 时要写 PROXY_NEW=")
    elif px:
        die("PROXY=off 时不写 proxy 那一行")
    pd = [m for m in machines if m["role"] in ("P", "D")]
    if not any(m["role"] == "P" for m in pd) or not any(m["role"] == "D" for m in pd):
        die("P、D 各至少一台")
    seen = set()
    for m in pd:
        if m["addr"] in seen:
            die("地址 %s 写了两次" % m["addr"])
        seen.add(m["addr"])
    if px and px[0]["addr"] not in seen:
        die("proxy 要在集群的某台机器上（地址要和 P 或 D 的一行相同）")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?", vals["CLUSTER"]):
        die("CLUSTER 只能有小写字母、数字和 -")
    # Panel two writes its scrape jobs and alert rules into a Prometheus: by default the panel one of the same cluster
    # (PANEL1_CLUSTER unset). A cluster without panel one (test-glm53 on site) writes PANEL1_CLUSTER=- and the
    # PROMETHEUS_* values of its own Prometheus, as remove-panel prints them from the old deploy.env.
    p1 = vals.get("PANEL1_CLUSTER") or vals["CLUSTER"]
    if p1 not in (vals["CLUSTER"], "-"):
        die("PANEL1_CLUSTER 只能不写（用同名集群的面板一）或写 -（没有面板一）")
    vals["PANEL1_CLUSTER"] = p1
    prom = [k for k in ("PROMETHEUS_URL", "PROMETHEUS_CONTAINER", "PROMETHEUS_YML") if vals.get(k)]
    if p1 != "-" and prom:
        die("有面板一的集群不写 %s（面板二用面板一的 Prometheus）" % "、".join(prom))
    if p1 == "-" and "PROMETHEUS_YML" not in prom:
        die("PANEL1_CLUSTER=- 时要写 PROMETHEUS_YML 等几行（remove-panel 打印的那几行）")
    if not vals.get("PANEL_PACK"):
        vals["PANEL_PACK"] = os.path.join(vals["KITS"], "vllm-pd-grafana-panel2")
    if not vals.get("STATE"):
        vals["STATE"] = os.path.expanduser("~/t53drive-state")
    for k in ("KITS", "PANEL_PACK", "ARRANGE_DIR", "STATE", "ENV_FROM", "PROXY_NEW"):
        if vals.get(k):
            vals[k] = os.path.expanduser(vals[k])
    # machine names: P1.., D1.. in list order (only these appear on screen, never the address)
    n = {"P": 0, "D": 0}
    for m in pd:
        n[m["role"]] += 1
        m["name"] = "%s%d" % (m["role"], n[m["role"]])
    if px:
        px[0]["name"] = "proxy"
    return vals, pd, px


def expected(pd):
    """Values the checks expect, computed from the list (not from any one cluster)."""
    d_total = sum(len(m["ports"]) for m in pd if m["role"] == "D")
    return {"d_total": d_total, "p_total": sum(len(m["ports"]) for m in pd if m["role"] == "P")}


def inventory(vals, pd):
    inv = {"model": vals.get("MODEL_NAME") or "glm-5.3",
           "prefill": {"api_servers": ["%s:%d" % (m["addr"], p) for m in pd if m["role"] == "P" for p in m["ports"]]},
           "decode": {"api_servers": ["%s:%d" % (m["addr"], p) for m in pd if m["role"] == "D" for p in m["ports"]]}}
    return inv


def read_csv(path):
    rows = [line.strip().rsplit(",", 1) for line in open(path) if line.strip()][1:]
    return {r: [a for a, n in rows if n.startswith(r)] for r in "PD"}


def compare(vals, pd, newcsv, oldcsv):
    new, old = read_csv(newcsv), read_csv(oldcsv)
    bad = 0
    idx = {m["addr"]: m["name"] for m in pd}

    def where(a):
        h, p = a.rsplit(":", 1)
        return "%s:%s" % (idx.get(h, "清单外的机器"), p)
    for r in "PD":
        n, o = new[r], old.get(r, [])
        if n == o:
            print("OK   %s 和面板一相同（%d 个，%s0–%s%d 一一对应）" % (r, len(n), r, r, len(n) - 1))
        elif sorted(n) == sorted(o):
            k = next(k for k in range(len(n)) if n[k] != o[k])
            print("FAIL %s 实例相同、顺序不同：从 %s%d 起对不上（清单 %s，面板一 %s）" % (r, r, k, where(n[k]), where(o[k])))
            bad = 1
        else:
            more = [where(a) for a in n if a not in o]
            less = [where(a) for a in o if a not in n]
            print("FAIL %s 和面板一不同：清单多 %s；清单少 %s" % (r, " ".join(more[:4]) or "无", " ".join(less[:4]) or "无"))
            bad = 1
    return bad


def main():
    cmd = sys.argv[1]
    vals, pd, px = load(sys.argv[2])
    if cmd == "parse":
        e = expected(pd)
        for k in KEYS:
            print("%s=%s" % (k, shq(vals.get(k, ""))))
        print("N_P=%d N_D=%d D_TOTAL=%d P_TOTAL=%d" % (sum(m["role"] == "P" for m in pd), sum(m["role"] == "D" for m in pd),
                                                      e["d_total"], e["p_total"]))
    elif cmd == "machines":
        for m in pd + px:
            print("%s %s %s %s %s %s" % (m["name"], m["addr"], m["role"], ",".join(map(str, m["ports"])), m["container"],
                                         m.get("file", "-")))
    elif cmd == "inventory":
        inv = inventory(vals, pd)
        if len(sys.argv) > 4 and sys.argv[4]:   # panel one's exporter ports: the add checks them against the vLLM ports
            p1 = json.load(open(sys.argv[4]))
            inv.update({k: p1[k] for k in ("npu_exporter_port", "node_exporter_port") if k in p1})
        json.dump(inv, open(sys.argv[3], "w"), indent=1)
    elif cmd == "compare":
        sys.exit(compare(vals, pd, sys.argv[3], sys.argv[4]))
    else:
        die("用法")


def shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


if __name__ == "__main__":
    main()
