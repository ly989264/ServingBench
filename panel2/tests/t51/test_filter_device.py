#!/usr/bin/env python3
"""T51 (pack two v3.1-extra), C15 (replaces C14): glm5.3's forward and sampling from the device time only, empty where
there is none (no host fallback); the Worker's records written up to a few steps late. The node filter only, without
containers.

  python3 -I tests/t51/test_filter_device.py <P.log> <D.log>

1. A list of one path as an accumulate value (profiles/glm53: stages.<seg>.device_duration_ns only): synthetic Worker
   records with a device time, a null one, none (host time only), an empty stage, a value that is not a number, no
   request, TP rank 1. The sums take the device time only; a step without one adds nothing; the self-check line's
   acc_fallback counts per rule the records that have the stage but no device time (the steps left empty). A list of
   two paths (device, then host; the C14 definitions of 52b8824b) still takes the first that is not null and counts
   the records whose value is the host time. With the pack's segments.json and the dsv41 one (single paths, not lists)
   the same records sum the host time and the self-check line has no acc_fallback.
1b. profiles/glm53 "missing_execution_id": a record with no execution_id is a step of its own and makes that rule's
   first-step value of its requests null; a request whose first record has none has no first step (first_exec null,
   every first_ns null); totals unchanged; the self-check line counts the records (exec_missing, once per record).
   The pack's segments.json and dsv41 take the record_id as the step, as in v3, and have no exec_missing.
2. A segments.json with a list read again while the process runs: the count starts at 0 and the line carries it.
3. Deferred writing on the rc1 logs (their forward and sampling stages given a device time equal to the host time, as
   the patched site writes both; see with_device): the Worker's execute and sample records of rank 0 written 1 or 2 steps later, in
   step order, timestamp_ns the time they are written (the patch defers them until the device time is read, at most
   4 steps). With every segment definition (the pack's, dsv41, glm53) every request's sums, steps, first-step values
   and first step are those of the logs as written.
4. The same with the records split over EngineCore and two Worker processes (TP2): the Worker's lines come later than
   the EngineCore's. Every part of a request has the same first step, so the views' choice of the earliest part
   (argMinIf over first_mono_ns) cannot change the first step; checked against the logs as written.
5. Time windows: the filter's periodic flush (every flush_seconds, 5 s) made to run after every step, so each late
   record goes into a later flush than its step's EngineCore records; and the request state dropped (as after
   idle_evict_seconds of no records) right before each batch of late records, so they start a new part of the request.
   The filter attributes by execution_id and request id, never by timestamp_ns; its lines are cumulative per part and
   the views add the parts up and take the first step from the earliest part. Same results as the logs as written.
Prints OK/FAIL per check; exit 1 on any FAIL.
"""
import collections
import importlib.util
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.join(HERE, "..", "..", "vllm-pd-grafana-panel2")
spec = importlib.util.spec_from_file_location("cand_test", os.path.join(HERE, "test_filter_candidate.py"))
T = importlib.util.module_from_spec(spec)
spec.loader.exec_module(T)
check, records, load_filter, replay, split_tp = T.check, T.records, T.load_filter, T.replay, T.split_tp


def parts(lines):
    """{(role, request_id, part): last pd_seg_partial line of the part}."""
    out = {}
    for _, o in lines:
        if o.get("event") == "pd_seg_partial":
            k = (o["role"], o["request_id"], o["part"])
            if k not in out or o["seq"] > out[k]["seq"]:
                out[k] = o
    return out


def per_request(lines):
    """{(role, request_id): (first_exec, {acc: (ns, steps, first_ns)})}, parts added up as the views do, the first
    step and the first-step values from the part with the smallest first_mono_ns."""
    by = collections.defaultdict(list)
    for (role, rid, _), o in parts(lines).items():
        by[(role, rid)].append(o)
    out = {}
    for key, os_ in by.items():
        os_.sort(key=lambda o: (o["first_mono_ns"] is None, o["first_mono_ns"] or 0))
        fe = next((o["first_exec"] for o in os_ if o.get("first_exec")), None)
        segs = {}
        for a in sorted({a for o in os_ for a in o["segs"]}):
            have = [o for o in os_ if a in o["segs"]]
            segs[a] = (sum(o["segs"][a]["ns"] for o in have), sum(o["segs"][a]["steps"] for o in have), have[0]["segs"][a]["first_ns"])
        out[key] = (fe, segs)
    return out


def selfchecks(lines):
    return [o for _, o in lines if o.get("event") == "pd_filter_selfcheck"]


def defer(recs, worker_proc=None, mark=None):
    """The Worker's execute/sample records of rank 0 written 1 or 2 steps later (by EngineCore execution records), in
    step order; timestamp_ns the time of writing. worker_proc: only records of that process (the TP2 split).
    mark "flush": the filter's periodic flush after every step; "evict": every request state dropped right before each
    batch of late records (markers that replay2 acts on)."""
    out, pending, order = [], [], {}
    last_ts = 0
    n_moved = collections.Counter()

    def release(n_steps):
        nonlocal last_ts
        for e in pending:
            e[0] -= n_steps
        if mark == "evict" and pending and pending[0][0] <= 0:
            out.append(("__EVICT__", None))
        while pending and pending[0][0] <= 0:
            _, proc, o = pending.pop(0)
            last_ts += 1
            o["timestamp_ns"] = max(o.get("timestamp_ns") or 0, last_ts)
            out.append((proc, json.dumps(o, separators=(",", ":"))))
    for proc, j in recs:
        o = json.loads(j)
        if isinstance(o.get("timestamp_ns"), int):
            last_ts = max(last_ts, o["timestamp_ns"])
        if (o.get("event") == "worker_summary" and o.get("tp_rank", 0) == 0 and o.get("phase") in ("execute", "sample")
                and o.get("execution_id") and (worker_proc is None or proc == worker_proc)):
            n = order.setdefault(o["execution_id"], 1 + len(order) % 2)
            n_moved[n] += 1
            pending.append([n, proc, o])
            continue
        out.append((proc, j))
        if o.get("event") == "core_summary" and o.get("scope") == "execution":
            release(1)
            if mark == "flush":
                out.append(("__FLUSH__", None))
    release(10 ** 9)
    return out, n_moved


def replay2(mod, mapping, recs, out, inst):
    """replay() of test_filter_candidate, plus the markers of defer(): __FLUSH__ runs every handler's periodic flush,
    __EVICT__ writes and drops every request state (what the filter does after idle_evict_seconds)."""
    import logging
    import re
    os.environ["PD_DIAG_INSTANCE"] = inst
    handlers = {}
    n = collections.Counter()
    for proc, payload in recs:
        if proc in ("__FLUSH__", "__EVICT__"):
            n[proc] += 1
            for h in handlers.values():
                with h._mu:
                    h._flush_partials(force_all=proc == "__EVICT__", reason="periodic" if proc == "__FLUSH__" else "idle")
            continue
        h = handlers.get(proc)
        if h is None:
            h = handlers[proc] = mod.PdDiagHandler(mapping=mapping, log_dir=os.path.join(out, re.sub(r"[^A-Za-z0-9_]", "_", proc)))
        rec = logging.LogRecord("vllm.v1.metrics.pd_diagnostics", logging.INFO, "pd_diagnostics.py", 0, "[PD_DIAG] %s", (payload,), None)
        rec.processName = proc
        h.handle(rec)
    for h in handlers.values():
        h.close()
    lines = []
    for root, _, files in os.walk(out):
        for f in sorted(files):
            for line in open(os.path.join(root, f), encoding="utf-8"):
                i = line.find("[PD_DIAG] ")
                if i >= 0:
                    lines.append((f.split(".")[0], json.loads(line[i + 10:])))
    return lines, n


def with_device(recs):
    """The rc1 logs predate the patch: their Worker records have the host time only. Sections 3-5 add a device time equal
    to the host time to every forward and sampling stage, as the patched site writes both, so the glm53 definitions
    (device time only, C15) have values to move between steps and parts. The pack's and dsv41's definitions read the
    host time, which is unchanged."""
    out = []
    for proc, payload in recs:
        o = json.loads(payload)
        if o.get("event") == "worker_summary":
            for seg in ("forward", "sampling"):
                st = (o.get("stages") or {}).get(seg)
                if isinstance(st, dict) and isinstance(st.get("duration_ns"), int):
                    st["device_duration_ns"] = st["duration_ns"]
            payload = json.dumps(o, separators=(",", ":"))
        out.append((proc, payload))
    return out


def synthetic():
    def w(phase, exe, rids, stage, rank=0):
        seg = "forward" if phase == "execute" else "sampling"
        return ("Worker_TP0", json.dumps({"event": "worker_summary", "version": 2, "phase": phase, "execution_id": exe,
                                          "request_ids": rids, "tp_rank": rank, "timestamp_ns": 1000 + len(exe),
                                          "stages": {seg: stage}}, separators=(",", ":")))
    r = "chatcmpl-c14-device-0001"
    recs = [
        w("execute", "e1", [r], {"duration_ns": 1000, "device_duration_ns": 700}),     # device
        w("execute", "e2", [r], {"duration_ns": 1100, "device_duration_ns": None}),    # null: host, counted
        w("execute", "e3", [r], {"duration_ns": 1200}),                                 # missing: host, counted
        w("execute", "e4", [r], {}),                                                    # neither: nothing
        w("execute", "e5", [r], {"duration_ns": 1300, "device_duration_ns": "x"}),     # not a number: nothing
        w("execute", "e6", [], {"duration_ns": 1400}),                                  # no request: dropped, not counted
        w("execute", "e7", [r], {"duration_ns": 9999}, rank=1),                         # rank 1: dropped, not counted
        w("sample", "e1", [r], {"duration_ns": 50, "device_duration_ns": 30}),
        w("sample", "e2", [r], {"duration_ns": 60}),
    ]
    return r, recs


def no_exec():
    """Two requests: ra's second forward record has no execution_id; rb's records start without one. The forward and
    sampling stages carry the same device and host time, so every segment definition reads the same numbers."""
    def rec(proc, ev, extra, stage):
        o = dict({"event": ev, "version": 2, "timestamp_ns": 5000 + len(out), "record_id": "rec-%d" % len(out)}, **extra)
        o["stages"] = stage
        out.append((proc, json.dumps(o, separators=(",", ":"))))
    out = []
    ra, rb = "chatcmpl-c-noexec-a", "chatcmpl-c-noexec-b"
    # ra: x1 (forward 100, sampling 10, wait_for_put 5), then forward 200 with no execution_id, then x3 (forward 300, sampling 20)
    rec("Worker_TP0", "worker_summary", {"phase": "execute", "execution_id": "x1", "request_ids": [ra], "tp_rank": 0},
        {"forward": {"duration_ns": 100, "device_duration_ns": 100}, "wait_for_put": {"duration_ns": 5}})
    rec("Worker_TP0", "worker_summary", {"phase": "sample", "execution_id": "x1", "request_ids": [ra], "tp_rank": 0}, {"sampling": {"duration_ns": 10, "device_duration_ns": 10}})
    rec("Worker_TP0", "worker_summary", {"phase": "execute", "execution_id": None, "request_ids": [ra], "tp_rank": 0},
        {"forward": {"duration_ns": 200, "device_duration_ns": 200}, "wait_for_put": {"duration_ns": 6}})
    rec("Worker_TP0", "worker_summary", {"phase": "execute", "execution_id": "x3", "request_ids": [ra], "tp_rank": 0}, {"forward": {"duration_ns": 300, "device_duration_ns": 300}})
    rec("Worker_TP0", "worker_summary", {"phase": "sample", "execution_id": "x3", "request_ids": [ra], "tp_rank": 0}, {"sampling": {"duration_ns": 20, "device_duration_ns": 20}})
    # rb: an EngineCore step with no execution_id first, then one with; its Worker: forward with none, then with
    rec("EngineCore", "core_summary", {"scope": "execution", "request_ids": [rb]}, {"alloc": {"duration_ns": 4}})
    rec("EngineCore", "core_summary", {"scope": "execution", "execution_id": "y2", "request_ids": [rb]}, {"alloc": {"duration_ns": 5}})
    rec("Worker_TP0", "worker_summary", {"phase": "execute", "execution_id": "", "request_ids": [rb], "tp_rank": 0}, {"forward": {"duration_ns": 300, "device_duration_ns": 300}})
    rec("Worker_TP0", "worker_summary", {"phase": "execute", "execution_id": "y2", "request_ids": [rb], "tp_rank": 0}, {"forward": {"duration_ns": 400, "device_duration_ns": 400}})
    rec("Worker_TP0", "worker_summary", {"phase": "sample", "execution_id": "y2", "request_ids": [rb], "tp_rank": 0}, {"sampling": {"duration_ns": 40, "device_duration_ns": 40}})
    return ra, rb, out


def main():
    p_log, d_log = sys.argv[1], sys.argv[2]
    work = tempfile.mkdtemp(prefix="t51-dev-")
    try:
        new = load_filter(os.path.join(PACK, "node", "pd_diag_logfilter.py"), "new_filter")
        default = os.path.join(PACK, "segments.json")
        profs = {"default": default, "dsv41": os.path.join(PACK, "profiles", "dsv41", "segments.json"),
                 "glm53": os.path.join(PACK, "profiles", "glm53", "segments.json")}
        acc = {a["id"]: a["value"] for a in json.load(open(profs["glm53"]))["accumulate"]}
        check("glm53: forward, sampling read the device time only (C15)",
              acc["forward"] == ["stages.forward.device_duration_ns"]
              and acc["sampling"] == ["stages.sampling.device_duration_ns"], json.dumps(acc))
        acc = {a["id"]: a["value"] for a in json.load(open(profs["dsv41"]))["accumulate"]}
        check("dsv41: single paths only (the delivered definitions)", all(isinstance(v, str) for v in acc.values()), json.dumps(acc))

        # 1. synthetic records
        rid, recs = synthetic()
        c14 = os.path.join(work, "glm53-c14.json")
        raw = json.load(open(profs["glm53"]))
        for a in raw["accumulate"]:
            if a["id"] in ("forward", "sampling"):
                a["value"] = ["stages.%s.device_duration_ns" % a["id"], "stages.%s.duration_ns" % a["id"]]
        json.dump(raw, open(c14, "w"))
        cases = (
            ("glm53", profs["glm53"], {"forward": (700, 1, 700), "sampling": (30, 1, 30)}, {"forward": 3, "sampling": 1},
             "sums take the device time only (forward 700, sampling 30); the other steps add nothing",
             "acc_fallback {forward: 3, sampling: 1}: the steps with the stage but no device time (null, host only, empty "
             "stage); not counted: not a number, no request, rank 1"),
            ("glm53 with two paths (C14)", c14, {"forward": (700 + 1100 + 1200, 3, 700), "sampling": (30 + 60, 2, 30)},
             {"forward": 2, "sampling": 1}, "sums take the first path that is not null (forward 700 + 1100 + 1200, sampling 30 + 60)",
             "acc_fallback {forward: 2, sampling: 1}: the records whose value is the host time"),
        )
        for i, (name, prof, want, want_fb, what, what_fb) in enumerate(cases):
            lines, counts = replay(new, prof, recs, os.path.join(work, "syn-glm53-%d" % i), "D0")
            got = per_request(lines).get(("worker", rid), (None, {}))
            check("%s: %s" % (name, what), {k: v for k, v in got[1].items() if k in want} == want and got[0] == "e1", json.dumps(got))
            fb = [o.get("acc_fallback") for o in selfchecks(lines)]
            check("%s: self-check line %s" % (name, what_fb), bool(fb) and all(x == want_fb for x in fb), json.dumps(fb))
            check("%s: the counting equation of the self-check line still holds" % name,
                  all(c["seen"] == c["written"] + c["dropped"] + c["no_event"] + c["rank_dropped"] + c["version_dropped"]
                      + c["accumulated"] + c["errors"] for c in counts.values()), json.dumps(counts))
        for name in ("default", "dsv41"):
            lines, _ = replay(new, profs[name], recs, os.path.join(work, "syn-" + name), "D0")
            got = per_request(lines).get(("worker", rid), (None, {}))
            check("%s: the same records sum the host time (forward 1000 + 1100 + 1200 + 1300, sampling 50 + 60)" % name,
                  {k: v for k, v in got[1].items() if k in ("forward", "sampling")} == {"forward": (4600, 4, 1000), "sampling": (110, 2, 50)},
                  json.dumps(got))
            check("%s: no acc_fallback, no exec_missing in the self-check line" % name,
                  all("acc_fallback" not in o and "exec_missing" not in o for o in selfchecks(lines)) and selfchecks(lines))

        # 1b. records with no execution_id
        ra, rb, recs_x = no_exec()
        for name in ("glm53", "default", "dsv41"):
            lines, _ = replay(new, profs[name], recs_x, os.path.join(work, "noexec-" + name), "D0")
            pr = per_request(lines)
            fa, sa = pr.get(("worker", ra), (None, {}))
            fb_, sb = pr.get(("worker", rb), (None, {}))
            ea, eb = pr.get(("engine", ra), (None, {})), pr.get(("engine", rb), (None, {}))
            sc = selfchecks(lines)
            if name == "glm53":
                check("glm53: a later record with no execution_id: forward steps 3, total 600, first null; sampling first 10 "
                      "(all its records have one); first step x1",
                      sa.get("forward") == (600, 3, None) and sa.get("sampling") == (30, 2, 10) and fa == "x1", json.dumps([fa, sa]))
                check("glm53: the first record with no execution_id: first step null, every first null; steps and totals as "
                      "the records (forward 2 steps 700, sampling 1 step 40, alloc 2 steps 9)",
                      fb_ is None and eb[0] is None and sb.get("forward") == (700, 2, None) and sb.get("sampling") == (40, 1, None)
                      and eb[1].get("alloc") == (9, 2, None), json.dumps([fb_, sb, eb]))
                # one line per process here (EngineCore 1 record, Worker 2; ra's execute record counts once although
                # forward and wait_for_put both read it)
                check("glm53: the self-check lines count the 3 records with no execution_id (EngineCore 1, Worker 2; a "
                      "record that two rules read counts once)", sorted(o.get("exec_missing") for o in sc) == [1, 2],
                      json.dumps([o.get("exec_missing") for o in sc]))
            else:
                check("%s: no key: the record_id stands for the step, as in v3 (first values numbers, no exec_missing)" % name,
                      sa.get("forward", (0, 0, None))[2] is not None and sb.get("forward", (0, 0, None))[2] is not None
                      and all("exec_missing" not in o for o in sc), json.dumps([sa, sb]))

        # 2. read again while running
        live = os.path.join(work, "live.json")
        shutil.copyfile(default, live)
        os.environ["PD_DIAG_INSTANCE"] = "D0"
        h = new.PdDiagHandler(mapping=live, log_dir=os.path.join(work, "live"))
        import logging
        for proc, payload in recs[:2]:
            rec = logging.LogRecord("vllm.v1.metrics.pd_diagnostics", logging.INFO, "pd_diagnostics.py", 0, "[PD_DIAG] %s", (payload,), None)
            rec.processName = proc
            h.handle(rec)
        before = h._fallback
        shutil.copyfile(profs["glm53"], live)
        with h._mu:
            h._load_config()
        for proc, payload in recs[1:3]:
            rec = logging.LogRecord("vllm.v1.metrics.pd_diagnostics", logging.INFO, "pd_diagnostics.py", 0, "[PD_DIAG] %s", (payload,), None)
            rec.processName = proc
            h.handle(rec)
        h.close()
        sc = [json.loads(l[l.index("[PD_DIAG] ") + 10:]) for root, _, fs in os.walk(os.path.join(work, "live")) for f in fs
              for l in open(os.path.join(root, f), encoding="utf-8") if "pd_filter_selfcheck" in l]
        check("read again while running: no count before (%s), then forward 2 for the two records without a device time "
              "(null, host only)" % before,
              before is None and sc and sc[-1].get("acc_fallback") == {"forward": 2, "sampling": 0}, json.dumps([o.get("acc_fallback") for o in sc]))

        # 3., 4. deferred writing on the rc1 logs
        for side, path, inst in (("P", p_log, "P0"), ("D", d_log, "D0")):
            recs = with_device(records(path))
            shifted, moved = defer(recs)
            print("     %s: %d Worker records written later (1 step %d, 2 steps %d)" % (side, sum(moved.values()), moved[1], moved[2]))
            for name, prof in profs.items():
                a, _ = replay(new, prof, recs, os.path.join(work, side, name, "as-written"), inst)
                b, _ = replay(new, prof, shifted, os.path.join(work, side, name, "deferred"), inst)
                pa, pb = per_request(a), per_request(b)
                diff = [k for k in set(pa) | set(pb) if pa.get(k) != pb.get(k)]
                check("%s %s, one process: Worker records 1-2 steps late, every request's sums, steps, first-step values and "
                      "first step unchanged (%d request·process)" % (side, name, len(pa)), not diff and pa,
                      json.dumps({str(k): [pa.get(k), pb.get(k)] for k in diff[:3]}))
            tp = split_tp(recs)
            tp_late, moved2 = defer(tp, worker_proc="Worker_TP0")
            for name, prof in profs.items():
                a, _ = replay(new, prof, tp, os.path.join(work, side, name, "tp2"), inst)
                b, _ = replay(new, prof, tp_late, os.path.join(work, side, name, "tp2-deferred"), inst)
                fe_parts = collections.defaultdict(set)
                for (role, rid_, _), o in parts(b).items():
                    if o.get("first_exec"):
                        fe_parts[rid_].add(o["first_exec"])
                multi = sum(1 for v in fe_parts.values() if len(v) > 1)
                pa, pb = per_request(a), per_request(b)
                diff = [k for k in set(pa) | set(pb) if pa.get(k) != pb.get(k)]
                check("%s %s, TP2: the Worker's lines later than the EngineCore's (%d records); every part of a request has "
                      "the same first step (%d requests with differing parts); sums and first step unchanged"
                      % (side, name, sum(moved2.values()), multi), multi == 0 and not diff and pa,
                      json.dumps({str(k): [pa.get(k), pb.get(k)] for k in diff[:3]}))
            # 5. time windows: a flush after every step; request states dropped before late records
            for mark in ("flush", "evict"):
                for split_name, base in (("one process", recs), ("TP2", tp)):
                    late, _ = defer(base, worker_proc=None if split_name == "one process" else "Worker_TP0", mark=mark)
                    for name, prof in profs.items():
                        a, _ = replay(new, prof, base, os.path.join(work, side, name, "w-" + mark + split_name[:3]), inst)
                        b, nm = replay2(new, prof, late, os.path.join(work, side, name, "w-" + mark + split_name[:3] + "-late"), inst)
                        pa, pb = per_request(a), per_request(b)
                        diff = [k for k in set(pa) | set(pb) if pa.get(k) != pb.get(k)]
                        n_parts = collections.Counter((r, rid_) for (r, rid_, _) in parts(b))
                        what = ("a flush after every step (%d flushes)" % nm["__FLUSH__"] if mark == "flush" else
                                "request states dropped before late records (%d times; up to %d parts per request)"
                                % (nm["__EVICT__"], max(n_parts.values())))
                        check("%s %s, %s, Worker records 1-2 steps late, %s: sums, steps, first-step values and first step "
                              "unchanged" % (side, name, split_name, what), not diff and pa and sum(nm.values()) > 0,
                              json.dumps({str(k): [pa.get(k), pb.get(k)] for k in diff[:3]}))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("%d FAIL" % len(T.FAILS))
    sys.exit(1 if T.FAILS else 0)


if __name__ == "__main__":
    main()
