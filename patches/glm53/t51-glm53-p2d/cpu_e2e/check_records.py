"""Summarise and check the raw [PD_DIAG] records of one case: python3 check_records.py <case dir> > report.json

Reads every *.log in the case dir written by run_case.sh (the console copy of the vllm logger, one per server),
groups records by server, process (APIServer / EngineCore / Worker, from the "(name pid=N)" prefix), event and
scope/phase, and checks:
  common   every record has version 2 and the common fields (host, pid, native_tid, run_id, timestamp_ns, record_id)
  chain    every core_summary[scope=execution] that scheduled tokens has exactly one worker_summary[phase=execute]
           with the same execution_id and the same request_ids, and one worker_summary[phase=sample]; no worker
           record names an execution_id the EngineCore did not write
  queue    every core_summary[scope=request] whose stages.queue.ranges is not empty starts at a step of this run
  fields   presence of the stage fields the panel reads (counts of records with each one)
"""

import collections
import glob
import gzip
import json
import os
import re
import sys

LINE = re.compile(r"\((?P<proc>[^ )]+)[^)]*pid=(?P<pid>\d+)\).*?\[PD_DIAG\] (?P<j>\{.*\})\s*$")
COMMON = ("event", "version", "host", "pid", "native_tid", "run_id", "timestamp_ns", "record_id")
FIELDS = {
    ("api_summary", "http"): ["stages.req_resolve.duration_ns", "stages.render.duration_ns", "stages.tokenize.duration_ns"],
    ("api_summary", "engine_submission"): ["stages.add_request.duration_ns"],
    ("api_summary", "request"): ["stages.detokenize.duration_ns", "terminal.detokenize_duration_ns"],
    ("core_summary", "request"): ["stages.queue.initial_wait_ns", "stages.hash.duration_ns"],
    ("core_summary", "execution"): ["execution_id", "request_ids", "stages.lookup.duration_ns", "stages.alloc.duration_ns",
                                    "stages.other.duration_ns"],
    ("worker_summary", "execute"): ["execution_id", "request_ids", "tp_rank", "runner", "stages.forward.duration_ns",
                                    "stages.submit.duration_ns"],
    ("worker_summary", "sample"): ["execution_id", "stages.sampling.duration_ns"],
}


def get(d, path):
    for part in path.split("."):
        if not isinstance(d, dict) or part not in d:
            return None
        d = d[part]
    return d


def kind(proc):
    for k in ("APIServer", "EngineCore", "Worker"):
        if k in proc:
            return k
    return "API(main)" if "MainProcess" in proc or proc.startswith("main") else proc


case = sys.argv[1]
report = {"case": os.path.basename(case.rstrip("/")), "servers": {}}
for path in sorted(glob.glob(os.path.join(case, "*.log")) + glob.glob(os.path.join(case, "*.log.gz"))):
    server = os.path.basename(path).split(".log")[0]
    if server == "proxy":
        continue
    recs = []
    for line in (gzip.open(path, "rt", errors="replace") if path.endswith(".gz") else open(path, errors="replace")):
        m = LINE.search(line)
        if m:
            try:
                rec = json.loads(m.group("j"))
            except ValueError:
                continue
            rec["_proc"] = kind(m.group("proc"))
            recs.append(rec)
    counts = collections.Counter(
        f"{r['_proc']} {r.get('event')}[{r.get('scope') or r.get('phase') or '-'}]" for r in recs)
    checks = {}
    missing_common = [r.get("record_id") for r in recs if any(k not in r for k in COMMON) or r.get("version") != 2]
    checks["common"] = {"ok": not missing_common, "records": len(recs), "bad": missing_common[:5]}
    execs = {r["execution_id"]: r for r in recs if r.get("event") == "core_summary" and r.get("scope") == "execution"}
    wexec = collections.defaultdict(list)
    wsample = collections.defaultdict(list)
    for r in recs:
        if r.get("event") != "worker_summary" or (r.get("tp_rank") or 0) != 0:
            continue
        if r.get("phase") == "execute":
            wexec[r.get("execution_id")].append(r)
        elif r.get("phase") == "sample":
            wsample[r.get("execution_id")].append(r)
    bad = []
    for xid, c in execs.items():
        scheduled = c.get("total_num_scheduled_tokens") or 0
        if scheduled <= 0:
            continue
        ex = wexec.get(xid, [])
        if len(ex) != 1:
            bad.append(f"{xid}: {len(ex)} worker execute records")
        elif sorted(ex[0].get("request_ids") or []) != sorted(c.get("request_ids") or []):
            bad.append(f"{xid}: request_ids differ")
        if len(wsample.get(xid, [])) != 1:
            bad.append(f"{xid}: {len(wsample.get(xid, []))} worker sample records")
    orphans = sorted(set(wexec) - set(execs)) + sorted(set(wsample) - set(execs))
    checks["chain"] = {"ok": not bad and not orphans and bool(execs), "engine_steps": len(execs),
                       "steps_with_tokens": sum(1 for c in execs.values() if (c.get("total_num_scheduled_tokens") or 0) > 0),
                       "worker_execute": sum(len(v) for v in wexec.values()), "worker_sample": sum(len(v) for v in wsample.values()),
                       "problems": bad[:10], "orphan_worker_ids": orphans[:5]}
    qbad = []
    for r in recs:
        if r.get("event") == "core_summary" and r.get("scope") == "request":
            ranges = get(r, "stages.queue.ranges") or []
            if ranges and ranges[0].get("execution_id") not in execs:
                qbad.append(r.get("request_id"))
    checks["queue"] = {"ok": not qbad, "bad": qbad[:5]}
    fields = {}
    for (event, scope), names in FIELDS.items():
        group = [r for r in recs if r.get("event") == event and (r.get("scope") == scope or r.get("phase") == scope)]
        fields[f"{event}[{scope}]"] = {"records": len(group), **{n: sum(get(r, n) is not None for r in group) for n in names}}
    checks["fields"] = fields
    statuses = collections.Counter(
        str(get(r, "terminal.status") or get(r, "terminal.finish_reason") or r.get("status"))
        for r in recs if r.get("event") == "api_summary" and r.get("scope") == "request")
    report["servers"][server] = {"records": len(recs), "counts": dict(sorted(counts.items())), "checks": checks,
                                 "api_request_status": dict(statuses)}
json.dump(report, sys.stdout, indent=1, ensure_ascii=False)
print()
