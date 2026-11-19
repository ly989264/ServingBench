#!/usr/bin/env python3
"""T51 glm5.3: the share of empty steps of guide A 7.3 (two runs of its query) on synthetic cases, end to end on this
machine in throw-away containers (ClickHouse 26.8.36.3, Vector 0.61.0, --rm): synthetic vLLM logs -> the pack's own
log filter (tests/t41/tools/check_pd_diag.py) -> node files -> Vector -> ClickHouse -> guide A's two queries, as written.

  python3 -I tests/t51/e2e_ratio73.py <pack> <work> <D.log> <guide A>

<D.log> is the rc1 D log (real hardware, TP1: an API server and an EngineCore, 3 requests, 144 steps). Each case is one
D instance on the same machine, with its own two processes; every forward and sampling record gets a device time equal to
its host time, except where the case says. Times are relative to N (about now): phase A is a copy of the log at N-200 s;
the first query runs at about N+130 s, after only the node-file lines up to N+120 s have been handed to the collector;
then the rest is handed over and the second query runs at N+930 s, with the first query's t10_utc.

  R1  phase B: 3 copies at N+400..N+420; 2 of every 5 forward and sampling records without a device time   -> row 1
  R2  phase B: 3 copies; every 10th forward record without stages.forward, every 10th sampling record
      without stages.sampling (no stage, so not counted as left empty)                                         -> row 2
  R3  phase B: 3 copies; 2 forward and 2 sampling records without a device time (about 0.5 %)                 -> row 3
  R4  phase B: 3 copies, every device time there                                                              -> row 4
  W   requests ended before t10 (phase A); phase B (1 copy) ends at N+921, within the minute before the
      second query: no request has ended in the window                                                    -> ratio empty, ended_win 0
  P1  restart inside the window: the old processes log one more copy at N+300, new processes (new pids) log
      phase B at N+400..N+420; both are in the second query's list                                           -> pid_list
  P10 restart more than 10 minutes before the second query: the old processes log one more copy at N+180
      (their self-check lines end 130 s later), new processes log phase B; the old pids are out of the
      second query's 10-minute list, so the number of pids is the same and only the list differs             -> pid_list
  A0D phase B: 3 copies, the EngineCore's step records without stages.alloc: requests end in the window, the
      scheduler side records no step                                                          -> ratio empty, ended_win > 0
  A0P the same on an instance named P9 (the rule is the same for P)                           -> ratio empty, ended_win > 0
  A1  phase B: the API server's records only (the EngineCore's filter stopped): its pid leaves the list -> pid_list
  S0  a copy at N-100 whose API server records come 506 s later: its 3 requests end in the window, every step
      before t10; the EngineCore stays up with steps that name no request        -> ratio empty, ended_win > 0, *_win 0
<这一批的实例> is every case's instance but R4's (D4): this_batch is 1 everywhere else, 0 on D4.

For each case: both outputs, the row of guide A's table it falls in (pid_list first, then the empty ratio, then the
table), the expected row, and the ratio, alloc_total and empty counts against values computed here from the case's log.
Writes <work>/e2e-ratio73.txt. Removes its containers and network at the end.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

SRC_PACK, WORK, LOG_D, GUIDE = sys.argv[1:5]
NET, CH, VEC = "t51r-net", "t51r-ch", "t51r-vec"
CH_IMG, VEC_IMG = "clickhouse/clickhouse-server:26.8.36.3", "timberio/vector:0.61.0-debian"
HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "..", "t41", "tools", "check_pd_diag.py")
CUT_S, Q2_S = 120, 930
DRY = os.environ.get("R73_DRY") == "1"      # only the cases and their node files, no containers
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


def cleanup():
    for c in (VEC, CH):
        subprocess.run(["docker", "rm", "-fv", c], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["docker", "network", "rm", NET], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---- guide A's two queries, as written --------------------------------------------------------------------------------
g = open(GUIDE, encoding="utf-8").read()
sec = g[g.index("### 7.3"):g.index("### 7.4")]
qs = re.findall(r"<<'SQL'\n(.*?)\nSQL\n", sec, re.S)
Q_FIRST, Q_SECOND = qs[1], qs[2]
assert Q_FIRST.startswith("WITH now64(3, 'UTC') AS t10") and "<第一次的 t10_utc>" in Q_SECOND, "guide A 7.3 changed"
assert "pid_list" in Q_FIRST and "pid_list" in Q_SECOND
FMT = " FORMAT PrettyCompactMonoBlock"
assert Q_FIRST.endswith(FMT) and Q_SECOND.endswith(FMT)

# ---- the pack ----------------------------------------------------------------------------------------------------------
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(WORK)
PACK = os.path.join(WORK, "pack")
shutil.copytree(SRC_PACK, PACK, ignore=shutil.ignore_patterns("generated", "__pycache__", "offline"))
shutil.copy(os.path.join(PACK, "profiles", "glm53", "segments.json"), os.path.join(PACK, "segments.json"))
say(None, "段定义：profiles/glm53/segments.json（config_id %s）；查询：%s 第 7.3 节的两条，原文" % (
    json.load(open(os.path.join(PACK, "segments.json")))["config_id"], os.path.basename(GUIDE)))

# ---- the base log --------------------------------------------------------------------------------------------------
base = []
for line in open(LOG_D, errors="replace"):
    i = line.find("[PD_DIAG] ")
    if i >= 0:
        base.append((line[:i], json.loads(line[i + 10:])))
PIDS = sorted({o["pid"] for _, o in base})          # the API server and the EngineCore


def seg_of(o):
    if o.get("event") == "worker_summary" and o.get("tp_rank", 0) == 0 and o.get("request_ids"):
        return {"execute": "forward", "sample": "sampling"}.get(o.get("phase"))
    return None


def shift_ns(o, d):
    if isinstance(o, dict):
        for k, v in o.items():
            if k.endswith("_ns") and isinstance(v, int) and not isinstance(v, bool) and v > 10 ** 13:
                o[k] = v + d
            else:
                shift_ns(v, d)
    elif isinstance(o, list):
        for v in o:
            shift_ns(v, d)


def shift_prefix(pfx, sec):
    m = re.search(r"(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)", pfx)
    t = datetime.datetime(2026, int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)))
    t += datetime.timedelta(seconds=sec)
    return pfx[:m.start()] + t.strftime("%m-%d %H:%M:%S") + pfx[m.end():]


ID = re.compile(r"\b(execution|engine|record|api|output-batch|api_submission|pd_request|pd_connector|tokenizer_end|pd_transfer|pd_read)-([0-9a-f]{32})")


def copy(tag, at_s, pidmap, new_run):
    """One copy of the base log at N+at_s, request and record ids made unique by tag, pids mapped (a restart: new pids,
    a new run_id), every forward and sampling stage with a device time equal to its host time."""
    out = []
    for k, (pfx, o) in enumerate(base):
        t = json.dumps(o)
        t = re.sub(r"rc1-20261116-(cold|hot|direct)\b", r"rc1-20261116-%s-\1" % tag, t)    # with or without the -<hex8> suffix
        t = ID.sub(lambda m: m.group(1) + "-" + hashlib.md5((tag + m.group(2)).encode()).hexdigest(), t)
        for a, b in pidmap.items():
            t = t.replace("-%d-" % a, "-%d-" % b)
        r = json.loads(t)
        r["pid"] = pidmap.get(o["pid"], o["pid"])
        if new_run and isinstance(r.get("run_id"), str):
            r["run_id"] = hashlib.md5((tag + r["run_id"]).encode()).hexdigest()
        shift_ns(r, (at_s + 200) * 10 ** 9)
        s = seg_of(r)
        if s and isinstance((r.get("stages") or {}).get(s), dict) and isinstance(r["stages"][s].get("duration_ns"), int):
            r["stages"][s]["device_duration_ns"] = r["stages"][s]["duration_ns"]
        p = shift_prefix(pfx, at_s + 200)
        for a, b in pidmap.items():
            p = p.replace("pid=%d)" % a, "pid=%d)" % b)
        out.append(((at_s, k), p, r))
    return out


def no_device(recs, pick):
    """Drop the device time of the forward / sampling records pick(seg, index) chooses; returns how many per segment."""
    n, idx = {"forward": 0, "sampling": 0}, {"forward": 0, "sampling": 0}
    for _, _, r in recs:
        s = seg_of(r)
        if s and isinstance((r.get("stages") or {}).get(s), dict):
            if pick(s, idx[s]):
                r["stages"][s].pop("device_duration_ns", None)
                n[s] += 1
            idx[s] += 1
    return n


def no_stage(recs, every):
    n, idx = {"forward": 0, "sampling": 0}, {"forward": 0, "sampling": 0}
    for _, _, r in recs:
        s = seg_of(r)
        if s and isinstance((r.get("stages") or {}).get(s), dict):
            if idx[s] % every == every - 1:
                del r["stages"][s]
                n[s] += 1
            idx[s] += 1
    return n


def counts(recs):
    """Request-steps the second query counts: alloc of the EngineCore's steps, forward / sampling with a device time;
    and the records the filter leaves empty (the stage there, no device time), per pid."""
    c = {"alloc": 0, "forward": 0, "sampling": 0}
    empty = {}
    for _, _, r in recs:
        if r.get("event") == "core_summary" and r.get("scope") == "execution" and isinstance(((r.get("stages") or {}).get("alloc") or {}).get("duration_ns"), int):
            c["alloc"] += len(r.get("request_ids") or [])
        s = seg_of(r)
        if s and isinstance((r.get("stages") or {}).get(s), dict):
            if isinstance(r["stages"][s].get("device_duration_ns"), int):
                c[s] += len(r["request_ids"])
            else:
                empty.setdefault(r["pid"], {"forward": 0, "sampling": 0})[s] += 1
    return c, empty


API_PID = min(PIDS)       # the API server (no "(EngineCore pid=N)" prefix); the other one is the EngineCore
ENGINE_PID = max(PIDS)


def no_alloc(recs):
    """The EngineCore's step records without stages.alloc: the requests run, the scheduler side records no step."""
    n = 0
    for _, _, r in recs:
        if r.get("event") == "core_summary" and r.get("scope") == "execution" and "alloc" in (r.get("stages") or {}):
            del r["stages"]["alloc"]
            n += 1
    return {"alloc": n}


NAMES_ = ("R1", "R2", "R3", "R4", "W", "P1", "P10", "A0D", "A0P", "A1", "S0")
CASES = {}      # name -> (instance, parts [(recs, phase)], expected row, note, pids at the first query, pids at the second)
for k, name in enumerate(NAMES_):
    inst = ("P%d" if name == "A0P" else "D%d") % (k + 1)
    old = {p: p + 100 * (k + 1) for p in PIDS}
    new = {p: p + 100 * (k + 1) + 50 for p in PIDS}
    parts = [(copy(inst + "a", -200, old, False), "A")]
    live2 = set(old.values())
    note = None
    if name in ("R1", "R2", "R3", "R4", "A0D", "A0P"):
        b = [copy("%sb%d" % (inst, j), 400 + 10 * j, old, False) for j in range(3)]
        flat = [x for y in b for x in y]
        note = {"R1": lambda: no_device(flat, lambda s_, i: i % 5 in (1, 3)), "R2": lambda: no_stage(flat, 10),
                "R3": lambda: no_device(flat, lambda s_, i: i in (50, 200)), "R4": lambda: {"forward": 0, "sampling": 0},
                "A0D": lambda: no_alloc(flat), "A0P": lambda: no_alloc(flat)}[name]()
        parts += [(x, "B") for x in b]
    elif name == "W":
        parts.append((copy(inst + "b0", 915, old, False), "B_late"))
    elif name == "A1":
        # the EngineCore's filter stops after phase A (or its files stop reaching the database): phase B has the API
        # server's records only, the EngineCore's self-check lines end 130 s after its last line
        b = [[x for x in copy("%sb%d" % (inst, j), 400 + 10 * j, old, False) if x[2]["pid"] == old[API_PID]] for j in range(3)]
        parts += [(x, "B") for x in b]
        live2 = {old[API_PID]}
        note = "第二阶段只有 API server 的记录"
    elif name == "S0":
        # requests that end in the window although every step ran before t10: a copy at N-100 whose API server records
        # (and so the requests' end) come 506 s later, at N+406; the EngineCore stays up with steps that name no request
        sc = []
        for (key, pfx, r) in copy(inst + "s", -100, old, False):
            if r["pid"] == old[API_PID]:
                shift_ns(r, 506 * 10 ** 9)
                pfx, key = shift_prefix(pfx, 506), (key[0] + 506, key[1])
            sc.append((key, pfx, r))
        parts.append((sc, "S"))
        for j, at in enumerate(range(400, 900, 60)):
            idle = [x for x in copy("%sk%d" % (inst, j), at, old, False) if x[2]["pid"] == old[ENGINE_PID] and not x[2].get("request_ids")
                    and x[2].get("event") in ("core_summary", "worker_summary")]
            parts.append((idle[:2], "K"))
        note = "第二阶段：只有 EngineCore 不带请求的空步"
    else:
        parts.append((copy(inst + "a2", 300 if name == "P1" else 180, old, False), "A2"))
        parts += [(copy("%sb%d" % (inst, j), 400 + 10 * j, new, True), "B") for j in range(3)]
        live2 = set(old.values()) | set(new.values()) if name == "P1" else set(new.values())
    expect_row = {"R1": "1", "R2": "2", "R3": "3", "R4": "4", "W": "ratio_empty", "P1": "pid_list", "P10": "pid_list",
                  "A0D": "chain_broken", "A0P": "chain_broken", "A1": "pid_list", "S0": "window_no_steps"}[name]
    CASES[name] = (inst, parts, expect_row, note, set(old.values()), live2)

BATCH = [c[0] for n_, c in CASES.items() if n_ != "R4"]
Q_FIRST_B, Q_SECOND_B = (q.replace("<这一批的实例>", "|".join(BATCH)) for q in (Q_FIRST, Q_SECOND))

# ---- containers ----------------------------------------------------------------------------------------------------
conf, data, pull = os.path.join(WORK, "vconf"), os.path.join(WORK, "vdata"), os.path.join(WORK, "pull")


def start_containers():
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
    for d in (conf, data, pull):
        os.makedirs(d)
    with open(os.path.join(conf, "inventory.csv"), "w") as f:
        f.write("addr,name\n")
    venv = dict(os.environ, CLICKHOUSE_ADDR=CH, CLICKHOUSE_CONTAINER=CH, CH_WRITER_PASSWORD="x", MODEL_NAME="glm-5.3")
    subprocess.run([sys.executable, "-I", os.path.join(PACK, "scripts", "render_vector.py"), os.path.join(conf, "vector.yaml"),
                    "/var/lib/vector", "/etc/vector"], env=venv, check=True)
    sh(["docker", "run", "-d", "--rm", "--name", VEC, "--network", NET, "-v", conf + ":/etc/vector", "-v", pull + ":/pull",
        "-v", data + ":/var/lib/vector", VEC_IMG, "--config", "/etc/vector/vector.yaml"])

    time.sleep(10)


cleanup()
pump_stop = threading.Event()
try:
    if not DRY:
        start_containers()
    else:
        os.makedirs(pull)

    # ---- the cases through the filter --------------------------------------------------------------------------------
    probe = os.path.join(WORK, "probe.log")
    with open(probe, "w") as f:
        for pfx, o in base:
            f.write(pfx + "[PD_DIAG] " + json.dumps(o, separators=(",", ":")) + "\n")
    r = subprocess.run([sys.executable, "-I", TOOL, probe, "-o", os.path.join(WORK, "probe-split"), "--pack", PACK, "--instance", "D0"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    shift0 = float(re.search(r"--shift (-?[0-9.]+)", r.stdout).group(1))     # the base log's last line 180 s before now
    files = {}          # (inst, file) -> [(epoch, line)]
    for name, (inst, parts, _, _, _, _) in CASES.items():
        recs = sorted((x for p_, _ in parts for x in p_), key=lambda x: x[0])
        path = os.path.join(WORK, "case-%s.log" % name)
        with open(path, "w") as f:
            for _, pfx, o in recs:
                f.write(pfx + "[PD_DIAG] " + json.dumps(o, separators=(",", ":")) + "\n")
        split = os.path.join(WORK, "split-" + name)
        r = subprocess.run([sys.executable, "-I", TOOL, path, "-o", split, "--pack", PACK, "--instance", inst, "--shift", repr(shift0)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        say(r.returncode in (0, 1, 4), "%s（%s）：check_pd_diag.py 拆成节点文件（退出码 %d）" % (name, inst, r.returncode))
        for fn in sorted(os.listdir(os.path.join(split, inst))):
            for line in open(os.path.join(split, inst, fn)):
                ts = datetime.datetime.fromisoformat(line[:29]).timestamp()
                files.setdefault((inst, fn), []).append((ts, line))
    # a keep-alive file of another instance (Z9, no requests: not in the queries' output): a self-check line every 3 s
    # (the probe's own line as the template), so that Vector always has a next event to hand the cases' last lines over
    tpl = None
    for fn in sorted(os.listdir(os.path.join(WORK, "probe-split", "D0"))):
        for line in open(os.path.join(WORK, "probe-split", "D0", fn)):
            if '"pd_filter_selfcheck"' in line and fn.startswith("api."):
                tpl = json.loads(line[line.index("[PD_DIAG] ") + 10:])
    zdir = os.path.join(pull, "n1", "Z9")
    os.makedirs(zdir)
    zname = "api.%s.99.0000.log" % time.strftime("%Y%m%dT%H%M%S")

    def pump():
        k = 0
        while not pump_stop.is_set():
            ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+00:00"
            o = dict(tpl, instance="Z9", pid=99, files=[zname], seen=k, timestamp_ns=int(time.monotonic() * 1e9))
            with open(os.path.join(zdir, zname), "a") as f:
                f.write("%s (APIServer pid=99) INFO [pd_diag_logfilter.py:0] [PD_DIAG] %s\n" % (ts, json.dumps(o)))
            k += 1
            pump_stop.wait(3)
    threading.Thread(target=pump, daemon=True).start()
    # N from the data: phase A's first line is at N-200 s (the file-open line of its first process)
    a0 = min(ts for (inst, fn), ls in files.items() if inst == "D1" for ts, _ in ls[:1])
    N = a0 + 200
    CUT = N + CUT_S
    say(None, "时刻：N = %s UTC（第一阶段 N-200 s）；先交给采集器 N+%d s 以前的行，第一次查询在那以后、第二阶段以前；第二次查询在 N+%d s"
        % (datetime.datetime.utcfromtimestamp(N).strftime("%H:%M:%S"), CUT_S, Q2_S))
    late = [(k, ts) for k, ls in files.items() for ts, l_ in ls if CUT - 10 < ts <= N + 170 and '"pd_seg_partial"' in l_]
    say(not late, "在切分点前后（N+%d..N+170 s）没有按请求累加的行：%s" % (CUT_S - 10, late[:3] or "无"))

    if DRY:
        for name, (inst, parts, expect_row, note, pids1, live2) in CASES.items():
            c, e = counts([x for p_, _ in parts for x in p_])
            spans = ["%s N%+.0f..N%+.0f" % (ph, min(x[0][0] for x in p_), max(x[0][0] for x in p_) + 6) for p_, ph in parts]
            say(None, "%s %s 期望 %s：全部 %s，留空 %s；%s；改动 %s" % (name, inst, expect_row, c, e, "，".join(spans), note))
        nfile = {}
        for (inst, fn), ls in files.items():
            nfile.setdefault(inst, []).append("%s %d 行 N%+.0f..N%+.0f" % (fn.split(".")[0] + "." + fn.split(".")[2], len(ls), ls[0][0] - N, ls[-1][0] - N))
        for inst in sorted(nfile):
            say(None, "%s 的节点文件：%s" % (inst, "；".join(nfile[inst])))
        raise SystemExit(0)

    def hand_over(cond):
        n = 0
        for (inst, fn), ls in files.items():
            d = os.path.join(pull, "n1", inst)
            os.makedirs(d, exist_ok=True)
            sel = [l_ for ts, l_ in ls if cond(ts)]
            if sel:
                with open(os.path.join(d, fn), "a") as f:
                    f.writelines(sel)
                n += len(sel)
        return n

    def settle(min_wait):
        last, stable = -1, 0
        t_end = time.time() + 300
        time.sleep(min_wait)
        while time.time() < t_end:
            n = int(ch("SELECT count() FROM vllm_pd.phase_line", fmt="TSV").strip() or 0) + int(
                ch("SELECT count() FROM vllm_pd.filter_selfcheck WHERE instance != 'Z9'", fmt="TSV").strip() or 0)
            stable = stable + 1 if n == last else 0
            last = n
            if stable >= 3:
                return n
            time.sleep(3)
        return last

    n1 = hand_over(lambda ts: ts <= CUT)
    got1 = settle(15)
    while time.time() < CUT + 5:
        time.sleep(1)
    say(None, "第一批：%d 行交给采集器，库里 %d 行（phase_line 加自检行）" % (n1, got1))

    def run_query(q, label):
        pretty = ch(q[:-len(FMT)], fmt="PrettyCompactMonoBlock")
        res = {r_["instance"]: r_ for r_ in rows(q[:-len(FMT)])}
        with open(os.path.join(WORK, "%s.txt" % label), "w") as f:
            f.write(pretty)
        return res, pretty

    first, pretty1 = run_query(Q_FIRST_B, "first")
    if not first:
        raise SystemExit("第一次查询没有输出")
    t10 = next(iter(first.values()))["t10_utc"]
    t10_epoch = datetime.datetime.fromisoformat(t10).replace(tzinfo=datetime.timezone.utc).timestamp()
    say(CUT < t10_epoch < N + 170, "第一次查询：t10_utc %s（N+%.0f s）" % (t10, t10_epoch - N))
    n2 = hand_over(lambda ts: ts > CUT)
    say(None, "第二批：%d 行交给采集器；等到 N+%d s" % (n2, Q2_S))
    while time.time() < N + Q2_S:
        time.sleep(5)
    got2 = settle(0)
    second, pretty2 = run_query(Q_SECOND_B.replace("<第一次的 t10_utc>", t10), "second")
    say(None, "第二次查询：N+%.0f s，库里 %d 行" % (time.time() - N, got2))
    out_lines.append("第一次查询的输出：\n" + pretty1.rstrip())
    out_lines.append("第二次查询的输出：\n" + pretty2.rstrip())

    # ---- what each case should give, and the row of guide A's 7.3 it falls in ------------------------------------
    def num(v):
        return None if v is None else int(v)

    def row_of(r1, r2, seg):
        if r1.get("pid_list") != r2.get("pid_list"):
            return "pid_list"
        pct = r2.get("%s_empty_pct" % seg)
        if pct is None:
            if not num(r2.get("ended_win")):
                return "ratio_empty"
            return "chain_broken" if num(r2.get("forward_win")) or num(r2.get("sampling_win")) else "window_no_steps"
        grew = num(r2.get("%s_empty" % seg)) > num(r1.get("%s_empty" % seg))
        return {(True, True): "1", (True, False): "2", (False, True): "3", (False, False): "4"}[(float(pct) > 1, grew)]

    NAMES = {"1": "第 1 行（超过 1%、留空次数增加了）", "2": "第 2 行（超过 1%、留空次数没有增加）", "3": "第 3 行（不超过 1%、增加了）",
             "4": "第 4 行（不超过 1%、没有增加）", "ratio_empty": "比例为空、ended_win 是 0（没判成）",
             "chain_broken": "比例为空、ended_win 大于 0、forward_win 或 sampling_win 大于 0（调度器侧断了）",
             "window_no_steps": "比例为空、ended_win 大于 0、三个 *_win 都是 0", "pid_list": "两次的 pid_list 不同（不可比）"}

    def verdict(row, inst):
        side = inst[0]
        if row in ("pid_list", "1"):
            return "这一批失败" if side == "D" else "带回来"
        return {"2": "不判失败，带回来", "3": "通过，带回来", "4": "通过", "ratio_empty": "没判成，带回来", "chain_broken": "这一批失败（P、D 都算）",
                "window_no_steps": "不直接判失败，带回来"}[row]

    def pick(parts, phases, pids):
        return [x for p_, ph in parts if ph in phases for x in p_ if x[2]["pid"] in pids]

    def ended(parts, phases):
        return len({x[2]["request_id"] for p_, ph in parts if ph in phases for x in p_
                    if x[2].get("event") == "api_summary" and x[2].get("scope") == "request"})

    for name, (inst, parts, expect_row, note, pids1, live2) in CASES.items():
        r1, r2 = first.get(inst) or {}, second.get(inst) or {}
        done = {"A", "A2", "B", "S"}    # requests ended a minute before the second query (not W's late phase)
        c_tot, _ = counts(pick(parts, done, live2))
        c_win, _ = counts(pick(parts, {"A2", "B"}, live2))          # steps after t10
        _, e_all = counts(pick(parts, {"A", "A2", "B", "B_late", "S", "K"}, live2))
        want = {"pid_list1": sorted(pids1), "pid_list2": sorted(live2), "alloc_total1": 144, "ended_win1": 0,
                "alloc_total2": c_tot["alloc"], "alloc_win2": c_win["alloc"], "ended_win2": ended(parts, {"A2", "B", "S"}),
                "forward_win2": c_win["forward"], "this_batch1": int(inst in BATCH), "this_batch2": int(inst in BATCH)}
        got = {"pid_list1": r1.get("pid_list"), "pid_list2": r2.get("pid_list"), "alloc_total1": num(r1.get("alloc_total")),
               "ended_win1": num(r1.get("ended_win")), "alloc_total2": num(r2.get("alloc_total")), "alloc_win2": num(r2.get("alloc_win")),
               "ended_win2": num(r2.get("ended_win")), "forward_win2": num(r2.get("forward_win")), "this_batch1": num(r1.get("this_batch")),
               "this_batch2": num(r2.get("this_batch"))}
        bad = ["%s %s（应 %s）" % (k_, got[k_], v_) for k_, v_ in want.items() if got[k_] != v_]
        res = []
        for seg in ("forward", "sampling"):
            want_pct = None if c_win["alloc"] == 0 else round(100 * (1 - c_win[seg] / c_win["alloc"]), 2)
            got_pct = r2.get("%s_empty_pct" % seg)
            want_e2 = sum(v[seg] for v in e_all.values())
            ok_pct = (got_pct is None and want_pct is None) or (got_pct is not None and want_pct is not None and abs(float(got_pct) - want_pct) <= 0.011)
            if not ok_pct:
                bad.append("%s 比例 %s（应 %s）" % (seg, got_pct, want_pct))
            if num(r2.get("%s_empty" % seg)) != want_e2:
                bad.append("%s 留空次数 %s（应 %d）" % (seg, r2.get("%s_empty" % seg), want_e2))
            res.append((seg, row_of(r1, r2, seg), got_pct, want_pct, r1.get("%s_empty" % seg), r2.get("%s_empty" % seg)))
        ok = not bad and all(g_ == expect_row for _, g_, _, _, _, _ in res)
        say(ok, "%s（%s，this_batch %s/%s）：期望 %s → %s；%s。第一次 pids %s pid_list %s ended_win %s alloc_total %s；第二次 window_min %s pids %s pid_list %s ended_win %s alloc_total %s alloc_win %s forward_win %s%s%s"
            % (name, inst, r1.get("this_batch"), r2.get("this_batch"), NAMES[expect_row], verdict(expect_row, inst), "；".join(
                "%s 落在 %s → %s，比例 %s%%（按日志 %s%%），留空次数 %s→%s" % (seg, NAMES[g_], verdict(g_, inst), p_, w_, e1, e2)
                for seg, g_, p_, w_, e1, e2 in res),
               r1.get("pids"), r1.get("pid_list"), r1.get("ended_win"), r1.get("alloc_total"), r2.get("window_min"), r2.get("pids"), r2.get("pid_list"),
               r2.get("ended_win"), r2.get("alloc_total"), r2.get("alloc_win"), r2.get("forward_win"), "；改动：%s" % note if note else "",
               "；数值不对：" + "，".join(bad) if bad else ""))
finally:
    pump_stop.set()
    if os.environ.get("R73_KEEP") != "1":      # R73_KEEP=1 leaves the containers up, to look into them
        cleanup()
    with open(os.path.join(WORK, "e2e-ratio73.txt"), "w") as f:
        f.write("\n".join(out_lines) + "\n")
    print("%d FAIL" % sum(1 for l_ in out_lines if l_.startswith("FAIL ")))
