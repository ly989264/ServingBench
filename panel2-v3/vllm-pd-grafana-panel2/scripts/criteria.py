#!/usr/bin/env python3
"""Judgement functions shared by the upgrade-safety check (bin/panel2_cluster.sh check), the update script, nodes.py and
diag.py, so that every tool gives the same answer on the same facts (design 2.1, 2.6, 2.7, 2.10, 2.13).

The functions here do not collect anything: callers gather the facts their own way (SSH, docker, ClickHouse, Prometheus)
and pass them in. The few helpers that build a query return the query text; running it is the caller's job. Nothing here
writes files or changes any service.

Every judgement returns a Verdict:
    Verdict(level, text, data)
    level   "OK" | "WARN" | "FAIL" | "INFO"
    text    one summary line in Chinese, without the machine address (the caller prefixes it)
    data    dict of the numbers behind the line, for reports and tests

Python 3.9 and later, standard library only.
"""
import hashlib
import json
import os
import re
from dataclasses import dataclass, field

# The two node files whose change needs a vLLM restart; segments.json is reread by the filter (design 1.2).
FILTER_FILES = ("pd_diag_logfilter.py", "pd_diag_logging.json")
NODE_FILES = FILTER_FILES + ("segments.json",)
# Mock: a D instance keeps up with about 1.7-2.3 requests a second; the guard sits below that (T32 A1).
MOCK_RATE_PER_D_MAX = 1.5
LEVELS = ("OK", "INFO", "WARN", "FAIL")
# line.keep_hours of the pack's segments.json; deploy.env's NODE_LOG_KEEP_HOURS replaces it in the cluster folder (known issue 14).
PACK_KEEP_HOURS = 12
# The two filter files of pack two v2.0 (tar 20dcc517); v2.1 does not change them.
V20_FILTER_SHAS = {
    "pd_diag_logfilter.py": "5d22a4209ac103f68886aab2e897118085d56c7ec4a9631aa2e845fd843c53b9",
    "pd_diag_logging.json": "9c8dbf061c72649336aa8c8aee25075aefe73f502e142565a71202eb9c7b2428",
}


@dataclass
class Verdict:
    level: str
    text: str
    data: dict = field(default_factory=dict)


def worst(verdicts):
    """The highest level among verdicts (FAIL > WARN > INFO > OK); "OK" for an empty list."""
    return max((v.level for v in verdicts), key=LEVELS.index, default="OK")


# ==================== node files ====================
def file_sha256(path):
    """sha256 hex of a file's bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def expected_node_sums(cluster_dir):
    """{file name: sha256} of the three node files as the monitoring machine would push them: node/pd_diag_logfilter.py,
    node/pd_diag_logging.json and the cluster folder's own segments.json (after log_dir was set from deploy.env)."""
    return {
        "pd_diag_logfilter.py": file_sha256(os.path.join(cluster_dir, "node", "pd_diag_logfilter.py")),
        "pd_diag_logging.json": file_sha256(os.path.join(cluster_dir, "node", "pd_diag_logging.json")),
        "segments.json": file_sha256(os.path.join(cluster_dir, "segments.json")),
    }


def judge_node_sums(expected, found):
    """found: {file name: sha256 or "" when missing} read on one node. FAIL listing the files that differ or are missing."""
    bad = [n for n in NODE_FILES if found.get(n) != expected.get(n)]
    if bad:
        return Verdict("FAIL", "节点文件和监控机不同或缺：%s → bin/nodes.sh push" % " ".join(bad), {"bad": bad})
    return Verdict("OK", "三个节点文件的 sha256 和监控机相同（segments.json %s）" % expected["segments.json"][:8], {})


# ==================== segments.json version ====================
def segments_version(path):
    """The version string a filter reports for this segments.json: "<config_id>@<sha1 of the file's bytes, 8 hex>"."""
    raw = open(path, "rb").read()
    cid = json.loads(raw.decode("utf-8")).get("config_id", "?")
    return "%s@%s" % (cid, hashlib.sha1(raw).hexdigest()[:8])


def expected_version(cluster_dir):
    """The version the nodes' filters should report: that of the cluster folder's segments.json (design 2.6), not the
    pack's."""
    return segments_version(os.path.join(cluster_dir, "segments.json"))


def set_log_dir(text, log_dir):
    """segments.json text with line.log_dir replaced by log_dir, every other byte kept (so that a site edit that only
    changed this value gives the same bytes, design 1.2). Raises ValueError when the file has no single log_dir value."""
    pat = re.compile(r'("log_dir"\s*:\s*)"((?:[^"\\]|\\.)*)"')
    if len(pat.findall(text)) != 1:
        raise ValueError("segments.json 里的 log_dir 不是正好一处")
    return pat.sub(lambda m: m.group(1) + json.dumps(log_dir), text, count=1)


KEEP_HOURS_RE = re.compile(r'("keep_hours"\s*:\s*)(-?[0-9][0-9.eE+-]*)')


def keep_hours_value(raw):
    """deploy.env's NODE_LOG_KEEP_HOURS as a number: blank means PACK_KEEP_HOURS. Raises ValueError for anything but a
    positive number (known issue 14)."""
    s = str(raw if raw is not None else "").strip()
    if not s:
        return float(PACK_KEEP_HOURS)
    try:
        h = float(s)
    except ValueError:
        raise ValueError("NODE_LOG_KEEP_HOURS=%s 不是数字" % s) from None
    if not 0 < h < float("inf"):
        raise ValueError("NODE_LOG_KEEP_HOURS=%s 要大于 0" % s)
    return h


def hours_text(h):
    """12.0 -> "12", 0.5 -> "0.5": the way the value is written into segments.json."""
    h = float(h)
    return "%d" % h if h.is_integer() else repr(h)


def set_keep_hours(text, hours):
    """segments.json text with line.keep_hours replaced by hours, every other byte kept, as set_log_dir does for log_dir
    (known issue 14). Raises ValueError when the file has no single keep_hours value."""
    if len(KEEP_HOURS_RE.findall(text)) != 1:
        raise ValueError("segments.json 里的 keep_hours 不是正好一处")
    return KEEP_HOURS_RE.sub(lambda m: m.group(1) + hours_text(hours), text, count=1)


def segments_key_diff(installed, packed, ignore=("line.log_dir",)):
    """Key-level differences between two parsed segments.json, as [(dotted key, installed value, pack value)], keys in
    `ignore` left out (design 1.2, suggestion 2). Lists compare as whole values."""
    out = []

    def walk(a, b, prefix):
        if isinstance(a, dict) and isinstance(b, dict):
            for k in sorted(set(a) | set(b)):
                walk(a.get(k), b.get(k), prefix + "." + k if prefix else k)
        elif a != b and prefix not in ignore:
            out.append((prefix, a, b))

    walk(installed, packed, "")
    return out


# ==================== mock ====================
def effective_mock_rate(mock_rate, n_d):
    """MOCK_RATE as the mock uses it: the whole model's requests a second. Blank means one per D instance (A1 1)."""
    s = str(mock_rate or "").strip()
    return float(s) if s else float(n_d)


def judge_mock_rate(mock_rate, n_d):
    """Each D carries MOCK_RATE / (D instances in the inventory). FAIL above 1.5 a second, with the suggested value."""
    rate = effective_mock_rate(mock_rate, n_d)
    per_d = rate / n_d if n_d else 0.0
    data = {"rate": rate, "n_d": n_d, "per_d": per_d, "suggested": n_d}
    if per_d > MOCK_RATE_PER_D_MAX:
        return Verdict("FAIL", "MOCK_RATE=%g，%d 个 D，每个 D %.1f 请求/秒，超过 %.1f → 改成 %d（或留空）" % (
            rate, n_d, per_d, MOCK_RATE_PER_D_MAX, n_d), data)
    return Verdict("OK", "mock 速率 %g 请求/秒，%d 个 D，每个 D %.1f" % (rate, n_d, per_d), data)


def judge_mock_processes(found, instance_names):
    """found: run-instance processes counted on one machine; instance_names: that machine's instances in the inventory."""
    want = len(instance_names)
    data = {"found": found, "want": want}
    if found == want:
        return Verdict("OK", "mock 进程 %d 个，应为 %d 个（%s）" % (found, want, " ".join(instance_names)), data)
    fix = "bin/nodes.sh start" if found < want else "有旧进程没停干净：bin/nodes.sh stop 后再 bin/nodes.sh start"
    return Verdict("FAIL", "mock 进程 %d 个，应为 %d 个 → %s" % (found, want, fix), data)


# ==================== one vLLM process since its start (fact 4, design 2.1) ====================
@dataclass
class Proc:
    """One filter process as seen on a node: an API server, EngineCore or Worker of vLLM, or a mock run-instance."""
    addr: str
    instance: str          # instance name from the inventory (P0, D3, ...), "" when unknown
    pid: int
    start_epoch: float     # process start time, seconds since the epoch, node clock
    env: dict = field(default_factory=dict)   # only the PD_DIAG_* / VLLM_* keys diag reads
    is_child: bool = False
    readable: bool = True  # False when /proc/<pid>/environ could not be read


def records_since_start_sql(table, procs):
    """SQL giving which processes and instances have written something since their start. Columns: instance, pid, n.
    filter_selfcheck carries (instance, pid): a row there since the process started means that process has had a record
    (it self-checks only into files a record opened). The line table has no pid: rows of an instance since the earliest
    start of its processes come back with pid 0, meaning "this instance". The caller runs it read-only."""
    if not procs:
        return None
    q = lambda x: x.replace("'", "")
    by_pid = " OR ".join("(instance = '%s' AND pid = %d AND ts >= toDateTime64(%f, 3))" % (
        q(p.instance), int(p.pid), float(p.start_epoch)) for p in procs)
    first = {}
    for p in procs:
        first[p.instance] = min(first.get(p.instance, p.start_epoch), p.start_epoch)
    by_inst = " OR ".join("(instance = '%s' AND ts >= toDateTime64(%f, 3))" % (q(i), float(t)) for i, t in first.items())
    return ("SELECT instance, pid, count() AS n FROM ("
            "SELECT instance, toUInt64(pid) AS pid FROM vllm_pd.filter_selfcheck WHERE %s "
            "UNION ALL SELECT instance, toUInt64(0) AS pid FROM %s WHERE %s) GROUP BY instance, pid") % (by_pid, table, by_inst)


def process_record_state(proc, seen):
    """seen: set of (instance, pid) that have rows since their start (from records_since_start_sql).
    Returns "records", "no_records_filter_created" (the handler exists: PD_DIAG_INSTANCE_SOURCE is in a child's
    environment, or in its own) or "no_records_no_filter"."""
    if (proc.instance, int(proc.pid)) in seen:
        return "records"
    if proc.env.get("PD_DIAG_INSTANCE_SOURCE"):
        return "no_records_filter_created"
    return "no_records_no_filter"


def instance_record_state(procs, seen):
    """Per instance, not per process (helper processes of vLLM, such as the DP coordinator or resource_tracker, never
    write a record): an instance "has records" when any of its processes has a row since its start.
    procs: Proc of one or more machines; seen: set of (instance, pid) from records_since_start_sql, where pid 0 stands
    for "a line of this instance".
    Returns {instance: state}, state one of "records", "no_records_filter_created" (none has a record, one carries
    PD_DIAG_INSTANCE_SOURCE), "no_records_no_filter", "unknown" (no record and the environment of some process could
    not be read, so the filter cannot be told apart)."""
    by = {}
    for p in procs:
        by.setdefault(p.instance, []).append(p)
    out = {}
    for inst, ps in by.items():
        if (inst, 0) in seen or any((p.instance, int(p.pid)) in seen for p in ps):
            out[inst] = "records"
        elif any(p.readable and p.env.get("PD_DIAG_INSTANCE_SOURCE") for p in ps):
            out[inst] = "no_records_filter_created"
        elif any(not p.readable for p in ps):
            out[inst] = "unknown"
        else:
            out[inst] = "no_records_no_filter"
    return out


def judge_process_states(states, item):
    """states: {instance: state} from instance_record_state for one machine; item: "过滤器" | "版本" | "链路通".
    The rule of design 2.1 for all three items: instances without a record since their start are WARN when the filter
    exists (or cannot be told) and FAIL when it does not. Instances with records are left to the item itself.
    Returns a list of Verdicts, empty when every instance has records."""
    groups = {}
    for inst, st in sorted(states.items()):
        if st != "records":
            groups.setdefault(st, []).append(inst)
    out = []
    if groups.get("no_records_filter_created"):
        ins = groups["no_records_filter_created"]
        out.append(Verdict("WARN", "%s：%s 这次启动以后还没有记录，有请求后再查" % (item, " ".join(ins)),
                           {"instances": ins, "state": "no_records_filter_created"}))
    if groups.get("unknown"):
        ins = groups["unknown"]
        out.append(Verdict("WARN", "%s：%s 这次启动以后没有记录，读不了进程环境变量，分不清过滤器有没有创建" % (item, " ".join(ins)),
                           {"instances": ins, "state": "unknown"}))
    if groups.get("no_records_no_filter"):
        ins = groups["no_records_no_filter"]
        out.append(Verdict("FAIL", "%s：%s 这次启动以后没有记录，过滤器也没有创建 → bin/diag.sh vllm 看结论" % (item, " ".join(ins)),
                           {"instances": ins, "state": "no_records_no_filter"}))
    return out


# ==================== versions (design 2.6) ====================
def current_versions(rows):
    """rows: [(version, instance, pid, first_epoch, last_epoch)] of filter_selfcheck in a recent window. Leaves out the
    processes that have stopped: those whose last line is older than the first line of a later process of the same
    instance (a mock or vLLM restart within the window would otherwise look like two versions writing at once).
    Returns [(version, instance, pid)] for judge_versions."""
    newest_first = {}
    for v, inst, pid, first, last in rows:
        newest_first[inst] = max(newest_first.get(inst, first), first)
    return [(v, inst, pid) for v, inst, pid, first, last in rows if last >= newest_first[inst] - 1]


# A running process whose start is at most this many seconds after a row's last line still counts as started after it
# (the node's start time is computed from whole seconds).
RESTART_SLACK_S = 2


def _same_instance(proc_inst, inst):
    return proc_inst in ("", inst)


def restarted_rows(rows, alive, addr_of):
    """Known issue 7: the rows of processes that a restart replaced. A row is one when its pid is not running on its
    machine and a running process of the same instance (of any instance of that machine when the process's instance
    is not known) started after the row's last line.
    rows: [(version, instance, pid, first_epoch, last_epoch)] of filter_selfcheck, by the node clock;
    alive: {addr: [(instance, pid, start_epoch)]} of the filter processes running now on the machines whose probe
    worked, start_epoch by the node clock, "" as instance when it is not known; addr_of: {instance: addr}.
    A row whose pid is not running and that no later process explains is not one of them: the pids may come from
    another pid namespace, and the row then counts as before. Returns a set of (instance, pid)."""
    out = set()
    for v, inst, pid, first, last in rows:
        procs = alive.get(addr_of.get(inst))
        if procs is None:
            continue
        mine = [x for x in procs if _same_instance(x[0], inst)]
        if any(int(p) == int(pid) for _, p, _ in mine):
            continue
        if any(start + RESTART_SLACK_S >= last for _, _, start in mine):
            out.add((inst, int(pid)))
    return out


def live_versions(rows, alive, addr_of):
    """[(version, instance, pid)] for judge_versions: current_versions after leaving out the rows of processes a restart
    replaced (restarted_rows). Machines missing from `alive` (their probe failed) keep the rule of current_versions
    alone."""
    gone = restarted_rows(rows, alive, addr_of)
    return current_versions([r for r in rows if (r[1], int(r[2])) not in gone])


def starting_instances(rows, alive, addr_of, node_now, limit):
    """Known issue 7: instances with a running filter process younger than `limit` seconds by the node clock and no
    self-check row from any of its running processes: just started, the first self-check line is on its way.
    node_now: {addr: now by that machine's clock}. Processes whose instance is not known are left out. Returns the
    instance names, sorted."""
    out = set()
    by_inst = {}
    for v, inst, pid, first, last in rows:
        by_inst.setdefault(inst, set()).add(int(pid))
    for addr, procs in alive.items():
        now = node_now.get(addr)
        if now is None:
            continue
        running = {}
        for inst, pid, start in procs:
            if inst:
                running.setdefault(inst, []).append((int(pid), start))
        for inst, ps in running.items():
            if addr_of.get(inst, addr) != addr:
                continue
            if by_inst.get(inst, set()) & {p for p, _ in ps}:
                continue
            if any(now - start < limit for _, start in ps):
                out.add(inst)
    return sorted(out)


def judge_versions(proc_versions, expected, mode, pushed_epoch, now, states=None, starting=()):
    """proc_versions: [(version, instance, pid)] from filter_selfcheck in the last 3 minutes, without the processes that
    stopped (live_versions); expected: expected_version(); starting: starting_instances().
    Within 120 s of pushed_epoch (the last change of segments.json on a node) a version other than `expected` is INFO in
    both modes, also next to `expected`: the filter rereads segments.json every 2 s and self-checks within 60 s
    (known issues 7 and 14). Outside that window two or more versions writing at once are FAIL, and in mock mode
    anything but `expected` is FAIL. No self-check line at all is FAIL in mock mode unless instances are just starting.
    vllm: instances whose state (instance_record_state) is not "records" are skipped here (judge_process_states speaks
    for them). Instances just started get one INFO line. Returns a list of Verdicts."""
    if states is not None and mode == "vllm":
        proc_versions = [x for x in proc_versions if states.get(x[1]) in (None, "records")]
    vs = sorted({v for v, _, _ in proc_versions})
    count = lambda v: sum(1 for x in proc_versions if x[0] == v)
    insts = sorted({i for _, i, _ in proc_versions})
    data = {"versions": {v: count(v) for v in vs}, "expected": expected, "instances": insts, "starting": list(starting)}
    in_window = bool(pushed_epoch) and now - pushed_epoch <= 120
    tail = [Verdict("INFO", "过滤器版本：%s 的进程刚启动，还没有自检行（过滤器每 60 秒写一行自检，再经拉取进 ClickHouse）→ 2 分钟后再查" % (
        " ".join(starting)), data)] if starting else []
    old = [v for v in vs if v != expected]
    if old and in_window and len(old) == 1:
        return [Verdict("INFO", "过滤器版本：下发后 %d 秒，还有进程是 %s，期望 %s；过滤器 2 秒内重读 segments.json，下一次自检最多 60 秒，2 分钟后再查" % (
            max(0, now - pushed_epoch), old[0], expected), data)] + tail
    if len(vs) > 1:
        return [Verdict("FAIL", "过滤器版本：两个以上版本同时在写：%s；期望 %s → 旧版本的进程还在：mock 模式先 bin/nodes.sh stop 再 start，vllm 模式重启那台的 vLLM" % (
            "，".join("%s（%d 个进程）" % (v, count(v)) for v in vs), expected), data)] + tail
    if not vs:
        if mode == "mock" and not starting:
            return [Verdict("FAIL", "过滤器版本：最近 3 分钟没有自检行 → bin/nodes.sh status 看 mock 在不在跑", data)]
        return tail
    if vs == [expected]:
        return [Verdict("OK", "过滤器版本：%d 个实例 %d 个进程都是 %s" % (len(insts), len(proc_versions), expected), data)] + tail
    return [Verdict("FAIL", "过滤器版本：%s，期望 %s → bin/nodes.sh push；%s" % (
        vs[0], expected, "mock 模式再 bin/nodes.sh start" if mode == "mock" else "2 分钟后仍不对，看 bin/diag.sh vllm"), data)] + tail


def known_filter_shas(cluster_dir):
    """{file: set of sha256} for FILTER_FILES that a running vLLM may have started with: the cluster folder's SHA256SUMS
    (node/<file> lines) plus the v2.0 values, which v2.1 keeps unchanged (design 1.2, limit 3). The shas_at_start for
    restart_needed()."""
    out = {f: {V20_FILTER_SHAS[f]} for f in FILTER_FILES}
    try:
        for line in open(os.path.join(cluster_dir, "SHA256SUMS"), encoding="utf-8"):
            parts = line.split()
            if len(parts) == 2:
                name = parts[1].lstrip("*").replace("./", "", 1)
                if name.startswith("node/") and name[5:] in out:
                    out[name[5:]].add(parts[0])
    except OSError:
        pass
    return out


def restart_needed(ctimes, shas, vllm_start, shas_at_start):
    """Design 2.10, blocker 5 fix 2: a vLLM restart is needed only when one of FILTER_FILES both changed after vLLM
    started (ctime later than vllm_start) and has content other than what vLLM started with.
    ctimes, shas: {file: value} read on the node; shas_at_start: {file: set of sha256} the file could have had when vLLM
    started (from the SHA256SUMS of the packs installed then). Returns (bool, [files])."""
    files = [f for f in FILTER_FILES
             if ctimes.get(f, 0) > vllm_start and shas.get(f) not in shas_at_start.get(f, set())]
    return bool(files), files


# ==================== log volume (design 2.7) ====================
def log_rate_query(window="30m"):
    """PromQL for each machine's log bytes a second, as the puller moves them (the same in mock and vllm mode)."""
    return "sum by (machine) (rate(vllm_puller_transferred_bytes_total[%s]))" % window


def estimate_log_bytes(bytes_per_s, keep_hours):
    """Bytes a node keeps at this rate: bytes_per_s x keep_hours; None when the rate is unknown."""
    return None if bytes_per_s is None else float(bytes_per_s) * float(keep_hours) * 3600


def judge_log_volume(estimate, free_bytes, where, keep_hours, param="MOCK_RATE / NODE_LOG_KEEP_HOURS"):
    """FAIL when the estimate does not fit into free_bytes of `where` (a node's log disk or the mirror disk); WARN
    "估不出" when the estimate is None (the puller is not running)."""
    data = {"estimate": estimate, "free": free_bytes, "where": where}
    if estimate is None:
        return Verdict("WARN", "%s：拉取程序没在运行，估不出 %s 小时的日志量" % (where, keep_hours), data)
    if estimate > free_bytes:
        return Verdict("FAIL", "%s：按最近 30 分钟估 %s 小时 %s，空闲只有 %s → 调小 %s" % (
            where, keep_hours, human(estimate), human(free_bytes), param), data)
    return Verdict("OK", "%s：空闲 %s；%s 小时日志估 %s" % (where, human(free_bytes), keep_hours, human(estimate)), data)


# ==================== chain thresholds (advisor blocker 3) ====================
def chain_thresholds(line, pull_interval_s):
    """OK limits in seconds for the age of the newest line at each stage, from segments.json's line block and
    PULL_INTERVAL_S: node = selfcheck + flush, mirror = node + pull interval, clickhouse = mirror + 60. WARN up to 3x."""
    node = float(line.get("selfcheck_seconds", 60)) + float(line.get("flush_seconds", 5))
    mirror = node + float(pull_interval_s or 10)
    return {"node": node, "mirror": mirror, "clickhouse": mirror + 60}


def age_level(age, limit):
    """OK up to limit, WARN up to 3 x limit, FAIL above; FAIL when age is None (nothing there)."""
    if age is None:
        return "FAIL"
    return "OK" if age <= limit else ("WARN" if age <= 3 * limit else "FAIL")


def judge_idle_chain(ages, limit, window):
    """Known issue 10, vllm mode: no row of this version in the last `window` seconds, every instance has written
    records since its start and the puller succeeded within 2 minutes. ages: {instance: seconds since the newest
    self-check line of this version, None when there is none}. The filters self-check every minute with or without
    requests, so: WARN「最近没有请求」while every instance's newest self-check line is at most 3 x limit old; FAIL
    naming the instances whose line is older or missing (their filter stopped writing)."""
    stale = sorted(i for i, a in ages.items() if a is None or a > 3 * limit)
    data = {"ages": dict(ages), "limit": limit, "stale": stale}
    if ages and not stale:
        return Verdict("WARN", "链路通：最近 %d 秒没有这一版的行；拉取正常，各实例这次启动以后都写过记录，最新自检行最多 %d 秒前：最近没有请求" % (
            window, max(ages.values())), data)
    return Verdict("FAIL", "链路通：最近 %d 秒没有这一版的行，%s的最新自检行%s → 过滤器停了，看 bin/diag.sh vllm" % (
        window, " ".join(stale) + " " if stale else "", "超过 %d 秒或没有" % (3 * limit) if stale else "查不到"), data)


# ==================== helpers ====================
def human(n):
    """Bytes as text: 1.4 GB."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024
