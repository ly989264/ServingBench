#!/usr/bin/env python3
"""T51 glm5.3 environment check.

Read-only. Standard library only. Never imports vllm, vllm_ascend, torch or
mooncake, never starts a device, never writes anywhere except the detail file
it prints at the end. Compatible with Python 3.9 and later.

Run it with the same python3 that runs vLLM, once per container:

    python3 t51_glm53_check.py \
        --run-dp-template run_dp_template.sh --launch-online-dp launch_online_dp.py \
        --start-script start_p.sh --mooncake-json mooncake.json \
        --start-mooncake start_mooncake.sh [--startup-log vllm.log]

On the proxy host:  python3 t51_glm53_check.py --proxy-file load_balance_proxy_server_example.py
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # never write .pyc anywhere, the standard library included

import argparse
import ast
import base64
import gzip
import hashlib
import importlib.machinery
import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import shlex
import shutil
import socket
import struct
import sysconfig
import time

TOOL_VERSION = "glm53-8"
EXPECTED_B64 = None  # filled in by build.py; otherwise t51_expected.json next to this file is used
MAX_SUMMARY_LINES = 20
MAX_LOG_BYTES = 64 * 1024 * 1024  # vLLM log: only the first 64 MiB are read
MAX_TEXT_BYTES = 16 * 1024 * 1024  # any text file (.py, launch files, metadata): at most 16 MiB
MAX_BINARY_BYTES = 1024 * 1024 * 1024  # .so and other binaries: skipped above 1 GiB, read in 1 MiB blocks
MAX_DYNSTR_BYTES = 64 * 1024 * 1024
MAX_LIBS_SCANNED = 3000
DEFAULT_MAX_SECONDS = 300
DEFAULT_MAX_READ_MIB = 1024  # total bytes read; above it the remaining Mooncake binaries are not hashed

# bytes read through this script's helpers; binaries skipped once the budget is used up
_IO = {"read": 0, "budget": None, "skipped": 0}
# time spent reading /proc/<pid>/maps and /proc/<pid>/environ
_PROC_COST = {"maps_reads": 0, "maps_s": 0.0, "environ_reads": 0, "environ_s": 0.0}
_TIMES = {}
REQUIRED_FLAGS = (("-I", "isolated"), ("-S", "no_site"), ("-B", "dont_write_bytecode"))


class Timeout(Exception):
    """The overall time limit was reached; the report is printed with what was collected so far."""


_DEADLINE = [None]


def check_deadline():
    if _DEADLINE[0] is not None and time.monotonic() > _DEADLINE[0]:
        raise Timeout()

# --------------------------------------------------------------------------- hashing

_NL = re.compile(r"\r\n|\r|\n")


def sha16(data):
    return hashlib.sha256(data).hexdigest()[:16]


def file_sha16(path, cap=MAX_BINARY_BYTES):
    """sha256 (first 16 hex) in 1 MiB blocks; files above cap are not read ("too_big")."""
    try:
        if os.path.getsize(path) > cap:
            return "too_big"
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                _IO["read"] += len(chunk)
                h.update(chunk)
        return h.hexdigest()[:16]
    except OSError:
        return None


def _text_hash(lines):
    return hashlib.sha256("\n".join(x.rstrip() for x in lines).encode("utf-8", "replace")).hexdigest()[:12]


def func_hashes(source):
    """Hash every top-level function, class body and method by its source text.

    The node is located with ast; the hash covers its decorators and source
    lines (trailing blanks stripped), so it does not depend on the Python
    version's ast.dump format. Returns None when the file does not parse.
    """
    text = source.decode("utf-8", "replace")
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    lines = _NL.split(text)
    out = {}

    def span(node):
        start = node.lineno
        for d in getattr(node, "decorator_list", ()):
            start = min(start, d.lineno)
        return start, node.end_lineno

    def visit(body, prefix, own_lines):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                a, b = span(node)
                for i in range(a, b + 1):
                    own_lines.discard(i)
                name = prefix + node.name
                if isinstance(node, ast.ClassDef):
                    cls_lines = set(range(a, b + 1))
                    visit(node.body, name + ".", cls_lines)
                    out[name + ".<class>"] = _text_hash([lines[i - 1] for i in sorted(cls_lines)])
                else:
                    out[name] = _text_hash(lines[a - 1 : b])

    module_lines = set(range(1, len(lines) + 1))
    visit(tree.body, "", module_lines)
    out["<module>"] = _text_hash([lines[i - 1] for i in sorted(module_lines)])
    return out


# --------------------------------------------------------------------------- redaction

IPV4 = re.compile(r"(?<![\w.])(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![\w.])")
# full form, or a compressed form with groups on both sides; "[" / "]" excluded so Python slices like
# x[1::2] are not taken for addresses
IPV6 = re.compile(
    r"(?<![\w:\[])(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{1,4}(?![\w:\]])"
    r"|(?<![\w:\[])(?:[0-9A-Fa-f]{1,4}:){1,6}:(?:[0-9A-Fa-f]{1,4}:){0,5}[0-9A-Fa-f]{1,4}(?![\w:\]])"
)
DATA_DIR = re.compile(r"/data/[^/\s\"']+")
STAFF_ID = re.compile(r"(?<![\w])[a-z]\d{8}(?![\w])")
LOOPBACK = {"0.0.0.0", "127.0.0.1"}
HOME = re.compile(r"/home/[^/\s\"']+")
SECRET_NAME = re.compile(r"PASS|SECRET|TOKEN|KEY|AUTH|CREDENTIAL", re.I)
COMMON_WORDS = {"false", "true", "none", "null", "enable", "enabled", "disable", "disabled", "default"}


def is_address_key(key):
    k = key.upper()
    return k in ADDRESS_ENV or k.endswith("_IP") or k == "IP" or "ADDR" in k or "HOST" in k


class Redactor:
    """Collects values that must never be printed, and checks text for them."""

    def __init__(self):
        self.hosts = set()
        self.secrets = set()
        self.placeholders = {}

    def add_host(self, value):
        if not value:
            return
        for part in re.split(r"[\s,;]+", str(value)):
            part = part.strip().strip("[]")
            m = re.match(r"^(?:[a-z][a-z0-9+.-]*://)?([^/:]+)", part, re.I)
            host = m.group(1) if m else part
            if len(host) >= 3 and host.lower() not in ("localhost", "0.0.0.0", "true", "false", "none", "null"):
                if not host.replace(".", "").isdigit() or IPV4.fullmatch(host):
                    self.hosts.add(host)

    def add_secret(self, value):
        if value and len(value) >= 6 and value.lower() not in COMMON_WORDS:
            self.secrets.add(value)

    def placeholder(self, value):
        """Stable placeholder for an address-like value (detail file only)."""
        if value is None:
            return None
        key = str(value)
        if key not in self.placeholders:
            self.placeholders[key] = "<ADDR-%d>" % (len(self.placeholders) + 1)
        return self.placeholders[key]

    @staticmethod
    def scrub_path(path):
        if path is None:
            return None
        return HOME.sub("/home/<USER>", str(path))

    def hits(self, text, allow_loopback=False):
        found = []
        if any(not (allow_loopback and m.group(0) in LOOPBACK) for m in IPV4.finditer(text)):
            found.append("ipv4")
        if IPV6.search(text):
            found.append("ipv6")
        if HOME.search(text.replace("/home/<USER>", "")):
            found.append("home")
        low = text.lower()
        for host in self.hosts:
            if re.search(r"(?<![\w.-])" + re.escape(host.lower()) + r"(?![\w-])", low):
                found.append("host")
                break
        for secret in self.secrets:
            if secret in text:
                found.append("secret")
                break
        return found


def redact_text(text, redact):
    """Replace addresses, hostnames, secrets and personal paths with placeholders; return (text, count)."""
    count = [0]

    def ph(m):
        value = m.group(0)
        if value in LOOPBACK:
            return value
        count[0] += 1
        return redact.placeholder(value)

    text = IPV4.sub(ph, text)
    text = IPV6.sub(ph, text)
    for host in sorted(redact.hosts, key=len, reverse=True):
        pat = re.compile(r"(?<![\w.-])" + re.escape(host) + r"(?![\w-])", re.I)
        text, n = pat.subn("<HOST>", text)
        count[0] += n
    for secret in redact.secrets:
        n = text.count(secret)
        if n:
            text = text.replace(secret, "<SECRET>")
            count[0] += n
    for rx, rep_ in ((HOME, "/home/<USER>"), (DATA_DIR, "/data/<DIR>"), (STAFF_ID, "<ID>")):
        count[0] += sum(1 for m in rx.findall(text) if m != rep_)
        text = rx.sub(rep_, text)
    return text, count[0]


def safe_ver(value):
    """Versions that look like an IPv4 address would trip the self-check."""
    if value is None:
        return "none"
    value = str(value)
    if IPV4.search(value):
        value = value.replace(".", "_")
    return value.replace(" ", "_")


# --------------------------------------------------------------------------- small helpers


def read_bytes(path, limit=MAX_TEXT_BYTES):
    """Read at most limit bytes (16 MiB by default); only used for text-sized files."""
    try:
        with open(path, "rb") as f:
            data = f.read(limit)
        _IO["read"] += len(data)
        return data
    except OSError:
        return None


def hash_scan_binary(path, patterns, cap=MAX_BINARY_BYTES, block=1 << 20, overlap=4096):
    """One pass over a binary: sha256 (first 16 hex) and the byte patterns, in 1 MiB blocks.

    Not read at all when it is above cap ("too_big") or would take the total read over the budget ("skipped")."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None, None
    if size > cap:
        return "too_big", None
    if _IO["budget"] is not None and _IO["read"] + size > _IO["budget"]:
        _IO["skipped"] += 1
        return "skipped", None
    found = {k: set() for k in patterns}
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            tail = b""
            while True:
                check_deadline()
                chunk = f.read(block)
                if not chunk:
                    break
                _IO["read"] += len(chunk)
                h.update(chunk)
                data = tail + chunk
                for k, rx in patterns.items():
                    for m in rx.finditer(data):
                        found[k].add(m.groups() if m.groups() else m.group(0))
                tail = data[-overlap:]
    except OSError:
        return None, None
    return h.hexdigest()[:16], found


def budgeted_sha16(path):
    """file_sha16 for a binary, within the total read budget."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if _IO["budget"] is not None and size <= MAX_BINARY_BYTES and _IO["read"] + size > _IO["budget"]:
        _IO["skipped"] += 1
        return "skipped"
    return file_sha16(path)


def scan_binary(path, patterns, cap=MAX_BINARY_BYTES, block=1 << 20, overlap=4096):
    """Search a binary for byte patterns in 1 MiB blocks; files above cap are not read. {key: set(matches)}."""
    try:
        if os.path.getsize(path) > cap:
            return None
        found = {k: set() for k in patterns}
        with open(path, "rb") as f:
            tail = b""
            while True:
                check_deadline()
                chunk = f.read(block)
                if not chunk:
                    break
                _IO["read"] += len(chunk)
                data = tail + chunk
                for k, rx in patterns.items():
                    for m in rx.finditer(data):
                        found[k].add(m.groups() if m.groups() else m.group(0))
                tail = data[-overlap:]
        return found
    except OSError:
        return None


def read_text(path, limit=None):
    data = read_bytes(path, limit)
    return None if data is None else data.decode("utf-8", "replace")


_DISTS = {}


def _norm_name(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def find_dist(name):
    """The installed distribution called name, from the dist-info directories in the site dirs (no code runs)."""
    if not _DISTS:
        _DISTS["_loaded"] = True
        try:
            for dist in importlib.metadata.distributions(path=list(PROC_SITE.get("meta_paths", []))):
                try:
                    key = _norm_name(dist.metadata["Name"] or "")
                except Exception:
                    continue
                if key and key not in _DISTS:
                    _DISTS[key] = dist
        except Exception:
            pass
    return _DISTS.get(_norm_name(name))


def dist_version(name):
    dist = find_dist(name)
    try:
        return dist.version if dist is not None else None
    except Exception:
        return None


def compact_ints(values):
    values = sorted(set(int(v) for v in values))
    if not values:
        return "-"
    parts, start, prev = [], values[0], values[0]
    for v in values[1:] + [None]:
        if v is not None and v == prev + 1:
            prev = v
            continue
        parts.append(str(start) if start == prev else "%d-%d" % (start, prev))
        if v is not None:
            start = prev = v
    return ",".join(parts)


def short_list(items, limit=180):
    items = list(items)
    out, size = [], 0
    for i, item in enumerate(items):
        if size + len(item) + 1 > limit and out:
            out.append("+%d" % (len(items) - i))
            break
        out.append(item)
        size += len(item) + 1
    return ",".join(out) if out else "-"


def same_node_str(value):
    if isinstance(value, bool):
        return "1" if value else "0"
    return "?" if value is None else str(value)


def boolstr(value):
    if value is None:
        return "?"
    return "1" if value else "0"


# --------------------------------------------------------------------------- expected data


def load_expected():
    if EXPECTED_B64:
        return json.loads(gzip.decompress(base64.b64decode(EXPECTED_B64)).decode("utf-8"))
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "t51_expected.json")
    with open(path, "rb") as f:
        return json.load(f)


# --------------------------------------------------------------------------- /proc scan

CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def _boot_time():
    text = read_text("/proc/stat") or ""
    m = re.search(r"^btime (\d+)", text, re.M)
    return int(m.group(1)) if m else None


class Proc:
    def __init__(self, pid):
        self.pid = pid
        raw = read_bytes("/proc/%d/cmdline" % pid) or b""
        self.argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        self.title = " ".join(self.argv)
        self.comm = (read_text("/proc/%d/comm" % pid) or "").strip()
        stat = read_text("/proc/%d/stat" % pid) or ""
        rest = stat.rsplit(")", 1)[-1].split()
        self.ppid = int(rest[1]) if len(rest) > 1 else 0
        self.start_ticks = int(rest[19]) if len(rest) > 19 else None

    def start_epoch(self, btime):
        if btime is None or self.start_ticks is None:
            return None
        return btime + self.start_ticks / CLK_TCK

    def environ(self):
        t0 = time.monotonic()
        data = read_bytes("/proc/%d/environ" % self.pid)
        _PROC_COST["environ_reads"] += 1
        _PROC_COST["environ_s"] += time.monotonic() - t0
        if data is None:
            return None
        env = {}
        for item in data.split(b"\0"):
            if b"=" in item:
                k, v = item.split(b"=", 1)
                env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
        return env

    def fd_target(self, fd):
        try:
            return os.readlink("/proc/%d/fd/%d" % (self.pid, fd))
        except OSError:
            return None

    def exe(self):
        try:
            return os.readlink("/proc/%d/exe" % self.pid)
        except OSError:
            return None

    def maps_paths(self):
        t0 = time.monotonic()
        text = read_text("/proc/%d/maps" % self.pid)
        _PROC_COST["maps_reads"] += 1
        _PROC_COST["maps_s"] += time.monotonic() - t0
        if text is None:
            return None
        paths = set()
        for line in text.splitlines():
            parts = line.split(None, 5)
            if len(parts) == 6 and parts[5].startswith("/"):
                paths.add(parts[5].replace(" (deleted)", ""))
        return paths


def scan_procs():
    procs = {}
    if not os.path.isdir("/proc"):
        return procs
    for name in os.listdir("/proc"):
        if name.isdigit():
            p = Proc(int(name))
            if p.argv or p.comm:
                procs[p.pid] = p
    return procs


def is_vllm_serve(argv):
    for i, a in enumerate(argv):
        base = os.path.basename(a)
        if base == "vllm" and i + 1 < len(argv) and argv[i + 1] == "serve":
            return True
        if a == "-m" and i + 1 < len(argv):
            mod = argv[i + 1]
            if mod == "vllm.entrypoints.openai.api_server":
                return True
            if mod in ("vllm.entrypoints.cli.main", "vllm") and "serve" in argv[i + 2 : i + 3]:
                return True
    return False


def descendants(procs, pid):
    kids = {}
    for p in procs.values():
        kids.setdefault(p.ppid, []).append(p.pid)
    out, stack = [], [pid]
    while stack:
        for c in kids.get(stack.pop(), ()):
            out.append(c)
            stack.append(c)
    return out


def ancestors(procs, pid):
    out, seen = [], set()
    cur = procs.get(pid)
    while cur is not None and cur.ppid and cur.ppid not in seen:
        seen.add(cur.ppid)
        cur = procs.get(cur.ppid)
        if cur is not None:
            out.append(cur)
    return out


# --------------------------------------------------------------------------- argument parsing


def parse_cli_args(tokens):
    """Very small `--flag value` / `--flag=value` parser; repeated flags keep the last value."""
    opts = {}
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("--"):
            if "=" in t:
                k, v = t[2:].split("=", 1)
                opts[k] = v
            elif i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                opts[t[2:]] = tokens[i + 1]
                i += 1
            else:
                opts[t[2:]] = True
        elif t == "-tp" and i + 1 < len(tokens):
            opts["tensor-parallel-size"] = tokens[i + 1]
            i += 1
        elif t == "-dp" and i + 1 < len(tokens):
            opts["data-parallel-size"] = tokens[i + 1]
            i += 1
        i += 1
    return opts


def serve_tokens(argv):
    for i, a in enumerate(argv):
        if a == "serve":
            return argv[i + 1 :]
        if a == "-m" and i + 1 < len(argv) and argv[i + 1] == "vllm.entrypoints.openai.api_server":
            return argv[i + 2 :]
    return argv[1:]


def jload(value):
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


CONNECTOR_SHORT = {
    "MooncakeHybridConnector": "MooncakeHybrid",
    "MooncakeConnectorV1": "MooncakeV1",
    "MooncakeConnector": "Mooncake",
    "AscendStoreConnector": "AscendStore",
    "SfaRemoteD2HConnector": "SfaRemoteD2H",
    "MultiConnector": "Multi",
}
P2P_CONNECTORS = ("MooncakeHybridConnector", "MooncakeConnectorV1", "MooncakeConnector", "MooncakeLayerwiseConnector")


def describe_kv(cfg):
    out = {"conn": "none", "kind": "?", "load_async": None, "layerwise": None, "policy": None, "sfa_d2h": False}
    if not isinstance(cfg, dict):
        return out
    out["policy"] = cfg.get("kv_load_failure_policy")
    subs = []
    extra = cfg.get("kv_connector_extra_config") or {}
    if isinstance(extra, dict) and isinstance(extra.get("connectors"), list):
        subs = [c for c in extra["connectors"] if isinstance(c, dict)]
    else:
        subs = [cfg]
    names = []
    for c in subs:
        name = str(c.get("kv_connector", "?"))
        names.append(CONNECTOR_SHORT.get(name, name))
        role = c.get("kv_role") or cfg.get("kv_role")
        cextra = c.get("kv_connector_extra_config") or {}
        if name in P2P_CONNECTORS:
            out["kind"] = {"kv_producer": "P", "kv_consumer": "D", "kv_both": "PD"}.get(role, "?")
        if name == "AscendStoreConnector" and isinstance(cextra, dict):
            out["load_async"] = cextra.get("load_async")
            out["layerwise"] = cextra.get("use_layerwise")
        if name == "SfaRemoteD2HConnector":
            out["sfa_d2h"] = True
    out["conn"] = "+".join(names) if names else "none"
    if out["kind"] == "?":
        out["kind"] = {"kv_producer": "P", "kv_consumer": "D", "kv_both": "PD"}.get(cfg.get("kv_role"), "?")
    return out


def unresolved(value):
    return isinstance(value, str) and value.startswith("$")


# Scheduler choice as vllm-ascend d4ad49796 makes it (platform.py: _check_ascend_config, then
# _setup_worker_and_scheduler; ascend_config.py: SchedulerExtensionConfig._resolve).
_ASC = "vllm_ascend.core."
SCHED_FILES = (
    ("vllm", "vllm/v1/core/sched/scheduler.py"),
    ("vllm", "vllm/v1/core/sched/async_scheduler.py"),
    ("vllm_ascend", "vllm_ascend/core/recompute_scheduler.py"),
    ("vllm_ascend", "vllm_ascend/core/dyntra_lb_scheduler.py"),
    ("vllm_ascend", "vllm_ascend/core/scheduler_profiling_chunk.py"),
    ("vllm_ascend", "vllm_ascend/core/short_request_first_scheduler.py"),
    ("vllm_ascend", "vllm_ascend/core/batch_job_aware_scheduler.py"),
)


def scheduler_choice(opts, kind, async_sched):
    """The scheduler class d4ad49796 would install for these serve options, and why."""
    add = jload(opts.get("additional-config"))
    add = add if isinstance(add, dict) else {}
    sc = add.get("scheduler_config") if isinstance(add.get("scheduler_config"), dict) else {}

    def resolve(key, default):
        if key in sc:
            return sc[key]
        if key in add:
            return add[key]
        return default

    def enabled(v):
        return isinstance(v, dict) and bool(v.get("enabled"))

    keys = {
        "recompute_scheduler_enable": bool(resolve("recompute_scheduler_enable", False)),
        "short_request_first": enabled(resolve("short_request_first_config", {})),
        "profiling_chunk": enabled(resolve("profiling_chunk_config", {})),
        "batch_job_sched": enabled(resolve("batch_job_sched_config", {})),
        "dyntra_lb": enabled(sc.get("dyntra_lb_config", {})),
        "enable_balance_scheduling": bool(resolve("enable_balance_scheduling", False)),
    }
    cli = opts.get("scheduler-cls")
    if isinstance(cli, str) and cli:
        cls, why = cli, "cli"
    elif async_sched is True:
        cls, why = "vllm.v1.core.sched.async_scheduler.AsyncScheduler", "default(async)"
    elif async_sched is False:
        cls, why = "vllm.v1.core.sched.scheduler.Scheduler", "default"
    else:
        cls, why = "vllm.v1.core.sched.scheduler.Scheduler|AsyncScheduler", "default(async_unknown)"
    a = async_sched is True
    if keys["short_request_first"] and a:
        cls, why = _ASC + "short_request_first_scheduler.ShortRequestFirstAsyncScheduler", "short_request_first"
    if keys["recompute_scheduler_enable"]:
        if kind == "D":
            if keys["dyntra_lb"]:
                cls = _ASC + ("recompute_scheduler.AsyncDyntraLBRecomputeScheduler" if a else "recompute_scheduler.DyntraLBRecomputeScheduler")
            else:
                cls = _ASC + ("recompute_scheduler.AsyncRecomputeScheduler" if a else "recompute_scheduler.RecomputeScheduler")
            why = "recompute_scheduler_enable"
        elif kind == "P":
            why += "+recompute_ignored_on_P"
    if keys["dyntra_lb"] and not keys["recompute_scheduler_enable"]:
        cls, why = _ASC + ("dyntra_lb_scheduler.AsyncDyntraLBScheduler" if a else "dyntra_lb_scheduler.DyntraLBScheduler"), "dyntra_lb"
    if keys["profiling_chunk"]:
        cls, why = _ASC + ("scheduler_profiling_chunk.ProfilingChunkAsyncScheduler" if a else "scheduler_profiling_chunk.ProfilingChunkScheduler"), "profiling_chunk"
    if keys["batch_job_sched"]:
        cls, why = _ASC + ("batch_job_aware_scheduler.BatchJobAwareAsyncScheduler" if a else "batch_job_aware_scheduler.BatchJobAwareScheduler"), "batch_job_sched"
    return {"cls": cls, "why": why, "keys": keys}


_SCHED_TABLE = {}


def scheduler_table():
    """Class name -> bases and schedule() facts, parsed (never imported) from the scheduler source files of the
    vllm and vllm_ascend copies already being checked."""
    if _SCHED_TABLE:
        return _SCHED_TABLE
    _SCHED_TABLE["_parsed"] = []
    for top, rel in SCHED_FILES:
        pkg = _RUNTIME.get(top)
        if not pkg:
            continue
        text = read_text(os.path.join(os.path.dirname(pkg), rel))
        if text is None:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        _SCHED_TABLE["_parsed"].append(rel)
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            bases = []
            for b in node.bases:
                if isinstance(b, ast.Name):
                    bases.append("Scheduler" if b.id == "_SchedulerBase" else b.id)
                elif isinstance(b, ast.Attribute):
                    bases.append(b.attr)
            sched = None
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "schedule":
                    calls_super = any(
                        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "schedule"
                        and isinstance(n.func.value, ast.Call) and isinstance(n.func.value.func, ast.Name) and n.func.value.func.id == "super"
                        for n in ast.walk(item)
                    )
                    sched = {"line": item.lineno, "calls_super": calls_super}
            _SCHED_TABLE.setdefault(node.name, {"bases": bases, "schedule": sched, "file": rel})
    return _SCHED_TABLE


def _mro(name, table, depth=0):
    """C3 linearization over the parsed classes (unknown names end the chain)."""
    if depth > 30 or name not in table:
        return [name]
    seqs = [list(_mro(b, table, depth + 1)) for b in table[name]["bases"]] + [list(table[name]["bases"])]
    out = [name]
    while True:
        seqs = [q for q in seqs if q]
        if not seqs:
            return out
        for q in seqs:
            head = q[0]
            if not any(head in t[1:] for t in seqs):
                break
        else:
            return out + [x for q in seqs for x in q if x not in out]
        out.append(head)
        for t in seqs:
            if t and t[0] == head:
                del t[0]


def schedule_path(cls):
    """Does cls.schedule() reach vLLM's Scheduler.schedule (where patch 01 records the step)?"""
    if "|" in cls:
        return "vllm_schedule", None
    table = scheduler_table()
    name = cls.rsplit(".", 1)[-1]
    if name not in table:
        return "unknown_class", None
    for c in _mro(name, table):
        info = table.get(c)
        if not info or not info["schedule"]:
            continue
        if c == "Scheduler" and info["file"].startswith("vllm/"):
            return "vllm_schedule", c
        if not info["schedule"]["calls_super"]:
            return "rewritten", "%s@%s:%d" % (c, info["file"].rsplit("/", 1)[-1], info["schedule"]["line"])
    return "no_schedule_found", None


def describe_instance(opts, redact):
    kv = describe_kv(jload(opts.get("kv-transfer-config")))
    if unresolved(opts.get("kv-transfer-config")):
        kv = {"conn": None, "kind": None, "load_async": None, "layerwise": None, "policy": None, "sfa_d2h": None}
    spec = jload(opts.get("speculative-config"))
    for key in ("data-parallel-address", "host"):
        if isinstance(opts.get(key), str):
            redact.add_host(opts[key])
    spec_desc = "none"
    spec_eager = None
    if unresolved(opts.get("speculative-config")):
        spec_desc = None
    if isinstance(spec, dict):
        spec_desc = "%s:%s" % (spec.get("method", "?"), spec.get("num_speculative_tokens", "?"))
        spec_eager = spec.get("enforce_eager")
    if opts.get("async-scheduling") is True:
        async_sched = True
    elif opts.get("no-async-scheduling") is True:
        async_sched = False
    else:
        async_sched = None
    return {
        "port": opts.get("port"),
        "kind": kv["kind"],
        "tp": opts.get("tensor-parallel-size", "1"),
        "pp": opts.get("pipeline-parallel-size", "1"),
        "dp": opts.get("data-parallel-size", "1"),
        "dp_rank": opts.get("data-parallel-rank"),
        "dp_local": opts.get("data-parallel-size-local"),
        "ep": boolstr(opts.get("enable-expert-parallel") is True),
        "conn": kv["conn"],
        "load_async": kv["load_async"],
        "layerwise": kv["layerwise"],
        "policy": kv["policy"],
        "sfa_d2h": kv["sfa_d2h"],
        "spec": spec_desc,
        "spec_eager": spec_eager,
        "eager": boolstr(opts.get("enforce-eager") is True),
        "async_sched": async_sched,
        "per_req_metrics": opts.get("enable-per-request-metrics") is True,
        "force_usage": opts.get("enable-force-include-usage") is True,
        "otlp": "otlp-traces-endpoint" in opts,
        "dp_addr": redact.placeholder(opts.get("data-parallel-address")) if opts.get("data-parallel-address") else None,
        "sched": scheduler_choice(opts, kv["kind"], async_sched),
    }


# --------------------------------------------------------------------------- launch files

ENV_KEYS = (
    "VLLM_USE_V2_MODEL_RUNNER",
    "VLLM_LOGGING_LEVEL",
    "VLLM_CONFIGURE_LOGGING",
    "VLLM_LOGGING_CONFIG_PATH",
    "MOONCAKE_CONFIG_PATH",
    "MOONCAKE_MASTER",
    "MOONCAKE_GLOBAL_SEGMENT_SIZE",
    "MC_STORE_CLIENT_METRIC",
    "GLOG_logtostderr",
    "GLOG_alsologtostderr",
    "GLOG_stderrthreshold",
    "GLOG_minloglevel",
    "GLOG_v",
    "GLOG_log_dir",
    "PYTHONPATH",
    "ASCEND_RT_VISIBLE_DEVICES",
    "VLLM_TTFT_DIAGNOSTICS",
    "VLLM_DISABLE_REQUEST_ID_RANDOMIZATION",
)
ENV_VALUE_KEYS = (
    "VLLM_USE_V2_MODEL_RUNNER",
    "VLLM_LOGGING_LEVEL",
    "VLLM_CONFIGURE_LOGGING",
    "MC_STORE_CLIENT_METRIC",
    "GLOG_logtostderr",
    "GLOG_alsologtostderr",
    "GLOG_stderrthreshold",
    "GLOG_minloglevel",
    "GLOG_v",
    "VLLM_TTFT_DIAGNOSTICS",
    "VLLM_DISABLE_REQUEST_ID_RANDOMIZATION",
)
ADDRESS_ENV = ("HCCL_IF_IP", "MOONCAKE_MASTER", "VLLM_HOST_IP", "MASTER_ADDR")


def env_view(env):
    """Whitelisted view of an environment: values only for non-path, non-address keys."""
    if env is None:
        return None
    out = {}
    for k in ENV_KEYS:
        if k not in env:
            continue
        if k in ENV_VALUE_KEYS:
            out[k] = env[k][:16]
        elif k == "ASCEND_RT_VISIBLE_DEVICES":
            out[k] = "n=%d" % len([x for x in env[k].split(",") if x.strip()])
        else:
            out[k] = "set"
    return out


def shell_logical_lines(text):
    lines, buf = [], ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        lines.append(buf)
        buf = ""
    if buf:
        lines.append(buf)
    return lines


def expand_vars(token, env):
    """Expand $NAME / ${NAME} from assignments seen earlier in the same file; keep $1..$9."""

    def sub(m):
        name = m.group(1) or m.group(2)
        return env.get(name, m.group(0))

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", sub, token)


def shell_split(line):
    try:
        return shlex.split(line, comments=True, posix=True)
    except ValueError:
        return line.split()


def parse_shell(path, redact):
    text = read_text(path)
    if text is None:
        return None
    info = {"sha": file_sha16(path), "exports": {}, "commands": [], "redirect": []}
    for line in shell_logical_lines(text):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", s)
        if m:
            key, value = m.group(1), m.group(2).strip().strip("\"'")
            if is_address_key(key):
                redact.add_host(value)
            if SECRET_NAME.search(key):
                redact.add_secret(value)
            info["exports"][key] = value
            continue
        tokens = [expand_vars(t, info["exports"]) for t in shell_split(s)]
        if not tokens:
            continue
        if any(t in (">", ">>", "2>", "2>>", "&>", "|", "1>") or t.startswith((">", "2>", "&>")) for t in tokens):
            kinds = set()
            for i, t in enumerate(tokens):
                if t in ("|",) and i + 1 < len(tokens):
                    kinds.add("pipe:" + os.path.basename(tokens[i + 1]))
                elif t.startswith(("2>&1",)):
                    kinds.add("2>&1")
                elif t.startswith(("2>",)):
                    kinds.add("stderr>file")
                elif t.startswith((">", "1>", "&>")):
                    kinds.add("stdout>file")
            info["redirect"].append(sorted(kinds))
        info["commands"].append(tokens)
    return info


def find_serve_in_shell(info):
    for tokens in info.get("commands", ()):
        if is_vllm_serve(tokens) or ("vllm" in tokens and "serve" in tokens):
            return serve_tokens(tokens)
    return None


def find_launch_call(info):
    for tokens in info.get("commands", ()):
        for i, t in enumerate(tokens):
            if os.path.basename(t).startswith("launch_online_dp"):
                return parse_cli_args(tokens[i + 1 :])
    return None


def parse_mooncake_json(path, redact):
    data = read_bytes(path)
    if data is None:
        return None
    try:
        cfg = json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        return {"sha": sha16(data), "error": "not_json"}
    out = {"sha": sha16(data), "keys": sorted(cfg) if isinstance(cfg, dict) else []}
    if isinstance(cfg, dict):
        for key in ("metadata_server", "master_server_address", "local_hostname"):
            if cfg.get(key):
                redact.add_host(cfg[key])
        for key, value in cfg.items():
            if SECRET_NAME.search(key) and isinstance(value, str):
                redact.add_secret(value)
        for key in (
            "protocol",
            "prefer_alloc_in_same_node",
            "preferred_segment",
            "enable_ssd_offload",
            "enable_ssd_prefetch",
            "global_segment_size",
            "local_buffer_size",
        ):
            if key in cfg and isinstance(cfg[key], (bool, int, float, str)):
                out[key] = cfg[key] if not isinstance(cfg[key], str) else cfg[key][:24]
    return out


def parse_start_mooncake(path, redact):
    info = parse_shell(path, redact)
    if info is None:
        return None
    out = {"sha": info["sha"], "master_flags": [], "found": False}
    for tokens in info["commands"]:
        for i, t in enumerate(tokens):
            if os.path.basename(t) == "mooncake_master":
                out["found"] = True
                opts = parse_cli_args([x if not x.startswith("-") or x.startswith("--") else "-" + x for x in tokens[i + 1 :]])
                for k, v in opts.items():
                    if isinstance(v, str) and (IPV4.search(v) or ":" in v or "addr" in k.lower() or "host" in k.lower()):
                        redact.add_host(v)
                out["master_flags"] = sorted(opts)
    return out


# --------------------------------------------------------------------------- code trees


PROC_SITE = {}


def _parse_editable_mapping(path):
    """MAPPING of a setuptools editable finder module, read as text (the module is never imported)."""
    text = read_text(path) or ""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {}
    for node in tree.body:
        target = node.targets[0] if isinstance(node, ast.Assign) and node.targets else getattr(node, "target", None)
        if isinstance(target, ast.Name) and target.id == "MAPPING" and getattr(node, "value", None) is not None:
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                return {}
            return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}
    return {}


def _pth_entries(sitedir):
    """What site.py would add for one site dir: path lines of *.pth files; 'import' lines are not run, only parsed
    for setuptools editable finders."""
    paths, editable = [], {}
    try:
        names = sorted(n for n in os.listdir(sitedir) if n.endswith(".pth"))
    except OSError:
        return paths, editable
    for fn in names:
        for line in (read_text(os.path.join(sitedir, fn)) or "").splitlines():
            line = line.rstrip()
            if not line or line.startswith("#"):
                continue
            if line.startswith(("import ", "import\t")):
                for m in re.finditer(r"\b(__editable___\w+_finder)\b", line):
                    editable.update(_parse_editable_mapping(os.path.join(sitedir, m.group(1) + ".py")))
                continue
            full = os.path.normpath(os.path.join(sitedir, line))
            if os.path.exists(full):
                paths.append(full)
    return paths, editable


def site_layout():
    """Site dirs of this interpreter (python3 -S: site.py did not run, so they are computed, nothing is executed)."""
    import site  # with -S, importing site defines its functions without running site.main()

    dirs = []
    try:
        dirs += site.getsitepackages()
    except Exception:
        pass
    for key in ("purelib", "platlib"):
        value = sysconfig.get_paths().get(key)
        if value:
            dirs.append(value)
    out = []
    for d in dirs:
        if os.path.isdir(d) and d not in out:
            out.append(d)
    try:
        user = site.getusersitepackages()
    except Exception:
        user = None
    return out, (user if user and os.path.isdir(user) else None)


def process_flags_and_first(argv, cwd):
    """Interpreter flags of the vLLM process and its sys.path[0], from /proc/<pid>/cmdline."""
    flags, first = set(), None
    if not argv:
        return flags, first
    i = 1 if os.path.basename(argv[0]).startswith("python") else 0
    if i == 0:
        return flags, (os.path.dirname(os.path.realpath(argv[0])) if argv[0].startswith("/") else None)
    while i < len(argv):
        a = argv[i]
        if a in ("-m", "-c"):
            first = cwd
            break
        if a.startswith("-") and not a.startswith("--") and len(a) > 1:
            flags.update(c for c in a[1:] if c.isalpha())
            if a in ("-W", "-X"):
                i += 1
            i += 1
            continue
        first = os.path.dirname(os.path.realpath(a if a.startswith("/") else os.path.join(cwd or "/", a)))
        break
    if "I" in flags or "P" in flags:
        first = None
    return flags, first


def process_search_path(api, run_env):
    """sys.path of the vLLM process, rebuilt from its cmdline, cwd and environment, plus editable finders."""
    try:
        cwd = os.readlink("/proc/%d/cwd" % api.pid) if api is not None else os.getcwd()
    except OSError:
        cwd = None
    argv = api.argv if api is not None else []
    flags, first = process_flags_and_first(argv, cwd)
    entries = []
    if first:
        entries.append((first, "script_dir" if api is not None else "cwd"))
    env = run_env if run_env is not None else dict(os.environ)
    if env.get("PYTHONPATH") and "E" not in flags and "I" not in flags:
        for e in env["PYTHONPATH"].split(":"):
            if not os.path.isabs(e):
                e = os.path.join(cwd, e) if cwd else None
            if e:
                entries.append((os.path.normpath(e), "pythonpath"))
    editable = {}
    if "S" not in flags:
        sdirs, user = PROC_SITE.get("site_dirs", []), PROC_SITE.get("user_site")
        no_user = "s" in flags or "I" in flags or env.get("PYTHONNOUSERSITE")
        if user and not no_user:
            entries.append((user, "user_site"))
            extra, ed = _pth_entries(user)
            entries += [(x, "pth") for x in extra]
            editable.update(ed)
        for d in sdirs:
            entries.append((d, "site"))
            extra, ed = _pth_entries(d)
            entries += [(x, "pth") for x in extra]
            editable.update(ed)
    return {"cwd": cwd, "flags": "".join(sorted(flags)), "entries": entries, "editable": editable}


def resolve_package(name, search):
    """First regular package called name along the process's sys.path, then its editable finders (appended to
    sys.meta_path after the path finder). Returns (dir, how, [all candidates])."""
    cands = []
    for entry, how in search["entries"]:
        d = os.path.join(entry, name)
        if os.path.isfile(os.path.join(d, "__init__.py")) and all(os.path.realpath(d) != os.path.realpath(c[0]) for c in cands):
            cands.append((d, how))
    ed = search["editable"].get(name)
    if ed and os.path.isfile(os.path.join(ed, "__init__.py")) and all(os.path.realpath(ed) != os.path.realpath(c[0]) for c in cands):
        cands.append((ed, "editable"))
    if not cands:
        return None, None, []
    return cands[0][0], cands[0][1], cands


def pyc_evidence(pkg_dir, rels, since):
    """For the given sources: is there a .pyc for this interpreter, does its header match the source, was it
    written after vLLM started. Headers are read (16 bytes); nothing is compiled."""
    tag = sys.implementation.cache_tag
    out = {"sources": 0, "pyc": 0, "match": 0, "after_start": 0}
    if not tag:
        return out
    root = os.path.dirname(pkg_dir)
    for rel in rels:
        src = os.path.join(root, rel)
        try:
            sst = os.stat(src)
        except OSError:
            continue
        out["sources"] += 1
        stem = os.path.splitext(os.path.basename(src))[0]
        pyc = os.path.join(os.path.dirname(src), "__pycache__", "%s.%s.pyc" % (stem, tag))
        try:
            pst = os.stat(pyc)
        except OSError:
            continue
        out["pyc"] += 1
        hdr = read_bytes(pyc, 16) or b""
        if len(hdr) == 16 and hdr[:4] == importlib.util.MAGIC_NUMBER:
            flags = int.from_bytes(hdr[4:8], "little")
            if flags & 1:
                data = read_bytes(src)
                ok = data is not None and importlib.util.source_hash(data) == hdr[8:16]
            else:
                ok = int.from_bytes(hdr[8:12], "little") == (int(sst.st_mtime) & 0xFFFFFFFF) and int.from_bytes(hdr[12:16], "little") == (sst.st_size & 0xFFFFFFFF)
            out["match"] += int(bool(ok))
        if since and pst.st_mtime >= since:
            out["after_start"] += 1
    return out


def install_kind(dist_name):
    dist = find_dist(dist_name)
    if dist is None:
        return "absent", None
    raw = dist.read_text("direct_url.json")
    if raw:
        try:
            info = json.loads(raw)
            if (info.get("dir_info") or {}).get("editable"):
                return "editable", info.get("url")
            if info.get("url", "").startswith("file:"):
                return "local", info.get("url")
        except ValueError:
            pass
    return "wheel", None


def git_head(root):
    git = os.path.join(root, ".git")
    if os.path.isfile(git):
        text = read_text(git) or ""
        m = re.match(r"gitdir:\s*(.+)", text.strip())
        if not m:
            return None
        git = os.path.join(root, m.group(1)) if not os.path.isabs(m.group(1)) else m.group(1)
    head = read_text(os.path.join(git, "HEAD"))
    if head is None:
        return None
    head = head.strip()
    if head.startswith("ref:"):
        ref = head[4:].strip()
        value = read_text(os.path.join(git, ref))
        if value:
            return value.strip()[:12]
        packed = read_text(os.path.join(git, "packed-refs")) or ""
        for line in packed.splitlines():
            if line.endswith(" " + ref):
                return line.split()[0][:12]
        return None
    return head[:12]


SKIP_FILES = {"_version.py"}


def hash_tree(pkg_dir, top):
    """Hash every .py file below pkg_dir; keys are paths relative to the parent (top/...)."""
    out = {}
    newest = 0.0
    for dirpath, dirnames, filenames in os.walk(pkg_dir):
        dirnames[:] = [d for d in dirnames if d != "__pycache__" and not d.startswith(".")]
        check_deadline()
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            rel = top + "/" + os.path.relpath(full, pkg_dir).replace(os.sep, "/")
            out[rel] = file_sha16(full)
            try:
                newest = max(newest, os.path.getmtime(full))
            except OSError:
                pass
    return out, newest


def compare_tree(name, pkg_dir, top, ref, focus, procs_start, max_changes=40):
    actual, newest = hash_tree(pkg_dir, top)
    base = ref["base"]
    alts = ref.get("alts", {})
    result = {
        "files_total": len(actual),
        "equal_base": 0,
        "missing": [],
        "added": [],
        "changed": {},
        "changed_after_start": [],
    }
    for rel, h in sorted(base["files"].items()):
        a = actual.get(rel)
        if a is None:
            result["missing"].append(rel)
            continue
        if a == h:
            result["equal_base"] += 1
            continue
        matched = [alt for alt, data in alts.items() if data["files"].get(rel) == a]
        result["changed"][rel] = {"matches": matched}
    for rel in sorted(actual):
        if rel not in base["files"]:
            matched = [alt for alt, data in alts.items() if data["files"].get(rel) == actual[rel]]
            if os.path.basename(rel) in SKIP_FILES:
                continue
            if matched:
                result["changed"][rel] = {"matches": matched, "new_file": True}
            else:
                result["added"].append(rel)
    # function-level detail for changed files that match no reference
    shown = 0
    for rel, info in sorted(result["changed"].items()):
        check_deadline()
        if info["matches"]:
            continue
        full = os.path.join(os.path.dirname(pkg_dir), rel)
        data = read_bytes(full)
        hashes = func_hashes(data) if data is not None else None
        if hashes is None:
            info["functions"] = "unparsed"
            continue
        candidates = [(ref["base_label"], base.get("funcs", {}).get(rel))]
        candidates += [(alt, d.get("funcs", {}).get(rel)) for alt, d in alts.items()]
        best = None
        for label, ref_funcs in candidates:
            if not ref_funcs:
                continue
            diff = sorted(
                set(k for k in set(ref_funcs) | set(hashes) if ref_funcs.get(k) != hashes.get(k))
            )
            if best is None or len(diff) < len(best[1]):
                best = (label, diff)
        if best is None:
            info["functions"] = "no_reference"
        else:
            info["nearest"] = best[0]
            info["functions"] = best[1][:max_changes]
            info["functions_more"] = max(0, len(best[1]) - max_changes)
        shown += 1
        try:
            if procs_start and os.path.getmtime(full) > procs_start:
                result["changed_after_start"].append(rel)
        except OSError:
            pass
    # state
    alt_full = []
    for alt, data in alts.items():
        touched = set(data["files"])
        ok = all(actual.get(rel) == h for rel, h in data["files"].items())
        others = all(actual.get(rel) == h for rel, h in base["files"].items() if rel not in touched)
        if ok and others and not result["missing"]:
            alt_full.append(alt)
    if not result["changed"] and not result["missing"]:
        state = ref["base_label"]
    elif alt_full:
        state = alt_full[0]
    else:
        state = "modified"
    result["state"] = state
    result["newest_mtime"] = newest
    result["focus_changed"] = sorted(r for r in result["changed"] if r in focus)
    return result, actual


def change_labels(result, strip, state=None):
    """Files equal to a known reference are counted per reference; the rest are listed with functions."""
    counts = {}
    labels = []
    for rel, info in sorted(result["changed"].items()):
        short = rel[len(strip) :] if rel.startswith(strip) else rel
        if info.get("matches"):
            ref = info["matches"][0]
            counts[ref] = counts.get(ref, 0) + 1
        elif isinstance(info.get("functions"), list):
            funcs = info["functions"]
            label = "%s~%s:%s" % (short, info.get("nearest", "?"), "|".join(funcs[:3]) if funcs else "?")
            more = len(funcs) - 3 + info.get("functions_more", 0)
            if more > 0:
                label += "|+%d" % more
            labels.append(label)
        else:
            labels.append("%s:%s" % (short, info.get("functions", "?")))
    for rel in result["missing"]:
        labels.append("-" + (rel[len(strip) :] if rel.startswith(strip) else rel))
    for rel in result["added"][:3]:
        labels.append("+" + (rel[len(strip) :] if rel.startswith(strip) else rel))
    if len(result["added"]) > 3:
        labels.append("+%d_more_added" % (len(result["added"]) - 3))
    head = ["same_as:%sx%d" % (ref, n) for ref, n in sorted(counts.items()) if ref != state]
    return head + labels


# --------------------------------------------------------------------------- mooncake


def elf_needed_glog(data):
    m = re.findall(rb"libglog[-\w]*\.so(?:\.\d+)*", data)
    return sorted(set(x.decode() for x in m))


GLOG_SO = {"0": "gettid", "1": "gettid", "2": "thread_id"}


def glog_family(names):
    """glog <= 0.6 prints gettid() in the log prefix (same as Python's native_tid); 0.7+ prints std::thread::id."""
    best = None
    for name in names:
        m = re.search(r"libglog[-\w]*\.so\.(\d+)((?:\.\d+)*)", name)
        if m:
            ver = m.group(1) + m.group(2)
            label = "%s(%s)" % (ver, GLOG_SO.get(m.group(1), "?"))
            if best is None or len(ver) > len(best[0]):
                best = (ver, label)
    return best[1] if best else None


def mooncake_files(site, pkg):
    """Binary and Python files of the Mooncake wheel: the package and its bundled .libs directory."""
    files = {}
    roots = [pkg]
    for cand in sorted(os.listdir(site)) if os.path.isdir(site) else ():
        if cand.startswith("mooncake_transfer_engine") and cand.endswith(".libs"):
            roots.append(os.path.join(site, cand))
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in filenames:
                if ".so" in fn or fn.endswith(".py") or fn == "mooncake_master":
                    full = os.path.join(dirpath, fn)
                    files[os.path.relpath(full, site).replace(os.sep, "/")] = full
    return files


def installed_record(dist_name):
    try:
        text = find_dist(dist_name).read_text("RECORD") or ""
    except Exception:
        return {}
    out = {}
    for line in text.splitlines():
        parts = line.rsplit(",", 2)
        if len(parts) == 3 and parts[1].startswith("sha256="):
            b64 = parts[1][len("sha256=") :]
            try:
                out[parts[0]] = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).hex()[:16]
            except ValueError:
                pass
    return out


def is_binary(rel):
    return ".so" in rel or rel.endswith("mooncake_master")


THIRD_PARTY = ("libglog", "libgflags", "libzstd", "libjsoncpp", "libyaml", "liburing", "libxxhash", "libasio")


MC_DISTS = ("mooncake-transfer-engine", "mooncake-transfer-engine-npu")
_RUNTIME = {}


def mc_dist():
    for name in MC_DISTS:
        version = dist_version(name)
        if version:
            return name, version
    return None, None


def check_mooncake(expected, worker_maps, redact, search):
    dist_name, dist_ver = mc_dist()
    out = {"version": dist_ver, "dist": dist_name, "pkg": None}
    pkg, how, _cands = resolve_package("mooncake", search)
    out["how"] = how
    if pkg is None:
        out["state"] = "absent"
        return out
    out["pkg"] = redact.scrub_path(pkg)
    site = os.path.dirname(pkg)
    paths = mooncake_files(site, pkg)
    files, timing, glog_names, display, store_elf, tags = {}, [], set(), set(), {}, set()
    # libmooncake_store first: its version string (git commit) must not be lost to the read budget
    for rel, full in sorted(paths.items(), key=lambda kv: (0 if "libmooncake_store" in kv[0] else 1, kv[0])):
        check_deadline()
        base = os.path.basename(rel)
        found = None
        if is_binary(rel) and not base.startswith(THIRD_PARTY):
            files[rel], found = hash_scan_binary(full, {"timing": TIMING_TAG, "glog": GLOG_NAME, "display": DISPLAY_VER})
        elif is_binary(rel):
            files[rel] = budgeted_sha16(full)
        else:
            files[rel] = file_sha16(full)
        if base.startswith("libglog"):
            glog_names.add(base)
        if found is not None:
            if found["timing"]:
                timing.append(rel)
                if base.startswith("libmooncake_store"):
                    tags.update(x.decode("ascii", "replace") if isinstance(x, bytes) else str(x) for x in found["timing"])
            glog_names.update(x.decode() for x in found["glog"])
            for ver, git in found["display"]:
                display.add((ver.decode("ascii", "replace"), git.decode("ascii", "replace")))
        if is_binary(rel) and base.startswith("libmooncake_store"):
            info = elf_info(full)  # ELF header and dynamic section only
            if info is not None:
                store_elf[rel] = info
    out["files"] = files
    out["timing_in"] = timing
    out["timing_tags"] = sorted(tags)
    # which libmooncake_store.so this is: the delivered replacement, the site's original (field check f-1), or other
    store_rel = os.path.basename(pkg) + "/libmooncake_store.so"
    orig = expected.get("mooncake_site_original") or {}
    repls = list(expected.get("mooncake_replacements") or [])
    if not repls and expected.get("mooncake_replacement"):
        repls = [dict(expected["mooncake_replacement"], label="")]
    got = files.get(store_rel)
    hit = [r for r in repls if got is not None and got == r.get("sha16")]
    out["store_is"] = ("replacement" if hit else "site_original" if orig and got == orig.get("sha16")
                       else "absent" if got is None else "other")
    out["replacement"] = hit[0].get("label") if hit else None
    sib = {}
    for name, want in sorted((expected.get("mooncake_lab_siblings") or {}).items()):
        exp = dynsym_exports(os.path.join(pkg, name))
        sib[name] = None if exp is None else [len(exp), sha16("\n".join(sorted(exp)).encode()), [len(exp), sha16("\n".join(sorted(exp)).encode())] == want]
    out["siblings"] = sib
    # build facts: version string compiled into the library (it carries the git commit), wheel tag, links
    out["display"] = sorted("%s (git: %s)" % d for d in display)
    out["git"] = sorted(set(d[1] for d in display))
    out["store_elf"] = store_elf
    try:
        wheel_text = (find_dist(dist_name).read_text("WHEEL") or "") if dist_name and find_dist(dist_name) else ""
    except Exception:
        wheel_text = ""
    out["wheel_tag"] = [line.split(":", 1)[1].strip() for line in wheel_text.splitlines() if line.startswith("Tag:")]
    # which published wheel was installed (by the installed RECORD), and what changed since
    record = installed_record(dist_name) if dist_name else {}
    bin_record = {k: v for k, v in record.items() if is_binary(k)}
    out["so_skipped"] = sorted(k for k, v in files.items() if v == "skipped")
    out["changed_since_install"] = sorted(k for k, v in bin_record.items() if files.get(k) not in (None, v, "skipped"))
    out["missing_since_install"] = sorted(k for k in bin_record if k not in files)
    best = None
    for label, ref in (expected.get("mooncake") or {}).items():
        ref_bin = {k: v for k, v in ref["files"].items() if is_binary(k)}
        act_bin = {k: v for k, v in files.items() if is_binary(k)}
        # a binary skipped for the read budget is unknown, not different
        diff = sorted(k for k in set(ref_bin) | set(act_bin) if act_bin.get(k) != "skipped" and ref_bin.get(k) != act_bin.get(k))
        # pip may record scripts under other paths (../../bin/...); compare the paths both records have
        shared = [k for k in bin_record if k in ref_bin]
        rec_same = len(shared) >= max(1, len(ref_bin) * 9 // 10) and all(ref_bin[k] == bin_record[k] for k in shared)
        score = (0 if rec_same else 1, len(diff))
        if best is None or score < best[0]:
            best = (score, label, diff, rec_same)
    if best is not None:
        out["nearest"] = best[1]
        out["record_matches_nearest"] = best[3]
        out["so_diff"] = best[2]
        out["state"] = best[1] if not best[2] else ("modified_" + best[1] if best[3] else "unknown_build")
    else:
        out["state"] = "no_reference"
    loaded, loaded_glog, glog_from = set(), set(), set()
    for maps in worker_maps:
        for path in maps:
            base = os.path.basename(path)
            if "mooncake" in path.lower():
                loaded.add(redact.scrub_path(path))
            if base.startswith("libglog"):
                loaded_glog.add(base)
                if ".libs/" in path or "/mooncake/" in path:
                    glog_from.add("bundled")
                elif path.startswith(SYSTEM_LIB_DIRS):
                    glog_from.add("system")
                else:
                    glog_from.add("other")
    out["loaded"] = sorted(loaded)
    out["glog_from"] = sorted(glog_from)
    out["glog_names"] = sorted(glog_names)
    out["glog_loaded"] = sorted(loaded_glog)
    out["glog"] = glog_family(sorted(loaded_glog)) or glog_family(sorted(glog_names)) or "unknown"
    return out


# --------------------------------------------------------------------------- build environment

BUILD_TOOLS = ("gcc", "g++", "make", "cmake", "ninja", "git", "patch")
BUILD_HEADERS = {
    "glog": ("glog/logging.h",),
    "gflags": ("gflags/gflags.h",),
    "jsoncpp": ("json/json.h", "jsoncpp/json/json.h"),
    "yaml-cpp": ("yaml-cpp/yaml.h",),
    "boost": ("boost/version.hpp",),
    "verbs": ("infiniband/verbs.h",),
    "numa": ("numa.h",),
    "curl": ("curl/curl.h",),
    "msgpack": ("msgpack.hpp",),
    "zstd": ("zstd.h",),
}


def check_build(cann_home):
    tools = {t: bool(shutil.which(t)) for t in BUILD_TOOLS}
    inc_dirs = ["/usr/include", "/usr/local/include"]
    headers = {}
    for name, cands in BUILD_HEADERS.items():
        headers[name] = any(os.path.exists(os.path.join(d, c)) for d in inc_dirs for c in cands)
    py_inc = sysconfig.get_paths().get("include")
    headers["Python.h"] = bool(py_inc and os.path.exists(os.path.join(py_inc, "Python.h")))
    headers["pybind11"] = dist_version("pybind11") is not None or os.path.exists("/usr/include/pybind11/pybind11.h")
    cann_dev = None
    if cann_home:
        cands = [cann_home, os.path.join(cann_home, platform.machine() + "-linux")]
        cann_dev = {
            "acl.h": any(os.path.exists(os.path.join(c, "include", "acl", "acl.h")) for c in cands),
            "hccl.h": any(os.path.exists(os.path.join(c, "include", "hccl", "hccl.h")) for c in cands),
            "libascendcl": any(os.path.exists(os.path.join(c, "lib64", "libascendcl.so")) for c in cands),
        }
    return {"tools": tools, "headers": headers, "cann_dev": cann_dev}


def find_cann(env):
    cands = []
    for key in ("ASCEND_TOOLKIT_HOME", "ASCEND_HOME_PATH"):
        if env and env.get(key):
            cands.append(env[key])
        if os.environ.get(key):
            cands.append(os.environ[key])
    cands += ["/usr/local/Ascend/ascend-toolkit/latest", "/usr/local/Ascend/cann"]
    for c in cands:
        if not os.path.isdir(c):
            continue
        for fn in ("version.cfg", "ascend_toolkit_install.info", os.path.join(platform.machine() + "-linux", "ascend_toolkit_install.info")):
            text = read_text(os.path.join(c, fn))
            if text:
                m = re.search(r"(?:^|\n)\s*(?:version|Version|toolkit_running_version)\s*=\s*\[?([^\]\n:]+)", text)
                if m:
                    return c, m.group(1).strip().split(":")[-1]
        return c, "unknown"
    return None, None


# --------------------------------------------------------------------------- logs


RUNNER_PATTERNS = (
    (re.compile(r"VLLM_USE_V2_MODEL_RUNNER=\S+ is set; using Model Runner (V[12])"), "env"),
    (re.compile(r"using the V1 model runner instead"), "fallback"),
    (re.compile(r"VLLM_USE_V2_MODEL_RUNNER is unset; using Model Runner V2 by default"), "default"),
    # vllm-ascend d4ad49796 (glm5.3) logs only when it builds the V2 runner (worker/worker.py)
    (re.compile(r"npu model runner v2 is in developing"), "log"),
)


def scan_log(path):
    out = {"readable": False}
    try:
        size = os.path.getsize(path)
    except OSError:
        return out
    out["readable"] = True
    out["bytes"] = size
    runners = set()
    counts = {"pd_diag": 0, "batch_put_timing": 0, "glog_before_init": 0}
    events = set()
    worker_runner = set()
    read = 0
    try:
        with open(path, "rb") as f:
            for raw in f:
                read += len(raw)
                _IO["read"] += len(raw)
                if read > MAX_LOG_BYTES:
                    out["truncated"] = True
                    break
                if read & 0xFFFFF < len(raw):
                    check_deadline()
                line = raw.decode("utf-8", "replace")
                if "Model Runner" in line or "model runner" in line:
                    for pat, how in RUNNER_PATTERNS:
                        m = pat.search(line)
                        if m:
                            gen = "V1" if how == "fallback" else ("V2" if how in ("default", "log") else m.group(1))
                            runners.add(gen + "(" + how + ")")
                if "[PD_DIAG]" in line:
                    counts["pd_diag"] += 1
                    m = re.search(r'"event":"([a-z_]+)"', line)
                    if m:
                        events.add(m.group(1))
                    m = re.search(r'"runner":"([\w.]+)"', line)
                    if m:
                        mod = m.group(1)
                        gen = "V2" if ".v2." in mod or ".gpu." in mod else ("V1" if "v1" in mod else "?")
                        worker_runner.add(gen + ":" + mod.rsplit(".", 1)[-1])
                if "[BatchPutTiming]" in line or "[BatchUpsertTiming]" in line:
                    counts["batch_put_timing"] += 1
                if "Logging before InitGoogleLogging" in line:
                    counts["glog_before_init"] += 1
    except OSError:
        pass
    out["runner_lines"] = sorted(runners)
    out["counts"] = counts
    out["events"] = sorted(events)
    out["worker_runner"] = sorted(worker_runner)
    return out


# --------------------------------------------------------------------------- output descriptors


def fd_kind(target, procs, pid):
    if target is None:
        return "?"
    if target.startswith("pipe:"):
        readers = []
        for p in procs.values():
            if p.pid == pid:
                continue
            try:
                for fd in os.listdir("/proc/%d/fd" % p.pid):
                    try:
                        if os.readlink("/proc/%d/fd/%s" % (p.pid, fd)) == target and fd == "0":
                            readers.append(os.path.basename(p.argv[0]) if p.argv else p.comm)
                    except OSError:
                        pass
            except OSError:
                pass
        if readers:
            return "pipe>" + "/".join(sorted(set(readers)))[:24]
        return "pipe>outside"
    if target.startswith("socket:"):
        return "socket"
    if target.startswith("/dev/pts") or target.startswith("/dev/tty"):
        return "tty"
    if target == "/dev/null":
        return "null"
    if target.startswith("/"):
        return "file"
    return "other"


def free_gib(path):
    try:
        st = os.statvfs(path)
        return round(st.f_bavail * st.f_frsize / (1 << 30), 1)
    except OSError:
        return None


KNOWN_PID1 = {"bash": "bash", "sh": "sh", "dash": "sh", "tini": "tini", "dumb-init": "dumb-init", "sleep": "sleep", "init": "init", "systemd": "systemd", "supervisord": "supervisord", "sshd": "sshd"}


# --------------------------------------------------------------------------- main


class Section:
    """Run one part of the check; a failure is recorded, the rest of the report still runs."""

    def __init__(self, name, errors):
        self.name = name
        self.errors = errors

    def __enter__(self):
        self.t0 = time.monotonic()
        return self

    def __exit__(self, et, ev, tb):
        _TIMES[self.name] = _TIMES.get(self.name, 0.0) + time.monotonic() - self.t0
        if et is None or not issubclass(et, Exception):
            return False
        line = 0
        while tb is not None:
            if tb.tb_frame.f_code.co_filename == __file__:
                line = tb.tb_lineno
            tb = tb.tb_next
        self.errors.append("%s:%s@%d" % (self.name, et.__name__, line))
        return True


def glibc_version():
    try:
        value = os.confstr("CS_GNU_LIBC_VERSION")
        if value:
            return value.replace("glibc ", "")
    except (ValueError, OSError, AttributeError):
        pass
    lib, ver = platform.libc_ver()
    return ver or "?"


def _diff_lines(old, new, a, b):
    import difflib

    out = []
    for line in difflib.unified_diff(old, new, a, b, n=3):
        if not line.endswith("\n"):
            line += "\n\\ No newline at end of file\n"
        out.append(line)
    return out


def export_diff(expected, redact, path):
    """Unified diff of the patch-relevant files against the embedded public originals.

    Covers the files the T51 patches change (vLLM and vllm-ascend), plus .py files that exist locally
    in the same directories but not in the public tree. Other changed files are listed by name only.
    The text is redacted (addresses, hostnames, secrets, personal paths) and self-checked before writing.
    """
    originals = expected.get("export") or {}
    stats = {"diffed": 0, "new": 0, "missing": 0, "lines": 0}
    header = [
        "# T51 glm5.3 local-modification export, tool %s" % TOOL_VERSION,
        "# baseline: vLLM %s, vllm-ascend %s (public sources embedded in the tool)"
        % (expected["vllm"]["base_label"], expected["vllm_ascend"]["base_label"]),
    ]
    body = []
    for top in ("vllm", "vllm_ascend"):
        files = originals.get(top) or {}
        pkg = _RUNTIME.get(top)
        if not pkg:
            header.append("# %s: package not found" % top)
            continue
        root = os.path.dirname(pkg)
        base_files = expected[top]["base"]["files"]
        dirs = set()
        exported_new = set()
        for rel in sorted(files):
            check_deadline()
            dirs.add(os.path.dirname(rel))
            data = read_bytes(os.path.join(root, rel))
            if data is None:
                stats["missing"] += 1
                header.append("# missing locally: %s" % rel)
                continue
            current = data.decode("utf-8", "replace")
            if current == files[rel]:
                continue
            body += _diff_lines(files[rel].splitlines(True), current.splitlines(True), "a/" + rel, "b/" + rel)
            stats["diffed"] += 1
        for d in sorted(dirs):
            full_d = os.path.join(root, d)
            if not os.path.isdir(full_d):
                continue
            for fn in sorted(os.listdir(full_d)):
                rel = d + "/" + fn
                if fn.endswith(".py") and rel not in base_files and rel not in files:
                    current = read_text(os.path.join(full_d, fn)) or ""
                    body += _diff_lines([], current.splitlines(True), "/dev/null", "b/" + rel)
                    stats["new"] += 1
                    exported_new.add(rel)
        exported = set(files) | exported_new
        others = [c for c in _RUNTIME.get(top + "_changed", []) if c.lstrip("+") not in exported]
        if others:
            header.append("# %s: other changed or added files (not exported): %s" % (top, ", ".join(others)))
    text = "\n".join(header) + "\n" + "".join(body)
    text, redacted = redact_text(text, redact)
    stats["lines"] = text.count("\n")
    stats["redacted"] = redacted
    bad = redact.hits(text, allow_loopback=True)
    if bad:
        return None, stats, bad
    written = safe_write(path, text)
    if written is None:
        return None, stats, ["write_failed"]
    return written, stats, []


SYSTEM_LIB_DIRS = ("/usr/lib/", "/usr/lib64/", "/lib/", "/lib64/", "/usr/local/lib/", "/usr/local/lib64/")
BASE_SONAMES = ("libc.so", "libm.so", "libstdc++.so", "libgcc_s.so", "ld-linux", "libpthread.so", "libdl.so", "librt.so")
DISPLAY_VER = re.compile(rb"(\d+\.\d+\.\d+[\w.+-]*) \(git: ([^)\s]{1,40})\)")
TIMING_TAG = re.compile(rb"\[Batch(?:Put|Upsert)Timing\]")  # both lines patch 04 adds
GLOG_NAME = re.compile(rb"libglog[-\w]*\.so(?:\.\d+)*")
INIT_GLOG = b"InitGoogleLogging"
GLOG_FILE = re.compile(r"\.log\.(INFO|WARNING|ERROR|FATAL)\.\d{8}-\d{6}\.\d+$")


def _pread(f, offset, size):
    f.seek(offset)
    data = f.read(size)
    _IO["read"] += len(data)
    return data


def elf_info(path, search=None):
    """From a 64-bit little-endian ELF, read by offsets (never the whole file): DT_NEEDED names, whether a
    .debug_info section exists, and whether the dynamic string table contains `search`."""
    try:
        with open(path, "rb") as f:
            hdr = f.read(64)
            _IO["read"] += len(hdr)
            if len(hdr) < 64 or hdr[:4] != b"\x7fELF" or hdr[4] != 2 or hdr[5] != 1:
                return None
            e_phoff, e_shoff = struct.unpack_from("<QQ", hdr, 0x20)
            e_phentsize, e_phnum, e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHHHH", hdr, 0x36)
            if e_phnum > 4096 or e_shnum > 65535:
                return None
            ph = _pread(f, e_phoff, e_phentsize * e_phnum)
            loads, dyn = [], None
            for i in range(e_phnum):
                p_type, _flags, p_offset, p_vaddr, _paddr, p_filesz = struct.unpack_from("<IIQQQQ", ph, i * e_phentsize)
                if p_type == 1:
                    loads.append((p_vaddr, p_offset, p_filesz))
                elif p_type == 2:
                    dyn = (p_offset, p_filesz)

            def to_offset(addr):
                for vaddr, offset, size in loads:
                    if vaddr <= addr < vaddr + size:
                        return addr - vaddr + offset
                return None

            needed, entries, strtab, strsz, hit = [], [], None, 0, None
            if dyn and dyn[1] <= (1 << 20):
                dd = _pread(f, dyn[0], dyn[1])
                for j in range(len(dd) // 16):
                    tag, val = struct.unpack_from("<qQ", dd, j * 16)
                    if tag == 0:
                        break
                    entries.append((tag, val))
                for tag, val in entries:
                    if tag == 5:
                        strtab = to_offset(val)
                    elif tag == 10:
                        strsz = val
                if strtab is not None:
                    for tag, val in entries:
                        if tag == 1:
                            needed.append(_pread(f, strtab + val, 256).split(b"\0", 1)[0].decode("utf-8", "replace"))
                    if search is not None and 0 < strsz <= MAX_DYNSTR_BYTES:
                        hit = search in _pread(f, strtab, strsz)
            debug = False
            if e_shoff and e_shnum and e_shstrndx < e_shnum:
                sh = _pread(f, e_shoff, e_shentsize * e_shnum)
                names_off, names_size = struct.unpack_from("<QQ", sh, e_shstrndx * e_shentsize + 0x18)
                names = _pread(f, names_off, min(names_size, 1 << 20))
                for i in range(e_shnum):
                    name_off = struct.unpack_from("<I", sh, i * e_shentsize)[0]
                    if names[name_off : names.find(b"\0", name_off)] == b".debug_info":
                        debug = True
                        break
            return {"needed": needed, "debug": debug, "search_hit": hit}
    except (OSError, struct.error, ValueError):
        return None

MAX_DYNSYM_BYTES = 64 * 1024 * 1024
SITE_PATCH_FILES = ("patch/__init__.py", "patch/platform/__init__.py", "patch/platform/patch_kv_load_failure_reason.py",
                    "patch/platform/patch_prefill_length_validation.py")
BASE_FIELDS = ("kind", "conn", "spec", "eager", "async_sched", "load_async", "tp", "dp", "sched_short", "diag_level")
# a field a baseline token of an older check does not carry: compared as this value
BASE_FIELD_DEFAULTS = {"diag_level": "3"}


def diag_level_of(env):
    """PD_DIAG_LEVEL as the T58 patches read it (the same table as vLLM's pd_diagnostics.diag_level() and Mooncake's
    pd_timing_level.h): unset or empty -> 3 default; exactly 1, 2 or 3 -> env; anything else -> 3 invalid."""
    if env is None:
        return None, "?"
    v = env.get("PD_DIAG_LEVEL")
    if v is None or v == "":
        return 3, "default"
    if v in ("1", "2", "3"):
        return int(v), "env"
    return 3, "invalid"


def dynsym_exports(path):
    """Exported names (GLOBAL or WEAK, defined) of the dynamic symbol table, read through the section headers."""
    try:
        with open(path, "rb") as f:
            hdr = _pread(f, 0, 64)
            if len(hdr) < 64 or hdr[:4] != b"\x7fELF" or hdr[4] != 2 or hdr[5] != 1:
                return None
            e_shoff = struct.unpack_from("<Q", hdr, 0x28)[0]
            e_shentsize, e_shnum = struct.unpack_from("<HH", hdr, 0x3A)
            if not e_shoff or e_shnum > 65535:
                return None
            sh = _pread(f, e_shoff, e_shentsize * e_shnum)
            secs = [struct.unpack_from("<IIQQQQII", sh, i * e_shentsize) for i in range(e_shnum)]
            dyn = [x for x in secs if x[1] == 11]
            if not dyn:
                return None
            off, size, link = dyn[0][4], dyn[0][5], dyn[0][6]
            if size > MAX_DYNSYM_BYTES or secs[link][5] > MAX_DYNSYM_BYTES:
                return None
            syms, strs = _pread(f, off, size), _pread(f, secs[link][4], secs[link][5])
            out = set()
            for i in range(len(syms) // 24):
                st_name, st_info, _o, st_shndx = struct.unpack_from("<IBBH", syms, i * 24)
                if st_shndx != 0 and (st_info >> 4) in (1, 2):
                    name = strs[st_name: strs.find(b"\0", st_name)].decode("utf-8", "replace")
                    if name:
                        out.add(name)
            return out
    except (OSError, struct.error, ValueError, IndexError):
        return None


def p2d_post(p2d, source):
    """The P/D connector after the p2d replay on site. Its bytes depend on the site's own edits, so it is judged by
    structure: the pd_diagnostics import exactly once; every change C01..C28 of the embedded replay_p2d.py present
    in its function (all non-blank lines of the change's text, in order); the replay's own result checks; and the
    embedded extra_checks.py giving the same line as on the p2d replacement file."""
    raw = base64.b64decode(p2d["replay_b64"])
    if hashlib.sha256(raw).hexdigest() != p2d["replay_sha256"]:
        return {"state": "script_mismatch"}
    ns = {"__name__": "t51_embedded_replay_p2d", "__file__": "replay_p2d.py"}
    exec(compile(raw.decode("utf-8"), "replay_p2d.py", "exec"), ns)  # main() is not called: nothing is written
    out = {"marker": source.count(ns["MARKER"]), "equals_replacement": sha16(source.encode()) == p2d["patched_sha16"]}
    if out["marker"] == 0:
        out["state"] = "not_replayed"
        return out
    try:
        tree = ast.parse(source)
    except SyntaxError:
        out["state"] = "does_not_parse"
        return out
    funcs = ns["functions"](tree)
    lines = source.splitlines()
    missing = []
    for c in ns["CHANGES"]:
        if c.get("text") is None:
            # a substitution on one line (C23: the READ goes through _transfer_batch): its replacement's call prefix
            want = [a["repl"].split("(", 1)[0] + "(" for a in c["alts"] if a.get("op") == "sub"]
            sub = True
        else:
            keys = ["{%s}" % a["capture"][1] for a in c["alts"] if a.get("capture")] + ["{line}"]
            want = [l.strip() for l in c["text"].splitlines() if l.strip() and not any(k in l for k in keys)]
            sub = False
        if c.get("module_level") or c["function"] == "<module>":
            scope = lines
        else:
            node = funcs.get(c["function"])
            if node is None:
                missing.append(c["id"])
                continue
            start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
            scope = lines[start - 1: node.end_lineno]
        if sub:
            if not want or not all(any(w in x for x in scope) for w in want):
                missing.append(c["id"])
            continue
        it = iter([x.strip() for x in scope])
        if not all(any(w == x for x in it) for w in want):
            missing.append(c["id"])
    out["changes"] = [len(ns["CHANGES"]) - len(missing), len(ns["CHANGES"])]
    out["missing"] = missing
    try:
        out["problems"] = ns["check_result"](source, source)
    except Exception as exc:
        out["problems"] = ["check_error:%s" % type(exc).__name__]
    xraw = base64.b64decode(p2d["extra_b64"])
    if hashlib.sha256(xraw).hexdigest() != p2d["extra_sha256"]:
        out["extra"] = "script_mismatch"
    else:
        xns = {"__name__": "t51_embedded_extra_checks", "__file__": "extra_checks.py"}
        exec(compile(xraw.decode("utf-8"), "extra_checks.py", "exec"), xns)
        try:
            out["extra"] = xns["extra_line"](source)
        except Exception as exc:
            out["extra"] = "error:%s" % type(exc).__name__
    out["extra_same"] = out["extra"] == p2d["extra_expected"]
    out["state"] = "ok" if out["marker"] == 1 and not missing and not out["problems"] and out["extra_same"] else "BAD"
    return out


def base_token(groups):
    """The configuration of every instance group as one short token (printed before patching, given back with
    --baseline after patching)."""
    rows = [[g["ports"]] + [str(g.get(k)) for k in BASE_FIELDS] for g in groups]
    return base64.urlsafe_b64encode(gzip.compress(json.dumps(rows, separators=(",", ":")).encode(), 9, mtime=0)).decode().rstrip("=")


def base_compare(token, groups):
    try:
        pad = "=" * (-len(token) % 4)
        rows = json.loads(gzip.decompress(base64.urlsafe_b64decode(token + pad)).decode())
    except Exception:
        return ["BASELINE_UNREADABLE"]
    old = {r[0]: dict(zip(BASE_FIELDS, r[1:])) for r in rows}
    for o in old.values():
        for k, dv in BASE_FIELD_DEFAULTS.items():
            o.setdefault(k, dv)
    new = {g["ports"]: {k: str(g.get(k)) for k in BASE_FIELDS} for g in groups}
    diffs = []
    for ports in sorted(set(old) | set(new)):
        if ports not in old or ports not in new:
            diffs.append("%s:%s" % (ports, "new_instances" if ports not in old else "instances_gone"))
            continue
        for k in BASE_FIELDS:
            if old[ports][k] != new[ports][k]:
                diffs.append("%s:%s:%s->%s" % (ports, k, old[ports][k], new[ports][k]))
    return diffs


def pkg_origin(name, pkg_dir, how, cands, dist_names, worker_maps, have_workers, focus, since):
    """Where the scanned copy comes from, whether it is the copy the dist-info describes and the workers load."""
    real = os.path.realpath(pkg_dir)
    out = {"from": how, "candidates": [[redact_path_only(c[0]), c[1]] for c in cands]}
    dist = dist_name = None
    for n in dist_names:
        if find_dist(n) is not None:
            dist = find_dist(n)
            dist_name = n
            break
    out["dist_at"], out["rec_diff"] = "none", None
    if dist is not None:
        try:
            site = os.path.realpath(str(dist.locate_file("")))
        except Exception:
            site = None
        expect = os.path.join(site, name) if site is not None else None
        raw = None
        try:
            raw = dist.read_text("direct_url.json")
        except Exception:
            pass
        if raw:
            try:
                info = json.loads(raw)
                if (info.get("dir_info") or {}).get("editable") and str(info.get("url", "")).startswith("file://"):
                    expect = os.path.join(info["url"][len("file://") :], name)
            except ValueError:
                pass
        same = expect is not None and os.path.realpath(expect) == real
        out["dist_at"] = "same" if same else "other"
        if same and site is not None and os.path.realpath(os.path.join(site, name)) == real:
            rec = {k: v for k, v in installed_record(dist_name).items() if k.startswith(name + "/") and k.endswith(".py")}
            if rec:
                changed = sum(1 for rel, h in rec.items() if file_sha16(os.path.join(site, rel)) != h)
                on_disk, _ = hash_tree(pkg_dir, name)
                extra = sum(1 for rel in on_disk if rel not in rec)
                out["rec_diff"] = "%d/%d" % (changed, extra)
    so = set()
    marker = "/" + name + "/"
    for maps in worker_maps:
        for path in maps:
            i = path.find(marker)
            if i >= 0 and ".so" in os.path.basename(path):
                so.add("same" if os.path.realpath(path[: i + len(marker) - 1]) == real else "other")
    out["so"] = ("other" if "other" in so else "same" if so else "none") if worker_maps else ("noperm" if have_workers else "-")
    text = read_text(os.path.join(pkg_dir, "_version.py"), 4096) or ""
    m = re.search(r"""version\s*(?::\s*str\s*)?=\s*['"]([^'"]+)['"]""", text)
    out["tree_ver"] = m.group(1) if m else "none"
    focus_rel = [r for r in focus if r.startswith(name + "/")]
    out["pyc"] = pyc_evidence(pkg_dir, focus_rel, since)
    out["pyc_other"] = {c[1] + ":" + str(i): pyc_evidence(c[0], focus_rel, since) for i, c in enumerate(cands) if os.path.realpath(c[0]) != real}
    return out


def redact_path_only(path):
    return re.sub(r"^/home/[^/]+", "/home/<USER>", re.sub(r"/data/[^/]+", "/data/<DIR>", path))


def port_field(ref, actual):
    """port=<version>:a/b,...: per embedded patched state (T51, T56), how many of the b files that port changes or
    adds are exactly the ported version."""
    parts = []
    for label, data in sorted((ref.get("alts") or {}).items()):
        files = data.get("files", {})
        parts.append("%s:%d/%d" % (label.split("+")[-1], sum(1 for rel, h in files.items() if actual.get(rel) == h), len(files)))
    return (" port=" + ",".join(parts)) if parts else ""


def alt_facts(ref, actual, res):
    """Per embedded patched state: exact files of how many, and the changed files outside that port (site edits)."""
    facts = []
    for label, data in sorted((ref.get("alts") or {}).items()):
        files = data.get("files", {})
        exact = sum(1 for rel, h in files.items() if actual.get(rel) == h)
        others = sorted((set(res["changed"]) | set(res["added"])) - set(files))
        facts.append({"label": label.split("+")[-1], "exact": exact, "of": len(files), "others": others})
    return facts


def verdict_of(facts, ignore=(), show_zero=False):
    """ok(<version>) when every file of one embedded port is exact (the newest such version), unpatched when no
    file of any port is, BAD(...) otherwise (against the port with the most exact files)."""
    if not facts:
        return "absent"
    full = [f for f in facts if f["of"] and f["exact"] == f["of"]]
    if full:
        f = full[-1]
        local = [r for r in f["others"] if r not in ignore]
        return "ok(%s%s)" % (f["label"], (",site_local=%d" % len(local)) if local or show_zero else "")
    if all(f["exact"] == 0 for f in facts):
        local = [r for r in facts[-1]["others"] if r not in ignore]
        return "unpatched" + ("(site_local=%d)" % len(local) if local or show_zero else "")
    f = max(facts, key=lambda x: (x["exact"] / max(x["of"], 1), x["label"]))
    return "BAD(%s:port=%d/%d,other=%d)" % (f["label"], f["exact"], f["of"], len([r for r in f["others"] if r not in ignore]))


def origin_fields(o):
    pyc = o.get("pyc") or {}
    return " from=%s cands=%d dist_at=%s rec_diff=%s so=%s pyc=%d/%d,match:%d,after_start:%d tree_ver=%s" % (
        o["from"], len(o.get("candidates", [])), o["dist_at"], o["rec_diff"] or "-", o["so"],
        pyc.get("pyc", 0), pyc.get("sources", 0), pyc.get("match", 0), pyc.get("after_start", 0), safe_ver(o["tree_ver"]))


def top_dirs(rels, strip, limit=120):
    counts = {}
    for rel in rels:
        rest = rel[len(strip):] if rel.startswith(strip) else rel
        top = rest.split("/", 1)[0] if "/" in rest else "."
        counts[top] = counts.get(top, 0) + 1
    return short_list(["%s:%d" % (k, v) for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))], limit)


def glog_files(dirs, since):
    n = 0
    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for fn in names:
            if GLOG_FILE.search(fn):
                try:
                    if since is None or os.path.getmtime(os.path.join(d, fn)) >= since:
                        n += 1
                except OSError:
                    pass
    return n


def decide_role(arg_role, start_script, run_kinds):
    file_role = None
    if start_script and os.path.isfile(start_script):
        base = os.path.basename(start_script).lower()
        file_role = "P" if "start_p" in base else ("D" if "start_d" in base else None)
    run_role = run_kinds[0] if len(run_kinds) == 1 else None
    if arg_role:
        role, src = arg_role, "arg"
    elif run_role:
        role, src = run_role, "run"
    elif file_role:
        role, src = file_role, "file"
    else:
        role, src = None, "-"
    conflicts = []
    if run_role:
        if arg_role and arg_role != run_role and arg_role != "proxy":
            conflicts.append("arg:" + arg_role)
        if file_role and file_role != run_role:
            conflicts.append("file:" + file_role)
    return role, src, conflicts


def strict_compare(top, pkg_dir, originals, port_text=None):
    """Byte-for-byte comparison of the scope files with the embedded public sources and, where the T51 port
    changes or adds a file, with the ported text. A file equal to neither gets its exact line ranges against
    the nearer of the two (public lines a1-a2 -> local lines b1-b2)."""
    import difflib

    port_text = port_text or {}
    root = os.path.dirname(pkg_dir)
    out = {}
    for rel in sorted(set(originals) | set(port_text)):
        check_deadline()
        pub, port = originals.get(rel), port_text.get(rel)
        ports = port if isinstance(port, dict) else ({"port": port} if port is not None else {})
        data = read_bytes(os.path.join(root, rel))
        if data is None:
            out[rel] = {"state": "missing" if pub is not None else "port_file_absent"}
            continue
        if pub is not None and data == pub.encode("utf-8"):
            out[rel] = {"state": "equal"}
            continue
        hit = [label for label, text in sorted(ports.items()) if data == text.encode("utf-8")]
        if hit:
            out[rel] = {"state": "port", "label": hit[-1]}
            continue
        b = data.decode("utf-8", "replace").splitlines(True)
        best = None
        for ref, text in [("public", pub)] + [("port", x) for _, x in sorted(ports.items())]:
            if text is None:
                continue
            ops = difflib.SequenceMatcher(None, text.splitlines(True), b, autojunk=False).get_opcodes()
            hunks = [[tag, i1 + 1, i2, j1 + 1, j2] for tag, i1, i2, j1, j2 in ops if tag != "equal"]
            size = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in ops if tag != "equal")
            if best is None or size < best[0]:
                best = (size, ref, hunks)
        out[rel] = {"state": "differs", "ref": best[1], "hunks": best[2][:500], "hunks_total": len(best[2])}
    return out


def strict_field(res, strip):
    counted = {r: v for r, v in res.items() if v["state"] != "port_file_absent"}
    equal = sum(1 for v in counted.values() if v["state"] == "equal")
    port = sum(1 for v in counted.values() if v["state"] == "port")
    missing = sum(1 for v in counted.values() if v["state"] == "missing")
    where = []
    for rel, v in sorted(counted.items()):
        if v["state"] == "differs":
            h = v["hunks"][0] if v["hunks"] else ["", 0, 0, 0, 0]
            short = rel[len(strip):] if rel.startswith(strip) else rel
            where.append("%s%s:%d-%d(+%d)" % (short, "~port" if v.get("ref") == "port" else "", h[3], max(h[3], h[4]), max(0, v["hunks_total"] - 1)))
    head = "%d/%d" % (equal + port, len(counted)) + ("(port:%d)" % port if port else "")
    return head, missing, where


def ms_service_facts(procs, workers, run_env):
    """MindStudio service metric / profiler: installed or not, whether vllm-ascend registers its metric provider
    (setup.py entry point group ms_service_metric.providers), whether vllm-ascend wrote its service-profiling
    symbol file (~/.config/vllm_ascend/service_profiling_symbols.<vllm version>.yaml, at plugin load), and which
    profiling-related variable names the workers have. Files and metadata only; nothing is imported."""
    out = {"metric": None, "profiler": None, "provider": "-", "prof_cfg": 0, "env": []}
    for key, names in (("metric", ("ms_service_metric", "ms-service-metric")), ("profiler", ("ms_service_profiler", "msserviceprofiler", "ms-service-profiler"))):
        for n in names:
            v = dist_version(n)
            if v:
                out[key] = v
                break
    dist = find_dist("vllm-ascend") or find_dist("vllm_ascend")
    if dist is not None:
        try:
            ep = dist.read_text("entry_points.txt") or ""
        except Exception:
            ep = ""
        out["provider"] = "registered" if "[ms_service_metric.providers]" in ep else "not_registered"
    home = (run_env or {}).get("HOME") or os.path.expanduser("~")
    try:
        out["prof_cfg"] = sum(1 for f in os.listdir(os.path.join(home, ".config", "vllm_ascend")) if f.startswith("service_profiling_symbols"))
    except OSError:
        pass
    names = set()
    for wl in workers.values():
        for w in wl[:4]:
            env = procs[w].environ() if w in procs else None
            if env:
                names.update(k for k in env if re.match(r"(SERVICE_PROF|MS_SERVICE|PROF_CONFIG|MSSERVICE)", k))
    out["env"] = sorted(names)
    return out


def glog_evidence(procs, workers, worker_maps, startup_log, run_env, first_start):
    """Where glog output of the vLLM workers goes, with the facts that decide it."""
    out = {}
    # 1. glog flags from the environment of every worker
    keys, mc_dirs, mc_set = set(), set(), 0
    for wl in workers.values():
        for w in wl:
            env = procs[w].environ() if w in procs else None
            if env:
                keys.update(k for k in env if k.startswith("GLOG_"))
                if env.get("MC_LOG_DIR"):
                    mc_set += 1
                    mc_dirs.add(env["MC_LOG_DIR"])
    out["env_keys"] = sorted(keys)
    # Mooncake's transfer engine (78566d45 config.cpp:473-505) initializes glog only when MC_LOG_DIR is set,
    # and then logs to files in that directory instead of stderr
    out["mc_log_dir_workers"] = mc_set
    # 2. does any loaded library reference InitGoogleLogging (only libraries that link glog are read)
    refs, scanned, glog_libs = set(), 0, set()
    seen = set()
    for maps in worker_maps:
        for path in sorted(maps):
            if ".so" not in os.path.basename(path) or path in seen:
                continue
            seen.add(path)
            if scanned >= MAX_LIBS_SCANNED:
                break
            check_deadline()
            base = os.path.basename(path)
            if base.startswith("libglog"):
                glog_libs.add(base)
                continue
            info = elf_info(path)
            scanned += 1
            if info and any(n.startswith("libglog") for n in info["needed"]):
                info2 = elf_info(path, INIT_GLOG)
                if info2 and info2["search_hit"]:
                    refs.add(base)
    out["libs_scanned"] = scanned
    out["init_refs"] = sorted(refs)
    out["glog_libs"] = sorted(glog_libs)
    # 3. glog log files written since vLLM started, in every directory glog would use
    env = run_env or {}
    dirs = sorted(mc_dirs) + [env.get(k) for k in ("GLOG_log_dir", "TEST_TMPDIR", "TMPDIR", "TMP") if env.get(k)] + ["/tmp"]
    out["log_files"] = glog_files(dirs, first_start)
    out["mc_log_dir_files"] = glog_files(sorted(mc_dirs), first_start) if mc_dirs else 0
    # 4. every worker's standard error: kind, and whether it is the same open file as the given vLLM log
    kinds, same = {}, 0
    try:
        log_st = os.stat(startup_log) if startup_log else None
    except OSError:
        log_st = None
    total = 0
    for wl in workers.values():
        for w in wl:
            total += 1
            target = procs[w].fd_target(2)
            k = fd_kind(target, procs, w)
            kinds[k] = kinds.get(k, 0) + 1
            if log_st is not None:
                try:
                    st = os.stat("/proc/%d/fd/2" % w)
                    same += int(st.st_ino == log_st.st_ino and st.st_dev == log_st.st_dev)
                except OSError:
                    pass
    out["worker_fd2"] = kinds
    out["workers"] = total
    out["same_as_log"] = same if log_st is not None else None
    if any(k in keys for k in ("GLOG_logtostdout",)):
        dest = "stdout(env)"
    elif "GLOG_logtostderr" in keys:
        dest = "stderr(env)"
    elif mc_set:
        dest = "files(MC_LOG_DIR,%d_workers,%d_files)" % (mc_set, out["mc_log_dir_files"])
    elif not refs and out["log_files"] == 0:
        dest = "stderr(not_initialized)"
    elif refs and all(r.startswith("libtransfer_engine") for r in refs) and out["log_files"] == 0:
        dest = "stderr(not_initialized:MC_LOG_DIR_unset)"
    elif out["log_files"]:
        dest = "files(%d)" % out["log_files"]
    else:
        dest = "uncertain(init_refs)"
    out["dest"] = dest
    return out


def build_report(args):
    redact = Redactor()
    try:
        redact.add_host(socket.gethostname())
        redact.add_host(socket.gethostname().split(".")[0])
    except Exception:
        pass
    redact.add_host((read_text("/etc/hostname") or "").strip())
    for k, v in os.environ.items():
        if SECRET_NAME.search(k):
            redact.add_secret(v)
        if is_address_key(k):
            redact.add_host(v)

    expected = load_expected()
    detail = {"tool": TOOL_VERSION, "generated_epoch": int(time.time()), "inputs": {}}
    sdirs, user_site = site_layout()
    PROC_SITE.update(site_dirs=sdirs, user_site=user_site, meta_paths=sdirs + ([user_site] if user_site else []))
    lines = []

    errors = []
    tmpl = start = mjson = mstart = launch_sha = ex_launch = ex_tmpl = None
    role_arg = args.role
    missing = []
    procs, btime, apis, api_pids = {}, None, [], set()
    engines, workers, worker_maps, first_start = {}, {}, [], None
    run_env, file_env, cann_home = None, {}, None
    code_digest, search = {}, {"entries": [], "editable": {}, "flags": ""}
    inst_running, inst_files, mismatches, env_mism, source = [], [], [], [], []
    runner = "?"
    role_idx = None
    first_diff = {}
    verdict_facts = {}
    finger = code_d = cfg_digest = "0" * 16
    # ---- inputs
    with Section('inputs', errors):
        files = {
            "run_dp_template": args.run_dp_template,
            "launch_online_dp": args.launch_online_dp,
            "start_script": args.start_script,
            "mooncake_json": args.mooncake_json,
            "start_mooncake": args.start_mooncake,
            "proxy_file": args.proxy_file,
            "startup_log": args.startup_log,
        }
        present = {k: bool(v and os.path.isfile(v)) for k, v in files.items() if v}
        missing = [k for k, v in files.items() if v and not os.path.isfile(v)]
        detail["inputs"] = {"given": sorted(present), "missing": missing}

        tmpl = parse_shell(args.run_dp_template, redact) if args.run_dp_template else None
        start = parse_shell(args.start_script, redact) if args.start_script else None
        mjson = parse_mooncake_json(args.mooncake_json, redact) if args.mooncake_json else None
        mstart = parse_start_mooncake(args.start_mooncake, redact) if args.start_mooncake else None
        launch_sha = file_sha16(args.launch_online_dp) if args.launch_online_dp else None
        ex_launch = expected["examples"].get("launch_online_dp.py")
        ex_tmpl = expected["examples"].get("run_dp_template.sh")

        role_arg = args.role

    # ---- processes
    with Section('processes', errors):
        procs = scan_procs()
        btime = _boot_time()
        apis = [p for p in procs.values() if is_vllm_serve(p.argv)]
        api_pids = set(p.pid for p in apis)
        apis = [p for p in apis if p.ppid not in api_pids]  # forked children share argv
        engines, workers, worker_maps = {}, {}, []
        for api in apis:
            desc = descendants(procs, api.pid)
            engines[api.pid] = [d for d in desc if procs[d].title.startswith("VLLM::EngineCore") or procs[d].comm.startswith("VLLM::EngineCor")]
            workers[api.pid] = [d for d in desc if procs[d].title.startswith("VLLM::Worker") or procs[d].comm.startswith("VLLM::Worker")]
        for api in apis[:2]:
            for w in workers.get(api.pid, [])[:1]:
                m = procs[w].maps_paths()
                if m is not None:
                    worker_maps.append(m)
        starts = [p.start_epoch(btime) for p in apis if p.start_epoch(btime)]
        first_start = min(starts) if starts else None

    # ---- environment of the running vLLM
    with Section('env', errors):
        run_env = apis[0].environ() if apis else None
        if run_env:
            for k, v in run_env.items():
                if SECRET_NAME.search(k):
                    redact.add_secret(v)
                if is_address_key(k):
                    redact.add_host(v)
        file_env = {}
        for info in (start, tmpl):
            if info:
                file_env.update(info["exports"])

    # ---- basics
    with Section('basics', errors):
        exe_same = None
        if apis:
            exe = apis[0].exe()
            if exe:
                exe_same = os.path.realpath(exe) == os.path.realpath(sys.executable)
        cann_home, cann_ver = find_cann(run_env)
        basics = {
            "arch": platform.machine(),
            "py": platform.python_version(),
            "py_same_as_vllm": exe_same,
            "glibc": glibc_version(),
            "torch": dist_version("torch"),
            "torch_npu": dist_version("torch-npu") or dist_version("torch_npu"),
            "transformers": dist_version("transformers"),
            "cann": cann_ver,
        }
        detail["basics"] = {k: (safe_ver(v) if isinstance(v, str) else v) for k, v in basics.items()}
        code_digest["basics"] = sha16(json.dumps({k: v for k, v in basics.items() if k != "py_same_as_vllm"}, sort_keys=True).encode())
        glibc = basics["glibc"]
        role_idx = len(lines)
        lines.append(
            "t51_check=%s role=%s arch=%s py=%s same_py=%s glibc=%s"
            % (TOOL_VERSION, role_arg or "?", basics["arch"], safe_ver(basics["py"]), boolstr(exe_same), safe_ver(glibc))
        )
        lines[-1] += (
            " torch=%s torch_npu=%s transformers=%s cann=%s"
            % (safe_ver(basics["torch"]), safe_ver(basics["torch_npu"]), safe_ver(basics["transformers"]), safe_ver(cann_ver))
        )

        search = process_search_path(apis[0] if apis else None, run_env)
        ordered = [e for e, _how in search["entries"]]
        PROC_SITE["meta_paths"] = ordered + [d for d in PROC_SITE["meta_paths"] if d not in ordered]
        _DISTS.clear()
        detail["search"] = {"flags": search["flags"], "entries": [[redact_path_only(e), how] for e, how in search["entries"]], "editable": sorted(search["editable"])}
    # ---- vLLM
    with Section('vllm', errors):
        vdir, vhow, vcands = resolve_package("vllm", search)
        if vdir:
            kind, _ = install_kind("vllm")
            root = os.path.dirname(vdir)
            _RUNTIME["vllm"] = vdir
            res, actual = compare_tree("vllm", vdir, "vllm", expected["vllm"], set(expected["vllm"]["focus"]), first_start)
            _RUNTIME["vllm_changed"] = sorted(res["changed"]) + ["+" + a for a in res["added"]]
            diag = os.path.exists(os.path.join(vdir, "v1", "metrics", "pd_diagnostics.py"))
            detail["vllm"] = {"version": dist_version("vllm"), "install": kind, "git": git_head(root), "dir": redact.scrub_path(vdir), **res}
            labels = change_labels(res, "vllm/", res["state"])
            origin = pkg_origin("vllm", vdir, vhow, vcands, ("vllm",), worker_maps, any(workers.values()), expected["vllm"]["focus"], first_start)
            detail["vllm"]["origin"] = origin
            lines.append(
                "vllm=%s src=%s git=%s state=%s changed=%d missing=%d added=%d diag=%s after_start=%d"
                % (safe_ver(dist_version("vllm")), kind, git_head(root) or "none", res["state"], len(res["changed"]), len(res["missing"]), len(res["added"]), boolstr(diag), len(res["changed_after_start"]))
                + origin_fields(origin)
                + port_field(expected["vllm"], actual)
            )
            if labels:
                lines.append("vllm_changes=" + short_list(labels, 200))
            code_digest["vllm"] = sha16(json.dumps(actual, sort_keys=True).encode())
            verdict_facts["vllm"] = alt_facts(expected["vllm"], actual, res)
        else:
            lines.append("vllm=absent")

    # ---- vllm-ascend
    with Section('ascend', errors):
        adir, ahow, acands = resolve_package("vllm_ascend", search)
        if adir:
            kind, _ = install_kind("vllm-ascend") if dist_version("vllm-ascend") else install_kind("vllm_ascend")
            root = os.path.dirname(adir)
            _RUNTIME["vllm_ascend"] = adir
            res, actual = compare_tree("vllm_ascend", adir, "vllm_ascend", expected["vllm_ascend"], set(expected["vllm_ascend"]["focus"]), first_start)
            _RUNTIME["vllm_ascend_changed"] = sorted(res["changed"]) + ["+" + a for a in res["added"]]
            diag = os.path.exists(os.path.join(adir, "distributed", "kv_transfer", "kv_pool", "ascend_store", "diagnostics.py"))
            ver = dist_version("vllm-ascend") or dist_version("vllm_ascend")
            detail["vllm_ascend"] = {"version": ver, "install": kind, "git": git_head(root), "dir": redact.scrub_path(adir), **res}
            labels = change_labels(res, "vllm_ascend/", res["state"])
            origin = pkg_origin("vllm_ascend", adir, ahow, acands, ("vllm-ascend", "vllm_ascend"), worker_maps, any(workers.values()), expected["vllm_ascend"]["focus"], first_start)
            detail["vllm_ascend"]["origin"] = origin
            lines.append(
                "ascend=%s src=%s git=%s state=%s changed=%d missing=%d added=%d diag=%s after_start=%d"
                % (safe_ver(ver), kind, git_head(root) or "none", res["state"], len(res["changed"]), len(res["missing"]), len(res["added"]), boolstr(diag), len(res["changed_after_start"]))
                + origin_fields(origin)
                + port_field(expected["vllm_ascend"], actual)
            )
            tree_txt = None
            if res["added"] or res["missing"]:
                tree_txt = "tree=changed:%s,added:%s,missing:%s" % (
                    top_dirs(sorted(res["changed"]), "vllm_ascend/") or "-", top_dirs(res["added"], "vllm_ascend/") or "-", top_dirs(res["missing"], "vllm_ascend/") or "-")
            if labels:
                lines.append("ascend_changes=" + short_list(labels, 200) + (" " + tree_txt if tree_txt else ""))
            elif tree_txt:
                lines.append("ascend_" + tree_txt)
            site_p = []
            for rel in SITE_PATCH_FILES:
                full = os.path.join(adir, rel)
                site_p.append("%s=%s" % (rel, file_sha16(full) if os.path.isfile(full) else "absent"))
            detail["vllm_ascend"]["site_patch_files"] = site_p
            lines.append("site_patches " + " ".join(site_p))
            code_digest["ascend"] = sha16(json.dumps(actual, sort_keys=True).encode())
            verdict_facts["ascend"] = alt_facts(expected["vllm_ascend"], actual, res)
        else:
            lines.append("ascend=absent")

    # ---- strict: the scope files byte for byte against the embedded public sources
    with Section('strict', errors):
        parts, strict_all = [], {}
        for top, label in (("vllm", "vllm"), ("vllm_ascend", "ascend")):
            pkg = _RUNTIME.get(top)
            originals = (expected.get("export") or {}).get(top) or {}
            if not pkg or not originals:
                parts.append("%s=-" % label)
                continue
            res = strict_compare(top, pkg, originals, (expected.get("port_text") or {}).get(top))
            strict_all[top] = res
            eq, miss, where = strict_field(res, top + "/")
            parts.append("%s=%s%s" % (label, eq, (",missing:%d" % miss) if miss else ""))
            if where:
                parts.append("%s_at=%s" % (label, short_list(where, 150)))
        detail["strict"] = strict_all
        lines.append("strict " + " ".join(parts))

    # ---- the P/D connector, replayed on site: by structure (p2d_post)
    p2d_res = None
    with Section('p2d', errors):
        adir_ = _RUNTIME.get("vllm_ascend")
        versions = expected.get("p2d_versions") or ({"t51": expected["p2d"]} if expected.get("p2d") else {})
        if adir_ and versions:
            data = read_bytes(os.path.join(adir_, "distributed", "kv_transfer", "kv_p2p", "mooncake_connector.py"))
            if data is None:
                p2d_res = {"state": "absent"}
            else:
                # every embedded replay version (T51, T56); the newest one the file fully matches wins
                runs = [dict(p2d_post(spec, data.decode("utf-8", "replace")), label=label) for label, spec in sorted(versions.items())]
                okv = [r for r in runs if r.get("state") == "ok"]
                if okv:
                    p2d_res = okv[-1]
                elif all(r.get("state") == "not_replayed" for r in runs):
                    p2d_res = runs[-1]
                else:
                    # judged against the version it is closest to: most of its changes found, then the newer one
                    def closeness(r):
                        ch = r.get("changes") or (0, 1)
                        return (ch[0] / max(ch[1], 1), -len(r.get("missing") or []), r["label"])
                    p2d_res = max((r for r in runs if r.get("state") != "not_replayed"), key=closeness)
                p2d_res["versions"] = {r["label"]: r.get("state") for r in runs}
            detail["p2d"] = p2d_res
            if p2d_res.get("changes"):
                txt = "p2d=%s(%s,changes=%d/%d,missing=%s,struct=%s,extra=%s,replacement_file=%s)" % (
                    p2d_res["state"], p2d_res.get("label", "-"), p2d_res["changes"][0], p2d_res["changes"][1], "+".join(p2d_res["missing"]) or "none",
                    "ok" if not p2d_res["problems"] else "problems:%d" % len(p2d_res["problems"]),
                    "same" if p2d_res["extra_same"] else "DIFFERS", boolstr(p2d_res["equals_replacement"]))
            else:
                txt = "p2d=%s" % p2d_res["state"]
            idx = [i for i, l in enumerate(lines) if l.startswith("strict ")]
            if idx:
                lines[idx[-1]] += " " + txt
            else:
                lines.append(txt)

    # ---- Mooncake
    with Section('mooncake', errors):
        mc = check_mooncake(expected, worker_maps, redact, search)
        detail["mooncake"] = mc
        if mc.get("state") == "absent":
            lines.append("mooncake=absent")
        else:
            lines.append(
                "mooncake=%s state=%s nearest=%s so_diff=%d changed_since_install=%d timing=%s store=%s glog=%s loaded=%s store_lib=%s"
                % (
                    safe_ver(mc.get("version")) + ("(npu)" if (mc.get("dist") or "").endswith("-npu") else ""),
                    mc.get("state"),
                    mc.get("nearest", "-"),
                    len(mc.get("so_diff", [])),
                    len(mc.get("changed_since_install", [])),
                    len(mc.get("timing_tags", [])),
                    mc.get("store_is", "?") + ("(%s)" % mc["replacement"] if mc.get("replacement") else ""),
                    mc.get("glog") or "?",
                    len(mc.get("loaded", [])) if worker_maps else "noperm" if apis else "-",
                    "+".join(sorted(set(os.path.dirname(x).rsplit("/", 1)[-1] + "/" + os.path.basename(x) for x in mc.get("loaded", []) if "libmooncake_store" in x))) or "-",
                )
            )
            code_digest["mooncake"] = sha16(json.dumps(mc.get("files", {}), sort_keys=True).encode())
            selves = mc.get("store_elf", {})
            needed = sorted(set(n for info in selves.values() for n in info["needed"] if not n.startswith(BASE_SONAMES)))
            lines.append(
                "mooncake_build git=%s version=%s wheel_tag=%s store_debug=%s glog_from=%s store_needed=%s"
                % (
                    ",".join(mc.get("git", [])) or "none",
                    short_list(sorted(set(d.split(" (git:")[0] for d in mc.get("display", []))), 40) or "none",
                    short_list(mc.get("wheel_tag", []), 60) or "none",
                    "/".join(sorted(set(boolstr(i["debug"]) for i in selves.values()))) or "-",
                    "+".join(mc.get("glog_from", [])) or "-",
                    short_list(needed, 260) or "-",
                )
            )
            sib = mc.get("siblings") or {}
            if sib:
                lines[-1] += " siblings=" + ",".join(
                    "%s:%s" % (n.replace(".so", ""), "unreadable" if v is None else "%d/%s%s" % (v[0], v[1][:8], "" if v[2] else "!lab")) for n, v in sorted(sib.items()))

        build = check_build(cann_home)
        detail["build"] = build
        have = [t for t, ok in build["tools"].items() if ok]
        miss_h = [h for h, ok in build["headers"].items() if not ok]
        cdev = build["cann_dev"]
        ms = ms_service_facts(procs, workers, run_env)
        detail["ms_service"] = ms
        lines.append(
            "build_tools=%s headers_missing=%s cann_dev=%s ms_metric=%s ms_profiler=%s metric_provider=%s prof_cfg=%d prof_env=%s"
            % (short_list(have, 60) if have else "none", short_list(miss_h, 80) if miss_h else "none", ("/".join(k for k, v in cdev.items() if v) or "none") if cdev is not None else "no_cann",
               safe_ver(ms["metric"] or "absent"), safe_ver(ms["profiler"] or "absent"), ms["provider"], ms["prof_cfg"], ",".join(ms["env"]) or "none")
        )

    # ---- instances: running
    with Section('inst_run', errors):
        inst_running = []
        for api in apis:
            opts = parse_cli_args(serve_tokens(api.argv))
            d = describe_instance(opts, redact)
            d["workers"] = len(workers.get(api.pid, []))
            d["engine"] = len(engines.get(api.pid, []))
            env = api.environ()
            d["env"] = env_view(env)
            if env and env.get("MOONCAKE_CONFIG_PATH"):
                mpath = env["MOONCAKE_CONFIG_PATH"]
                d["mooncake_json_same"] = bool(args.mooncake_json) and os.path.realpath(mpath) == os.path.realpath(args.mooncake_json)
                mj = parse_mooncake_json(mpath, redact)
                d["prefer_same_node"] = None if mj is None else mj.get("prefer_alloc_in_same_node", "default_true")
            inst_running.append(d)
        detail["instances_running"] = inst_running
        run_kinds = sorted(set(d.get("kind") for d in inst_running if d.get("kind") in ("P", "D")))
        role, role_src, role_conflicts = decide_role(args.role, args.start_script, run_kinds)
        detail["role"] = {"role": role, "from": role_src, "conflicts": role_conflicts}
        if role_idx is not None:
            lines[role_idx] = lines[role_idx].replace("role=%s " % (role_arg or "?"), "role=%s%s " % (role or "?", (" role_conflict=" + ",".join(role_conflicts)) if role_conflicts else ""), 1)

    # ---- instances: from files
    with Section('inst_file', errors):
        inst_files = []
        serve = find_serve_in_shell(tmpl) if tmpl else None
        if serve is not None:
            opts = parse_cli_args(serve)
            base = describe_instance(opts, redact)
            # site template: options come through variables ($VAR on the serve line, set elsewhere in the file);
            # what a variable after the last literal flag may set is unknown on the file side, not a mismatch
            var_at = [i for i, t in enumerate(serve) if "$" in t]
            if var_at:
                last_var = max(var_at)

                def _last(names):
                    hit = [i for i, t in enumerate(serve) if t.split("=", 1)[0] in names]
                    return max(hit) if hit else -1
                if _last(("--async-scheduling", "--no-async-scheduling")) < last_var:
                    base["async_sched"] = None
                if _last(("--enforce-eager",)) < last_var:
                    base["eager"] = None
                if _last(("--speculative-config",)) < 0:
                    base["spec"] = None
                if _last(("--kv-transfer-config",)) < 0:
                    base.update(kind=None, conn=None, load_async=None)
            base["_vars"] = len(var_at)
            call = find_launch_call(start) if start else None
            if call:
                n_local = int(call.get("dp-size-local", -1)) if str(call.get("dp-size-local", "-1")).lstrip("-").isdigit() else -1
                dp_size = call.get("dp-size")
                if n_local == -1 and str(dp_size).isdigit():
                    n_local = int(dp_size)
                rank0 = int(call.get("dp-rank-start", 0)) if str(call.get("dp-rank-start", "0")).isdigit() else 0
                port0 = int(call.get("vllm-start-port", 9000)) if str(call.get("vllm-start-port", "9000")).isdigit() else None
                if call.get("dp-address"):
                    redact.add_host(call["dp-address"])
                for i in range(max(n_local, 0)):
                    d = dict(base)
                    d.update(
                        port=str(port0 + i) if port0 is not None else "?",
                        dp=str(dp_size),
                        dp_rank=str(rank0 + i),
                        tp=str(call.get("tp-size", "1")),
                        dp_addr=redact.placeholder(call.get("dp-address")) if call.get("dp-address") else base.get("dp_addr"),
                    )
                    inst_files.append(d)
            else:
                inst_files.append(base)
        detail["instances_files"] = inst_files

    # ---- compare files vs running, by port
    with Section('compare', errors):
        mismatches, first_diff = [], {}
        fields = ("kind", "tp", "dp", "dp_rank", "conn", "load_async", "spec", "spec_eager", "eager", "async_sched", "policy", "ep", "sched_cls")
        for d in list(inst_running) + list(inst_files):
            d["sched_cls"] = (d.get("sched") or {}).get("cls", "?").rsplit(".", 1)[-1]
        by_port = {str(d.get("port")): d for d in inst_files}
        for d in inst_running:
            f = by_port.get(str(d.get("port")))
            if f is None:
                mismatches.append("port%s:not_in_files" % d.get("port"))
                continue
            for k in fields:
                fv, rv = f.get(k), d.get(k)
                if fv is None or unresolved(fv):
                    continue
                if str(fv) != str(rv):
                    mismatches.append("port%s:%s" % (d.get("port"), k))
                    first_diff.setdefault(k, "%s:%s/%s" % (k, boolstr(fv) if isinstance(fv, bool) else fv, boolstr(rv) if isinstance(rv, bool) else rv))
        if inst_files and inst_running and len(inst_files) != len(inst_running):
            mismatches.append("count:%d_files_%d_running" % (len(inst_files), len(inst_running)))
        env_mism = []
        if run_env is not None:
            for k in ENV_KEYS:
                if k in file_env and k in run_env and k in ENV_VALUE_KEYS and file_env[k] != run_env[k]:
                    env_mism.append(k)
                elif (k in file_env) != (k in run_env) and k in ("VLLM_USE_V2_MODEL_RUNNER", "VLLM_LOGGING_CONFIG_PATH", "MOONCAKE_CONFIG_PATH", "GLOG_logtostderr"):
                    env_mism.append(k)
        detail["file_vs_running"] = {"instances": mismatches, "env": env_mism, "first_values_file_run": first_diff}

    # ---- instance lines, grouped
    with Section('instance_lines', errors):
        source = inst_running if inst_running else inst_files
        groups = {}
        for d in source:
            sch = d.get("sched") or {}
            if sch and "path" not in sch:
                sch["path"], sch["defined_at"] = schedule_path(sch["cls"])
            d["sched_short"] = "%s(%s)" % (sch.get("cls", "?").rsplit(".", 1)[-1], sch.get("why", "?"))
            d["sched_path"] = sch.get("path", "?") + (":" + sch["defined_at"] if sch.get("path") == "rewritten" else "")
            d["sched_keys"] = ",".join(k for k, v in sorted((sch.get("keys") or {}).items()) if v) or "none"
            key = tuple((k, str(d.get(k))) for k in ("kind", "tp", "pp", "dp", "ep", "conn", "load_async", "layerwise", "policy", "spec", "spec_eager", "eager", "async_sched", "workers", "prefer_same_node", "sfa_d2h", "sched_short", "sched_path", "sched_keys"))
            groups.setdefault(key, []).append(d)
        for key, members in sorted(groups.items(), key=lambda kv: str(kv[0])):
            d = members[0]
            ports = [m.get("port") for m in members if str(m.get("port", "")).isdigit()]
            ranks = [m.get("dp_rank") for m in members if str(m.get("dp_rank", "")).isdigit()]
            lines.append(
                "inst[%s n=%d] kind=%s ports=%s tp=%s dp=%s ranks=%s pp=%s ep=%s workers=%s conn=%s load_async=%s layerwise=%s policy=%s spec=%s spec_eager=%s eager=%s async=%s same_node=%s sfa_d2h=%s sched=%s sched_path=%s sched_keys=%s"
                % (
                    "run" if inst_running else "file",
                    len(members),
                    d.get("kind"),
                    compact_ints(ports) if ports else "?",
                    d.get("tp"),
                    d.get("dp"),
                    compact_ints(ranks) if ranks else "?",
                    d.get("pp"),
                    d.get("ep"),
                    d.get("workers", "-"),
                    d.get("conn"),
                    boolstr(d.get("load_async")),
                    boolstr(d.get("layerwise")),
                    d.get("policy") or "default",
                    d.get("spec"),
                    boolstr(d.get("spec_eager")),
                    d.get("eager"),
                    boolstr(d.get("async_sched")),
                    same_node_str(d.get("prefer_same_node") if d.get("prefer_same_node") is not None else (mjson.get("prefer_alloc_in_same_node", "default_true") if mjson else None)),
                    boolstr(d.get("sfa_d2h")),
                    d.get("sched_short"),
                    d.get("sched_path"),
                    d.get("sched_keys"),
                )
            )
        detail["scheduler_sources_parsed"] = scheduler_table().get("_parsed", [])
        if not source:
            lines.append("inst=none_running_and_no_template")
        if inst_running:
            bgroups = []
            for key, members in sorted(groups.items(), key=lambda kv: str(kv[0])):
                ports = [m.get("port") for m in members if str(m.get("port", "")).isdigit()]
                g = dict(members[0])
                g["ports"] = compact_ints(ports) if ports else "?"
                g["eager"] = str(g.get("eager"))
                g["async_sched"] = boolstr(g.get("async_sched"))
                g["load_async"] = boolstr(g.get("load_async"))
                g["diag_level"] = str(diag_level_of(run_env)[0] if run_env is not None else "?")
                bgroups.append(g)
            token = base_token(bgroups)
            detail["baseline_token"] = token
            if getattr(args, "baseline", None):
                diffs = base_compare(args.baseline, bgroups)
                lines.append("baseline=same" if not diffs else "BASELINE_DIFF " + short_list(diffs, 300))
            # without --baseline the token is printed at the end of the fingerprint line

    # ---- runtime layout
    with Section('run', errors):
        runner = "?"
        ev = run_env if run_env is not None else file_env
        if ev and "VLLM_USE_V2_MODEL_RUNNER" in ev:
            runner = ("V2" if ev["VLLM_USE_V2_MODEL_RUNNER"] not in ("0", "false", "False") else "V1") + "(env)"
        log = scan_log(args.startup_log) if args.startup_log else None
        if log and log.get("runner_lines"):
            runner = "/".join(log["runner_lines"])
        elif runner == "?":
            # d4ad49796 picks V1 unless VLLM_USE_V2_MODEL_RUNNER is set (patch_use_v2_model_runner.py),
            # and logs nothing for V1
            runner = "V1(default,no_v2_line_in_log)" if log and log.get("readable") else "V1(default)"
        detail["startup_log"] = log
        pid1 = procs.get(1)
        pid1_kind = "?"
        if pid1 is not None:
            if pid1.pid in api_pids:
                pid1_kind = "vllm"
            else:
                base = os.path.basename(pid1.argv[0]) if pid1.argv else pid1.comm
                pid1_kind = KNOWN_PID1.get(base, "python" if base.startswith("python") else "other")
        launchers = set()
        known = set(os.path.basename(v) for v in (args.run_dp_template, args.launch_online_dp, args.start_script) if v)
        known |= {"run_dp_template.sh", "launch_online_dp.py", "start_p.sh", "start_d.sh"}
        for api in apis:
            for a in ancestors(procs, api.pid):
                for tok in a.argv[:3]:
                    if os.path.basename(tok) in known:
                        launchers.add(os.path.basename(tok))
        out_kind = err_kind = werr_kind = "?"
        free = None
        if apis:
            api = apis[0]
            t1, t2 = api.fd_target(1), api.fd_target(2)
            out_kind = fd_kind(t1, procs, api.pid)
            err_kind = fd_kind(t2, procs, api.pid)
            if workers.get(api.pid):
                w = procs[workers[api.pid][0]]
                werr_kind = fd_kind(w.fd_target(2), procs, w.pid)
            target_dir = os.path.dirname(t1) if t1 and t1.startswith("/") and not t1.startswith("/dev") else "/proc/%d/cwd" % api.pid
            free = free_gib(target_dir)
            detail["fds"] = {"api_stdout": redact.scrub_path(t1 if t1 and t1.startswith("/") else None), "api_stderr": redact.scrub_path(t2 if t2 and t2.startswith("/") else None)}
        logcfg = (run_env or {}).get("VLLM_LOGGING_CONFIG_PATH")
        genv = run_env if run_env is not None else file_env
        parts = []
        for k in ("GLOG_logtostderr", "GLOG_alsologtostderr", "GLOG_stderrthreshold", "GLOG_minloglevel"):
            if genv.get(k) is not None:
                parts.append("%s=%s" % (k[5:], genv[k][:8]))
        if genv.get("GLOG_log_dir"):
            parts.append("log_dir=set")
        glog_env = ",".join(parts) or "unset"
        lines.append(
            "run pid1=%s launcher=%s stdout=%s stderr=%s worker_stderr=%s logcfg=%s glog_env=%s free_gib=%s runner=%s env_read=%s req_id_norand=%s diag_level=%s"
            % (
                pid1_kind,
                "+".join(sorted(launchers)) or "-",
                out_kind,
                err_kind,
                werr_kind,
                "set" if logcfg else ("unset" if run_env is not None else "?"),
                glog_env,
                free if free is not None else "?",
                runner,
                boolstr(run_env is not None) if apis else "-",
                ("unset" if run_env.get("VLLM_DISABLE_REQUEST_ID_RANDOMIZATION") is None else safe_ver(run_env["VLLM_DISABLE_REQUEST_ID_RANDOMIZATION"])[:8]) if run_env is not None else "?",
                "%s(%s)" % diag_level_of(run_env) if run_env is not None else "?",
            )
        )
        gdirs = ["/tmp"]
        if genv.get("GLOG_log_dir"):
            gdirs.insert(0, genv["GLOG_log_dir"])
        n_glog_files = glog_files(gdirs, first_start)
        detail["glog_files_since_start"] = n_glog_files
        if log:
            c = log.get("counts", {})
            lines.append(
                "log readable=%s pd_diag=%d events=%s worker_runner=%s batch_put_timing=%d glog_before_init=%d glog_files=%d"
                % (boolstr(log.get("readable")), c.get("pd_diag", 0), short_list(log.get("events", []), 90), short_list(log.get("worker_runner", []), 60), c.get("batch_put_timing", 0), c.get("glog_before_init", 0), n_glog_files)
            )
        ge = glog_evidence(procs, workers, worker_maps, args.startup_log, run_env, first_start)
        detail["glog"] = ge
        fd2 = ",".join("%s:%d" % kv for kv in sorted(ge["worker_fd2"].items())) or "-"
        lines.append(
            "glog lib=%s env=%s mc_log_dir=%d init_refs=%s(scanned=%d) log_files=%d worker_fd2=%s same_as_log=%s dest=%s"
            % (
                "+".join(ge["glog_libs"]) or "-",
                ",".join(k[5:] for k in ge["env_keys"]) or "unset",
                ge["mc_log_dir_workers"],
                short_list(ge["init_refs"], 80) if ge["init_refs"] else "none",
                ge["libs_scanned"],
                ge["log_files"],
                fd2,
                "-" if ge["same_as_log"] is None else "%d/%d" % (ge["same_as_log"], ge["workers"]),
                ge["dest"],
            )
        )
        files_line = []
        if args.launch_online_dp:
            files_line.append("launch=%s" % ("public" if launch_sha and launch_sha == ex_launch else ("missing" if launch_sha is None else "edited")))
        if tmpl is not None:
            files_line.append("template=%s" % ("public" if tmpl["sha"] == ex_tmpl else "edited"))
            files_line.append("redirect=%s" % (short_list(sorted(set(x for r in tmpl["redirect"] for x in r)), 40) or "none"))
        if mjson is not None:
            files_line.append("mooncake_json=%s" % ("keys:%d" % len(mjson.get("keys", [])) if "error" not in mjson else mjson["error"]))
            if "protocol" in mjson:
                files_line.append("protocol=%s" % mjson["protocol"])
        if mstart is not None:
            files_line.append("master_cmd=%s" % ("found" if mstart["found"] else "not_found"))
        if tmpl is not None:
            n_serve = sum(1 for t in tmpl.get("commands", ()) if is_vllm_serve(t) or ("vllm" in t and "serve" in t))
            if n_serve != 1:
                files_line.append("serve_lines=%d" % n_serve)
        if mismatches or env_mism:
            files_line.append("file_vs_run=%s" % short_list(mismatches + env_mism, 100))
            if first_diff:
                files_line.append("file/run=%s" % short_list([first_diff[k] for k in fields if k in first_diff], 120))
        elif inst_files and inst_running:
            files_line.append("file_vs_run=same")
        if missing:
            files_line.append("missing_inputs=%s" % ",".join(missing))
        if files_line:
            lines.append("files " + " ".join(files_line))

    # ---- proxy
    with Section('proxy', errors):
        if args.proxy_file:
            psha = file_sha16(args.proxy_file)
            pref = expected["proxy"]
            pstate = "main" if psha == pref["sha"] else ("missing" if psha is None else "edited")
            pfuncs = []
            if pstate == "edited":
                data = read_bytes(args.proxy_file)
                h = func_hashes(data) if data else None
                if h:
                    pfuncs = sorted(k for k in set(h) | set(pref["funcs"]) if h.get(k) != pref["funcs"].get(k))
            pprocs = [p for p in procs.values() if any(os.path.basename(a) == os.path.basename(args.proxy_file) for a in p.argv[:3])]
            pp = set(p.pid for p in pprocs)
            pprocs = [p for p in pprocs if p.ppid not in pp]
            workers_opt = set()
            for p in pprocs:
                o = parse_cli_args(p.argv)
                workers_opt.add(str(o.get("workers", "default")))
            lines.append(
                "proxy file=%s funcs_changed=%s fastapi=%s httpx=%s uvicorn=%s running=%d workers=%s"
                % (pstate, short_list(pfuncs, 80) if pfuncs else "-", safe_ver(dist_version("fastapi")), safe_ver(dist_version("httpx")), safe_ver(dist_version("uvicorn")), len(pprocs), "/".join(sorted(workers_opt)) or "-")
            )
            detail["proxy"] = {"state": pstate, "funcs_changed": pfuncs, "running": len(pprocs)}
            code_digest["proxy"] = psha or "none"

    # ---- verdict per component, for the check after patching (guide A step 8)
    with Section('verdict', errors):
        vv = verdict_of(verdict_facts.get("vllm"))
        conn_rel = "vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py"
        av = verdict_of(verdict_facts.get("ascend"), ignore=(conn_rel,), show_zero=True)
        if p2d_res is None:
            pv = "-"
        elif p2d_res.get("state") == "ok":
            pv = "ok(%s)" % p2d_res.get("label", "-")
        elif p2d_res.get("state") == "not_replayed":
            pv = "unpatched"
        else:
            pv = "BAD(%s:%s)" % (p2d_res.get("label", "-"), ",".join(x for x in (
                ("missing:" + "+".join(p2d_res.get("missing") or [])) if p2d_res.get("missing") else "",
                "struct" if p2d_res.get("problems") else "", "" if p2d_res.get("extra_same", True) else "extra",
                p2d_res.get("state") if p2d_res.get("state") not in ("BAD",) else "") if x))
        mc_is = (detail.get("mooncake") or {}).get("store_is")
        mlabel = (detail.get("mooncake") or {}).get("replacement")
        mv = {"replacement": "ok(%s)" % mlabel if mlabel else "ok", "site_original": "not_replaced", None: "-"}.get(mc_is, "BAD(%s)" % mc_is)
        detail["verdict"] = {"vllm": vv, "ascend": av, "p2d": pv, "mooncake": mv}
        lines.append("verdict vllm=%s ascend=%s p2d=%s mooncake=%s" % (vv, av, pv, mv))

    # ---- fingerprint: code state + configuration shape (no per-machine ranks or ports)
    with Section('fingerprint', errors):
        shape = []
        for d in source:
            shape.append({k: str(d.get(k)) for k in ("kind", "tp", "pp", "dp", "ep", "conn", "load_async", "layerwise", "policy", "spec", "spec_eager", "eager", "async_sched", "prefer_same_node", "sfa_d2h", "sched_short", "sched_path", "sched_keys")})
        shape.sort(key=lambda x: json.dumps(x, sort_keys=True))
        cfg_digest = sha16(json.dumps({"inst": shape, "runner": runner, "mjson": {k: v for k, v in (mjson or {}).items() if k not in ("sha",)}}, sort_keys=True).encode())
        code_d = sha16(json.dumps(code_digest, sort_keys=True).encode())
        finger = sha16((code_d + cfg_digest).encode())
        detail["fingerprint"] = {"all": finger, "code": code_d, "config": cfg_digest, "parts": code_digest}

    timeouts = [e.split(":", 1)[0] for e in errors if ":Timeout@" in e]
    if timeouts:
        lines.append("timeout=%ds sections=%s" % (int(args.max_seconds), ",".join(timeouts)))
        errors = [e for e in errors if ":Timeout@" not in e]
    if errors:
        lines.append("errors=" + short_list(errors, 200))
    if len(lines) > MAX_SUMMARY_LINES - 2:
        keep = lines[: MAX_SUMMARY_LINES - 3]
        keep.append("truncated=%d_lines_see_detail" % (len(lines) - len(keep)))
        lines = keep
    return lines, detail, finger, code_d, cfg_digest, redact


def lower_priority():
    """nice 19 and idle IO class for this process only."""
    out = {}
    try:
        os.nice(19)
        out["nice"] = os.nice(0)
    except OSError:
        out["nice"] = "?"
    out["io"] = "unchanged"
    try:
        nr = {"aarch64": 30, "x86_64": 251}.get(platform.machine())
        if nr:
            import ctypes

            fn = ctypes.CDLL(None, use_errno=True).syscall
            fn.argtypes = [ctypes.c_long] * 4
            fn.restype = ctypes.c_long
            if fn(nr, 1, 0, 3 << 13) == 0:  # ioprio_set(IOPRIO_WHO_PROCESS, self, IOPRIO_CLASS_IDLE)
                out["io"] = "idle"
    except Exception:
        pass
    return out


def safe_write(path, text):
    """Create a new file (never follow a symlink, never overwrite); another name is used if it exists."""
    base, ext = os.path.splitext(path)
    for n in range(20):
        p = path if n == 0 else "%s.%d%s" % (base, n, ext)
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            continue
        except OSError:
            return None
        with os.fdopen(fd, "w") as f:
            f.write(text)
        return p
    return None


def main(argv=None):
    missing = [flag for flag, attr in REQUIRED_FLAGS if not getattr(sys.flags, attr, 0)]
    if missing:
        sys.stdout.write("t51_check=%s REFUSED missing_flags=%s run: python3 -I -S -B <this script> ... (nothing read, nothing written)\n" % (TOOL_VERSION, ",".join(missing)))
        return 3
    ap = argparse.ArgumentParser(description="T51 glm5.3 read-only environment check")
    ap.add_argument("--run-dp-template")
    ap.add_argument("--launch-online-dp")
    ap.add_argument("--start-script", help="start_p.sh or start_d.sh")
    ap.add_argument("--mooncake-json")
    ap.add_argument("--start-mooncake")
    ap.add_argument("--proxy-file")
    ap.add_argument("--startup-log")
    ap.add_argument("--role", choices=("P", "D", "proxy"))
    ap.add_argument("--baseline", help="the baseline_token= value printed by the run before patching (guide A step 2)")
    ap.add_argument("--out-dir", default="/tmp", help="where the detail and export files go: /tmp or a directory below it")
    ap.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS, help="overall time limit (default 300)")
    ap.add_argument("--max-read-mib", type=float, default=DEFAULT_MAX_READ_MIB, help="total read budget in MiB (default 1024)")
    ap.add_argument(
        "--export-diff",
        action="store_true",
        help="also write a redacted unified diff of the patch-relevant files against the public sources",
    )
    args = ap.parse_args(argv)
    out_real = os.path.realpath(args.out_dir)
    if not (out_real == "/tmp" or out_real.startswith("/tmp/")) or not os.path.isdir(out_real):
        sys.stdout.write("t51_check=%s REFUSED out_dir_not_under_tmp (nothing read, nothing written)\n" % TOOL_VERSION)
        return 3
    if args.proxy_file and not args.role and not args.start_script:
        args.role = "proxy"
    prio = lower_priority()
    t_start = time.monotonic()
    _DEADLINE[0] = t_start + args.max_seconds
    _IO["budget"] = int(args.max_read_mib * 1024 * 1024)

    lines, detail, finger, code_d, cfg_d, redact = build_report(args)
    detail["priority"] = prio
    if lines:
        lines[0] += " nice=%s io=%s" % (prio["nice"], prio["io"])
    if args.export_diff:
        try:
            _DEADLINE[0] = max(_DEADLINE[0], time.monotonic() + 60)  # the export gets its own minute
            t_exp = time.monotonic()
            epath, est, ebad = export_diff(load_expected(), redact, os.path.join(out_real, "t51_glm53_diff_%s.patch" % finger))
            _TIMES["export"] = time.monotonic() - t_exp
        except Exception as exc:  # the main report must still come out
            epath, est, ebad = None, {}, ["error:" + type(exc).__name__]
        if epath:
            lines.append("export=%s diffed=%d new=%d missing=%d lines=%d redacted=%d" % (epath, est["diffed"], est["new"], est["missing"], est["lines"], est["redacted"]))
        else:
            lines.append("export=BLOCKED self_check=%s (diff not written)" % ",".join(ebad))
    lines.append("detail=%s" % os.path.join(out_real, "t51_check_%s.json" % finger))
    elapsed = time.monotonic() - t_start
    slow = sorted(_TIMES.items(), key=lambda kv: -kv[1])[:3]
    detail["cost"] = {
        "read_bytes": _IO["read"],
        "read_budget_bytes": _IO["budget"],
        "binaries_skipped": _IO["skipped"],
        "elapsed_s": round(elapsed, 3),
        "sections_s": {k: round(v, 3) for k, v in sorted(_TIMES.items())},
        "proc_reads": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in _PROC_COST.items()},
    }
    lines.append(
        "fingerprint=%s code=%s config=%s read=%.1fMiB elapsed=%.1fs slow=%s so_skipped=%d"
        % (finger, code_d[:8], cfg_d[:8], _IO["read"] / 1048576.0, elapsed, ",".join("%s:%.1f" % kv for kv in slow) or "-", _IO["skipped"])
        + ((" baseline_token=" + detail["baseline_token"]) if detail.get("baseline_token") and not args.baseline else "")
    )
    summary = "\n".join(lines) + "\n"
    detail_text = json.dumps(detail, indent=1, sort_keys=True, default=str)

    bad = sorted(set(redact.hits(summary)) | set("detail:" + h for h in redact.hits(detail_text)))
    if bad:
        sys.stdout.write("t51_check=%s BLOCKED self_check=%s (nothing written)\n" % (TOOL_VERSION, ",".join(bad)))
        return 2
    path = safe_write(os.path.join(out_real, "t51_check_%s.json" % finger), detail_text)
    lines[-2] = "detail=%s" % (path or "not_written")
    summary = "\n".join(lines) + "\n"
    sys.stdout.write(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
