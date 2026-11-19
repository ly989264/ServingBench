#!/usr/bin/env python3
"""T51 dsv4.1 environment check.

Read-only. Standard library only. Never imports vllm, vllm_ascend, torch or
mooncake, never starts a device, never writes anywhere except the detail file
it prints at the end. Compatible with Python 3.9 and later.

Run it with the same python3 that runs vLLM, once per container:

    python3 t51_dsv41_check.py \
        --run-dp-template run_dp_template.sh --launch-online-dp launch_online_dp.py \
        --start-script start_p.sh --mooncake-json mooncake.json \
        --start-mooncake start_mooncake.sh [--startup-log vllm.log]

On the proxy host:  python3 t51_dsv41_check.py --proxy-file load_balance_proxy_server_example.py
"""

from __future__ import annotations

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
import sys
import sysconfig
import time

TOOL_VERSION = "1"
EXPECTED_B64 = None  # filled in by build.py; otherwise t51_expected.json next to this file is used
MAX_SUMMARY_LINES = 20
MAX_LOG_BYTES = 512 * 1024 * 1024

# --------------------------------------------------------------------------- hashing

_NL = re.compile(r"\r\n|\r|\n")


def sha16(data):
    return hashlib.sha256(data).hexdigest()[:16]


def file_sha16(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
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
IPV6 = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{1,4}(?![\w:])|(?<![\w:])[0-9A-Fa-f]{0,4}::[0-9A-Fa-f:]{1,30}(?![\w:])")
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

    def hits(self, text):
        found = []
        if IPV4.search(text):
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


def safe_ver(value):
    """Versions that look like an IPv4 address would trip the self-check."""
    if value is None:
        return "none"
    value = str(value)
    if IPV4.search(value):
        value = value.replace(".", "_")
    return value.replace(" ", "_")


# --------------------------------------------------------------------------- small helpers


def read_bytes(path, limit=None):
    try:
        with open(path, "rb") as f:
            return f.read() if limit is None else f.read(limit)
    except OSError:
        return None


def read_text(path, limit=None):
    data = read_bytes(path, limit)
    return None if data is None else data.decode("utf-8", "replace")


def dist_version(name):
    try:
        return importlib.metadata.version(name)
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
        data = read_bytes("/proc/%d/environ" % self.pid)
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
        text = read_text("/proc/%d/maps" % self.pid)
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


def find_package_dir(name, extra_paths):
    for entry in extra_paths:
        if not entry:
            continue
        try:
            spec = importlib.machinery.PathFinder.find_spec(name, [entry])
        except Exception:
            spec = None
        if spec is not None and spec.submodule_search_locations:
            return list(spec.submodule_search_locations)[0]
    try:
        spec = importlib.util.find_spec(name)
    except Exception:
        spec = None
    if spec is not None and spec.submodule_search_locations:
        return list(spec.submodule_search_locations)[0]
    return None


def install_kind(dist_name):
    try:
        dist = importlib.metadata.distribution(dist_name)
    except Exception:
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
        text = importlib.metadata.distribution(dist_name).read_text("RECORD") or ""
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


def check_mooncake(expected, worker_maps, redact):
    out = {"version": dist_version("mooncake-transfer-engine"), "pkg": None}
    pkg = find_package_dir("mooncake", [])
    if pkg is None:
        out["state"] = "absent"
        return out
    out["pkg"] = redact.scrub_path(pkg)
    site = os.path.dirname(pkg)
    paths = mooncake_files(site, pkg)
    files, timing, glog_names = {}, [], set()
    for rel, full in sorted(paths.items()):
        files[rel] = file_sha16(full)
        base = os.path.basename(rel)
        if base.startswith("libglog"):
            glog_names.add(base)
        if is_binary(rel) and not base.startswith(THIRD_PARTY):
            data = read_bytes(full)
            if data is not None:
                if b"[BatchPutTiming]" in data:
                    timing.append(rel)
                glog_names.update(elf_needed_glog(data))
    out["files"] = files
    out["timing_in"] = timing
    # which published wheel was installed (by the installed RECORD), and what changed since
    record = installed_record("mooncake-transfer-engine")
    bin_record = {k: v for k, v in record.items() if is_binary(k)}
    out["changed_since_install"] = sorted(k for k, v in bin_record.items() if files.get(k) not in (None, v))
    out["missing_since_install"] = sorted(k for k in bin_record if k not in files)
    best = None
    for label, ref in (expected.get("mooncake") or {}).items():
        ref_bin = {k: v for k, v in ref["files"].items() if is_binary(k)}
        act_bin = {k: v for k, v in files.items() if is_binary(k)}
        diff = sorted(k for k in set(ref_bin) | set(act_bin) if ref_bin.get(k) != act_bin.get(k))
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
    loaded, loaded_glog = set(), set()
    for maps in worker_maps:
        for path in maps:
            base = os.path.basename(path)
            if "mooncake" in path.lower():
                loaded.add(redact.scrub_path(path))
            if base.startswith("libglog"):
                loaded_glog.add(base)
    out["loaded"] = sorted(loaded)
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
                if read > MAX_LOG_BYTES:
                    out["truncated"] = True
                    break
                line = raw.decode("utf-8", "replace")
                if "Model Runner" in line or "model runner" in line:
                    for pat, how in RUNNER_PATTERNS:
                        m = pat.search(line)
                        if m:
                            runners.add(("V1" if how == "fallback" else ("V2" if how == "default" else m.group(1))) + "(" + how + ")")
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
        return self

    def __exit__(self, et, ev, tb):
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
    lines = []

    errors = []
    tmpl = start = mjson = mstart = launch_sha = ex_launch = ex_tmpl = None
    role_arg = args.role
    missing = []
    procs, btime, apis, api_pids = {}, None, [], set()
    engines, workers, worker_maps, first_start = {}, {}, [], None
    run_env, file_env, cann_home = None, {}, None
    code_digest, py_paths = {}, []
    inst_running, inst_files, mismatches, env_mism, source = [], [], [], [], []
    runner = "?"
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
        if not role_arg and args.start_script:
            base = os.path.basename(args.start_script).lower()
            role_arg = "P" if "start_p" in base else ("D" if "start_d" in base else None)

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
        lines.append(
            "t51_check=%s role=%s arch=%s py=%s same_py=%s glibc=%s"
            % (TOOL_VERSION, role_arg or "?", basics["arch"], safe_ver(basics["py"]), boolstr(exe_same), safe_ver(glibc))
        )
        lines.append(
            "torch=%s torch_npu=%s transformers=%s cann=%s"
            % (safe_ver(basics["torch"]), safe_ver(basics["torch_npu"]), safe_ver(basics["transformers"]), safe_ver(cann_ver))
        )

        py_paths = []
        if run_env and run_env.get("PYTHONPATH"):
            py_paths = run_env["PYTHONPATH"].split(":")
        elif os.environ.get("PYTHONPATH"):
            py_paths = os.environ["PYTHONPATH"].split(":")
    # ---- vLLM
    with Section('vllm', errors):
        vdir = find_package_dir("vllm", py_paths)
        if vdir:
            kind, _ = install_kind("vllm")
            root = os.path.dirname(vdir)
            res, actual = compare_tree("vllm", vdir, "vllm", expected["vllm"], set(expected["vllm"]["focus"]), first_start)
            diag = os.path.exists(os.path.join(vdir, "v1", "metrics", "pd_diagnostics.py"))
            detail["vllm"] = {"version": dist_version("vllm"), "install": kind, "git": git_head(root), "dir": redact.scrub_path(vdir), **res}
            labels = change_labels(res, "vllm/", res["state"])
            lines.append(
                "vllm=%s src=%s git=%s state=%s changed=%d missing=%d added=%d diag=%s after_start=%d"
                % (safe_ver(dist_version("vllm")), kind, git_head(root) or "none", res["state"], len(res["changed"]), len(res["missing"]), len(res["added"]), boolstr(diag), len(res["changed_after_start"]))
            )
            if labels:
                lines.append("vllm_changes=" + short_list(labels, 200))
            code_digest["vllm"] = sha16(json.dumps(actual, sort_keys=True).encode())
        else:
            lines.append("vllm=absent")

    # ---- vllm-ascend
    with Section('ascend', errors):
        adir = find_package_dir("vllm_ascend", py_paths)
        if adir:
            kind, _ = install_kind("vllm-ascend") if dist_version("vllm-ascend") else install_kind("vllm_ascend")
            root = os.path.dirname(adir)
            res, actual = compare_tree("vllm_ascend", adir, "vllm_ascend", expected["vllm_ascend"], set(expected["vllm_ascend"]["focus"]), first_start)
            diag = os.path.exists(os.path.join(adir, "distributed", "kv_transfer", "kv_pool", "ascend_store", "diagnostics.py"))
            ver = dist_version("vllm-ascend") or dist_version("vllm_ascend")
            detail["vllm_ascend"] = {"version": ver, "install": kind, "git": git_head(root), "dir": redact.scrub_path(adir), **res}
            labels = change_labels(res, "vllm_ascend/", res["state"])
            lines.append(
                "ascend=%s src=%s git=%s state=%s changed=%d missing=%d added=%d diag=%s after_start=%d"
                % (safe_ver(ver), kind, git_head(root) or "none", res["state"], len(res["changed"]), len(res["missing"]), len(res["added"]), boolstr(diag), len(res["changed_after_start"]))
            )
            if labels:
                lines.append("ascend_changes=" + short_list(labels, 200))
            code_digest["ascend"] = sha16(json.dumps(actual, sort_keys=True).encode())
        else:
            lines.append("ascend=absent")

    # ---- Mooncake
    with Section('mooncake', errors):
        mc = check_mooncake(expected, worker_maps, redact)
        detail["mooncake"] = mc
        if mc.get("state") == "absent":
            lines.append("mooncake=absent")
        else:
            lines.append(
                "mooncake=%s state=%s nearest=%s so_diff=%d changed_since_install=%d timing=%s glog=%s loaded=%s store_lib=%s"
                % (
                    safe_ver(mc.get("version")),
                    mc.get("state"),
                    mc.get("nearest", "-"),
                    len(mc.get("so_diff", [])),
                    len(mc.get("changed_since_install", [])),
                    boolstr(bool(mc.get("timing_in"))),
                    mc.get("glog") or "?",
                    len(mc.get("loaded", [])) if worker_maps else "noperm" if apis else "-",
                    "+".join(sorted(set(os.path.dirname(x).rsplit("/", 1)[-1] + "/" + os.path.basename(x) for x in mc.get("loaded", []) if "libmooncake_store" in x))) or "-",
                )
            )
            code_digest["mooncake"] = sha16(json.dumps(mc.get("files", {}), sort_keys=True).encode())

        build = check_build(cann_home)
        detail["build"] = build
        have = [t for t, ok in build["tools"].items() if ok]
        miss_h = [h for h, ok in build["headers"].items() if not ok]
        cdev = build["cann_dev"]
        lines.append(
            "build_tools=%s headers_missing=%s cann_dev=%s"
            % (short_list(have, 60) if have else "none", short_list(miss_h, 80) if miss_h else "none", ("/".join(k for k, v in cdev.items() if v) or "none") if cdev is not None else "no_cann")
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

    # ---- instances: from files
    with Section('inst_file', errors):
        inst_files = []
        serve = find_serve_in_shell(tmpl) if tmpl else None
        if serve is not None:
            opts = parse_cli_args(serve)
            base = describe_instance(opts, redact)
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
        mismatches = []
        fields = ("kind", "tp", "dp", "dp_rank", "conn", "load_async", "spec", "spec_eager", "eager", "async_sched", "policy", "ep")
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
        if inst_files and inst_running and len(inst_files) != len(inst_running):
            mismatches.append("count:%d_files_%d_running" % (len(inst_files), len(inst_running)))
        env_mism = []
        if run_env is not None:
            for k in ENV_KEYS:
                if k in file_env and k in run_env and k in ENV_VALUE_KEYS and file_env[k] != run_env[k]:
                    env_mism.append(k)
                elif (k in file_env) != (k in run_env) and k in ("VLLM_USE_V2_MODEL_RUNNER", "VLLM_LOGGING_CONFIG_PATH", "MOONCAKE_CONFIG_PATH", "GLOG_logtostderr"):
                    env_mism.append(k)
        detail["file_vs_running"] = {"instances": mismatches, "env": env_mism}

    # ---- instance lines, grouped
    with Section('instance_lines', errors):
        source = inst_running if inst_running else inst_files
        groups = {}
        for d in source:
            key = tuple((k, str(d.get(k))) for k in ("kind", "tp", "pp", "dp", "ep", "conn", "load_async", "layerwise", "policy", "spec", "spec_eager", "eager", "async_sched", "workers", "prefer_same_node", "sfa_d2h"))
            groups.setdefault(key, []).append(d)
        for key, members in sorted(groups.items(), key=lambda kv: str(kv[0])):
            d = members[0]
            ports = [m.get("port") for m in members if str(m.get("port", "")).isdigit()]
            ranks = [m.get("dp_rank") for m in members if str(m.get("dp_rank", "")).isdigit()]
            lines.append(
                "inst[%s n=%d] kind=%s ports=%s tp=%s dp=%s ranks=%s pp=%s ep=%s workers=%s conn=%s load_async=%s layerwise=%s policy=%s spec=%s spec_eager=%s eager=%s async=%s same_node=%s sfa_d2h=%s"
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
                )
            )
        if not source:
            lines.append("inst=none_running_and_no_template")

    # ---- runtime layout
    with Section('run', errors):
        runner = "?"
        ev = run_env if run_env is not None else file_env
        if ev and "VLLM_USE_V2_MODEL_RUNNER" in ev:
            runner = ("V2" if ev["VLLM_USE_V2_MODEL_RUNNER"] not in ("0", "false", "False") else "V1") + "(env)"
        log = scan_log(args.startup_log) if args.startup_log else None
        if log and log.get("runner_lines"):
            runner = "/".join(log["runner_lines"])
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
            "run pid1=%s launcher=%s stdout=%s stderr=%s worker_stderr=%s logcfg=%s glog_env=%s free_gib=%s runner=%s env_read=%s"
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
            )
        )
        if log:
            c = log.get("counts", {})
            lines.append(
                "log readable=%s pd_diag=%d events=%s worker_runner=%s batch_put_timing=%d glog_before_init=%d"
                % (boolstr(log.get("readable")), c.get("pd_diag", 0), short_list(log.get("events", []), 90), short_list(log.get("worker_runner", []), 60), c.get("batch_put_timing", 0), c.get("glog_before_init", 0))
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
        if mismatches or env_mism:
            files_line.append("file_vs_run=%s" % short_list(mismatches + env_mism, 100))
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

    # ---- fingerprint: code state + configuration shape (no per-machine ranks or ports)
    with Section('fingerprint', errors):
        shape = []
        for d in source:
            shape.append({k: str(d.get(k)) for k in ("kind", "tp", "pp", "dp", "ep", "conn", "load_async", "layerwise", "policy", "spec", "spec_eager", "eager", "async_sched", "prefer_same_node", "sfa_d2h")})
        shape.sort(key=lambda x: json.dumps(x, sort_keys=True))
        cfg_digest = sha16(json.dumps({"inst": shape, "runner": runner, "mjson": {k: v for k, v in (mjson or {}).items() if k not in ("sha",)}}, sort_keys=True).encode())
        code_d = sha16(json.dumps(code_digest, sort_keys=True).encode())
        finger = sha16((code_d + cfg_digest).encode())
        detail["fingerprint"] = {"all": finger, "code": code_d, "config": cfg_digest, "parts": code_digest}

    if errors:
        lines.append("errors=" + short_list(errors, 200))
    if len(lines) > MAX_SUMMARY_LINES - 2:
        keep = lines[: MAX_SUMMARY_LINES - 3]
        keep.append("truncated=%d_lines_see_detail" % (len(lines) - len(keep)))
        lines = keep
    return lines, detail, finger, code_d, cfg_digest, redact


def main(argv=None):
    ap = argparse.ArgumentParser(description="T51 dsv4.1 read-only environment check")
    ap.add_argument("--run-dp-template")
    ap.add_argument("--launch-online-dp")
    ap.add_argument("--start-script", help="start_p.sh or start_d.sh")
    ap.add_argument("--mooncake-json")
    ap.add_argument("--start-mooncake")
    ap.add_argument("--proxy-file")
    ap.add_argument("--startup-log")
    ap.add_argument("--role", choices=("P", "D", "proxy"))
    ap.add_argument("--out-dir", default="/tmp", help="where the detail file goes (default /tmp)")
    args = ap.parse_args(argv)
    if args.proxy_file and not args.role and not args.start_script:
        args.role = "proxy"

    lines, detail, finger, code_d, cfg_d, redact = build_report(args)
    lines.append("detail=%s" % os.path.join(args.out_dir, "t51_check_%s.json" % finger))
    lines.append("fingerprint=%s code=%s config=%s" % (finger, code_d[:8], cfg_d[:8]))
    summary = "\n".join(lines) + "\n"
    detail_text = json.dumps(detail, indent=1, sort_keys=True, default=str)

    bad = sorted(set(redact.hits(summary)) | set("detail:" + h for h in redact.hits(detail_text)))
    if bad:
        sys.stdout.write("t51_check=%s BLOCKED self_check=%s (nothing written)\n" % (TOOL_VERSION, ",".join(bad)))
        return 2
    path = os.path.join(args.out_dir, "t51_check_%s.json" % finger)
    try:
        with open(path, "w") as f:
            f.write(detail_text)
    except OSError:
        lines[-2] = "detail=not_written"
        summary = "\n".join(lines) + "\n"
    sys.stdout.write(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
