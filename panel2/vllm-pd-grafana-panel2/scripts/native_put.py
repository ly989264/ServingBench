#!/usr/bin/env python3
"""Mooncake's own timing of a batch write, for the three segments under P's background put: submit, wait_for_transfer
and finalize (phases 2, 3 and 4 of a call).

v3.3-extra (T58): Mooncake's client (patch 04b) writes its timing into files of its own on the node,
/data/pd-diag/logs/mooncake/mc.<start>.<pid>.<seq>.log, in the node files' line format ("[PD_DIAG] " and one JSON
object): a pd_file_open line, one mc_batch_put / mc_batch_upsert line per call, and an mc_timing_stats line a minute.
It writes them only at PD_DIAG_LEVEL 3 with MC_PD_TIMING not 0. The node folder comes back with the puller's rsync, so
these files are in the mirror at PULL_DIR/<machine>/mooncake/ (or <machine>/d<n>/mooncake/); the collector leaves them
alone (render_vector.NODE_EXCLUDE). Nothing is fetched over SSH any more, and MOONCAKE_LOG_FILES, MOONCAKE_TID_FALLBACK
and MOONCAKE_LOG_COLLECT are not read.

Association (Associator): a pool_task[save] record (written as is by the node filter into pool.<start>.<pid>.<seq>.log,
mirrored by rsync) lists its calls into Mooncake (native_calls: native_tid, start_wall_ns, end_wall_ns, submitted_keys,
submitted_bytes). A timing line belongs to a call when it comes from the same machine, the same process (the line's pid
and the pid in the pool file's name) and the same thread (native_tid), has the same submitted_keys, and its wall_ns (UTC
ns at the end of the batch) lies within the call's start_wall_ns and end_wall_ns (UTC ns, the same clock). The byte count
only tells several such lines apart, and exactly one must be left; a matched line whose submitted_bytes differ from the
call's is counted (bytes_mismatch). When every call of a task has its line, one record pd_native_put (record version 2,
unchanged from v3.2-extra) is written for the task into PULL_DIR/_native/<machine>/<instance>/native.<start>.<pid>.<seq>.log
in the node files' line format, with the phase sums as stages.<segment>.duration_ns. A task whose lines do not all arrive
within MATCH_WINDOW_S, and a line no task claims within it, are dropped and counted.

The first round starts at the end of every file already in the mirror (only what is written after the puller starts is
paired); a file seen later is read from its start.

Standard library only; Python 3.9 and later.
"""
import glob
import json
import os
import re
import socket
import threading
import time

MC_FILE = re.compile(r"^mc\.\d{8}T\d{6}\.\d+\.\d{4}\.log$")
POOL_FILE = re.compile(r"^pool\.\d{8}T\d{6}\.(\d+)\.\d{4}\.log$")
MC_EVENTS = ("mc_batch_put", "mc_batch_upsert")
PHASES = (("submit", "phase2_submit_transfers_us"), ("wait_for_transfer", "phase3_wait_transfers_us"),
          ("finalize", "phase4_finalize_batch_put_us"), ("start_batch_put", "phase1_start_batch_put_us"))
MATCH_WINDOW_S = 120.0
CHUNK_CAP = 256 * 1024 * 1024       # bytes of one file read in one round at most; the rest follows in the next rounds
OUT_ROTATE = 64 * 1024 * 1024
MARK = "[PD_DIAG] "


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def parse_mc(line):
    """One line of a Mooncake file as a dict (pid, tid, keys, bytes, wall_ns, phase fields, scope, status), or None when
    it is not a timing line (pd_file_open, mc_timing_stats, a broken line). Raises nothing."""
    i = line.find(MARK)
    if i < 0:
        return None
    try:
        e = json.loads(line[i + len(MARK):])
    except ValueError:
        return None
    if not isinstance(e, dict) or e.get("event") not in MC_EVENTS:
        return None
    out = {"pid": _int(e.get("pid")), "tid": _int(e.get("native_tid")), "keys": _int(e.get("submitted_keys")),
           "bytes": _int(e.get("submitted_bytes")), "wall_ns": _int(e.get("wall_ns")), "scope": e.get("timing_scope"),
           "status": e.get("status"), "complete": e.get("complete") is True}
    if None in (out["pid"], out["tid"], out["keys"], out["bytes"], out["wall_ns"]):
        return None
    for _, field in PHASES:
        out[field] = _int(e.get(field))
    return out


def save_calls(rec):
    """The calls into Mooncake's write of one pool_task[save] record: [(tid, start_ns, end_ns, keys, bytes)], UTC ns."""
    out = []
    for c in rec.get("native_calls") or []:
        if not isinstance(c, dict):
            continue
        op = str(c.get("operation") or "")
        if "put" not in op and "upsert" not in op:
            continue
        try:
            out.append((int(c["native_tid"]), int(c["start_wall_ns"]), int(c["end_wall_ns"]),
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
    __slots__ = ("node", "instance", "pid", "rec", "calls", "matched", "seen")

    def __init__(self, node, instance, pid, rec, calls, now):
        self.node, self.instance, self.pid, self.rec, self.calls = node, instance, pid, rec, calls
        self.matched = [None] * len(calls)
        self.seen = now


class Associator:
    """Pairs Mooncake's timing lines with the calls of pool_task[save] records, per machine folder (the mirror's
    <machine>) and process, and writes one pd_native_put record per task whose calls all found their line. Feed it with
    add_task / add_native; write() gets (node, instance, line) for every record."""

    def __init__(self, write, window_s=MATCH_WINDOW_S):
        self.write = write
        self.window_s = window_s
        self.tasks = {}        # node -> [task]
        self.natives = {}      # (node, pid, tid) -> [(native, arrival)]
        self.counts = dict.fromkeys(("native_lines", "save_records", "calls", "matched_calls", "bytes_mismatch",
                                     "records_written", "tasks_incomplete", "lines_unmatched", "lines_unparsed",
                                     "mc_files"), 0)

    @staticmethod
    def _pick(fits, nbytes):
        """Of the lines that fit a call (machine, process, thread, keys, time), the one; bytes only tells several apart,
        and exactly one line must be left. Returns the index into fits or None."""
        if len(fits) == 1:
            return 0
        same = [i for i, (_, n) in enumerate(fits) if n["bytes"] == nbytes]
        return same[0] if len(same) == 1 else None

    def _try(self, task):
        for i, (tid, start, end, keys, nbytes) in enumerate(task.calls):
            if task.matched[i] is not None:
                continue
            lines = self.natives.get((task.node, task.pid, tid)) or []
            fits = [(j, n) for j, (n, _) in enumerate(lines) if n["keys"] == keys and start <= n["wall_ns"] <= end]
            k = self._pick(fits, nbytes) if fits else None
            if k is None:
                continue
            j, n = fits[k]
            task.matched[i] = n
            self.counts["matched_calls"] += 1
            if n["bytes"] != nbytes:
                self.counts["bytes_mismatch"] += 1
            del lines[j]
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

    def add_task(self, node, instance, pid, rec, now=None):
        """A pool_task record from <node>/<instance>/pool.<start>.<pid>.<seq>.log; only save with calls into Mooncake."""
        now = time.monotonic() if now is None else now
        if rec.get("event") != "pool_task" or rec.get("operation") != "save":
            return
        self.counts["save_records"] += 1
        calls = save_calls(rec)
        if not calls:
            return
        self.counts["calls"] += len(calls)
        task = _Task(node, instance, pid, rec, calls, now)
        if not self._try(task):
            self.tasks.setdefault(node, []).append(task)

    def add_native(self, node, line, now=None):
        """A line of <node>/mooncake/mc.*.log. Lines that are not timing lines (pd_file_open, mc_timing_stats) are skipped
        quietly; a timing line that cannot be read is counted (lines_unparsed)."""
        now = time.monotonic() if now is None else now
        n = parse_mc(line)
        if n is None:
            if any('"event":"%s"' % ev in line or '"event": "%s"' % ev in line for ev in MC_EVENTS):
                self.counts["lines_unparsed"] += 1
            return
        self.counts["native_lines"] += 1
        self.natives.setdefault((node, n["pid"], n["tid"]), []).append((n, now))
        waiting = self.tasks.get(node) or []
        for task in list(waiting):
            if task.pid == n["pid"] and any(m is None and c[0] == n["tid"] for c, m in zip(task.calls, task.matched)) \
                    and self._try(task):
                waiting.remove(task)

    def expire(self, now=None):
        now = time.monotonic() if now is None else now
        for node, tasks in self.tasks.items():
            keep = [t for t in tasks if now - t.seen <= self.window_s]
            self.counts["tasks_incomplete"] += len(tasks) - len(keep)
            self.tasks[node] = keep
        for key in list(self.natives):
            keep = [x for x in self.natives[key] if now - x[1] <= self.window_s]
            self.counts["lines_unmatched"] += len(self.natives[key]) - len(keep)
            if keep:
                self.natives[key] = keep
            else:
                del self.natives[key]


# ---- files on the monitoring machine --------------------------------------------------------------------------------

class Tail:
    """Complete new lines of files, from where the last call stopped (state: {path: [inode, offset]}). Files seen for the
    first time start at their end when start_at_end is set (the first round after the puller started)."""

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

def _rel_parts(pull_dir, path):
    return os.path.relpath(path, pull_dir).split(os.sep)


class Puller:
    """Association for the puller (scripts/puller.py), one thread, every round: the mirrored Mooncake files and the
    mirrored pool files of every machine folder. Always on: with no Mooncake files in the mirror it pairs nothing."""

    def __init__(self, env, pull_dir, state_dir):
        self.env = env
        self.pull_dir = pull_dir
        self.old_mirror = os.path.join(pull_dir, "_mooncake")     # v3.2-extra's fetched lines: only cleaned up
        self.out = os.path.join(pull_dir, "_native")
        self.state_file = os.path.join(state_dir, "native.json")
        keep = float(env.get("NODE_LOG_KEEP_HOURS") or 12)
        self.keep_s = keep * 3600
        try:
            self.state = json.load(open(self.state_file))
        except (OSError, ValueError):
            self.state = {}
        self.state.pop("fetch", None)                 # v3.2-extra's fetch offsets
        self.state.setdefault("tail", {})
        self.started = bool(self.state.get("started_v33"))
        self.tail = Tail(self.state["tail"])
        self.writer = Writer(self.out, keep)
        self.assoc = Associator(self.writer)
        self.errors = {}
        self.lock = threading.Lock()

    def _files(self, pattern_tail, name_re):
        out = []
        for p in sorted(glob.glob(os.path.join(self.pull_dir, "*", *pattern_tail))
                        + glob.glob(os.path.join(self.pull_dir, "*", "d*", *pattern_tail))):
            rel = _rel_parts(self.pull_dir, p)
            if rel[0].startswith("_") or not name_re.match(rel[-1]):
                continue
            out.append((p, rel))
        return out

    def run_once(self):
        now = time.monotonic()
        seen = set()
        first = not self.started
        with self.lock:
            # timing lines first: a task that comes in the same round finds its lines already there
            mc = self._files(("mooncake", "mc.*.log"), MC_FILE)
            self.assoc.counts["mc_files"] = len(mc)
            for p, rel in mc:
                seen.add(p)
                for line in self.tail.read(p, start_at_end=first):
                    if line:
                        self.assoc.add_native(rel[0], line, now)
            for p, rel in self._files(("*", "pool.*.log"), POOL_FILE):
                if rel[-2] == "mooncake":
                    continue
                seen.add(p)
                node, instance, pid = rel[0], rel[-2], int(POOL_FILE.match(rel[-1]).group(1))
                for line in self.tail.read(p, start_at_end=first):
                    i = line.find(MARK)
                    if i < 0 or '"pool_task"' not in line or '"save"' not in line:
                        continue
                    try:
                        rec = json.loads(line[i + len(MARK):])
                    except ValueError:
                        continue
                    self.assoc.add_task(node, instance, pid, rec, now)
            self.assoc.expire(now)
            self.tail.forget_missing(seen)
            self.state["started_v33"] = True
            self.started = True
            self._save()

    def clean(self):
        cutoff = time.time() - self.keep_s - 86400
        for p in glob.glob(os.path.join(self.old_mirror, "*", "*.log")):
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
