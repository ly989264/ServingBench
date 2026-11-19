#!/usr/bin/env python3
"""T41 (pack two v3): end to end on this machine, in throw-away containers (Vector 0.61.0, ClickHouse 26.8.36.3,
--rm): node files -> Vector (the pack's rendered config) -> ClickHouse (the pack's schema.sql and views.sql) -> the
per-request JSON view and every dashboard query. Adapted from the T29 test of the demo repository.

  python3 -I e2e_v3.py <pack> <work> mock                     the mock, P0 and D0, against the mock's truth
  python3 -I e2e_v3.py <pack> <work> replay <P.log> <D.log>   a P log as P0 and its D log as D0 (split by
                                                              tools/check_pd_diag.py, clocks kept aligned), against
                                                              values computed here from the logs (not by the filter)

Besides the segments of v2.1 it checks the eight segments v3 fills (P start_load_kv/submit, P background wait_for_save
and its five children, D start_load_kv/wait_for_transfer, and their parents) and that the segments v3 leaves empty are
empty (proxy, P wait_for_put, P start_load_kv/wait_for_transfer, the three children of put, D start_load_kv/submit).

Values are compared within 0.001 ms + 1e-6 relative (phase_line keeps Float32 milliseconds, the JSON 3 decimals).
Writes <work>/e2e-<mode>.txt. Removes its containers and network at the end."""
import collections
import json
import os
import re
import shutil
import subprocess
import sys
import time

PACK, WORK, MODE = sys.argv[1], sys.argv[2], sys.argv[3]
LOG = sys.argv[4] if len(sys.argv) > 4 else None
LOG_D = sys.argv[5] if len(sys.argv) > 5 else LOG
NET, CH, VEC = "t41-net", "t41-ch", "t41-vec"
CH_IMG, VEC_IMG = "clickhouse/clickhouse-server:26.8.36.3", "timberio/vector:0.61.0-debian"
TOOLS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools")
# The segments v3 leaves empty: every request must have null there.
EMPTY_P = ("p.api_gateway", "p.lb_proxy", "p.wait_for_put", "p.start_load_kv.wait_for_transfer",
           "b.wait_for_save.put.submit", "b.wait_for_save.put.wait_for_transfer", "b.wait_for_save.put.finalize")
EMPTY_D = ("d.lb_proxy", "d.start_load_kv.submit")
PREFIX = re.compile(r"^(?:chatcmpl-|cmpl-|resp[-_])")
out_lines = []


def say(ok, text):
    line = ("OK   " if ok is True else "FAIL " if ok is False else "     ") + text
    out_lines.append(line)
    print(line, flush=True)


def sh(cmd, inp=None, check=True):
    r = subprocess.run(cmd, input=inp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    if check and r.returncode:
        raise SystemExit("命令失败：%s\n%s" % (" ".join(cmd), r.stdout[-2000:]))
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


# ---- node files ------------------------------------------------------------------------------------------------------
shutil.rmtree(WORK, ignore_errors=True)
pull = os.path.join(WORK, "pull")
os.makedirs(pull)
now = time.time()
expect = {}            # (side, X) -> {json path: ms}
expect_exec = {}       # (side, X) -> executions
expect_missing = 0
if MODE == "mock":
    node = os.path.join(WORK, "node")
    os.makedirs(node)
    for f in ("node/pd_diag_logfilter.py", "node/pd_diag_logging.json", "segments.json"):
        shutil.copy(os.path.join(PACK, f), node)
    inv = os.path.join(PACK, "samples", "pd032_sample_inventory.json")
    truth = {}
    for inst in ("P0", "D0"):
        filt = os.path.join(WORK, "f-" + inst)
        sh([sys.executable, "-I", os.path.join(PACK, "mock", "pd032_mock.py"), "sample", "--inventory", inv, "--instances", inst,
            "--seconds", "40", "--rate", "6", "--start-at", str(int(now - 240)), "--node-dir", node, "--filtered-dir", filt,
            "--truth", os.path.join(WORK, inst + ".truth.jsonl")])
        shutil.copytree(os.path.join(filt, inst), os.path.join(pull, "n-" + inst, inst))
        for line in open(os.path.join(WORK, inst + ".truth.jsonl")):
            t = json.loads(line)
            truth[(inst, t["request_id"])] = t
    for (inst, rid), t in truth.items():
        ev, sg = t.get("events") or {}, t["segs"]
        if "api_total" not in ev:
            continue
        ms = lambda v: None if v is None else v / 1e6
        acc = lambda k, part="ns": None if k not in sg else ms(sg[k]["ns"] - sg[k]["first_ns"] if part == "rest" else sg[k][part])
        side = "p" if inst == "P0" else "d"
        e = {"total": ms(ev["api_total"]), side + ".req_resolve": ms(ev["req_resolve"]), side + ".tokenizer": ms(ev["tokenizer"]),
             side + ".add_request": ms(ev.get("add_request")), side + ".schedule.queue_time": ms(ev.get("queue_time"))}
        if side == "p":
            e.update({"p.schedule.hash": ms(ev.get("hash")), "p.schedule.lookup": acc("lookup"), "p.schedule.alloc": acc("alloc"),
                      "p.schedule.other": acc("schedule_other"), "p.forward": acc("forward"), "p.sampling": acc("sampling"),
                      "p.detokenizer": ms(ev.get("detokenizer")),
                      "p.start_load_kv.submit": ms(ev.get("load_submit")), "p.start_load_kv": ms(ev.get("load_submit")),
                      "b.wait_for_save": ms(ev.get("save"))})
            for s_ in ("build_key", "lookup", "prepare", "sync", "put"):
                e["b.wait_for_save." + s_] = ms(ev.get("save_" + s_))
            e.update({k: None for k in EMPTY_P})
        else:
            e.update({"d.schedule.alloc": acc("alloc", "first_ns"), "d.schedule.other": acc("schedule_other", "first_ns"),
                      "d.forward": acc("forward", "first_ns"), "d.sampling": acc("sampling", "first_ns"),
                      "d.detokenizer": ms(ev.get("detokenizer_first")),
                      "r.schedule.alloc": acc("alloc", "rest"), "r.schedule.other": acc("schedule_other", "rest"),
                      "r.forward": acc("forward", "rest"), "r.sampling": acc("sampling", "rest"), "r.detokenizer": ms(ev.get("detokenizer_rest")),
                      "steps": (sg["forward"]["steps"] - 1) if "forward" in sg else None,
                      "d.start_load_kv.wait_for_transfer": ms(ev.get("wait_for_transfer")),
                      "d.start_load_kv": ms(ev.get("wait_for_transfer"))})
            e.update({k: None for k in EMPTY_D})
            expect_missing += "detokenizer_first" in ev and ev["detokenizer_first"] is None
        expect[(side, x_of(rid))] = e
        if "executions" in ev:
            expect_exec[(side, x_of(rid))] = ev["executions"]
else:
    # The P log as P0 and the D log as D0. The D log is split with the time shift the P log got, so that both sides keep
    # their real distance (check_pd_diag.py prints it).
    shift = None
    for side, path in (("P0", LOG), ("D0", LOG_D)):
        split = os.path.join(WORK, "split-" + side)
        cmd = [sys.executable, "-I", os.path.join(TOOLS, "check_pd_diag.py"), path, "-o", split, "--pack", PACK, "--instance", side]
        if shift is not None:
            cmd += ["--shift", shift]
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        m_ = re.search(r"--shift (-?[0-9.]+)", r.stdout)
        if shift is None and m_:
            shift = m_.group(1)
        # Exit code 1 is the format check: the copy in tools/ knows the 7 kinds of v2.1 and counts pool_task and
        # pd_transfer_summary as unknown (E2). Every line still goes to the filter.
        e2 = sorted(set(re.findall(r"E2[^\n]*", r.stdout)))
        say(r.returncode in (0, 1, 4), "check_pd_diag.py 拆成 %s（退出码 %d%s）" % (side, r.returncode, "，" + e2[0][:120] if e2 else ""))
        shutil.copytree(os.path.join(split, side), os.path.join(pull, "n-" + side, side))

    def load(path):
        out = []
        for line in open(path, errors="replace"):
            i = line.find("[PD_DIAG] ")
            if i >= 0:
                out.append(json.loads(line[i + 10:]))
        return out

    # The values straight from the logs, without the filter.
    for side, path in (("p", LOG), ("d", LOG_D)):
        recs = load(path)
        first, steps = {}, collections.defaultdict(lambda: collections.defaultdict(list))
        for o in recs:
            if o["event"] == "core_summary" and o["scope"] == "execution":
                for rid in o["request_ids"]:
                    first.setdefault(rid, o["execution_id"])
                    for k in ("lookup", "alloc", "other"):
                        v = (o["stages"].get(k) or {}).get("duration_ns")
                        if isinstance(v, int):
                            steps[rid][k].append((o["execution_id"], v))
            if o["event"] == "worker_summary" and o.get("tp_rank") == 0:
                k = "forward" if o["phase"] == "execute" else "sampling"
                for rid in o["request_ids"]:
                    v = (o["stages"].get(k) or {}).get("duration_ns")
                    if isinstance(v, int):
                        steps[rid][k].append((o["execution_id"], v))
        http = {o["observation_id"]: o for o in recs if o["event"] == "api_summary" and o["scope"] == "http"}
        sub = {}
        for o in recs:
            if o["event"] == "api_summary" and o["scope"] == "engine_submission":
                rid = next(l["request_id"] for l in o["links"] if l.get("kind") == "request_id")
                sub[rid] = o
        core = {}
        for o in recs:
            if o["event"] == "core_summary" and o["scope"] == "request":
                if o["request_id"] not in core or o["timestamp_ns"] > core[o["request_id"]]["timestamp_ns"]:
                    core[o["request_id"]] = o
        # v3: the pool tasks of rank 0 that name their request (a shared task names none), and D's read envelope.
        # With more than one record per request the dashboard shows the largest, so the expectation is the largest.
        save, lsub, env = {}, {}, {}
        for o in recs:
            if o["event"] == "pool_task" and o.get("tp_rank", 0) == 0 and o.get("request_id") and o.get("scope") == "task":
                if o.get("operation") == "save" and isinstance(o.get("duration_ns"), int):
                    if o["request_id"] not in save or o["duration_ns"] > save[o["request_id"]]["duration_ns"]:
                        save[o["request_id"]] = o
                if o.get("operation") == "load_submit" and o.get("status") == "submission_returned" and isinstance(o.get("duration_ns"), int):
                    lsub[o["request_id"]] = max(lsub.get(o["request_id"], 0), o["duration_ns"])
            if o["event"] == "pd_transfer_summary" and o.get("scope") == "request_generation" and isinstance(o.get("transfer_envelope_ns"), int):
                env[o["request_id"]] = max(env.get(o["request_id"], 0), o["transfer_envelope_ns"])
        for o in recs:
            if not (o["event"] == "api_summary" and o["scope"] == "request"):
                continue
            rid, X = o["request_id"], x_of(o["request_id"])
            h, c, sg = http[o["observation_id"]], core[rid], steps[rid]
            tot = lambda k: sum(v for _, v in sg[k]) / 1e6 if sg[k] else None
            fst = lambda k: sum(v for e, v in sg[k] if e == first[rid]) / 1e6 if sg[k] else None
            ms = lambda v: None if v is None else v / 1e6
            base = {"total": (h["end_ns"] - h["start_ns"]) / 1e6,
                    "req_resolve": (h["stages"]["req_resolve"]["duration_ns"] + h["stages"]["render"]["duration_ns"]) / 1e6,
                    "tokenizer": h["stages"]["tokenize"]["duration_ns"] / 1e6, "add_request": sub[rid]["stages"]["add_request"]["duration_ns"] / 1e6,
                    "schedule.queue_time": c["stages"]["queue"]["initial_wait_ns"] / 1e6}
            if side == "p":
                e = {"p." + k: v for k, v in base.items() if k != "total"}
                e.update({"total": base["total"], "p.schedule.hash": c["stages"]["hash"]["duration_ns"] / 1e6, "p.schedule.lookup": tot("lookup"),
                          "p.schedule.alloc": tot("alloc"), "p.schedule.other": tot("other"), "p.forward": tot("forward"),
                          "p.sampling": tot("sampling"), "p.detokenizer": o["terminal"]["detokenize_duration_ns"] / 1e6,
                          "p.start_load_kv.submit": ms(lsub.get(rid)), "p.start_load_kv": ms(lsub.get(rid))})
                sv = save.get(rid)
                e["b.wait_for_save"] = ms(sv["duration_ns"]) if sv else None
                for s_ in ("build_key", "lookup", "prepare", "sync", "put"):
                    e["b.wait_for_save." + s_] = ms(((sv or {}).get("stages", {}).get(s_) or {}).get("duration_ns"))
                e.update({k: None for k in EMPTY_P})
            else:
                rg = [x["duration_ns"] for x in o["stages"]["detokenize"]["ranges"] if x["execution_id"] == first[rid]]
                dtot = o["terminal"]["detokenize_duration_ns"]
                e = {"d." + k: v for k, v in base.items() if k != "total"}
                e.update({"total": base["total"], "d.schedule.alloc": fst("alloc"), "d.schedule.other": fst("other"), "d.forward": fst("forward"),
                          "d.sampling": fst("sampling"), "d.detokenizer": rg[0] / 1e6 if rg else None,
                          "r.schedule.alloc": tot("alloc") - fst("alloc"), "r.schedule.other": tot("other") - fst("other"),
                          "r.forward": tot("forward") - fst("forward"), "r.sampling": tot("sampling") - fst("sampling"),
                          "r.detokenizer": (dtot - rg[0]) / 1e6 if rg else None, "steps": len(sg["forward"]) - 1,
                          "d.start_load_kv.wait_for_transfer": ms(env.get(rid)), "d.start_load_kv": ms(env.get(rid))})
                e.update({k: None for k in EMPTY_D})
                expect_missing += not rg
            expect[(side, X)] = e
            m = [x for x in c["members"] if x["request_id"] == rid][0]
            counted = len({e_ for e_, _ in sg["forward"]})
            say(m["executions"] == counted, "%s %s：executions %d，按 request_ids 数出来的步数 %d" % (side, X[-8:], m["executions"], counted))
            expect_exec[(side, X)] = m["executions"]

# ---- containers ------------------------------------------------------------------------------------------------------
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
    # Rows an earlier pack wrote (no config_id) for the same requests: an API row, an engine row and a sum of 1000 ms
    # for forward. The views must not see them.
    old = []
    for (side, X) in sorted(expect):
        role = "prefill" if side == "p" else "decode"
        old.append("(now64(3), '%s', '', '%s', 'api', '', 'glm-5.2', '%s0', 'old', 5000, 9000)" % (X, role, side.upper()))
        old.append("(now64(3), '%s', '', '%s', 'worker', 'old-part', 'glm-5.2', '%s0', 'old', NULL, NULL)" % (X, role, side.upper()))
    ch("INSERT INTO vllm_pd.phase_line (ts, request_id, attempt_id, role, proc, part, model, instance, node, official_e2e_ms, api_total_ms) VALUES "
       + ", ".join(old), fmt=None)
    ch("ALTER TABLE vllm_pd.phase_line UPDATE acc__forward__ns = 1000000000, acc__forward__steps = 1, acc__forward__first_ns = 1000000000 "
       "WHERE part = 'old-part' SETTINGS mutations_sync = 1", fmt=None)
    say(None, "先写进 %d 行旧格式的行（config_id 为空，同一批请求键）" % len(old))
    conf, data = os.path.join(WORK, "vconf"), os.path.join(WORK, "vdata")
    os.makedirs(conf)
    os.makedirs(data)
    with open(os.path.join(conf, "inventory.csv"), "w") as f:
        f.write("addr,name\n")
    env = dict(os.environ, CLICKHOUSE_ADDR=CH, CLICKHOUSE_CONTAINER=CH, CH_WRITER_PASSWORD="x", MODEL_NAME="glm-5.2")
    subprocess.run([sys.executable, "-I", os.path.join(PACK, "scripts", "render_vector.py"), os.path.join(conf, "vector.yaml"),
                    "/var/lib/vector", "/etc/vector"], env=env, check=True)
    sh(["docker", "run", "-d", "--rm", "--name", VEC, "--network", NET, "-v", conf + ":/etc/vector", "-v", pull + ":/pull",
        "-v", data + ":/var/lib/vector", VEC_IMG, "--config", "/etc/vector/vector.yaml"])
    want_api = len(expect)
    last, stable = -1, 0
    for _ in range(120):
        time.sleep(2)
        n = int(ch("SELECT count() FROM vllm_pd.phase_line", fmt="TSV").strip() or 0)
        api = int(ch("SELECT count() FROM vllm_pd.phase_line_cur WHERE proc = 'api' AND part = ''", fmt="TSV").strip() or 0)
        stable = stable + 1 if n == last else 0
        last = n
        if api >= want_api and stable >= 3:
            break
    say(api >= want_api, "采集：phase_line %d 行，其中 API 合并行 %d（应有 %d）" % (n, api, want_api))
    other = rows("SELECT event, count() AS n FROM vllm_pd.filter_other GROUP BY event")
    dead = sh(["docker", "exec", VEC, "sh", "-c", "cat /var/lib/vector/dead-letter-*.jsonl 2>/dev/null | wc -l"]).strip()
    say(not other and dead == "0", "没有认不出的行（filter_other %s），没有解析失败的行（%s）" % (other or "空", dead))
    cfg = rows("SELECT config_id, count() AS n FROM vllm_pd.phase_line WHERE node != 'old' GROUP BY config_id")
    say(len(cfg) == 1 and cfg[0]["config_id"] != "", "采集写的每行都带 config_id：%s" % cfg)

    # ---- the per-request JSON against the expected values ----------------------------------------------------------
    docs = {}
    for X in sorted({k[1] for k in expect}):
        out = ch("SELECT doc FROM vllm_pd.request_json(id = '%s')" % X, fmt="TSVRaw").strip()
        if out:
            docs[X] = json.loads(out)

    def get(doc, side, key):
        if key == "total":
            return doc["latency_ms"]["p_total" if side == "p" else "d_total"]
        if key == "steps":
            return doc["tpot"]["steps"]
        grp, path = key.split(".", 1)
        node = doc["tpot"]["d"] if grp == "r" else doc["background"]["p"] if grp == "b" else doc["ttft"][grp]
        parts = path.split(".")
        try:
            n = node[parts[0]]
            for p_ in parts[1:]:
                n = n["sub"][p_]
            return n["ms"]
        except (KeyError, TypeError):
            return "（JSON 里没有这个路径）"
    diff = n_vals = 0
    seen = {}
    for (side, X), e in sorted(expect.items()):
        doc = docs.get(X)
        if doc is None:
            diff += 1
            if diff <= 5:
                say(False, "%s %s：库里没有这个请求" % (side, X))
            continue
        for k, v in e.items():
            n_vals += 1
            g = get(doc, side, k)
            same = (g is None and v is None) or (isinstance(g, (int, float)) and v is not None and abs(g - v) <= 0.001 + 1e-6 * abs(v))
            seen[k] = seen.get(k, 0) + (g is not None and not isinstance(g, str))
            if not same:
                diff += 1
                if diff <= 8:
                    say(False, "%s %s %s：应为 %s，库里 %s" % (side, X[-12:], k, v, g))
    say(diff == 0, "单请求 JSON：%d 个请求、%d 个值，和%s比，差 %d 个" % (len(docs), n_vals, "mock 真值" if MODE == "mock" else "直接从日志算的值", diff))
    # The JSON's "missing" notes (for example a process count other than 3 on a side).
    notes = collections.Counter(m for d_ in docs.values() for m in (d_.get("missing") or []))
    say(None, "单请求 JSON 的 missing 提示：%s" % (dict(notes) if notes else "无"))
    # Per segment: in how many requests the dashboard has a value (the new v3 segments and the ones left empty).
    keys = sorted({k for e in expect.values() for k in e})
    say(None, "各段有值的请求数：" + "，".join("%s %d" % (k, seen.get(k, 0)) for k in keys if k not in ("total", "steps")))
    if MODE == "replay":
        for (side, X), e in sorted(expect.items()):
            doc = docs.get(X) or {}
            for k in sorted(e):
                if k.startswith(("p.start_load_kv", "b.", "d.start_load_kv", "p.schedule.queue_time", "d.schedule.queue_time")):
                    say(None, "  %s %s %s：日志 %s，看板 %s" % (side, X[-12:], k, e[k], get(doc, side, k) if doc else None))
    ex = rows("SELECT request_id, toString(role) AS role, argMaxIf(executions, core_mono_ns, isNotNull(core_mono_ns) AND isNotNull(executions)) AS e, "
              "countIf(isNotNull(core_mono_ns)) AS n FROM vllm_pd.phase_line_cur GROUP BY request_id, attempt_id, role")
    got_exec = {("p" if r["role"] == "prefill" else "d", r["request_id"]): r["e"] for r in ex if r["n"]}
    dup = sum(1 for r in ex if r["n"] > 1)
    bad_exec = sum(1 for k, v in expect_exec.items() if got_exec.get(k) != v)
    say(bad_exec == 0 and dup == 0, "轮次 / 步数（executions）：%d 个，不同 %d；core_summary[request] 多于一条的请求 %d" % (len(expect_exec), bad_exec, dup))
    miss = rows("SELECT count() AS n FROM (SELECT request_id, argMinIf(first_exec, first_mono_ns, first_exec != '') AS fe, "
                "anyIf(rg__detokenize__exec, notEmpty(rg__detokenize__exec)) AS rx FROM vllm_pd.phase_line_cur WHERE role = 'decode' "
                "GROUP BY request_id, attempt_id HAVING notEmpty(rx) AND fe != '' AND indexOf(rx, fe) = 0)")[0]["n"]
    say(int(miss) == expect_missing, "D 第一步的 detokenize 区间不在 ranges 里的请求：库里 %s，应为 %d（第一步、第二步起都留空）" % (miss, expect_missing))

    # ---- every dashboard query runs ---------------------------------------------------------------------------------
    board = json.load(open(os.path.join(PACK, "generated", "vllm-pd-request.json")))
    one = sorted(docs)[0] if docs else ""
    sub_ = {"$__timeFilter(ts)": "ts >= now() - INTERVAL 1 HOUR", "$__fromTime": "(now() - INTERVAL 1 HOUR)", "$__toTime": "now()",
            "'$model'": "'glm-5.2'", "${instance:singlequote}": "'P0','D0'", "${request_id}": one, "${order_by:text}": "x"}
    failed, empty, total_q = [], [], 0

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
                for k, v in sub_.items():
                    q = q.replace(k, v)
                q = q.replace("${order_by}", order)
                r = subprocess.run(["docker", "exec", "-i", CH, "clickhouse-client", "--user", "vllm_writer", "--password", "x", "-n"],
                                   input=q + " FORMAT JSONEachRow;\n", stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                if r.returncode:
                    failed.append("%s [%s]: %s" % (p_.get("title"), order, r.stderr.strip().splitlines()[0][:200] if r.stderr.strip() else "?"))
                elif not r.stdout.strip():
                    empty.append("%s [%s]" % (p_.get("title"), order))
    say(not failed, "看板查询：%d 次（5 种排序依据），出错 %d" % (total_q, len(failed)))
    for f in failed[:6]:
        say(None, "  " + f)
    say(None, "没有数据的查询：%d（%s）" % (len(empty), "；".join(sorted(set(e.split(" [")[0] for e in empty)))[:300]))
finally:
    cleanup()
    with open(os.path.join(WORK, "e2e-%s.txt" % MODE), "w") as f:
        f.write("\n".join(out_lines) + "\n")
fails = sum(1 for x in out_lines if x.startswith("FAIL"))
print("%d FAIL" % fails)
sys.exit(1 if fails else 0)
