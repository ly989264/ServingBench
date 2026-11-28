"""Logging handler for the pd032 [PD_DIAG] lines, loaded into vLLM through VLLM_LOGGING_CONFIG_PATH.

What it does in each process, driven by segments.json (the same file the monitoring machine uses):
  - only the events segments.json names are read (api_summary, core_summary, worker_summary), and only records whose
    "version" is line.record_version; any other [PD_DIAG] line is dropped and counted;
  - per-step records matched by an "accumulate" rule (event plus top-level fields such as scope or phase) are not
    written; the value at the rule's path (for example stages.forward.duration_ns) is added up per request and written
    as one cumulative line per request every few seconds (event "pd_seg_partial"); a null value adds nothing;
    v3.1-extra: the rule's value may be a list of paths, of which the first that is not null is taken (a device time,
    else the host time); the self-check line counts per rule the records whose value came from a later path
    ("acc_fallback", {rule id: count}; only when segments.json has such a rule); a list of one path (a device time
    only, no fallback) counts instead the records that have the path's parent object but no value at the path (the
    step's stage is there, its device time is not: the step adds nothing, the segment stays empty for it);
    v3.1-extra, only when segments.json has "missing_execution_id": "own_step_first_null": a matched record with no
    execution_id counts as a step of its own and makes the rule's first-step value of its requests unknown (first_ns
    null; when it is a request's first record, the request's first step is unknown: first_exec null, every first_ns
    null); the self-check line counts these records ("exec_missing"). Without the key a record with no execution_id
    takes its record_id as the step, as in v3;
  - request-level records matched by a "passthrough" rule are written unchanged to this process's own files;
  - worker lines of TP ranks other than 0 are dropped;
  - v3.1-extra, only when segments.json has a "candidate" section: a request's first-candidate step (the step that
    started its KV load: on D the read from P, on P a load from the cache pool) does not name the request, so its values
    (the Worker's submit; the EngineCore's lookup, alloc and other) are kept per execution_id for a while (hold_steps,
    hold_seconds) and added to the requests that records of the same process name for that execution_id (candidate.map:
    D's pd_transfer_summary[link] in the EngineCore, D's pd_transfer_summary[task] and P's pool_task[load_submit] in the
    Worker), whichever comes first; a request that the step's own record already names is not added again. The sums go
    into the request's pd_seg_partial line under the candidate rules' ids. Each process does this on its own, so it holds
    for TP > 1, where the Worker of rank 0 and the EngineCore are different processes. The self-check line counts the
    named pairs (cand_maps), the values added (cand_applied), steps whose named requests did not get every value before
    the step left the buffer (cand_unpaired), and steps pushed out by hold_steps before hold_seconds (cand_evicted_full);
  - every minute it writes one self-check line (event "pd_filter_selfcheck");
    T58: the line carries diag_level, the PD_DIAG_LEVEL in effect in this process (1, 2 or 3; see _diag_level);
  - when segments.json changes on disk it is read again, without restarting vLLM.
Any error is caught and counted here; nothing is raised into vLLM.

Files: <log_dir>/<instance>/<role>.<start time>.<pid>.<seq>.log, one per process and role (api, engine,
worker, pool). A full file is closed and a new name is opened; files are never renamed. Files older than
keep_hours are deleted. Each line is
  2026-11-11T09:30:01.123+08:00 (EngineCore_DP0 pid=4711) INFO [pd_diagnostics.py:104] [PD_DIAG] {...}

Instance: the name of the <instance> folder, which the monitoring machine maps to P0, D17 and so on. PD_DIAG_INSTANCE
set to a name or a port is used as it is. Not set, or set to "auto": the port of this vLLM, taken from its own command
line (--port 8001 or --port=8001), so that one set of variables serves every DP rank a launch script starts on the
machine. With no --port on the command line (none at all, or only in a --config file) it is vLLM's default 8000 and the
source is "default", which the post-deployment check reports. It is written back to PD_DIAG_INSTANCE, with its source in
PD_DIAG_INSTANCE_SOURCE, when the handler is created, before vLLM starts its EngineCore and Worker processes, so they
inherit the same instance. The file-open and self-check lines carry the source and the --data-parallel-rank of the
command line, if any.

Environment: PD_DIAG_INSTANCE (see above), PD_DIAG_MAPPING (path of segments.json, default: next to this file),
PD_DIAG_LOG_DIR (overrides line.log_dir).

Written for Python 3.9 and later; standard library only.
"""

import atexit
import collections
import hashlib
import json
import logging
import os
import re
import socket
import sys
import threading
import time

__all__ = ["PdDiagHandler", "DropPdDiag", "VllmTextFormatter"]

_MARK = "[PD_DIAG]"
_RANK_RE = re.compile(r"_TP(\d+)")
_FILE_RE = re.compile(r"^(api|engine|worker|pool)\.\d{8}T\d{6}\.\d+\.\d{4}\.log$")


def _where(spec):
    """A rule's "where" (top-level field -> value) as a hashable tuple."""
    return tuple(sorted((spec.get("where") or {}).items()))


def _where_ok(obj, where):
    for k, v in where:
        if obj.get(k) != v:
            return False
    return True


def _get_path(obj, path):
    """The value at a dotted path (a tuple of keys), or None where a level is missing or not an object."""
    for k in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj
_DEFAULT_MAPPING = os.path.join(os.path.dirname(os.path.abspath(__file__)), "segments.json")


def _iso_time(seconds):
    """Wall time with milliseconds and the local UTC offset, e.g. 2026-11-11T09:30:01.123+08:00."""
    local = time.localtime(seconds)
    offset = local.tm_gmtoff if hasattr(local, "tm_gmtoff") else -time.timezone
    sign = "+" if offset >= 0 else "-"
    offset = abs(int(offset))
    ms = int((seconds - int(seconds)) * 1000)
    return "%s.%03d%s%02d:%02d" % (time.strftime("%Y-%m-%dT%H:%M:%S", local), ms, sign, offset // 3600, (offset % 3600) // 60)


def _proc_name(record):
    return getattr(record, "vllm_process_name", None) or record.processName


def _is_pd_diag(record):
    msg = record.msg
    return isinstance(msg, str) and msg.startswith(_MARK)


def argv_value(argv, *names):
    """The value of an option in a command line ("--port 8001" or "--port=8001"; vLLM also accepts "--data_parallel_rank"),
    or None."""
    for i, a in enumerate(argv):
        if not a.startswith("-"):
            continue
        opt, eq, val = a.partition("=")
        opt = opt[:2] + opt[2:].replace("_", "-") if opt.startswith("--") else opt
        if opt in names:
            if eq:
                return val
            if i + 1 < len(argv):
                return argv[i + 1]
    return None


def argv_port(argv):
    return argv_value(argv, "--port")


def argv_dp_rank(argv):
    """--data-parallel-rank (or -dpn) of the command line as an int, or None."""
    v = argv_value(argv, "--data-parallel-rank", "-dpn")
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def resolve_instance(environ=None, argv=None):
    """(instance, source) of this process. Source "env": PD_DIAG_INSTANCE set by hand to a name or a port; "argv": the
    --port of the command line; "default": no --port, so vLLM's default 8000. Not set by hand, the result and its source
    are written back to the environment, for the processes started later."""
    environ = os.environ if environ is None else environ
    argv = sys.argv if argv is None else argv
    value = (environ.get("PD_DIAG_INSTANCE") or "").strip()
    if value and value != "auto":
        src = environ.get("PD_DIAG_INSTANCE_SOURCE")
        return value, (src if src in ("argv", "default") else "env")
    port = argv_port(argv)
    value, src = (port, "argv") if port else ("8000", "default")
    environ["PD_DIAG_INSTANCE"] = value
    environ["PD_DIAG_INSTANCE_SOURCE"] = src
    return value, src


class DropPdDiag(logging.Filter):
    """For vLLM's own console handler: keeps [PD_DIAG] records out of stdout and serving.log."""

    def filter(self, record):
        try:
            return not _is_pd_diag(record)
        except Exception:
            return True


class VllmTextFormatter(logging.Formatter):
    """vLLM 0.32's console format, without importing vLLM: same prefix, same date format, same handling of newlines."""

    def __init__(self, fmt=None, datefmt="%m-%d %H:%M:%S", style="%", log_level=None):
        if fmt is None:
            fmt = (os.environ.get("VLLM_LOGGING_PREFIX", "") + "(%(vllm_process_name)s pid=%(process)d) "
                   "%(levelname)s %(asctime)s [%(fileinfo)s:%(lineno)d] %(message)s")
        super().__init__(fmt, datefmt, style)

    def format(self, record):
        record.fileinfo = record.filename
        if not hasattr(record, "vllm_process_name"):
            record.vllm_process_name = record.processName
        msg = super().format(record)
        if record.message != "":
            parts = msg.split(record.message)
            msg = msg.replace("\n", "\r\n" + parts[0])
        return msg


class _Config:
    """The parts of segments.json the node needs, checked once when loaded."""

    def __init__(self, path):
        with open(path, "rb") as f:
            raw_bytes = f.read()
        raw = json.loads(raw_bytes.decode("utf-8"))
        if raw.get("version") != 2:
            raise ValueError("segments.json: version must be 2")
        line = raw["line"]
        self.version = "%s@%s" % (raw.get("config_id", "?"), hashlib.sha1(raw_bytes).hexdigest()[:8])
        self.log_dir = line["log_dir"]
        self.rotate_bytes = int(float(line.get("rotate_mib", 256)) * 1024 * 1024)
        self.keep_seconds = float(line.get("keep_hours", 12)) * 3600
        self.flush_s = float(line.get("flush_seconds", 5))
        self.selfcheck_s = float(line.get("selfcheck_seconds", 60))
        self.evict_s = float(line.get("idle_evict_seconds", 120))
        self.max_requests = int(line.get("max_requests", 20000))
        self.record_version = line.get("record_version")
        roles = raw["roles"]
        self.by_name = [(r["contains"], r["role"]) for r in roles["by_process_name"]]
        self.by_prefix = [(r["prefix"], r["role"], tuple(r.get("from", ())), tuple(r.get("except", ())))
                          for r in roles.get("by_event_prefix", [])]
        self.keep_ranks = frozenset(int(x) for x in roles.get("keep_tp_rank", [0]))
        self.ranked = frozenset(roles.get("ranked_roles", ["worker", "pool"]))
        self.acc = {}
        self.acc_lists = []     # v3.1-extra: ids of the rules whose value is a list of paths
        acc_rules = set()
        for a in raw.get("accumulate", []):
            if a["key"] not in ("request_id", "request_ids"):
                raise ValueError("segments.json: accumulate key must be request_id or request_ids")
            value = a.get("value", "duration_ns")
            if isinstance(value, list):
                if not value or not all(isinstance(v, str) and v for v in value):
                    raise ValueError("segments.json: accumulate value must be a path or a list of paths")
                paths = tuple(tuple(v.split(".")) for v in value)
                if a["id"] not in self.acc_lists:
                    self.acc_lists.append(a["id"])
            else:
                paths = (tuple(value.split(".")),)
            spec = (a["id"], a["role"], a["key"], _where(a), a.get("empty_key", "drop_and_count"), paths)
            self.acc.setdefault(a["event"], []).append(spec)
            acc_rules.add((a["event"], spec[3]))
        # "passthrough": a list of event names (every record of the event) or {"event", "where"} rules.
        rules = set()
        for p in raw.get("passthrough", []):
            rules.add((p, ()) if isinstance(p, str) else (p["event"], _where(p)))
        # Records a segment source or a request field reads are written too, so a mapping change cannot silently drop
        # them; a rule that is already an accumulate rule is left to the accumulation.
        stack = []
        for side in raw.get("sides", {}).values():
            for phase in ("proxy", "ttft", "tpot", "background"):
                stack.extend(side.get(phase, []))
        while stack:
            node = stack.pop()
            stack.extend(node.get("children", []))
            for src in node.get("src") or []:
                if "event" in src:
                    rules.add((src["event"], _where(src)))
        for spec in raw.get("request", {}).values():
            if "event" in spec:
                rules.add((spec["event"], _where(spec)))
        self.pass_rules = {}
        for event, where in sorted(rules - acc_rules):
            self.pass_rules.setdefault(event, []).append(where)
        # v3.1-extra: first-candidate steps (module docstring). Absent: nothing of it runs.
        cand = raw.get("candidate") or {}
        self.cand_map, self.cand_acc = {}, {}
        for m in cand.get("map", []):
            self.cand_map.setdefault(m["event"], []).append(_where(m))
        for a in cand.get("accumulate", []):
            self.cand_acc.setdefault(a["event"], []).append(
                (a["id"], a["role"], _where(a), tuple(a.get("value", "duration_ns").split("."))))
        mode = raw.get("missing_execution_id")
        if mode not in (None, "own_step_first_null"):
            raise ValueError("segments.json: missing_execution_id must be own_step_first_null")
        self.exec_strict = mode == "own_step_first_null"
        self.cand_steps = int(cand.get("hold_steps", 8192))
        self.cand_seconds = float(cand.get("hold_seconds", 120))
        self.events = frozenset(self.acc) | frozenset(self.pass_rules) | frozenset(self.cand_map) | frozenset(self.cand_acc)


class _Acc:
    __slots__ = ("total", "steps", "first", "last_exec")

    def __init__(self):
        self.total = 0
        self.steps = 0
        self.first = 0
        self.last_exec = None


class _Req:
    __slots__ = ("role", "rid", "part", "seq", "acc", "dirty", "seen", "first_mono", "last_mono", "tp_rank", "proc",
                 "first_exec", "first_unknown")

    def __init__(self, role, rid, part, tp_rank, proc):
        self.role, self.rid, self.part, self.tp_rank, self.proc = role, rid, part, tp_rank, proc
        # The request's first step in this role: the first execution_id of any accumulated record that names it,
        # whether or not that record has a value. "first" sums are taken at this step only.
        self.first_exec = None
        self.first_unknown = False      # v3.1-extra, missing_execution_id: the first record had no execution_id
        self.seq = 0
        self.acc = {}
        self.dirty = False
        self.seen = time.monotonic()
        self.first_mono = None
        self.last_mono = None


class _File:
    __slots__ = ("role", "path", "fh", "bytes", "seq", "proc")


def _diag_level():
    """T58: the PD_DIAG_LEVEL in effect in this process, for the self-check line. The patches' own pd_diagnostics.diag_level()
    (a (level, source) pair) when that module is already loaded (nothing is imported here), else the same rule on the
    environment: exactly "1", "2" or "3" is that level; unset, empty or anything else (" 2 ", "2x", "0") is 3. Never raises."""
    try:
        mod = sys.modules.get("vllm.v1.metrics.pd_diagnostics")
        fn = getattr(mod, "diag_level", None) if mod is not None else None
        if fn is not None:
            v = fn()
            v = getattr(v, "level", v)
            if isinstance(v, int) and not isinstance(v, bool) and v in (1, 2, 3):
                return v
    except Exception:
        pass
    v = os.environ.get("PD_DIAG_LEVEL")
    return int(v) if v in ("1", "2", "3") else 3


class PdDiagHandler(logging.Handler):
    """Writes this process's [PD_DIAG] lines as described in the module docstring."""

    def __init__(self, mapping=None, log_dir=None, level=logging.INFO):
        super().__init__(level)
        # Our own lock around all state: pool lines arrive from a background thread of the Worker process while runner
        # lines arrive from its main thread, and the flush thread reads the same state. It does not rely on the
        # logging handler lock, so it holds however the handler is called.
        self._mu = threading.RLock()
        try:
            # Here and not at the first line: vLLM starts its EngineCore before this process writes any [PD_DIAG] line.
            resolve_instance()
        except Exception:
            pass
        self._mapping = mapping or os.environ.get("PD_DIAG_MAPPING") or _DEFAULT_MAPPING
        self._log_dir_arg = log_dir
        self._pid = None
        self._counts = {}
        self._fallback = None       # v3.1-extra: {accumulate rule id: records whose value came from a later path}
        self._cfg = None
        self._cfg_mtime = None
        self._cfg_errors = 0
        self._last_cfg_error = ""
        try:
            self._load_config(first=True)
        except Exception as exc:
            self._cfg_errors += 1
            self._last_cfg_error = "%s: %s" % (type(exc).__name__, exc)
        atexit.register(self._at_exit)
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(before=self.flush, after_in_child=self._reinit_lock)

    def _reinit_lock(self):
        # A forked child gets a copy of the lock in whatever state another thread left it; start with a fresh one.
        self._mu = threading.RLock()

    # ---- process state -------------------------------------------------------------------------------------------

    def _init_process(self):
        # After a fork the parent's open files stay referenced here and are never flushed or closed by the child,
        # so the child cannot write the parent's buffered bytes a second time.
        if getattr(self, "_files", None):
            self._inherited = getattr(self, "_inherited", []) + list(self._files.values())
        self._pid = os.getpid()
        self._run = "%x%x" % (int(time.time() * 1000), os.getpid())
        self._start = time.strftime("%Y%m%dT%H%M%S", time.localtime())
        self._host = socket.gethostname()
        self._instance, self._instance_source = resolve_instance()
        self._dp_rank = argv_dp_rank(sys.argv)
        self._files = {}
        self._reqs = {}
        self._line_no = 0
        self._counts = dict.fromkeys(
            ("seen", "written", "dropped", "no_event", "rank_dropped", "accumulated", "empty_key_dropped", "errors",
             "partials_written", "evicted", "forced", "config_reloads", "version_dropped"), 0)
        if self._cfg is not None and (self._cfg.cand_map or self._cfg.cand_acc):
            self._counts.update(cand_maps=0, cand_applied=0, cand_unpaired=0, cand_evicted_full=0)
        self._fallback = dict.fromkeys(self._cfg.acc_lists, 0) if self._cfg is not None and self._cfg.acc_lists else None
        if self._cfg is not None and self._cfg.exec_strict:
            self._counts["exec_missing"] = 0
        self._cand = collections.OrderedDict()
        now = time.monotonic()
        self._next_flush = now
        self._next_check = now + (self._cfg.selfcheck_s if self._cfg else 60)
        self._next_stat = now + 2
        self._next_clean = now + 60
        self._thread = threading.Thread(target=self._ticker, name="pd-diag-flush", daemon=True)
        self._thread.start()

    def _load_config(self, first=False):
        mtime = os.stat(self._mapping).st_mtime_ns
        cfg = _Config(self._mapping)
        self._cfg_mtime = mtime
        if not first and self._pid is not None:
            # Close the running sums under the old version before switching.
            self._flush_partials(force_all=True, reason="config_change")
            self._reqs = {}
            self._cand = collections.OrderedDict()
            self._counts["config_reloads"] += 1
            if cfg.cand_map or cfg.cand_acc:
                for k in ("cand_maps", "cand_applied", "cand_unpaired", "cand_evicted_full"):
                    self._counts.setdefault(k, 0)
            if cfg.exec_strict:
                self._counts.setdefault("exec_missing", 0)
            if cfg.acc_lists:
                if self._fallback is None:
                    self._fallback = {}
                for k in cfg.acc_lists:
                    self._fallback.setdefault(k, 0)
        self._cfg = cfg

    def _log_dir(self):
        return self._log_dir_arg or os.environ.get("PD_DIAG_LOG_DIR") or self._cfg.log_dir

    # ---- logging entry point -------------------------------------------------------------------------------------

    def emit(self, record):
        try:
            if not _is_pd_diag(record):
                return
            with self._mu:
                if self._pid != os.getpid():
                    self._init_process()
                if self._cfg is None:
                    return
                self._handle(record)
        except Exception:
            try:
                self._counts["errors"] = self._counts.get("errors", 0) + 1
            except Exception:
                pass

    def handleError(self, record):  # never print tracebacks into vLLM's stderr
        pass

    def _handle(self, record):
        c = self._counts
        c["seen"] += 1
        cfg = self._cfg
        if record.args and len(record.args) == 1 and record.msg == "[PD_DIAG] %s":
            payload = record.args[0]
        else:
            payload = record.getMessage()[len(_MARK):].lstrip()
        if not isinstance(payload, str):
            payload = str(payload)
        event = _field_str(payload, "event")
        if not event:
            c["no_event"] += 1      # not a [PD_DIAG] record (or a damaged one): dropped
            return
        if event not in cfg.events:
            c["dropped"] += 1       # an event segments.json does not read
            return
        proc = _proc_name(record)
        role = self._role(proc, event)
        tp_rank = None
        if role in cfg.ranked:
            tp_rank = _field_int(payload, "tp_rank")
            if tp_rank is None:
                m = _RANK_RE.search(proc)
                tp_rank = int(m.group(1)) if m else 0
            if tp_rank not in cfg.keep_ranks:
                c["rank_dropped"] += 1
                return
        obj = json.loads(payload)
        if cfg.record_version is not None and obj.get("version") != cfg.record_version:
            c["version_dropped"] += 1
            return
        if event in cfg.cand_acc or event in cfg.cand_map:
            self._candidate(cfg, event, obj, role, tp_rank, proc)
        specs = cfg.acc.get(event)
        if specs and self._accumulate_obj(specs, obj, role, tp_rank, proc):
            c["accumulated"] += 1
            return
        for where in cfg.pass_rules.get(event, ()):
            if _where_ok(obj, where):
                self._write(role, proc, record, payload)
                c["written"] += 1
                return
        c["dropped"] += 1

    def _role(self, proc, event):
        cfg = self._cfg
        role = None
        for needle, r in cfg.by_name:
            if needle in proc:
                role = r
                break
        for prefix, r, from_roles, excepted in cfg.by_prefix:
            if event.startswith(prefix) and event not in excepted and (not from_roles or role in from_roles):
                return r
        return role or "api"

    # ---- accumulation ---------------------------------------------------------------------------------------------

    def _accumulate_obj(self, specs, obj, role, tp_rank, proc):
        """Adds the record's values to its requests. True when an accumulate rule matched the record, which is then
        not written, also when its value is null or it names no request."""
        strict = self._cfg.exec_strict
        if strict:
            exe = obj.get("execution_id")
            if not (isinstance(exe, str) and exe):
                exe = None
        else:
            exe = obj.get("execution_id") or obj.get("record_id")
        mono = obj.get("timestamp_ns")
        matched = False
        for acc_id, acc_role, key, where, empty, paths in specs:
            if not _where_ok(obj, where):
                continue
            if strict and exe is None and not matched:
                self._counts["exec_missing"] += 1       # once per record
            matched = True
            value = _get_path(obj, paths[0])
            fell_back = False
            left_empty = (value is None and len(paths) == 1 and len(paths[0]) > 1
                          and isinstance(_get_path(obj, paths[0][:-1]), dict))
            if value is None and len(paths) > 1:
                # a list of paths: the first that is not null; a later one is counted below once the record counts
                for p in paths[1:]:
                    value = _get_path(obj, p)
                    if value is not None:
                        fell_back = True
                        break
            has = isinstance(value, int) and not isinstance(value, bool)
            if key == "request_ids":
                ids = obj.get("request_ids")
                if ids is not None and not isinstance(ids, list):
                    self._counts["errors"] += 1
                    continue
                if not ids:
                    if obj.get("request_id"):
                        ids = [obj["request_id"]]
                    else:
                        self._counts["empty_key_dropped"] += 1
                        continue
            else:
                rid = obj.get("request_id")
                if not rid:
                    self._counts["empty_key_dropped"] += 1
                    continue
                ids = (rid,)
            if ((fell_back and has) or left_empty) and self._fallback is not None:
                self._fallback[acc_id] = self._fallback.get(acc_id, 0) + 1
            now = time.monotonic()
            for rid in ids:
                st = self._reqs.get((role, rid))
                if st is None:
                    if len(self._reqs) >= self._cfg.max_requests:
                        self._force_oldest()
                    st = _Req(role, rid, "%s-%d-%d" % (self._run, os.getpid(), time.monotonic_ns()), tp_rank, proc)
                    self._reqs[(role, rid)] = st
                if strict:
                    if st.first_exec is None and not st.first_unknown:
                        if exe is None:
                            st.first_unknown = True     # the first step is not in the log: every first value unknown
                        else:
                            st.first_exec = exe
                elif st.first_exec is None:
                    st.first_exec = exe
                st.seen = now
                if not has:
                    continue
                a = st.acc.get(acc_id)
                if a is None:
                    a = st.acc[acc_id] = _Acc()
                if strict and exe is None:
                    a.steps += 1            # a step of its own
                    a.first = None          # which step it was is not in the log: the first-step value is unknown
                elif exe != a.last_exec:
                    a.steps += 1
                    a.last_exec = exe
                a.total += value
                if strict and st.first_unknown:
                    a.first = None
                elif a.first is not None and exe == st.first_exec:
                    a.first += value
                st.dirty = True
                if isinstance(mono, int):
                    if st.first_mono is None:
                        st.first_mono = mono
                    st.last_mono = mono
        return matched

    # ---- first-candidate steps (v3.1-extra) -------------------------------------------------------------------------

    def _candidate(self, cfg, event, obj, role, tp_rank, proc):
        """Keeps a step's candidate values and the requests named for its execution_id; adds each value to each request
        once. Only reads the record: what happens to it afterwards (accumulated, written, dropped) is as before."""
        exe = obj.get("execution_id")
        if not isinstance(exe, str) or not exe:
            return
        now = time.monotonic()
        ent = self._cand.get(exe)
        changed = False
        for acc_id, acc_role, where, path in cfg.cand_acc.get(event, ()):
            if not _where_ok(obj, where):
                continue
            value = _get_path(obj, path)
            if not isinstance(value, int) or isinstance(value, bool):
                continue
            if ent is None:
                ent = self._cand_entry(exe, now)
            ids = obj.get("request_ids")
            ent["vals"][(role, acc_id)] = (value, frozenset(ids) if isinstance(ids, list) else frozenset(), tp_rank, proc)
            changed = True
        for where in cfg.cand_map.get(event, ()):
            rid = obj.get("request_id")
            if _where_ok(obj, where) and isinstance(rid, str) and rid:
                if ent is None:
                    ent = self._cand_entry(exe, now)
                if rid not in ent["rids"]:
                    ent["rids"].add(rid)
                    self._counts["cand_maps"] += 1
                    changed = True
                break
        if changed:
            self._cand_apply(ent)

    def _cand_entry(self, exe, now):
        cfg = self._cfg
        while self._cand:
            k, old = next(iter(self._cand.items()))
            if len(self._cand) < cfg.cand_steps and now - old["t"] <= cfg.cand_seconds:
                break
            self._cand.popitem(last=False)
            if now - old["t"] <= cfg.cand_seconds:
                self._counts["cand_evicted_full"] += 1     # pushed out by hold_steps before hold_seconds ran out
            # requests were named for this step but not every value reached them (a value never came in this process)
            if old["rids"] and (not old["vals"] or len(old["done"]) < len(old["rids"]) * len(old["vals"])):
                self._counts["cand_unpaired"] += 1
        ent = {"t": now, "vals": {}, "rids": set(), "done": set()}
        self._cand[exe] = ent
        return ent

    def _cand_apply(self, ent):
        for rid in ent["rids"]:
            for (role, acc_id), (value, ids, tp_rank, proc) in ent["vals"].items():
                key = (rid, role, acc_id)
                if key in ent["done"]:
                    continue
                ent["done"].add(key)
                if rid in ids:
                    continue        # the step's own record names the request: its sums already have this step
                st = self._reqs.get((role, rid))
                if st is None:
                    if len(self._reqs) >= self._cfg.max_requests:
                        self._force_oldest()
                    st = _Req(role, rid, "%s-%d-%d" % (self._run, os.getpid(), time.monotonic_ns()), tp_rank, proc)
                    self._reqs[(role, rid)] = st
                st.seen = time.monotonic()
                a = st.acc.get(acc_id)
                if a is None:
                    a = st.acc[acc_id] = _Acc()
                a.steps += 1
                a.total += value
                if self._cfg.exec_strict and st.first_unknown:
                    a.first = None
                st.dirty = True
                self._counts["cand_applied"] += 1

    def _force_oldest(self):
        oldest = min(self._reqs.values(), key=lambda s: s.seen)
        if oldest.dirty:
            self._write_partial(oldest, "forced")
        del self._reqs[(oldest.role, oldest.rid)]
        self._counts["forced"] += 1

    def _flush_partials(self, force_all=False, reason="periodic"):
        now = time.monotonic()
        gone = []
        for k, st in self._reqs.items():
            if st.dirty:
                self._write_partial(st, reason)
            if force_all or now - st.seen > self._cfg.evict_s:
                gone.append(k)
        for k in gone:
            del self._reqs[k]
        self._counts["evicted"] += len(gone)

    def _write_partial(self, st, reason):
        segs = {}
        for acc_id, a in st.acc.items():
            segs[acc_id] = {"ns": a.total, "steps": a.steps, "first_ns": a.first}
        st.seq += 1
        row = {
            "event": "pd_seg_partial", "version": 1, "config_version": self._cfg.version,
            "instance": self._instance, "host": self._host, "pid": os.getpid(), "role": st.role,
            "tp_rank": st.tp_rank, "request_id": st.rid, "part": st.part, "seq": st.seq, "reason": reason,
            "first_mono_ns": st.first_mono, "last_mono_ns": st.last_mono, "first_exec": st.first_exec, "segs": segs,
            "timestamp_ns": time.monotonic_ns(), "record_id": "seg-%s-%d" % (st.part, st.seq),
        }
        self._write_raw(st.role, st.proc, json.dumps(row, separators=(",", ":")))
        st.dirty = False
        self._counts["partials_written"] += 1

    # ---- files ----------------------------------------------------------------------------------------------------

    def _write(self, role, proc, record, payload):
        line = "%s (%s pid=%d) %s [%s:%d] [PD_DIAG] %s\n" % (
            _iso_time(record.created), proc, record.process or os.getpid(), record.levelname,
            record.filename, record.lineno, payload)
        self._put(role, proc, line)

    def _write_raw(self, role, proc, payload):
        line = "%s (%s pid=%d) INFO [pd_diag_logfilter.py:0] [PD_DIAG] %s\n" % (
            _iso_time(time.time()), proc, os.getpid(), payload)
        self._put(role, proc, line)

    def _put(self, role, proc, line):
        f = self._files.get(role)
        if f is None or f.bytes >= self._cfg.rotate_bytes:
            f = self._open(role, proc, (f.seq + 1) if f else 0, f)
        data = line.encode("utf-8", "replace")
        f.fh.write(data)
        f.bytes += len(data)

    def _open(self, role, proc, seq, old):
        if old is not None:
            try:
                old.fh.close()
            except Exception:
                pass
        d = os.path.join(self._log_dir(), self._instance)
        os.makedirs(d, exist_ok=True)
        f = _File()
        f.role, f.seq, f.proc = role, seq, proc
        f.path = os.path.join(d, "%s.%s.%d.%04d.log" % (role, self._start, os.getpid(), seq))
        f.fh = open(f.path, "ab", buffering=1024 * 1024)
        f.bytes = f.fh.tell()
        self._files[role] = f
        # The first line names the file, so no two files start with the same line (the collector identifies files by it).
        head = {"event": "pd_file_open", "version": 1, "instance": self._instance, "host": self._host,
                "instance_source": self._instance_source, "dp_rank_argv": self._dp_rank,
                "pid": os.getpid(), "role": role, "file": os.path.basename(f.path), "seq": seq,
                "config_version": self._cfg.version, "timestamp_ns": time.monotonic_ns()}
        data = ("%s (%s pid=%d) INFO [pd_diag_logfilter.py:0] [PD_DIAG] %s\n" % (
            _iso_time(time.time()), proc, os.getpid(), json.dumps(head, separators=(",", ":")))).encode("utf-8")
        f.fh.write(data)
        f.bytes += len(data)
        return f

    def _clean_old(self):
        d = os.path.join(self._log_dir(), self._instance)
        cutoff = time.time() - self._cfg.keep_seconds
        current = {f.path for f in self._files.values()}
        try:
            names = os.listdir(d)
        except OSError:
            return
        for name in names:
            p = os.path.join(d, name)
            if p in current or not _FILE_RE.match(name):
                continue
            try:
                if os.stat(p).st_mtime < cutoff:
                    os.unlink(p)
            except OSError:
                pass

    def _selfcheck(self):
        row = dict(self._counts)
        if self._fallback is not None:
            row["acc_fallback"] = dict(self._fallback)
        row.update(event="pd_filter_selfcheck", version=1, config_version=self._cfg.version if self._cfg else None,
                   config_errors=self._cfg_errors, last_config_error=self._last_cfg_error or None,
                   instance=self._instance, host=self._host, pid=os.getpid(), inflight=len(self._reqs),
                   instance_source=self._instance_source, dp_rank_argv=self._dp_rank,
                   files=sorted(os.path.basename(f.path) for f in self._files.values()),
                   python=sys.version.split()[0], diag_level=_diag_level(), timestamp_ns=time.monotonic_ns())
        payload = json.dumps(row, separators=(",", ":"))
        for role, f in list(self._files.items()):
            self._write_raw(role, f.proc, payload)

    # ---- timer ----------------------------------------------------------------------------------------------------

    def _ticker(self):
        me = os.getpid()
        while os.getpid() == me:
            time.sleep(1.0)
            try:
                with self._mu:
                    if self._pid == me:
                        self._tick()
            except Exception:
                try:
                    self._counts["errors"] += 1
                except Exception:
                    pass

    def _tick(self):
        now = time.monotonic()
        if now >= self._next_stat:
            self._next_stat = now + 2
            try:
                mtime = os.stat(self._mapping).st_mtime_ns
                if mtime != self._cfg_mtime:
                    try:
                        self._load_config()
                    except Exception as exc:
                        self._cfg_mtime = mtime   # do not retry until the file changes again
                        self._cfg_errors += 1
                        self._last_cfg_error = "%s: %s" % (type(exc).__name__, exc)
            except OSError:
                pass
        if self._cfg is None:
            return
        if now >= self._next_flush:
            self._next_flush = now + self._cfg.flush_s
            self._flush_partials()
        if now >= self._next_check:
            self._next_check = now + self._cfg.selfcheck_s
            self._selfcheck()
        if now >= self._next_clean:
            self._next_clean = now + 600
            self._clean_old()
        for f in self._files.values():
            f.fh.flush()

    def flush(self):
        try:
            with self._mu:
                if self._pid == os.getpid():
                    for f in getattr(self, "_files", {}).values():
                        f.fh.flush()
        except Exception:
            pass

    def _at_exit(self):
        try:
            with self._mu:
                if self._pid == os.getpid() and self._cfg is not None:
                    self._flush_partials(force_all=True, reason="exit")
                    self._selfcheck()
                    for f in self._files.values():
                        f.fh.flush()
        except Exception:
            pass

    def close(self):
        self._at_exit()
        super().close()


def _field_str(payload, name):
    """The string value of a top-level key, read without parsing the whole line (keys are written without spaces).
    emit() adds "event" after all other fields, so the last occurrence is the top-level one."""
    i = payload.rfind('"%s":"' % name)
    if i < 0:
        return ""
    i += len(name) + 4
    j = payload.find('"', i)
    return payload[i:j] if j > i else ""


def _field_int(payload, name):
    i = payload.find('"%s":' % name)
    if i < 0:
        return None
    i += len(name) + 3
    j = i
    while j < len(payload) and payload[j] in "-0123456789":
        j += 1
    try:
        return int(payload[i:j])
    except ValueError:
        return None
