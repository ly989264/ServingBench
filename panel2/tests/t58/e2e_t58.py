#!/usr/bin/env python3
"""T56 (pack two v3.1-extra, D's new segments as docs/tasks/T56/32_精简落地决定 section five defines them): end to end on
this machine, as tests/t51/e2e_v31.py (this is a copy of it with the T56 parts added), in throw-away containers (Vector
0.61.0, ClickHouse 26.8.36.3, --rm).

  python3 -I tests/t56/e2e_t56.py <pack> <work> <P.log> <D.log> [glm53|glm53-sum|glm53-19c] [shift]

  glm53       profiles/glm53/segments.json, 甲: start_load_kv takes the receive thread's wall-clock envelope; 3 comparison rows
  glm53-sum   乙, given as E2E_SEGMENTS=tests/t56/profile-glm53-sum/segments.json (not in the pack: the user picked 甲):
              start_load_kv the sum of its L2s; a 4th comparison row, the envelope
  glm53-19c   the 19c segment definitions, given as E2E_SEGMENTS=<its segments.json>: the old panel on the new records
  E2E_T56_RECORDS=0    without the T56 records: the new panel on the old patch's records (new segments empty, no error)
  E2E_REF_DOCS=<docs.json of an e2e_v31 glm53 run on the same logs>: every segment of the 19c panel compared with this
              run's, request by request (with the T56 records: but D's start_load_kv, whose value the T56 definitions change)

The rc1 logs are made into the glm5.3 shape as e2e_v31 does (its docstring), D's retried request's first attempt also
without vLLM's times (official queued_time, prefill_time, decode_time null: it never ran a step). Then the T56 records
(tests/t56/t56_records.py: record format 1.4.0, 10_ frozen de58c746) are added to D's log, after D's copies:
  - 16 reading tasks for each request that read from P, the three maxima (F3) and kv_load_envelope_ns on its rank-0
    request_generation, and a rank-1 copy of that with ten times the values on a Worker process of its own (TP > 1);
  - kv_ready_wait on core_summary[request] of the requests that read from P (the direct request and its copies: none);
  - prepare_inputs, lm_head, bookkeeping, draft on the Worker records (device time on rank 0), step_device_span_ns,
    cudagraph_runtime_mode (the cold request's first step NONE, other first steps PIECEWISE, later steps FULL); the copy
    with the host time only (C15) has no device time on them either, and the cold request's second step has
    prepare_inputs with the host time only;
  - of the hot request's copies on D, hotn has no kv_load_envelope_ns; hotu, hote, hots each break one item of the
    first-batch check (32_ section three: 2, 3, 1b), and a step of the cold request breaks 1a.
Checks, besides e2e_v31's: every value of the per-request JSON (the new segments and comparison rows too) against values
computed here from the logs; the first-batch check's items 1-3 on the records and, where the dashboard has the values,
on its per-request JSON, each finding exactly what was made to break it (4: the filter's acc_fallback; 5: the graph-mode
rules); the five rules no segment reads (step_device_span, graph_full / _piecewise / _none, bookkeeping) in ClickHouse against the
records; the three inequalities of 10_ section 3 and the maxima on the records; the distribution tables' 「有值 / 请求」
column (T55); the rows without a share (comparison rows) in the distribution table and the single-request table;
56 segments (19c: 47), the comparison rows apart, and coverage.sh's count, comparison block and reason text.
Writes <work>/e2e-<run>.txt. Removes its containers and network at the end (E2E_KEEP=1 keeps them).
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
PROFILE = sys.argv[5] if len(sys.argv) > 5 else "glm53"
SHIFT = len(sys.argv) > 6 and sys.argv[6] == "shift"     # C14: rank-0 worker records written 1-2 steps later
if PROFILE not in ("glm53", "glm53-sum", "glm53-19c"):
    raise SystemExit("段定义只能是 glm53、glm53-sum、glm53-19c")
IS_G53 = True
T56REC = os.environ.get("E2E_T56_RECORDS", "1") != "0"
SEG_FILE = os.environ.get("E2E_SEGMENTS")
if PROFILE in ("glm53-19c", "glm53-sum") and not SEG_FILE:
    raise SystemExit("%s 要用 E2E_SEGMENTS 给出它的 segments.json" % PROFILE)
REF_DOCS = os.environ.get("E2E_REF_DOCS")
# T58 (v3.3-extra): the PD_DIAG_LEVEL the P and D records are written at (3, the default, is T56's records unchanged;
# 1 and 2 take out what W1's T58 design, part 乙 section 2, does not record there and add diag_level to the request
# summaries), and the dashboard's 层级 the board queries run at.
LEVEL_P = int(os.environ.get("E2E_LEVEL_P", "3"))
LEVEL_D = int(os.environ.get("E2E_LEVEL_D", "3"))
BOARD_LEVEL = os.environ.get("E2E_BOARD_LEVEL", "3")
RUN = PROFILE + ("" if T56REC else "-oldrec") + ("-shift" if SHIFT else "") + ("" if (LEVEL_P, LEVEL_D) == (3, 3) else "-L%d%d" % (LEVEL_P, LEVEL_D))
G53 = IS_G53     # C12, the refined C2, C15 and missing_execution_id are in the glm53 definitions only
T56_RG = ("task_max_queue_wait_ns", "task_max_meta_fetch_ns", "task_max_prepare_ns", "kv_load_envelope_ns")
# C15: a copy of the direct request whose Worker records have the host time only (glm53; E2E_HOSTONLY=0 leaves it out,
# to compare the other requests with a run without it)
HOSTONLY = G53 and os.environ.get("E2E_HOSTONLY", "1") != "0"
COPY_TAGS = ("noexec", "partexec", "retry") + (("hostonly",) if HOSTONLY else ())
hostonly_x = None
NET, CH, VEC = "t58e-net", "t58e-ch", "t58e-vec"
CH_IMG, VEC_IMG = "clickhouse/clickhouse-server:26.8.36.3", "timberio/vector:0.61.0-debian"
HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, "..", "t41", "tools")
T56DIR = os.path.join(HERE, "..", "t56")
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
shutil.copy(SEG_FILE or os.path.join(PACK, "profiles", PROFILE, "segments.json"), os.path.join(PACK, "segments.json"))
profile = json.load(open(os.path.join(PACK, "segments.json")))
say(None, "段定义：%s（config_id %s）；T56 的记录：%s" % (SEG_FILE or "profiles/%s/segments.json" % PROFILE, profile["config_id"],
                                                    "加上" if T56REC else "不加（旧补丁的记录）"))
# T56: the profile's segments ("p.x", "b.x", "d.x", "r.x") and comparison rows, those outside the side's total
PROFILE_KEYS, COMPARE_KEYS, OUTSIDE_KEYS = set(), set(), set()


def _walk(nodes, prefix):
    for nd in nodes:
        (COMPARE_KEYS if nd.get("compare") else PROFILE_KEYS).add(prefix + nd["id"])
        if nd.get("outside_total") or nd.get("compare"):
            OUTSIDE_KEYS.add(prefix + nd["id"])
        _walk(nd.get("children") or [], prefix + nd["id"] + ".")


for _side, _grps in (("prefill", (("p", "proxy"), ("p", "ttft"), ("b", "background"))),
                     ("decode", (("d", "proxy"), ("d", "ttft"), ("r", "tpot")))):
    for _g, _ph in _grps:
        _walk(profile["sides"][_side].get(_ph) or [], _g + ".")
T56_PROFILE = "d.kv_wait_vllm" in COMPARE_KEYS
NEW_STEP = ("prepare_inputs", "lm_head", "draft")
NEW_D = ("d.start_load_kv.receive_queue", "d.start_load_kv.prepare_read", "d.kv_ready_wait") + \
    tuple(g_ + "." + s_ for g_ in ("d", "r") for s_ in NEW_STEP)
EMPTY_D = NEW_D if T56_PROFILE and not T56REC else ()
EMPTY_CMP = ("d.recv_envelope",) if "d.recv_envelope" in COMPARE_KEYS and not T56REC else ()
# 甲: start_load_kv has a value of its own (the envelope), its children are not added up
ENV_PARENT = bool(next(n for n in profile["sides"]["decode"]["ttft"] if n["id"] == "start_load_kv").get("src"))
HIDDEN = ("step_device_span", "graph_full", "graph_piecewise", "graph_none", "bookkeeping")   # rules no segment reads (10_ 5 end)
# {accumulate id: (phase, where, path)}: the Worker's per-step sums of the profile (forward, sampling; with T56 the new ones
# and the five rules no segment reads)
WORKER_ACC, ACC_SINGLE = {}, {}
for _a in profile["accumulate"]:
    if _a.get("event") == "worker_summary" and (_a.get("where") or {}).get("phase") in ("execute", "sample"):
        _v = _a["value"][0] if isinstance(_a["value"], list) else _a["value"]
        WORKER_ACC[_a["id"]] = (_a["where"]["phase"], dict(_a["where"]), tuple(_v.split(".")))
        ACC_SINGLE[_a["id"]] = isinstance(_a["value"], list) and len(_a["value"]) == 1
DEVICE_ONLY = tuple(k_ for k_, (_, _, p_) in WORKER_ACC.items() if p_[-1] == "device_duration_ns" and k_ not in HIDDEN)
SEG_ACC = tuple(k_ for k_ in WORKER_ACC if k_ not in HIDDEN)


def acc_value(o, path):
    for k_ in path:
        o = o.get(k_) if isinstance(o, dict) else None
    return o


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
if IS_G53:
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
            if not IS_G53 and o.get("event") == "pool_task" and o.get("operation") == "load" and o.get("request_id") == rid_hot:
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
if IS_G53:
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


if IS_G53:
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
                            for f_ in ("queued_time", "prefill_time", "decode_time"):   # T56: nor has it vLLM's times
                                (r.get("official") or {})[f_] = None
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

# ---- T56: the new records on D (tests/t56/t56_records.py) ----------------------------------------------------------
sys.path.insert(0, T56DIR)
sys.path.insert(0, HERE)
import t56_records  # noqa: E402
import native_put_v32  # noqa: E402  (v3.2-extra's glog parser: the expected values and the conversion to Mooncake's files)
t56 = {"rg": {}, "kv_ready_wait": {}, "violators": {}, "tasks": {}, "env_null": None}
tag_d = {t_: cd for cd, _, t_ in d_clones}
d_req_ids = {x_of(o["request_id"]): o["request_id"] for o in load(aug_d) if o.get("event") == "api_summary" and o.get("scope") == "request"}
rid_cold_d = d_req_ids[x_of(rid_cold)]
if T56REC:
    t56_lines, t56 = t56_records.augment(open(aug_d, errors="replace").readlines(), {
        "env_null": tag_d["hotn"], "E1": rid_cold_d, "E3": tag_d["hotu"], "E4": tag_d["hote"], "E5": tag_d["hots"],
        "host_only_suffix": "-ho" if HOSTONLY else None, "host_only_record": (rid_cold_d, 1), "graph_none": rid_cold_d,
        "big_prep": rid_dir})
    aug_t56 = os.path.join(WORK, "D.t56.log")
    with open(aug_t56, "w") as f:
        f.writelines(t56_lines)
    aug_d = aug_t56
    say(None, "T56 的记录（D）：从 P 读 KV 的 %d 个请求各 16 个读取任务，它们的 request_generation 带 3 个最大值和包络，各跟一条 rank 1 的副本（%d 条）；"
        "等调度 %d 个请求；Worker 记录加 4 个 stage %d 处、step_device_span_ns %d 条；%s 没有包络；造来不满足等式的：%s；%s 第 2 步的 prepare_inputs 只有主机时长"
        % (len(t56["rg"]), t56["rank1"], len(t56["kv_ready_wait"]), t56["worker_stages"], t56["spans"], x_of(t56["env_null"]),
           "，".join("%s %s" % (k_, v_[-12:] if v_.startswith("execution") else x_of(v_)) for k_, v_ in sorted(t56["violators"].items())),
           x_of(rid_cold_d)))

# ---- T58: the records as the patches write them at PD_DIAG_LEVEL 1 or 2 (W1's T58 design, part 乙 section 2) ----------
def at_level(path, level, side):
    """Rewrites a node log in place: diag_level on the request summaries; what the level does not record taken out."""
    if level >= 3:
        return {}
    n = collections.Counter()
    out_ = path + ".lv"
    with open(path, errors="replace") as src, open(out_, "w") as dst:
        for line in src:
            i = line.find("[PD_DIAG] ")
            if i < 0:
                dst.write(line)
                continue
            try:
                o = json.loads(line[i + 10:])
            except ValueError:
                dst.write(line)
                continue
            ev, sc = o.get("event"), o.get("scope")
            if (ev, sc) in (("api_summary", "request"), ("core_summary", "request")):
                o["diag_level"] = level
                n["diag_level"] += 1
            if side == "p" and ev == "pool_task" and o.get("operation") == "save":
                if o.pop("native_calls", None) is not None:
                    n["native_calls"] += 1
                if level == 1:
                    for s_ in ("build_key", "lookup", "prepare", "sync", "put"):
                        if (o.get("stages") or {}).pop(s_, None) is not None:
                            n["save_stage"] += 1
            if side == "d" and ev == "worker_summary" and o.get("phase") == "sample":
                if (o.get("stages") or {}).pop("bookkeeping", None) is not None:
                    n["bookkeeping"] += 1
            if side == "d" and level == 1:
                if ev == "pd_transfer_summary" and sc == "request_generation":
                    for f_ in ("task_max_queue_wait_ns", "task_max_meta_fetch_ns", "task_max_prepare_ns", "transfer_envelope_ns"):
                        if o.pop(f_, None) is not None:
                            n["rg_field"] += 1
                if ev == "worker_summary" and o.get("phase") == "execute":
                    if (o.get("stages") or {}).pop("submit", None) is not None:
                        n["submit"] += 1
            dst.write(line[:i + 10] + json.dumps(o, separators=(",", ":")) + "\n")
    os.replace(out_, path)
    return dict(n)


for side_, path_, lv_ in (("p", aug_p, LEVEL_P), ("d", aug_d, LEVEL_D)):
    if lv_ < 3:
        say(None, "T58：%s 侧按 %d 级写：%s" % (side_.upper(), lv_, at_level(path_, lv_, side_)))

def _ids(nodes, prefix):
    for nd in nodes:
        yield prefix + nd["id"], nd
        yield from _ids(nd.get("children", []), prefix + nd["id"] + ".")


LV_SKIP = {path_ for side_, grp_, phases_ in (("p", "p", ("proxy", "ttft")), ("p", "b", ("background",)), ("d", "d", ("proxy", "ttft")), ("d", "r", ("tpot",)))
           for ph_ in phases_ for path_, nd_ in _ids(profile["sides"]["prefill" if side_ == "p" else "decode"].get(ph_, []), grp_ + ".")
           if nd_.get("from_level", 1) > (LEVEL_P if side_ == "p" else LEVEL_D)}

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
if LEVEL_P < 3:
    native_lines = []      # Mooncake writes no file below level 3
# T58: Mooncake's own files (patch 04b): each glog line as the JSON line it becomes, in mc.<start>.<pid>.0000.log of the
# process whose pool records name its thread; UTC wall_ns from the line's local time and the calls' offset.
pool_tid_pid, off_s, year_of = {}, None, None
for root_, _, fs_ in os.walk(os.path.join(pull, "n-P0")):
    for f_ in fs_:
        m_ = re.match(r"^pool\.\d{8}T\d{6}\.(\d+)\.\d{4}\.log$", f_)
        if not m_:
            continue
        for l_ in open(os.path.join(root_, f_), errors="replace"):
            if '"pool_task"' not in l_ or "[PD_DIAG] " not in l_:
                continue
            o_ = json.loads(l_[l_.index("[PD_DIAG] ") + 10:])
            for c_ in o_.get("native_calls") or []:
                if isinstance(c_, dict) and "native_tid" in c_:
                    pool_tid_pid.setdefault(int(c_["native_tid"]), int(m_.group(1)))
                    if off_s is None:
                        off_s = int(c_.get("wall_utc_offset_seconds") or 0)
                        year_of = (datetime.datetime(1970, 1, 1) + datetime.timedelta(microseconds=int(c_["start_wall_ns"]) // 1000)).year
mc_by_pid = collections.defaultdict(list)
for l_ in native_lines:
    nn = native_put_v32.parse_native(l_)
    if nn is None:
        continue
    pid_ = pool_tid_pid.get(nn["tid"], 1)
    loc = native_put_v32.native_local_us(nn, year_of or 2026)
    e_ = {"event": "mc_batch_put" if "[BatchPutTiming]" in l_ else "mc_batch_upsert", "version": 1, "pid": pid_, "native_tid": nn["tid"],
          "submitted_keys": nn["keys"], "submitted_bytes": nn["bytes"], "wall_ns": loc * 1000 - (off_s or 0) * 1000000000,
          "timing_scope": nn["scope"], "status": nn["status"], "complete": nn["complete"]}
    for _, f_ in native_put_v32.PHASES:
        if nn.get(f_) is not None:
            e_[f_] = nn[f_]
    mc_by_pid[pid_].append(e_)
mc_dir = os.path.join(pull, "n-P0", "mooncake")
for pid_, es_ in mc_by_pid.items():
    os.makedirs(mc_dir, exist_ok=True)
    with open(os.path.join(mc_dir, "mc.%s.%d.0000.log" % (time.strftime("%Y%m%dT%H%M%S"), pid_)), "w") as f:
        f.write("[PD_DIAG] " + json.dumps({"event": "pd_file_open", "version": 1, "source": "mooncake", "pid": pid_}) + "\n")
        for e_ in es_:
            f.write("[PD_DIAG] " + json.dumps(e_, separators=(",", ":")) + "\n")
        f.write("[PD_DIAG] " + json.dumps({"event": "mc_timing_stats", "written": len(es_), "dropped": 0, "queue_max": 1}) + "\n")
say(None, "T58：Mooncake 自己的文件 %d 个（%s），共 %d 行计时" % (len(mc_by_pid), "、".join("pid %d：%d 行" % (k, len(v)) for k, v in sorted(mc_by_pid.items())),
                                                    sum(len(v) for v in mc_by_pid.values())))
sys.path.insert(0, os.path.join(PACK, "scripts"))
import native_put  # noqa: E402
state = os.path.join(WORK, "puller-state")
os.makedirs(state)
json.dump({"started_v33": True}, open(os.path.join(state, "native.json"), "w"))
nat = native_put.Puller({}, pull, state)
nat.run_once()
say(nat.assoc.counts["records_written"] == len(native_lines), "拉取程序的 Mooncake 配对（读镜像里的 mc.*.log）：%s" % nat.assoc.counts)
nat_recs = []
for root_, _, fs_ in os.walk(os.path.join(pull, "_native")):
    for f_ in fs_:
        for l_ in open(os.path.join(root_, f_), errors="replace"):
            if "[PD_DIAG] " in l_:
                nat_recs.append(json.loads(l_[l_.index("[PD_DIAG] ") + 10:]))
say(nat.assoc.counts["lines_unparsed"] == 0, "Mooncake 计时行全部解析成功（lines_unparsed %d）" % nat.assoc.counts["lines_unparsed"])
if G53 and LEVEL_P >= 3:
    shr_ = [o for o in nat_recs if o.get("task_id") == "pool-shared-save"]
    st_ = shr_[0]["stages"] if len(shr_) == 1 else {}
    say(st_.get("submit") == {"duration_ns": 500000} and st_.get("wait_for_transfer") == {"duration_ns": 1500000}
        and st_.get("finalize") == {"duration_ns": 300000} and "start_batch_put" not in st_,
        "旧签名包装那一行（glog 0.6，complete=false）：pd_native_put 的 submit / wait_for_transfer / finalize 是 0.5 / 1.5 / 0.3 ms，"
        "没有 start_batch_put（缺项，不是 0）：%s" % json.dumps(st_))

# The proxy's lines for the requests that went through it.
recs_p, recs_d = load(aug_p), load(aug_d)
for side_, recs_ in (("p", recs_p), ("d", recs_d)):     # T56: the new one-path Worker sums, left empty where the stage has no value
    for o in recs_:
        if o.get("event") == "worker_summary" and o.get("tp_rank", 0) == 0 and o.get("request_ids") and o.get("version") == 2:
            for kk, (ph_, wh_, path_) in WORKER_ACC.items():
                if kk not in ("forward", "sampling") and ACC_SINGLE[kk] and not any(o.get(a_) != b_ for a_, b_ in wh_.items()) \
                        and isinstance(acc_value(o, path_[:-1]), dict) and acc_value(o, path_) is None:
                    fb_want[(side_, kk)] += 1
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
dropped_keys = set()      # T56: values computed here for segments the profile does not have (19c: the 9 new ones)
hidden_exp = collections.defaultdict(dict)     # T56: request -> rule no segment reads -> (ns, steps, first_ns)
rgx_x = {}                                     # T56: request key -> the new request_generation fields (D)
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
            # C15 (glm53): the device time only, a step without one adds nothing. T56: every Worker sum of the profile
            # (with T56 the three new stages, device time, and the five rules no segment reads)
            for kk, (ph_, wh_, path_) in WORKER_ACC.items():
                if any(o.get(a_) != b_ for a_, b_ in wh_.items()):
                    continue
                v = acc_value(o, path_)
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
    rgx = collections.defaultdict(dict)          # T56: request -> the new request_generation fields (rank 0)
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
        # rank 0 only, as the filter keeps (T56 adds a rank-1 copy with ten times the values)
        if o["event"] == "pd_transfer_summary" and o.get("scope") == "request_generation" and o.get("tp_rank", 0) == 0:
            if isinstance(o.get("transfer_envelope_ns"), int):
                env[o["request_id"]] = max(env.get(o["request_id"], 0), o["transfer_envelope_ns"])
            for f_ in T56_RG:
                if isinstance(o.get(f_), int):
                    rgx[o["request_id"]][f_] = max(rgx[o["request_id"]].get(f_, 0), o[f_])
    native = collections.defaultdict(dict)       # request -> phase field -> largest over its save tasks' lines
    if side == "p":
        for n_ in native_lines:
            nn = native_put_v32.parse_native(n_)
            for rid, os_ in tasks_of.items():
                for o in os_:
                    for c in native_put_v32.save_calls(o):
                        t = native_put_v32.native_local_us(nn, (datetime.datetime(1970, 1, 1) + datetime.timedelta(microseconds=c[1])).year)
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
            # T56: 接收排队 and 准备读表 (two maxima added up) under start_load_kv; 甲: start_load_kv is the envelope when
            # there is one, else (and 乙 always) the sum of its children; 等调度 an L1
            rv_ = rgx.get(rid) or {}
            if rv_:
                rgx_x[x_of(rid)] = rv_
            kvr_ = (c["stages"].get("kv_ready_wait") or {}).get("duration_ns")
            prd_ = [rv_.get(f_) for f_ in ("task_max_meta_fetch_ns", "task_max_prepare_ns") if rv_.get(f_) is not None]
            l2_ = {"d.start_load_kv.receive_queue": ms(rv_.get("task_max_queue_wait_ns")), "d.start_load_kv.prepare_read": ms(sum(prd_)) if prd_ else None}
            e.update(l2_)
            e["d.kv_ready_wait"] = ms(kvr_)
            kids_ = [v_ for k_, v_ in [("d.start_load_kv.submit", sub_ms), ("d.start_load_kv.wait_for_transfer", wft_ms)] + list(l2_.items())
                     if k_ in PROFILE_KEYS]
            e["d.start_load_kv"] = None if all(x_ is None for x_ in kids_) else sum(x_ or 0 for x_ in kids_)
            if ENV_PARENT and rv_.get("kv_load_envelope_ns") is not None:
                e["d.start_load_kv"] = ms(rv_["kv_load_envelope_ns"])
            for kk in HIDDEN:       # the rules no segment reads: raw sums, steps and the first step, as the filter keeps them
                if kk in WORKER_ACC and (sg[kk] or kk not in hidden_exp[x_of(rid)]):
                    hidden_exp[x_of(rid)][kk] = (sum(x_ for _, x_ in sg[kk]) if sg[kk] else None, len(sg[kk]) or None,
                                                 None if not sg[kk] or first.get(rid) is None or any(e2 is None for e2, _ in sg[kk])
                                                 else next((x_ for e2, x_ in sg[kk] if e2 == first[rid]), 0))   # the filter: 0
            off_ = o.get("official") or {}
            sec_ = lambda f_: off_[f_] * 1000 if isinstance(off_.get(f_), (int, float)) else None
            e.update({"d.recv_envelope": ms(rv_.get("kv_load_envelope_ns")), "d.kv_wait_vllm": sec_("queued_time"),
                      "d.first_step_vllm": sec_("prefill_time"), "r.rest_vllm": sec_("decode_time")})
            for kk in NEW_STEP:
                e["d." + kk], e["r." + kk] = fst(kk), rest(kk)
            expect_missing += not rg
            if G53 and (cv.get((rid, "cand_alloc")) or cv.get((rid, "cand_other"))):
                c2_left.append((X, ms(cv.get((rid, "cand_alloc"))), ms(cv.get((rid, "cand_other")))))
        dropped_keys.update(k_ for k_ in e if k_ not in PROFILE_KEYS and k_ not in COMPARE_KEYS and k_ not in ("total", "steps"))
        e = {k_: v_ for k_, v_ in e.items() if k_ in PROFILE_KEYS or k_ in COMPARE_KEYS or k_ in ("total", "steps")}
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
say(not (dropped_keys - set(NEW_D) - {"d.recv_envelope", "d.kv_wait_vllm", "d.first_step_vllm", "r.rest_vllm"}),
    "期望值里不在这份段定义里的（只应是 T56 的新段、对照行）：%s" % ("、".join(sorted(dropped_keys)) or "无"))
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
    if LEVEL_P < 3:
        want_nat = 0          # T58: no Mooncake file below level 3
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
        want_.update({(s_, kk): fb_want.get((s_, kk), 0) for s_ in ("p", "d") for kk in WORKER_ACC if kk not in ("forward", "sampling") and ACC_SINGLE[kk]})
        say(all((got_.get(kk) or 0) == v for kk, v in want_.items()) and all(v > 0 for kk, v in want_.items() if kk[1] in ("forward", "sampling")),
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
        segs_ = tuple(g_ + "." + k_ for k_ in DEVICE_ONLY for g_ in ("d", "r"))     # T56: and the new device-only sums
        others = ("d.schedule", "d.schedule.alloc", "r.schedule.alloc", "d.detokenizer", "r.detokenizer", "d.req_resolve", "d.tokenizer",
                  "d.add_request", "total")
        vals = {kk: get(doc, "d", kk) for kk in segs_ + others}
        ref = {kk: get(dsd, "d", kk) for kk in others}
        ok_ = doc and all(vals[kk] is None for kk in segs_) and all(
            isinstance(vals[kk], (int, float)) and isinstance(ref[kk], (int, float)) for kk in others)
        st_ho, st_dir = get(doc, "d", "steps"), get(dsd, "d", "steps")
        ok_ = ok_ and st_ho == 0
        say(ok_, "C15（只有 host 时间的副本）：第一步、第二步起的 " + "、".join(DEVICE_ONLY) + " 都为空；第二步起的步数 %s（步数只数有设备时间的 forward 步，"
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
    seg, cmp_ = [], []
    for side, grp, phases in (("p", "p", ("proxy", "ttft")), ("p", "b", ("background",)), ("d", "d", ("proxy", "ttft")), ("d", "r", ("tpot",))):
        for ph in phases:
            for path, nd in paths(profile["sides"]["prefill" if side == "p" else "decode"].get(ph, []), grp + "."):
                (cmp_ if nd.get("compare") else seg).append((side, path))
    have = {}
    for side, path in seg + cmp_:
        have[(side, path)] = sum(1 for (s_, X) in expect if s_ == side and isinstance(get(docs.get(X, {}), side, path) if docs.get(X) else None, (int, float)))
    filled = [k for k in seg if have[k] > 0]
    empty = [k for k in seg if have[k] == 0]
    # T58: the segments above a side's level are empty by design ("按粒度不记")
    BY_LEVEL = []
    for side_, grp_, phases_ in (("p", "p", ("proxy", "ttft")), ("p", "b", ("background",)), ("d", "d", ("proxy", "ttft")), ("d", "r", ("tpot",))):
        for ph_ in phases_:
            for path_, nd_ in paths(profile["sides"]["prefill" if side_ == "p" else "decode"].get(ph_, []), grp_ + "."):
                if nd_.get("from_level", 1) > (LEVEL_P if side_ == "p" else LEVEL_D) and not nd_.get("compare"):
                    BY_LEVEL.append((side_, path_))
    want_empty = sorted([("p", x) for x in EMPTY_P] + [("d", x) for x in EMPTY_D] + BY_LEVEL)
    N_SEG = 56 if T56_PROFILE else 47
    say(len(seg) == N_SEG and len(filled) == N_SEG - len(want_empty) and sorted(empty) == want_empty,
        "%d 段里 %d 段有值，为空的：%s" % (len(seg), len(filled), "、".join(p for _, p in empty) or "无"))
    if cmp_:
        cmp_empty = sorted(p for s_, p in cmp_ if have[(s_, p)] == 0)
        say(len(cmp_) == len(COMPARE_KEYS) == (3 if ENV_PARENT else 4) and cmp_empty == sorted(EMPTY_CMP), "对照行 %d 行（不是段，不计入段数），为空的：%s（应为：%s）" % (
            len(cmp_), "、".join(cmp_empty) or "无", "、".join(EMPTY_CMP) or "无"))
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
    cov_name = lambda k_: "D %s%s" % ("第二步起 " if k_.startswith("r.") else "", k_.split(".", 1)[1].replace(".", "/"))
    line_ok = any(l.startswith("%d 段里 %d 段有值" % (N_SEG, N_SEG - len(EMPTY_P) - len(EMPTY_D) - len(BY_LEVEL))) for l in cov_out)
    if BY_LEVEL:
        bl_ = [l for l in cov_out if l.startswith("按粒度不记的")]
        m_ = re.search(r"按粒度不记的 (\d+) 段", bl_[0]) if bl_ else None
        say(bool(m_) and int(m_.group(1)) == len(BY_LEVEL), "T58 coverage.sh：按粒度不记的 %d 段（应为 %d 段）：%s" % (
            int(m_.group(1)) if m_ else -1, len(BY_LEVEL), bl_[0] if bl_ else "没有这一行"))
    if EMPTY_D or EMPTY_CMP:
        # the new panel on the old records: the new segments and the receive thread's row are empty, and only they
        want_fail = sorted(cov_name(k_) for k_ in EMPTY_D + EMPTY_CMP)
        m_ = re.search(r"RESULT: FAIL（有来源但这些请求里都为空的 (\d+) 段：(.*)）", "\n".join(cov_out))
        got_fail = sorted(m_.group(2).split("、")) if m_ else None
        say(rc_cov == 1 and line_ok and got_fail == want_fail, "coverage.sh（旧补丁的记录）：为空的正是 %d 个新段%s：%s"
            % (len(EMPTY_D), "和接收线程（墙钟）那一行" if EMPTY_CMP else "", " / ".join(cov_out[-2:])))
    else:
        say(rc_cov == 0 and line_ok, "coverage.sh：%s" % " / ".join(cov_out[-2:]))
    if T56_PROFILE:
        i_ = [i for i, l in enumerate(cov_out) if l.startswith("对照行")]
        got_c = {}
        for l in cov_out[i_[0] + 1:i_[0] + 5] if i_ else []:
            m_ = re.match(r"^  (D (?:第二步起 )?\S+)\s+(\d+) / (\d+)", l)
            if m_:
                got_c[m_.group(1)] = int(m_.group(2))
        want_c = {cov_name(p): have[(s_, p)] for s_, p in cmp_}
        say(got_c == want_c, "coverage.sh 的对照行（另列，不计入段数）：%s（按单请求 JSON 数：%s）" % (got_c, want_c))
        reason_ = [l for l in cov_out if l.startswith("  D kv_ready_wait")]
        say(bool(reason_) and "比字节到齐晚一步" in reason_[0], "coverage.sh 等调度一行的原因文字：%s" % (reason_[0].strip() if reason_ else "没有这一行"))
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

    # ---- T56: the records, the five equations, T55, the rows without a share, the 19c panel's values ----------------
    sub_t = {"$__timeFilter(ts)": "ts >= now() - INTERVAL 1 HOUR", "$__fromTime": "(now() - INTERVAL 1 HOUR)", "$__toTime": "now()",
             "'$model'": "'glm-5.2'", "${instance:singlequote}": "'P0','D0'", "${request_id}": x_of(rid_cold_d), "${order_by:text}": "x",
             "${level}": BOARD_LEVEL}

    def run_q(q):
        for kk, v in sub_t.items():
            q = q.replace(kk, v)
        return rows(q.replace("${order_by}", "d_e2e_ms"))
    viol = {k_: ({v_} if k_ == "E1" else {x_of(v_)}) for k_, v_ in (t56.get("violators") or {}).items()}
    av = lambda t_, k_: ((t_.get("associated_stages") or {}).get(k_) or {}).get("duration_ns")
    if T56REC and LEVEL_D >= 3:
        tk, rg_ = collections.defaultdict(list), {}
        for o in recs_d:
            if o.get("event") == "pd_transfer_summary" and o.get("tp_rank", 0) == 0 and o.get("request_id"):
                if o.get("scope") == "task":
                    tk[o["request_id"]].append(o)
                elif o.get("scope") == "request_generation":
                    rg_[o["request_id"]] = o
        bad_f3, bad_ineq, n16, done1, mr_req = [], collections.defaultdict(set), 0, 0, 0
        for rid, g in rg_.items():
            ts_ = tk.get(rid) or []
            n16 += len(ts_) == 16
            for key_, f_ in (("queue_wait_ns", "task_max_queue_wait_ns"), ("meta_fetch", "task_max_meta_fetch_ns"), ("prepare", "task_max_prepare_ns")):
                want_ = max(((t_.get(key_) if key_ == "queue_wait_ns" else av(t_, key_)) or 0) for t_ in ts_) if ts_ else None
                if g.get(f_) != want_:
                    bad_f3.append("%s %s %s / %s" % (x_of(rid), f_, g.get(f_), want_))
            both = [av(t_, "meta_fetch") + av(t_, "metadata_request") for t_ in ts_ if av(t_, "metadata_request") is not None]
            if both and max(both) > (g.get("task_max_meta_fetch_ns") or 0):
                mr_req += 1           # with metadata_request added the maximum would be larger: it was not added
            dn = [t_ for t_ in ts_ if t_.get("request_done_ns") is not None]
            done1 += len(dn) == (0 if g.get("kv_load_envelope_ns") is None else 1) and all("request_done_ns" in t_ for t_ in ts_)
            env_ = g.get("kv_load_envelope_ns")
            if env_ is None:
                continue
            if env_ < g["transfer_envelope_ns"]:
                bad_ineq[1].add(x_of(rid))
            if any(env_ < (t_.get("queue_wait_ns") or 0) + (av(t_, "meta_fetch") or 0) + (av(t_, "prepare") or 0)
                   + (((t_.get("stages") or {}).get("wait_for_transfer") or {}).get("duration_ns") or 0) for t_ in ts_):
                bad_ineq[2].add(x_of(rid))
            if env_ > max(t_["end_ns"] for t_ in ts_) - min(t_["queued_ns"] for t_ in ts_):
                bad_ineq[3].add(x_of(rid))
        r1 = sum(1 for o in recs_d if o.get("event") == "pd_transfer_summary" and o.get("scope") == "request_generation" and o.get("tp_rank") == 1)
        say(bool(rg_) and n16 == len(rg_) and not bad_f3 and done1 == len(rg_),
            "合成记录：从 P 读 KV 的 %d 个请求各有 16 条 rank 0 的读取任务（%d 个）；request_generation 的 3 个最大值等于 16 条任务里的最大值%s；"
            "request_done_ns 每条任务都有这个键，只在标记完成的那一条上有值，没有包络的请求一条也没有（%d 个请求对）"
            % (len(rg_), n16, "；" + "；".join(bad_f3[:4]) if bad_f3 else "", done1))
        say(mr_req >= 1, "合成记录：取对端信息的最大值没有把 metadata_request 再加一遍（%d 个请求的任务带 metadata_request，加上它最大值会更大）" % mr_req)
        bad_all = set().union(*bad_ineq.values()) if bad_ineq else set()
        say(bad_all <= viol.get("E3", set()), "合成记录：10_ 第 3 节的三条不等式，不满足的只有造来不满足 E3 的请求：%s"
            % ({k_: sorted(v_) for k_, v_ in sorted(bad_ineq.items())} or "全部满足"))
        say(None, "rank 1 的 request_generation %d 条，写在单独的 Worker 进程 %s 的行上（TP > 1 的形状），值是 rank 0 的 10 倍；"
            "单请求 JSON 和按 rank 0 算的相同，说明过滤器丢掉了它们" % (r1, t56.get("rank1_proc")))
    if T56REC or T56_PROFILE:
        # the first-batch check (32_ section three): 1a span, 1b the first step, 2 the envelope, 3 等 KV; on the records,
        # and on the per-request JSON where the dashboard has the values (1a reads the step's span, which it has not)
        wk, spans, first_d, core_d, api_d = collections.defaultdict(dict), {}, {}, {}, {}
        rg0 = {o["request_id"]: o for o in recs_d if o.get("event") == "pd_transfer_summary" and o.get("scope") == "request_generation"
               and o.get("tp_rank", 0) == 0 and o.get("request_id")}
        for o in recs_d:
            if o.get("event") == "worker_summary" and o.get("tp_rank", 0) == 0 and o.get("execution_id"):
                for s_ in ("prepare_inputs", "forward", "lm_head", "sampling", "bookkeeping", "draft"):
                    v_ = ((o.get("stages") or {}).get(s_) or {}).get("device_duration_ns")
                    if isinstance(v_, int):
                        wk[o["execution_id"]][s_] = v_
                if isinstance(o.get("step_device_span_ns"), int):
                    spans[o["execution_id"]] = o["step_device_span_ns"]
            if o.get("event") == "core_summary" and o.get("scope") == "execution":
                for r_ in o.get("request_ids") or []:
                    first_d.setdefault(r_, o.get("execution_id"))
            if o.get("event") == "core_summary" and o.get("scope") == "request":
                if o["request_id"] not in core_d or o["timestamp_ns"] > core_d[o["request_id"]]["timestamp_ns"]:
                    core_d[o["request_id"]] = o
            if o.get("event") == "api_summary" and o.get("scope") == "request":
                api_d.setdefault(o["request_id"], o)
        e1 = {exe for exe, sp in spans.items() if sum(wk[exe].values()) > sp}
        e3r, e4r, e5r = set(), set(), set()
        for rid, c_ in core_d.items():
            kv_ = (c_.get("stages") or {}).get("kv_ready_wait")
            g = rg0.get(rid) or {}
            if g.get("kv_load_envelope_ns") is not None and \
                    g["kv_load_envelope_ns"] < max([g.get("transfer_envelope_ns") or 0] + [g.get(f_) or 0 for f_ in T56_RG[:3]]):
                e3r.add(x_of(rid))
            qt_ = ((api_d.get(rid) or {}).get("official") or {}).get("queued_time")
            if kv_ and g.get("kv_load_envelope_ns") is not None and isinstance(qt_, (int, float)) and \
                    qt_ * 1e9 < c_["stages"]["queue"]["initial_wait_ns"] + g["kv_load_envelope_ns"] + kv_["duration_ns"]:
                e4r.add(x_of(rid))
        for rid, a_ in api_d.items():
            pt_ = (a_.get("official") or {}).get("prefill_time")
            exe = first_d.get(rid)
            if isinstance(pt_, (int, float)) and exe and wk.get(exe) and sum(wk[exe].values()) > pt_ * 1e9:
                e5r.add(x_of(rid))
        dd = {X: docs[X] for (s_, X) in expect if s_ == "d" and X in docs}
        num = lambda X, k_: get(dd[X], "d", k_) if isinstance(get(dd[X], "d", k_), (int, float)) else None
        tol = 0.002       # ms: the JSON rounds to 0.001 ms
        in_total = [k_ for k_ in ("d.prepare_inputs", "d.forward", "d.lm_head", "d.sampling", "d.draft") if k_ in PROFILE_KEYS]
        env_key = "d.start_load_kv" if ENV_PARENT else "d.recv_envelope"
        e3d, e4d, e5d = set(), set(), set()
        for X in dd if T56_PROFILE else ():
            env_ = num(X, env_key) if (not ENV_PARENT or (rgx_x.get(X) or {}).get("kv_load_envelope_ns") is not None) else None
            wft_, rq_ = num(X, "d.start_load_kv.wait_for_transfer"), num(X, "d.start_load_kv.receive_queue")
            if env_ is not None and any(v_ is not None and env_ < v_ - tol for v_ in (wft_, rq_)):
                e3d.add(X)
            q_, kv_, vq_ = num(X, "d.schedule.queue_time"), num(X, "d.kv_ready_wait"), num(X, "d.kv_wait_vllm")
            if None not in (q_, kv_, vq_, env_) and vq_ < q_ + env_ + kv_ - tol:
                e4d.add(X)
            fs_ = [num(X, k_) for k_ in in_total]
            fv_ = num(X, "d.first_step_vllm")
            if fv_ is not None and any(v_ is not None for v_ in fs_) and sum(v_ or 0 for v_ in fs_) > fv_ + tol:
                e5d.add(X)
        board_ = T56_PROFILE
        for k_, what, on_rec, on_board in [c_ for c_ in (
                ("E1", "核对 1a：6 段设备时长之和（含记账）≤ step_device_span_ns（只核记录：看板没有这个字段）", e1, None),
                ("E5", "核对 1b：第一步的 L1 之和 ≤ 第一步（vLLM 口径）（记录：6 段设备时长；看板：%s）" % "、".join(k_[2:] for k_ in in_total),
                 e5r, e5d if board_ else None),
                ("E3", "核对 2：墙钟包络 ≥ wait_for_transfer，也 ≥ 3 个最大值（看板：%s ≥ wait_for_transfer、接收排队；准备读表是两个最大值之和，不比）"
                 % ("start_load_kv" if ENV_PARENT else "接收线程（墙钟）"), e3r, e3d if board_ else None),
                ("E4", "核对 3：等 KV（vLLM 口径）≥ 排队时间 + 墙钟包络 + 等调度（三项都有值的请求）", e4r, e4d if board_ else None))
                if LEVEL_D >= {"E1": 3, "E5": 3, "E3": 2}.get(c_[0], 1)]:     # T58: the fields each check reads exist from that level
            want_ = viol.get(k_, set())
            ok_ = on_rec == want_ and (on_board is None or on_board == want_)
            say(ok_, "%s：记录上不满足的 %s%s；造来不满足的 %s" % (
                what, "、".join(sorted(x_[-12:] for x_ in on_rec)) or "无",
                "，看板上 %s" % ("、".join(sorted(on_board)) or "无") if on_board is not None else "", "、".join(sorted(x_[-12:] for x_ in want_)) or "无"))
    if T56_PROFILE:
        # T55: 「有值 / 请求」 = the requests with a value / the requests the row could have, both distribution tables
        os.environ["SEGMENTS_FILE"] = os.path.join(PACK, "segments.json")
        spec_g = importlib.util.spec_from_file_location("generate_t56", os.path.join(PACK, "scripts", "generate.py"))
        gen = importlib.util.module_from_spec(spec_g)
        spec_g.loader.exec_module(gen)
        tree_ = gen.Tree()
        for role_, s_ in (("prefill", "p"), ("decode", "d")):
            colkey = {}
            for ph, grp in ((("proxy", "p"), ("ttft", "p"), ("background", "b")) if s_ == "p" else (("proxy", "d"), ("ttft", "d"), ("tpot", "r"))):
                for top in tree_.sides[role_][ph]:
                    for n_ in top.walk():
                        colkey[n_.column] = grp + "." + ".".join(n_.path)
            out_ = run_q(gen.dist_sql(tree_, role_))     # one row per row of rows_of, in its order (ORDER BY idx)
            # the table's requests: the window's (sampled) requests this side finished, as dist_parts takes them
            tot_ = "p_total_ms" if role_ == "prefill" else "d_total_ms"
            t55_pool = {r_["request_id"] for r_ in run_q("WITH %s SELECT request_id FROM s WHERE ifNull(%s, 0) > 0" % (gen.s_window_cte(tree_), tot_))}
            out_of = sorted(X for (s2, X) in expect if s2 == s_ and X not in t55_pool)
            say(None, "T55：%s 侧分布表的请求 %d 个；有 %s 侧值、不在表里的：%s" % (s_.upper(), len(t55_pool), s_.upper(), "、".join(out_of) or "无"))
            rows_ = gen.rows_of(tree_, role_)
            pool_all = [X for (s2, X) in expect if s2 == s_ and X in t55_pool]
            bad, n_rows, ex_ = [], 0, []
            if len(out_) != len(rows_):
                bad.append("分布表 %d 行，段定义 %d 行" % (len(out_), len(rows_)))

            def has_(X, key_):    # a value of its own, or (a parent) of a segment below it
                e_ = expect[(s_, X)]
                return e_.get(key_) is not None or any(v_ is not None for k2, v_ in e_.items() if k2.startswith(key_ + "."))
            for r_, got_r in zip(rows_, out_):
                lab = gen.dist_label(r_)
                if got_r.get("阶段") != lab:
                    bad.append("第 %d 行是 %s，应为 %s" % (n_rows + 1, got_r.get("阶段"), lab))
                if not r_.columns:
                    continue      # the side's total
                key_ = colkey.get(r_.columns[0])
                pool = [X for X in pool_all if not r_.rest or (expect[(s_, X)].get("steps") or 0) > 0]
                k_ = sum(1 for X in pool if has_(X, key_))
                want_ = "%d / %d" % (k_, len(pool)) if pool else None
                side_lv = LEVEL_P if s_ == "p" else LEVEL_D
                if r_.lvl > side_lv and pool:
                    want_ = "未采（粒度 %d 级）" % side_lv      # T58: every request of the run is at this level
                n_rows += 1
                got = {lab: got_r}
                g_ = got_r.get(gen.HAS_VALUE)
                if g_ != want_:
                    bad.append("%s：%s，应为 %s" % (lab, g_, want_))
                elif k_ < len(pool) and len(ex_) < 4:
                    ex_.append("%s %s" % (lab, g_))
                # rows without a share: the comparison rows
                sh_ = (got.get(lab) or {}).get("均值占比")
                if (not r_.share and r_.source != "summary" and sh_ is not None) or (r_.share and k_ and sh_ is None and r_.source == "fold"):
                    bad.append("%s 的均值占比 %s（这一行%s占比）" % (lab, sh_, "不该有" if not r_.share else "该有"))
            say(not bad, "T55：%s 侧分布表「有值 / 请求」%d 行和按日志算的相同，没有占比的行（对照行）均值占比为空%s；例如：%s"
                % (s_.upper(), n_rows, "；" + "；".join(bad[:6]) if bad else "", "；".join(ex_)))
        # T58: the per-request JSON carries each side's level; the 层级 variable cuts the distribution tables' rows
        # a request with no record on a side (sent to D directly) has 0 there
        want_dl = lambda d_: {"p": LEVEL_P if (d_.get("route") or {}).get("p") else 0, "d": LEVEL_D if (d_.get("route") or {}).get("d") else 0}
        dl_bad = ["%s %s" % (X[-12:], docs[X].get("diag_level")) for X in sorted({X for _, X in expect if docs.get(X)})
                  if docs[X].get("diag_level") != want_dl(docs[X])]
        n_nop = sum(1 for X in {X for _, X in expect if docs.get(X)} if not (docs[X].get("route") or {}).get("p"))
        say(not dl_bad, "T58 单请求 JSON 的 diag_level：有 P 侧的请求是 p %d，D 侧 d %d；没有 P 侧的 %d 个请求 p 是 0%s" % (
            LEVEL_P, LEVEL_D, n_nop, "；不对的：" + "、".join(dl_bad[:4]) if dl_bad else ""))
        for lv_ in ("1", "2"):
            for role_ in ("prefill", "decode"):
                q_ = gen.dist_sql(tree_, role_)
                for kk, v in sub_t.items():
                    if kk != "${level}":
                        q_ = q_.replace(kk, v)
                got_l = [r_["阶段"] for r_ in rows(q_.replace("${level}", lv_).replace("${order_by}", "d_e2e_ms"))]
                want_l = [gen.dist_label(r_) for r_ in gen.rows_of(tree_, role_) if r_.depth <= int(lv_)]
                say(got_l == want_l, "T58 层级 %s：%s 侧分布表 %d 行，正是深度 ≤ %s 的那几行（应为 %d 行）" % (
                    lv_, "P" if role_ == "prefill" else "D", len(got_l), lv_, len(want_l)))
        det = run_q(gen.detail_d(tree_))
        no_share = [r_ for r_ in gen.rows_of(tree_, "decode") if not r_.share and r_.columns]
        labs = {r_.label for r_ in no_share}
        bad = ["%s 占D合计 %s" % (r_["阶段"], r_["占D合计"]) for r_ in det if r_["阶段"] in labs and r_["占D合计"] is not None]
        say(not bad and len(det) > 0,
            "单请求拆解（%s）：%d 行没有占比（%s），占D合计都为空%s"
            % (x_of(rid_cold_d)[-12:], len(labs), "、".join(sorted(l_.strip("└├│  ") for l_ in labs)), "；" + "；".join(bad[:4]) if bad else ""))
    if any(k_ in WORKER_ACC for k_ in HIDDEN):
        # the five rules no segment reads, as the first-batch check reads them: acc__<id>__ns, __steps, __first_ns
        cols_ = ", ".join("max(acc__{0}__ns) AS {0}_ns, max(acc__{0}__steps) AS {0}_steps, max(acc__{0}__first_ns) AS {0}_first".format(k_)
                          for k_ in HIDDEN)
        got_h = {r_["request_id"]: r_ for r_ in rows("SELECT request_id, %s FROM vllm_pd.phase_line_cur WHERE role = 'decode' AND part != '' "
                                                     "GROUP BY request_id" % cols_)}
        bad, n_h, modes = [], 0, collections.Counter()
        for X, hx in sorted(hidden_exp.items()):
            for k_, (ns_, st_, f_) in hx.items():
                g_ = got_h.get(X) or {}
                n_h += 1
                got3 = tuple(None if g_.get(k_ + s2) is None else int(g_[k_ + s2]) for s2 in ("_ns", "_steps", "_first"))
                if got3 != (ns_, st_, f_):
                    bad.append("%s %s：库里 %s，应为 %s" % (X[-12:], k_, got3, (ns_, st_, f_)))
                if k_.startswith("graph_") and f_:
                    modes[k_] += 1
        say(not bad and n_h > 0, "不上看板的 5 条规则（step_device_span、graph_full/piecewise/none、bookkeeping）：%d 个请求 × 规则的总和、步数、第一步和按记录算的相同；"
            "第一步的图模式：%s%s" % (len(hidden_exp), dict(modes), "；" + "；".join(bad[:5]) if bad else ""))
        def shown(o):     # what a panel shows (titles, column names, overrides), without its SQL
            if isinstance(o, dict):
                return {k_: shown(v_) for k_, v_ in o.items() if k_ not in ("rawSql", "targets")}
            return [shown(v_) for v_ in o] if isinstance(o, list) else o
        board_txt = json.dumps(shown(json.load(open(os.path.join(PACK, "generated", "vllm-pd-request.json")))), ensure_ascii=False)
        det_txt = json.dumps(run_q(gen.detail_d(tree_)) + run_q(gen.dist_sql(tree_, "decode")), ensure_ascii=False) if T56_PROFILE else ""
        cov_txt = "\n".join(cov_out)
        leak = [k_ for k_ in HIDDEN if k_ in board_txt or k_ in cov_txt or k_ in det_txt]
        # 32_ 14:5x: the step of kv_ready_wait by its id, from what ClickHouse has: the passed-through core_summary[request]
        # (diag_event.raw) against the EngineCore's partial sums (phase_line first_exec), D only
        kvx = {r_["x"]: r_["sx"] for r_ in rows("SELECT x, any(JSONExtractString(raw, 'stages', 'kv_ready_wait', 'ranges', 1, 'scheduled_execution_id')) AS sx "
                                                "FROM vllm_pd.diag_event WHERE event = 'core_summary[request]' AND side = 'decode' "
                                                "AND JSONHas(raw, 'stages', 'kv_ready_wait') GROUP BY x")}
        fex = {r_["request_id"]: r_["fe"] for r_ in rows("SELECT request_id, argMinIf(first_exec, first_mono_ns, first_exec != '') AS fe "
                                                         "FROM vllm_pd.phase_line_cur WHERE role = 'decode' AND proc = 'engine' AND part != '' "
                                                         "GROUP BY request_id")}
        want_kv = sorted(x_of(r_) for r_ in (t56.get("kv_ready_wait") or {}))
        bad_id = ["%s：%s / %s" % (X[-12:], sx_[-10:], (fex.get(X) or "")[-10:]) for X, sx_ in sorted(kvx.items()) if sx_ != fex.get(X)]
        say(sorted(kvx) == want_kv and not bad_id, "等调度那一步按标识核（32_ 14:5x）：diag_event 里带 kv_ready_wait 的 D 请求 %d 个（应为 %d 个），"
            "ranges[0].scheduled_execution_id 都等于 EngineCore 部分和的 first_exec%s" % (len(kvx), len(want_kv), "；不等的：" + "；".join(bad_id[:4]) if bad_id else ""))
        if T56REC:
            # the first-step analysis query (tests/t56/first_step_analysis.sql, read only, for the manual): one request at a
            # time, it must tell the two causes apart: the direct request (input preparation 40 ms on its first step) and
            # the cold request (its first step without a graph); every number against the records
            sql_fs = open(os.path.join(HERE, "first_step_analysis.sql")).read().replace("<D 实例>", "D0").replace("<分钟>", "100000")
            lv_all = rows(sql_fs)
            say(len(lv_all) == 2 and all(r_.get("D 侧粒度") == str(LEVEL_D) for r_ in lv_all) and int(lv_all[0]["可比请求数"]) > 0,
                "T58 第一步开销分析：两行的「D 侧粒度」是 %s（应为 %d），第一步可比请求 %s 个（低于 3 级时按 5 段算，应大于 0）" % (
                    [r_.get("D 侧粒度") for r_ in lv_all], LEVEL_D, lv_all[0]["可比请求数"] if lv_all else None))
            fsq = lambda X: rows(sql_fs.replace("/*请求*/", "AND request_id = '%s'" % X)) if X else rows(sql_fs)
            all_ = fsq(None)
            xd, xc = x_of(rid_dir), x_of(rid_cold_d)
            rd, rc = fsq(xd), fsq(xc)
            pi = "prepare_inputs"
            first_exe = lambda X: next((first_d.get(r_) for r_ in first_d if x_of(r_) == X and first_d.get(r_)), None)
            ed, ec = first_exe(xd), first_exe(xc)
            n_rest_c = sum(1 for o in recs_d if o.get("event") == "worker_summary" and o.get("phase") == "execute" and o.get("tp_rank", 0) == 0
                           and rid_cold_d in (o.get("request_ids") or [])) - 1
            gap_c = (spans[ec] - sum(wk[ec].values())) / 1e6 if ec in spans and len(wk.get(ec, {})) == 6 else None
            ok_d = (len(rd) == 2 and rd[0]["步"] == "第一步" and abs((rd[0][pi] or 0) - wk.get(ed, {}).get("prepare_inputs", 0) / 1e6) < 0.002
                    and (rd[0][pi] or 0) >= 30 and (rd[1][pi] or 99) < 5 and int(rd[0]["NONE 步数"]) == 0)
            ok_c = (len(rc) == 2 and int(rc[0]["NONE 步数"]) == 1 and int(rc[0]["FULL 步数"]) == 0 and int(rc[0]["PIECEWISE 步数"]) == 0
                    and int(rc[1]["FULL 步数"]) == n_rest_c and int(rc[1]["NONE 步数"]) == 0 and (rc[0][pi] or 0) < 5
                    and (gap_c is None or abs((rc[0]["空隙（step_device_span − 6 段之和）"] or 0) - gap_c) < 0.002))
            say((ok_d and ok_c or LEVEL_D < 3) and len(all_) == 2,
                "第一步开销分析查询：只看直连请求，第一步输入准备 %s ms（记录 %.3f ms），第二步起每步 %s ms，第一步不是 NONE，分得出「是输入准备」；"
                "只看冷请求，第一步 NONE %s 步、FULL %s 步，第二步起 FULL %s 步（记录 %d 步），第一步空隙 %s ms（记录 %s），分得出「退回不走图」；"
                "整个实例 %d 行：%s" % (
                    rd[0][pi] if rd else None, wk.get(ed, {}).get("prepare_inputs", 0) / 1e6, rd[1][pi] if len(rd) > 1 else None,
                    rc[0]["NONE 步数"] if rc else None, rc[0]["FULL 步数"] if rc else None, rc[1]["FULL 步数"] if len(rc) > 1 else None,
                    n_rest_c, rc[0]["空隙（step_device_span − 6 段之和）"] if rc else None, "%.3f" % gap_c if gap_c is not None else "无",
                    len(all_), json.dumps(all_, ensure_ascii=False)[:600]))
        say(not leak, "这 5 条规则不在看板（panel 的标题、列名，单请求拆解和分布表的行）和 coverage 的输出里%s" % ("：" + "、".join(leak) if leak else ""))
    if REF_DOCS:
        ref = json.load(open(REF_DOCS))

        def keys_of(doc_):
            out_ = []

            def walk_(node_, grp_, pre_):
                for id_, v_ in (node_ or {}).items():
                    if isinstance(v_, dict) and "ms" in v_:
                        out_.append(grp_ + "." + pre_ + id_)
                        walk_(v_.get("sub"), grp_, pre_ + id_ + ".")
            walk_(doc_["ttft"]["p"], "p", "")
            walk_(doc_["ttft"]["d"], "d", "")
            walk_(doc_["tpot"]["d"], "r", "")
            walk_(doc_["background"]["p"], "b", "")
            return out_
        skip = {"d.start_load_kv"} if T56_PROFILE and T56REC else set()
        skip |= LV_SKIP       # T58: the segments above the run's levels are not recorded
        diffs, n_cmp, n_keys = [], 0, set()
        for X, rd_ in sorted(ref.items()):
            if X not in docs:
                diffs.append("%s：这次没有这个请求" % X[-12:])
                continue
            for kk in keys_of(rd_):
                n_keys.add(kk)
                if kk in skip:
                    continue
                side_ = "p" if kk[0] in "pb" else "d"
                a_, b_ = get(rd_, side_, kk), get(docs[X], side_, kk)
                n_cmp += 1
                if not ((a_ is None and b_ is None) or (isinstance(a_, (int, float)) and isinstance(b_, (int, float)) and abs(a_ - b_) <= 1e-9 + 1e-9 * abs(a_))):
                    diffs.append("%s %s：19c %s，这次 %s" % (X[-12:], kk, a_, b_))
        say(not diffs and len(n_keys) == 47, "和 19c 面板逐个请求比（e2e_v31 glm53 的 docs.json，%d 个请求、%d 段）：%d 个值%s，不同的 %d 个%s" % (
            len(ref), len(n_keys), n_cmp, "（不比 D 的 start_load_kv：T56 的段定义改了它的值，甲取墙钟包络，乙多加两个 L2）" if skip else "", len(diffs),
            "；" + "；".join(diffs[:6]) if diffs else ""))

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
    for order, lv_q in [(o_, l_) for l_ in sorted({BOARD_LEVEL, "1"}) for o_ in ("d_e2e_ms", "p_ttft_ms", "p_total_ms", "d_total_ms", "tpot_ms")]:
        for p_ in flat(board["panels"]):
            for t in p_.get("targets") or []:
                q = t.get("rawSql")
                if not q:
                    continue
                total_q += 1
                for kk, v in sub_.items():
                    q = q.replace(kk, v)
                q = q.replace("${order_by}", order).replace("${level}", lv_q)     # T58: the 层级 variable
                r = subprocess.run(["docker", "exec", "-i", CH, "clickhouse-client", "--user", "vllm_writer", "--password", "x", "-n"],
                                   input=q + " FORMAT JSONEachRow;\n", stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                if r.returncode:
                    failed.append("%s [%s]: %s" % (p_.get("title"), order, r.stderr.strip().splitlines()[0][:200] if r.stderr.strip() else "?"))
                elif not r.stdout.strip():
                    empty_q.append("%s [%s]" % (p_.get("title"), order))
    say(not failed, "看板查询：%d 次（5 种排序依据 × 层级 %s），出错 %d" % (total_q, "、".join(sorted({BOARD_LEVEL, "1"})), len(failed)))
    for f_ in failed[:6]:
        say(None, "  " + f_)
    say(None, "没有数据的查询：%d（%s）" % (len(empty_q), "；".join(sorted(set(e.split(" [")[0] for e in empty_q)))[:300]))
finally:
    if os.environ.get("E2E_KEEP") != "1":
        cleanup()
    with open(os.path.join(WORK, "e2e-%s.txt" % RUN), "w") as f:
        f.write("\n".join(out_lines) + "\n")
fails = sum(1 for x in out_lines if x.startswith("FAIL"))
print("%d FAIL" % fails)
sys.exit(1 if fails else 0)
