#!/usr/bin/env python3
"""Panel 2: check a redirected vLLM log, and turn it into the files the log filter writes, for import into panel 2.

  python3 check_pd_diag.py <log file>                                  format check only (same as check_pd_diag_format.py)
  python3 check_pd_diag.py <log file> -o <out> --node <machine>        also write <out>/<instance>/<role>.<start>.<pid>.<seq>.log

1. Format check: every [PD_DIAG] line against the version 2 format, the 7 kinds of record and the fields panel 2
   reads (check_pd_diag_format.py in this folder does it; the summary and exit code are the same).
2. With -o: every [PD_DIAG] line goes, one by one, to the pack's own log filter (node/pd_diag_logfilter.py with the
   pack's segments.json, found with --pack), as if vLLM had just logged it. What comes out is what the filter writes
   online: the passthrough lines, the per-request sums (pd_seg_partial), the file header and the self-check lines,
   with the same roles, TP rank rule (only rank 0 of worker_summary is kept) and file names. The puller, the
   collector and the dashboard take these files unchanged.

   What the filter takes from its process online is taken from the log here:
   - the process: one filter per pid of the log (the "pid" of each line), with its process name from the line prefix
     "(EngineCore pid=N)"; a line without that prefix is the API server's main process;
   - the host name: the "host" of the lines;
   - the instance: --instance, or else the --port vLLM printed at start ("non-default args: {... 'port': N ...}"),
     or else 8000 when it printed its arguments without a port, or else "unknown";
   - the clock: the filter's clock follows the lines ("timestamp_ns", monotonic); its timer (flush every 5 s,
     self-check every minute, requests idle for 2 minutes closed) runs on that clock. After the last line the clock
     runs on for 130 s, so every request is closed the way it is online when no more lines come;
   - wall time (the time the dashboard shows): the original time of each line is rebuilt from its timestamp_ns and the
     seconds vLLM printed at the start of the lines (local time; the year from the file's date): every line bounds
     the offset between the two clocks to one second, and many lines narrow it to milliseconds. By default the whole
     log is then moved so that its last line is 3 minutes before now, keeping the time between lines: ClickHouse drops
     rows older than the event table's retention, and the dashboard opens on the last 15 minutes. The shift is
     printed; the other side's log of the same requests (P or D) is split with --shift <that value>, so the two sides
     stay aligned. --keep-time keeps the original times.

Options: --pack <install folder of panel 2> (default ~/vllm-pd-grafana-panel2): the filter, segments.json,
inventory.json and deploy.env are read from there. --node <machine address as in inventory.json>: checks that
<machine>:<instance> is in the inventory and prints the commands that put the files where the puller takes them.

Exit code: 0 format OK; 1 format errors (files are still written); 2 bad arguments or a file that cannot be read;
3 no [PD_DIAG] line; 4 the files were written but the instance is not in inventory.json for --node.
Python 3.8 or later, standard library only.
"""
import argparse
import atexit
import importlib.util
import json
import logging
import os
import re
import sys
import time as _time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import check_pd_diag_format as fmt  # noqa: E402

MARK = "[PD_DIAG] "
PREFIX = re.compile(r"(?:\(([A-Za-z][A-Za-z0-9_]*) pid=(\d+)\) )?([A-Z]+) (\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)(?:\.\d+)? \[([^\]:]+):(\d+)\] $")
ARGS_PORT = re.compile(r"non-default args: \{.*?'port': (\d+)")
ARGS_DP = re.compile(r"non-default args: \{.*?'data_parallel_rank': (\d+)")
PROC = re.compile(r"\((\w+) pid=(\d+)\)")
DP_NAME = re.compile(r"_DP(\d+)")
RUN_ON_S = 130      # after the last line: longer than the filter's 120 s idle eviction
BEFORE_NOW_S = 180  # the last line lands this long before now (so the 130 s after it are still in the past)


class Out(object):
    """UTF-8 text to the real stdout. It has no .buffer, so check_pd_diag_format writes to it as it is instead of
    wrapping (and later closing) sys.stdout.buffer."""
    def __init__(self, raw):
        self.raw = raw

    def write(self, s):
        self.raw.write(s.encode("utf-8", "replace"))
        self.raw.flush()

    def flush(self):
        self.raw.flush()


class Ctx(object):
    """What the filter would read from its process and the clock, set before each line it gets."""
    pid = 0
    host = ""
    mono_ns = 0
    offset_s = 0.0


CTX = Ctx()


class TimeShim(object):
    def __getattr__(self, name):
        return getattr(_time, name)

    def time(self):
        return CTX.mono_ns / 1e9 + CTX.offset_s

    def monotonic(self):
        return CTX.mono_ns / 1e9

    def monotonic_ns(self):
        return CTX.mono_ns

    def localtime(self, secs=None):
        return _time.localtime(self.time() if secs is None else secs)


class OsShim(object):
    def __getattr__(self, name):
        return getattr(os, name)

    def getpid(self):
        return CTX.pid


class SocketShim(object):
    def gethostname(self):
        return CTX.host


def load_filter(pack):
    path = os.path.join(pack, "node", "pd_diag_logfilter.py")
    spec = importlib.util.spec_from_file_location("pd_diag_logfilter_offline", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.time, mod.os, mod.socket = TimeShim(), OsShim(), SocketShim()
    return mod


def prepass(path):
    """Instance hints and the clock: the range of timestamp_ns, and the wall-time offset (wall = mono + offset) that
    every line's printed second allows. A line printed at second S with monotonic time t says S <= t + offset < S + 1;
    over many lines these bounds close in to well under a second."""
    api = []            # (API server pid or None, port, DP rank) of every "non-default args" line
    lo = hi = None
    off_lo, off_hi = None, None
    votes = {}          # offset rounded to the millisecond (the middle of each line's one-second window) -> lines
    year = _time.localtime(os.stat(path).st_mtime).tm_year
    now = _time.time()
    cache = {}
    with fmt.open_text(path) as fh:
        for b in fh:
            line = b.decode("utf-8", "replace")
            i = line.find(MARK)
            if i < 0:
                if "non-default args" in line:
                    m = ARGS_PORT.search(line)
                    d = ARGS_DP.search(line)
                    pm = PROC.search(line)
                    api.append((int(pm.group(2)) if pm else None, m.group(1) if m else "8000", d.group(1) if d else "0"))
                continue
            try:
                mono = json.loads(line[i + len(MARK):]).get("timestamp_ns")
            except ValueError:
                continue
            if not isinstance(mono, int):
                continue
            lo = mono if lo is None else min(lo, mono)
            hi = mono if hi is None else max(hi, mono)
            m = PREFIX.search(line[:i])
            if m:
                key = m.group(4, 5, 6, 7, 8)
                w = cache.get(key)
                if w is None:
                    mo, d, hh, mi, ss = (int(x) for x in key)
                    w = _time.mktime((year, mo, d, hh, mi, ss, 0, 0, -1))
                    if w > now + 86400:      # a log from last year
                        w = _time.mktime((year - 1, mo, d, hh, mi, ss, 0, 0, -1))
                    cache[key] = w
                t = mono / 1e9
                off_lo = w - t if off_lo is None else max(off_lo, w - t)
                off_hi = w + 1 - t if off_hi is None else min(off_hi, w + 1 - t)
                k = round(w + 0.5 - t, 3)
                votes[k] = votes.get(k, 0) + 1
    return api, lo, hi, off_lo, off_hi, votes


def median_offset(votes):
    """The offset most lines agree with, and half the spread of the middle half, when the strict bounds do not close
    (a line whose printed second contradicts its timestamp_ns, e.g. after the clock was stepped)."""
    keys = sorted(votes)
    total = sum(votes.values())
    acc, q = 0, {}
    for k in keys:
        acc += votes[k]
        for name, frac in (("q1", 0.25), ("med", 0.5), ("q3", 0.75)):
            if name not in q and acc >= total * frac:
                q[name] = k
    return q["med"], (q["q3"] - q["q1"]) / 2 + 0.5


def wall_offset(opts, hi, off_lo, off_hi, votes):
    """(offset, shift, how): wall = mono + offset. The original offset comes from the printed times; the shift moves
    the whole log (default: its last line to BEFORE_NOW_S before now)."""
    orig, note = None, ""
    if off_lo is not None and off_lo <= off_hi:
        orig, err = (off_lo + off_hi) / 2, (off_hi - off_lo) / 2
    elif votes:
        orig, err = median_offset(votes)
        note = "（有的行打印的时间和行里的单调时刻对不上，可能中途调过钟；按多数行估计）"
    if opts.keep_time or opts.shift is not None:
        if orig is None:
            raise SystemExit("日志行前面没有 vLLM 的时间前缀（MM-DD hh:mm:ss），还原不了原来的时间；不要用 --keep-time / --shift")
        shift = 0.0 if opts.keep_time else float(opts.shift)
        what = "原来的时间" if opts.keep_time else "整体平移 %.3f 秒（--shift）" % shift
    elif orig is None:
        return (_time.time() - BEFORE_NOW_S) - hi / 1e9, None, "没有时间前缀：最后一行放在现在之前 %d 秒（P、D 对不齐）" % BEFORE_NOW_S
    else:
        shift = round((_time.time() - BEFORE_NOW_S) - (hi / 1e9 + orig), 3)
        what = "整体平移 %.3f 秒，最后一行落在现在之前 %d 秒" % (shift, BEFORE_NOW_S)
    return orig + shift, shift, "%s；原时间按每行打印的秒还原，误差 ±%.0f ms%s" % (what, err * 1000, note)


def inventory_check(pack, node, instance):
    try:
        inv = json.load(open(os.path.join(pack, "inventory.json")))
    except (IOError, OSError, ValueError) as e:
        return False, "读不了 %s：%s" % (os.path.join(pack, "inventory.json"), e)
    names = {}
    for side, letter in (("prefill", "P"), ("decode", "D")):
        for k, addr in enumerate(inv.get(side, {}).get("api_servers", [])):
            names[addr] = "%s%d" % (letter, k)
    if node is None:
        hits = sorted(a for a in names if a.rsplit(":", 1)[1] == instance)
        return bool(hits), ("清单里端口是 %s 的有：%s（用 --node 指定放到哪台）" % (instance, "、".join("%s=%s" % (names[a], a) for a in hits))
                            if hits else "清单 inventory.json 里没有端口 %s" % instance)
    addr = "%s:%s" % (node, instance)
    if addr in names:
        return True, "%s 在清单里，是 %s" % (addr, names[addr])
    return False, "%s 不在清单 inventory.json 里：看板认不出这个实例。清单里的有：%s" % (addr, "、".join(sorted(names)))


def node_log_dir(pack):
    try:
        for line in open(os.path.join(pack, "deploy.env")):
            if line.startswith("NODE_LOG_DIR="):
                return line.split("=", 1)[1].strip().strip('"') or None
    except (IOError, OSError):
        pass
    return None


def split(path, out, opts, out_stream):
    pack = os.path.expanduser(opts.pack)
    for f in ("node/pd_diag_logfilter.py", "segments.json"):
        if not os.path.isfile(os.path.join(pack, f)):
            out_stream.write("拆分：--pack %s 下没有 %s（要指向面板二的安装目录）\n" % (pack, f))
            return 2
    api, lo, hi, off_lo, off_hi, votes = prepass(path)
    if hi is None:
        out_stream.write("拆分：没有可用的 [PD_DIAG] 行\n")
        return 3
    ports = sorted({a[1] for a in api})
    by_pid = {a[0]: (a[1], a[2]) for a in api if a[0] is not None}
    by_dp = {}
    for a in api:
        by_dp.setdefault(a[2], a[1])
    multi = not opts.instance and len(ports) > 1

    def instance_of(proc, pid):
        """(instance, source, DP rank) of one process. One vllm serve per file: its port. A file with several
        (external DP: one vllm serve per rank, all into the same file): the API server by its pid, EngineCore and
        Worker by the DP rank in their name (EngineCore_DP3, Worker_DP3_TP1_EP7)."""
        if opts.instance:
            return opts.instance, "env", None
        if not ports:
            return "unknown", "env", None
        if not multi:
            return ports[0], "argv" if ports[0] != "8000" else "default", api[0][2] if api[0][2] != "0" else None
        if pid in by_pid:
            port, dp = by_pid[pid]
            return port, "argv", dp
        m = DP_NAME.search(proc or "")
        if m and m.group(1) in by_dp:
            return by_dp[m.group(1)], "argv", m.group(1)
        return "unknown", "env", None

    offset, shift, how = wall_offset(opts, hi, off_lo, off_hi, votes)
    saved_argv, saved_env = sys.argv, {k: os.environ.get(k) for k in ("PD_DIAG_INSTANCE", "PD_DIAG_INSTANCE_SOURCE")}
    per = {}                                       # instance -> processes
    names = set()                                  # process names seen
    mod = load_filter(pack)
    mapping = os.path.join(pack, "segments.json")
    handlers, clock = {}, {}
    CTX.offset_s = offset
    n = 0
    try:
        with fmt.open_text(path) as fh:
            for b in fh:
                line = b.decode("utf-8", "replace")
                i = line.find(MARK)
                if i < 0:
                    continue
                payload = line[i + len(MARK):].rstrip("\r\n")
                try:
                    obj = json.loads(payload)
                except ValueError:
                    obj = {}
                m = PREFIX.search(line[:i])
                pid = obj.get("pid") if isinstance(obj.get("pid"), int) else (int(m.group(2)) if m else 0)
                mono = obj.get("timestamp_ns") if isinstance(obj.get("timestamp_ns"), int) else None
                CTX.pid, CTX.host = pid, str(obj.get("host") or "")
                h = handlers.get(pid)
                if h is None:
                    # What the process's own filter would read: its instance (PD_DIAG_INSTANCE as written back from its
                    # --port) and its command line (--data-parallel-rank, for the file header).
                    inst, src, dp = instance_of(m.group(1) if m else None, pid)
                    os.environ["PD_DIAG_INSTANCE"], os.environ["PD_DIAG_INSTANCE_SOURCE"] = inst, src
                    sys.argv = ["vllm", "serve", "--port", inst] + (["--data-parallel-rank", dp] if dp is not None and (dp != "0" or multi) else [])
                    per.setdefault(inst, []).append(pid)
                    names.add(m.group(1) if m else "MainProcess")
                    h = mod.PdDiagHandler(mapping=mapping, log_dir=out)
                    h._ticker = lambda: None          # its timer is run below, on the log's clock
                    atexit.unregister(h._at_exit)
                    handlers[pid] = h
                if mono is not None:
                    # The filter's 1-second timer, on the log's clock, up to this line (from its second line on: the
                    # first one starts the filter's process state).
                    last = clock.get(pid)
                    while last is not None and last + 10 ** 9 <= mono:
                        last += 10 ** 9
                        CTX.mono_ns = last
                        h._tick()
                    if last is not None:
                        clock[pid] = last
                    CTX.mono_ns = mono
                rec = logging.LogRecord("vllm.v1.metrics.pd_diagnostics", logging.INFO, "pd_diagnostics.py",
                                        int(m.group(10)) if m else 0, "[PD_DIAG] %s", (payload,), None)
                rec.created = CTX.mono_ns / 1e9 + offset
                rec.msecs = (rec.created - int(rec.created)) * 1000
                rec.process = pid
                rec.processName = rec.vllm_process_name = (m.group(1) if m else None) or "MainProcess"
                rec.levelname = m.group(3) if m else "INFO"
                h.emit(rec)
                if clock.get(pid) is None and mono is not None:
                    clock[pid] = mono
                n += 1
        for pid, h in handlers.items():
            CTX.pid = pid
            last = clock.get(pid)
            if last is None:
                continue
            end = last + RUN_ON_S * 10 ** 9
            while last + 10 ** 9 <= end:
                last += 10 ** 9
                CTX.mono_ns = last
                h._tick()
            for f in getattr(h, "_files", {}).values():
                f.fh.flush()
                f.fh.close()
            h._files = {}
    finally:
        sys.argv = saved_argv
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    out_stream.write("\n== 拆分（交给包里的过滤器 %s，segments.json 的 config_id %s）\n" % (
        os.path.join(pack, "node", "pd_diag_logfilter.py"), json.load(open(mapping)).get("config_id")))
    out_stream.write("   %d 个进程，%d 行 [PD_DIAG]，实例 %s；时间：%s\n" % (
        len(handlers), n, "、".join("%s（%d 个进程）" % (k, len(v)) for k, v in sorted(per.items())), how))
    ranks = sorted({DP_NAME.search(name).group(1) for name in names if DP_NAME.search(name)}, key=int)
    if not multi and not opts.instance and len(ranks) > 1:
        out_stream.write("   提醒：进程名里有 %d 个 DP rank（%s），但只找到 %s 的启动参数，全部归到实例 %s。一个 vllm serve 开了 DP 时这样是对的；"
                         "要是每个 rank 是各自的 vllm serve（外部负载均衡，各有端口），说明日志里缺了其他 rank 的启动参数（比如从中间截的），这样拆是错的\n" % (
                             len(ranks), "、".join(ranks), "%d 个 vllm serve" % len(ports) if ports else "0 个 vllm serve", "、".join(sorted(per)) or "unknown"))
    if multi:
        out_stream.write("   这份日志里有 %d 个 vllm serve（端口 %s），按各自的端口分开；EngineCore、Worker 按进程名里的 DP rank 归到对应的端口\n" % (
            len(ports), "、".join(ports)))
    counts = {}
    for h in handlers.values():
        for k, v in (getattr(h, "_counts", {}) or {}).items():
            counts[k] = counts.get(k, 0) + v
    keys = ("seen", "written", "accumulated", "partials_written", "rank_dropped", "dropped", "version_dropped", "no_event",
            "empty_key_dropped", "errors")
    out_stream.write("   过滤器计数：%s\n" % "，".join("%s %d" % (k, counts.get(k, 0)) for k in keys))
    ok = True
    logdir = node_log_dir(pack) or "<NODE_LOG_DIR>"
    target = opts.node or "<机器>"
    for instance in sorted(per):
        d = os.path.join(out, instance)
        for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            with open(os.path.join(d, name), "rb") as f:
                out_stream.write("   %s/%s：%d 行\n" % (instance, name, sum(1 for _ in f)))
        good, msg = inventory_check(pack, opts.node, instance)
        ok &= good
        out_stream.write("   清单：%s\n" % msg)
    out_stream.write("   导入（拉取程序下一轮就会拉走）：\n")
    for instance in sorted(per):
        if os.path.isdir(os.path.join(out, instance)):
            out_stream.write("     ssh %s 'mkdir -p %s/%s'\n" % (target, logdir, instance))
            out_stream.write("     rsync -rt %s/ %s:%s/%s/\n" % (os.path.join(out, instance), target, logdir, instance))
    if shift is not None and not opts.keep_time:
        out_stream.write("   同一批请求的另一侧（P 或 D）日志，用 --shift %.3f 拆，两侧的时间才对得上\n" % shift)
    if counts.get("errors"):
        out_stream.write("   过滤器内部出错 %d 次，导入前先看看\n" % counts["errors"])
    return 0 if ok else 4


def main(argv=None):
    ap = argparse.ArgumentParser(description="面板二：校验重定向出来的 vLLM 日志，并拆成过滤器写的文件（导入看板二）")
    ap.add_argument("log", help="一个日志文件（.gz 可以）")
    ap.add_argument("-o", "--out", help="拆分结果放到这个目录：<目录>/<实例>/<角色>.<开始时间>.<pid>.<序号>.log")
    ap.add_argument("--pack", default="~/vllm-pd-grafana-panel2", help="面板二的安装目录（默认 ~/vllm-pd-grafana-panel2）")
    ap.add_argument("--node", help="要放到哪台机器（inventory.json 里的地址），用来核对实例名")
    ap.add_argument("--instance", help="实例名（端口）；不给时从日志里 vLLM 打印的 --port 取")
    ap.add_argument("--keep-time", action="store_true", help="不平移时间，用日志原来的时间")
    ap.add_argument("--shift", type=float, help="整体平移这么多秒（拆同一批请求的另一侧日志时，用第一份打印的值）")
    ap.add_argument("--show-examples", action="store_true", help="格式校验里打印每类问题的原文例子")
    opts = ap.parse_args(argv)
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = Out(sys.stdout.buffer)
    if not os.path.isfile(opts.log):
        sys.stderr.write("读不了 %s\n" % opts.log)
        return 2
    check_args = [opts.log] + (["--show-examples"] if opts.show_examples else [])
    code = fmt.main(check_args)
    if opts.out is None or code in (2, 3):
        return code
    out = sys.stdout
    split_code = split(opts.log, os.path.abspath(opts.out), opts, out)
    out.flush()
    return split_code if split_code else code


if __name__ == "__main__":
    sys.exit(main())
