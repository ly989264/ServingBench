"""Per request: engine members[].executions vs worker execute records with a
forward stage (T50 rule: rank 0, phase=execute, integer stages.forward.duration_ns).
usage: python3 mtp_executions.py DUMP.jsonl"""
import collections, json, sys
recs = [json.loads(l) for l in open(sys.argv[1])]
for scen in ("E1_engine_sync_prefill_decode", "E2_engine_remote_kv_spec5", "E3_engine_batch_queue", "E5_engine_async_scheduling", "E6_engine_async_spec5", "E7_engine_sync_kv_load"):
    rs = [o["rec"] for o in recs if o.get("scenario") == scen and "rec" in o]
    eng, steps, spec, toks = {}, collections.Counter(), collections.Counter(), collections.Counter()
    for r in rs:
        if r["event"] == "core_summary" and r.get("scope") == "request":
            m = r["members"][0]
            eng[m["request_id"]] = m["executions"]
        if r["event"] == "core_summary" and r.get("scope") == "execution":
            for m in r.get("members", []):
                spec[m["request_id"]] = max(spec[m["request_id"]], m["num_spec_tokens"])
        if (r["event"] == "worker_summary" and r.get("phase") == "execute" and r.get("tp_rank", 0) == 0
                and isinstance(r.get("stages", {}).get("forward", {}).get("duration_ns"), int)):
            for rid in r["request_ids"]:
                steps[rid] += 1
    for o in recs:
        if o.get("scenario") == scen and "transport" in o:
            for out in o["transport"]["outputs"]:
                toks[out["request_id"]] += out["n_tokens"]
    for rid in sorted(eng):
        print(f"{scen} request={rid} executions={eng[rid]} forward_steps={steps[rid]} "
              f"max_spec_tokens={spec[rid]} output_tokens={toks[rid]} equal={eng[rid] == steps[rid]}")
