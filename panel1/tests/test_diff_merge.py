#!/usr/bin/env python3
"""Offline tests of diff_panel1.py (built, with the baseline embedded): decision units, --merge, config values,
exit codes (test-only). Usage: test_diff_merge.py <diff_panel1.py>"""
import copy
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.stdout = __import__("io").TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)  # LANG=C runs
DIFF = os.path.abspath(sys.argv[1])
T = tempfile.mkdtemp(prefix="t26dm-", dir=os.environ.get("T26_TMP"))
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print("%s %s %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)


def run(*args):
    r = subprocess.run([sys.executable, "-I", DIFF] + list(args), cwd=T, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       encoding="utf-8")
    return r.returncode, r.stdout


ns = {}
src = open(DIFF, encoding="utf-8").read()
exec(compile(src.replace('if __name__ == "__main__":', "if False:"), "diff_panel1", "exec"), ns)
base_raw = ns["baseline"]()
os.makedirs(os.path.join(T, "base"))
run("--dump-baseline", os.path.join(T, "base"))


def write(tag, boards):
    d = os.path.join(T, tag)
    os.makedirs(d, exist_ok=True)
    paths = []
    for side, name in (("P", "vllm-pd-prefill.json"), ("D", "vllm-pd-decode.json")):
        p = os.path.join(d, name)
        json.dump(boards[side], open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        paths.append(p)
    return paths


def walk(ps):
    for p in ps:
        yield p
        yield from walk(p.get("panels") or [])


def panel(d, pid):
    return next(p for p in walk(d["panels"]) if p["id"] == pid)


# --- planted site: two titles, an added panel, a deleted panel, a query, a variable field, a dashboard field
site = copy.deepcopy(base_raw)
P, D = site["P"], site["D"]
ids_p = [p["id"] for p in walk(P["panels"]) if p.get("type") not in ("row",) and p.get("targets")]
t1, t2, q1 = ids_p[0], ids_p[1], ids_p[2]
panel(P, t1)["title"] = "实验室改标题一"
panel(P, t2)["title"] = "实验室改标题二"
panel(P, q1)["targets"][0]["expr"] = panel(P, q1)["targets"][0]["expr"] + " * 1"
new = copy.deepcopy(panel(P, t1))
new["id"] = 9001
new["title"] = "实验室新增"
new["gridPos"] = dict(new["gridPos"], y=999)
P["panels"].append(new)
gone = [p["id"] for p in D["panels"] if p.get("type") != "row"][-1]
D["panels"] = [p for p in D["panels"] if p["id"] != gone]
D["refresh"] = "1m" if D.get("refresh") != "1m" else "5m"
var = next(v for v in P["templating"]["list"] if v.get("type") == "query")
var["sort"] = 7
sp = write("site", site)

code, out = run("--files", *sp, "--decisions-out", "dec.txt", "-o", "site-diff.txt")
lines = [l for l in open(os.path.join(T, "dec.txt"), encoding="utf-8") if not l.startswith("#")]
keys = [l.split()[1] for l in lines]
want = {"P/%d/title" % t1, "P/%d/title" % t2, "P/%d/targets/%s" % (q1, panel(P, q1)["targets"][0]["refId"]), "P/9001/新增",
        "D/%d/删除" % gone, "D/dash/refresh", "P/var/%s" % var["name"]}
check("planted site: exit 1, one decision unit per change", code == 1 and set(keys) == want and len(keys) == len(want),
      "exit %d, %d units %s" % (code, len(keys), sorted(set(keys) ^ want)))

base_paths = [os.path.join(T, "base", n) for n in ("vllm-pd-prefill.json", "vllm-pd-decode.json")]
code, out = run("--files", *base_paths, "-o", "b.txt")
check("baseline itself: exit 0", code == 0, out.strip().splitlines()[-1])


def merged(keep, tag):
    with open(os.path.join(T, "dec.txt"), encoding="utf-8") as f:
        text = f.read()
    outl = []
    for l in text.split("\n"):
        if l and not l.startswith("#"):
            k = l.split()[1]
            l = ("保留" if k in keep else "丢弃") + l[2:]
        outl.append(l)
    open(os.path.join(T, tag + ".txt"), "w", encoding="utf-8").write("\n".join(outl))
    od = os.path.join(T, tag)
    code, out = run("--files", *sp, "--merge", "--decisions", tag + ".txt", "--out-dir", od)
    return code, out, od


def norm_pair(paths):
    return {s: ns["normalize"](json.load(open(p, encoding="utf-8"))) for s, p in zip(("P", "D"), paths)}


site_n, base_n = norm_pair(sp), norm_pair(base_paths)
bad = []
n_sub = 0
for r in (0, 1, 3, len(want) - 1, len(want)):
    for keep in itertools.combinations(sorted(want), r):
        n_sub += 1
        code, out, od = merged(set(keep), "m%d" % n_sub)
        if code != 0:
            bad.append((keep, out.strip()[-200:]))
            continue
        nn = norm_pair([os.path.join(od, n) for n in ("vllm-pd-prefill.json", "vllm-pd-decode.json")])
        left = set(k for k, _ in ns["units"](nn, site_n))
        moved = set(k for k, _ in ns["units"](base_n, nn))
        if left != want - set(keep) or moved != set(keep):
            bad.append((keep, "left %s moved %s" % (sorted(left), sorted(moved))))
        # the output carries the baseline's uid and title
        pj = json.load(open(os.path.join(od, "vllm-pd-prefill.json"), encoding="utf-8"))
        if pj["uid"] != base_raw["P"]["uid"] or "panels" not in pj:
            bad.append((keep, "uid %s" % pj.get("uid")))
check("--merge: all %d subsets of size 0, 1, 3, 6, 7: baseline + kept, site - dropped" % n_sub, not bad, str(bad[:2]))

code, out, od = merged(set(want), "all")
nn = norm_pair([os.path.join(od, n) for n in ("vllm-pd-prefill.json", "vllm-pd-decode.json")])
check("keep all: new baseline equals the site", ns["dump"](nn) == ns["dump"](site_n))
code, out, od = merged(set(), "none")
nn = norm_pair([os.path.join(od, n) for n in ("vllm-pd-prefill.json", "vllm-pd-decode.json")])
check("drop all: new baseline equals the old one", ns["dump"](nn) == ns["dump"](base_n))

# --- config values are not differences
cl = copy.deepcopy(base_raw)
for s in ("P", "D"):
    cl[s]["uid"] += "-cpu1"
    cl[s]["title"] += "（cpu1）"
    for v in cl[s]["templating"]["list"]:
        if v["type"] == "datasource":
            v["regex"] = "/^Prometheus（cpu1）$/"
            v["current"] = {"text": "Prometheus（cpu1）", "value": "prometheus-cpu1"}
code, out = run("--files", *write("cluster", cl), "-o", "c.txt")
check("cluster board (uid, title suffix, datasource regex and current): exit 0", code == 0, out.strip().splitlines()[-1])
cl2 = copy.deepcopy(cl)
cl2["P"]["title"] = "别的标题（cpu1）"
code, out = run("--files", *write("cluster2", cl2), "-o", "c2.txt")
check("cluster board with a renamed title: exit 1", code == 1)

# --- errors
code, out = run("--files", "nope.json", "nope2.json")
check("missing file: exit 2", code == 2, out.strip()[-80:])
text = open(os.path.join(T, "dec.txt"), encoding="utf-8").read()
open(os.path.join(T, "short.txt"), "w", encoding="utf-8").write("\n".join(text.split("\n")[:-2]))
code, out = run("--files", *sp, "--merge", "--decisions", "short.txt", "--out-dir", os.path.join(T, "x1"))
check("decision file with a line missing: refused, exit 2", code == 2 and "少了" in out, out.strip()[-80:])
open(os.path.join(T, "bad.txt"), "w", encoding="utf-8").write(text.replace("\n保留 ", "\n也许 ", 1))
code, out = run("--files", *sp, "--merge", "--decisions", "bad.txt", "--out-dir", os.path.join(T, "x2"))
check("decision word other than 保留/丢弃: refused, exit 2", code == 2 and "读不懂" in out)
code, out = run("--files", *base_paths, "--merge", "--decisions", "dec.txt", "--out-dir", os.path.join(T, "x3"))
check("decision file for another site state: refused, exit 2", code == 2 and "摘要" in out)
# a kept panel in a row whose addition was dropped
rs = copy.deepcopy(base_raw)
row = {"id": 9100, "type": "row", "title": "实验室新行", "collapsed": False, "gridPos": {"h": 1, "w": 24, "x": 0, "y": 2000}, "panels": []}
kid = copy.deepcopy(panel(rs["P"], t1))
kid.update(id=9101, title="新行里的", gridPos=dict(kid["gridPos"], y=2001))
rs["P"]["panels"] += [row, kid]
rp = write("rowsite", rs)
run("--files", *rp, "--decisions-out", "rdec.txt", "-o", "r.txt")
rt = open(os.path.join(T, "rdec.txt"), encoding="utf-8").read().replace("保留 P/9100/新增", "丢弃 P/9100/新增")
open(os.path.join(T, "rdec2.txt"), "w", encoding="utf-8").write(rt)
code, out = run("--files", *rp, "--merge", "--decisions", "rdec2.txt", "--out-dir", os.path.join(T, "x4"))
check("kept panel inside a dropped new row: refused, exit 2", code == 2 and "所在的行" in out, out.strip()[-80:])
code, out = run("--files", *rp, "--merge", "--decisions", "rdec.txt", "--out-dir", os.path.join(T, "x5"))
check("new row and its panel kept together: merged", code == 0, out.strip()[-60:])

shutil.rmtree(T)
n_fail = sum(1 for r in results if not r[1])
print("RESULT: %d checks, %d failed" % (len(results), n_fail))
sys.exit(1 if n_fail else 0)
