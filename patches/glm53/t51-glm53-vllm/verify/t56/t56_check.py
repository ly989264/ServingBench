"""kv_ready_wait in the D records of d_equiv.py: the equalities the panel and the site check rely on.
usage: python3 t56_check.py DUMP.jsonl"""
import collections
import json
import sys

KEYS = {"calls", "duration_ns", "start_ns", "end_ns", "status", "exceptions", "measured_calls", "skipped_calls",
        "unmeasured_calls", "ranges", "dropped_ranges", "complete", "end_observed", "timing_scope"}
by = collections.defaultdict(list)
for line in open(sys.argv[1]):
    o = json.loads(line)
    if "rec" in o:
        by[o["scenario"]].append(o["rec"])
problems = 0


def bad(s, msg):
    global problems
    problems += 1
    print(f"  PROBLEM {s}: {msg}")


for s, recs in sorted(by.items()):
    sched = {r["execution_id"]: r["stages"].get("schedule") for r in recs
             if r["event"] == "core_summary" and r.get("scope") == "execution"}
    reqs = [r for r in recs if r["event"] == "core_summary" and r.get("scope") == "request"]
    with_stage = [r for r in reqs if "kv_ready_wait" in r["stages"]]
    statuses = collections.Counter()
    for r in with_stage:
        st = r["stages"]["kv_ready_wait"]
        rid = r["request_id"]
        if set(st) != KEYS:
            bad(s, f"{rid} keys {sorted(set(st) ^ KEYS)}")
        first = st["ranges"][0]
        for k in ("start_ns", "end_ns", "duration_ns", "status"):
            if st[k] != first[k]:
                bad(s, f"{rid} top-level {k} {st[k]} != first round {first[k]}")
        if st["calls"] != len(st["ranges"]) + st["dropped_ranges"]:
            bad(s, f"{rid} calls {st['calls']} != rounds")
        for rg in st["ranges"]:
            statuses[rg["status"]] += 1
            if rg["status"] == "ok":
                start = sched.get(rg["scheduled_execution_id"])
                if start is None:
                    bad(s, f"{rid} no execution record for {rg['scheduled_execution_id']}")
                    continue
                if start["start_ns"] != rg["end_ns"]:
                    bad(s, f"{rid} end_ns {rg['end_ns']} != schedule.start_ns of {rg['scheduled_execution_id']}")
                if not (rg["start_ns"] <= rg["end_ns"] and rg["duration_ns"] == rg["end_ns"] - rg["start_ns"]):
                    bad(s, f"{rid} start/end/duration")
                if rg["ready_execution_id"] not in sched:
                    bad(s, f"{rid} ready_execution_id has no execution record")
                elif sched[rg["ready_execution_id"]]["start_ns"] >= rg["end_ns"]:
                    bad(s, f"{rid} ready step does not precede the scheduled step")
                if rg["promoted_ns"] is None or rg["promoted_ns"] < rg["start_ns"] or (
                        rg["promoted_ns"] > start["end_ns"]):
                    bad(s, f"{rid} promoted_ns outside ready .. end of the scheduling call")
    starts = collections.Counter("start" if rg["start_ns"] is not None else "no start"
                                 for r in with_stage for rg in r["stages"]["kv_ready_wait"]["ranges"])
    print(f"== {s}: {len(reqs)} request records, {len(with_stage)} with kv_ready_wait, rounds {dict(statuses)}"
          f" {dict(starts)}")
print("problems:", problems)
