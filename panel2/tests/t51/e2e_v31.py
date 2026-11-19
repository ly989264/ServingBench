#!/usr/bin/env python3
"""T51 (pack two v3.1-extra): end to end on this machine with the dsv4.1 or glm5.3 segment definitions, in throw-away
containers (Vector 0.61.0, ClickHouse 26.8.36.3, --rm): node files -> the puller's Mooncake pairing -> Vector -> ClickHouse
-> the per-request JSON and every dashboard query.

  python3 -I tests/t51/e2e_v31.py <pack> <work> <P.log> <D.log> [dsv41|glm53]

<pack> is the v3.1-extra panel-2 folder (copied into <work>; profiles/<profile>/segments.json becomes its segments.json, as
bin/panel2_cluster.sh add does for SEGMENTS_PROFILE=<profile>; dsv41 when left out). The rc1 logs (real hardware, TP1, the
patch's records) are used as P0 and D0, with what the rc1 run did not write added on top:
  - the "wait for put" stage in P's worker_summary[execute] records (as the vllm-ascend patch is to write it:
    stages.wait_for_put.duration_ns, assumed until the record format note says otherwise);
  - the proxy's line for the two requests that went through the proxy (the third, "direct", went to D only);
  - Mooncake's timing line comes from the rc1 P log itself (real), as the puller's fetch would mirror it.
With glm53 the logs are turned into the glm5.3 shape first (vLLM v0.30, MooncakeConnectorV1 plus AscendStoreConnector;
record format note "glm5.3 版 1", sections 2.1 and 3):
  - P loads from the cache pool synchronously (load_async=false): the step that loads the hot request's KV names the request
    (no empty first-candidate step); P writes no pool_load and no load_submit record, but one pool_task[load] per request
    of the step (queue_enter_ns null, no queue counters). Six copies of the hot request share its step, so that every
    terminal state is there: succeeded (hot), backend_failure, unknown_result, exception (the four C11 takes) and all_hit,
    skipped, missing_load_spec (not taken); the cold request's own step has a no_task record. A rank-1 copy of the hot
    request's record must be left out (rank 0 only), and the step's stages.submit is the sum of its loads;
  - D loads asynchronously (load_async=true): the direct request (D only) hits the cache pool, which gives D an empty
    first-candidate step and its two mapping records (pd_transfer_summary[link] pool_load in the EngineCore,
    pool_task[load_submit] in the Worker); D's records of reading from P keep the hybrid connector's format, which the
    record format note gives MooncakeConnectorV1 too;
  - MTP (deepseek_mtp, D k=5): D's decode steps schedule 6 tokens, 5 of them speculative (members' scheduled_tokens and
    num_spec_tokens; the filter reads neither).
Every value of the per-request JSON is compared with values computed here from those logs (not by the filter). Then:
in how many requests each of the 47 segments has a value (dsv41: 46, all but P's api gateway; glm53: 45, also without P's
start_load_kv/wait_for_transfer, which has no source there), bin/coverage.sh's own queries, the dashboard queries.
Writes <work>/e2e-<profile>.txt. Removes its containers and network at the end.
"""
import collections
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time

SRC_PACK, WORK, LOG, LOG_D = sys.argv[1:5]
PROFILE = sys.argv[5] if len(sys.argv) > 5 else "dsv41"
SHIFT = len(sys.argv) > 6 and sys.argv[6] == "shift"     # C14: rank-0 worker records written 1-2 steps later
G53 = PROFILE == "glm53"     # C12, the refined C2, C15 and missing_execution_id are in the glm53 definitions only
# C15: a copy of the direct request whose Worker records have the host time only (glm53; E2E_HOSTONLY=0 leaves it out,
# to compare the other requests with a run without it)
HOSTONLY = G53 and os.environ.get("E2E_HOSTONLY", "1") != "0"
COPY_TAGS = ("noexec", "partexec", "retry") + (("hostonly",) if HOSTONLY else ())
hostonly_x = None
NET, CH, VEC = "t51-net", "t51-ch", "t51-vec"
CH_IMG, VEC_IMG = "clickhouse/clickhouse-server:26.8.36.3", "timberio/vector:0.61.0-debian"
HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, "..", "t41", "tools")
PREFIX = re.compile(r"^(?:chatcmpl-|cmpl-|resp[-_])")
EMPTY_P = ("p.api_gateway",)
out_lines = []


def say(ok, text):
    line = ("OK   " if ok is True else "FAIL " if ok is False else "     ") + text
    out_lines.append(line)
    print(line, flush=True)


def sh(cmd, inp=None, check=True, env=None):
    r = subprocess.run(cmd, input=inp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, env=env)
    if check and r.returncode:
        raise SystemExit("命令失败：%s\n%s" % (" ".join(cmd), r.stdout[-3000:]))
    return r.stdout


def ch(sql, fmt="JSONEachRow"):
    q = sql.strip().rstrip(";") + (" FORMAT %s" % fmt if fmt else "")
    return sh(["docker", "exec", "-i", CH, "clickhouse-client", "--user", "vllm_writer", "--password", "x", "-n"], inp=q + ";\n")


def rows(sql):
    return [json.loads(x) for x in ch(sql).splitlines() if x.strip()]


def x_of(rid):
    i = PREFIX.sub("", rid)
    m = re.match(r"^(.+)-[0-9a-f]{8}$", i)
    return m.group(1) if m else i


def cleanup():
    for c in (VEC, CH):
        subprocess.run(["docker", "rm", "-fv", c], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["docker", "network", "rm", NET], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def load(path):
    out = []
    for line in open(path, errors="replace"):
        i = line.find("[PD_DIAG] ")
        if i >= 0:
            out.append(json.loads(line[i + 10:]))
    return out


# ---- the pack and the logs ------------------------------------------------------------------------------------------
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(WORK)
PACK = os.path.join(WORK, "pack")
shutil.copytree(SRC_PACK, PACK, ignore=shutil.ignore_patterns("generated", "__pycache__", "offline"))
shutil.copy(os.path.join(PACK, "profiles", PROFILE, "segments.json"), os.path.join(PACK, "segments.json"))
profile = json.load(open(os.path.join(PACK, "segments.json")))
say(None, "段定义：profiles/%s/segments.json（config_id %s）" % (PROFILE, profile["config_id"]))


def node_src(side, phase, path):
    nodes = profile["sides"][side][phase]
    for part in path.split("."):
        n = [x for x in nodes if x["id"] == part][0]
        nodes = n.get("children") or []
    return n.get("src")


# P's start_load_kv/submit: the records its sources name (dsv41 load_submit; glm53 the four C11 states of load)
P_SUB_SRC = node_src("prefill", "ttft", "start_load_kv.submit") or []


def src_match(o, srcs):
    for x in srcs:
        if o.get("event") == x.get("event") and all(o.get(a) == b for a, b in (x.get("where") or {}).items()):
            return o.get(x.get("field", "duration_ns"))
    return None


# P's start_load_kv/wait_for_transfer has no source with glm53 (synchronous load): empty like the api gateway
P_WFT = node_src("prefill", "ttft", "start_load_kv.wait_for_transfer") is not None
if not P_WFT:
    EMPTY_P = EMPTY_P + ("p.start_load_kv.wait_for_transfer",)

# P: the wait-for-put stage added to every rank-0 execute record that names requests (1 us + 1 us per step, distinct values)
aug_p = os.path.join(WORK, "P.aug.log")
wfp = collections.defaultdict(int)
k = 0
# C9: two cache-pool tasks shared by the cold and the hot request (request_id null, members[]), as concurrency makes
# them: a save task with one call into Mooncake (its timing line is added below) and a load task. Their values are
# larger than the requests' own tasks, so each member's segment is the shared task's (the dashboard keeps the largest).
shared_lines = []
NATIVE_YEAR = [None]      # the year of the Mooncake timing lines (the shared save task's call, local time)
rid_cold = rid_hot = None
for o in load(LOG):
    if o.get("event") == "api_summary" and o.get("scope") == "request":
        if "-cold-" in o["request_id"]:
            rid_cold = o["request_id"]
        if "-hot-" in o["request_id"]:
            rid_hot = o["request_id"]
members = [{"request_id": rid_cold, "execution_id": None}, {"request_id": rid_hot, "execution_id": None}]
G53 = {}
LOG_P = LOG
if PROFILE == "glm53":
    LOG_P = os.path.join(WORK, "P.glm53.log")
    recs0 = load(LOG)
    # the step that loads the hot request's KV (rc1: its first-candidate step, where the async submission returned)
    X = [o for o in recs0 if o.get("event") == "pool_task" and o.get("operation") == "load_submit"
         and o.get("status") == "submission_returned" and o.get("request_id") == rid_hot][0]["execution_id"]
    Xc = [o for o in recs0 if o.get("event") == "core_summary" and o.get("scope") == "execution" and rid_cold in (o.get("request_ids") or [])][0]["execution_id"]
    hot_req = [o for o in recs0 if o.get("event") == "api_summary" and o.get("scope") == "request" and o["request_id"] == rid_hot][0]
    obs_hot = hot_req["observation_id"]

    def id_pairs(rid, tag, obs, subm):
        """A copy's ids: vLLM's request id, the API's external_req_id (the same without the 8-hex suffix), its observation
        and submission ids; the collector keys a request on these."""
        ext = rid.rsplit("-", 1)[0]
        return [(rid, rid.replace("-hot-", "-%s-" % tag)), ('"%s"' % ext, '"%s-%s"' % (ext[:-len("-hot")], tag)),
                (obs, obs + "-" + tag)] + ([(subm, subm + "-" + tag)] if subm else [])

    def renamed(o, pairs):
        txt = json.dumps(o, separators=(",", ":"))
        for a, b in pairs:
            txt = txt.replace(a, b)
        return json.loads(txt)
    TAGS = (("hotb", "backend_failure", 1500000), ("hotu", "unknown_result", 900000), ("hote", "exception", 12000),
            ("hotn", "all_hit", 4000), ("hots", "skipped", 3000), ("hotm", "missing_load_spec", 2000))
    TAKEN = ("succeeded", "backend_failure", "unknown_result", "exception")
    clones = [(rid_hot.replace("-hot-", "-%s-" % t), t, st, dur) for t, st, dur in TAGS]
    hot_load = [o for o in recs0 if o.get("event") == "pool_task" and o.get("operation") == "load" and o.get("request_id") == rid_hot][0]
    loads = [(rid_hot, "succeeded", hot_load["duration_ns"])] + [(c, st, dur) for c, _, st, dur in clones]
    step_submit = sum(d for _, _, d in loads)

    def sync_load(base, rid, status, dur, exe, tag, rank=0):
        r = json.loads(json.dumps(base))
        r.update(request_id=rid, request_ids=[rid], execution_id=exe, status=status, duration_ns=dur, end_ns=r["start_ns"] + dur,
                 queue_enter_ns=None, members=[{"request_id": rid, "execution_id": exe}], member_count=1, tp_rank=rank,
                 record_id="record-glm53-load-%s" % tag, task_id="pool-glm53-load-%s" % tag)
        for kk in ("queue_wait_ns", "queue_entries"):
            (r.get("counters") or {}).pop(kk, None)
        return r
    n_steps = 0
    with open(LOG, errors="replace") as src, open(LOG_P, "w") as dst:
        for line in src:
            i = line.find("[PD_DIAG] ")
            if i < 0:
                dst.write(line)
                continue
            o = json.loads(line[i + 10:])
            pre = line[:i + 10]
            out = [o]
            if o.get("event") == "pool_task" and o.get("operation") == "load_submit":
                continue                # P loads synchronously: no load_submit (note 2.1)
            if o.get("event") == "pd_transfer_summary" and o.get("transfer_role") == "pool_load":
                continue                # and no pool_load mapping
            if o.get("event") == "pool_task" and o.get("operation") == "load" and o.get("request_id") == rid_hot:
                out = [sync_load(o, rid_hot, "succeeded", o["duration_ns"], X, "hot")]
                out += [sync_load(o, c, st, dur, X, t) for c, t, st, dur in clones]
                out.append(sync_load(o, rid_hot, "succeeded", 9000000, X, "hot-rank1", rank=1))
            elif (o.get("event") in ("core_summary", "worker_summary") and o.get("scope", "execution") in ("execution", None)
                  and (o.get("execution_id") == X or rid_hot in (o.get("request_ids") or []))):
                if o.get("event") == "core_summary" and o.get("scope") != "execution":
                    pass
                else:
                    ids = [r for r in (o.get("request_ids") or []) if r != rid_hot]
                    o["request_ids"] = ids + [rid_hot] + [c for c, _, _, _ in clones]
                    if o.get("event") == "core_summary":
                        hm = [m for m in (o.get("members") or []) if m.get("request_id") == rid_hot]
                        if hm:
                            o["members"] = (o.get("members") or []) + [dict(hm[0], request_id=c) for c, _, _, _ in clones]
                    if o.get("event") == "worker_summary" and o.get("phase") == "execute" and o.get("execution_id") == X:
                        o["stages"]["submit"] = dict(o["stages"].get("submit") or {}, duration_ns=step_submit)
                    n_steps += 1
            elif o.get("event") == "core_summary" and o.get("scope") == "execution" and o.get("execution_id") == Xc:
                out.append(sync_load(hot_load, rid_cold, "no_task", 3100, Xc, "cold"))
            elif o.get("event") in ("api_summary", "core_summary") and (o.get("request_id") == rid_hot or obs_hot in line):
                for c, t, _, _ in clones:
                    r = renamed(o, id_pairs(rid_hot, t, obs_hot, hot_req.get("submission_id")))
                    if r.get("record_id"):
                        r["record_id"] = r["record_id"] + "-" + t
                    out.append(r)
            for r in out:
                dst.write(pre + json.dumps(r, separators=(",", ":")) + "\n")
    G53 = {"X": X, "loads": loads, "step_submit": step_submit, "taken": TAKEN, "hot_dur": hot_load["duration_ns"],
           "nonhit": [rid_cold] + [c for c, _, st, _ in clones if st not in TAKEN],
           "excluded": sum(d for _, st, d in loads if st not in TAKEN)}
    say(None, "glm53：P 同步加载；热请求加载那一步（%s）有 %d 个请求（热请求和 6 个副本），这一步的 stages.submit 是 %d 条加载记录之和 %.3f ms；"
              "冷请求那一步一条 no_task；另有一条 rank 1 的记录；%d 条步骤记录改过" % (X[-12:], len(loads), len(loads), step_submit / 1e6, n_steps))
with open(LOG_P, errors="replace") as src, open(aug_p, "w") as dst:
    for line in src:
        i = line.find("[PD_DIAG] ")
        extra = None
        if i >= 0:
            o = json.loads(line[i + 10:])
            if o.get("event") == "worker_summary" and o.get("phase") == "execute" and o.get("tp_rank") == 0 and o.get("request_ids"):
                k += 1
                v = 1000 * (10 + k)
                o["stages"]["wait_for_put"] = {"calls": 1, "duration_ns": v, "status": "ok", "measured_calls": 1}
                for rid in o["request_ids"]:
                    wfp[rid] += v
                line = line[:i + 10] + json.dumps(o, separators=(",", ":")) + "\n"
            if o.get("event") == "pool_task" and o.get("operation") == "save" and o.get("request_id") == rid_cold and o.get("native_calls"):
                c0 = o["native_calls"][0]
                shr = json.loads(json.dumps(o))
                st = c0["end_wall_ns"] + 1000000
                shr.update(request_id=None, execution_id=None, task_id="pool-shared-save", record_id="record-shared-save", member_count=2,
                          members=members, duration_ns=9000000, timestamp_ns=o["timestamp_ns"] + 1)
                for kk, v in (("build_key", 100000), ("lookup", 400000), ("prepare", 300000), ("sync", 50000), ("put", 6000000)):
                    shr["stages"][kk]["duration_ns"] = v
                shr["counters"]["queue_wait_ns"] = 50000
                shr["native_calls"] = [dict(c0, call_id="pool_call-shared", start_wall_ns=st, end_wall_ns=st + 2000000,
                                           submitted_keys=2, submitted_bytes=8192)]
                extra = shr
                # the timing line, 1 ms into the call, local time of the node
                import datetime as _dt
                lt = _dt.datetime(1970, 1, 1) + _dt.timedelta(microseconds=(st + 1000000) // 1000 + c0["wall_utc_offset_seconds"] * 1000000)
                NATIVE_YEAR[0] = lt.year
                if G53:
                    # glm5.3 site: glog 0.6 (the year in the prefix); this line as the old-signature wrapper prints it
                    # (complete=false, no phase 1, no total)
                    shared_lines.append("I%s %6d client_service.cpp:3348] [BatchPutTiming] keys=2 bytes=8192 "
                                        "phase2_submit_transfers_us=500 phase3_wait_transfers_us=1500 phase4_finalize_batch_put_us=300 "
                                        "timing_scope=prefer_same_node_full status=ok complete=false failed_keys=0"
                                        % (lt.strftime("%Y%m%d %H:%M:%S.%f"), c0["native_tid"]))
                else:
                    shared_lines.append("I%s %6d client_service.cpp:3327] [BatchPutTiming] keys=2 bytes=8192 phase1_start_batch_put_us=10 "
                                        "phase2_submit_transfers_us=500 phase3_wait_transfers_us=1500 phase4_finalize_batch_put_us=300 "
                                        "total_us=2310 timing_scope=prefer_same_node_full status=ok complete=true failed_keys=0"
                                        % (lt.strftime("%m%d %H:%M:%S.%f"), c0["native_tid"]))
            if PROFILE != "glm53" and o.get("event") == "pool_task" and o.get("operation") == "load" and o.get("request_id") == rid_hot:
                # (glm53: P's synchronous loads are one request each, note 2.1, so there is no shared load task)
                shr = json.loads(json.dumps(o))
                shr.update(request_id=None, execution_id=None, task_id="pool-shared-load", record_id="record-shared-load", member_count=2,
                          members=members, duration_ns=3000000, timestamp_ns=o["timestamp_ns"] + 1)
                shr["counters"]["queue_wait_ns"] = 500000
                extra = shr
        dst.write(line)
        if extra is not None:
            dst.write(line[:i + 10] + json.dumps(extra, separators=(",", ":")) + "\n")
say(None, "P 日志加上等 put 完成：%d 条 execute 记录；加上冷、热两个请求共用的保存任务和加载任务各一条（C9）" % k)

# D: the glm5.3 shape (glm53): a cache-pool hit of the direct request, MTP in the decode steps
aug_d = LOG_D
if PROFILE == "glm53":
    aug_d = os.path.join(WORK, "D.aug.log")
    recs0 = load(LOG_D)
    rid_dir = [o for o in recs0 if o.get("event") == "api_summary" and o.get("scope") == "request" and "-direct-" in o["request_id"]][0]["request_id"]
    first_dir = [o for o in recs0 if o.get("event") == "core_summary" and o.get("scope") == "execution" and rid_dir in (o.get("request_ids") or [])][0]
    tmpl_w = [o for o in recs0 if o.get("event") == "worker_summary" and o.get("phase") == "execute" and o.get("execution_id") == first_dir["execution_id"]][0]
    tmpl_link = [o for o in recs0 if o.get("event") == "pd_transfer_summary" and o.get("scope") == "link"][0]
    tmpl_sub = [o for o in load(LOG) if o.get("event") == "pool_task" and o.get("operation") == "load_submit" and o.get("status") == "submission_returned"][0]
    zx = first_dir["execution_id"] + "-dpool"
    ts0 = first_dir["timestamp_ns"] - 2000000
    step_c = json.loads(json.dumps(first_dir))
    step_c.update(execution_id=zx, request_ids=[], members=[], timestamp_ns=ts0, record_id="record-glm53-dpool-core")
    for kk, v in (("lookup", 210000), ("alloc", 31000), ("other", 47000), ("schedule", 330000)):
        step_c["stages"][kk] = dict(step_c["stages"].get(kk) or {}, duration_ns=v)
    step_w = json.loads(json.dumps(tmpl_w))
    step_w.update(execution_id=zx, request_ids=[], timestamp_ns=ts0 + 100000, record_id="record-glm53-dpool-worker")
    step_w["stages"] = {"submit": dict(tmpl_w["stages"]["submit"], duration_ns=160000)}
    link = json.loads(json.dumps(tmpl_link))
    link.update(transfer_role="pool_load", request_id=rid_dir, request_ids=[rid_dir], execution_id=zx, links=[], status="linked",
                complete=True, timestamp_ns=ts0 + 50000, record_id="record-glm53-dpool-link", vllm_cached_tokens=0, kvpool_cached_tokens=128)
    sub = json.loads(json.dumps(tmpl_sub))
    sub.update(request_id=rid_dir, request_ids=[rid_dir], execution_id=zx, duration_ns=140000, kv_role="kv_consumer",
               engine_id=first_dir.get("engine_id"), members=[{"request_id": rid_dir, "execution_id": zx}], member_count=1,
               timestamp_ns=ts0 + 150000, record_id="record-glm53-dpool-submit", task_id="pool-glm53-dpool")
    n_mtp = 0
    # the hot request's six copies on P (above) also run on D: its D records, with the copies' ids, and its D steps
    rid_hot_d = [o for o in recs0 if o.get("event") == "api_summary" and o.get("scope") == "request" and x_of(o["request_id"]) == x_of(rid_hot)][0]["request_id"]
    hot_req_d = [o for o in recs0 if o.get("event") == "api_summary" and o.get("scope") == "request" and o["request_id"] == rid_hot_d][0]
    obs_hot_d = hot_req_d["observation_id"]
    d_clones = [(rid_hot_d.replace("-hot-", "-%s-" % t), c, t) for c, t, _, _ in clones]
    n_dsteps = 0
    with open(LOG_D, errors="replace") as src, open(aug_d, "w") as dst:
        for line in src:
            i = line.find("[PD_DIAG] ")
            if i >= 0:
                o = json.loads(line[i + 10:])
                steprec = (o.get("event") == "core_summary" and o.get("scope") == "execution") or o.get("event") == "worker_summary"
                if steprec and rid_hot_d in (o.get("request_ids") or []):
                    o["request_ids"] = list(o["request_ids"]) + [cd for cd, _, _ in d_clones]
                    hm = [m for m in (o.get("members") or []) if m.get("request_id") == rid_hot_d]
                    if hm:
                        o["members"] = list(o["members"]) + [dict(hm[0], request_id=cd) for cd, _, _ in d_clones]
                    n_dsteps += 1
                    line = line[:i + 10] + json.dumps(o, separators=(",", ":")) + "\n"
                elif not steprec and (o.get("request_id") == rid_hot_d or (o.get("event") == "api_summary" and obs_hot_d in line)):
                    for cd, cp, t in d_clones:
                        r = renamed(o, id_pairs(rid_hot_d, t, obs_hot_d, hot_req_d.get("submission_id")) + id_pairs(rid_hot, t, obs_hot, None)[:2])
                        if r.get("record_id"):
                            r["record_id"] = r["record_id"] + "-" + t
                        dst.write(line[:i + 10] + json.dumps(r, separators=(",", ":")) + "\n")
                if o.get("event") == "worker_summary" and o.get("phase") == "execute" and o.get("request_ids") and o.get("tp_rank") == 0:
                    o["stages"]["wait_for_put"] = {"calls": 1, "duration_ns": 7000, "status": "ok", "measured_calls": 1}
                    line = line[:i + 10] + json.dumps(o, separators=(",", ":")) + "\n"
                if o.get("event") == "core_summary" and o.get("scope") == "execution":
                    if o.get("execution_id") == first_dir["execution_id"]:
                        for extra in (step_c, link, step_w, sub):
                            dst.write(line[:i + 10] + json.dumps(extra, separators=(",", ":")) + "\n")
                    changed = False
                    for m in o.get("members") or []:
                        if (m.get("num_output_tokens") or 0) > 0:
                            m["scheduled_tokens"], m["num_spec_tokens"] = 6, 5
                            changed = True
                    if changed:
                        n_mtp += 1
                        line = line[:i + 10] + json.dumps(o, separators=(",", ":")) + "\n"
            dst.write(line)
    say(None, "glm53：D 的直连请求命中缓存池（首次候选空步一步、pool_load 和 load_submit 映射各一条）；MTP：%d 条 D 解码步记录 scheduled_tokens=6、num_spec_tokens=5；"
              "D 的 execute 记录也带等 put 完成（kv_both），D 的段里没有它；热请求的 6 个副本在 D 上也有（%d 条步骤记录带上它们）" % (n_mtp, n_dsteps))

# C15 (was C14): device times on the rank-0 worker records (forward, sampling); 2 of every 5 records have none (key
# missing or null): in the glm53 definitions those steps add nothing (no host fallback)
# and falls back to the host time. "shift": those records leave 1 or 2 steps later, in step order (the patch defers
# them on rank 0 until the device time is read, at most 4 steps), timestamp_ns the time they are written.
fb_want = collections.Counter()      # (side, segment) -> records with the stage but no device time (steps left empty)


def c14_pass(path, side):
    out = path + ".c14"
    k = n_dev = n_shift = 0
    pending, order = [], {}
    last_ts = 0
    by_n = collections.Counter()
    with open(path, errors="replace") as src, open(out, "w") as dst:
        def release(prefix, n_steps):
            nonlocal last_ts, n_shift
            for e_ in pending:
                e_[0] -= n_steps
            while pending and pending[0][0] <= 0:
                _, pfx, o_ = pending.pop(0)
                last_ts += 1
                o_["timestamp_ns"] = max(o_.get("timestamp_ns") or 0, last_ts)
                dst.write((prefix if prefix is not None and prefix.split(")")[0] == pfx.split(")")[0] else pfx)
                          + "[PD_DIAG] " + json.dumps(o_, separators=(",", ":")) + "\n")
                n_shift += 1
        for line in src:
            i = line.find("[PD_DIAG] ")
            if i < 0:
                dst.write(line)
                continue
            o = json.loads(line[i + 10:])
            if isinstance(o.get("timestamp_ns"), int):
                last_ts = max(last_ts, o["timestamp_ns"])
            if o.get("event") == "worker_summary" and o.get("tp_rank", 0) == 0 and o.get("phase") in ("execute", "sample"):
                seg = "forward" if o["phase"] == "execute" else "sampling"
                st = (o.get("stages") or {}).get(seg)
                if G53 and isinstance(st, dict) and isinstance(st.get("duration_ns"), int):
                    k += 1
                    if k % 5 == 3:
                        st.pop("device_duration_ns", None)
                    elif k % 5 == 4:
                        st["device_duration_ns"] = None
                    else:
                        st["device_duration_ns"] = st["duration_ns"] * 3 // 4 + 1000 + k
                        n_dev += 1
                    if st.get("device_duration_ns") is None and o.get("request_ids"):
                        fb_want[(side, seg)] += 1
                if SHIFT and o.get("execution_id"):
                    n_ = order.setdefault(o["execution_id"], 1 + len(order) % 2)
                    by_n[n_] += 1
                    pending.append([n_, line[:i], o])
                    continue
                line = line[:i + 10] + json.dumps(o, separators=(",", ":")) + "\n"
            dst.write(line)
            if SHIFT and o.get("event") == "core_summary" and o.get("scope") == "execution":
                release(line[:i], 1)
        release(None, 10 ** 9)          # the process exits: what is left is written
    os.replace(out, path)
    return k, n_dev, n_shift, by_n


if PROFILE in ("dsv41", "glm53"):
    for side_, path_ in (("p", aug_p), ("d", aug_d)):
        if path_ in (LOG, LOG_D):        # never write into the given logs
            cp_ = os.path.join(WORK, os.path.basename(path_) + "." + side_ + ".log")
            shutil.copyfile(path_, cp_)
            if side_ == "p":
                aug_p = cp_
            else:
                aug_d = cp_
            path_ = cp_
        k_, nd_, ns_, bn_ = c14_pass(path_, side_)
        say(None, "C15（%s）：rank 0 的 forward、sampling 记录 %d 条，其中 %d 条带设备时间，其余没有（缺字段、为 null 各一半左右），这几步留空%s"
            % (side_.upper(), k_, nd_, "；推迟写出 %d 条（推迟 1 步 %d 条、2 步 %d 条，按步骤先后）" % (ns_, bn_[1], bn_[2]) if SHIFT else ""))

# missing_execution_id (glm53): two copies of the direct request on D, each with its own copies of the direct request's
# step records. noexec: no execution_id on any of them (the EngineCore's without the key, the Worker's null);
# partexec: every one renamed <id>-px, except the execute record of the third step, which has none. Their request-level
# records are the direct request's with the copy's ids; the cache-pool records of the first-candidate step are left out.
noexec_want = collections.Counter()      # instance -> records with no execution_id that a rule reads
exec_cases = []
if G53:
    recs_dx = load(aug_d)
    req_dir = [o for o in recs_dx if o.get("event") == "api_summary" and o.get("scope") == "request" and o["request_id"] == rid_dir][0]
    obs_dir, subm_dir = req_dir["observation_id"], req_dir.get("submission_id")
    steps_dir = []
    for o in recs_dx:
        if o.get("event") == "core_summary" and o.get("scope") == "execution" and rid_dir in (o.get("request_ids") or []):
            steps_dir.append(o["execution_id"])
    miss_px = steps_dir[2]
    ext_dir = rid_dir.rsplit("-", 1)[0]

    def dir_pairs(tag):
        return [(rid_dir, rid_dir.replace("-direct-", "-%s-" % tag)), ('"%s"' % ext_dir, '"%s-%s"' % (ext_dir[:-len("-direct")], tag)),
                (obs_dir, obs_dir + "-" + tag)] + ([(subm_dir, subm_dir + "-" + tag)] if subm_dir else [])
    rid_retry_a = rid_dir.replace("-direct-", "-retry-").rsplit("-", 1)[0] + "-0000a7a7"   # same client id, another suffix
    retry_a_src, retry_a_done = [], False
    with open(aug_d, errors="replace") as src:
        for line in src:
            i = line.find("[PD_DIAG] ")
            if i >= 0:
                o = json.loads(line[i + 10:])
                if (o.get("event") == "api_summary" and o.get("observation_id") == obs_dir) or (
                        o.get("event") == "core_summary" and o.get("scope") == "request" and o.get("request_id") == rid_dir):
                    retry_a_src.append({"rec": o, "pfx": line[:i]})

    def shift_ns(o, d):
        """Moves every monotonic time of a record (an int field *_ns larger than 1e13; durations are smaller) by d."""
        if isinstance(o, dict):
            for k_, v_ in o.items():
                if k_.endswith("_ns") and isinstance(v_, int) and not isinstance(v_, bool) and v_ > 10 ** 13:
                    o[k_] = v_ + d
                else:
                    shift_ns(v_, d)
        elif isinstance(o, list):
            for v_ in o:
                shift_ns(v_, d)

    def shift_prefix(pfx, sec):
        m_ = re.search(r"(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)", pfx)
        t_ = datetime.datetime(2026, int(m_.group(1)), int(m_.group(2)), int(m_.group(3)), int(m_.group(4)), int(m_.group(5)))
        t_ += datetime.timedelta(seconds=sec)
        return pfx[:m_.start()] + t_.strftime("%m-%d %H:%M:%S") + pfx[m_.end():]
    out_ = aug_d + ".x"
    n_copy = collections.Counter()
    with open(aug_d, errors="replace") as src, open(out_, "w") as dst:
        for line in src:
            dst.write(line)
            i = line.find("[PD_DIAG] ")
            if i < 0:
                continue
            o = json.loads(line[i + 10:])
            steprec = (o.get("event") == "core_summary" and o.get("scope") == "execution") or o.get("event") == "worker_summary"
            if steprec and rid_dir in (o.get("request_ids") or []) and o.get("tp_rank", 0) == 0:
                for tag in COPY_TAGS:
                    c_ = json.loads(json.dumps(o))
                    cid = rid_dir.replace("-direct-", "-%s-" % tag)
                    c_["request_ids"] = [cid]
                    if c_.get("members"):
                        c_["members"] = [dict(m, request_id=cid) for m in c_["members"] if m.get("request_id") == rid_dir]
                    exe = o.get("execution_id")
                    if tag == "retry":
                        c_["execution_id"] = exe + "-rt"
                    elif tag == "hostonly":
                        c_["execution_id"] = exe + "-ho"
                        if o.get("event") == "worker_summary":       # C15: the host time only, no device time
                            for sg_ in ("forward", "sampling"):
                                st_ = (c_.get("stages") or {}).get(sg_)
                                if isinstance(st_, dict):
                                    st_.pop("device_duration_ns", None)
                    elif tag == "noexec" or (o.get("event") == "worker_summary" and o.get("phase") == "execute" and exe == miss_px):
                        if o.get("event") == "core_summary":
                            c_.pop("execution_id", None)
                        else:
                            c_["execution_id"] = None
                    else:
                        c_["execution_id"] = exe + "-px"
                    if c_.get("record_id"):
                        c_["record_id"] = c_["record_id"] + "-" + tag
                    if o.get("event") == "worker_summary":
                        sg_ = "forward" if o.get("phase") == "execute" else "sampling"
                        st_ = (c_.get("stages") or {}).get(sg_)
                        if isinstance(st_, dict) and isinstance(st_.get("duration_ns"), int) and st_.get("device_duration_ns") is None:
                            fb_want[("d", sg_)] += 1     # the copy of a record without a device time is left empty too
                    n_copy[tag] += 1
                    dst.write(line[:i + 10] + json.dumps(c_, separators=(",", ":")) + "\n")
            elif not steprec and o.get("event") not in ("pool_task", "pd_transfer_summary") and (
                    o.get("request_id") == rid_dir or (o.get("event") == "api_summary" and o.get("observation_id") == obs_dir)):
                # (the observation id compared as a field: "…-1" is the start of other requests' "…-13")
                if o.get("event") == "api_summary" and o.get("scope") == "http" and not retry_a_done:
                    # the first attempt of "retry", before the second: request-level records only, 5 s earlier
                    retry_a_done = True
                    for o_a in retry_a_src:
                        r = renamed(o_a["rec"], dir_pairs("retry") + [(rid_dir.replace("-direct-", "-retry-"), rid_retry_a)])
                        r = json.loads(json.dumps(r).replace(obs_dir + "-retry", obs_dir + "-retrya"))
                        if subm_dir:
                            r = json.loads(json.dumps(r).replace(subm_dir + "-retry", subm_dir + "-retrya"))
                        shift_ns(r, -5000000000)
                        if r.get("record_id"):
                            r["record_id"] = r["record_id"] + "-retrya"
                        if r.get("event") == "core_summary":
                            r["stages"]["queue"]["initial_wait_ns"] = 777000        # its own queue time
                            for m in r.get("members") or []:
                                m["executions"] = 0
                        if r.get("event") == "api_summary" and r.get("scope") == "http":
                            r["stages"]["req_resolve"]["duration_ns"] += 111000     # its own API server times
                            r["end_ns"] = r["start_ns"] + 5000000                   # sent back after 5 ms
                        if r.get("event") == "api_summary" and r.get("scope") == "request":
                            r["stages"]["detokenize"]["ranges"] = []                 # it never ran a step
                        dst.write(shift_prefix(o_a["pfx"], -5) + "[PD_DIAG] " + json.dumps(r, separators=(",", ":")) + "\n")
                for tag in COPY_TAGS:
                    r = renamed(o, dir_pairs(tag))
                    if r.get("record_id"):
                        r["record_id"] = r["record_id"] + "-" + tag
                    if tag in ("partexec", "retry", "hostonly") and r.get("event") == "api_summary" and r.get("scope") == "request":
                        for x_ in ((r.get("stages") or {}).get("detokenize") or {}).get("ranges") or []:
                            if x_.get("execution_id"):
                                x_["execution_id"] = x_["execution_id"] + {"partexec": "-px", "retry": "-rt", "hostonly": "-ho"}[tag]
                    dst.write(line[:i + 10] + json.dumps(r, separators=(",", ":")) + "\n")
    os.replace(out_, aug_d)
    exec_cases = [(x_of(rid_dir.replace("-direct-", "-noexec-")), "noexec"), (x_of(rid_dir.replace("-direct-", "-partexec-")), "partexec")]
    retry_x = x_of(rid_dir.replace("-direct-", "-retry-"))
    say(None, "D 退回 P 重算：请求 %s 两次尝试，第一次（%s）D 上只有请求级记录、早 5 秒、排队 0.777 ms；第二次照直连请求（%d 条步骤记录）"
        % (retry_x, rid_retry_a[-8:], n_copy["retry"]))
    say(None, "missing_execution_id：直连请求的两个副本，noexec 的 %d 条步骤记录都没有 execution_id，partexec 的 %d 条里只有第 3 步的 "
        "execute 那条没有；它们的请求级记录照直连请求（不含首次候选那一步的缓存池记录）" % (n_copy["noexec"], n_copy["partexec"]))
    if HOSTONLY:
        hostonly_x = x_of(rid_dir.replace("-direct-", "-hostonly-"))
        say(None, "C15：直连请求的又一个副本 %s，%d 条步骤记录照直连请求（步骤号加 -ho），Worker 的 forward、sampling 只有 host 时间、没有设备时间"
            % (hostonly_x, n_copy["hostonly"]))

pull = os.path.join(WORK, "pull")
os.makedirs(pull)
shift = None
for side, path in (("P0", aug_p), ("D0", aug_d)):
    split = os.path.join(WORK, "split-" + side)
    cmd = [sys.executable, "-I", os.path.join(TOOLS, "check_pd_diag.py"), path, "-o", split, "--pack", PACK, "--instance", side]
    if shift is not None:
        cmd += ["--shift", shift]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    m_ = re.search(r"--shift (-?[0-9.]+)", r.stdout)
    if shift is None and m_:
        shift = m_.group(1)
    say(r.returncode in (0, 1, 4), "check_pd_diag.py 拆成 %s（退出码 %d）" % (side, r.returncode))
    shutil.copytree(os.path.join(split, side), os.path.join(pull, "n-" + side, side))

# Mooncake: the timing line of the rc1 P log, mirrored as the puller's fetch does; then the puller's pairing, once.
GLOG_AT = re.compile(r"[IWEF](?:\d{4})?\d{4} \d{2}:\d{2}:\d{2}\.\d{6} ")      # glog 0.6 (with the year) or 0.4
native_lines = [l[GLOG_AT.search(l).start():].rstrip("\n") for l in open(LOG, errors="replace")
                if ("[BatchPutTiming]" in l or "[BatchUpsertTiming]" in l) and GLOG_AT.search(l)]
if G53 and NATIVE_YEAR[0]:
    # the glm5.3 site's glog 0.6 prints the year: the rc1 line (glog 0.4) as glog 0.6 would print it
    native_lines = ["%s%04d%s" % (l[0], NATIVE_YEAR[0], l[1:]) if re.match(r"[IWEF]\d{4} ", l) else l for l in native_lines]
native_lines += shared_lines
say(None, "Mooncake 计时行 %d 行：%s" % (len(native_lines), "，".join(
    ("glog 0.6（前缀带年份）" if re.match(r"[IWEF]\d{8} ", l) else "glog 0.4（不带年份）")
    + ("、旧签名包装（complete=false，没有 phase1、total）" if "complete=false" in l else "") for l in native_lines)))
os.makedirs(os.path.join(pull, "_mooncake", "n-P0"))
with open(os.path.join(pull, "_mooncake", "n-P0", "host-vllm_log.%s.log" % time.strftime("%Y%m%d")), "w") as f:
    f.write("\n".join(native_lines) + "\n")
sys.path.insert(0, os.path.join(PACK, "scripts"))
import native_put  # noqa: E402
state = os.path.join(WORK, "puller-state")
os.makedirs(state)
json.dump({"started": True}, open(os.path.join(state, "native.json"), "w"))
nat = native_put.Puller({"MOONCAKE_LOG_FILES": "n-P0=/x/vllm.log"}, pull, state, lambda a: a, lambda: [], None)
nat.run_once()
say(nat.assoc.counts["records_written"] == len(native_lines), "拉取程序的 Mooncake 配对：%s" % nat.assoc.counts)
nat_recs = []
for root_, _, fs_ in os.walk(os.path.join(pull, "_native")):
    for f_ in fs_:
        for l_ in open(os.path.join(root_, f_), errors="replace"):
            if "[PD_DIAG] " in l_:
                nat_recs.append(json.loads(l_[l_.index("[PD_DIAG] ") + 10:]))
say(nat.assoc.counts["lines_unparsed"] == 0, "Mooncake 计时行全部解析成功（lines_unparsed %d）" % nat.assoc.counts["lines_unparsed"])
if G53:
    shr_ = [o for o in nat_recs if o.get("task_id") == "pool-shared-save"]
    st_ = shr_[0]["stages"] if len(shr_) == 1 else {}
    say(st_.get("submit") == {"duration_ns": 500000} and st_.get("wait_for_transfer") == {"duration_ns": 1500000}
        and st_.get("finalize") == {"duration_ns": 300000} and "start_batch_put" not in st_,
        "旧签名包装那一行（glog 0.6，complete=false）：pd_native_put 的 submit / wait_for_transfer / finalize 是 0.5 / 1.5 / 0.3 ms，"
        "没有 start_batch_put（缺项，不是 0）：%s" % json.dumps(st_))

# The proxy's lines for the requests that went through it.
recs_p, recs_d = load(aug_p), load(aug_d)
xs_p = sorted({x_of(o["request_id"]) for o in recs_p if o["event"] == "api_summary" and o["scope"] == "request"})
proxy_expect = {}
pdir = os.path.join(pull, "_proxy", "proxyhost", "proxy-9000")
os.makedirs(pdir)
name = "proxy.%s.4242.0000.log" % time.strftime("%Y%m%dT%H%M%S")
with open(os.path.join(pdir, name), "w") as f:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000+00:00", time.gmtime(time.time() - 120))
    f.write("%s (proxy pid=4242) INFO [load_balance_proxy_server_example.py:0] [PD_DIAG] %s\n" % (ts, json.dumps(
        {"event": "pd_file_open", "version": 1, "instance": "proxy-9000", "pid": 4242, "role": "proxy", "file": name, "seq": 0})))
    for i, X in enumerate(xs_p):
        e = {"proxy_to_p_ms": 0.25 + i / 10, "p_total_ms": 200.0 + i, "proxy_to_d_ms": 0.35 + i / 10, "d_first_ms": 100.0 + i,
             "d_total_ms": 700.0 + i}
        e["ttft_ms"] = round(e["proxy_to_p_ms"] + e["p_total_ms"] + e["proxy_to_d_ms"] + e["d_first_ms"], 3)
        e["e2e_ms"] = round(e["proxy_to_p_ms"] + e["p_total_ms"] + e["proxy_to_d_ms"] + e["d_total_ms"], 3)
        row = dict(e, event="pd_proxy_request", version=1, request_id=X, client_request_id="client-%d" % i, api="/chat/completions",
                   status="ok", http_status=200, stream=bool(i % 2), include_usage=None, n=None, p_addr="P0", d_addr="D0", retries=i,
                   recompute_retries=0, p_http_retries=i, d_http_retries=0, retry_ms=None, api_gateway_ms=None, pid=4242)
        f.write("%s (proxy pid=4242) INFO [load_balance_proxy_server_example.py:0] [PD_DIAG] %s\n" % (ts, json.dumps(row)))
        proxy_expect[X] = dict(e, client_request_id="client-%d" % i, retries=i)
say(None, "proxy 行：%d 个请求（%s）" % (len(proxy_expect), "、".join(x[-8:] for x in proxy_expect)))

# ---- the values straight from the logs ------------------------------------------------------------------------------
cand = profile["candidate"]


def cand_values(recs):
    """{(rid, acc_id): ns} for the first-candidate steps (TP1: one process, every record together)."""
    pairs = set()
    for o in recs:
        for m in cand["map"]:
            if o.get("event") == m["event"] and all(o.get(a) == b for a, b in m["where"].items()) and o.get("request_id") and o.get("execution_id"):
                pairs.add((o["request_id"], o["execution_id"]))
    out = collections.defaultdict(int)
    for rid, exe in pairs:
        for a in cand["accumulate"]:
            for o in recs:
                if o.get("execution_id") != exe or o.get("event") != a["event"] or not all(o.get(p) == q for p, q in a["where"].items()):
                    continue
                if o.get("tp_rank") not in (None, 0) or rid in (o.get("request_ids") or []):
                    continue
                v = (o["stages"].get(a["value"].split(".")[1]) or {}).get("duration_ns")
                if isinstance(v, int):
                    out[(rid, a["id"])] += v
    return out


expect, expect_exec = {}, {}
retry_a_exp = {}        # (side, X) -> the first attempt's own values (glm53 "retry")
rid_retry_a = rid_retry_a if G53 else None
retry_x = retry_x if G53 else None
expect_missing = 0
c12_diff, c2_left = [], []        # (request, ...) where C12's reading matters, D requests whose first-candidate step had alloc/other
# C9 in bin/coverage.sh: per P request and cache-pool segment, the largest value of its own tasks and of shared tasks
src_max = {}


def bump(rid, key, v, shared):
    k_ = (rid, key, shared)
    src_max[k_] = max(src_max.get(k_, v), v)


for side, recs in (("p", recs_p), ("d", recs_d)):
    cv = cand_values(recs)
    first, steps = {}, collections.defaultdict(lambda: collections.defaultdict(list))
    for o in recs:
        if o["event"] == "core_summary" and o["scope"] == "execution":
            for rid in o["request_ids"]:
                first.setdefault(rid, o.get("execution_id") or None)
                for kk in ("lookup", "alloc", "other"):
                    v = (o["stages"].get(kk) or {}).get("duration_ns")
                    if isinstance(v, int):
                        steps[rid][kk].append((o.get("execution_id") or None, v))
        if o["event"] == "worker_summary" and o.get("tp_rank") == 0:
            kk = "forward" if o["phase"] == "execute" else "sampling"
            st_ = o["stages"].get(kk) or {}
            # C15 (glm53): the device time only, a step without one adds nothing; the other definitions: the host time
            v = st_.get("device_duration_ns") if G53 else st_.get("duration_ns")
            for rid in o["request_ids"]:
                if isinstance(v, int):
                    steps[rid][kk].append((o.get("execution_id") or None, v))
    http = {o["observation_id"]: o for o in recs if o["event"] == "api_summary" and o["scope"] == "http"}
    sub = {}
    for o in recs:
        if o["event"] == "api_summary" and o["scope"] == "engine_submission":
            sub[next(l["request_id"] for l in o["links"] if l.get("kind") == "request_id")] = o
    core = {}
    for o in recs:
        if o["event"] == "core_summary" and o["scope"] == "request":
            if o["request_id"] not in core or o["timestamp_ns"] > core[o["request_id"]]["timestamp_ns"]:
                core[o["request_id"]] = o
    save, lsub, env, load_ = collections.defaultdict(dict), {}, {}, {}
    sv_dur, sv_qw = {}, {}           # C12 check: the largest processing and the largest queue wait, each on its own
    tasks_of = collections.defaultdict(list)      # request -> its save tasks, its own and (C9) those it is a member of
    for o in recs:
        if o["event"] == "pool_task" and o.get("tp_rank", 0) == 0 and o.get("scope") == "task":
            # C9 (shared_members in the dsv41 segment definitions): a task naming no request counts for every member
            targets = [o["request_id"]] if o.get("request_id") else [m["request_id"] for m in o.get("members") or [] if m.get("request_id")]
            shared_rec = not o.get("request_id")
            for rid in targets:
                if side == "p" and o.get("operation") == "save" and isinstance(o.get("duration_ns"), int):
                    for s_ in ("", "build_key", "lookup", "prepare", "sync", "put"):
                        v_ = (o["duration_ns"] + (((o.get("counters") or {}).get("queue_wait_ns") or 0) if G53 else 0)) if not s_ else \
                            ((o.get("stages") or {}).get(s_) or {}).get("duration_ns")
                        if isinstance(v_, int):
                            bump(rid, "b.wait_for_save" + ("." + s_ if s_ else ""), v_, shared_rec)
                if side == "p" and isinstance(src_match(o, P_SUB_SRC), int):
                    bump(rid, "p.start_load_kv.submit", src_match(o, P_SUB_SRC), shared_rec)
                if side == "p" and P_WFT and o.get("operation") == "load" and isinstance(o.get("duration_ns"), int):
                    bump(rid, "p.start_load_kv.wait_for_transfer", o["duration_ns"] + ((o.get("counters") or {}).get("queue_wait_ns") or 0), shared_rec)
                if o.get("operation") == "save" and isinstance(o.get("duration_ns"), int):
                    # the dashboard keeps the largest value of each column over a request's records; C12: the parent
                    # column of one record is its queue wait + processing (two fields of one record add up, as in C1)
                    cols = {"": o["duration_ns"] + (((o.get("counters") or {}).get("queue_wait_ns") or 0) if G53 else 0)}
                    if side == "p":
                        sv_dur[rid] = max(sv_dur.get(rid, 0), o["duration_ns"])
                        sv_qw[rid] = max(sv_qw.get(rid, 0), (o.get("counters") or {}).get("queue_wait_ns") or 0)
                    for s_ in ("build_key", "lookup", "prepare", "sync", "put"):
                        cols[s_] = ((o.get("stages") or {}).get(s_) or {}).get("duration_ns")
                    for kk, v in cols.items():
                        if isinstance(v, int) and v > (save[rid].get(kk) or -1):
                            save[rid][kk] = v
                    tasks_of[rid].append(o)
                if side == "p" and isinstance(src_match(o, P_SUB_SRC), int):
                    lsub[rid] = max(lsub.get(rid, 0), src_match(o, P_SUB_SRC))
                if o.get("operation") == "load" and isinstance(o.get("duration_ns"), int):
                    # C1: queue wait + processing; the dashboard keeps the largest record of a request
                    t = o["duration_ns"] + ((o.get("counters") or {}).get("queue_wait_ns") or 0)
                    load_[rid] = max(load_.get(rid, 0), t)
        if o["event"] == "pd_transfer_summary" and o.get("scope") == "request_generation" and isinstance(o.get("transfer_envelope_ns"), int):
            env[o["request_id"]] = max(env.get(o["request_id"], 0), o["transfer_envelope_ns"])
    native = collections.defaultdict(dict)       # request -> phase field -> largest over its save tasks' lines
    if side == "p":
        for n_ in native_lines:
            nn = native_put.parse_native(n_)
            for rid, os_ in tasks_of.items():
                for o in os_:
                    for c in native_put.save_calls(o):
                        t = native_put.native_local_us(nn, (datetime.datetime(1970, 1, 1) + datetime.timedelta(microseconds=c[1])).year)
                        if c[0] == nn["tid"] and c[3] == nn["keys"] and c[1] <= t <= c[2]:
                            for f_ in ("phase2_submit_transfers_us", "phase3_wait_transfers_us", "phase4_finalize_batch_put_us"):
                                native[rid][f_] = max(native[rid].get(f_, 0), nn[f_])
                            for s_, f_ in (("submit", "phase2_submit_transfers_us"), ("wait_for_transfer", "phase3_wait_transfers_us"),
                                           ("finalize", "phase4_finalize_batch_put_us")):
                                bump(rid, "b.wait_for_save.put." + s_, nn[f_], not o.get("request_id"))
    for o in recs:
        if not (o["event"] == "api_summary" and o["scope"] == "request"):
            continue
        rid, X = o["request_id"], x_of(o["request_id"])
        h, c, sg = http[o["observation_id"]], core[rid], steps[rid]
        ms = lambda v: None if v is None else v / 1e6

        def tot(kk, extra=None):
            v = sum(x for _, x in sg[kk]) if sg[kk] else None
            if extra is not None:
                v = (v or 0) + extra
            return ms(v)

        def fst(kk, extra=None):
            if G53 and sg[kk] and (first.get(rid) is None or any(e_ is None for e_, _ in sg[kk])):
                return None     # missing_execution_id: the first step, or which step a record was, is not in the log
            v = sum(x for e_, x in sg[kk] if e_ == first[rid]) if sg[kk] else None
            if extra is not None:
                v = (v or 0) + extra
            return ms(v)

        def rest(kk):
            f_ = fst(kk)
            return None if f_ is None else tot(kk) - f_
        base = {"req_resolve": (h["stages"]["req_resolve"]["duration_ns"] + h["stages"]["render"]["duration_ns"]) / 1e6,
                "tokenizer": h["stages"]["tokenize"]["duration_ns"] / 1e6, "add_request": sub[rid]["stages"]["add_request"]["duration_ns"] / 1e6,
                "schedule.queue_time": c["stages"]["queue"]["initial_wait_ns"] / 1e6}
        px = proxy_expect.get(X)
        if side == "p":
            e = {"p." + kk: v for kk, v in base.items()}
            sub_ms, wft_ms = ms(lsub.get(rid)), (ms(load_.get(rid)) if P_WFT else None)
            e.update({"total": (h["end_ns"] - h["start_ns"]) / 1e6, "p.schedule.hash": c["stages"]["hash"]["duration_ns"] / 1e6,
                      "p.schedule.lookup": tot("lookup", cv.get((rid, "cand_lookup"))), "p.schedule.alloc": tot("alloc", cv.get((rid, "cand_alloc"))),
                      "p.schedule.other": tot("other", cv.get((rid, "cand_other"))), "p.forward": tot("forward"), "p.sampling": tot("sampling"),
                      "p.detokenizer": o["terminal"]["detokenize_duration_ns"] / 1e6,
                      "p.wait_for_put": ms(wfp.get(rid)),
                      "p.start_load_kv.submit": sub_ms, "p.start_load_kv.wait_for_transfer": wft_ms,
                      "p.start_load_kv": None if sub_ms is None and wft_ms is None else (sub_ms or 0) + (wft_ms or 0),
                      "p.lb_proxy": px["proxy_to_p_ms"] if px else None})
            sv = save.get(rid) or {}
            e["b.wait_for_save"] = ms(sv.get(""))
            for s_ in ("build_key", "lookup", "prepare", "sync", "put"):
                e["b.wait_for_save." + s_] = ms(sv.get(s_))
            nn = native.get(rid)
            for s_, field in (("submit", "phase2_submit_transfers_us"), ("wait_for_transfer", "phase3_wait_transfers_us"),
                              ("finalize", "phase4_finalize_batch_put_us")):
                e["b.wait_for_save.put." + s_] = nn[field] / 1000 if nn else None
            e.update({kk: None for kk in EMPTY_P})
            if G53 and rid in sv_dur and sv_dur[rid] + sv_qw[rid] != (save.get(rid) or {}).get(""):
                c12_diff.append((X, ms(save[rid][""]), ms(sv_dur[rid] + sv_qw[rid])))
        else:
            rg = [x["duration_ns"] for x in o["stages"]["detokenize"]["ranges"] if first.get(rid) and x["execution_id"] == first[rid]]
            dtot = o["terminal"]["detokenize_duration_ns"]
            sub_ms, wft_ms = ms(cv.get((rid, "cand_submit"))), ms(env.get(rid))
            e = {"d." + kk: v for kk, v in base.items()}
            e.update({"total": (h["end_ns"] - h["start_ns"]) / 1e6,
                      # the refined C2 (glm53): D's first step without the first-candidate step's alloc and other
                      "d.schedule.alloc": fst("alloc") if G53 else fst("alloc", cv.get((rid, "cand_alloc"))),
                      "d.schedule.other": fst("other") if G53 else fst("other", cv.get((rid, "cand_other"))),
                      "d.forward": fst("forward"), "d.sampling": fst("sampling"), "d.detokenizer": rg[0] / 1e6 if rg else None,
                      "r.schedule.alloc": rest("alloc"), "r.schedule.other": rest("other"),
                      "r.forward": rest("forward"), "r.sampling": rest("sampling"),
                      "r.detokenizer": (dtot - rg[0]) / 1e6 if rg else None, "steps": max(len(sg["forward"]), 1) - 1,     # as the view: greatest(forward steps, 1) - 1 (C15: steps with a device time)
                      "d.start_load_kv.submit": sub_ms, "d.start_load_kv.wait_for_transfer": wft_ms,
                      "d.start_load_kv": None if sub_ms is None and wft_ms is None else (sub_ms or 0) + (wft_ms or 0),
                      "d.lb_proxy": px["proxy_to_d_ms"] if px else None})
            expect_missing += not rg
            if G53 and (cv.get((rid, "cand_alloc")) or cv.get((rid, "cand_other"))):
                c2_left.append((X, ms(cv.get((rid, "cand_alloc"))), ms(cv.get((rid, "cand_other")))))
        if G53 and rid == rid_retry_a:
            retry_a_exp[(side, X)] = e
            continue
        expect[(side, X)] = e
        mm = [x for x in c["members"] if x["request_id"] == rid][0]
        expect_exec[(side, X)] = mm["executions"]
retry_fold = {}
for (side_, X_), ea in retry_a_exp.items():
    eb = expect[(side_, X_)]
    for k_ in list(eb):
        if k_.startswith("r.") or k_ == "steps":
            continue                    # second step on: summed; the first attempt has no steps, so the second's
        if k_ == "total":
            # P 合计 / D 合计 come from the request's API rows over all its attempts: the largest (anchor, maxIf)
            if ea.get(k_) is not None and eb[k_] is not None and ea[k_] > eb[k_]:
                retry_fold[k_] = (ea[k_], eb[k_])
                eb[k_] = ea[k_]
            continue
        if ea.get(k_) is not None and ea.get(k_) != eb[k_]:
            retry_fold[k_] = (ea[k_], eb[k_])
            eb[k_] = ea[k_]             # first step, request-level: the earliest attempt that has a value
say(None, "C12：P 后台 wait_for_save 每条保存记录取排队时间加处理时间，再按请求取最大；两种取法结果不同的请求 %d 个：%s"
    "（前一个数是看板应显示的值，后一个是「处理时间最大值加排队时间最大值」）"
    % (len(c12_diff), "；".join("%s %.3f / %.3f ms" % t for t in c12_diff) or "无"))
say(None, "C2 细化：D 上首次候选那一步有 alloc 或 other 的请求 %d 个，期望值里不再加上：%s"
    % (len(c2_left), "；".join("%s alloc %s ms、other %s ms" % (x, "%.3f" % a if a is not None else "无", "%.3f" % b if b is not None else "无")
                               for x, a, b in c2_left) or "无"))

# ---- containers -----------------------------------------------------------------------------------------------------
cleanup()
try:
    sh(["docker", "network", "create", NET])
    sh(["docker", "run", "-d", "--rm", "--name", CH, "--network", NET, "-e", "CLICKHOUSE_USER=vllm_writer", "-e", "CLICKHOUSE_PASSWORD=x",
        "-e", "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1", CH_IMG])
    for _ in range(60):
        if subprocess.run(["docker", "exec", CH, "clickhouse-client", "--user", "vllm_writer", "--password", "x", "-q", "SELECT 1"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            break
        time.sleep(1)
    sh([sys.executable, "-I", os.path.join(PACK, "scripts", "generate.py")])
    ch(open(os.path.join(PACK, "generated", "schema.sql")).read(), fmt=None)
    ch(open(os.path.join(PACK, "generated", "views.sql")).read(), fmt=None)
    conf, data = os.path.join(WORK, "vconf"), os.path.join(WORK, "vdata")
    os.makedirs(conf)
    os.makedirs(data)
    with open(os.path.join(conf, "inventory.csv"), "w") as f:
        f.write("addr,name\n")
    venv = dict(os.environ, CLICKHOUSE_ADDR=CH, CLICKHOUSE_CONTAINER=CH, CH_WRITER_PASSWORD="x", MODEL_NAME="glm-5.2",
                PROXY_COLLECT="1", MOONCAKE_LOG_COLLECT="1")
    subprocess.run([sys.executable, "-I", os.path.join(PACK, "scripts", "render_vector.py"), os.path.join(conf, "vector.yaml"),
                    "/var/lib/vector", "/etc/vector"], env=venv, check=True)
    v = subprocess.run(["docker", "run", "--rm", "-v", conf + ":/etc/vector:ro", VEC_IMG, "validate", "--no-environment", "/etc/vector/vector.yaml"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    say(v.returncode == 0, "Vector 配置 validate%s" % ("" if v.returncode == 0 else "：" + v.stdout[-800:]))
    if v.returncode:
        raise SystemExit(1)
    sh(["docker", "run", "-d", "--rm", "--name", VEC, "--network", NET, "-v", conf + ":/etc/vector", "-v", pull + ":/pull",
        "-v", data + ":/var/lib/vector", VEC_IMG, "--config", "/etc/vector/vector.yaml"])
    want_api = len(expect)
    # Vector 0.61's disk buffer does not hand over events written before the sink's reader first waits: they go out with
    # the next event (found here: the proxy file is read within the first second). Online the proxy writes a line per
    # request; here one more line, for a request that is not in the logs, plays the next request.
    for k_ in range(2):     # two more lines, 8 s apart: the last line written can itself wait for a next one
        time.sleep(8)
        with open(os.path.join(pdir, name), "a") as f:
            late = dict(row, request_id="late-request-not-in-the-logs-%d" % k_, client_request_id="late")
            f.write("%s (proxy pid=4242) INFO [load_balance_proxy_server_example.py:0] [PD_DIAG] %s\n" % (ts, json.dumps(late)))
    last, stable = -1, 0
    for _ in range(120):
        time.sleep(2)
        n = int(ch("SELECT count() FROM vllm_pd.phase_line", fmt="TSV").strip() or 0)
        api = int(ch("SELECT count() FROM vllm_pd.phase_line_cur WHERE proc = 'api' AND part = ''", fmt="TSV").strip() or 0)
        npx = int(ch("SELECT count() FROM vllm_pd.proxy_line", fmt="TSV").strip() or 0)
        stable = stable + 1 if n == last else 0
        last = n
        if api >= want_api and npx >= len(proxy_expect) + 1 and stable >= 3:
            break
    say(api >= want_api, "采集：phase_line %d 行，其中 API 合并行 %d（应有 %d）" % (n, api, want_api))
    real = int(ch("SELECT count() FROM vllm_pd.proxy_line WHERE request_id NOT LIKE 'late-%'", fmt="TSV").strip() or 0)
    say(real == len(proxy_expect) and npx >= len(proxy_expect) + 1,
        "采集：proxy_line 里日志中的请求 %d 行（应有 %d），启动后追加的两行进了 %d 行（最后一行可能还在等下一行）" % (real, len(proxy_expect), npx - real))
    if real != len(proxy_expect):
        logs = sh(["docker", "logs", VEC], check=False)
        for l_ in [l_ for l_ in logs.splitlines() if "proxy" in l_ or "ERROR" in l_ or "WARN" in l_][-12:]:
            say(None, "  vector: " + l_[:300])
    nat_rows = int(ch("SELECT count() FROM vllm_pd.phase_line_cur WHERE proc = 'save' AND isNotNull(wait_for_save__put__submit_ms)", fmt="TSV").strip() or 0)
    want_nat = len(native_lines) - len(shared_lines) + len(shared_lines) * len(members)   # C9: a shared task's record, one row per member
    say(nat_rows == want_nat, "采集：Mooncake 配对记录 %d 行（应有 %d：共用任务那一条按成员拆成 %d 行）" % (nat_rows, want_nat, len(members)))
    other = rows("SELECT event, count() AS n FROM vllm_pd.filter_other GROUP BY event")
    dead = sh(["docker", "exec", VEC, "sh", "-c", "cat /var/lib/vector/dead-letter-*.jsonl 2>/dev/null | wc -l"]).strip()
    say(not other and dead == "0", "没有认不出的行（filter_other %s），没有解析失败的行（%s）" % (other or "空", dead))
    pf = int(ch("SELECT count() FROM vllm_pd.filter_file WHERE role = 'proxy' OR instance LIKE 'proxy-%'", fmt="TSV").strip() or 0)
    say(pf == 0, "proxy 文件的文件头不进 filter_file（%d 行）" % pf)
    cfg = rows("SELECT config_id, count() AS n FROM vllm_pd.phase_line GROUP BY config_id")
    say(len(cfg) == 1 and cfg[0]["config_id"] == profile["config_id"], "采集写的每行都带 %s 的 config_id：%s" % (PROFILE, cfg))
    sc = rows("SELECT sum(cand_maps) AS m, sum(cand_applied) AS a FROM (SELECT argMax(cand_maps, ts) AS cand_maps, argMax(cand_applied, ts) AS cand_applied "
              "FROM vllm_pd.filter_selfcheck GROUP BY node, instance, pid)")
    say(None, "过滤器自检行的首次候选计数：%s" % sc)
    if G53:
        fb = rows("SELECT instance, toJSONString(fb) AS fb FROM (SELECT instance, argMax(acc_fallback, ts) AS fb "
                  "FROM vllm_pd.filter_selfcheck GROUP BY node, instance, pid) ORDER BY instance")
        got_ = collections.Counter()        # every process of an instance writes its own line (the API server's has 0)
        for r in fb:
            for kk, v in json.loads(r["fb"]).items():
                got_[(r["instance"][0].lower(), kk)] += v
        want_ = {kk: fb_want.get(kk, 0) for kk in (("p", "forward"), ("p", "sampling"), ("d", "forward"), ("d", "sampling"))}
        say(all(got_.get(kk) == v for kk, v in want_.items()) and all(v > 0 for v in want_.values()),
            "C15：过滤器自检行的留空次数（acc_fallback，按段）和日志里有这一段、没有设备时间的记录数相同：%s"
            % "，".join("%s %s %s/%s" % (kk[0].upper(), kk[1], got_.get(kk), v) for kk, v in want_.items()))

    if G53:
        for inst_, recs_ in (("P0", recs_p), ("D0", recs_d)):     # every record a rule reads that has no execution_id
            noexec_want[inst_] = sum(1 for o in recs_ if not o.get("execution_id") and o.get("version") == 2 and (
                (o.get("event") == "core_summary" and o.get("scope") == "execution")
                or (o.get("event") == "worker_summary" and o.get("phase") in ("execute", "sample") and o.get("tp_rank", 0) == 0)))
        em = rows("SELECT instance, sum(n) AS n FROM (SELECT instance, argMax(exec_missing, ts) AS n FROM vllm_pd.filter_selfcheck "
                  "GROUP BY node, instance, pid) GROUP BY instance ORDER BY instance")
        got_em = {r["instance"]: int(r["n"]) for r in em}
        say(got_em.get("D0") == noexec_want["D0"] and got_em.get("P0") == noexec_want["P0"],
            "missing_execution_id：过滤器自检行的 exec_missing 和日志里没有 execution_id 的步骤记录数相同：P0 %s/%d，D0 %s/%d"
            % (got_em.get("P0"), noexec_want["P0"], got_em.get("D0"), noexec_want["D0"]))

    # ---- the per-request JSON against the expected values ------------------------------------------------------------
    docs = {}
    for X in sorted({kk[1] for kk in expect}):
        o_ = ch("SELECT doc FROM vllm_pd.request_json(id = '%s')" % X, fmt="TSVRaw").strip()
        if o_:
            docs[X] = json.loads(o_)
    json.dump(docs, open(os.path.join(WORK, "docs.json"), "w"), indent=1, sort_keys=True)   # to compare two runs

    def get(doc, side, key):
        if key == "total":
            return doc["latency_ms"]["p_total" if side == "p" else "d_total"]
        if key == "steps":
            return doc["tpot"]["steps"]
        grp, path = key.split(".", 1)
        node = doc["tpot"]["d"] if grp == "r" else doc["background"]["p"] if grp == "b" else doc["ttft"][grp]
        parts = path.split(".")
        try:
            n_ = node[parts[0]]
            for p_ in parts[1:]:
                n_ = n_["sub"][p_]
            return n_["ms"]
        except (KeyError, TypeError):
            return "（JSON 里没有这个路径）"
    diff = n_vals = 0
    for (side, X), e in sorted(expect.items()):
        doc = docs.get(X)
        if doc is None:
            diff += 1
            say(False, "%s %s：库里没有这个请求" % (side, X))
            continue
        for kk, v in e.items():
            n_vals += 1
            g = get(doc, side, kk)
            same = (g is None and v is None) or (isinstance(g, (int, float)) and v is not None and abs(g - v) <= 0.001 + 1e-6 * abs(v))
            if not same:
                diff += 1
                if diff <= 12:
                    say(False, "%s %s %s：应为 %s，库里 %s" % (side, X[-12:], kk, v, g))
    say(diff == 0, "单请求 JSON：%d 个请求、%d 个值，和直接从日志算的值比，差 %d 个" % (len(docs), n_vals, diff))
    if G53:
        doc = docs.get(retry_x) or {}
        att = doc.get("attempts")
        vals = {kk: get(doc, "d", kk) for kk in ("d.schedule.queue_time", "d.req_resolve", "d.forward", "d.sampling", "d.schedule.alloc",
                                                 "d.detokenizer", "r.forward", "steps", "total")}
        say(att == 2 and all(vals[kk] is not None for kk in ("d.forward", "d.sampling", "d.schedule.alloc", "d.detokenizer", "r.forward")),
            "D 退回 P 重算：看板一行，attempts %s；第一步的 forward、sampling、alloc、detokenizer 取第二次（第一次为空，argMin 跳过 NULL）："
            "forward %s ms、alloc %s ms、detokenizer %s ms；第二步起 forward %s ms、步数 %s 是第二次的；D 合计 %s ms 是两次里大的那次"
            "（第一次只有 5 ms）；取第一次的值（第一次有值的）：%s"
            % (att, vals["d.forward"], vals["d.schedule.alloc"], vals["d.detokenizer"], vals["r.forward"], vals["steps"], vals["total"],
               "，".join("%s %s（第二次是 %s）" % (k_, a_, b_) for k_, (a_, b_) in sorted(retry_fold.items()))))
    for X, tag in exec_cases:
        doc = docs.get(X) or {}
        vals = {kk: get(doc, "d", kk) for kk in ("d.forward", "r.forward", "d.sampling", "r.sampling", "d.schedule.alloc",
                                                 "r.schedule.alloc", "d.detokenizer", "r.detokenizer", "steps", "total")}
        if tag == "noexec":
            ok_ = all(vals[kk] is None for kk in vals if kk not in ("steps", "total")) and vals["steps"] == expect[("d", X)]["steps"] \
                and isinstance(vals["total"], (int, float))
            what = "第一步、第二步起的 forward、sampling、alloc、detokenizer 都为空；步数 %s、D 合计 %s ms 照记录" % (vals["steps"], vals["total"])
        else:
            ok_ = vals["d.forward"] is None and vals["r.forward"] is None and all(
                isinstance(vals[kk], (int, float)) for kk in ("d.sampling", "r.sampling", "d.schedule.alloc", "r.schedule.alloc",
                                                             "d.detokenizer", "r.detokenizer")) and vals["steps"] == expect[("d", X)]["steps"]
            what = ("forward 第一步、第二步起为空；sampling、alloc、detokenizer 的记录都带 execution_id，照常有值（sampling 第一步 %s ms）；步数 %s"
                    % (vals["d.sampling"], vals["steps"]))
        say(ok_, "missing_execution_id（%s）：%s" % (tag, what))
    if HOSTONLY:
        doc = docs.get(hostonly_x) or {}
        dsd = docs.get(x_of(rid_dir)) or {}
        segs_ = ("d.forward", "r.forward", "d.sampling", "r.sampling")
        others = ("d.schedule", "d.schedule.alloc", "r.schedule.alloc", "d.detokenizer", "r.detokenizer", "d.req_resolve", "d.tokenizer",
                  "d.add_request", "total")
        vals = {kk: get(doc, "d", kk) for kk in segs_ + others}
        ref = {kk: get(dsd, "d", kk) for kk in others}
        ok_ = doc and all(vals[kk] is None for kk in segs_) and all(
            isinstance(vals[kk], (int, float)) and isinstance(ref[kk], (int, float)) for kk in others)
        st_ho, st_dir = get(doc, "d", "steps"), get(dsd, "d", "steps")
        ok_ = ok_ and st_ho == 0
        say(ok_, "C15（只有 host 时间的副本）：第一步、第二步起的 forward、sampling 都为空；第二步起的步数 %s（步数只数有设备时间的 forward 步，"
            "直连请求是 %s）；其余各段照常有值：%s；直连请求同一段 %s"
            % (st_ho, st_dir, "，".join("%s %s" % (kk, vals[kk]) for kk in others), "，".join("%s %s" % (kk, ref[kk]) for kk in others)))
    if G53:
        # C15, guide A 7.3: the share of steps left empty, 1 - forward steps / alloc steps (request-steps), per instance,
        # the guide's query with the window starting at the process start (the replay takes seconds), against the logs
        q_ratio = "WITH toDateTime64('1970-01-01 00:00:00.000', 3, 'UTC') AS t10, cur AS (SELECT DISTINCT instance, pid FROM vllm_pd.filter_selfcheck WHERE ts > now() - INTERVAL 10 MINUTE), done AS (SELECT DISTINCT instance, request_id FROM vllm_pd.phase_line_cur WHERE proc = 'api' AND api_end_ts < now() - INTERVAL 1 MINUTE), ew AS (SELECT instance, uniqExact(request_id) AS ne FROM vllm_pd.phase_line_cur WHERE proc = 'api' AND api_end_ts >= t10 AND api_end_ts < now() - INTERVAL 1 MINUTE GROUP BY instance), emp AS (SELECT instance, arraySort(groupUniqArray(pid)) AS pl, length(pl) AS np, sum(f) AS fe, sum(sa) AS se FROM (SELECT instance, pid, argMax(acc_fallback['forward'], ts) AS f, argMax(acc_fallback['sampling'], ts) AS sa FROM vllm_pd.filter_selfcheck WHERE ts > now() - INTERVAL 10 MINUTE GROUP BY instance, pid) GROUP BY instance) SELECT i.instance AS instance, match(i.instance, '^(P0|D0)$') AS this_batch, toString(t10) AS t10_utc, dateDiff('minute', t10, now()) AS window_min, ew.ne AS ended_win, emp.np AS pids, emp.pl AS pid_list, emp.fe AS forward_empty, emp.se AS sampling_empty, x.a_tot AS alloc_total, x.f_tot AS forward_total, x.s_tot AS sampling_total, x.a_win AS alloc_win, x.f_win AS forward_win, x.s_win AS sampling_win, if(x.a_win = 0, NULL, round(100 * (1 - x.f_win / x.a_win), 2)) AS forward_empty_pct, if(x.a_win = 0, NULL, round(100 * (1 - x.s_win / x.a_win), 2)) AS sampling_empty_pct FROM (SELECT DISTINCT instance FROM done) AS i LEFT JOIN (SELECT instance, sum(al) AS a_tot, sum(fw) AS f_tot, sum(sm) AS s_tot, sum(al - al0) AS a_win, sum(fw - fw0) AS f_win, sum(sm - sm0) AS s_win FROM (SELECT l.instance AS instance, l.request_id AS request_id, l.part AS part, toUInt32OrZero(splitByChar('-', l.part)[2]) AS pid, ifNull(max(l.acc__forward__steps), 0) AS fw, ifNull(maxIf(l.acc__forward__steps, l.ts < t10), 0) AS fw0, ifNull(max(l.acc__sampling__steps), 0) AS sm, ifNull(maxIf(l.acc__sampling__steps, l.ts < t10), 0) AS sm0, ifNull(max(l.acc__alloc__steps), 0) AS al, ifNull(maxIf(l.acc__alloc__steps, l.ts < t10), 0) AS al0 FROM vllm_pd.phase_line_cur AS l WHERE l.part != '' AND (l.instance, l.request_id) IN (SELECT instance, request_id FROM done) GROUP BY l.instance, l.request_id, l.part) WHERE (instance, pid) IN (SELECT instance, pid FROM cur) GROUP BY instance) AS x ON i.instance = x.instance LEFT JOIN emp ON i.instance = emp.instance LEFT JOIN ew ON i.instance = ew.instance ORDER BY this_batch DESC, instance SETTINGS join_use_nulls = 0"
        q_ratio_now = "WITH now64(3, 'UTC') AS t10, cur AS (SELECT DISTINCT instance, pid FROM vllm_pd.filter_selfcheck WHERE ts > now() - INTERVAL 10 MINUTE), done AS (SELECT DISTINCT instance, request_id FROM vllm_pd.phase_line_cur WHERE proc = 'api' AND api_end_ts < now() - INTERVAL 1 MINUTE), ew AS (SELECT instance, uniqExact(request_id) AS ne FROM vllm_pd.phase_line_cur WHERE proc = 'api' AND api_end_ts >= t10 AND api_end_ts < now() - INTERVAL 1 MINUTE GROUP BY instance), emp AS (SELECT instance, arraySort(groupUniqArray(pid)) AS pl, length(pl) AS np, sum(f) AS fe, sum(sa) AS se FROM (SELECT instance, pid, argMax(acc_fallback['forward'], ts) AS f, argMax(acc_fallback['sampling'], ts) AS sa FROM vllm_pd.filter_selfcheck WHERE ts > now() - INTERVAL 10 MINUTE GROUP BY instance, pid) GROUP BY instance) SELECT i.instance AS instance, match(i.instance, '^(P0|D0)$') AS this_batch, toString(t10) AS t10_utc, dateDiff('minute', t10, now()) AS window_min, ew.ne AS ended_win, emp.np AS pids, emp.pl AS pid_list, emp.fe AS forward_empty, emp.se AS sampling_empty, x.a_tot AS alloc_total, x.f_tot AS forward_total, x.s_tot AS sampling_total, x.a_win AS alloc_win, x.f_win AS forward_win, x.s_win AS sampling_win, if(x.a_win = 0, NULL, round(100 * (1 - x.f_win / x.a_win), 2)) AS forward_empty_pct, if(x.a_win = 0, NULL, round(100 * (1 - x.s_win / x.a_win), 2)) AS sampling_empty_pct FROM (SELECT DISTINCT instance FROM done) AS i LEFT JOIN (SELECT instance, sum(al) AS a_tot, sum(fw) AS f_tot, sum(sm) AS s_tot, sum(al - al0) AS a_win, sum(fw - fw0) AS f_win, sum(sm - sm0) AS s_win FROM (SELECT l.instance AS instance, l.request_id AS request_id, l.part AS part, toUInt32OrZero(splitByChar('-', l.part)[2]) AS pid, ifNull(max(l.acc__forward__steps), 0) AS fw, ifNull(maxIf(l.acc__forward__steps, l.ts < t10), 0) AS fw0, ifNull(max(l.acc__sampling__steps), 0) AS sm, ifNull(maxIf(l.acc__sampling__steps, l.ts < t10), 0) AS sm0, ifNull(max(l.acc__alloc__steps), 0) AS al, ifNull(maxIf(l.acc__alloc__steps, l.ts < t10), 0) AS al0 FROM vllm_pd.phase_line_cur AS l WHERE l.part != '' AND (l.instance, l.request_id) IN (SELECT instance, request_id FROM done) GROUP BY l.instance, l.request_id, l.part) WHERE (instance, pid) IN (SELECT instance, pid FROM cur) GROUP BY instance) AS x ON i.instance = x.instance LEFT JOIN emp ON i.instance = emp.instance LEFT JOIN ew ON i.instance = ew.instance ORDER BY this_batch DESC, instance SETTINGS join_use_nulls = 0"
        got_r = {r["instance"]: r for r in rows(q_ratio)}
        got_n = {r["instance"]: r for r in rows(q_ratio_now)}
        say(set(got_n) == {"P0", "D0"} and all(int(r["alloc_win"]) == 0 and r["forward_empty_pct"] is None and r["sampling_empty_pct"] is None
                                                  and int(r["alloc_total"]) > 0 for r in got_n.values()),
            "C15 留空比例（操作说明 A 7.3 的第一次查询，t10 = 现在）：窗口里没有步数，两个比例为空，到现在为止的步数照常：%s"
            % "，".join("%s alloc_total %s alloc_win %s" % (k, r["alloc_total"], r["alloc_win"]) for k, r in sorted(got_n.items())))
        say(set(got_r) == set(got_n) == {"P0", "D0"} and all(int(r["this_batch"]) == 1 for r in list(got_r.values()) + list(got_n.values())),
            "C15 留空比例的 this_batch（<这一批的实例> 填 P0|D0）：两个实例两次都是 1")
        say(set(got_r) == set(got_n) and all(int(got_r[k]["pids"]) >= 1 and got_r[k]["pids"] == got_n[k]["pids"] for k in got_n),
            "C15 留空比例的 pids（两次查询各自的进程个数）：两次相同：%s"
            % "，".join("%s %s / %s" % (k, got_n[k]["pids"], (got_r.get(k) or {}).get("pids")) for k in sorted(got_n)))
        for inst_, recs_ in (("P0", recs_p), ("D0", recs_d)):
            a_ = f_ = s_ = 0
            for o in recs_:
                if o.get("version") != 2:
                    continue
                if o["event"] == "core_summary" and o.get("scope") == "execution" and isinstance(((o.get("stages") or {}).get("alloc") or {}).get("duration_ns"), int):
                    a_ += len(o.get("request_ids") or [])
                if o["event"] == "worker_summary" and o.get("tp_rank", 0) == 0 and o.get("request_ids"):
                    sg_ = "forward" if o.get("phase") == "execute" else "sampling" if o.get("phase") == "sample" else None
                    if sg_ and isinstance(((o.get("stages") or {}).get(sg_) or {}).get("device_duration_ns"), int):
                        if sg_ == "forward":
                            f_ += len(o["request_ids"])
                        else:
                            s_ += len(o["request_ids"])
            want_f, want_s = round(100 * (1 - f_ / max(a_, 1)), 2), round(100 * (1 - s_ / max(a_, 1)), 2)
            g_ = got_r.get(inst_) or {}
            say(bool(g_) and abs(float(g_["forward_empty_pct"]) - want_f) <= 0.011 and abs(float(g_["sampling_empty_pct"]) - want_s) <= 0.011,
                "C15 留空比例（操作说明 A 7.3 的第二次查询，t10 早于全部数据）：%s 库里 alloc %s 步、forward %s 步、sampling %s 步，"
                "forward_empty_pct %s%%、sampling_empty_pct %s%%；按日志算 alloc %d、forward %d、sampling %d，%s%%、%s%%"
                % (inst_, g_.get("alloc_win"), g_.get("forward_win"), g_.get("sampling_win"), g_.get("forward_empty_pct"),
                   g_.get("sampling_empty_pct"), a_, f_, s_, want_f, want_s))
    if G53:
        def psub(rid):
            d_ = docs.get(x_of(rid))
            return get(d_, "p", "p.start_load_kv.submit") if d_ else "（库里没有）"
        empty_ok = [x_of(r)[-6:] for r in G53["nonhit"] if psub(r) is None]
        say(len(empty_ok) == len(G53["nonhit"]), "C11：没命中缓存池的 %d 个请求（no_task、all_hit、skipped、missing_load_spec）P submit 为空：%s" % (
            len(G53["nonhit"]), "、".join(empty_ok)))
        for rid, st, dur in G53["loads"]:
            if st in G53["taken"]:
                v = psub(rid)
                say(isinstance(v, (int, float)) and abs(v - dur / 1e6) < 1e-3, "C11：%s 的 P submit 照记录显示：%s ms（记录 %.3f ms）" % (st, v, dur / 1e6))
        v = psub(rid_hot)
        say(isinstance(v, (int, float)) and abs(v - G53["hot_dur"] / 1e6) < 1e-3, "C11：只取 rank 0：热请求 %s ms，rank 1 那条 9 ms 的记录没有算进去" % v)
        tot_ = sum(psub(r) for r, _, _ in G53["loads"] if isinstance(psub(r), (int, float)))
        gap = G53["step_submit"] / 1e6 - tot_
        say(abs(gap - G53["excluded"] / 1e6) < 1e-3, "C11：同一步 %d 个请求的 P submit 之和 %.3f ms，这一步的 stages.submit %.3f ms，差 %.3f ms，"
            "正是不取的 3 条记录（all_hit、skipped、missing_load_spec）之和" % (len(G53["loads"]), tot_, G53["step_submit"] / 1e6, gap))
    # the proxy's own numbers in the JSON (C5), and their absence for the request that did not go through the proxy
    bad = []
    for X, doc in docs.items():
        px = proxy_expect.get(X)
        got = (doc.get("client_request_id"), doc["route"]["retries"], doc["latency_ms"]["e2e"], doc["latency_ms"]["ttft"], doc["latency_ms"]["starts_at"])
        want = (px["client_request_id"], px["retries"], px["e2e_ms"], px["ttft_ms"], "proxy") if px else (None, None, None, None, "instance")
        if not all((a is None and b is None) or a == b or (isinstance(a, float) and isinstance(b, float) and abs(a - b) <= 0.001) for a, b in zip(got, want)):
            bad.append("%s：%s，应为 %s" % (X[-8:], got, want))
        miss_px = "client_request_id" in (doc.get("missing") or [])
        if miss_px != (px is None):
            bad.append("%s：missing 里的 proxy 字段 %s" % (X[-8:], doc.get("missing")))
    say(not bad, "单请求 JSON 的 proxy 字段：有 proxy 行的 %d 个请求取 proxy 的值，没有的为空并列在 missing 里%s" % (
        len(proxy_expect), "；" + "；".join(bad) if bad else ""))
    # P 合计 and D 合计 stay the API server's (C5)
    tot_bad = [X for (side, X), e in expect.items() if docs.get(X) and abs(get(docs[X], side, "total") - e["total"]) > 0.001]
    say(not tot_bad, "P 合计、D 合计是 API server 的口径，不含 proxy 段（%d 个请求）" % len(expect))

    # ---- 47 segments: in how many requests each has a value ----------------------------------------------------------
    def paths(nodes, prefix):
        for nd in nodes:
            yield prefix + nd["id"], nd
            yield from paths(nd.get("children", []), prefix + nd["id"] + ".")
    seg = []
    for side, grp, phases in (("p", "p", ("proxy", "ttft")), ("p", "b", ("background",)), ("d", "d", ("proxy", "ttft")), ("d", "r", ("tpot",))):
        for ph in phases:
            for path, _ in paths(profile["sides"]["prefill" if side == "p" else "decode"].get(ph, []), grp + "."):
                seg.append((side, path))
    have = {}
    for side, path in seg:
        have[(side, path)] = sum(1 for (s_, X) in expect if s_ == side and isinstance(get(docs.get(X, {}), side, path) if docs.get(X) else None, (int, float)))
    filled = [k for k, v in have.items() if v > 0]
    empty = [k for k, v in have.items() if v == 0]
    say(len(seg) == 47 and len(filled) == 47 - len(EMPTY_P) and sorted(empty) == sorted(("p", x) for x in EMPTY_P),
        "47 段里 %d 段有值，为空的：%s" % (len(filled), "、".join(p for _, p in empty) or "无"))
    say(None, "各段有值的请求数：" + "，".join("%s %d" % (p, v) for (_, p), v in sorted(have.items())))

    # ---- bin/coverage.sh's own queries (scripts/coverage.py), with its C9 block ------------------------------------
    import contextlib
    import importlib.util
    import io
    os.environ["SEGMENTS_FILE"] = os.path.join(PACK, "segments.json")
    spec_ = importlib.util.spec_from_file_location("coverage_t51", os.path.join(PACK, "scripts", "coverage.py"))
    cov = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(cov)
    cov.ch = lambda sql: sh(["docker", "exec", "-i", CH, "clickhouse-client", "--user", "vllm_writer", "--password", "x", "-q", sql])
    buf = io.StringIO()
    old_argv = sys.argv
    sys.argv = ["coverage.py", "200", "100000"]      # the rc1 logs are days old
    try:
        with contextlib.redirect_stdout(buf):
            rc_cov = cov.main()
    finally:
        sys.argv = old_argv
    cov_out = buf.getvalue().splitlines()
    say(rc_cov == 0 and any(l.startswith("47 段里 %d 段有值" % (47 - len(EMPTY_P))) for l in cov_out), "coverage.sh：%s" % " / ".join(cov_out[-2:]))
    keys = ["p.start_load_kv.submit", "p.start_load_kv.wait_for_transfer", "b.wait_for_save"] + \
           ["b.wait_for_save." + s_ for s_ in ("build_key", "lookup", "prepare", "sync", "put", "put.submit", "put.wait_for_transfer", "put.finalize")]
    keys = [k_ for k_ in keys if k_ not in EMPTY_P]
    rids = sorted({k_[0] for k_ in src_max})
    want = {}
    for key in keys:
        hv = fs = 0
        for rid in rids:
            own, shv = src_max.get((rid, key, False)), src_max.get((rid, key, True))
            if own is None and shv is None:
                continue
            hv += 1
            fs += 1 if shv is not None and (own is None or shv >= own) else 0
        want[key] = (fs, hv)
    any_h = sum(1 for rid in rids if any(src_max.get((rid, k_, False)) is not None or src_max.get((rid, k_, True)) is not None for k_ in keys))
    any_s = sum(1 for rid in rids if any(src_max.get((rid, k_, True)) is not None and (src_max.get((rid, k_, False)) is None
                                                                                      or src_max[(rid, k_, True)] >= src_max[(rid, k_, False)]) for k_ in keys))
    got = {}
    head = [i for i, l in enumerate(cov_out) if "来自共用任务" in l]
    for l in cov_out[head[0] + 1:] if head else []:
        m_ = re.match(r"^  (P (?:后台 )?\S+|这几段里任一段)\s+(\d+) / (\d+)", l)
        if m_:
            got[m_.group(1)] = (int(m_.group(2)), int(m_.group(3)))
    name = lambda k_: "P %s%s" % ("后台 " if k_.startswith("b.") else "", k_.split(".", 1)[1].replace(".", "/"))
    bad = ["%s：%s，应为 %s" % (name(k_), got.get(name(k_)), v) for k_, v in want.items() if got.get(name(k_)) != v]
    if got.get("这几段里任一段") != (any_s, any_h):
        bad.append("任一段：%s，应为 %s" % (got.get("这几段里任一段"), (any_s, any_h)))
    say(not bad and len(got) == len(keys) + 1, "coverage.sh 的共用任务一项（C9）：%d 段和「任一段」与直接从日志算的相同%s" % (
        len(keys), "；" + "；".join(bad) if bad else ""))
    say(None, "coverage.sh 的共用任务一项：" + "，".join("%s %d/%d" % (k_, a, b) for k_, (a, b) in got.items()))

    # ---- every dashboard query runs -----------------------------------------------------------------------------------
    board = json.load(open(os.path.join(PACK, "generated", "vllm-pd-request.json")))
    one = sorted(docs)[0] if docs else ""
    sub_ = {"$__timeFilter(ts)": "ts >= now() - INTERVAL 1 HOUR", "$__fromTime": "(now() - INTERVAL 1 HOUR)", "$__toTime": "now()",
            "'$model'": "'glm-5.2'", "${instance:singlequote}": "'P0','D0'", "${request_id}": one, "${order_by:text}": "x"}
    failed, empty_q, total_q = [], [], 0

    def flat(ps):
        for p_ in ps:
            yield p_
            yield from flat(p_.get("panels") or [])
    for order in ("d_e2e_ms", "p_ttft_ms", "p_total_ms", "d_total_ms", "tpot_ms"):
        for p_ in flat(board["panels"]):
            for t in p_.get("targets") or []:
                q = t.get("rawSql")
                if not q:
                    continue
                total_q += 1
                for kk, v in sub_.items():
                    q = q.replace(kk, v)
                q = q.replace("${order_by}", order)
                r = subprocess.run(["docker", "exec", "-i", CH, "clickhouse-client", "--user", "vllm_writer", "--password", "x", "-n"],
                                   input=q + " FORMAT JSONEachRow;\n", stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                if r.returncode:
                    failed.append("%s [%s]: %s" % (p_.get("title"), order, r.stderr.strip().splitlines()[0][:200] if r.stderr.strip() else "?"))
                elif not r.stdout.strip():
                    empty_q.append("%s [%s]" % (p_.get("title"), order))
    say(not failed, "看板查询：%d 次（5 种排序依据），出错 %d" % (total_q, len(failed)))
    for f_ in failed[:6]:
        say(None, "  " + f_)
    say(None, "没有数据的查询：%d（%s）" % (len(empty_q), "；".join(sorted(set(e.split(" [")[0] for e in empty_q)))[:300]))
finally:
    cleanup()
    with open(os.path.join(WORK, "e2e-%s.txt" % PROFILE), "w") as f:
        f.write("\n".join(out_lines) + "\n")
fails = sum(1 for x in out_lines if x.startswith("FAIL"))
print("%d FAIL" % fails)
sys.exit(1 if fails else 0)
