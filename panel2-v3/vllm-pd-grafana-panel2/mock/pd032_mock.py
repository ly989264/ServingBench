#!/usr/bin/env python3
"""Stands in for vLLM on intranet machines without a model: writes the [PD_DIAG] stream (version 2) of every instance
on this machine, at the current time, through the same logging config, module and segments.json the monitoring machine
sends to the nodes. What lands on disk is therefore exactly what the real filter writes (request-level records and
per-request sums); the per-step records are generated in memory and summed by the filter, as in vLLM.

The records are the kinds the diagnostics patches (vLLM 01, Ascend 02) write: api_summary (scope http,
engine_submission, request), core_summary (scope request, execution), worker_summary (phase execute, sample), pool_task
(P's KV pool: save_submit every step, the background save, load_submit and, on a pool hit, the background load) and
pd_transfer_summary (D's KV read from P: task, request_generation; link on both sides), with the same fields and stages.
As in the patches, the queue ends at the first candidate step, and a request that waits for KV (a pool hit on P, every
request on D) is not in the request_ids of that step: its lookup and alloc are charged to the requests the step runs.
Nothing else is written: what the log does not record (proxy, Mooncake's native put phases) takes time here as it would
in vLLM, without a record.

Request i is determined by (seed, i) alone, so every machine agrees on every request without talking to the others,
and each machine only writes the lines of its own instances. P runs every request on its own; D batches the requests
it runs, so B (requests per decode step) follows from the load.

Commands
  run          one process per instance of --host, until stopped (used by bin/node_mock.sh)
  run-instance one instance (started by run)
  sample       simulated time, as fast as possible: the full raw stream in vLLM's default text format (--raw-dir),
               the true per-request sums (--truth), and optionally the filtered files (--filtered-dir)

All values are synthetic. Standard library only; Python 3.9 or later.
"""
import argparse
import hashlib
import heapq
import json
import logging
import logging.config
import os
import random
import signal
import socket
import subprocess
import sys
import time

LOGGER = "vllm.v1.metrics.pd_diagnostics"
CHUNK = 8192            # prefill tokens per step
P_RANKS_DEFAULT = 2     # TP ranks written for P (rank 0 is kept by the filter, the others are dropped)
OUT_NS = 0.1e6          # EngineCore's output handling after a step (not recorded)


def digest(*parts):
    return hashlib.blake2b(":".join(str(x) for x in parts).encode(), digest_size=16).hexdigest()


def rng(*parts):
    return random.Random(int(digest(*parts)[:16], 16))


def as_uuid(h):
    return "%s-%s-4%s-a%s-%s" % (h[:8], h[8:12], h[13:16], h[17:20], h[20:32])


# ---- inventory ---------------------------------------------------------------------------------------------------

def load_inventory(path):
    with open(path, encoding="utf-8") as f:
        inv = json.load(f)
    p, d = list(inv["prefill"]["api_servers"]), list(inv["decode"]["api_servers"])
    if not p or not d:
        raise SystemExit("inventory: prefill.api_servers and decode.api_servers must not be empty")
    return {"p": p, "d": d, "model": inv.get("model", "glm-5.2"),
            "p_host": [a.rsplit(":", 1)[0] for a in p], "d_host": [a.rsplit(":", 1)[0] for a in d],
            "p_ranks": int(inv["prefill"].get("procs_per_instance", 1)),
            "d_ranks": int(inv["decode"].get("procs_per_instance", 1))}


def port_of(inv, name):
    """The API server port of instance P<k> or D<k>, from its address in the inventory."""
    side = inv["p"] if name.startswith("P") else inv["d"]
    return side[int(name[1:])].rsplit(":", 1)[1]


def instances_of(inv, host):
    out = ["P%d" % k for k, h in enumerate(inv["p_host"]) if h == host]
    out += ["D%d" % k for k, h in enumerate(inv["d_host"]) if h == host]
    return out


class Model:
    """Everything about request i that both sides need, derived only from (seed, i)."""

    def __init__(self, inv, seed, rate):
        self.inv, self.seed, self.rate = inv, seed, rate
        self.slots = max(50, int(rate * 3))

    def t0(self, i):
        return (i + rng(self.seed, "t", i).random()) / self.rate

    def route(self, i):
        r = rng(self.seed, "route", i)
        slot = i % self.slots
        s = rng(self.seed, "home", slot)
        home = s.randrange(len(self.inv["p"]))
        p = home if r.random() < 0.85 else r.randrange(len(self.inv["p"]))
        return p, r.randrange(len(self.inv["d"]))

    def wave(self, t):
        return 1.0 + 0.5 * (1 + ((t % 3600) / 1800.0 - 1) ** 2)

    def request(self, i):
        """The part both P and D must agree on: ids, tokens, P's own durations, D's arrival and outcome."""
        seed = self.seed
        r = rng(seed, "req", i)
        L = lambda mu, s=0.5: r.lognormvariate(mu, s)
        t0 = self.t0(i)
        p, d = self.route(i)
        x = as_uuid(digest(seed, "x", i))
        slot = i % self.slots
        n = i // self.slots
        s = rng(seed, "sess", slot, n // 20)
        system = int(s.uniform(4000, 12000))
        turn = n % 20
        prompt = int(system + s.uniform(600, 3200) * turn + r.uniform(50, 800))
        hit = int(system * r.uniform(0.7, 1.0)) if turn == 0 else int(max(0, prompt - 3600) * r.uniform(0.92, 1.0))
        output = 1 + int(L(5.0, 1.0))
        kind = r.random()
        # About 3 in 100: a completions request with two prompts (one HTTP request, two engine requests); never an abort
        # or an error, so that both prompts end the same way.
        prompts = 2 if 0.95 <= kind < 0.98 else 1
        wave = self.wave(t0)
        # P side, independent of other requests: every request is its own batch on P.
        computed = max(1, prompt - hit)
        chunks = max(1, -(-computed // CHUNK))
        pre = dict(resolve=L(0.0) * 1e6, render=L(0.3) * 1e6, tokenize=max(0.2, prompt / 1000.0 * L(0.1, 0.25)) * 1e6,
                   submit=L(-1.0) * 1e6)
        p_queue = L(1.0, 1.0) * wave * 1e6
        p_hash = prompt / 1000.0 * 0.05 * L(0, 0.2) * 1e6
        p_lookup = L(0.5) * 1e6
        steps = []
        left = computed
        for k in range(chunks):
            tok = min(CHUNK, left)
            left -= tok
            steps.append(dict(tokens=tok, forward=(4 + tok / 1000.0 * L(3.3, 0.3) * wave) * 1e6, sampling=L(0.5) * 1e6,
                              alloc=L(-1.0) * 1e6, other=L(0.0) * 1e6))
        p_detok = L(-2.0) * 1e6
        p_abort = kind < 0.002
        p2p, p2d = L(-0.5, 0.4) * 1e6, L(-0.6, 0.4) * 1e6
        dd = dict(transfer=prompt / 1000.0 * L(0.4, 0.3) * 1e6, queue=L(0.5, 1.0) * wave * 1e6,
                  hash=prompt / 1000.0 * 0.05 * L(0, 0.2) * 1e6,
                  pre=dict(resolve=L(0.0) * 1e6, render=L(0.0) * 1e6, tokenize=L(-1.0) * 1e6, submit=L(-1.0) * 1e6),
                  abort_after=(int(output * r.uniform(0.05, 0.7)) if 0.002 <= kind < 0.006 else None),
                  error=0.006 <= kind < 0.008)
        # The KV pool and the KV read, from their own stream so that the values above stay what they were.
        k = rng(seed, "kv", i)
        K = lambda mu, s=0.3: k.lognormvariate(mu, s)
        # A pool hit loads the request's KV before it runs; only single-prompt requests (a completions request with two
        # prompts shares one save task instead, see run_prefill).
        pool_hit = k.random() < 0.3 and prompts == 1
        pool = dict(hit=pool_hit, all_hit=pool_hit and k.random() < 0.5,
                    cand=dict(lookup=(K(-0.6) * 1e6, K(-1.5) * 1e6), alloc=K(-2.8) * 1e6, other=K(-1.2) * 1e6),
                    load_submit=K(-2.6) * 1e6, load=dict(build_key=K(-3.7) * 1e6, prepare=K(-1.6) * 1e6,
                                                         get=prompt / 1000.0 * K(1.0) * 1e6),
                    load_wait=K(1.5) * 1e6,
                    save=dict(build_key=K(-3.4) * 1e6, lookup=K(-1.4) * 1e6, prepare=K(-1.6) * 1e6, sync=K(-3.6) * 1e6,
                              put=max(1, computed) / 1000.0 * K(2.0) * 1e6),
                    save_submit=K(-1.6) * 1e6, gap=K(-4.0) * 1e6)
        dd["lookup"] = (K(-3.7) * 1e6, K(-4.2) * 1e6)
        dd["alloc"] = K(-2.8) * 1e6
        dd["meta"], dd["prepare"], dd["xq"] = K(-2.0) * 1e6, K(0.9) * 1e6, K(-0.7) * 1e6
        dd["poll"] = K(0.5) * 1e6
        return dict(i=i, t0=t0, p=p, d=d, di=dd, x=x, prompts=prompts, prompt=prompt, hit=hit, output=output, computed=computed, pre=pre,
                    p_queue=p_queue, p_hash=p_hash, p_lookup=p_lookup, steps=steps, p_detok=p_detok, p_abort=p_abort,
                    p2p=p2p, p2d=p2d, pool=pool)


# ---- emitting records --------------------------------------------------------------------------------------------

def stage(mono_start, dur_ns, **extra):
    """A stage that ran once: what vLLM writes under "stages"."""
    d = int(dur_ns)
    st = {"calls": 1, "duration_ns": d, "start_ns": mono_start, "end_ns": mono_start + d, "status": "ok", "exceptions": 0,
          "measured_calls": 1, "skipped_calls": 0, "unmeasured_calls": 0}
    st.update(extra)
    return st


def skipped():
    """A stage that did not run in this step (lookup after a request's first step, alloc in an empty step)."""
    return {"calls": 0, "duration_ns": None, "start_ns": None, "end_ns": None, "status": "skipped", "exceptions": 0,
            "measured_calls": 0, "skipped_calls": 0, "unmeasured_calls": 0}


def unmeasured():
    """A pool stage the task did not run (a load has no put; an all-hit save has no prepare, sync or put)."""
    return {"calls": 0, "status": "unmeasured", "exceptions": 0}


def calls_stage(mono_start, durs, gap_ns=0, **extra):
    """A stage of several calls (lookup: local and external; build_key, prepare: one per block group): duration_ns is
    the sum of the calls, start_ns and end_ns the envelope, as the patch writes them."""
    durs = [int(d) for d in durs]
    end = mono_start + sum(durs) + int(gap_ns) * (len(durs) - 1)
    st = {"calls": len(durs), "duration_ns": sum(durs), "start_ns": mono_start, "end_ns": end, "status": "ok", "exceptions": 0,
          "measured_calls": len(durs), "skipped_calls": 0, "unmeasured_calls": 0}
    st.update(extra)
    return st, end


class Emitter:
    """Builds records the way vLLM's diagnostics do and sends them through the logger (and the raw file)."""

    def __init__(self, instance, seed, raw_dir=None, skew_s=0.0, host_name=None, bad_rate=0.0, run_salt=""):
        self.instance, self.seed, self.skew_s = instance, seed, skew_s
        # vLLM gives every process a new run_id when it starts; the live mock does the same (run_salt), so observation
        # and execution IDs do not repeat after a restart. The samples leave it empty and stay byte for byte the same.
        self.run_salt = run_salt
        self.bad_rate, self.bad = bad_rate, random.Random(seed ^ 0x5BAD)
        self.bad_lines = 0
        self.host = host_name or socket.gethostname()
        self.log = logging.getLogger(LOGGER)
        self.counter = 0
        # Monotonic clock of the simulated process: wall time plus a fixed offset, positive like a real uptime counter.
        self.mono_off = 10 ** 15 + int(digest(seed, "mono", instance)[:6], 16) * 10 ** 6 - 1_700_000_000 * 10 ** 9
        self.procs = {}
        self.raw_dir = raw_dir
        self.raw = {}
        self.current = None
        self.lines = 0
        self.out = []          # (wall, n, name, pid, payload): written when the wall clock reaches them
        self.after = {}        # n -> what to do once line n is written (the truth counts only lines that were written)
        base = 20000 + int(digest(seed, "pid", instance)[:4], 16) % 20000
        self.base = base
        factory = logging.getLogRecordFactory()
        em = self

        def make(*a, **k):
            rec = factory(*a, **k)
            if em.current is not None:
                name, pid, wall = em.current
                rec.vllm_process_name = name
                rec.process = pid
                rec.created = wall + em.skew_s
                rec.msecs = (rec.created - int(rec.created)) * 1000
                rec.filename, rec.lineno = "pd_diagnostics.py", 156
            return rec
        logging.setLogRecordFactory(make)

    def proc(self, name):
        p = self.procs.get(name)
        if p is None:
            pid = self.base + len(self.procs) * 7 + 1
            run = digest(self.seed, "run", self.instance, name, self.run_salt) if self.run_salt else digest(self.seed, "run", self.instance, name)
            p = self.procs[name] = (pid, run[:32])
        return p

    def mono(self, wall_s):
        return int(wall_s * 1e9) + self.mono_off

    def emit(self, name, wall_s, event, fields):
        pid, run = self.proc(name)
        self.counter += 1
        v = {"observation_id": None, "complete": True, "dropped_observations": 0, "links": [], "status": "ok", "stages": {}}
        v.update(fields)
        # As in vLLM: the common fields after the record's own, "event" last.
        v.update(version=2, host=self.host, pid=pid, native_tid=pid, run_id=run, timestamp_ns=self.mono(wall_s),
                 record_id="record-%s-%d-%d" % (run, pid, self.counter))
        v["event"] = event
        payload = json.dumps(v, separators=(",", ":"))
        heapq.heappush(self.out, (wall_s, self.counter, name, pid, payload))

    def drain(self, until, stopped=None):
        """Writes the lines due by `until`. With stopped, returns early once it is true (see Instance.run_until)."""
        while self.out and self.out[0][0] <= until:
            wall_s, n, name, pid, payload = heapq.heappop(self.out)
            self._write(name, pid, wall_s, payload)
            for fn in self.after.pop(n, ()):
                fn()
            if stopped is not None and stopped():
                return

    def on_write(self, fn):
        """Run fn when the line emitted last is written."""
        self.after.setdefault(self.counter, []).append(fn)

    def _write(self, name, pid, wall_s, payload):
        if self.bad_rate and self.bad.random() < self.bad_rate:
            # A damaged line (test): cut in the middle, the way a torn write would leave it.
            self.bad_lines += 1
            self.current = (name, pid, wall_s)
            try:
                self.log.info("[PD_DIAG] %s", payload[: len(payload) // 2])
            finally:
                self.current = None
        self.current = (name, pid, wall_s)
        try:
            self.log.info("[PD_DIAG] %s", payload)
        finally:
            self.current = None
        self.lines += 1
        if self.raw_dir is not None:
            f = self.raw.get(name)
            if f is None:
                os.makedirs(self.raw_dir, exist_ok=True)
                f = self.raw[name] = open(os.path.join(self.raw_dir, "%s.%s.log" % (self.instance, name)), "a", encoding="utf-8")
            f.write("(%s pid=%d) INFO %s [pd_diagnostics.py:156] [PD_DIAG] %s\n" % (
                name, pid, time.strftime("%m-%d %H:%M:%S", time.localtime(wall_s + self.skew_s)), payload))

    def close(self):
        self.drain(float("inf"))
        for f in self.raw.values():
            f.close()


class Truth:
    """What the filter and the collector must produce, per (instance, request): the sums of the accumulated segments
    (rank 0 only; "first" is the request's first step) and the values read from request-level records."""

    def __init__(self):
        self.rows = {}
        self.events = {}
        self.first = {}

    def add_event(self, instance, rid, seg, value):
        self.events.setdefault((instance, rid), {})[seg] = value

    def add(self, instance, rid, seg, exe, ns):
        first = self.first.setdefault((instance, rid), exe)
        row = self.rows.setdefault((instance, rid), {})
        a = row.setdefault(seg, {"ns": 0, "steps": 0, "first_ns": 0, "_last": None})
        if exe != a["_last"]:
            a["steps"] += 1
            a["_last"] = exe
        a["ns"] += int(ns)
        if exe == first:
            a["first_ns"] += int(ns)

    def dump(self, path):
        with open(path, "w", encoding="utf-8") as f:
            for key in sorted(set(self.rows) | set(self.events)):
                inst, rid = key
                segs = self.rows.get(key, {})
                clean = {k: {"ns": v["ns"], "steps": v["steps"], "first_ns": v["first_ns"]} for k, v in segs.items()}
                row = {"instance": inst, "request_id": rid, "segs": clean}
                if key in self.events:
                    row["events"] = self.events[key]
                f.write(json.dumps(row, separators=(",", ":")) + "\n")


# ---- one instance ------------------------------------------------------------------------------------------------

def p_plan(model, req, pname, p_ranks):
    """P's timeline of request req, in ns after it reaches P's API server. run_prefill places its records with it and D
    computes it too (p_end), so that a request reaches D after it has left P. A pool hit adds the first candidate step
    (lookup, alloc, the load submission) and the wait for the background load before the request's own steps."""
    pre = req["pre"]
    pool = req["pool"]
    plan = {"resolve": pre["resolve"], "render": pre["render"], "tokenize": pre["tokenize"], "submit": pre["submit"],
            "submits": int(pre["submit"]) * req["prompts"], "hash": req["p_hash"], "queue": req["p_queue"], "steps": []}
    jit = 1.0 + 0.01 * (p_ranks - 1)            # the slowest rank decides when a step ends
    last = len(req["steps"]) - 1
    for k, st in enumerate(req["steps"]):
        r_out = rng(model.seed, "pout", pname, req["i"], k)
        out = OUT_NS * r_out.uniform(0.6, 1.4)
        # Worker per step, after forward: start_load_kv (submit) and the connector's wait_for_save, which on the last
        # step submits the request's save to the pool.
        submit = 8000 * r_out.uniform(0.9, 1.9)
        wfs = 200000 * r_out.uniform(0.9, 1.2) + (pool["save_submit"] if k == last else 0)
        lookup = None
        if k == 0 and not pool["hit"]:
            lookup = (int(req["p_lookup"] * 0.7), int(req["p_lookup"] * 0.3))      # local and external lookup
        plan["steps"].append(dict(lookup=lookup, alloc=st["alloc"], other=st["other"], forward=st["forward"],
                                  sampling=st["sampling"], submit=submit, wfs=wfs, tokens=st["tokens"],
                                  worker=(int(st["forward"] * jit) + int(submit * jit) + int(wfs * jit) + int(st["sampling"] * jit)),
                                  out=out))
    if pool["hit"]:
        r_c = rng(model.seed, "pcand", pname, req["i"])
        c, ld = pool["cand"], pool["load"]
        submit = pool["load_submit"] + 0.6e6 * r_c.uniform(0.8, 1.2)
        wfs = 200000 * r_c.uniform(0.9, 1.2)
        task = int(ld["build_key"]) + int(ld["prepare"]) + int(ld["get"]) + 3 * int(pool["gap"])
        plan["cand"] = dict(lookup=(int(c["lookup"][0]), int(c["lookup"][1])), alloc=c["alloc"], other=c["other"],
                            submit=submit, wfs=wfs, worker=int(submit * jit) + int(wfs * jit),
                            out=OUT_NS * r_c.uniform(0.6, 1.4), task=task, wait=task + int(pool["load_wait"]))
    plan["detok"] = req["p_detok"]
    return plan


class Instance:
    def __init__(self, inv, name, model, em, truth=None, p_ranks=P_RANKS_DEFAULT, step_events="full"):
        self.inv, self.name, self.model, self.em, self.truth = inv, name, model, em, truth
        self.side = "prefill" if name[0] == "P" else "decode"
        self.k = int(name[1:])
        self.p_ranks = max(1, min(p_ranks, inv["p_ranks"]))
        self.p_ranks_all = self.p_ranks   # P's rank count, also known on D
        # full: also the empty steps vLLM runs after a request finishes (no request in them); core: only steps with requests.
        self.full = step_events == "full"
        dp = self.k
        self.api = "APIServer"
        self.core = "EngineCore_DP%d" % dp
        if self.side == "prefill":
            # P: multiprocess executor, one Worker process per rank.
            self.workers = ["Worker_DP%d_TP%d" % (dp, r) for r in range(self.p_ranks)]
        else:
            # D: one process per instance, so the worker runs inside EngineCore (uniproc executor).
            self.workers = [self.core]
        self.engine_id = "engine-%s-%s" % (name, digest(model.seed, "eng", name)[:8])
        self.exec_n = 0
        self.obs_n = 0
        self.sub_n = 0
        self.heap = []
        self.tie = 0
        # D state
        self.waiting = []      # (ready_time, i, k, u): KV received, runs from ready_time
        self.cands = []        # (candidate_time, i, k, u): reaches its first candidate step at candidate_time
        self.task_n = 0
        self.running = []
        self.next_step = None

    # -- helpers --
    def push(self, at, fn, *args):
        self.tie += 1
        heapq.heappush(self.heap, (at, self.tie, fn, args))

    def exe(self):
        self.exec_n += 1
        return "execution-%s-%d" % (self.engine_id, self.exec_n)

    def acc(self, rid, seg, exe, ns, rank):
        # Called right after the record it counts; added to the truth when that record's line is written, so a stop
        # with lines still waiting for their time leaves them out of both the files and the truth.
        if self.truth is not None and rank == 0 and ns is not None:
            self.em.on_write(lambda: self.truth.add(self.name, rid, seg, exe, ns))

    def ev(self, rid, values):
        if self.truth is not None:
            self.em.on_write(lambda: [self.truth.add_event(self.name, rid, k, v) for k, v in values.items()])

    def idents(self, req, salt):
        """One HTTP request (one observation); one engine request per prompt. Chat: chatcmpl-<x>; completions with two
        prompts: cmpl-<x>-0 and cmpl-<x>-1, the prompt index at the end as vLLM writes it."""
        self.obs_n += 1
        run = self.em.proc(self.api)[1]
        obs = "api-%s-%d" % (run, self.obs_n)
        out = []
        for k in range(req["prompts"]):
            external = "chatcmpl-" + req["x"] if req["prompts"] == 1 else "cmpl-%s-%d" % (req["x"], k)
            tail = digest(self.model.seed, salt, req["i"]) if req["prompts"] == 1 else digest(self.model.seed, salt, req["i"], k)
            self.sub_n += 1
            out.append(dict(external=external, internal="%s-%s" % (external, tail[:8]), obs=obs,
                            sub="api_submission-%s-%d" % (run, self.sub_n), k=k))
        return out

    def scope(self, e, ids):
        """The fields every per-step record of EngineCore and the worker carries."""
        return dict(request_id=None, observation_id=None, external_req_id=None, api_request_id=None, parent_request_id=None,
                    prompt_index=None, batch_shared=True, execution_id=e, request_ids=ids, engine_id=self.engine_id,
                    dp_rank=self.k, kv_role="kv_producer" if self.side == "prefill" else "kv_consumer", no_forward=not ids)

    def wscope(self, e, ids, rank):
        return dict(self.scope(e, ids), tp_rank=rank, pp_rank=0, timing_domain="host_monotonic",
                    runner="vllm_ascend.worker.v2.model_runner.NPUModelRunner")

    def core_exec(self, t, e, ids, members, s0, lookup, alloc, other_ns, tokens_each):
        """core_summary[execution] of one scheduler step, written when the step's schedule() returns at t."""
        em = self.em
        other = {"calls": 1, "duration_ns": int(other_ns), "start_ns": None, "end_ns": None, "status": "ok",
                 "exceptions": 0, "measured_calls": 1, "skipped_calls": 0, "unmeasured_calls": 0,
                 "source": "schedule_minus_nonoverlapping_manager_and_external_lookup_ranges", "timing_scope": "schedule_residual"}
        em.emit(self.core, t, "core_summary", dict(self.scope(e, ids), scope="execution", role="unknown", members=members,
                                                   num_scheduled_tokens={x: tokens_each for x in ids},
                                                   total_num_scheduled_tokens=tokens_each * len(ids),
                                                   finished_request_ids=[], preempted_request_ids=[],
                                                   stages={"lookup": lookup, "alloc": alloc,
                                                           "schedule": stage(em.mono(s0), int((t - s0) * 1e9)), "other": other}))

    def worker_execute(self, W, e, ids, rank, w0, forward_ns, submit_ns, wfs_ns):
        """worker_summary[execute]: forward (absent in a step without forward), then start_load_kv (submit) and the
        connector's wait_for_save, in that order as the patch measures them. Returns the time the record is written."""
        em = self.em
        st = {}
        t = w0
        if forward_ns is not None:
            st["forward"] = stage(em.mono(t), forward_ns, completion="host_return")
            t += forward_ns / 1e9
        st["submit"] = stage(em.mono(t), submit_ns, completion="submission_return_not_transfer_completion")
        t += submit_ns / 1e9
        st["wait_for_save"] = stage(em.mono(t), wfs_ns, deferred=False)
        t += wfs_ns / 1e9
        em.emit(W, t, "worker_summary", dict(self.wscope(e, ids, rank), phase="execute", stages=st,
                                             links=[{"kind": "kv.completion_observed", "finished_sending": [],
                                                     "finished_recving": [], "failed_recving": []}]))
        return t

    def pool_task(self, W, wall, rank, e, op, status, rid=None, rids=(), start=None, end=None, queue_enter=None,
                  stages=None, assoc=None, counters=None):
        """pool_task[task] of the Ascend pool, written when the task (or submission) completes at wall. rid: the task's
        own request; rids without rid: work shared by several requests (request_id stays null, as the patch writes it)."""
        em = self.em
        self.task_n += 1
        st = {s: unmeasured() for s in ("prepare", "lookup", "sync", "put", "build_key")}
        st.update(stages or {})
        names = [rid] if rid else list(rids)
        f = dict(self.wscope(e, list(rids), rank), request_id=rid, remote_request_id=None, remote_engine_id=None,
                 pcp_rank=0, dcp_rank=0, task_id="pool_task-%s-%d-%d" % (self.engine_id, rank, self.task_n), operation=op,
                 scope="task", start_ns=None if start is None else em.mono(start), end_ns=None if end is None else em.mono(end),
                 duration_ns=None if start is None else em.mono(end) - em.mono(start),
                 queue_enter_ns=None if queue_enter is None else em.mono(queue_enter), status=status, incomplete=False,
                 complete=True, member_count=len(names), members=[{"request_id": x, "execution_id": e} for x in names],
                 members_limit=128, members_dropped=0, native_calls=[], native_calls_limit=64, native_call_count=0,
                 native_calls_dropped=0, stages=st, associated_stages=assoc or {}, counters=counters or {},
                 timing_scope="task_processing_envelope")
        em.emit(W, wall, "pool_task", f)

    def transfer_link(self, t, e, rid, producer_rid, role):
        """pd_transfer_summary[link]: the P/D association only, no timing (the filter does not read it)."""
        self.em.emit(self.core, t, "pd_transfer_summary", dict(self.scope(e, [rid] if role == "producer" else []), request_id=rid,
                                                               scope="link", transfer_role=role, complete=True, incomplete=False,
                                                               links=[{"kind": "producer", "request_id": producer_rid}],
                                                               status="offered" if role == "producer" else "linked"))

    # -- records of either side --
    def api_front(self, t, pre, idents):
        """The API server's front part: resolve, render, tokenize (written later in api_summary[http]), then one
        add_request per prompt (api_summary[engine_submission], written when it returns). As in the patch, add_request
        runs from the tokenizer's return to the ZMQ send."""
        em, A = self.em, self.api
        st = {}
        for name, key in (("req_resolve", "resolve"), ("render", "render"), ("tokenize", "tokenize")):
            st[name] = stage(em.mono(t), pre[key])
            t += int(pre[key]) / 1e9
        for ident in idents:
            sub = stage(em.mono(t), pre["submit"], source="AsyncMPClient.input_socket.send_multipart",
                        timing_scope="tokenizer_end_to_zmq_send", aggregation="union_per_input_boundary",
                        boundary_status="ok", send_completed=True, reason=None, error_type=None)
            t += int(pre["submit"]) / 1e9
            em.emit(A, t, "api_summary", dict(scope="engine_submission", observation_id=ident["obs"],
                                              external_req_id=ident["external"], submission_id=ident["sub"],
                                              status="accepted", official_status="pending", completion="submission_returned",
                                              request_completion="pending", error_type=None, stages={"add_request": sub},
                                              links=[{"kind": "frontend", "observation_id": ident["obs"], "submission_id": None},
                                                     {"kind": "request_id", "request_id": ident["internal"],
                                                      "external_req_id": ident["external"]},
                                                     {"kind": "engine_request", "request_id": ident["internal"]},
                                                     {"kind": "add_request_send", "request_id": ident["internal"],
                                                      "boundary_source": "tokenizer_return", "status": "ok",
                                                      "duration_ns": int(pre["submit"]), "timing_scope": "tokenizer_end_to_zmq_send",
                                                      "shared_preparation": False}]))
            self.ev(ident["internal"], {"add_request": int(pre["submit"])})
        return t, st

    def core_request(self, t, ident, queue_start, queue_end, cand_e, hashes, executions, decode_steps, preempt, status, last_e):
        """core_summary[request]: the queue up to the first candidate step (cand_e) and every block hash call
        (hashes: [(execution_id or None, start wall, ns)], the first one when EngineCore takes the request in)."""
        em, C = self.em, self.core
        queue_ns = int((queue_end - queue_start) * 1e9)
        q = stage(em.mono(queue_start), queue_ns, initial_wait_ns=queue_ns, dropped_ranges=0, complete=True,
                  timing_scope="enqueue_to_first_candidate", end_observed=True,
                  ranges=[{"execution_id": cand_e, "episode": 1, "start_ns": em.mono(queue_start),
                           "end_ns": em.mono(queue_start) + queue_ns, "duration_ns": queue_ns, "status": "ok",
                           "transition": "candidate"}])
        ranges = [{"execution_id": x, "start_ns": em.mono(s), "end_ns": em.mono(s) + int(ns), "duration_ns": int(ns),
                   "status": "ok", "error_type": None} for x, s, ns in hashes]
        hash_ns = sum(int(ns) for _, _, ns in hashes)
        # At most 64 ranges are kept (the first ones); duration_ns and calls still count every call.
        h = {"calls": len(ranges), "duration_ns": hash_ns, "start_ns": ranges[0]["start_ns"], "end_ns": ranges[-1]["end_ns"],
             "status": "ok", "exceptions": 0, "measured_calls": len(ranges), "skipped_calls": 0, "unmeasured_calls": 0,
             "failed_calls": 0, "blocks": 0, "dropped_ranges": max(0, len(ranges) - 64), "complete": len(ranges) <= 64,
             "ranges": ranges[:64], "timing_scope": "associated_request_block_hasher_calls"}
        member = {"request_id": ident["internal"], "status": status, "observation_id": ident["obs"], "engine_id": self.engine_id,
                  "executions": executions, "preemptions": preempt, "decode_steps": decode_steps}
        em.emit(C, t, "core_summary", dict(scope="request", request_id=ident["internal"], observation_id=ident["obs"],
                                           external_req_id=ident["external"], submission_id=ident["sub"], engine_id=self.engine_id,
                                           dp_rank=self.k, kv_role=self.scope(None, [])["kv_role"], batch_shared=False,
                                           no_forward=False, complete=False, timing_scope="associated_request_lifetime",
                                           completion=status, last_execution_id=last_e, execution_id=None,
                                           request_ids=[ident["internal"]], members=[member], stages={"queue": q, "hash": h}))
        self.ev(ident["internal"], {"queue_time": queue_ns, "hash": hash_ns, "executions": executions})

    def api_request_end(self, t, ident, official, status, detok):
        """api_summary[request] of one engine request. detok: [(execution_id, ns)], one call per step that produced a
        token. At most 64 ranges are kept, as vLLM does (dropped_ranges counts the rest); which end is cut is not
        known, so the mock cuts the head of every other request and the tail of the others."""
        em, A = self.em, self.api
        total = sum(ns for _, ns in detok)
        calls = len(detok)
        kept = detok
        if calls > 64:
            kept = detok[-64:] if int(digest(self.model.seed, "cut", ident["internal"])[:2], 16) % 2 else detok[:64]
        ranges = [{"start_ns": None, "end_ns": None, "duration_ns": ns, "execution_id": e, "status": "ok", "error_type": None}
                  for e, ns in kept]
        dk = {"calls": calls, "duration_ns": total, "start_ns": None, "end_ns": None, "status": "ok", "exceptions": 0,
              "measured_calls": calls, "skipped_calls": 0, "unmeasured_calls": 0, "input_tokens": calls,
              "dropped_ranges": calls - len(kept), "complete": calls == len(kept), "ranges": ranges}
        off = dict(request_id=ident["external"], source="stats.IterationStats.update_from_finished_request", unit="seconds",
                   **official)
        em.emit(A, t, "api_summary", dict(scope="request", request_id=ident["internal"], observation_id=ident["obs"],
                                          external_req_id=ident["external"], parent_request_id=None, choice_index=0,
                                          submission_id=ident["sub"], status=official["status"],
                                          official_status="complete" if status == "completed" else status,
                                          completion="request_state_terminal", official=off,
                                          terminal={"status": official["status"], "error_type": None,
                                                    "detokenize_duration_ns": total, "detokenize_calls": calls,
                                                    "detokenize_input_tokens": calls, "source": "request_state"},
                                          stages={"detokenize": dk},
                                          links=[{"kind": "frontend", "observation_id": ident["obs"], "submission_id": ident["sub"]}]))
        values = {"detokenizer": total}
        if self.side == "decode" and detok:
            first = dict(kept).get(detok[0][0])
            values["detokenizer_first"] = first
            values["detokenizer_rest"] = None if first is None else total - first
        self.ev(ident["internal"], values)

    def api_http_end(self, t_start, t, idents, front, status, http):
        """api_summary[http]: written once the HTTP response is done, after every engine request of it."""
        em, A = self.em, self.api
        links = [{"kind": "api_request", "api_request_id": idents[0]["external"] if len(idents) == 1 else idents[0]["external"][:-2],
                  "external_req_id": ident["external"], "prompt_index": ident["k"]} for ident in idents]
        links += [{"kind": "engine_submission", "submission_id": ident["sub"], "external_req_id": ident["external"]} for ident in idents]
        em.emit(A, t, "api_summary", dict(scope="http", observation_id=idents[0]["obs"], start_ns=em.mono(t_start), end_ns=em.mono(t),
                                          http_status=http, status=status, method="POST",
                                          route="/v1/chat/completions" if len(idents) == 1 else "/v1/completions",
                                          stream=True, response_complete=status == "completed",
                                          disconnect_observed=status == "disconnected", completion="http_terminal",
                                          stages=front, links=links))
        values = {"api_total": em.mono(t) - em.mono(t_start),
                  "req_resolve": front["req_resolve"]["duration_ns"] + front["render"]["duration_ns"],
                  "tokenizer": front["tokenize"]["duration_ns"]}
        for ident in idents:
            self.ev(ident["internal"], values)

    # -- P --
    def run_prefill(self, req):
        em, C = self.em, self.core
        tp0 = req["t0"] + req["p2p"] / 1e9
        idents = self.idents(req, "pi")
        ids = [x["internal"] for x in idents]
        pool = req["pool"]
        plan = p_plan(self.model, req, self.name, self.p_ranks)
        t, front = self.api_front(tp0, req["pre"], idents)
        hashes = [(None, t, plan["hash"])]    # block hashes are computed when EngineCore takes the request in
        t += int(plan["hash"]) / 1e9
        q0 = t
        t += int(plan["queue"]) / 1e9
        q_end = t
        cand_e = None
        c = plan.get("cand")
        if c:
            # Pool hit: the first candidate step looks the request up, allocates its blocks and submits the load from
            # the pool, but runs nothing, so the request is not in the step's request_ids. Its lookup, alloc and other
            # belong to no request the filter can name (the truth leaves them out, as the filter does).
            cand_e = self.exe()
            s0 = t
            lookup, _ = calls_stage(em.mono(t), c["lookup"])
            t += sum(c["lookup"]) / 1e9
            alloc = stage(em.mono(t), c["alloc"])
            t += int(c["alloc"]) / 1e9
            t += int(c["other"]) / 1e9
            self.core_exec(t, cand_e, [], [], s0, lookup, alloc, c["other"], 0)
            ld = pool["load"]
            for rank, W in enumerate(self.workers):
                j = 1.0 + 0.01 * rank
                sb, wf = int(c["submit"] * j), int(c["wfs"] * j)
                self.worker_execute(W, cand_e, [], rank, t, None, sb, wf)
                ls0 = t + 0.4 * sb / 1e9
                ls_ns = int(pool["load_submit"] * j)
                self.pool_task(W, ls0 + ls_ns / 1e9, rank, cand_e, "load_submit", "submission_returned", rid=ids[0],
                               rids=[], start=ls0, end=ls0 + ls_ns / 1e9,
                               assoc={"processing": stage(em.mono(ls0), int(ls_ns * 0.7))})
                if rank == 0:
                    self.ev(ids[0], {"load_submit": em.mono(ls0 + ls_ns / 1e9) - em.mono(ls0)})
                self.pool_task(W, t + (sb + wf * 0.5) / 1e9, rank, cand_e, "save_submit", "no_task", start=t + sb / 1e9,
                               end=t + (sb + wf * 0.5) / 1e9, stages={s: skipped() for s in ("prepare", "lookup", "sync", "put", "build_key")})
                # The background load: build_key, prepare, then the backend get.
                g = int(pool["gap"])
                l0 = ls0 + ls_ns / 1e9 + g / 1e9
                bk, _ = calls_stage(em.mono(l0), [int(ld["build_key"] * j / 5)] * 5)
                p0 = l0 + (int(ld["build_key"] * j / 5) * 5 + g) / 1e9
                pr, _ = calls_stage(em.mono(p0), [int(ld["prepare"] * j / 4)] * 4)
                b0 = p0 + (int(ld["prepare"] * j / 4) * 4 + g) / 1e9
                gn = int(ld["get"] * j)
                l1 = b0 + gn / 1e9
                self.pool_task(W, l1, rank, cand_e, "load", "succeeded", rid=ids[0], start=l0, end=l1, queue_enter=l0 - g / 1e9,
                               stages={"build_key": bk, "prepare": pr},
                               assoc={"backend_get": stage(em.mono(b0), gn), "processing": stage(em.mono(l0), int((l1 - l0) * 1e9))},
                               counters={"get_calls": 1, "get_submitted_keys": 4})
            t += (c["worker"] + int(c["out"])) / 1e9
            t += int(c["wait"]) / 1e9         # the load runs in the background; the request is scheduled once it is done
        e = first_e = None
        last = len(plan["steps"]) - 1
        for k, st in enumerate(plan["steps"]):
            # The prompts of one HTTP request are prefilled together: every step names all of them, and each of them is
            # charged the whole step.
            e = self.exe()
            first_e = first_e or e
            s0 = t
            if st["lookup"] is not None:
                lookup, _ = calls_stage(em.mono(t), st["lookup"])
                t += sum(st["lookup"]) / 1e9
            else:
                lookup = skipped()
            alloc = stage(em.mono(t), st["alloc"])
            t += int(st["alloc"]) / 1e9
            t += int(st["other"]) / 1e9
            members = [{"request_id": x["internal"], "observation_id": x["obs"], "engine_id": self.engine_id,
                        "scheduled_tokens": st["tokens"], "num_prompt_tokens": req["prompt"],
                        "is_prefill_chunk": k < last, "execution_participation": k + 1} for x in idents]
            self.core_exec(t, e, ids, members, s0, lookup, alloc, st["other"], st["tokens"])
            if k == 0:
                for x in ids:
                    self.transfer_link(t, e, x, x, "producer")
            for x in ids:
                self.acc(x, "lookup", e, st["lookup"] and sum(st["lookup"]), 0)
                self.acc(x, "alloc", e, int(st["alloc"]), 0)
                self.acc(x, "schedule_other", e, int(st["other"]), 0)
            for rank, W in enumerate(self.workers):
                j = 1.0 + 0.01 * rank
                fw, sp = int(st["forward"] * j), int(st["sampling"] * j)
                sb, wf = int(st["submit"] * j), int(st["wfs"] * j)
                w_end = self.worker_execute(W, e, ids, rank, t, fw, sb, wf)
                em.emit(W, w_end + sp / 1e9, "worker_summary", dict(self.wscope(e, ids, rank), phase="sample", stages={
                    "sampling": stage(em.mono(w_end), sp, algorithm="sampler", completion="host_return")}))
                for x in ids:
                    self.acc(x, "forward", e, fw, rank)
                    self.acc(x, "sampling", e, sp, rank)
                sub0 = t + fw / 1e9
                if k == 0 and not pool["hit"]:
                    # A request that missed the pool: the load submission finds no load spec.
                    for x in ids:
                        self.pool_task(W, sub0 + sb * 0.6 / 1e9, rank, e, "load_submit", "missing_load_spec", rid=x, rids=[x],
                                       start=sub0 + sb * 0.2 / 1e9, end=sub0 + sb * 0.6 / 1e9)
                ws0 = sub0 + sb / 1e9
                if k == last:
                    self.save(W, rank, e, req, idents, ws0, wf)
                else:
                    self.pool_task(W, ws0 + 0.1 * wf / 1e9, rank, e, "save_submit", "no_task", start=ws0, end=ws0 + 0.1 * wf / 1e9,
                                   stages={s: skipped() for s in ("prepare", "lookup", "sync", "put", "build_key")})
            t += (st["worker"] + int(st["out"])) / 1e9
            # update_from_output appends the new token's block hashes, once per request per step.
            hashes.append((e, t - int(st["out"]) / 1e9 + 1e-6, 3000 * rng(self.model.seed, "phash", req["i"], k).uniform(0.8, 1.2)))
        for ident in idents:
            self.core_request(t, ident, q0, q_end, cand_e or first_e, hashes, len(plan["steps"]), 0, 0,
                              "FINISHED_LENGTH_CAPPED", e)
        end = t + int(plan["detok"]) / 1e9
        queued = plan["queue"] / 1e9 + ((sum(c["lookup"]) + int(c["alloc"]) + int(c["other"]) + c["worker"] + int(c["out"])
                                         + int(c["wait"])) / 1e9 if c else 0.0)
        official = dict(status="length", time_to_first_token=end - tp0, e2e_latency=end - tp0, queued_time=queued,
                        prefill_time=(end - tp0) - queued, inference_time=end - tp0, decode_time=0.0,
                        mean_time_per_output_token=0.0, num_prompt_tokens=req["prompt"], num_generation_tokens=1,
                        num_preemptions=0, num_cached_tokens=req["hit"], first_token_observed=True)
        for ident in idents:
            # P's one token: one detokenize call, in the last step.
            self.api_request_end(end, ident, official, "completed", [(e, int(plan["detok"]))])
        self.api_http_end(tp0, end, idents, front, "completed", 200)

    def save(self, W, rank, e, req, idents, ws0, wf):
        """The last step's wait_for_save submits the request's save; the save itself runs in the background. A
        completions request with two prompts shares one save task: request_id stays null and request_ids names both,
        and the filter charges it to neither (shared work is not charged to every member)."""
        pool = req["pool"]
        sv = pool["save"]
        j = 1.0 + 0.01 * rank
        g = int(pool["gap"])
        ids = [x["internal"] for x in idents]
        shared = len(ids) > 1
        rid = None if shared else ids[0]
        ss_ns = int(pool["save_submit"] * j)
        ss0 = ws0 + 0.2 * (wf - ss_ns) / 1e9
        self.pool_task(W, ss0 + ss_ns / 1e9, rank, e, "save_submit", "submission_returned", rid=rid, rids=ids,
                       start=ss0, end=ss0 + ss_ns / 1e9, assoc={"queue_submission": stage(self.em.mono(ss0), int(ss_ns * 0.35))})
        enter = ss0 + ss_ns * 0.8 / 1e9
        s0 = enter + 200e-6 * j
        t = s0
        stg = {}
        bk, end = calls_stage(self.em.mono(t), [int(sv["build_key"] * j / 5)] * 5, g)
        stg["build_key"] = bk
        t = (end - self.em.mono(0)) / 1e9 + g / 1e9
        lk = stage(self.em.mono(t), int(sv["lookup"] * j))
        stg["lookup"] = lk
        t += int(sv["lookup"] * j) / 1e9 + g / 1e9
        status = "all_hit" if pool["all_hit"] else "succeeded"
        if not pool["all_hit"]:
            pr, end = calls_stage(self.em.mono(t), [int(sv["prepare"] * j / 4)] * 4, g)
            stg["prepare"] = pr
            t = (end - self.em.mono(0)) / 1e9 + g / 1e9
            stg["sync"] = stage(self.em.mono(t), int(sv["sync"] * j))
            t += int(sv["sync"] * j) / 1e9 + g / 1e9
            pn = int(sv["put"] * j)
            stg["put"] = stage(self.em.mono(t), pn)
            t += pn / 1e9
        t += g / 1e9
        self.pool_task(W, t, rank, e, "save", status, rid=rid, rids=[] if not shared else ids, start=s0, end=t, queue_enter=enter,
                       stages=stg, assoc={"processing": stage(self.em.mono(s0), int((t - s0) * 1e9) - g)})
        if rank == 0 and not shared:
            v = {"save": self.em.mono(t) - self.em.mono(s0)}
            for s in ("build_key", "lookup", "prepare", "sync", "put"):
                v["save_" + s] = stg[s]["duration_ns"] if s in stg else None
            self.ev(rid, v)

    def p_end(self, req):
        """When P's API server finishes request req (D works it out the same way)."""
        plan = p_plan(self.model, req, "P%d" % req["p"], self.p_ranks_all)
        t = sum(int(plan[k]) for k in ("resolve", "render", "tokenize", "submits", "hash", "queue", "detok"))
        for st in plan["steps"]:
            t += sum(st["lookup"] or ()) + int(st["alloc"]) + int(st["other"]) + st["worker"] + int(st["out"])
        c = plan.get("cand")
        if c:
            t += sum(c["lookup"]) + int(c["alloc"]) + int(c["other"]) + c["worker"] + int(c["out"]) + int(c["wait"])
        return req["t0"] + (req["p2p"] + t) / 1e9

    # -- D --
    def d_arrive(self, req):
        dd = req["di"]
        idents = self.idents(req, "di")
        p_ids = self.p_internals(req)
        t0 = req["d_arrive"]
        t, front = self.api_front(t0, dd["pre"], idents)
        group = dict(idents=idents, front=front, start=t0, left=len(idents), end=t0)
        if dd["error"]:
            end = t + 0.002
            official = dict(status="error", time_to_first_token=0.0, e2e_latency=end - t0, queued_time=0.0, prefill_time=0.0,
                            inference_time=0.0, decode_time=0.0, mean_time_per_output_token=0.0, num_prompt_tokens=req["prompt"],
                            num_generation_tokens=0, num_preemptions=0, num_cached_tokens=req["prompt"], first_token_observed=False)
            for ident in idents:
                self.api_request_end(end, ident, official, "error", [])
            self.api_http_end(t0, end, idents, front, "error", 500)
            return
        # Block hashes when EngineCore takes the request in, then the queue up to the first candidate step. That step
        # submits the KV read from P; the request runs once the read is done (d_step).
        hash_start = t
        t += int(dd["hash"]) / 1e9
        q_start = t
        cand_at = q_start + int(dd["queue"]) / 1e9
        for n, ident in enumerate(idents):
            u = dict(req=req, ident=ident, group=group, steps_done=0, detok=[], first_tok=None, preempt=0, skip=0,
                     hashes=[(None, hash_start, dd["hash"])], q_start=q_start, cand_at=cand_at, prompt=req["prompt"],
                     output=req["output"], di=dd, p_rid=p_ids[n])
            heapq.heappush(self.cands, (cand_at, req["i"], ident["k"], u))
        self.wake(cand_at)

    def p_internals(self, req):
        """P's request ids of req (the producer side of the KV read), computed the way P's idents does."""
        out = []
        for k in range(req["prompts"]):
            external = "chatcmpl-" + req["x"] if req["prompts"] == 1 else "cmpl-%s-%d" % (req["x"], k)
            tail = digest(self.model.seed, "pi", req["i"]) if req["prompts"] == 1 else digest(self.model.seed, "pi", req["i"], k)
            out.append("%s-%s" % (external, tail[:8]))
        return out

    def wake(self, at):
        if self.next_step is None or (not self.running and at < self.next_step):
            self.next_step = at
            self.push(at, self.d_step, at)

    def d_read(self, u, e, sub0):
        """The KV read of a candidate request, submitted at sub0 inside the step's start_load_kv: the task record, then
        the request's generation closure (the read envelope panel 2 shows as wait_for_transfer) and the link."""
        em, dd = self.em, u["di"]
        rid = u["ident"]["internal"]
        queued = sub0
        submitted = queued + int(dd["xq"] * 0.55) / 1e9
        start = queued + int(dd["xq"]) / 1e9
        meta = stage(em.mono(start + 4e-6), int(dd["meta"]))
        prep0 = start + (int(dd["meta"]) + 140000) / 1e9
        prep = stage(em.mono(prep0), int(dd["prepare"]))
        w0 = prep0 + (int(dd["prepare"]) + 200000) / 1e9
        wn = int(dd["transfer"])
        w1 = w0 + wn / 1e9
        end = w1 + 300e-6
        nbytes = u["prompt"] * 32768
        task = dict(self.wscope(e, [], 0), request_id=rid, no_forward=True, scope="task", remote_request_id=u["p_rid"],
                    remote_engine_id="engine-P", connector_id="mooncake_hybrid", task_id="pd_transfer-%s-%s" % (self.engine_id, rid[-8:]),
                    direction="read", offset=0, request_generation="pd_request-%s-%s" % (self.engine_id, rid[-8:]),
                    start_ns=em.mono(start), queued_ns=em.mono(queued), submitted_ns=em.mono(submitted),
                    queue_wait_ns=em.mono(start) - em.mono(queued), final_task=True, error_type=None, end_ns=em.mono(end),
                    processing_duration_ns=em.mono(end) - em.mono(start), timing_scope="task_processing_envelope",
                    transfer_envelope_ns=wn, transfer_timing_scope="native_read_envelope_not_call_sum",
                    submitted_bytes=nbytes, succeeded_bytes=nbytes, failed_bytes=0, transfer_status="succeeded",
                    tracker_completed=True, native_call_count=1, native_calls_limit=64, native_calls_dropped=0,
                    native_calls=[{"operation": "batch_transfer_sync_read", "status": "succeeded", "start_ns": em.mono(w0),
                                   "end_ns": em.mono(w1), "duration_ns": wn, "submitted_bytes": nbytes}],
                    associated_stages={"meta_fetch": meta, "prepare": prep}, incomplete=False, complete=True,
                    stages={"wait_for_transfer": stage(em.mono(w0), wn, completion="native_read_return")},
                    links=[{"kind": "producer", "request_id": u["p_rid"]}], status="succeeded")
        em.emit(self.core, end, "pd_transfer_summary", task)
        gen = dict(request_id=rid, remote_request_id=None, remote_engine_id=None, engine_id=self.engine_id,
                   connector_id="mooncake_hybrid", task_id=None, execution_id=None, tp_rank=0, dp_rank=self.k, direction="read",
                   offset=None, request_generation=task["request_generation"], pending_tasks=0, final_task_queued=True,
                   submitted_tasks=1, succeeded_tasks=1, failed_tasks=0, skipped_tasks=0, submitted_bytes=nbytes,
                   succeeded_bytes=nbytes, failed_bytes=0, tracker_completed=True, native_calls_dropped=0, status="succeeded",
                   scope="request_generation", transfer_envelope_ns=wn,
                   timing_scope="native_read_envelope_not_task_sum_or_pure_wait", complete=True, incomplete=False)
        em.emit(self.core, end + 1e-6, "pd_transfer_summary", gen)
        self.ev(rid, {"wait_for_transfer": wn})
        self.transfer_link(end + 2e-6, e, rid, u["p_rid"], "consumer")
        return end

    def d_step(self, at=None):
        em, C = self.em, self.core
        now = self.next_step
        if at is not None and at != now:
            return   # a step scheduled earlier and superseded by an earlier arrival
        cands = []
        while self.cands and self.cands[0][0] <= now:
            cands.append(heapq.heappop(self.cands)[3])
        while self.waiting and self.waiting[0][0] <= now:
            u = heapq.heappop(self.waiting)[3]
            u["joined"] = now
            self.running.append(u)
        active = [u for u in self.running if u["skip"] == 0]
        for u in self.running:
            if u["skip"] > 0:
                u["skip"] -= 1
        if not active and not cands:
            if self.running:
                self.next_step = now + 0.025
            else:
                nxt = [q[0][0] for q in (self.waiting, self.cands) if q]
                self.next_step = min(nxt) if nxt else None
            if self.next_step is not None:
                self.push(self.next_step, self.d_step, self.next_step)
            return
        B = len(active)
        wave = self.model.wave(now)
        r0 = rng(self.model.seed, "dstep", self.name, self.exec_n)
        e = self.exe()
        ids = [u["ident"]["internal"] for u in active]
        # Candidates: their lookup (local and external) and the blocks for the KV they will receive are done in this
        # step, which does not run them, so these times go to the requests the step runs (and to none when it runs none).
        lk_calls = [int(x) for u in cands for x in u["di"]["lookup"]]
        lookup_ns = sum(lk_calls)
        alloc_ns = int(sum(0.02e6 * r0.uniform(0.5, 2.0) * (5 if u["steps_done"] == 0 else 1) for u in active))
        alloc_ns += int(sum(u["di"]["alloc"] for u in cands))
        other_ns = int((0.3 + 0.02 * B + 0.1 * len(cands)) * 1e6 * r0.uniform(0.8, 1.2))
        lookup = calls_stage(em.mono(now), lk_calls)[0] if lk_calls else skipped()
        t = now + (lookup_ns + alloc_ns + other_ns) / 1e9
        for u in cands:
            u["cand_e"] = e
            u["q_end"] = now + 30e-6       # picked by the scheduler right after the step starts
        members = [{"request_id": u["ident"]["internal"], "observation_id": u["ident"]["obs"], "engine_id": self.engine_id,
                    "scheduled_tokens": 1, "num_prompt_tokens": u["prompt"], "is_prefill_chunk": False,
                    "execution_participation": u["steps_done"] + 1} for u in active]
        self.core_exec(t, e, ids, members, now, lookup, stage(em.mono(now + lookup_ns / 1e9), alloc_ns) if alloc_ns else skipped(),
                       other_ns, 1)
        for x in ids:
            self.acc(x, "lookup", e, lookup_ns or None, 0)
            self.acc(x, "alloc", e, alloc_ns or None, 0)
            self.acc(x, "schedule_other", e, other_ns, 0)
        sb = int(11000 * r0.uniform(0.6, 1.4) + 80000 * len(cands))
        wf = int(1600 * r0.uniform(0.8, 1.5))
        fw = int((22 + 1.1 * B) * 1e6 * wave * r0.uniform(0.95, 1.05)) if active else None
        w_end = self.worker_execute(C, e, ids, 0, t, fw, sb, wf)
        sub0 = t + (fw or 0) / 1e9
        for n, u in enumerate(cands):
            done_at = self.d_read(u, e, sub0 + (n + 1) * 70e-6)
            heapq.heappush(self.waiting, (done_at + int(u["di"]["poll"]) / 1e9, u["req"]["i"], u["ident"]["k"], u))
        if not active:
            t = w_end + OUT_NS * r0.uniform(0.6, 1.4) / 1e9
            nxt = [q[0][0] for q in (self.waiting, self.cands) if q]
            self.next_step = max(t, min(nxt)) if nxt else None
            if self.next_step is not None:
                self.push(self.next_step, self.d_step, self.next_step)
            return
        sp = int((0.6 + 0.03 * B) * 1e6 * r0.uniform(0.9, 1.1))
        em.emit(C, w_end + sp / 1e9, "worker_summary", dict(self.wscope(e, ids, 0), phase="sample", stages={
            "sampling": stage(em.mono(w_end), sp, algorithm="sampler", completion="host_return")}))
        for x in ids:
            self.acc(x, "forward", e, fw, 0)
            self.acc(x, "sampling", e, sp, 0)
        t = w_end + (sp + OUT_NS * r0.uniform(0.6, 1.4)) / 1e9
        done = []
        for u in active:
            u["steps_done"] += 1
            u["detok"].append((e, int(0.05e6 * r0.uniform(0.5, 1.5))))
            u["hashes"].append((e, t - 50e-6, 2500 * r0.uniform(0.8, 1.2)))
            if u["first_tok"] is None:
                u["first_tok"] = t
            stop = u["di"]["abort_after"]
            if u["steps_done"] >= u["output"] or (stop is not None and u["steps_done"] >= max(1, stop)):
                done.append(u)
            elif B > 6 and r0.random() < 0.004:
                # Preempted for a few steps, then scheduled again.
                u["skip"] = r0.randint(2, 8)
                u["preempt"] += 1
        for u in done:
            self.running.remove(u)
            self.d_finish(u, t, e)
        if done and self.full:
            # vLLM runs one more step with no request after requests finish: no forward, empty request list.
            e2 = self.exe()
            self.core_exec(t + 47e-6, e2, [], [], t, skipped(), skipped(), 47000, 0)
            self.worker_execute(C, e2, [], 0, t + 47e-6, None, int(9000 * r0.uniform(0.8, 1.2)), int(1500 * r0.uniform(0.8, 1.2)))
        step_ms = (25 + 1.2 * B) * wave * r0.uniform(0.97, 1.03)
        self.next_step = max(t, now + step_ms / 1000.0)
        self.push(self.next_step, self.d_step, self.next_step)

    def d_finish(self, u, t, e):
        aborted = u["di"]["abort_after"] is not None
        ident, group = u["ident"], u["group"]
        n = u["steps_done"]
        self.core_request(t, ident, u["q_start"], u["q_end"], u["cand_e"], u["hashes"], n, n - 1, u["preempt"],
                          "FINISHED_ABORTED" if aborted else "FINISHED_STOPPED", e)
        end = t + 0.0005
        ttft = u["first_tok"] - group["start"]
        official = dict(status="abort" if aborted else "stop", time_to_first_token=ttft, e2e_latency=end - group["start"],
                        queued_time=u["joined"] - u["q_start"], prefill_time=u["first_tok"] - u["joined"],
                        inference_time=end - u["joined"], decode_time=end - u["first_tok"],
                        mean_time_per_output_token=((end - u["first_tok"]) / (n - 1)) if n > 1 else 0.0,
                        num_prompt_tokens=u["prompt"], num_generation_tokens=n, num_preemptions=u["preempt"],
                        num_cached_tokens=u["prompt"], first_token_observed=True)
        status = "disconnected" if aborted else "completed"
        self.api_request_end(end, ident, official, status, u["detok"])
        group["left"] -= 1
        group["end"] = max(group["end"], end)
        if group["left"] == 0:
            self.api_http_end(group["start"], group["end"], group["idents"], group["front"], status, 200)

    # -- scheduling the requests of this instance --
    def feed(self, i_from, i_to):
        m = self.model
        for i in range(i_from, i_to):
            p, d = m.route(i)
            if self.side == "prefill" and p != self.k:
                continue
            if self.side == "decode" and d != self.k:
                continue
            req = m.request(i)
            if self.side == "prefill":
                self.push(req["t0"] + req["p2p"] / 1e9, self.run_prefill, req)
            elif not req["p_abort"]:
                req["d_arrive"] = self.p_end(req) + req["p2d"] / 1e9
                self.push(req["d_arrive"], self.d_arrive, req)

    def run_until(self, t_end, stopped=None):
        """Runs the events up to t_end. With stopped, looks at it after every event and returns early when it is true, so
        that a stop takes effect within a second even when this instance has fallen far behind (T32 A2). Every event, not
        every 1000: behind, one decode step goes over thousands of running requests, and the look is one dict read."""
        while self.heap and self.heap[0][0] <= t_end:
            at, _, fn, args = heapq.heappop(self.heap)
            fn(*args)
            if stopped is not None and stopped():
                return


# ---- commands ------------------------------------------------------------------------------------------------------

def configure_logging(node_dir, instance=None, log_dir=None):
    # The live mock passes no instance: like vLLM started by a launch script, each rank process has the machine's
    # one set of variables and its own --port on the command line, from which the filter takes the instance.
    if instance:
        os.environ["PD_DIAG_INSTANCE"] = instance
    os.environ["PD_DIAG_MAPPING"] = os.path.join(node_dir, "segments.json")
    if log_dir:
        os.environ["PD_DIAG_LOG_DIR"] = log_dir
    sys.path.insert(0, node_dir)
    with open(os.path.join(node_dir, "pd_diag_logging.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    # The console handler would print vLLM's normal lines; the mock has none, so it goes to /dev/null.
    cfg["handlers"]["vllm"]["stream"] = open(os.devnull, "w")
    logging.config.dictConfig(cfg)


def cmd_run_instance(a):
    inv = load_inventory(a.inventory)
    configure_logging(a.node_dir, None, a.log_dir)
    model = Model(inv, a.seed, a.rate)
    em = Emitter(a.instance, a.seed, skew_s=a.clock_offset_s, bad_rate=a.bad_line_rate, run_salt=str(time.time_ns()))
    truth = Truth() if a.truth_file else None
    inst = Instance(inv, a.instance, model, em, truth=truth, p_ranks=a.p_ranks, step_events=a.step_events)
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("flag", True))
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("flag", True))
    start = time.time()
    i = int(start * a.rate)
    end = start + a.seconds if a.seconds else None
    last_stats = 0.0
    while not stop["flag"]:
        now = time.time()
        if end is not None and now >= end:
            break
        # Requests are fed 30 s ahead, so a D sees a request as soon as its P part would have finished.
        target = int((now + 30) * a.rate)
        if target > i:
            inst.feed(i, target)
            i = target
        inst.run_until(now, lambda: stop["flag"])
        em.drain(now, lambda: stop["flag"])
        if a.stats_file and now - last_stats >= 5:
            last_stats = now
            tmp = a.stats_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"instance": a.instance, "lines": em.lines, "running": len(inst.running), "time": now}, f)
            os.replace(tmp, a.stats_file)
        time.sleep(0.01)
    if not stop["flag"]:
        # --seconds ran out: the lines due by now. A stop drops what was not written yet (about 10 ms of lines when the
        # mock keeps up; when it has fallen behind, the backlog, which would otherwise keep it from exiting, T32 A2).
        em.drain(time.time())
    logging.shutdown()
    if truth is not None:
        # Test option: what the filter must have written for the steps emitted so far.
        truth.dump(a.truth_file)
    return 0


def cmd_run(a):
    inv = load_inventory(a.inventory)
    names = instances_of(inv, a.host)
    if not names:
        raise SystemExit("清单里没有地址为 %s 的 P 或 D 实例" % a.host)
    children = []
    for name in names:
        # One launch per machine and one process per DP rank, all with the same environment, each with its own --port
        # and the DP and TP arguments vllm serve gets in external load balancing (the inventory's order is the DP rank).
        side = inv["p"] if name.startswith("P") else inv["d"]
        tp = inv["p_ranks"] if name.startswith("P") else inv["d_ranks"]
        args = [sys.executable, os.path.abspath(__file__), "run-instance", "--inventory", a.inventory, "--instance", name,
                "--port", port_of(inv, name), "--data-parallel-size", str(len(side)), "--data-parallel-rank", name[1:],
                "--tensor-parallel-size", str(tp), "--node-dir", a.node_dir, "--rate", str(a.rate), "--seed", str(a.seed), "--p-ranks", str(a.p_ranks),
                "--step-events", a.step_events, "--clock-offset-s", str(a.clock_offset_s), "--seconds", str(a.seconds)]
        if a.log_dir:
            args += ["--log-dir", a.log_dir]
        if a.stats_dir:
            os.makedirs(a.stats_dir, exist_ok=True)
            args += ["--stats-file", os.path.join(a.stats_dir, name + ".json")]
        if a.truth_dir:
            os.makedirs(a.truth_dir, exist_ok=True)
            args += ["--truth-file", os.path.join(a.truth_dir, name + ".jsonl")]
        children.append(subprocess.Popen(args))
    print("mock：%s 上 %d 个实例（%s），全集群 %.0f 请求/秒" % (a.host, len(names), " ".join(names), a.rate), flush=True)

    def stop(*_):
        for c in children:
            if c.poll() is None:
                c.terminate()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    rc = 0
    for c in children:
        rc = max(rc, c.wait())
    return rc


def cmd_sample(a):
    """Simulated time: the raw stream for the reference script, the truth, and (with --filtered-dir) the filter output."""
    inv = load_inventory(a.inventory)
    names = a.instances.split(",")
    truth = Truth()
    if a.filtered_dir:
        if len(names) != 1:
            raise SystemExit("--filtered-dir works with one instance per run (the instance name is per process)")
        configure_logging(a.node_dir, names[0], a.filtered_dir)
    else:
        logging.getLogger(LOGGER).addHandler(logging.NullHandler())
        logging.getLogger(LOGGER).propagate = False
        logging.getLogger(LOGGER).setLevel(logging.INFO)
    model = Model(inv, a.seed, a.rate)
    start = a.start_at or time.time()
    i0 = int(start * a.rate)
    i1 = int((start + a.seconds) * a.rate)
    handler = None
    for h in logging.getLogger("vllm").handlers:
        if type(h).__name__ == "PdDiagHandler":
            handler = h
    for name in names:
        em = Emitter(name, a.seed, raw_dir=a.raw_dir, host_name="mock-" + name, bad_rate=a.bad_line_rate)
        inst = Instance(inv, name, model, em, truth=truth, p_ranks=a.p_ranks, step_events=a.step_events)
        inst.feed(i0, i1)
        t = start
        while inst.heap or em.out:
            t += a.flush_every
            inst.run_until(t)
            em.drain(t)
            if handler is not None:
                # Simulated time runs faster than the filter's own flush timer; flush on the simulated clock instead.
                with handler._mu:
                    handler._next_flush = 0
                    handler._tick()
        em.close()
        if em.bad_lines:
            print(json.dumps({"instance": name, "bad_lines": em.bad_lines}))
    if a.truth:
        truth.dump(a.truth)
    logging.shutdown()
    print(json.dumps({"instances": names, "requests_window": i1 - i0, "truth_rows": len(truth.rows)}))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--inventory", required=True)
        p.add_argument("--rate", type=float, default=100.0, help="requests per second across the whole cluster")
        p.add_argument("--seed", type=int, default=20261111, help="must be the same on every machine")
        p.add_argument("--p-ranks", type=int, default=P_RANKS_DEFAULT, help="TP ranks written on P (only rank 0 is kept)")
        p.add_argument("--step-events", choices=("full", "core"), default="full",
                       help="full: also the empty steps vLLM runs after requests finish; core: only steps with requests")
        p.add_argument("--node-dir", help="directory with the three node files (pd_diag_logging.json, pd_diag_logfilter.py, segments.json)")
        p.add_argument("--log-dir", help="overrides line.log_dir of segments.json")
        p.add_argument("--bad-line-rate", type=float, default=0.0, help="share of lines additionally written cut in half (test)")

    p = sub.add_parser("run")
    common(p)
    p.add_argument("--host", required=True)
    p.add_argument("--seconds", type=float, default=0)
    p.add_argument("--clock-offset-s", type=float, default=0.0, help="shift this machine's wall clock, as a wrong clock would")
    p.add_argument("--stats-dir")
    p.add_argument("--truth-dir", help="test: write each instance's true per-request sums here when it stops")
    p = sub.add_parser("run-instance")
    common(p)
    p.add_argument("--instance", required=True)
    p.add_argument("--port", required=True, help="this rank's API server port, as vllm serve gets it; the filter takes the instance from it")
    p.add_argument("--data-parallel-size", type=int)
    p.add_argument("--data-parallel-rank", type=int, help="the filter writes it into its self-check lines")
    p.add_argument("--tensor-parallel-size", type=int)
    p.add_argument("--seconds", type=float, default=0)
    p.add_argument("--clock-offset-s", type=float, default=0.0)
    p.add_argument("--stats-file")
    p.add_argument("--truth-file")
    p = sub.add_parser("sample")
    common(p)
    p.add_argument("--instances", required=True, help="for example P0,D0")
    p.add_argument("--seconds", type=float, default=20)
    p.add_argument("--start-at", type=float, default=0)
    p.add_argument("--raw-dir")
    p.add_argument("--truth")
    p.add_argument("--filtered-dir")
    p.add_argument("--flush-every", type=float, default=5.0, help="simulated seconds between flushes of the filter (test)")
    a = ap.parse_args()
    if a.cmd in ("run", "run-instance") and not a.node_dir:
        ap.error("--node-dir is required")
    if a.cmd == "sample" and a.filtered_dir and not a.node_dir:
        ap.error("--filtered-dir needs --node-dir")
    return {"run": cmd_run, "run-instance": cmd_run_instance, "sample": cmd_sample}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
