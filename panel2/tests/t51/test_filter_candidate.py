#!/usr/bin/env python3
"""T51 (pack two v3.1-extra): the node filter's first-candidate steps (C2), on the rc1 logs (real hardware, TP1, the
patch's records as they are), without containers.

  python3 -I tests/t51/test_filter_candidate.py <P.log> <D.log>

1. Compatibility: with the pack's default segments.json the v3.1-extra filter writes the same lines as the v3 filter
   (pd-v3-dev main 1815e9a, read with git show), record for record, after the run-dependent fields (part, record_id,
   time, pid) are taken out.
2. With profiles/dsv41/segments.json, one process as in rc1 (TP1): every request's sums of the five v3 rules are those
   of the v3 filter, and the candidate sums (cand_submit, cand_lookup, cand_alloc, cand_other) are those computed here from
   the raw records: for each (request, execution_id) named by a candidate.map record, the step's value, unless the step's
   own record names the request.
3. The same records split over processes as with TP > 1: EngineCore (core_summary, pd_transfer_summary[link]), Worker of
   rank 0 (worker_summary, pool_task, the other pd_transfer_summary), Worker of rank 1 (copies with tp_rank 1, dropped).
   Same candidate sums.
4. A busy step: D's first-candidate step made to name another request (as when other requests decode in the same step):
   the D request still gets the step's values, the other request gets them through its own sums only.
5. A step that already names the request (synchronous load): nothing added a second time.
Prints OK/FAIL per check; exit 1 on any FAIL.
"""
import collections
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.join(HERE, "..", "..", "vllm-pd-grafana-panel2")
LINE = re.compile(r"\((?P<proc>[^ ]+) pid=\d+\).*?\[PD_DIAG\] (?P<j>\{.*\})\s*$")
FAILS = []


def check(name, ok, detail=""):
    print(("OK   " if ok else "FAIL ") + name + ("  -- " + detail[:600] if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def records(path):
    out = []
    for line in open(path, encoding="utf-8", errors="replace"):
        m = LINE.search(line)
        if m:
            out.append((m.group("proc"), m.group("j")))
    return out


def load_filter(path, name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def replay(mod, mapping, recs, out, inst):
    """Feeds (process name, json) records to one handler per process name; returns the lines each wrote."""
    os.environ["PD_DIAG_INSTANCE"] = inst
    handlers = {}
    for proc, payload in recs:
        h = handlers.get(proc)
        if h is None:
            h = handlers[proc] = mod.PdDiagHandler(mapping=mapping, log_dir=os.path.join(out, re.sub(r"[^A-Za-z0-9_]", "_", proc)))
        rec = logging.LogRecord("vllm.v1.metrics.pd_diagnostics", logging.INFO, "pd_diagnostics.py", 0, "[PD_DIAG] %s", (payload,), None)
        rec.processName = proc
        h.handle(rec)
    counts = {}
    for proc, h in handlers.items():
        h.close()
        counts[proc] = dict(h._counts)
    lines = []
    for root, _, files in os.walk(out):
        for f in sorted(files):
            for line in open(os.path.join(root, f), encoding="utf-8"):
                i = line.find("[PD_DIAG] ")
                if i >= 0:
                    lines.append((f.split(".")[0], json.loads(line[i + 10:])))
    return lines, counts


def sums(lines):
    """{(role, request_id): {acc_id: (ns, steps)}} from the highest seq of every part."""
    last = {}
    for _, o in lines:
        if o.get("event") == "pd_seg_partial" and (o["part"] not in last or o["seq"] > last[o["part"]]["seq"]):
            last[o["part"]] = o
    out = collections.defaultdict(dict)
    for o in last.values():
        d = out[(o["role"], o["request_id"])]
        for k, v in o["segs"].items():
            ns, st = d.get(k, (0, 0))
            d[k] = (ns + v["ns"], st + v["steps"])
    return out


def normal(lines):
    """Everything but the run-dependent fields, for comparing two filters on the same input."""
    out = []
    for f, o in lines:
        o = dict(o)
        if o.get("event") in ("pd_file_open", "pd_filter_selfcheck"):
            continue
        for k in ("part", "record_id", "timestamp_ns", "pid", "host", "config_version", "seq", "reason"):
            o.pop(k, None)
        out.append((f, json.dumps(o, sort_keys=True)))
    return sorted(out)


def expected_cand(profile, recs):
    """The candidate sums computed straight from the raw records (not by the filter). A pairing needs the naming record
    and the step's record in the same process (with TP > 1, P's EngineCore has no naming record: its values stay out)."""
    out = collections.defaultdict(dict)
    by_proc = collections.defaultdict(list)
    for proc, j in recs:
        by_proc[proc].append(j)
    for js in by_proc.values():
        for key, d in _expected_cand(profile, js).items():
            for k, v in d.items():
                ns, st = out[key].get(k, (0, 0))
                out[key][k] = (ns + v[0], st + v[1])
    return out


def _expected_cand(profile, js):
    cand = profile["candidate"]

    def ok(o, where):
        return all(o.get(k) == v for k, v in (where or {}).items())

    pairs = set()
    objs = [json.loads(j) for j in js]
    for o in objs:
        if o.get("version") != 2:
            continue
        for m in cand["map"]:
            if o.get("event") == m["event"] and ok(o, m.get("where")) and o.get("request_id") and o.get("execution_id"):
                if o.get("tp_rank") not in (None, 0):
                    continue
                pairs.add((o["request_id"], o["execution_id"]))
    by_exec = collections.defaultdict(list)
    for o in objs:
        if o.get("execution_id") and o.get("tp_rank") in (None, 0):
            by_exec[o["execution_id"]].append(o)
    out = collections.defaultdict(dict)
    for rid, exe in pairs:
        for a in cand["accumulate"]:
            role = "worker" if a["event"] == "worker_summary" else "engine"
            for o in by_exec[exe]:
                if o.get("event") != a["event"] or not ok(o, a.get("where")):
                    continue
                v = o
                for k in a["value"].split("."):
                    v = v.get(k) if isinstance(v, dict) else None
                if not isinstance(v, int) or rid in (o.get("request_ids") or []):
                    continue
                ns, st = out[(role, rid)].get(a["id"], (0, 0))
                out[(role, rid)][a["id"]] = (ns + v, st + 1)
    return out


def split_tp(recs, pool_load=False):
    """The rc1 records (TP1, one EngineCore process) as a TP2 instance would write them. pool_load: also the EngineCore's
    pd_transfer_summary[link] with transfer_role "pool_load" that the vllm-ascend patch writes for P's asynchronous
    cache-pool loads (record format note, section 3.3), made from the pool's load_submit records of rc1."""
    out = []
    for proc, j in recs:
        o = json.loads(j)
        ev = o.get("event")
        if pool_load and ev == "pool_task" and o.get("operation") == "load_submit" and o.get("status") == "submission_returned":
            link = {"request_id": o["request_id"], "execution_id": o["execution_id"], "request_ids": [], "scope": "link",
                    "transfer_role": "pool_load", "load_async": True, "links": [], "status": "linked", "complete": True,
                    "incomplete": False, "event": "pd_transfer_summary", "version": 2, "timestamp_ns": o["timestamp_ns"] - 1}
            out.append((proc, json.dumps(link, separators=(",", ":"))))  # the EngineCore, where the pool scheduler runs
        if proc.startswith("EngineCore") and (ev in ("worker_summary", "pool_task") or (ev == "pd_transfer_summary" and o.get("scope") != "link")):
            out.append(("Worker_TP0", j))
            o1 = dict(o, tp_rank=1)
            out.append(("Worker_TP1", json.dumps(o1, separators=(",", ":"))))
        else:
            out.append((proc, j))
    return out


def mutate(recs, exe, rid_list):
    out = []
    for proc, j in recs:
        o = json.loads(j)
        if o.get("execution_id") == exe and o.get("event") in ("core_summary", "worker_summary") and (o.get("scope") == "execution" or o.get("phase") == "execute"):
            o["request_ids"] = rid_list
            j = json.dumps(o, separators=(",", ":"))
        out.append((proc, j))
    return out


def main():
    p_log, d_log = sys.argv[1], sys.argv[2]
    work = tempfile.mkdtemp(prefix="t51-cand-")
    try:
        v3_src = subprocess.run(["git", "-C", PACK, "show", "1815e9a:vllm-pd-grafana-panel2/node/pd_diag_logfilter.py"],
                                stdout=subprocess.PIPE, check=True).stdout
        v3_path = os.path.join(work, "v3_filter.py")
        open(v3_path, "wb").write(v3_src)
        v3 = load_filter(v3_path, "v3_filter")
        new = load_filter(os.path.join(PACK, "node", "pd_diag_logfilter.py"), "new_filter")
        default = os.path.join(PACK, "segments.json")
        dsv = os.path.join(PACK, "profiles", "dsv41", "segments.json")
        profile = json.load(open(dsv))
        for side, path, inst in (("P", p_log, "P0"), ("D", d_log, "D0")):
            recs = records(path)
            a, _ = replay(v3, default, recs, os.path.join(work, side, "v3"), inst)
            b, cb = replay(new, default, recs, os.path.join(work, side, "new-default"), inst)
            check("%s: default segments.json, same lines as the v3 filter (%d lines)" % (side, len(a)), normal(a) == normal(b))
            c, cc = replay(new, dsv, recs, os.path.join(work, side, "new-dsv41"), inst)
            sa, sc = sums(a), sums(c)
            v3_ids = [x["id"] for x in json.load(open(default))["accumulate"]]
            same = all({k: v for k, v in sc.get(key, {}).items() if k in v3_ids} == sa[key] for key in sa)
            check("%s: dsv41 profile, the five v3 sums of every request unchanged" % side, same and set(sa) <= set(sc))
            exp = expected_cand(profile, recs)
            got = {key: {k: v for k, v in d.items() if k.startswith("cand_")} for key, d in sc.items()}
            got = {k: v for k, v in got.items() if v}
            check("%s: candidate sums as computed from the raw records (%d requests, %d values)" % (
                side, len(exp), sum(len(v) for v in exp.values())), got == dict(exp), json.dumps({"got": {str(k): v for k, v in got.items()}, "exp": {str(k): v for k, v in exp.items()}}))
            print("     %s candidate sums: %s" % (side, json.dumps({"%s %s" % (k[0], k[1][-8:]): v for k, v in sorted(got.items())})))
            counts = collections.Counter()
            for d in cc.values():
                counts.update(d)
            check("%s: self-check counts candidate pairings, the counting equation still holds" % side,
                  counts.get("cand_applied", 0) == sum(len(v) for v in exp.values())
                  and counts["seen"] == counts["written"] + counts["dropped"] + counts["no_event"] + counts["rank_dropped"]
                  + counts["version_dropped"] + counts["accumulated"] + counts["errors"], json.dumps(counts))
            # TP > 1
            t, ct = replay(new, dsv, split_tp(recs), os.path.join(work, side, "tp2"), inst)
            st = sums(t)
            got2 = {key: {k: v for k, v in d.items() if k.startswith("cand_")} for key, d in st.items()}
            got2 = {k: v for k, v in got2.items() if v}
            exp2 = expected_cand(profile, split_tp(recs))
            check("%s: records split over EngineCore and two Worker processes (TP2): candidate sums as computed per process%s" % (
                side, "; P's EngineCore has no naming record, so only the Worker's submit" if side == "P" else ""),
                  got2 == dict(exp2), json.dumps({"got": {str(k): v for k, v in got2.items()}, "exp": {str(k): v for k, v in exp2.items()}}))
            check("%s: TP2, rank 1's copies dropped" % side, ct.get("Worker_TP1", {}).get("rank_dropped", 0) > 0
                  and ct["Worker_TP1"].get("partials_written", 0) == 0)
            if side == "P":
                # with the patch's pool_load link in the EngineCore, P's EngineCore values come too (TP2 = TP1 result)
                t3, _ = replay(new, dsv, split_tp(recs, pool_load=True), os.path.join(work, side, "tp2-poolload"), inst)
                got3 = {key: {k: v for k, v in d.items() if k.startswith("cand_")} for key, d in sums(t3).items()}
                got3 = {k: v for k, v in got3.items() if v}
                check("P: TP2 with the patch's pool_load link: candidate sums as with TP1", got3 == dict(exp),
                      json.dumps({"got": {str(k): v for k, v in got3.items()}, "exp": {str(k): v for k, v in exp.items()}}))
            if side == "D":
                # the buffer's limits come from segments.json; a too small hold_steps pushes steps out, and that is counted
                small = os.path.join(work, "small.json")
                prof2 = json.load(open(dsv))
                prof2["candidate"]["hold_steps"] = 2
                json.dump(prof2, open(small, "w"))
                _, cs = replay(new, small, recs, os.path.join(work, side, "small"), inst)
                c2 = collections.Counter()
                for d in cs.values():
                    c2.update(d)
                check("D: hold_steps 2 in segments.json: steps pushed out are counted (cand_evicted_full %d, cand_unpaired %d)" % (
                    c2.get("cand_evicted_full", 0), c2.get("cand_unpaired", 0)), c2.get("cand_evicted_full", 0) > 0, json.dumps(c2))
                # 4. the busy step: D's first-candidate step names another request
                pairs = [(json.loads(j)["request_id"], json.loads(j)["execution_id"]) for _, j in recs
                         if '"pd_transfer_summary"' in j and '"link"' in j]
                rid, exe = pairs[0]
                busy = mutate(recs, exe, ["chatcmpl-busy-other-0000abcd"])
                u, _ = replay(new, dsv, busy, os.path.join(work, side, "busy"), inst)
                su = sums(u)
                mine = {k: v for k, v in su.get(("worker", rid), {}).items() if k == "cand_submit"}
                other = su.get(("worker", "chatcmpl-busy-other-0000abcd"), {})
                check("D busy step: the D request still gets the step's submit", mine == {k: v for k, v in exp[("worker", rid)].items() if k == "cand_submit"},
                      json.dumps(mine))
                check("D busy step: the other request has no candidate sums", not any(k.startswith("cand_") for k in other), json.dumps(other))
            if side == "P":
                # 5. the request named in its own first-candidate step
                pairs = [(json.loads(j)["request_id"], json.loads(j)["execution_id"]) for _, j in recs
                         if '"pool_task"' in j and '"load_submit"' in j and '"submission_returned"' in j]
                rid, exe = pairs[0]
                named = mutate(recs, exe, [rid])
                v, _ = replay(new, dsv, named, os.path.join(work, side, "named"), inst)
                sv = sums(v)
                check("P step that names the request: nothing added a second time",
                      not any(k.startswith("cand_") for d in (sv.get(("engine", rid), {}), sv.get(("worker", rid), {})) for k in d),
                      json.dumps({"engine": sv.get(("engine", rid)), "worker": sv.get(("worker", rid))}))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("%d FAIL" % len(FAILS))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
