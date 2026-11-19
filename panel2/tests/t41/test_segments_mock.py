#!/usr/bin/env python3
"""T41 (pack two v3): unit tests for segments.json and the mock. Standard library only, no containers.

    python3 -I tests/t41/test_segments_mock.py            (from the repository root)

segments.json: the segment tree is the user's list, unchanged from v2.1; exactly the eight segments of the v3 plan have
a source, the nine it leaves empty have none; the roles keep only TP rank 0 of the pool records.
Mock: a short simulated sample (P0 and D0) through the pack's own node filter; the raw stream has the record kinds and
shapes of the diagnostics patches, and the filter writes exactly what segments.json reads.
"""
import collections
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
PACK = os.path.join(REPO, "vllm-pd-grafana-panel2")
sys.path.insert(0, os.path.join(PACK, "scripts"))
from segtree import Tree  # noqa: E402

# Every segment of v2.1 (side, phase, path): v3 adds none and removes none.
TREE = {
    ("prefill", "proxy"): ["api_gateway", "lb_proxy"],
    ("prefill", "ttft"): ["req_resolve", "tokenizer", "add_request", "schedule", "schedule/queue_time", "schedule/hash",
                          "schedule/lookup", "schedule/alloc", "schedule/other", "wait_for_put", "start_load_kv",
                          "start_load_kv/submit", "start_load_kv/wait_for_transfer", "forward", "sampling", "detokenizer"],
    ("prefill", "background"): ["wait_for_save", "wait_for_save/build_key", "wait_for_save/lookup", "wait_for_save/prepare",
                                "wait_for_save/sync", "wait_for_save/put", "wait_for_save/put/submit",
                                "wait_for_save/put/wait_for_transfer", "wait_for_save/put/finalize"],
    ("decode", "proxy"): ["lb_proxy"],
    ("decode", "ttft"): ["req_resolve", "tokenizer", "add_request", "schedule", "schedule/queue_time", "schedule/alloc",
                         "schedule/other", "start_load_kv", "start_load_kv/submit", "start_load_kv/wait_for_transfer",
                         "forward", "sampling", "detokenizer"],
    ("decode", "tpot"): ["schedule", "schedule/alloc", "schedule/other", "forward", "sampling", "detokenizer"],
}
SAVE = {"scope": "task", "operation": "save"}
FILLED = {   # the eight segments v3 fills: (side, phase, path) -> (event, where, field)
    ("prefill", "ttft", "start_load_kv/submit"): ("pool_task", {"scope": "task", "operation": "load_submit",
                                                                "status": "submission_returned"}, "duration_ns"),
    ("prefill", "background", "wait_for_save"): ("pool_task", SAVE, "duration_ns"),
    ("prefill", "background", "wait_for_save/build_key"): ("pool_task", SAVE, "stages.build_key.duration_ns"),
    ("prefill", "background", "wait_for_save/lookup"): ("pool_task", SAVE, "stages.lookup.duration_ns"),
    ("prefill", "background", "wait_for_save/prepare"): ("pool_task", SAVE, "stages.prepare.duration_ns"),
    ("prefill", "background", "wait_for_save/sync"): ("pool_task", SAVE, "stages.sync.duration_ns"),
    ("prefill", "background", "wait_for_save/put"): ("pool_task", SAVE, "stages.put.duration_ns"),
    ("decode", "ttft", "start_load_kv/wait_for_transfer"): ("pd_transfer_summary", {"scope": "request_generation"},
                                                            "transfer_envelope_ns"),
}
LEFT_EMPTY = [   # the nine segments v3 leaves empty
    ("prefill", "proxy", "api_gateway"), ("prefill", "proxy", "lb_proxy"), ("decode", "proxy", "lb_proxy"),
    ("prefill", "ttft", "wait_for_put"), ("prefill", "ttft", "start_load_kv/wait_for_transfer"),
    ("prefill", "background", "wait_for_save/put/submit"), ("prefill", "background", "wait_for_save/put/wait_for_transfer"),
    ("prefill", "background", "wait_for_save/put/finalize"), ("decode", "ttft", "start_load_kv/submit"),
]
PARENTS = [("prefill", "ttft", "schedule"), ("decode", "ttft", "schedule"), ("decode", "tpot", "schedule"),
           ("prefill", "ttft", "start_load_kv"), ("decode", "ttft", "start_load_kv")]


def nodes(raw):
    """(side, phase, path) -> raw node, for every node of the tree."""
    out = {}

    def walk(n, side, phase, pre):
        path = pre + n["id"]
        out[(side, phase, path)] = n
        for c in n.get("children", []):
            walk(c, side, phase, path + "/")
    for side, s in raw["sides"].items():
        for phase in ("proxy", "ttft", "tpot", "background"):
            for n in s.get(phase, []):
                walk(n, side, phase, "")
    return out


class SegmentsJson(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(PACK, "segments.json"), encoding="utf-8") as f:
            cls.raw = json.load(f)
        cls.nodes = nodes(cls.raw)

    def test_tree_is_the_users_list(self):
        got = collections.defaultdict(list)
        for side, phase, path in self.nodes:
            got[(side, phase)].append(path)
        self.assertEqual({k: sorted(v) for k, v in got.items()}, {k: sorted(v) for k, v in TREE.items()})

    def test_eight_filled(self):
        for key, (event, where, field) in FILLED.items():
            src = self.nodes[key].get("src")
            self.assertEqual(src, [{"event": event, "where": where, "field": field}], key)

    def test_nine_left_empty(self):
        for key in LEFT_EMPTY:
            self.assertIsNone(self.nodes[key].get("src"), key)

    def test_parents_follow_children(self):
        for key in PARENTS:
            self.assertNotIn("src", {k for k, v in self.nodes[key].items() if v is not None}, key)

    def test_only_these_reads_pool_and_transfer(self):
        """No other segment reads the new record kinds, and no where-less rule lets every pool_task through."""
        for key, n in self.nodes.items():
            for s in n.get("src") or []:
                if s.get("event") in ("pool_task", "pd_transfer_summary"):
                    self.assertIn(key, FILLED)
                    self.assertIn("scope", s["where"])
                    if s["event"] == "pool_task":
                        self.assertIn("operation", s["where"])
        self.assertNotIn("pool_task", [p if isinstance(p, str) else p["event"] for p in self.raw["passthrough"]])

    def test_roles_and_config(self):
        roles = self.raw["roles"]
        self.assertIn({"prefix": "pool_task", "role": "pool"}, roles["by_event_prefix"])
        self.assertEqual(roles["ranked_roles"], ["worker", "pool"])
        self.assertEqual(roles["keep_tp_rank"], [0])
        self.assertEqual(self.raw["config_id"], "pd-diag-v3-2026-11-16")
        self.assertEqual(self.raw["line"]["record_version"], 2)

    def test_segtree_loads(self):
        tree = Tree()
        self.assertEqual(len(tree.nodes("prefill", "ttft", "background")), 25)
        self.assertEqual(len(tree.nodes("decode", "ttft", "tpot")), 19)


class Mock(unittest.TestCase):
    """A short sample: the raw stream (what vLLM would print) and the files the pack's filter writes from it."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="t41-")
        node = os.path.join(cls.tmp, "node")
        os.makedirs(node)
        for f in ("node/pd_diag_logfilter.py", "node/pd_diag_logging.json", "segments.json"):
            shutil.copy(os.path.join(PACK, f), node)
        inv = os.path.join(PACK, "samples", "pd032_sample_inventory.json")
        mock = os.path.join(PACK, "mock", "pd032_mock.py")
        common = ["--inventory", inv, "--seconds", "30", "--rate", "6", "--start-at", "1794790000"]
        subprocess.run([sys.executable, "-I", mock, "sample", "--instances", "P0,D0", "--raw-dir", os.path.join(cls.tmp, "raw"),
                        "--truth", os.path.join(cls.tmp, "truth.jsonl")] + common, check=True, stdout=subprocess.DEVNULL)
        for inst in ("P0", "D0"):
            subprocess.run([sys.executable, "-I", mock, "sample", "--instances", inst, "--node-dir", node,
                            "--filtered-dir", os.path.join(cls.tmp, "f"), "--truth", os.path.join(cls.tmp, inst + ".truth.jsonl")]
                           + common, check=True, stdout=subprocess.DEVNULL)
        cls.raw = collections.defaultdict(list)
        for name in sorted(os.listdir(os.path.join(cls.tmp, "raw"))):
            inst = name.split(".")[0]
            with open(os.path.join(cls.tmp, "raw", name), encoding="utf-8") as f:
                for line in f:
                    cls.raw[inst].append(json.loads(line[line.index("[PD_DIAG] ") + 10:]))
        cls.filtered = collections.defaultdict(list)
        for inst in ("P0", "D0"):
            d = os.path.join(cls.tmp, "f", inst)
            for name in sorted(os.listdir(d)):
                with open(os.path.join(d, name), encoding="utf-8") as f:
                    for line in f:
                        i = line.find("[PD_DIAG] ")
                        if i >= 0:
                            cls.filtered[inst].append((name.split(".")[0], json.loads(line[i + 10:])))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def kinds(self, inst):
        return collections.Counter((o["event"], o.get("scope") or o.get("phase"), o.get("operation"), o.get("status")
                                    if o["event"] == "pool_task" else None) for o in self.raw[inst])

    def test_raw_kinds(self):
        p, d = self.kinds("P0"), self.kinds("D0")
        for k in (("pool_task", "task", "save", "succeeded"), ("pool_task", "task", "save", "all_hit"),
                  ("pool_task", "task", "save_submit", "submission_returned"), ("pool_task", "task", "save_submit", "no_task"),
                  ("pool_task", "task", "load_submit", "submission_returned"), ("pool_task", "task", "load_submit", "missing_load_spec"),
                  ("pool_task", "task", "load", "succeeded"), ("pd_transfer_summary", "link", None, None)):
            self.assertGreater(p[k], 0, k)
        for k in (("pd_transfer_summary", "task", None, None), ("pd_transfer_summary", "request_generation", None, None),
                  ("pd_transfer_summary", "link", None, None)):
            self.assertGreater(d[k], 0, k)
        self.assertFalse([k for k in d if k[0] == "pool_task"], "D has no pool")

    def test_patch_shapes(self):
        for inst in ("P0", "D0"):
            for o in self.raw[inst]:
                st = o.get("stages") or {}
                if o["event"] == "worker_summary" and o["phase"] == "execute":
                    self.assertNotIn("mode", st.get("forward", {}))
                    self.assertEqual(list(st)[-2:], ["submit", "wait_for_save"])
                if o["event"] == "core_summary" and o["scope"] == "request":
                    self.assertEqual(st["queue"]["timing_scope"], "enqueue_to_first_candidate")
                    self.assertEqual(st["queue"]["ranges"][0]["transition"], "candidate")
                    if st["hash"]["dropped_ranges"] == 0:
                        self.assertEqual(st["hash"]["duration_ns"], sum(r["duration_ns"] for r in st["hash"]["ranges"]))
                    self.assertLessEqual(len(st["hash"]["ranges"]), 64)
                    self.assertEqual(st["hash"]["calls"], len(st["hash"]["ranges"]) + st["hash"]["dropped_ranges"])
                    self.assertIsNone(st["hash"]["ranges"][0]["execution_id"])
                if o["event"] == "core_summary" and o["scope"] == "execution" and st["lookup"]["status"] == "ok":
                    self.assertEqual(st["lookup"]["calls"] % 2, 0)
                if o["event"] == "api_summary" and o["scope"] == "engine_submission":
                    self.assertEqual(st["add_request"]["timing_scope"], "tokenizer_end_to_zmq_send")

    def test_candidate_steps_name_no_request(self):
        """The step that starts a request's KV load (pool hit on P, every request on D) does not run it."""
        for inst in ("P0", "D0"):
            execs = {o["execution_id"]: o for o in self.raw[inst] if o["event"] == "core_summary" and o["scope"] == "execution"}
            n = 0
            for o in self.raw[inst]:
                if o["event"] == "core_summary" and o["scope"] == "request":
                    cand = o["stages"]["queue"]["ranges"][0]["execution_id"]
                    if o["request_id"] not in execs[cand]["request_ids"]:
                        n += 1
            self.assertGreater(n, 0, inst)

    def test_shared_save_names_no_request(self):
        shared = [o for o in self.raw["P0"] if o["event"] == "pool_task" and o["operation"] == "save" and not o["request_id"]]
        self.assertTrue(shared)
        self.assertTrue(all(len(o["request_ids"]) == 2 for o in shared))

    def test_filter_writes_what_segments_read(self):
        """Of the pool and transfer records the filter writes only the ones a segment reads, pool records of rank 0 only,
        into the pool role's own files."""
        for inst in ("P0", "D0"):
            for role, o in self.filtered[inst]:
                if o["event"] == "pool_task":
                    self.assertEqual(role, "pool")
                    self.assertEqual(o["tp_rank"], 0)
                    self.assertIn((o["operation"], o["status"]), {("save", "succeeded"), ("save", "all_hit"),
                                                                  ("load_submit", "submission_returned")})
                if o["event"] == "pd_transfer_summary":
                    self.assertEqual(o["scope"], "request_generation")
        p = collections.Counter(o["event"] for _, o in self.filtered["P0"])
        d = collections.Counter(o["event"] for _, o in self.filtered["D0"])
        raw_p = sum(1 for o in self.raw["P0"] if o["event"] == "pool_task" and o["tp_rank"] == 0 and
                    (o["operation"] == "save" or (o["operation"], o["status"]) == ("load_submit", "submission_returned")))
        self.assertEqual(p["pool_task"], raw_p)
        self.assertEqual(d["pd_transfer_summary"], sum(1 for o in self.raw["D0"] if o["event"] == "pd_transfer_summary"
                                                       and o["scope"] == "request_generation"))

    def test_truth_has_the_new_segments(self):
        with open(os.path.join(self.tmp, "truth.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(x) for x in f]
        p = [r for r in rows if r["instance"] == "P0" and "events" in r]
        d = [r for r in rows if r["instance"] == "D0" and "events" in r]
        self.assertTrue(any("save" in r["events"] for r in p))
        self.assertTrue(any("load_submit" in r["events"] for r in p))
        self.assertTrue(any(r["events"].get("save_put") is None and "save" in r["events"] for r in p), "an all-hit save")
        self.assertTrue(all("wait_for_transfer" in r["events"] for r in d if "api_total" in r["events"]
                            and r["events"].get("executions")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
