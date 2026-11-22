#!/usr/bin/env python3
"""T64 1.9: processes above vllm serve in the launch chain (start_p0.sh -> launch_online_dp.py -> per-rank python ->
run_dp_template.sh -> vllm serve) have no API-server ancestor. They are neither vLLM child processes nor part of any
instance: diag's environment-variable items and check's record-state items leave them out. A real child of vllm serve
without VLLM_LOGGING_CONFIG_PATH is still reported. Standard library only, no service needed:

    python3 tests/t64/test_proc_tree_19.py
"""
import json
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.join(ROOT, "vllm-pd-grafana-panel2", "scripts")
sys.dont_write_bytecode = True
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import criteria as C  # noqa: E402
import diag as D  # noqa: E402
import safety as S  # noqa: E402

CONF = "/data/pd-diag/conf"
LOG = "/data/pd-diag/logs"
CFG = CONF + "/pd_diag_logging.json"
GOOD_ENV = {"VLLM_LOGGING_CONFIG_PATH": CFG, "PYTHONPATH": CONF}
CHILD_ENV = dict(GOOD_ENV, PD_DIAG_INSTANCE_SOURCE="argv")
NOW, UP, HZ = 1000000, 10000, 100
START_TICKS = 500000                      # every process started at NOW - UP + 5000
START = NOW - UP + START_TICKS / HZ
SUMS = {f: "%064x" % i for i, f in enumerate(D.NODE_FILES)}
CNAME = "vllm-ascend-glm53"
ABOVE_PIDS = {"2", "3", "4"}              # launch_online_dp.py and its two per-rank processes


def tree(ports, broken_child=False):
    """[(pid, ppid, comm, cmd, env)] of one machine, one DP rank per port: the whole tree, collected or not."""
    t = [("1", "0", "bash", "bash /work/start_p0.sh", {}),
         ("2", "1", "python3", "python3 launch_online_dp.py --dp-size %d --vllm-start-port %s" % (len(ports), ports[0]), {})]
    for rank, port in enumerate(ports):
        b = 10 * (rank + 1)
        t += [(str(3 + rank), "2", "python3", "python3 launch_online_dp.py --rank %d --vllm-port %s" % (rank, port), {}),
              (str(b), str(3 + rank), "bash", "bash run_dp_template.sh %d" % rank, {}),
              (str(b + 1), str(b), "vllm", "/usr/bin/python3 /usr/local/bin/vllm serve /models/glm --port %s" % port, GOOD_ENV),
              (str(b + 2), str(b + 1), "VLLM::EngineCor", "VLLM::EngineCore_DP%d" % rank, CHILD_ENV),
              (str(b + 3), str(b + 2), "VLLM::Worker", "python3 -c from multiprocessing.spawn import spawn_main; "
                                                       "spawn_main(tracker_fd=5, pipe_handle=7) --multiprocessing-fork", CHILD_ENV)]
    if broken_child:
        # a real child of vllm serve that lost the variable: must still be reported
        t.append(("99", str(11), "VLLM::DPCoord", "VLLM::DPCoordinator", {"PYTHONPATH": CONF}))
    return t


def inside_lines(t):
    """What diag's INSIDE script prints for this tree: only command lines with vllm / VLLM / spawn_main."""
    out = ["clock\t%d\t%d\t%d" % (NOW, UP, HZ)]
    for pid, ppid, comm, cmd, env in t:
        if not any(k in cmd for k in ("vllm", "VLLM", "spawn_main")):
            continue
        e = "\001".join("%s=%s" % kv for kv in env.items())
        out.append("\t".join(("proc", pid, ppid, "0", str(START_TICKS), comm, "yes", cmd, e, pid)))
    for f, s in SUMS.items():
        out.append("\t".join(("file", CONF, f, s)))
    return out


def run_check_container(ports, names, broken_child=False):
    clock, procs, files = D.parse_inside(inside_lines(tree(ports, broken_child)))
    mounts = [{"Source": CONF, "Destination": CONF, "Type": "bind"}, {"Source": LOG, "Destination": LOG, "Type": "bind"}]
    rows = [["inspect", CNAME, "running|2026-11-21T00:00:00|0|"], ["mounts", CNAME, json.dumps(mounts)],
            ["logdriver", CNAME, "json-file"], ["logtail", CNAME, "5"]]
    facts = {"per_container": {CNAME: {"clock": clock, "procs": procs, "files": files, "start": START}},
             "filter_files": {}, "role_files": {p: {"phase": int(START) + 100} for p in ports}, "stdout_file": ""}
    c = types.SimpleNamespace(node_sums=SUMS, known_filter={}, conf_in=lambda: CONF)
    r = D.Report("t64")
    D.check_container(c, r, "10.0.0.2", CNAME, rows, CONF, LOG, CONF, LOG, names, {"facts": facts})
    return r.lines, procs


def lines_of(lines, level):
    return [t for lv, t in lines if lv == level]


class TestDiag(unittest.TestCase):
    PORTS = ["9000", "9001"]
    NAMES = [("D0", "9000"), ("D1", "9001")]

    def test_to_procs_leaves_out_processes_above_serve(self):
        clock, raw, _ = D.parse_inside(inside_lines(tree(self.PORTS)))
        self.assertTrue(ABOVE_PIDS <= {p["pid"] for p in raw}, "the launch-chain processes are collected")
        procs = D.to_procs("10.0.0.2", self.NAMES, clock, raw)
        self.assertFalse(ABOVE_PIDS & {str(p.pid) for p in procs})
        self.assertEqual({p.instance for p in procs}, {"D0", "D1"})
        self.assertEqual(sorted((p.instance, p.is_child) for p in procs),
                         [("D0", False), ("D0", True), ("D0", True), ("D1", False), ("D1", True), ("D1", True)])

    def test_check_container_no_fail_from_above_serve(self):
        lines, _ = run_check_container(self.PORTS, self.NAMES)
        self.assertEqual(lines_of(lines, "FAIL"), [], lines)
        info = [t for t in lines_of(lines, "INFO") if "往上找不到 vLLM 主进程" in t]
        self.assertEqual(len(info), 1, lines)
        self.assertIn("3 个进程", info[0])
        self.assertTrue(any("主进程 2 个，子进程 4 个" in t for t in lines_of(lines, "INFO")), lines)
        self.assertTrue(any("主进程和 4 个子进程都有 VLLM_LOGGING_CONFIG_PATH" in t for t in lines_of(lines, "OK")), lines)
        self.assertTrue(any("结论：日志配置生效" in t for t in lines_of(lines, "OK")), lines)

    def test_real_child_without_variable_still_fails(self):
        lines, _ = run_check_container(self.PORTS, self.NAMES, broken_child=True)
        fails = lines_of(lines, "FAIL")
        self.assertTrue(any("1 个子进程没有" in t and "pid 99" in t for t in fails), lines)
        self.assertTrue(any("结论：子进程没有继承变量：1 个子进程" in t for t in fails), lines)


class TestSafety(unittest.TestCase):
    HOSTS = {"10.0.0.1": [("P0", "8000")], "10.0.0.2": [("D0", "9000"), ("D1", "9001")]}

    def setUp(self):
        self.saved = S.probe_vllm, S.ch

    def tearDown(self):
        S.probe_vllm, S.ch = self.saved

    def states(self):
        def probe(c, addr):
            names = self.HOSTS[addr]
            clock, raw, _ = D.parse_inside(inside_lines(tree([p for _, p in names])))
            return D.to_procs(addr, names, clock, raw), {"docker": "docker", "containers": [CNAME], "found": []}
        # every real instance has lines since its start (pid 0 = "a line of this instance")
        S.probe_vllm = probe
        S.ch = lambda c, sql: ([{"instance": n, "pid": 0, "n": 3} for ns in self.HOSTS.values() for n, _ in ns], None)
        c = types.SimpleNamespace(instances=self.HOSTS)
        o = S.Out()
        return S.vllm_states(c, o, {}), o

    def test_no_empty_instance_and_no_extra_fail(self):
        st, o = self.states()
        self.assertEqual(st, {"P0": "records", "D0": "records", "D1": "records"})
        self.assertEqual([t for lv, t in o.lines if lv == "FAIL"], [])
        for item in ("过滤器", "版本"):
            self.assertEqual(C.judge_process_states(st, item), [])

    def test_real_child_still_belongs_to_its_instance(self):
        # the broken child is a process of D0 (it is still judged with D0, not dropped)
        def probe_one():
            names = self.HOSTS["10.0.0.2"]
            clock, raw, _ = D.parse_inside(inside_lines(tree([p for _, p in names], True)))
            return D.to_procs("10.0.0.2", names, clock, raw)
        p99 = [p for p in probe_one() if p.pid == 99]
        self.assertEqual([(p.instance, p.is_child) for p in p99], [("D0", True)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
