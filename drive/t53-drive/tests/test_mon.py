#!/usr/bin/env python3
"""Unit tests of lib/mon.py's judges on crafted inputs: python3 -I tests/test_mon.py (prints one line per case)."""
import os
import subprocess
import sys
import tempfile

MON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib", "mon.py")
T = tempfile.mkdtemp()
bad = 0


def f(name, text):
    p = os.path.join(T, name)
    open(p, "w", encoding="utf-8").write(text)
    return p


def case(label, args, rc, head):
    global bad
    r = subprocess.run([sys.executable, "-I", MON] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = r.stdout.decode()
    good = r.returncode == rc and out.startswith(head)
    bad += not good
    print("%s %-40s rc=%d %s" % ("PASS" if good else "FAIL", label, r.returncode, out.strip()[:90]))


def tsv(rows):
    return "\n".join("\t".join(map(str, r)) for r in rows) + "\n"


# selfcheck (old step 36)
H = ["instance", "cand_maps", "no_execution_id"]
case("selfcheck ok", ["selfcheck", f("s1", tsv([H, ["P0", 0, 0], ["D0", 3, 0], ["D1", 2, 0]])), "3"], 0, "OK")
case("selfcheck count", ["selfcheck", f("s2", tsv([H, ["P0", 0, 0], ["D0", 3, 0]])), "3"], 1, "FAIL 过滤器自检有 2")
case("selfcheck D cand_maps 0", ["selfcheck", f("s3", tsv([H, ["P0", 0, 0], ["D0", 0, 0]])), "2"], 1, "FAIL D 实例 cand_maps")
case("selfcheck no_execution_id", ["selfcheck", f("s4", tsv([H, ["P0", 0, 1], ["D0", 2, 0]])), "2"], 1, "FAIL no_execution_id")

# coverage (old step 64)
LB = "P lb_proxy、D lb_proxy"
PUT = "P 后台 wait_for_save/put/submit、P 后台 wait_for_save/put/wait_for_transfer、P 后台 wait_for_save/put/finalize"
cov = lambda empty: "56 段里 %d 段有值；x\n" % (56 - len(empty.split("、")) if empty else 56) + (
    "RESULT: OK\n" if not empty else "RESULT: FAIL（有来源但这些请求里都为空的 %d 段：%s）\n" % (len(empty.split("、")), empty))
case("coverage all, proxy on, mc replace", ["coverage", f("c1", cov("")), "on", "replace"], 0, "OK")
case("coverage mc skip: 3 put empty", ["coverage", f("c2", cov(PUT)), "on", "skip"], 0, "OK")
case("coverage proxy off: lb empty", ["coverage", f("c3", cov(LB)), "off", "replace"], 0, "OK")
case("coverage both off", ["coverage", f("c4", cov(LB + "、" + PUT)), "off", "skip"], 0, "OK")
case("coverage extra empty", ["coverage", f("c5", cov("D forward")), "on", "replace"], 1, "FAIL 各段有值：这些段不该为空")
case("coverage skip but put has values", ["coverage", f("c6", cov("")), "on", "skip"], 1, "FAIL 各段有值：按清单应为空")
case("coverage no RESULT", ["coverage", f("c7", "nothing\n"), "on", "replace"], 1, "FAIL 各段有值")

# statediff (old step 65)
A = "file /g/vllm-pd-grafana-panel1/glm53-1/deploy.env aaaa\nboard vllm-pd-decode-glm53-1 v3\n"
case("statediff same", ["statediff", f("a1", A), f("b1", A), "glm53-1", "glm53-1"], 0, "OK")
B2 = A + "file /g/vllm-pd-grafana-panel2/glm53-1/deploy.env bbbb\nboard vllm-pd-request-glm53-1 v1\n"
case("statediff own items only", ["statediff", f("a2", A), f("b2", B2), "glm53-1", "glm53-1"], 0, "OK")
B3 = A.replace("deploy.env aaaa", "deploy.env cccc")
case("statediff panel one changed", ["statediff", f("a3", A), f("b3", B3), "glm53-1", "glm53-1"], 1, "FAIL 面板一或别的集群有变化")
B4 = A + "board vllm-pd-request-test-glm53 v2\n"
case("statediff other cluster", ["statediff", f("a4", A), f("b4", B4), "glm53-1", "glm53-1"], 1, "FAIL")
B5 = A + "file /g/vllm-pd-prometheus-test-glm53/prometheus.yml dddd\n"
case("statediff own prometheus (no panel one)", ["statediff", f("a5", A), f("b5", B5), "test-glm53", "-"], 0, "OK")

G1 = A + "容器 obs-grafana 启动于 2026-11-22T09:41:46Z\n"
G2 = A + "容器 obs-grafana 启动于 2026-11-22T09:42:54Z\n集群 glm53-1 模型文件夹 glm53-1\n"
case("statediff Grafana restarted (plugin)", ["statediff", f("a6", G1), f("b6", G2), "glm53-1", "glm53-1", "obs-grafana"], 0, "OK")
case("statediff Grafana restart, name not given", ["statediff", f("a7", G1), f("b7", G2), "glm53-1", "glm53-1"], 1, "FAIL")
G3 = G1 + "容器 other-grafana 启动于 x\n"
case("statediff another container restarted", ["statediff", f("a8", G1), f("b8", G3), "glm53-1", "glm53-1", "obs-grafana"], 1, "FAIL")
case("coverage lab may-empty", ["coverage", f("c8", cov("D forward")), "on", "replace", "D forward、P x"], 0, "OK")
case("coverage lab any may be empty", ["coverage", f("c10", cov("D forward、D y")), "on", "skip", "*"], 1, "FAIL 各段有值：按清单应为空")
case("coverage lab any, put empty", ["coverage", f("c11", cov("D forward、" + PUT)), "on", "skip", "*"], 0, "OK")
case("coverage lab may-empty, other empty", ["coverage", f("c9", cov("D forward、D y")), "on", "replace", "D forward"], 1, "FAIL")

# empty ratio (old steps 37, 39)
E = ["instance", "pid_list", "window_min", "ended_win", "forward_empty_pct", "forward_win", "forward_empty",
     "sampling_empty_pct", "sampling_win", "sampling_empty"]
e1 = f("e1", tsv([E, ["D0", "1,2", 30, 5, 0.1, 100, 1, 0.0, 100, 0], ["P0", "9", 30, 5, 0.0, 10, 0, 0.0, 10, 0]]))
case("empty ok", ["empty", e1, f("e2", tsv([E, ["D0", "1,2", 30, 9, 0.2, 200, 2, 0.0, 200, 0], ["P0", "9", 30, 9, 0, 20, 0, 0, 20, 0]]))], 0, "OK")
case("empty D restarted", ["empty", e1, f("e3", tsv([E, ["D0", "1,3", 30, 9, 0.2, 200, 2, 0, 200, 0]]))], 1, "FAIL 留空比例：D0 进程重启过")
case("empty P restarted = note", ["empty", e1, f("e4", tsv([E, ["P0", "8", 30, 9, 0, 20, 0, 0, 20, 0]]))], 0, "OK")
case("empty D 2% growing", ["empty", e1, f("e5", tsv([E, ["D0", "1,2", 30, 9, 2.0, 200, 4, 0, 200, 0]]))], 1, "FAIL 留空比例：D0 forward 留空")
case("empty D 2% not growing", ["empty", e1, f("e6", tsv([E, ["D0", "1,2", 30, 9, 2.0, 200, 1, 0, 200, 0]]))], 0, "OK")
case("empty window wrong", ["empty", e1, f("e7", tsv([E, ["D0", "1,2", 60, 9, 0, 200, 1, 0, 200, 0]]))], 1, "FAIL 留空比例：窗口")
case("empty lab: report only", ["empty", e1, f("e9", tsv([E, ["D0", "1,2", 30, 9, 2.0, 200, 4, 0, 200, 0]])), "1"], 0, "OK   留空比例（实验室")
case("empty second empty", ["empty", e1, f("e8", tsv([E]))], 1, "FAIL 留空比例第二次没有结果")

# arrange plan
case("arrange own items", ["arrange", f("r1", "== plan\nmove vllm-pd-request-glm53-1 -> GLM-5.3/glm53-1\nRESULT: OK\n"), "glm53-1"], 0, "OK   看板归位 1 项")
case("arrange nothing", ["arrange", f("r2", "== plan\nRESULT: OK\n"), "glm53-1"], 3, "OK   看板归位 0 项")
case("arrange other cluster", ["arrange", f("r3", "move vllm-pd-request-test-glm53 -> GLM-5.3/test-glm53\n"), "glm53-1"], 1, "FAIL 看板归位还要改别的集群")

# first batch (old step 42): never fails
case("first no rows", ["first", f("q1", ""), "D0"], 0, "INFO 首批数据：没有结果")
case("first fail rows", ["first", f("q2", tsv([["item", "result", "bad", "n"], ["span", "FAIL", 1, 10], ["RESULT", "FAIL", 1, 10]])), "D0"], 0, "INFO 首批数据（D0）：FAIL")

print("RESULT: %s" % ("OK" if not bad else "FAIL %d" % bad))
sys.exit(1 if bad else 0)
