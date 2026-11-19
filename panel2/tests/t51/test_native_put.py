#!/usr/bin/env python3
"""T51 (pack two v3.1-extra): scripts/native_put.py on its own, no containers except the fetch scripts' check.

  python3 -I tests/t51/test_native_put.py [<P.log of the rc1 run>]

1. Association on a synthetic case: two calls of one task, lines of another thread (another TP rank), a line just outside
   the call, the year boundary; a task whose line never comes, and a line no task claims, expire and are counted.
   The thread fallback (MOONCAKE_TID_FALLBACK) is off by default: a save task that arrives before the lines of several
   TP ranks, rank 0's line last, still gets rank 0's own line.
1b. glog 0.6 prints the year in the prefix (IYYYYMMDD; the glm5.3 site), glog 0.4 does not (IMMDD; Mooncake's npu
   wheel, the lab). Both parse; a task matched with lines of either kind gives the same record; a call across the new
   year matches a line just after midnight that carries the new year (the yearless line, read in the call's year, does
   not); the old-signature wrapper line (complete=false, no phase 1, no total) still gives submit, wait_for_transfer,
   finalize, and no start_batch_put (missing, not 0).
1c. W1's lines (glog 0.6.0, the site's own binary, lab runs of the replacement library; the 0.4 forms with the year
   taken out): all parse; the full line and the old-signature wrapper's empty batch each give one record.
2. With the rc1 P log (real hardware, one batch write): its pool_task[save] record and its [BatchPutTiming] line give one
   pd_native_put record with phases 2, 3, 4 as submit, wait_for_transfer, finalize.
3. The fetch scripts in a throw-away container each (python:3.12-slim: GNU tools; alpine: BusyBox): only complete
   timing lines, from where the last round stopped; a truncated file starts again from its start; a line being written
   waits for the next round; the first round starts at the end.
Prints OK/FAIL per check; exit 1 on any FAIL.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "vllm-pd-grafana-panel2", "scripts"))
import native_put as N  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(("OK   " if ok else "FAIL ") + name + ("  -- " + detail if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def line(mo, da, t, tid, keys, nbytes, p=(10, 20, 30, 40), scope="normal_full"):
    hh, mi, ss, us = t
    return ("I%02d%02d %02d:%02d:%02d.%06d %6d client_service.cpp:3327] [BatchPutTiming] keys=%d bytes=%d "
            "phase1_start_batch_put_us=%d phase2_submit_transfers_us=%d phase3_wait_transfers_us=%d "
            "phase4_finalize_batch_put_us=%d total_us=%d timing_scope=%s status=ok complete=true failed_keys=0"
            % (mo, da, hh, mi, ss, us, tid, keys, nbytes, p[0], p[1], p[2], p[3], sum(p), scope))


def wall_ns(y, mo, da, hh, mi, ss, us, offset_s=28800):
    import datetime
    t = datetime.datetime(y, mo, da, hh, mi, ss, us) - datetime.timedelta(seconds=offset_s)
    return int((t - datetime.datetime(1970, 1, 1)).total_seconds()) * 1000000000 + us * 1000 - us * 1000 + t.microsecond * 1000


def call(tid, keys, nbytes, start, end, offset_s=28800):
    return {"operation": "mooncake_put", "native_tid": tid, "start_wall_ns": wall_ns(*start, offset_s=offset_s),
            "end_wall_ns": wall_ns(*end, offset_s=offset_s), "wall_utc_offset_seconds": offset_s,
            "submitted_keys": keys, "submitted_bytes": nbytes}


def line06(y, mo, da, t, tid, keys, nbytes, p=(10, 20, 30, 40), scope="normal_full"):
    """A timing line as glog 0.6 prints it: the year in the prefix."""
    return "I%04d" % y + line(mo, da, t, tid, keys, nbytes, p, scope)[1:]


def line06_legacy(y, mo, da, t, tid, keys, nbytes, p=(20, 30, 40), scope="normal_full"):
    """The old-signature wrapper's line: complete=false, no phase 1, no total."""
    hh, mi, ss, us = t
    return ("I%04d%02d%02d %02d:%02d:%02d.%06d %6d client_service.cpp:3348] [BatchPutTiming] keys=%d bytes=%d "
            "phase2_submit_transfers_us=%d phase3_wait_transfers_us=%d phase4_finalize_batch_put_us=%d "
            "timing_scope=%s status=ok complete=false failed_keys=0"
            % (y, mo, da, hh, mi, ss, us, tid, keys, nbytes, p[0], p[1], p[2], scope))


def glog06():
    l6, l4 = line06(2026, 11, 19, (8, 36, 3, 863473), 1664, 4, 16384), line(11, 19, (8, 36, 3, 863473), 1664, 4, 16384)
    a6, a4 = N.parse_native(l6), N.parse_native(l4)
    check("glog 0.6: a line with the year in the prefix parses (year 2026, 11-19, thread 1664)",
          a6 is not None and (a6["year"], a6["month"], a6["day"], a6["tid"]) == (2026, 11, 19, 1664), l6)
    check("glog 0.4: a line without the year still parses (year None)", a4 is not None and a4["year"] is None, l4)
    check("both kinds give the same fields apart from the year",
          a6 is not None and a4 is not None and {k: v for k, v in a6.items() if k != "year"} == {k: v for k, v in a4.items() if k != "year"})
    recs = {}
    for kind, mk in (("0.4", lambda *x, **k: line(*x[1:], **k)), ("0.6", line06)):
        out = []
        a = N.Associator(lambda node, inst, payload: out.append(json.loads(payload)), window_s=60)
        a.add_task("n1", "P0", {"event": "pool_task", "operation": "save", "request_id": "chatcmpl-g-1234abcd",
                                "native_calls": [call(77, 4, 100, (2026, 11, 19, 8, 36, 3, 0), (2026, 11, 19, 8, 36, 4, 0))]}, now=0)
        a.add_native("n1", mk(2026, 11, 19, (8, 36, 3, 500000), 77, 4, 100, p=(1, 2, 3, 4)), now=0)
        recs[kind] = out
    check("glog 0.6 and 0.4: the same task gives the same record (submit 2 us, wait_for_transfer 3 us, finalize 4 us)",
          len(recs["0.6"]) == 1 and recs["0.6"] == recs["0.4"] and recs["0.6"][0]["stages"].get("submit") == {"duration_ns": 2000},
          json.dumps(recs))
    # the new year: a call from 31 Dec 23:59:59.9 to 1 Jan 00:00:00.8 local, the line printed at 00:00:00.1 on 1 Jan
    for kind, ln, want in (("glog 0.6 (the line's year 2027)", line06(2027, 1, 1, (0, 0, 0, 100000), 80, 1, 9), 1),
                           ("glog 0.4 (no year: read in the call's year 2026)", line(1, 1, (0, 0, 0, 100000), 80, 1, 9), 0)):
        out = []
        a = N.Associator(lambda node, inst, payload: out.append(json.loads(payload)), window_s=60)
        a.add_task("n1", "P1", {"event": "pool_task", "operation": "save", "request_id": "chatcmpl-ny-1234abcd",
                                "native_calls": [call(80, 1, 9, (2026, 12, 31, 23, 59, 59, 900000), (2027, 1, 1, 0, 0, 0, 800000))]}, now=0)
        a.add_native("n1", ln, now=0)
        check("new year, %s: %s" % (kind, "matched" if want else "not matched (why the line's own year is used)"), len(out) == want, json.dumps(out))
    out = []
    a = N.Associator(lambda node, inst, payload: out.append(json.loads(payload)), window_s=60)
    a.add_task("n1", "P0", {"event": "pool_task", "operation": "save", "request_id": "chatcmpl-lg-1234abcd",
                            "native_calls": [call(90, 2, 50, (2026, 11, 19, 9, 0, 0, 0), (2026, 11, 19, 9, 0, 1, 0))]}, now=0)
    lg = line06_legacy(2026, 11, 19, (9, 0, 0, 400000), 90, 2, 50, p=(21, 31, 41))
    a.add_native("n1", lg, now=0)
    st = out[0]["stages"] if len(out) == 1 else {}
    check("old-signature wrapper line (complete=false, no phase 1, no total): submit / wait_for_transfer / finalize 21 / 31 / 41 us, "
          "start_batch_put missing (not 0)",
          st.get("submit") == {"duration_ns": 21000} and st.get("wait_for_transfer") == {"duration_ns": 31000}
          and st.get("finalize") == {"duration_ns": 41000} and "start_batch_put" not in st, json.dumps(out) + " " + lg)


# 1c. Lines as W1 handed them over (t51-glm53-kit, lab runs of the replacement library with glog 0.6.0, the glm5.3 site's
# own binary): a full line from the put test and the old-signature wrapper's empty batch (complete=false, no phase 1, no
# total); then the same two with the glog 0.4 prefix (the year's four digits taken out, not printed by glog 0.4 itself).
W1_LINES = [
    "I20261119 09:05:49.419656   343 client_service.cpp:3348] [BatchPutTiming] keys=4 bytes=16384 phase1_start_batch_put_us=185 "
    "phase2_submit_transfers_us=9 phase3_wait_transfers_us=64 phase4_finalize_batch_put_us=180 total_us=439 timing_scope=normal_full "
    "status=ok complete=true failed_keys=0",
    "I20261119 08:36:03.863473  1664 client_service.cpp:3348] [BatchPutTiming] keys=0 bytes=0 phase2_submit_transfers_us=1 "
    "phase3_wait_transfers_us=1 phase4_finalize_batch_put_us=9 timing_scope=prefer_same_node_full status=ok complete=false failed_keys=0",
    "I1119 09:05:49.419656   343 client_service.cpp:3348] [BatchPutTiming] keys=4 bytes=16384 phase1_start_batch_put_us=185 "
    "phase2_submit_transfers_us=9 phase3_wait_transfers_us=64 phase4_finalize_batch_put_us=180 total_us=439 timing_scope=normal_full "
    "status=ok complete=true failed_keys=0",
    "I1119 08:36:03.863473  1664 client_service.cpp:3348] [BatchPutTiming] keys=0 bytes=0 phase2_submit_transfers_us=1 "
    "phase3_wait_transfers_us=1 phase4_finalize_batch_put_us=9 timing_scope=prefer_same_node_full status=ok complete=false failed_keys=0",
]


def w1_lines():
    parsed = [N.parse_native(l_) for l_ in W1_LINES]
    check("W1's lines: all 4 parse (year 2026 on the glog 0.6 two, none on the 0.4 two)",
          all(p_ is not None for p_ in parsed) and [p_["year"] for p_ in parsed] == [2026, 2026, None, None], json.dumps(parsed))
    check("W1's lines: the 0.6 and 0.4 forms give the same fields apart from the year",
          all(p_ is not None for p_ in parsed) and all({k: v for k, v in parsed[i].items() if k != "year"} == {k: v for k, v in parsed[i + 2].items() if k != "year"} for i in (0, 1)))
    cases = (("full line", 343, 4, 16384, (9, 5, 49, 0), (9, 5, 50, 0),
              {"start_batch_put": {"duration_ns": 185000}, "submit": {"duration_ns": 9000}, "wait_for_transfer": {"duration_ns": 64000},
               "finalize": {"duration_ns": 180000}}),
             ("old-signature wrapper, empty batch (complete=false)", 1664, 0, 0, (8, 36, 3, 0), (8, 36, 4, 0),
              {"submit": {"duration_ns": 1000}, "wait_for_transfer": {"duration_ns": 1000}, "finalize": {"duration_ns": 9000}}))
    for k, (what, tid, keys, nbytes, t0, t1, want) in enumerate(cases):
        recs = {}
        for form, ln in (("glog 0.6", W1_LINES[k]), ("glog 0.4", W1_LINES[k + 2])):
            out = []
            a = N.Associator(lambda node, inst, payload: out.append(json.loads(payload)), window_s=60)
            a.add_task("n1", "P0", {"event": "pool_task", "operation": "save", "request_id": "chatcmpl-w1-%d-1234abcd" % k,
                                    "native_calls": [call(tid, keys, nbytes, (2026, 11, 19) + t0, (2026, 11, 19) + t1)]}, now=0)
            a.add_native("n1", ln, now=0)
            recs[form] = out
        st = {kk: v for kk, v in (recs["glog 0.6"][0]["stages"].items() if len(recs["glog 0.6"]) == 1 else [])
              if kk in ("start_batch_put", "submit", "wait_for_transfer", "finalize")}
        check("W1's %s: one record, %s; the 0.4 form gives the same record" % (
              what, "，".join("%s %d us" % (kk, v["duration_ns"] // 1000) for kk, v in want.items())),
              st == want and recs["glog 0.6"] == recs["glog 0.4"], json.dumps(recs))


def synthetic():
    out = []
    a = N.Associator(lambda node, inst, payload: out.append((node, inst, json.loads(payload))), window_s=60)
    rec = {"event": "pool_task", "operation": "save", "scope": "task", "request_id": "chatcmpl-x-1234abcd", "task_id": "t1",
           "native_calls": [call(77, 4, 100, (2026, 11, 18, 10, 0, 0, 0), (2026, 11, 18, 10, 0, 0, 900)),
                            call(77, 4, 100, (2026, 11, 18, 10, 0, 0, 1000), (2026, 11, 18, 10, 0, 0, 1900)),
                            {"operation": "mooncake_is_exist", "native_tid": 77}]}
    # another rank's thread (78), and a line of thread 77 just after the first call's end
    a.add_native("n1", line(11, 18, (10, 0, 0, 500), 78, 4, 100), now=0)
    a.add_native("n1", line(11, 18, (10, 0, 0, 950), 77, 4, 100), now=0)
    a.add_task("n1", "P0", rec, now=1)
    check("synthetic: no record while the calls lack their lines", out == [])
    a.add_native("n1", line(11, 18, (10, 0, 0, 400), 77, 4, 100, p=(1, 2, 3, 4)), now=2)
    a.add_native("n1", line(11, 18, (10, 0, 0, 1500), 77, 4, 100, p=(5, 6, 7, 8)), now=2)
    ok = len(out) == 1 and out[0][:2] == ("n1", "P0")
    st = out[0][2]["stages"] if ok else {}
    check("synthetic: both calls matched, phases summed (us -> ns)", ok and st.get("submit") == {"duration_ns": 8000}
          and st.get("wait_for_transfer") == {"duration_ns": 10000} and st.get("finalize") == {"duration_ns": 12000},
          json.dumps(out))
    check("synthetic: the record carries the request id, version 2, two calls",
          ok and out[0][2]["request_id"] == "chatcmpl-x-1234abcd" and out[0][2]["version"] == 2 and out[0][2]["native_calls"] == 2)
    # the year boundary: a call on 31 Dec 23:59:59.999 local, the line printed as 1231
    rec2 = {"event": "pool_task", "operation": "save", "request_id": "chatcmpl-y-1234abcd",
            "native_calls": [call(80, 1, 9, (2026, 12, 31, 23, 59, 59, 999000), (2027, 1, 1, 0, 0, 0, 500))]}
    a.add_task("n1", "P1", rec2, now=3)
    a.add_native("n1", line(12, 31, (23, 59, 59, 999500), 80, 1, 9), now=3)
    check("synthetic: a call across the new year matches", len(out) == 2 and out[1][1] == "P1", json.dumps(out[-1:]))
    # a task whose line never comes
    rec3 = {"event": "pool_task", "operation": "save", "request_id": "chatcmpl-z-1234abcd",
            "native_calls": [call(81, 1, 9, (2026, 11, 18, 11, 0, 0, 0), (2026, 11, 18, 11, 0, 0, 10))]}
    a.add_task("n1", "P0", rec3, now=10)
    a.add_native("n2", line(11, 18, (11, 0, 0, 5), 81, 1, 9), now=10)     # another machine: never matches
    a.expire(now=200)
    c = a.counts
    check("synthetic: expired task and unclaimed lines are counted",
          c["tasks_incomplete"] == 1 and c["lines_unmatched"] == 3, json.dumps(c))
    check("synthetic: a save task without calls into Mooncake (all keys already there) writes nothing",
          (a.add_task("n1", "P0", {"event": "pool_task", "operation": "save", "native_calls": []}) or True) and len(out) == 2)


def tid_fallback():
    """glog 0.7 prints another thread number. MOONCAKE_TID_FALLBACK=1: one fitting line of another thread is taken, two
    are left alone. Default (off): no line of another thread is taken."""
    out = []
    a = N.Associator(lambda node, inst, payload: out.append(json.loads(payload)), window_s=60, tid_fallback=True)
    rec = {"event": "pool_task", "operation": "save", "request_id": "chatcmpl-f-1234abcd",
           "native_calls": [call(90, 2, 20, (2026, 11, 18, 12, 0, 0, 0), (2026, 11, 18, 12, 0, 0, 900))]}
    a.add_native("n1", line(11, 18, (12, 0, 0, 400), 140001, 2, 20), now=0)
    a.add_task("n1", "P0", rec, now=0)
    check("tid fallback: the one fitting line of another thread is taken",
          len(out) == 1 and a.counts["matched_without_tid"] == 1, json.dumps(a.counts))
    out2 = []
    b = N.Associator(lambda node, inst, payload: out2.append(payload), window_s=60, tid_fallback=True)
    b.add_native("n1", line(11, 18, (12, 0, 0, 400), 140001, 2, 20), now=0)
    b.add_native("n1", line(11, 18, (12, 0, 0, 500), 140002, 2, 20), now=0)
    b.add_task("n1", "P0", rec, now=0)
    check("tid fallback: two fitting lines of other threads, none taken", out2 == [] and b.counts["matched_without_tid"] == 0)
    out3 = []
    c = N.Associator(lambda node, inst, payload: out3.append(payload), window_s=60)
    c.add_native("n1", line(11, 18, (12, 0, 0, 400), 140001, 2, 20), now=0)
    c.add_task("n1", "P0", rec, now=0)
    c.expire(now=200)
    check("tid fallback off (default): the fitting line of another thread is not taken",
          out3 == [] and c.counts["matched_calls"] == 0 and c.counts["lines_unmatched"] == 1, json.dumps(c.counts))


def rank_order():
    """The save task arrives first; then the timing lines of four TP ranks one by one, rank 0's last, all with the same
    keys, bytes and time. By default rank 0's call gets rank 0's own line, never another rank's."""
    rec = {"event": "pool_task", "operation": "save", "request_id": "chatcmpl-r-1234abcd", "tp_rank": 0,
           "native_calls": [call(90, 2, 20, (2026, 11, 18, 14, 0, 0, 0), (2026, 11, 18, 14, 0, 0, 900))]}
    others = [line(11, 18, (14, 0, 0, 400), tid, 2, 20, p=(9, 9, 9, 9)) for tid in (140001, 140002, 140003)]
    own = line(11, 18, (14, 0, 0, 450), 90, 2, 20, p=(1, 2, 3, 4))
    out = []
    a = N.Associator(lambda node, inst, payload: out.append(json.loads(payload)), window_s=60)
    a.add_task("n1", "P0", rec, now=0)
    for n in others:
        a.add_native("n1", n, now=1)
    check("rank order: no record while only other ranks' lines have arrived", out == [], json.dumps(out))
    a.add_native("n1", own, now=2)
    ok = len(out) == 1
    check("rank order: rank 0's call gets rank 0's own line (default, no fallback)",
          ok and out[0]["stages"].get("submit") == {"duration_ns": 2000} and a.counts["matched_without_tid"] == 0,
          json.dumps(out) + json.dumps(a.counts))
    a.expire(now=200)
    check("rank order: the other ranks' lines stay unclaimed and are counted", a.counts["lines_unmatched"] == 3,
          json.dumps(a.counts))
    out2 = []
    b = N.Associator(lambda node, inst, payload: out2.append(json.loads(payload)), window_s=60, tid_fallback=True)
    b.add_task("n1", "P0", rec, now=0)
    for n in others + [own]:
        b.add_native("n1", n, now=1)
    check("rank order: with MOONCAKE_TID_FALLBACK=1 the first other rank's line is taken (why it is off by default)",
          len(out2) == 1 and out2[0]["stages"].get("submit") == {"duration_ns": 9000}
          and b.counts["matched_without_tid"] == 1, json.dumps(out2) + json.dumps(b.counts))


def bytes_rule():
    """C10: bytes is not needed; it tells several fitting lines apart, a different byte count is counted."""
    out = []
    a = N.Associator(lambda node, inst, payload: out.append(payload), window_s=60)
    rec = {"event": "pool_task", "operation": "save", "request_id": "chatcmpl-b-1234abcd",
           "native_calls": [call(91, 3, 300, (2026, 11, 18, 13, 0, 0, 0), (2026, 11, 18, 13, 0, 0, 900))]}
    a.add_native("n1", line(11, 18, (13, 0, 0, 400), 91, 3, 999), now=0)
    a.add_task("n1", "P0", rec, now=0)
    check("C10: the one line of the thread with keys and time is taken although its bytes differ",
          len(out) == 1 and a.counts["bytes_mismatch"] == 1, json.dumps(a.counts))
    out2 = []
    b = N.Associator(lambda node, inst, payload: out2.append(json.loads(payload)), window_s=60, tid_fallback=True)
    b.add_native("n1", line(11, 18, (13, 0, 0, 300), 150001, 3, 111, p=(1, 1, 1, 1)), now=0)
    b.add_native("n1", line(11, 18, (13, 0, 0, 500), 150002, 3, 300, p=(2, 2, 2, 2)), now=0)
    b.add_task("n1", "P0", rec, now=0)
    check("C10: of two fitting lines of other threads, bytes picks the one", len(out2) == 1 and
          out2[0]["stages"]["submit"] == {"duration_ns": 2000} and b.counts["bytes_mismatch"] == 0, json.dumps(b.counts))
    out3 = []
    c = N.Associator(lambda node, inst, payload: out3.append(payload), window_s=60, tid_fallback=True)
    c.add_native("n1", line(11, 18, (13, 0, 0, 300), 150001, 3, 111), now=0)
    c.add_native("n1", line(11, 18, (13, 0, 0, 500), 150002, 3, 222), now=0)
    c.add_task("n1", "P0", rec, now=0)
    check("C10: two fitting lines and bytes tells neither: none taken", out3 == [] and c.counts["matched_calls"] == 0)


def rc1(path):
    lines = open(path, errors="replace").read().splitlines()
    natives = [l[l.find("I1"):] if not l.startswith("I") else l for l in lines if "[BatchPutTiming]" in l]
    recs = []
    for l in lines:
        i = l.find("[PD_DIAG] ")
        if i >= 0 and '"pool_task"' in l:
            recs.append(json.loads(l[i + 10:]))
    out = []
    a = N.Associator(lambda node, inst, payload: out.append(json.loads(payload)))
    for n in natives:
        a.add_native("p", n)
    for r in recs:
        a.add_task("p", "P0", r)
    saves = [r for r in recs if r.get("operation") == "save" and r.get("native_calls")]
    check("rc1: one timing line, one save task with a call into Mooncake", len(natives) == 1 and len(saves) == 1)
    ok = len(out) == 1
    st = out[0]["stages"] if ok else {}
    check("rc1: the line matches the call (thread, keys, bytes, time)", ok, json.dumps(a.counts))
    check("rc1: submit / wait_for_transfer / finalize = phases 2 / 3 / 4 (179 / 824 / 142 us)",
          st.get("submit") == {"duration_ns": 179000} and st.get("wait_for_transfer") == {"duration_ns": 824000}
          and st.get("finalize") == {"duration_ns": 142000}, json.dumps(st))
    check("rc1: the record names the save task's request", ok and out[0]["request_id"] == saves[0]["request_id"]
          and out[0]["timing_scopes"] == ["prefer_same_node_full"])


def fetch_scripts():
    probe = subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if probe.returncode:
        print("SKIP fetch scripts: docker is not available")
        return
    for image in ("python:3.12-slim", "alpine:latest"):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "vllm.log")

            def run(state, first, image=image, d=d):
                script = N.file_script(["/x/vllm*.log"], state, first)
                r = subprocess.run(["docker", "run", "--rm", "-i", "--network", "none", "-v", d + ":/x", image, "sh", "-s"],
                                   input=script.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
                return N.parse_file_output(r.stdout.decode()), r.stderr.decode()

            open(f, "w").write("old line\n" + line(11, 18, (1, 0, 0, 0), 5, 1, 1) + "\n")
            (offs, files, lines), err = run({}, True)
            size = os.path.getsize(f)
            check("%s: the first round starts at the end" % image, lines == [] and list(offs.values()) == [size], err + str(offs))
            with open(f, "a") as fh:
                fh.write("vLLM INFO something\n" + line(11, 18, (1, 0, 1, 0), 5, 2, 2) + "\n" + "partial [BatchPutTiming] keys=")
            (offs2, _, lines2), err = run(offs, False)
            ino = list(offs2)[0]
            check("%s: new complete timing lines only, the line being written waits" % image,
                  len(lines2) == 1 and "keys=2 " in lines2[0] and offs2[ino] == os.path.getsize(f) - len("partial [BatchPutTiming] keys="),
                  err + str(lines2) + str(offs2))
            with open(f, "a") as fh:
                fh.write("3 bytes=3 phase2_submit_transfers_us=1\n")
            (offs3, _, lines3), err = run(offs2, False)
            check("%s: the finished line comes in the next round" % image,
                  lines3 == ["partial [BatchPutTiming] keys=3 bytes=3 phase2_submit_transfers_us=1"], err + str(lines3))
            open(f, "w").write(line(11, 18, (1, 0, 2, 0), 5, 4, 4) + "\n")      # truncated and rewritten (same inode)
            (offs4, _, lines4), err = run(offs3, False)
            check("%s: a truncated file is read again from its start" % image, len(lines4) == 1 and "keys=4 " in lines4[0],
                  err + str(lines4))
            g = os.path.join(d, "vllm2.log")
            open(g, "w").write(line(11, 18, (1, 0, 3, 0), 6, 5, 5) + "\n")       # a file that appears later
            (offs5, files5, lines5), err = run(offs4, False)
            check("%s: a file that appears after the first round is read from its start" % image,
                  len(lines5) == 1 and "keys=5 " in lines5[0] and len(offs5) == 2, err + str(lines5))


def main():
    synthetic()
    glog06()
    w1_lines()
    tid_fallback()
    rank_order()
    bytes_rule()
    if len(sys.argv) > 1:
        rc1(sys.argv[1])
    else:
        print("SKIP rc1: no P log given")
    fetch_scripts()
    print("%d FAIL" % len(FAILS))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
