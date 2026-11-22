#!/usr/bin/env python3
"""t53drive, monitor side: the registry, and judging what the panel's own commands printed. python3 -I, stdlib only.

  mon.py reg <cluster> <panel1|panel2> <dir|port|has>    a value from ~/.vllm-pd-clusters.json ("-" when absent)
  mon.py env <deploy.env> <key>...                       which keys are missing or empty (prints "missing k" lines)
  mon.py add <log>                                       panel2_cluster.sh add
  mon.py check <log> <instances>                         panel2_cluster.sh check
  mon.py arrange <log> <cluster>                         arrange_boards.py plan: only this cluster's items?
  mon.py selfcheck <tsv> <instances>                     filter self-check (old step 36)
  mon.py coverage <log> <proxy on|off> <mooncake replace|skip> [lab]   bin/coverage.sh (old step 64)
  mon.py statediff <before> <after> <cluster> <panel1 cluster> [grafana container]   arrange_boards.py state (old 65)
  mon.py empty <tsv1> <tsv2>                             empty ratio, two shots (old steps 37, 39)
  mon.py first <tsv> <instance>                          first batch (old step 42): one line, never fails
Each judging command prints one line "OK ..." or "FAIL ..." and exits 0 / 1.
"""
import json
import os
import re
import sys


def ok(msg):
    print("OK   " + msg)
    sys.exit(0)


def fail(msg):
    print("FAIL " + msg)
    sys.exit(1)


def tsv(path):
    rows = [l.rstrip("\n").split("\t") for l in open(path, encoding="utf-8", errors="replace") if l.strip()]
    if not rows:
        return []
    head = rows[0]
    return [dict(zip(head, r)) for r in rows[1:] if len(r) == len(head)]


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def lines(path):
    return [l.rstrip("\n") for l in open(path, encoding="utf-8", errors="replace")]


def reg(cluster, panel, what):
    try:
        d = json.load(open(os.path.expanduser("~/.vllm-pd-clusters.json")))
    except (OSError, ValueError):
        print("-")
        return
    sec = d.get("clusters", {}).get(cluster, {}).get(panel)
    if what == "has":
        print("yes" if sec else "no")
    elif not sec:
        print("-")
    elif what == "dir":
        print(sec.get("dir") or "-")
    elif what == "port":
        print(sec.get("values", {}).get("PROMETHEUS_PORT") or "-")
    elif what == "others":   # other clusters that have a panel two
        print(" ".join(sorted(c for c, v in d.get("clusters", {}).items() if c != cluster and v.get("panel2"))) or "-")


def last_result(ls):
    r = [l for l in ls if l.startswith("RESULT:")]
    return r[-1] if r else ""


def j_add(path):
    ls = lines(path)
    r = last_result(ls)
    if r.startswith("RESULT: OK（集群"):
        ok("面板二的监控机部分装好了")
    bad = [l for l in ls if l.startswith("FAIL")]
    fail("add 没过：%s" % ((bad[0] if bad else r)[:100] or "没有 RESULT 行"))


def j_check(path, n):
    ls = lines(path)
    r = last_result(ls)
    bad = [l for l in ls if l.startswith("FAIL")]
    if r != "RESULT: OK":
        fail("check 没过：%s" % ((bad[0] if bad else r)[:100] or "没有 RESULT 行"))
    if not any("面板二目录是 v3.4-extra" in l for l in ls):
        fail("check 没说面板二目录是 v3.4-extra")
    m = [re.search(r"过滤器版本：(\d+) 个实例", l) for l in ls]
    m = [x for x in m if x]
    if not m or int(m[0].group(1)) != int(n):
        fail("过滤器版本那一行的实例数是 %s，清单是 %s" % (m[0].group(1) if m else "无", n))
    ok("check：RESULT: OK，%s 个实例都是这一版的过滤器" % n)


def j_arrange(path, cluster):
    ls = lines(path)
    acts = [l for l in ls if l and not l.startswith(("==", "RESULT", "OK", "INFO", " "))]
    other = [l for l in acts if cluster not in l]
    if other:
        fail("看板归位还要改别的集群的项（%s）：没有执行 apply" % other[0][:60])
    print("OK   看板归位 %d 项，都是本集群的" % len(acts))
    sys.exit(0 if acts else 3)   # 3: nothing to change


def j_selfcheck(path, n):
    rows = tsv(path)
    if len(rows) != int(n):
        fail("过滤器自检有 %d 个实例，清单是 %s 个（拉起不到 5 分钟，或者有实例没写自检行）" % (len(rows), n))
    d0 = [r["instance"] for r in rows if r["instance"].startswith("D") and (num(r.get("cand_maps")) or 0) <= 0]
    ne = [r["instance"] for r in rows if (num(r.get("no_execution_id")) or 0) > 0]
    if d0:
        fail("D 实例 cand_maps 是 0：%s" % " ".join(d0[:8]))
    if ne:
        fail("no_execution_id 大于 0：%s" % " ".join(ne[:8]))
    ok("过滤器自检：%s 个实例，D 的 cand_maps 都大于 0，no_execution_id 都是 0" % n)


PUT3 = ["P 后台 wait_for_save/put/submit", "P 后台 wait_for_save/put/wait_for_transfer", "P 后台 wait_for_save/put/finalize"]
LB2 = ["P lb_proxy", "D lb_proxy"]


def j_coverage(path, proxy, mc, lab=""):
    ls = lines(path)
    r = last_result(ls)
    allow = (LB2 if proxy == "off" else []) + (PUT3 if mc == "skip" else [])
    may = [x for x in lab.split("、") if x]   # lab only (T53_LAB_COVERAGE_EMPTY): the lab's sample has no such records
    tot = [l for l in ls if re.match(r"\d+ 段里 \d+ 段有值", l)]
    summary = tot[-1].split("；")[0] if tot else ""
    if r == "RESULT: OK":
        empty = []
    else:
        m = re.match(r"RESULT: FAIL（有来源但这些请求里都为空的 \d+ 段：(.*)）$", r)
        if not m:
            fail("各段有值：%s" % (r[8:90] or "没有 RESULT 行"))
        empty = m.group(1).split("、")
    if may == ["*"]:   # lab: the replayed sample cannot fill the glm5.3 segments; any may be empty
        may = [e for e in empty if e not in allow]
    extra = [e for e in empty if e not in allow and e not in may]
    if extra:
        fail("各段有值：这些段不该为空：%s" % "、".join(extra[:6]))
    if [e for e in allow if e not in empty]:
        fail("各段有值：按清单应为空的段反而有值：%s" % "、".join(e for e in allow if e not in empty))
    lab_n = len([e for e in empty if e in may])
    ok("各段有值：%s，为空的正好是按清单不做的 %d 段%s" % (summary, len(allow), "（实验室的样例另缺 %d 段）" % lab_n if lab_n else ""))


def j_statediff(a, b, cluster, p1, grafana=""):
    A, B = set(lines(a)), set(lines(b))
    # Grafana restarts once when this add installs the ClickHouse plugin (no other panel two had it): not a change of
    # panel one, reported on the line
    restarted = bool(grafana) and any(l.startswith("容器 %s 启动于" % grafana) for l in A ^ B)
    allowed = ("集群 %s " % cluster, "容器 %s 启动于" % grafana if grafana else "\0", "/vllm-pd-grafana-panel2/%s/" % cluster, "vllm-pd-request-%s " % cluster, "vllm-pd-clickhouse-%s " % cluster,
               "vllm-pd-vector-%s " % cluster, "vllm-pd-prometheus-%s " % cluster, "/vllm-pd-prometheus-%s/" % cluster,
               "/vllm-pd-grafana-panel1/%s/prometheus/prometheus.yml " % p1)
    bad = [l for l in sorted(A ^ B) if not any(x in l for x in allowed)]
    if bad:
        fail("面板一或别的集群有变化（%d 行），第一行：%s" % (len(bad), bad[0][:70]))
    ok("面板一和别的集群没有变（只有本集群面板二的项和面板一 prometheus.yml 不同%s）" % ("；Grafana 装插件重启过一次" if restarted else ""))


def j_empty(p1, p2, lab="0"):
    if lab != "1":
        _j_empty(p1, p2)
    import io   # lab only: the replayed sample has no device-side timing, so D steps are all empty
    buf, sys.stdout = sys.stdout, io.StringIO()
    try:
        _j_empty(p1, p2)
    except SystemExit:
        pass
    got, sys.stdout = sys.stdout.getvalue().strip(), buf
    got = got[5:].split("留空比例：", 1)[-1]
    ok("留空比例（实验室：样例没有设备侧计时，只报不判）：%s" % got[:80])


def _j_empty(p1, p2):
    a = {r["instance"]: r for r in tsv(p1)}
    b = {r["instance"]: r for r in tsv(p2)}
    if not b:
        fail("留空比例第二次没有结果")
    w = num(next(iter(b.values())).get("window_min"))
    if w is not None and w > 45:
        fail("留空比例：窗口 %d 分钟，t10 不对" % w)
    fails, notes = [], []
    for inst in sorted(b):
        r, r0 = b[inst], a.get(inst, {})
        side_d = inst.startswith("D")
        if r0 and r0.get("pid_list") != r.get("pid_list"):
            (fails if side_d else notes).append("%s 进程重启过" % inst)
            continue
        ended = num(r.get("ended_win")) or 0
        for k in ("forward", "sampling"):
            pct = num(r.get("%s_empty_pct" % k))
            win = num(r.get("%s_win" % k)) or 0
            if pct is None:
                if ended > 0 and win > 0:
                    fails.append("%s %s 调度器侧断了" % (inst, k))
                elif ended == 0:
                    notes.append("%s 这段时间没有请求结束" % inst)
                continue
            grew = (num(r.get("%s_empty" % k)) or 0) > (num(r0.get("%s_empty" % k)) or 0)
            if pct > 1 and grew:
                (fails if side_d else notes).append("%s %s 留空 %.2f%%" % (inst, k, pct))
            elif pct > 1:
                notes.append("%s %s 留空 %.2f%%（次数没增加）" % (inst, k, pct))
    if fails:
        fail("留空比例：" + "；".join(fails[:5]) + ("；共 %d 项" % len(fails) if len(fails) > 5 else ""))
    ok("留空比例：%d 个实例都不超过 1%%%s" % (len(b), ("（另有 %d 项只带回，见日志）" % len(notes)) if notes else ""))


def j_first(path, inst):
    rows = tsv(path)
    if not rows:
        print("INFO 首批数据：没有结果（只带回，不回滚）")
        sys.exit(0)
    res = {r["item"]: r for r in rows}
    f = [r for r in rows if r.get("result") == "FAIL"]
    nd = [r["item"] for r in rows if r.get("result") == "NO_DATA" and r["item"] != "RESULT"]
    tail = "；".join("%s %s/%s" % (r["item"], r["bad"], r["n"]) for r in f if r["item"] != "RESULT")
    print("INFO 首批数据（%s）：%s%s%s（只带回，不回滚）" % (inst, res.get("RESULT", {}).get("result", "?"),
                                                    "，FAIL：" + tail if tail else "", "，NO_DATA：" + "、".join(nd) if nd else ""))
    sys.exit(0)


def main():
    c, a = sys.argv[1], sys.argv[2:]
    if c == "reg":
        reg(*a)
    elif c == "env":
        vals = {}
        for l in lines(a[0]):
            m = re.match(r"^([A-Z0-9_]+)=(.*)$", l)
            if m:
                vals[m.group(1)] = m.group(2)
        for k in a[1:]:
            if not vals.get(k):
                print("missing " + k)
    else:
        {"add": j_add, "check": j_check, "arrange": j_arrange, "selfcheck": j_selfcheck, "coverage": j_coverage,
         "statediff": j_statediff, "empty": j_empty, "first": j_first}[c](*a)


if __name__ == "__main__":
    main()
