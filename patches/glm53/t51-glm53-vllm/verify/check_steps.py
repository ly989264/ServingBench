"""Per-step invariants on a pd_equiv.py dump (engine scenarios E1-E7).

1. Scheduler.schedule() sets SchedulerOutput.pd_execution_id on every step,
   before EngineCore's fallback; ids are unique.
2. Exactly one core_summary[execution] per step, same id; one
   worker_summary[execute] per step, same id; sample records only for those ids.
3. Every transported output carries the id of a step that scheduled it.
4. Async scenarios: the next step is scheduled while the previous one is still
   in flight (its core_summary is written later than the next schedule starts).
5. E7: with a synchronous load the submit stage ends before forward starts and
   covers the 20 ms load; otherwise submit starts after forward ends.

usage: python3 check_steps.py DUMP.jsonl   (exit 1 on any failure)
"""
import collections
import json
import sys

recs = [json.loads(line) for line in open(sys.argv[1])]
failures = 0


def check(cond, msg):
    global failures
    if not cond:
        failures += 1
    print(("  ok   " if cond else "  FAIL ") + msg)


by = collections.defaultdict(list)
for o in recs:
    by[o["scenario"]].append(o)
for scen in sorted(s for s in by if s.startswith("E")):
    items = by[scen]
    if any("driver_error" in o for o in items):
        print(f"== {scen}: driver error")
        failures += 1
        continue
    rids = {o["rec"]["execution_id"]: o["rec"]["request_ids"] for o in items if "rec" in o
            and o["rec"]["event"] == "worker_summary" and o["rec"].get("phase") == "execute"}
    sched_ids = [o["step"]["execution_id"] for o in items
                 if "step" in o and o["step"]["seen_by"] == "scheduler.schedule"]
    exec_steps = [o["step"] for o in items if "step" in o and o["step"]["seen_by"] == "execute_model"]
    core_exec = [o["rec"] for o in items if "rec" in o and o["rec"]["event"] == "core_summary"
                 and o["rec"].get("scope") == "execution"]
    w_exec = [o["rec"] for o in items if "rec" in o and o["rec"]["event"] == "worker_summary"
              and o["rec"].get("phase") == "execute"]
    w_sample = [o["rec"] for o in items if "rec" in o and o["rec"]["event"] == "worker_summary"
                and o["rec"].get("phase") == "sample"]
    outs = [x for o in items if "transport" in o for x in o["transport"]["outputs"]]
    print(f"== {scen}: {len(sched_ids)} steps, {len(core_exec)} core_summary[execution], "
          f"{len(w_exec)} worker execute, {len(w_sample)} worker sample, {len(outs)} outputs")
    check(sched_ids and all(i is not None for i in sched_ids),
          "pd_execution_id set by Scheduler.schedule() on every step")
    check(len(set(sched_ids)) == len(sched_ids), "pd_execution_id unique per step")
    check([s["execution_id"] for s in exec_steps] == sched_ids,
          "execute_model receives each step's id, in schedule order")
    check(sorted(r["execution_id"] for r in core_exec) == sorted(sched_ids),
          "exactly one core_summary[execution] per step, same id")
    check(sorted(r["execution_id"] for r in w_exec) == sorted(sched_ids),
          "exactly one worker_summary[execute] per step, same id")
    # EngineCore calls sample_tokens only for steps that ran the model
    # (total scheduled tokens > 0); each such step gets exactly one sample record.
    sampled_steps = [s["execution_id"] for s in exec_steps if s["total_tokens"] > 0]
    check(sorted(r["execution_id"] for r in w_sample) == sorted(sampled_steps),
          f"exactly one worker_summary[sample] per step that ran the model, same id "
          f"({len(sampled_steps)} steps)")
    check(all(r["request_ids"] == rids[r["execution_id"]] for r in w_sample if r["execution_id"] in rids),
          "sample record lists the same request_ids as its execute record")
    step_reqs = {s["execution_id"]: set(s["request_ids"]) for s in exec_steps}
    check(all(o["pd_execution_id"] in step_reqs and o["request_id"] in step_reqs[o["pd_execution_id"]]
              for o in outs if o["n_tokens"]),
          "every output with tokens carries the id of a step that scheduled its request")
    by_id = {r["execution_id"]: r for r in core_exec}
    check(all(by_id[i]["request_ids"] == rids[i] for i in sched_ids),
          "core and worker records of a step list the same request_ids")
    if "async" in scen:
        overlap = sum(
            1 for a, b in zip(sched_ids, sched_ids[1:])
            if by_id[b]["stages"]["schedule"]["start_ns"] < by_id[a]["timestamp_ns"]
        )
        check(overlap > 0, f"next step scheduled before the previous step's summary was written: {overlap} times")
    if scen.startswith("E7"):
        sync_ids = {s["execution_id"] for s in exec_steps if s["has_sync_kv_loads"]}
        check(bool(sync_ids), f"steps with synchronous loads: {len(sync_ids)}")
        for r in w_exec:
            st = r["stages"]
            if "submit" not in st or "forward" not in st:
                continue
            sub, fwd = st["submit"], st["forward"]
            if r["execution_id"] in sync_ids:
                check(sub["end_ns"] <= fwd["start_ns"] and sub["duration_ns"] >= 20_000_000,
                      f"sync load step: submit before forward, {sub['duration_ns'] / 1e6:.1f} ms >= 20 ms")
            else:
                check(sub["start_ns"] >= fwd["end_ns"],
                      "step without sync load: submit after forward")
print("failures:", failures)
sys.exit(1 if failures else 0)
