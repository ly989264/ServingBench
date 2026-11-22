#!/usr/bin/env python3
"""T56: builds the two segment definitions of the D side's new segments from the 19c glm53 profile, as
docs/tasks/T56/32_精简落地决定 section five and the record format 1.4.0 (10_, frozen de58c746) define them; which one a
cluster uses is its SEGMENTS_PROFILE (the switch; the user picks):
  profiles/glm53      甲 (the advisor's): start_load_kv takes the receive thread's wall-clock envelope
                      (request_generation.kv_load_envelope_ns); its four L2s, maxima over the tasks, overlap and need not
                      add up to it. 3 comparison rows.
  tests/t56/profile-glm53-sum  乙: start_load_kv stays the sum of its L2s; a 4th comparison row shows the envelope. The
                      user picked 甲 (11-20): 乙 is not in the pack, kept here for the tests.
Both: D's 「到第一个 token」 gets under start_load_kv 接收排队 (task_max_queue_wait_ns) and 准备读表 (task_max_meta_fetch_ns +
task_max_prepare_ns, two fields of the one record added up), between submit and wait_for_transfer; 等调度
(core_summary[request] stages.kv_ready_wait) as an L1 after start_load_kv; the first step and the later steps each get
输入准备, lm_head, MTP 草稿 (device time), in a step's order: 输入准备, forward, lm_head, sampling, MTP 草稿. Comparison rows
等 KV / 第一步 / 第二步起 (vLLM 口径), from D's api_summary[request].official, in seconds. Five rules no segment reads
(10_ section 5 end, 「只记录的字段」; 32_ section four: for the first-batch check and for deciding later whether 记账 goes on the
dashboard; not on the dashboard or in coverage): step_device_span (step_device_span_ns of the sample record); graph_full,
graph_piecewise, graph_none (the execute records with that cudagraph_runtime_mode, counted by their steps); bookkeeping
(the device time of 记账, as the step segments take theirs; a segment can later read it with {"acc": "bookkeeping"}).
P is not changed.

  python3 tests/t56/make_profiles.py <19c segments.json> <pack folder>
"""
import copy
import json
import pathlib
import sys

CONFIG_ID = {"glm53": "pd-diag-v3.2-extra-glm53-2026-11-20a", "glm53-sum": "pd-diag-v3.2-extra-glm53sum-2026-11-20a"}
RG = {"event": "pd_transfer_summary", "where": {"scope": "request_generation"}}
API_REQ = {"event": "api_summary", "where": {"scope": "request"}}
NEW_STEP = ("prepare_inputs", "lm_head", "draft")
PHASE = {"prepare_inputs": "execute", "lm_head": "execute", "draft": "sample"}
# display names: the code name, written as the existing segments are (the user's rule, 11-20): a step segment as forward's
# 「forward（设备侧，第一步）」; no Chinese name (the meaning goes into the delivery note)
LABEL = {"prepare_inputs": "prepare_inputs（", "lm_head": "lm_head（", "draft": "draft（"}
# rules no segment reads (10_ section 5 end); ids as the first-batch check reads them (acc__<id>__ns, __steps, __first_ns)
HIDDEN = [{"id": "step_device_span", "role": "worker", "event": "worker_summary", "where": {"phase": "sample"},
           "key": "request_ids", "value": "step_device_span_ns"}] + [
    {"id": "graph_" + m.lower(), "role": "worker", "event": "worker_summary", "where": {"phase": "execute", "cudagraph_runtime_mode": m},
     "key": "request_ids", "value": "stages.forward.duration_ns"} for m in ("FULL", "PIECEWISE", "NONE")] + [
    {"id": "bookkeeping", "role": "worker", "event": "worker_summary", "where": {"phase": "sample"},
     "key": "request_ids", "value": ["stages.bookkeeping.device_duration_ns"]}]


def src_rg(*fields):
    return [dict(RG, field=f) for f in fields]


def step_node(stage, part):
    return {"id": stage, "label": "%s设备侧，%s）" % (LABEL[stage], "第一步" if part == "first" else "第二步起"), "proc": "worker",
            "src": [{"acc": stage, "part": part}]}


def build(base, variant):
    d = copy.deepcopy(base)
    d["config_id"] = CONFIG_ID[variant]
    ids = {a["id"] for a in d["accumulate"]}
    for st in NEW_STEP:
        assert st not in ids, st
        d["accumulate"].append({"id": st, "role": "worker", "event": "worker_summary", "where": {"phase": PHASE[st]},
                                "key": "request_ids", "value": ["stages.%s.device_duration_ns" % st]})
    d["accumulate"] += copy.deepcopy(HIDDEN)
    dec = d["sides"]["decode"]
    ttft, tpot = dec["ttft"], dec["tpot"]
    # start_load_kv: submit, 接收排队, 准备读表, wait_for_transfer; 甲 takes the envelope itself
    slk = next(n for n in ttft if n["id"] == "start_load_kv")
    sub = {c["id"]: c for c in slk["children"]}
    assert set(sub) == {"submit", "wait_for_transfer"} and "src" not in slk, slk
    slk["children"] = [
        sub["submit"],
        {"id": "receive_queue", "label": "receive_queue", "proc": "worker", "src": src_rg("task_max_queue_wait_ns")},
        {"id": "prepare_read", "label": "prepare_read（meta_fetch + prepare）", "proc": "worker", "src": src_rg("task_max_meta_fetch_ns", "task_max_prepare_ns")},
        sub["wait_for_transfer"],
    ]
    after = []
    if variant == "glm53":
        slk["src"] = src_rg("kv_load_envelope_ns")
    else:
        after.append({"id": "recv_envelope", "label": "kv_load_envelope_ns", "proc": "worker", "compare": True, "src": src_rg("kv_load_envelope_ns")})
    after += [
        {"id": "kv_ready_wait", "label": "kv_ready_wait", "proc": "engine",
         "src": [{"event": "core_summary", "where": {"scope": "request"}, "field": "stages.kv_ready_wait.duration_ns"}]},
        {"id": "kv_wait_vllm", "label": "queued_time (vLLM)", "proc": "api", "compare": True,
         "src": [dict(API_REQ, field="official.queued_time", unit="s")]},
    ]
    i = ttft.index(slk)
    ttft[i + 1:i + 1] = after
    # the first step: 输入准备, forward, lm_head, sampling, MTP 草稿, then the first-step comparison, detokenizer
    assert [n["id"] for n in ttft][-3:] == ["forward", "sampling", "detokenizer"]
    fwd, smp, det = ttft[-3:]
    ttft[-3:] = [step_node("prepare_inputs", "first"), fwd, step_node("lm_head", "first"), smp, step_node("draft", "first"),
                 {"id": "first_step_vllm", "label": "prefill_time (vLLM)", "proc": "api", "compare": True,
                  "src": [dict(API_REQ, field="official.prefill_time", unit="s")]}, det]
    # later steps: schedule, 输入准备, forward, lm_head, sampling, MTP 草稿, detokenizer, then the comparison
    assert [n["id"] for n in tpot] == ["schedule", "forward", "sampling", "detokenizer"]
    sch, fwd, smp, det = tpot
    dec["tpot"] = [sch, step_node("prepare_inputs", "rest"), fwd, step_node("lm_head", "rest"), smp, step_node("draft", "rest"), det,
                   {"id": "rest_vllm", "label": "decode_time (vLLM)", "proc": "api", "compare": True,
                    "src": [dict(API_REQ, field="official.decode_time", unit="s")]}]
    return d


def main():
    base_path, pack = sys.argv[1], pathlib.Path(sys.argv[2])
    base = json.load(open(base_path, encoding="utf-8"))
    assert base["config_id"] == "pd-diag-v3.1-extra-glm53-2026-11-19c", base["config_id"]
    for variant in ("glm53", "glm53-sum"):
        # 甲 is the pack's (the user picked it, 11-20); 乙 stays in the repository for the tests, outside the pack
        out = (pack / "profiles" / variant / "segments.json") if variant == "glm53" else \
            (pathlib.Path(__file__).resolve().parent / "profile-glm53-sum" / "segments.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(build(base, variant), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("wrote", out)


if __name__ == "__main__":
    main()
