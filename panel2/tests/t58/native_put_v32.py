#!/usr/bin/env python3
"""Mooncake's own timing of a batch write ([BatchPutTiming] / [BatchUpsertTiming], printed by its C++ client through glog to
vLLM's standard error) for the three segments under P's background put: submit, wait_for_transfer and finalize (phases
2, 3 and 4 of a call). The filter cannot see these lines (they do not go through Python logging), so the puller does:

  1. Fetch (Puller.fetch, every round, one SSH command per source): only these two kinds of line, read from where the last
     round stopped, appended to the mirror PULL_DIR/_mooncake/<machine>/<source>.<day>.log. A source is one entry of
     deploy.env MOONCAKE_LOG_FILES, "address=what;what,address=what" where what is
        /path/with/*glob*        files on the machine itself (host paths)
        exec:<container>:/path   files inside a running container (read through docker exec)
        logs:<container>         the container's docker logs
     The first round of a source starts at the end of each file (or now, for docker logs): only lines written after
     collection was switched on are read. A file seen later (a new name, or a file that was truncated) is read from its start.
  2. Associate (Associator.run_once): a pool_task[save] record (written as is by the node filter into pool.*.log, mirrored
     by rsync) lists its calls into Mooncake (native_calls: native_tid, start_wall_ns, end_wall_ns, submitted_keys,
     submitted_bytes). A timing line belongs to a call when it comes from the same machine and thread, has the same number
     of keys, and its time lies within the call: glog takes it inside the call, after phase 4 (C10). The byte count only
     tells several such lines apart, and exactly one must be left; a matched line whose bytes differ from submitted_bytes
     is counted (bytes_mismatch), since the record format note expects them equal. glog prints the
     node's local time, with the year (IYYYYMMDD, glog 0.6, as on the glm5.3 site) or without it (IMMDD, glog 0.4, as
     Mooncake's npu wheel bundles); both are read. The call's wall times are UTC with the node's offset
     (wall_utc_offset_seconds), so both are compared as the node's local time: in the line's own year when the line has
     one, else in the call's year.
     glog 0.7 and later print another thread number than the call's native_tid (the record format note, section 4).
     Only with deploy.env MOONCAKE_TID_FALLBACK=1 (for those versions) does a call with no line of its own thread take the
     one line of the same machine whose keys, bytes and time fit, when exactly one does (counted as matched_without_tid).
     Off by default: with TP > 1 the ranks' lines arrive one by one, and a rank's line could be taken for rank 0's call
     before rank 0's own line arrives.
     When every call of a task has its line, one record pd_native_put (record version 2) is written for the task into
     PULL_DIR/_native/<machine>/<instance>/native.<start>.<pid>.<seq>.log, in the node files' line format, with the phase
     sums as stages.<segment>.duration_ns. The collector reads it like any other record (segments.json src). A task whose
     lines do not all arrive within MATCH_WINDOW_S, and a line no task claims within it, are dropped and counted.

Standard library only; Python 3.9 and later.
"""
import datetime
import glob
import json
import os
import re
import shlex
import socket
import subprocess
import tempfile
import threading
import time

MARKS = ("[BatchPutTiming]", "[BatchUpsertTiming]")
# glog's prefix: IYYYYMMDD (glog 0.6) or IMMDD (glog 0.4), then the node's local time, thread and file:line.
GLOG = re.compile(r"^[IWEF](\d{4})?(\d{2})(\d{2}) (\d{2}):(\d{2}):(\d{2})\.(\d{6})\s+(\d+) [^\]]*\] \[(BatchPutTiming|BatchUpsertTiming)\] (.*)$")
POOL_FILE = re.compile(r"^pool\.\d{8}T\d{6}\.\d+\.\d{4}\.log$")
PHASES = (("submit", "phase2_submit_transfers_us"), ("wait_for_transfer", "phase3_wait_transfers_us"),
          ("finalize", "phase4_finalize_batch_put_us"), ("start_batch_put", "phase1_start_batch_put_us"))
MATCH_WINDOW_S = 120.0
CHUNK_CAP = 256 * 1024 * 1024       # bytes of one file read in one round at most; the rest follows in the next rounds
OUT_ROTATE = 64 * 1024 * 1024


# ---- the sources ------------------------------------------------------------------------------------------------------

def parse_sources(value):
    """MOONCAKE_LOG_FILES as {address: [source]}; a source is ("host", None, [pattern]), ("exec", container, [pattern])
    or ("logs", container, None). Raises ValueError on a malformed entry."""
    out = {}
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        addr, eq, rest = item.partition("=")
        addr, rest = addr.strip(), rest.strip()
        if not eq or not addr or not rest:
            raise ValueError("MOONCAKE_LOG_FILES 的「%s」不是 地址=来源" % item)
        host_pats = []
        for what in (w.strip() for w in rest.split(";")):
            if not what:
                continue
            if what.startswith("logs:"):
                ctr = what[5:]
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", ctr):
                    raise ValueError("MOONCAKE_LOG_FILES 的容器名「%s」不对" % ctr)
                out.setdefault(addr, []).append(("logs", ctr, None))
            elif what.startswith("exec:"):
                ctr, colon, pat = what[5:].partition(":")
                if not colon or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", ctr) or not pat.startswith("/"):
                    raise ValueError("MOONCAKE_LOG_FILES 的「%s」要写成 exec:容器名:/容器里的路径" % what)
                out.setdefault(addr, []).append(("exec", ctr, [pat]))
            elif what.startswith("/"):
                host_pats.append(what)
            else:
                raise ValueError("MOONCAKE_LOG_FILES 的「%s」：路径要写绝对路径，或 exec:容器:/路径、logs:容器" % what)
        if host_pats:
            out.setdefault(addr, []).append(("host", None, host_pats))
    return out


def source_name(src):
    kind, ctr, pats = src
    text = kind + ("-" + ctr if ctr else "") + ("-" + "+".join(pats) if pats else "")
    return re.sub(r"[^A-Za-z0-9._-]", "_", text)[:120]


# The script run on the machine (or in the container) for file sources. Input: FIRST (1 for a source's first round) and the
# state lines "<inode> <offset>"; output: "F <inode> <offset after this round> <path>" per file and "L <inode> <line>" per
# timing line. Only complete lines are taken; a line still being written is read in the next round.
FILE_SCRIPT = r'''LC_ALL=C; export LC_ALL
FIRST=@@FIRST@@; CAP=@@CAP@@
@@STATE@@
for pat in @@PATTERNS@@; do
  for f in $pat; do
    [ -f "$f" ] || continue
    set -- $(stat -c '%i %s' "$f" 2>/dev/null) || continue
    ino=$1; size=$2
    eval "off=\${o_$ino:-}"
    if [ -z "$off" ]; then if [ "$FIRST" = 1 ]; then off=$size; else off=0; fi; fi
    [ "$size" -lt "$off" ] && off=0
    end=$size; [ $((end - off)) -gt "$CAP" ] && end=$((off + CAP))
    if [ "$end" -le "$off" ]; then echo "F $ino $off $f"; continue; fi
    { tail -c +$((off + 1)) "$f" | head -c $((end - off)); printf '\n\001END\n'; } | awk -v ino="$ino" -v off="$off" -v f="$f" '
      $0 == "\001END" { print "F " ino " " (off + n) " " f; exit }
      { if (have) { n += length(prev) + 1; if (index(prev, "[BatchPutTiming]") || index(prev, "[BatchUpsertTiming]")) print "L " ino " " prev }
        prev = $0; have = 1 }'
  done
done
'''

LOGS_SCRIPT = r'''LC_ALL=C; export LC_ALL
if [ -z "@@SINCE@@" ]; then echo "T $(date -u +%Y-%m-%dT%H:%M:%SZ)"; exit 0; fi
docker logs --timestamps --since "@@SINCE@@" @@CTR@@ 2>&1 | grep -a -F -e '[BatchPutTiming]' -e '[BatchUpsertTiming]' | sed 's/^/D /'
'''


def file_script(patterns, state, first):
    lines = "\n".join("o_%d=%d" % (int(i), int(o)) for i, o in state.items())
    return (FILE_SCRIPT.replace("@@FIRST@@", "1" if first else "0").replace("@@CAP@@", str(CHUNK_CAP))
            .replace("@@STATE@@", lines).replace("@@PATTERNS@@", " ".join(shlex.quote(p) for p in patterns)))


def parse_file_output(text):
    """(offsets {inode: offset}, files {inode: path}, lines [text]) from FILE_SCRIPT's output."""
    offsets, files, lines = {}, {}, []
    for raw in text.splitlines():
        if raw.startswith("L "):
            parts = raw.split(" ", 2)
            if len(parts) == 3:
                lines.append(parts[2])
        elif raw.startswith("F "):
            parts = raw.split(" ", 3)
            if len(parts) == 4 and parts[1].isdigit() and parts[2].isdigit():
                offsets[parts[1]] = int(parts[2])
                files[parts[1]] = parts[3]
    return offsets, files, lines


def parse_logs_output(text, since):
    """(new since, lines) from LOGS_SCRIPT's output: lines newer than since, the docker timestamp removed."""
    lines, last = [], since
    for raw in text.splitlines():
        if raw.startswith("T ") and not since:
            return raw[2:].strip(), []
        if not raw.startswith("D "):
            continue
        ts, _, line = raw[2:].partition(" ")
        if since and ts <= since:
            continue
        lines.append(line)
        if ts > (last or ""):
            last = ts
    return last, lines


# ---- parsing --------------------------------------------------------------------------------------------------------

def parse_native(line):
    """One timing line as a dict (tid, year or None, month, day, us of the day, keys, bytes, phase fields, scope, status),
    or None. year is the line's own (glog 0.6); None when glog printed no year (glog 0.4)."""
    m = GLOG.match(line.rstrip("\n"))
    if not m:
        return None
    yr, mo, da, hh, mi, ss, us, tid, kind, rest = m.groups()
    kv = dict(p.split("=", 1) for p in rest.split() if "=" in p)
    try:
        out = {"tid": int(tid), "year": int(yr) if yr else None, "month": int(mo), "day": int(da),
               "us": ((int(hh) * 60 + int(mi)) * 60 + int(ss)) * 1000000 + int(us), "kind": kind,
               "keys": int(kv["keys"]), "bytes": int(kv["bytes"]), "scope": kv.get("timing_scope"), "status": kv.get("status"),
               "complete": kv.get("complete") == "true"}
    except (KeyError, ValueError):
        return None
    for _, field in PHASES:
        v = kv.get(field)
        out[field] = int(v) if v is not None and v.isdigit() else None
    return out


def local_us(wall_ns, offset_s):
    """A UTC wall time in ns as microseconds since 1970 of the node's local clock (a naive local time)."""
    return (int(wall_ns) + int(offset_s) * 1000000000) // 1000


def native_local_us(n, year):
    """The line's local time in us since 1970: in the line's own year when it has one, else in the given year (the call's)."""
    try:
        day = datetime.datetime(n.get("year") or year, n["month"], n["day"])
    except ValueError:
        return None
    return int((day - datetime.datetime(1970, 1, 1)).total_seconds()) * 1000000 + n["us"]


def save_calls(rec):
    """The calls into Mooncake's write of one pool_task[save] record: [(tid, start, end, keys, bytes)] in naive local us."""
    out = []
    for c in rec.get("native_calls") or []:
        if not isinstance(c, dict):
            continue
        op = str(c.get("operation") or "")
        if "put" not in op and "upsert" not in op:
            continue
        try:
            off = c.get("wall_utc_offset_seconds") or 0
            out.append((int(c["native_tid"]), local_us(c["start_wall_ns"], off), local_us(c["end_wall_ns"], off),
                        int(c["submitted_keys"]), int(c["submitted_bytes"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def iso_time(seconds):
    local = time.localtime(seconds)
    offset = local.tm_gmtoff if hasattr(local, "tm_gmtoff") else -time.timezone
    sign = "+" if offset >= 0 else "-"
    offset = abs(int(offset))
    ms = int((seconds - int(seconds)) * 1000)
    return "%s.%03d%s%02d:%02d" % (time.strftime("%Y-%m-%dT%H:%M:%S", local), ms, sign, offset // 3600, (offset % 3600) // 60)


# ---- association ----------------------------------------------------------------------------------------------------

class _Task:
    __slots__ = ("node", "instance", "rec", "calls", "matched", "seen")

    def __init__(self, node, instance, rec, calls, now):
        self.node, self.instance, self.rec, self.calls = node, instance, rec, calls
        self.matched = [None] * len(calls)
        self.seen = now


class Associator:
    """Pairs timing lines with the calls of pool_task[save] records, per machine folder (the mirror's <machine>), and
    writes one pd_native_put record per task whose calls all found their line. Feed it with add_task / add_native (or
    run_once over the mirror); write() gets (node, instance, line) for every record."""

    def __init__(self, write, window_s=MATCH_WINDOW_S, tid_fallback=False):
        self.write = write
        self.window_s = window_s
        self.tid_fallback = tid_fallback   # MOONCAKE_TID_FALLBACK=1: a call with no line of its thread may take another's
        self.tasks = {}        # node -> [task]
        self.natives = {}      # node -> {thread id -> [(native, arrival)]}: other TP ranks' threads never match and wait apart
        self.counts = dict.fromkeys(("native_lines", "save_records", "calls", "matched_calls", "matched_without_tid",
                                     "bytes_mismatch", "records_written", "tasks_incomplete", "lines_unmatched",
                                     "lines_unparsed"), 0)

    @staticmethod
    def _pick(fits, nbytes):
        """C10: of the lines that fit a call (machine, thread, time, keys), the one; bytes only tells several apart, and
        exactly one line must be left. Returns the index into fits or None."""
        if len(fits) == 1:
            return 0
        same = [i for i, f in enumerate(fits) if f[2]["bytes"] == nbytes]
        return same[0] if len(same) == 1 else None

    def _take(self, task, i, fits, k, by_tid, without_tid):
        other, j, n = fits[k]
        task.matched[i] = n
        self.counts["matched_calls"] += 1
        if without_tid:
            self.counts["matched_without_tid"] += 1
        if n["bytes"] != task.calls[i][4]:
            self.counts["bytes_mismatch"] += 1     # the record format note expects them equal (section 4)
        del by_tid[other][j]

    def _try(self, task, now):
        by_tid = self.natives.get(task.node) or {}
        for i, (tid, start, end, keys, nbytes) in enumerate(task.calls):
            if task.matched[i] is not None:
                continue
            year = (datetime.datetime(1970, 1, 1) + datetime.timedelta(microseconds=start)).year

            def fitting(threads):
                out = []
                for other in threads:
                    for j, (n, _) in enumerate(by_tid.get(other) or []):
                        t = native_local_us(n, year)
                        if n["keys"] == keys and t is not None and start <= t <= end:
                            out.append((other, j, n))
                return out
            own = by_tid.get(tid) or []
            if own:
                fits = fitting([tid])
                k = self._pick(fits, nbytes) if fits else None
                if k is not None:
                    self._take(task, i, fits, k, by_tid, False)
            elif self.tid_fallback:
                # no line of this thread at all (glog 0.7 prints another thread number): the lines of any thread
                fits = fitting(list(by_tid))
                k = self._pick(fits, nbytes) if fits else None
                if k is not None:
                    self._take(task, i, fits, k, by_tid, True)
        if all(m is not None for m in task.matched):
            self._emit(task)
            return True
        return False

    def _emit(self, task):
        rec = task.rec
        stages = {}
        for seg, field in PHASES:
            vals = [m[field] for m in task.matched]
            if any(v is None for v in vals):
                continue
            stages[seg] = {"duration_ns": sum(vals) * 1000}
        out = {"event": "pd_native_put", "version": 2, "request_id": rec.get("request_id"),
               "execution_id": rec.get("execution_id"), "task_id": rec.get("task_id"), "tp_rank": rec.get("tp_rank"),
               "operation": rec.get("operation"), "native_calls": len(task.calls),
               "timing_scopes": sorted({m["scope"] for m in task.matched if m["scope"]}),
               "status": "ok" if all(m["status"] == "ok" for m in task.matched) else "error",
               "stages": stages, "pool_record_id": rec.get("record_id"), "timestamp_ns": rec.get("timestamp_ns")}
        if not rec.get("request_id"):
            # a save task shared by several requests (C9): its members, so the collector can count it for each
            out["members"] = [{"request_id": m.get("request_id")} for m in rec.get("members") or []
                              if isinstance(m, dict) and m.get("request_id")]
        self.write(task.node, task.instance, json.dumps(out, separators=(",", ":")))
        self.counts["records_written"] += 1

    def add_task(self, node, instance, rec, now=None):
        """A pool_task record from <node>/<instance>/pool.*.log; only operation save with calls into Mooncake is kept."""
        now = time.monotonic() if now is None else now
        if rec.get("event") != "pool_task" or rec.get("operation") != "save":
            return
        self.counts["save_records"] += 1
        calls = save_calls(rec)
        if not calls:
            return
        self.counts["calls"] += len(calls)
        task = _Task(node, instance, rec, calls, now)
        if not self._try(task, now):
            self.tasks.setdefault(node, []).append(task)

    def add_native(self, node, line, now=None):
        now = time.monotonic() if now is None else now
        n = parse_native(line)
        if n is None:
            self.counts["lines_unparsed"] += 1
            return
        self.counts["native_lines"] += 1
        self.natives.setdefault(node, {}).setdefault(n["tid"], []).append((n, now))
        waiting = self.tasks.get(node) or []
        for task in list(waiting):
            # a call of this line's thread; with the fallback on, also a call whose thread wrote no line
            if any(m is None and (c[0] == n["tid"] or (self.tid_fallback and not self.natives[node].get(c[0])))
                   for c, m in zip(task.calls, task.matched)) and self._try(task, now):
                waiting.remove(task)

    def expire(self, now=None):
        now = time.monotonic() if now is None else now
        for node, tasks in self.tasks.items():
            keep = [t for t in tasks if now - t.seen <= self.window_s]
            self.counts["tasks_incomplete"] += len(tasks) - len(keep)
            self.tasks[node] = keep
        for by_tid in self.natives.values():
            for tid in list(by_tid):
                keep = [x for x in by_tid[tid] if now - x[1] <= self.window_s]
                self.counts["lines_unmatched"] += len(by_tid[tid]) - len(keep)
                if keep:
                    by_tid[tid] = keep
                else:
                    del by_tid[tid]


# ---- files on the monitoring machine --------------------------------------------------------------------------------

class Tail:
    """Complete new lines of files, from where the last call stopped (state: {path: [inode, offset]}). Files seen for the
    first time start at their end when start_at_end is set (the first round after collection was switched on)."""

    def __init__(self, state):
        self.state = state

    def read(self, path, start_at_end):
        try:
            st = os.stat(path)
        except OSError:
            return []
        ino, size = st.st_ino, st.st_size
        prev = self.state.get(path)
        off = prev[1] if prev and prev[0] == ino and prev[1] <= size else (size if start_at_end and not prev else 0)
        if size <= off:
            self.state[path] = [ino, off]
            return []
        with open(path, "rb") as f:
            f.seek(off)
            data = f.read(min(size - off, CHUNK_CAP))
        cut = data.rfind(b"\n")
        if cut < 0:
            self.state[path] = [ino, off]
            return []
        self.state[path] = [ino, off + cut + 1]
        return data[:cut].decode("utf-8", "replace").split("\n")

    def forget_missing(self, keep):
        for p in [p for p in self.state if p not in keep]:
            del self.state[p]


class Writer:
    """The associator's output files, one per machine and instance, in the node files' format and naming."""

    def __init__(self, root, keep_hours):
        self.root = root
        self.keep_s = keep_hours * 3600
        self.start = time.strftime("%Y%m%dT%H%M%S", time.localtime())
        self.pid = os.getpid()
        self.files = {}
        self.host = socket.gethostname()

    def __call__(self, node, instance, payload):
        key = (node, instance)
        f = self.files.get(key)
        if f is None or f["bytes"] >= OUT_ROTATE:
            f = self._open(node, instance, (f["seq"] + 1) if f else 0, f)
        line = ("%s (native pid=%d) INFO [native_put.py:0] [PD_DIAG] %s\n" % (iso_time(time.time()), self.pid, payload)).encode()
        f["fh"].write(line)
        f["bytes"] += len(line)

    def _open(self, node, instance, seq, old):
        if old is not None:
            old["fh"].close()
        d = os.path.join(self.root, node, instance)
        os.makedirs(d, exist_ok=True)
        name = "native.%s.%d.%04d.log" % (self.start, self.pid, seq)
        fh = open(os.path.join(d, name), "ab", buffering=0)
        head = {"event": "pd_file_open", "version": 1, "instance": instance, "host": self.host, "pid": self.pid, "role": "native",
                "file": name, "seq": seq, "timestamp_ns": time.monotonic_ns()}
        line = ("%s (native pid=%d) INFO [native_put.py:0] [PD_DIAG] %s\n" % (iso_time(time.time()), self.pid, json.dumps(head, separators=(",", ":")))).encode()
        fh.write(line)
        f = {"fh": fh, "bytes": len(line), "seq": seq}
        self.files[(node, instance)] = f
        return f

    def clean(self):
        cutoff = time.time() - self.keep_s
        current = {os.path.realpath(f["fh"].name) for f in self.files.values()}
        for p in glob.glob(os.path.join(self.root, "*", "*", "native.*.log")):
            try:
                if os.path.realpath(p) not in current and os.stat(p).st_mtime < cutoff:
                    os.unlink(p)
            except OSError:
                pass


# ---- the puller's part ----------------------------------------------------------------------------------------------

class Puller:
    """Fetch and association for the puller (scripts/puller.py), one thread. Disabled unless MOONCAKE_LOG_COLLECT=1."""

    def __init__(self, env, pull_dir, state_dir, folder_of, ssh_options, run):
        self.env = env
        self.pull_dir = pull_dir
        self.mirror = os.path.join(pull_dir, "_mooncake")
        self.out = os.path.join(pull_dir, "_native")
        self.state_file = os.path.join(state_dir, "native.json")
        self.folder_of = folder_of          # machine address -> its folder under PULL_DIR
        self.ssh_options = ssh_options
        self.run_cmd = run
        self.sources = parse_sources(env.get("MOONCAKE_LOG_FILES"))
        keep = float(env.get("NODE_LOG_KEEP_HOURS") or 12)
        self.keep_s = keep * 3600
        try:
            self.state = json.load(open(self.state_file))
        except (OSError, ValueError):
            self.state = {}
        self.state.setdefault("fetch", {})
        self.state.setdefault("tail", {})
        self.started = bool(self.state.get("started"))
        self.tail = Tail(self.state["tail"])
        self.writer = Writer(self.out, keep)
        self.assoc = Associator(self.writer, tid_fallback=env.get("MOONCAKE_TID_FALLBACK") == "1")
        self.errors = {}
        self.lock = threading.Lock()

    # fetch
    def _remote(self, addr, src, script):
        kind, ctr, _ = src
        if kind == "exec":
            cmd = ["ssh"] + self.ssh_options() + [addr, "docker exec -i %s sh -s" % shlex.quote(ctr)]
        else:
            cmd = ["ssh"] + self.ssh_options() + [addr, "sh -s"]
        with tempfile.TemporaryFile() as err:
            try:
                p = subprocess.run(cmd, input=script.encode(), stdout=subprocess.PIPE, stderr=err, timeout=120)
            except subprocess.TimeoutExpired:
                return 124, "", "超时"
            err.seek(0)
            return p.returncode, p.stdout.decode("utf-8", "replace"), err.read().decode("utf-8", "replace").strip()

    def fetch(self, addr):
        folder = self.folder_of(addr)
        if not folder:
            return
        for src in self.sources.get(addr, []):
            key = "%s|%s" % (addr, source_name(src))
            st = self.state["fetch"].get(key)
            first = st is None
            kind, ctr, pats = src
            if kind == "logs":
                since = (st or {}).get("since", "")
                code, out, err = self._remote(addr, src, LOGS_SCRIPT.replace("@@SINCE@@", since).replace("@@CTR@@", shlex.quote(ctr)))
                if code != 0:
                    self.errors[key] = (err or "退出码 %d" % code)[:200]
                    continue
                since, lines = parse_logs_output(out, since)
                new_state = {"since": since}
            else:
                offsets = (st or {}).get("offsets", {})
                code, out, err = self._remote(addr, src, file_script(pats, offsets, first))
                if code != 0:
                    self.errors[key] = (err or "退出码 %d" % code)[:200]
                    continue
                offsets, _, lines = parse_file_output(out)
                new_state = {"offsets": offsets}
            self.errors.pop(key, None)
            if lines:
                d = os.path.join(self.mirror, folder)
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, "%s.%s.log" % (source_name(src), time.strftime("%Y%m%d"))), "a", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
            with self.lock:
                self.state["fetch"][key] = new_state

    # associate
    def run_once(self):
        now = time.monotonic()
        seen = set()
        first = not self.started
        with self.lock:
            # timing lines first: a task that comes in the same round finds its lines already there
            for p in sorted(glob.glob(os.path.join(self.mirror, "*", "*.log"))):
                seen.add(p)
                node = os.path.basename(os.path.dirname(p))
                for line in self.tail.read(p, start_at_end=False):
                    if line:
                        self.assoc.add_native(node, line, now)
            for p in sorted(glob.glob(os.path.join(self.pull_dir, "*", "*", "pool.*.log"))
                            + glob.glob(os.path.join(self.pull_dir, "*", "d*", "*", "pool.*.log"))):
                rel = os.path.relpath(p, self.pull_dir).split(os.sep)
                if rel[0].startswith("_") or not POOL_FILE.match(rel[-1]):
                    continue
                seen.add(p)
                node, instance = rel[0], rel[-2]
                for line in self.tail.read(p, start_at_end=first):
                    i = line.find("[PD_DIAG] ")
                    if i < 0 or '"pool_task"' not in line or '"save"' not in line:
                        continue
                    try:
                        rec = json.loads(line[i + 10:])
                    except ValueError:
                        continue
                    self.assoc.add_task(node, instance, rec, now)
            self.assoc.expire(now)
            self.tail.forget_missing(seen)
            self.state["started"] = True
            self.started = True
            self._save()

    def clean(self):
        cutoff = time.time() - self.keep_s - 86400
        for p in glob.glob(os.path.join(self.mirror, "*", "*.log")):
            try:
                if os.stat(p).st_mtime < cutoff:
                    os.unlink(p)
            except OSError:
                pass
        self.writer.clean()

    def _save(self):
        tmp = self.state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.state_file)

    def status(self):
        return {"counts": dict(self.assoc.counts), "errors": dict(self.errors)}
