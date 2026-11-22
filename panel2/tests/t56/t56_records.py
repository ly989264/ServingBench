#!/usr/bin/env python3
"""T56: what the three T56 patches add to D's records (record format glm5.3 1.4.0, docs/tasks/T56/10_, frozen sha16
91242fe1), synthesized on a D log of the 1.3.3 shape for the panel's tests. P's records are left as they are.

augment(lines, cases) -> (lines, info). lines: the log's lines in order ([PD_DIAG] records and anything else).
  - Receive thread (02): a request's first rank-0 reading task (pd_transfer_summary[task]) gets 15 more tasks of the same
    step (16 = P's TP ports x shards on test-glm53), each with its own queue_wait_ns, associated_stages.meta_fetch and
    .prepare and stages.wait_for_transfer; three of them have associated_stages.metadata_request, inside meta_fetch. Each
    of the three maxima comes from another task (5, 9, 13). The new tasks read inside the real task's native read, so the
    request's transfer_envelope_ns stays the same, and finish before it: the real task completes the request
    (tracker_completed) and has request_done_ns, the other 15 have null. The request's rank-0 request_generation gets the
    three maxima over its 16 tasks (F3: task_max_queue_wait_ns, task_max_meta_fetch_ns, task_max_prepare_ns, as the
    connector computes them), kv_load_envelope_ns (request_done_ns - the earliest queued_ns) and kv_load_timing_scope;
    a rank-1 copy of it with ten times those values and transfer_envelope_ns follows, on the line of a Worker process
    of its own ("(Worker_TP1 pid=<a new pid>)", as with TP > 1, where the filter keeps rank 0 only). With TP1 the Worker
    runs in the EngineCore process: its lines are the engine's, which the filter does not sort by rank (there is only
    rank 0 then).
  - The scheduler's wait (01): core_summary[request] of a request that read from P gets stages.kv_ready_wait, from 1.2 ms
    after the request was marked done (the scheduler sees finished_recving; less when the step comes sooner) to the start
    of schedule() of the step that runs it (the first core_summary[execution] naming it): end_ns = that step's
    stages.schedule.start_ns. A request that did not read from P has none.
  - The runner (03): worker_summary records that ran the target model get prepare_inputs and lm_head (execute),
    bookkeeping and draft (sample), each with the host time and, on rank 0, a device time, distinct per record; the
    rank-0 execute record gets cudagraph_runtime_mode (PIECEWISE on a step that is a request's first, FULL otherwise;
    NONE on the first step of cases["graph_none"]);
    the rank-0 sample record gets step_device_span_ns (prepare_inputs' start to draft's end: 150 us and more above the step's device times) when both
    have a device time.
cases, request ids (each may be left out):
  "env_null": kv_load_envelope_ns null (the connector did not mark it done); request_done_ns null on its tasks;
  "E1": the first step of this request after its second gets step_device_span_ns 1 us below its device times;
  "E2": kv_ready_wait.end_ns 5 us after the step's schedule.start_ns;
  "E3": kv_load_envelope_ns 0.1 ms below transfer_envelope_ns (so below the largest of the three maxima too);
  "E4": kv_ready_wait long enough that the queue time + kv_load_envelope_ns + kv_ready_wait exceed vLLM's queued_time
        by 1 ms;
  "E5": vLLM's prefill_time 5 ms below the request's first-step device times (the host variant leaves two of the six
        out of the D total, about 1 ms: still below);
  "host_only_suffix": execution ids ending so (the copy whose Worker records have the host time only, C15): the four
        new stages have no device time either;
  "host_only_record": (request id, n): the execute record of the request's n-th step (0-based) has prepare_inputs with
        the host time only;
  "graph_none": the request whose first step ran without a graph (cudagraph_runtime_mode NONE);
  "big_prep": the request whose first step has a large input preparation (40 ms device time, 45 ms host time).
info: what was made, for the tests' own checks.
"""
import collections
import json
import re

TAG = "[PD_DIAG] "
PROC = re.compile(r"^\((\w+) pid=(\d+)\)")
F3 = (("queue_wait_ns", "task_max_queue_wait_ns"), ("meta_fetch", "task_max_meta_fetch_ns"), ("prepare", "task_max_prepare_ns"))
ENV_SCOPE = "first_task_queued_to_request_marked_done"
N_TASKS = 16
STEP_DEV = ("prepare_inputs", "forward", "lm_head", "sampling", "bookkeeping", "draft")


def parse(line):
    i = line.find(TAG)
    if i < 0:
        return None, None
    return line[:i + len(TAG)], json.loads(line[i + len(TAG):])


def dump(pre, o):
    return pre + json.dumps(o, separators=(",", ":")) + "\n"


def stage(start, dur, dev):
    """A stage as Batch.add_stage writes it; device_duration_ns only when there is one."""
    s = {"calls": 1, "duration_ns": dur, "start_ns": start, "end_ns": start + dur, "status": "ok", "exceptions": 0,
         "measured_calls": 1, "skipped_calls": 0, "unmeasured_calls": 0}
    if dev is not None:
        s["device_duration_ns"] = dev
    return s


def assoc(start, dur):
    """An associated stage of a task record (10_ 6.1: no measured_calls keys)."""
    return {"calls": 1, "duration_ns": dur, "exceptions": 0, "start_ns": start, "end_ns": start + dur, "status": "ok"}


def mult(j):
    """Task j (1..15) against the real task: queue wait, meta fetch, prepare, read. Each maximum is another task's."""
    return (1.6 if j == 5 else 0.4 + 0.05 * j, 1.7 if j == 9 else 0.5 + 0.03 * j, 1.4 if j == 13 else 0.6 + 0.02 * j,
            0.3 + 0.02 * j)


def synth_tasks(t0, done):
    """The 15 tasks next to t0, each ending (20 us after its read) before the request is marked done."""
    a0 = t0.get("associated_stages") or {}
    calls = t0.get("native_calls") or []
    n0s = min(c["start_ns"] for c in calls) if calls else t0["start_ns"]
    n0e = max(c["end_ns"] for c in calls) if calls else t0["end_ns"]
    qw0 = t0.get("queue_wait_ns") or 100000
    mf0 = (a0.get("meta_fetch") or {}).get("duration_ns") or 20000
    pr0 = (a0.get("prepare") or {}).get("duration_ns") or 200000
    rd0 = ((t0.get("stages") or {}).get("wait_for_transfer") or {}).get("duration_ns") or (n0e - n0s)
    out = []
    for j in range(1, N_TASKS):
        qm, fm, pm, rm = mult(j)
        q = t0["queued_ns"] + 1000 * j
        qw, mf, pr = int(qw0 * qm), int(mf0 * fm), int(pr0 * pm)
        st = q + qw
        mf_s = st + 1000
        pr_s = mf_s + mf + 1000
        rd_s = max(pr_s + pr + 1000, n0s)
        rd = min(int(rd0 * rm), n0e - rd_s - 1000, done - rd_s - 40000)
        if rd <= 0:
            raise SystemExit("T56 合成：请求 %s 的第 %d 个任务放不进真实任务的读取窗口（准备 %d ns）" % (t0["request_id"], j, pr))
        end = rd_s + rd + 20000
        t = json.loads(json.dumps(t0))
        t.update(task_id="%s-t%02d" % (t0.get("task_id") or "task", j), offset=j, queued_ns=q, submitted_ns=q + 100,
                 start_ns=st, queue_wait_ns=qw, end_ns=end, processing_duration_ns=end - st, transfer_envelope_ns=rd,
                 final_task=False, tracker_completed=False, request_done_ns=None, timestamp_ns=(t0.get("timestamp_ns") or end) - 1000 * (N_TASKS - j))
        if t.get("record_id"):
            t["record_id"] = "%s-t%02d" % (t0["record_id"], j)
        t["associated_stages"] = {"meta_fetch": assoc(mf_s, mf), "prepare": assoc(pr_s, pr)}
        if j in (3, 9, 11):        # the peer's metadata was not cached: fetched inside meta_fetch
            t["associated_stages"]["metadata_request"] = assoc(mf_s + 500, mf * 7 // 10)
        t["stages"] = {"wait_for_transfer": dict((t0.get("stages") or {}).get("wait_for_transfer") or {}, duration_ns=rd, start_ns=rd_s, end_ns=rd_s + rd)}
        if calls:
            c = dict(calls[0], start_ns=rd_s, end_ns=rd_s + rd, duration_ns=rd, call_id="%s-t%02d" % (calls[0].get("call_id"), j))
            if isinstance(calls[0].get("start_wall_ns"), int):
                c["start_wall_ns"] = calls[0]["start_wall_ns"] + (rd_s - calls[0]["start_ns"])
                c["end_wall_ns"] = c["start_wall_ns"] + rd
            t["native_calls"] = [c]
            t["native_call_count"] = 1
        out.append(t)
    return out


def task_value(t, key):
    if key == "queue_wait_ns":
        return t.get("queue_wait_ns")
    return ((t.get("associated_stages") or {}).get(key) or {}).get("duration_ns")


def augment(lines, cases=None):
    cases = cases or {}
    recs = [parse(l) for l in lines]
    ho_suffix = cases.get("host_only_suffix")
    # ---- pass 1: the steps, the requests' records
    core_exec, steps_of, prev_of = [], collections.defaultdict(list), {}
    tasks0, rg0, core_req, api_req = {}, collections.defaultdict(list), collections.defaultdict(list), collections.defaultdict(list)
    last = None
    for pre, o in recs:
        if o is None:
            continue
        ev, sc = o.get("event"), o.get("scope")
        if ev == "core_summary" and sc == "execution" and o.get("execution_id"):
            core_exec.append(o)
            prev_of[o["execution_id"]] = last
            last = o["execution_id"]
            for rid in o.get("request_ids") or []:
                steps_of[rid].append(o)
        elif ev == "pd_transfer_summary" and sc == "task" and o.get("tp_rank", 0) == 0 and o.get("direction", "read") == "read" \
                and o.get("request_id") and o["request_id"] not in tasks0:
            tasks0[o["request_id"]] = o
        elif ev == "pd_transfer_summary" and sc == "request_generation" and o.get("tp_rank", 0) == 0 and o.get("request_id"):
            rg0[o["request_id"]].append(o)
        elif ev == "core_summary" and sc == "request" and o.get("request_id"):
            core_req[o["request_id"]].append(o)
        elif ev == "api_summary" and sc == "request" and o.get("request_id"):
            api_req[o["request_id"]].append(o)
    first_steps = {s[0]["execution_id"] for s in steps_of.values() if s}
    none_step = steps_of[cases["graph_none"]][0]["execution_id"] if steps_of.get(cases.get("graph_none")) else None
    big_step = steps_of[cases["big_prep"]][0]["execution_id"] if steps_of.get(cases.get("big_prep")) else None
    host_rec = None
    if cases.get("host_only_record"):
        rid_, n_ = cases["host_only_record"]
        if len(steps_of.get(rid_, [])) > n_:
            host_rec = steps_of[rid_][n_]["execution_id"]
    # ---- the runner's four stages (03)
    dev = collections.defaultdict(dict)       # execution id -> stage -> device time (rank 0)
    k = n_stage = 0
    sample_recs = []
    for pre, o in recs:
        if o is None or o.get("event") != "worker_summary":
            continue
        st = o.get("stages") or {}
        exe = o.get("execution_id")
        # the host time on every rank, the device time on rank 0 only (C14)
        host_only = bool(ho_suffix and exe and exe.endswith(ho_suffix)) or o.get("tp_rank", 0) != 0
        if o.get("phase") == "execute" and isinstance(st.get("forward"), dict):
            k += 1
            fw = st["forward"]
            fs = fw.get("start_ns") or o.get("timestamp_ns") or 0
            fe = fw.get("end_ns") or fs + (fw.get("duration_ns") or 0)
            ph, lh = 1200000 + 1000 * k, 500000 + 500 * k
            pdev = ph * 3 // 5
            if exe == big_step:          # cases["big_prep"]: a first step whose input preparation is large
                ph, pdev = 45000000, 40000000
            st["prepare_inputs"] = stage(fs - ph - 10000, ph, None if host_only or exe == host_rec else pdev)
            st["lm_head"] = stage(fe + 5000, lh, None if host_only else lh * 9 // 10)
            if o.get("tp_rank", 0) == 0:     # rank 0 only (10_ section 5)
                o["cudagraph_runtime_mode"] = "NONE" if exe == none_step else "PIECEWISE" if exe in first_steps else "FULL"
            n_stage += 2
        elif o.get("phase") == "sample" and isinstance(st.get("sampling"), dict):
            k += 1
            sp = st["sampling"]
            ss = sp.get("start_ns") or o.get("timestamp_ns") or 0
            se = sp.get("end_ns") or ss + (sp.get("duration_ns") or 0)
            bh, dh = 300000 + 300 * k, 6000000 + 1000 * k
            st["bookkeeping"] = stage(se + 5000, bh, None if host_only else bh // 2)
            st["draft"] = stage(se + 5000 + bh + 5000, dh, None if host_only else dh * 19 // 20)
            if o.get("tp_rank", 0) == 0:
                sample_recs.append(o)
            n_stage += 2
        else:
            continue
        if exe and o.get("tp_rank", 0) == 0:
            for s_ in STEP_DEV:
                v = (st.get(s_) or {}).get("device_duration_ns")
                if isinstance(v, int) and s_ in st:
                    dev[exe][s_] = v
    # step_device_span_ns: prepare_inputs' start event to draft's end event (both read), on the sample record
    e1_exe = None
    if cases.get("E1") and len(steps_of.get(cases["E1"], [])) > 2:
        for s_ in steps_of[cases["E1"]][2:]:
            d_ = dev.get(s_["execution_id"]) or {}
            if "prepare_inputs" in d_ and "draft" in d_:
                e1_exe = s_["execution_id"]
                break
    n_span = 0
    for i_, o in enumerate(sample_recs):
        exe = o.get("execution_id")
        d_ = dev.get(exe) or {}
        if not exe or "prepare_inputs" not in d_ or "draft" not in d_:
            continue
        tot = sum(d_.values())
        o["step_device_span_ns"] = tot - 1000 if exe == e1_exe else tot + 150000 + 1000 * (i_ % 7)
        n_span += 1
    # ---- the receive thread's tasks and the request's envelope (02)
    new_tasks, rg_extra, done_at = {}, {}, {}
    info = {"tasks": {}, "rg": {}, "kv_ready_wait": {}, "violators": {}, "env_null": cases.get("env_null"),
            "host_only_record": host_rec, "worker_stages": n_stage, "spans": n_span}
    for rid, t0 in tasks0.items():
        if not rg0.get(rid):
            continue
        calls = t0.get("native_calls") or []
        n0e = max(c["end_ns"] for c in calls) if calls else t0["end_ns"]
        done = min(max(t0["end_ns"] - 250000, n0e + 100000), t0["end_ns"] - 1000)
        ts = [t0] + synth_tasks(t0, done)
        new_tasks[rid] = ts[1:]
        t0["request_done_ns"] = None if rid == cases.get("env_null") else (done if t0.get("tracker_completed") else None)
        done_at[rid] = done
        mx = {f: max(v for v in (task_value(t, key) for t in ts) if isinstance(v, int)) for key, f in F3}
        env = None if rid == cases.get("env_null") else done - min(t["queued_ns"] for t in ts)
        for g in rg0[rid]:
            if rid == cases.get("E3") and env is not None:
                env = g["transfer_envelope_ns"] - 100000
            g.update(mx, kv_load_envelope_ns=env, kv_load_timing_scope=ENV_SCOPE, submitted_tasks=N_TASKS, succeeded_tasks=N_TASKS,
                     submitted_bytes=(g.get("submitted_bytes") or 0) * N_TASKS, succeeded_bytes=(g.get("succeeded_bytes") or 0) * N_TASKS)
            r1 = json.loads(json.dumps(g))
            r1.update({f: v * 10 for f, v in mx.items()}, tp_rank=1, kv_load_envelope_ns=None if env is None else env * 10,
                      transfer_envelope_ns=g["transfer_envelope_ns"] * 10)
            if r1.get("record_id"):
                r1["record_id"] += "-rank1"
            rg_extra[id(g)] = r1
        info["tasks"][rid] = ts
        info["rg"][rid] = dict(mx, kv_load_envelope_ns=env, transfer_envelope_ns=rg0[rid][0]["transfer_envelope_ns"])
    if cases.get("E3"):
        info["violators"]["E3"] = cases["E3"]
    # ---- the scheduler's wait (01): requests that read from P
    for rid in info["rg"]:
        if not steps_of.get(rid) or not core_req.get(rid):
            continue
        step = steps_of[rid][0]
        end = step["stages"]["schedule"]["start_ns"]
        seen = (done_at[rid] if rid != cases.get("env_null") else tasks0[rid]["end_ns"])
        gap = end - seen
        start = seen + min(1200000, gap // 3) if gap > 3000 else end - 1000
        if rid == cases.get("E2"):
            end += 5000
            info["violators"]["E2"] = rid
        if rid == cases.get("E4"):
            api_ = api_req.get(rid) or []
            qt = ((api_[0].get("official") or {}).get("queued_time") if api_ else None)
            qn = ((core_req[rid][-1].get("stages") or {}).get("queue") or {}).get("initial_wait_ns") or 0
            env = info["rg"][rid]["kv_load_envelope_ns"] or 0
            if isinstance(qt, (int, float)):
                start = end - (int(qt * 1e9) - qn - env + 1000000)
                info["violators"]["E4"] = rid
        dur = end - start
        kv = {"calls": 1, "duration_ns": dur, "start_ns": start, "end_ns": end, "status": "ok", "exceptions": 0, "measured_calls": 1,
              "skipped_calls": 0, "unmeasured_calls": 0,
              "ranges": [{"episode": 1, "start_ns": start, "promoted_ns": start + min(20000, dur // 2), "end_ns": end, "duration_ns": dur,
                          "ready_execution_id": prev_of.get(step["execution_id"]), "scheduled_execution_id": step["execution_id"],
                          "status": "ok", "load_failed": False}],
              "dropped_ranges": 0, "complete": True, "end_observed": True, "timing_scope": "kv_ready_seen_to_scheduled"}
        for c in core_req[rid]:
            c["stages"]["kv_ready_wait"] = json.loads(json.dumps(kv))
        info["kv_ready_wait"][rid] = kv
    # ---- E5: vLLM's prefill_time below the first step's device times
    rid5 = cases.get("E5")
    if rid5 and steps_of.get(rid5) and api_req.get(rid5):
        tot = sum((dev.get(steps_of[rid5][0]["execution_id"]) or {}).values())
        for a in api_req[rid5]:
            if isinstance((a.get("official") or {}).get("prefill_time"), (int, float)):
                a["official"]["prefill_time"] = (tot - 5000000) / 1e9
        info["violators"]["E5"] = rid5
    if e1_exe:
        info["violators"]["E1"] = e1_exe
    # ---- pass 2: write
    pids = [int(m.group(2)) for m in (PROC.match(l) for l in lines) if m]
    w1 = "(Worker_TP1 pid=%d)" % (max(pids) + 1 if pids else 1)       # a Worker process of its own, rank 1
    out = []
    for line, (pre, o) in zip(lines, recs):
        if o is None:
            out.append(line)
            continue
        out.append(dump(pre, o))
        if o.get("event") == "pd_transfer_summary" and o.get("scope") == "task" and tasks0.get(o.get("request_id")) is o:
            for t in new_tasks.get(o["request_id"], []):
                out.append(dump(pre, t))
        if id(o) in rg_extra:
            out.append(dump(PROC.sub(w1, pre) if PROC.match(pre) else pre, rg_extra[id(o)]))
    info["rank1"] = len(rg_extra)
    info["rank1_proc"] = w1
    return out, info
