#!/usr/bin/env python3
"""T51 glm5.3 small field check: five questions about the site's vllm-ascend and one about Mooncake,
answered in at most 15 lines that can be copied by hand.

Read-only. Standard library only. Never imports vllm, vllm_ascend, torch or mooncake, reads no /proc
entries, starts no process, opens no network connection and writes no file at all (output goes to the
terminal only). Must be run as  python3 -I -S -B t51_glm53_fieldcheck.py [--role P|D]
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # never write .pyc, the standard library included

import argparse
import ast
import base64
import gzip
import hashlib
import json
import os
import platform
import re
import socket
import struct
import sysconfig
import time

TOOL = "glm53f-3b"
DATA_B64 = None  # filled in by tools/build_fieldcheck.py
MAX_LINES = 20  # without --show-diff
MAX_TEXT = 16 * 1024 * 1024
MAX_BINARY = 1024 * 1024 * 1024
MAX_SECONDS = 120
REQUIRED_FLAGS = (("-I", "isolated"), ("-S", "no_site"), ("-B", "dont_write_bytecode"))
SCOPE_CONN = "vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py"
CALLS = (
    ("bt_read", r"batch_transfer_sync_read"),
    ("q_put", r"request_queue\.put\b"),
    ("task_done", r"_mark_request_task_done"),
    ("zmq_send", r"ensure_zmq_send"),
    ("zmq_recv", r"ensure_zmq_recv"),
    ("retry", r"(?i)retr(y|ies)"),
)
_IO = {"read": 0}
_T0 = [time.monotonic()]


class Timeout(Exception):
    pass


def check_time():
    if time.monotonic() - _T0[0] > MAX_SECONDS:
        raise Timeout()


def read_bytes(path, limit=MAX_TEXT):
    try:
        with open(path, "rb") as f:
            data = f.read(limit)
        _IO["read"] += len(data)
        return data
    except OSError:
        return None


def sha16_file(path):
    try:
        if os.path.getsize(path) > MAX_BINARY:
            return "too_big"
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                _IO["read"] += len(chunk)
                h.update(chunk)
        return h.hexdigest()[:16]
    except OSError:
        return None


def load_data():
    return json.loads(gzip.decompress(base64.b64decode(DATA_B64)).decode("utf-8")) if DATA_B64 else {}


# ------------------------------------------------------------------ package location (site dirs only)


def site_dirs():
    import site  # with -S this only defines functions; site.main() does not run

    out = []
    try:
        out += site.getsitepackages()
    except Exception:
        pass
    for key in ("purelib", "platlib"):
        v = sysconfig.get_paths().get(key)
        if v:
            out.append(v)
    seen = []
    for d in out:
        if os.path.isdir(d) and d not in seen:
            seen.append(d)
    return seen


def find_pkg(name, dirs):
    for d in dirs:
        p = os.path.join(d, name)
        if os.path.isfile(os.path.join(p, "__init__.py")):
            return p
    return None


def scope_files(pkg):
    """vllm_ascend/patch/**/*.py, vllm_ascend/*.py and the P/D connector, keyed vllm_ascend/..."""
    root = os.path.dirname(pkg)
    out = {}
    for dirpath, dirnames, filenames in os.walk(os.path.join(pkg, "patch")):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if fn.endswith(".py"):
                full = os.path.join(dirpath, fn)
                out[os.path.relpath(full, root).replace(os.sep, "/")] = full
    for fn in os.listdir(pkg):
        if fn.endswith(".py") and fn != "_version.py":
            out["vllm_ascend/" + fn] = os.path.join(pkg, fn)
    conn = os.path.join(root, SCOPE_CONN)
    if os.path.isfile(conn):
        out[SCOPE_CONN] = conn
    return out


# ------------------------------------------------------------------ functions of the P/D connector


def functions(text):
    """qualname -> (text hash, stats) for top-level functions and class methods; nested defs stay in their parent."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    lines = text.splitlines()
    out = {}

    def add(node, qual):
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        seg = "\n".join(l.rstrip() for l in lines[start - 1 : node.end_lineno])
        stats = {"lines": node.end_lineno - start + 1}
        for key, rx in CALLS:
            stats[key] = len(re.findall(rx, seg))
        stats["loops"] = sum(1 for n in ast.walk(node) if isinstance(n, (ast.For, ast.While, ast.AsyncFor)))
        out[qual] = (hashlib.sha256(seg.encode("utf-8")).hexdigest()[:16], stats)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node, node.name)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add(item, node.name + "." + item.name)
    return out


# ------------------------------------------------------------------ ELF facts of the Mooncake store library


def _pread(f, off, n):
    f.seek(off)
    data = f.read(n)
    _IO["read"] += len(data)
    return data


def elf_dynamic(path, search):
    """SONAME, RUNPATH/RPATH, NEEDED, and whether the dynamic string table contains `search`."""
    try:
        with open(path, "rb") as f:
            hdr = _pread(f, 0, 64)
            if len(hdr) < 64 or hdr[:4] != b"\x7fELF" or hdr[4] != 2 or hdr[5] != 1:
                return None
            e_phoff = struct.unpack_from("<Q", hdr, 0x20)[0]
            e_phentsize, e_phnum = struct.unpack_from("<HH", hdr, 0x36)
            if e_phnum > 4096:
                return None
            ph = _pread(f, e_phoff, e_phentsize * e_phnum)
            loads, dyn = [], None
            for i in range(e_phnum):
                p_type, _fl, p_off, p_vaddr, _pa, p_filesz = struct.unpack_from("<IIQQQQ", ph, i * e_phentsize)
                if p_type == 1:
                    loads.append((p_vaddr, p_off, p_filesz))
                elif p_type == 2:
                    dyn = (p_off, p_filesz)
            if not dyn or dyn[1] > (1 << 20):
                return None
            dd = _pread(f, dyn[0], dyn[1])
            ent = []
            for j in range(len(dd) // 16):
                tag, val = struct.unpack_from("<qQ", dd, j * 16)
                if tag == 0:
                    break
                ent.append((tag, val))
            strtab = strsz = None
            for tag, val in ent:
                if tag == 5:
                    for vaddr, off, size in loads:
                        if vaddr <= val < vaddr + size:
                            strtab = val - vaddr + off
                elif tag == 10:
                    strsz = val

            def s(off):
                return _pread(f, strtab + off, 512).split(b"\0", 1)[0].decode("utf-8", "replace")

            if strtab is None:
                return None
            out = {"needed": [s(v) for t, v in ent if t == 1], "soname": next((s(v) for t, v in ent if t == 14), "-"),
                   "runpath": next((s(v) for t, v in ent if t in (29, 15)), "-"), "hit": False}
            if strsz and strsz <= 64 * 1024 * 1024:
                out["hit"] = search in _pread(f, strtab, strsz)
            return out
    except (OSError, struct.error, ValueError):
        return None


def mooncake_store(sdirs):
    """Binaries of the Mooncake package that export the client's BatchPut (they hold client_service.cpp)."""
    pkg = find_pkg("mooncake", sdirs)
    if not pkg:
        return []
    site = os.path.dirname(pkg)
    roots = [pkg] + [os.path.join(site, d) for d in sorted(os.listdir(site)) if d.startswith("mooncake_transfer_engine") and d.endswith(".libs")]
    out = []
    for root in roots:
        for fn in sorted(os.listdir(root)):
            full = os.path.join(root, fn)
            if ".so" not in fn or not os.path.isfile(full):
                continue
            check_time()
            info = elf_dynamic(full, b"_ZN8mooncake6Client8BatchPut")
            if info and info["hit"]:
                sha, found = scan_flags(full, (b"MOONCAKE_NVME_KV_DRIVER", b"[BatchPutTiming]"))
                names = dynsym_names(full)
                if names:
                    exp, imp = names
                    info["exports"] = len(exp)
                    info["exports_sha16"] = hashlib.sha256("\n".join(sorted(exp)).encode()).hexdigest()[:16]
                    info["imports"] = len(imp)
                    info["assert"] = int("__assert_fail" in imp)
                info["nvme_stub"] = int(b"MOONCAKE_NVME_KV_DRIVER" in found)
                info["timing"] = int(b"[BatchPutTiming]" in found)
                out.append((os.path.relpath(full, site), os.path.getsize(full), sha, info))
    return out


def dynsym_names(path):
    """Exported and imported names of the dynamic symbol table (read through the section headers)."""
    try:
        with open(path, "rb") as f:
            hdr = _pread(f, 0, 64)
            e_shoff = struct.unpack_from("<Q", hdr, 0x28)[0]
            e_shentsize, e_shnum = struct.unpack_from("<HH", hdr, 0x3A)
            if not e_shoff or e_shnum > 65535:
                return None
            sh = _pread(f, e_shoff, e_shentsize * e_shnum)
            secs = [struct.unpack_from("<IIQQQQII", sh, i * e_shentsize) for i in range(e_shnum)]
            dyn = [x for x in secs if x[1] == 11]  # SHT_DYNSYM
            if not dyn:
                return None
            _n, _t, _fl, _a, off, size, link, _i = dyn[0]
            stab = secs[link]
            if size > 64 * 1024 * 1024 or stab[5] > 64 * 1024 * 1024:
                return None
            syms = _pread(f, off, size)
            strs = _pread(f, stab[4], stab[5])
            exported, imported = [], []
            for i in range(len(syms) // 24):
                st_name, st_info, _other, st_shndx = struct.unpack_from("<IBBH", syms, i * 24)
                name = strs[st_name : strs.find(b"\0", st_name)].decode("utf-8", "replace")
                if not name or (st_info >> 4) not in (1, 2):
                    continue
                (imported if st_shndx == 0 else exported).append(name)
            return exported, imported
    except (OSError, struct.error, ValueError, IndexError):
        return None


def scan_flags(path, needles, cap=MAX_BINARY, block=1 << 20, overlap=256):
    """sha256 (16 hex) and which byte strings occur, in one pass of 1 MiB blocks."""
    found = set()
    try:
        if os.path.getsize(path) > cap:
            return "too_big", found
        h = hashlib.sha256()
        with open(path, "rb") as f:
            tail = b""
            while True:
                check_time()
                chunk = f.read(block)
                if not chunk:
                    break
                _IO["read"] += len(chunk)
                h.update(chunk)
                data = tail + chunk
                found.update(n for n in needles if n in data)
                tail = data[-overlap:]
        return h.hexdigest()[:16], found
    except OSError:
        return None, found


def clean_runpath(rp):
    return ":".join(t if t.startswith("$ORIGIN") or t == "-" else "<abs>" for t in rp.split(":"))


# ------------------------------------------------------------------ launch files (optional): how the serve options are given


SERVE_OPTS = (("kv", "--kv-transfer-config"), ("spec", "--speculative-config"), ("eager", "--enforce-eager"),
              ("async", "--async-scheduling"), ("noasync", "--no-async-scheduling"), ("addcfg", "--additional-config"),
              ("sched", "--scheduler-cls"))


def vllm_start_epoch():
    """Earliest start time of a running `vllm serve` (from /proc/<pid>/stat and /proc/stat btime)."""
    try:
        btime = next(int(l.split()[1]) for l in (read_bytes("/proc/stat", 1 << 20) or b"").decode().splitlines() if l.startswith("btime"))
        tck = os.sysconf("SC_CLK_TCK")
    except (StopIteration, ValueError, OSError):
        return None
    starts = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        argv = (read_bytes("/proc/%s/cmdline" % d, 65536) or b"").split(b"\0")
        if not any(a.endswith(b"vllm") for a in argv[:2]) or b"serve" not in argv[:4]:
            continue
        stat = (read_bytes("/proc/%s/stat" % d, 4096) or b"").decode("utf-8", "replace")
        try:
            starts.append(btime + int(stat.rsplit(")", 1)[1].split()[19]) / tck)
        except (IndexError, ValueError):
            continue
    return min(starts) if starts else None


def worker_log_env():
    """Across running vLLM workers: how many have MC_LOG_DIR set (libtransfer_engine then initializes glog and
    logs to files in that directory instead of stderr), and which GLOG_* keys are set. Names only, no values."""
    total, with_dir, keys = 0, 0, set()
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        comm = (read_bytes("/proc/%s/comm" % d, 64) or b"").decode("utf-8", "replace").strip()
        if not comm.startswith("VLLM::Worker"):
            continue
        env = read_bytes("/proc/%s/environ" % d, 1 << 20)
        if env is None:
            continue
        total += 1
        names = [x.split(b"=", 1)[0].decode("utf-8", "replace") for x in env.split(b"\0") if b"=" in x]
        with_dir += int("MC_LOG_DIR" in names)
        keys.update(n for n in names if n.startswith("GLOG_"))
    return total, with_dir, sorted(keys)


def launch_line(label, path, start):
    data = read_bytes(path)
    if data is None:
        return "launch %s=unreadable" % label
    text = data.decode("utf-8", "replace").replace("\\\n", " ")
    serve = [l for l in text.splitlines() if re.search(r"\bvllm\b.*\bserve\b", l) and not l.lstrip().startswith("#")]
    parts = []
    for key, opt in SERVE_OPTS:
        anywhere = len(re.findall(re.escape(opt) + r"(?![\w-])", text))
        kinds = []
        for l in serve:
            m = re.search(re.escape(opt) + r"(?![\w-])(?:[ =]+(\S+))?", l)
            if m:
                val = m.group(1) or ""
                kinds.append("var" if val.startswith(("$", "\"$", "'$")) else "lit")
        parts.append("%s=%s%s" % (key, "+".join(kinds) or "absent", "(file:%d)" % anywhere if anywhere != len(kinds) else ""))
    fwd = any(re.search(r'"\$@"|\$\{@\}|\$\*|\$\{[A-Za-z_]+\[@\]\}', l) for l in serve)
    try:
        mt = os.path.getmtime(path)
        when = "-" if start is None else ("after_start" if mt > start else "before_start")
    except OSError:
        when = "?"
    mc_dir = len(re.findall(r"\bMC_LOG_DIR\b", text))
    return "launch %s serve_lines=%d fwd_args=%d mtime=%s %s mc_log_dir=%d" % (label, len(serve), int(fwd), when, " ".join(parts), mc_dir)


# ------------------------------------------------------------------ replacements made by site patch files (AST)


def _const_str(node):
    node = getattr(node, "value", node) if node.__class__.__name__ == "Index" else node
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _module_of(rel):
    return rel[:-3].replace("/", ".")


def _aliases(tree, mod):
    """Local name -> dotted path, from the imports of a file (relative imports resolved)."""
    al = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    al[a.asname] = a.name
                else:
                    root = a.name.split(".")[0]
                    al.setdefault(root, root)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = mod.split(".")
                pkg = parts[: max(0, len(parts) - node.level)]
                base = ".".join(pkg + ([base] if base else []))
            for a in node.names:
                if a.name != "*":
                    al[a.asname or a.name] = (base + "." + a.name) if base else a.name
    return al


def _dotted(node, al):
    if isinstance(node, ast.Name):
        return al.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value, al)
        return base + "." + node.attr if base else None
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "modules" \
            and isinstance(node.value.value, ast.Name) and node.value.value.id == "sys":
        return _const_str(node.slice)
    if isinstance(node, ast.Call) and isinstance(node.func, (ast.Attribute, ast.Name)) and node.args \
            and getattr(node.func, "attr", getattr(node.func, "id", "")) == "import_module":
        return _const_str(node.args[0])
    return None


def replacements(text, mod):
    """Every module-level object a file replaces: (target, kind, value node, line). Kinds: attr (X.y = v),
    def+assign (X.y = <function defined in this file>), setattr, dictset (vars(X)["y"] / X.__dict__["y"] /
    globals()["y"]), rebind (a name brought in with `from m import y`, assigned again at module level)."""
    tree = ast.parse(text)
    al = _aliases(tree, mod)
    defs = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs[n.name] = n
    fromnames = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name != "*":
                    fromnames[a.asname or a.name] = al.get(a.asname or a.name)
    out = []
    top = set(id(n) for n in tree.body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and getattr(node, "value", None) is not None:
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                if isinstance(t, ast.Attribute):
                    d = _dotted(t, al)
                    if d:
                        out.append((d, "def+assign" if isinstance(node.value, ast.Name) and node.value.id in defs else "attr", node.value, node.lineno))
                elif isinstance(t, ast.Subscript):
                    key, base, owner = _const_str(t.slice), t.value, None
                    if isinstance(base, ast.Call) and isinstance(base.func, ast.Name) and base.func.id == "vars" and base.args:
                        owner = _dotted(base.args[0], al)
                    elif isinstance(base, ast.Attribute) and base.attr == "__dict__":
                        owner = _dotted(base.value, al)
                    elif isinstance(base, ast.Call) and isinstance(base.func, ast.Name) and base.func.id == "globals":
                        owner = mod
                    if owner and key:
                        out.append((owner + "." + key, "dictset", node.value, node.lineno))
                elif isinstance(t, ast.Name) and id(node) in top and fromnames.get(t.id):
                    out.append((fromnames[t.id], "rebind", node.value, node.lineno))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "setattr" and len(node.args) >= 3:
            owner, key = _dotted(node.args[0], al), _const_str(node.args[1])
            if owner and key:
                out.append((owner + "." + key, "setattr", node.args[2], node.lineno))
    return out, tree, al, defs


def _func_node(tree, qual):
    parts = qual.split(".")
    body = tree.body
    node = None
    for i, p in enumerate(parts):
        node = next((n for n in body if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == p), None)
        if node is None:
            return None
        body = node.body
    return node if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None


def _func_lines(text, node):
    lines = text.splitlines()
    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
    return start, lines[start - 1 : node.end_lineno]


def _norm(lines, start):
    """Lines stripped, blank and comment lines skipped; with their absolute line numbers."""
    out = []
    for i, l in enumerate(lines):
        s = l.strip()
        if s and not s.startswith("#"):
            out.append((start + i, s))
    return out


def _args(node):
    a = node.args
    names = [x.arg for x in getattr(a, "posonlyargs", [])] + [x.arg for x in a.args]
    if a.vararg:
        names.append("*" + a.vararg.arg)
    names += [x.arg for x in a.kwonlyargs]
    if a.kwarg:
        names.append("**" + a.kwarg.arg)
    return names


def _wraps_orig(tree, al, target, fn):
    """Does fn call the original, saved before the replacement (`_orig = X.f` or `X._orig_f = X.f` or
    getattr(X, "f")), or is the replacement built by a call that receives the original?"""
    names, attrs = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            v, t = node.value, node.targets[0]
            is_orig = _dotted(v, al) == target or (
                isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "getattr" and len(v.args) >= 2
                and "%s.%s" % (_dotted(v.args[0], al), _const_str(v.args[1])) == target)
            if is_orig:
                if isinstance(t, ast.Name):
                    names.add(t.id)
                elif isinstance(t, ast.Attribute):
                    attrs.add(t.attr)
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Name) and f.id in names) or (isinstance(f, ast.Attribute) and f.attr in attrs):
                return 1
    return 0


def compare_01(target, kind, value, tree, al, defs, text, v01):
    """For a replaced function that patch 01 changes: signature, wraps_orig, size, normalized difference with the
    v0.30.0 original (counts and original line ranges), and how many of 01's changes still find their context."""
    import difflib

    rel = qual = None
    parts = target.split(".")
    for i in range(len(parts) - 1, 0, -1):
        cand = "/".join(parts[:i]) + ".py"
        if cand in v01["files"]:
            rel, qual = cand, ".".join(parts[i:])
            break
    if rel is None or ("%s::%s" % (rel, qual)) not in v01["changed"]:
        return None
    orig_text, port_text = v01["files"][rel]["orig"], v01["files"][rel]["port"]
    o_node, p_node = _func_node(ast.parse(orig_text), qual), _func_node(ast.parse(port_text), qual)
    fn = None
    if isinstance(value, ast.Name):
        fn = defs.get(value.id)
    elif isinstance(value, ast.Attribute):
        cls = next((n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and isinstance(value.value, ast.Name) and n.name == value.value.id), None)
        if cls is not None:
            fn = next((n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == value.attr), None)
    res = {"target": target, "rel": rel, "qual": qual}
    if fn is None or o_node is None:
        call_wrap = isinstance(value, ast.Call) and any(_dotted(a, al) == target for a in value.args)
        res.update(fn=None, wraps=1 if call_wrap else 0)
        return res
    o_start, o_lines = _func_lines(orig_text, o_node)
    s_start, s_lines = _func_lines(text, fn)
    o_norm, s_norm = _norm(o_lines, o_start), _norm(s_lines, s_start)
    ops = difflib.SequenceMatcher(None, [x[1] for x in o_norm], [x[1] for x in s_norm], autojunk=False).get_opcodes()
    cnt = {"eq": 0, "ins": 0, "del": 0, "rep_o": 0, "rep_s": 0}
    at = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            cnt["eq"] += i2 - i1
            continue
        if tag == "insert":
            cnt["ins"] += j2 - j1
            at.append("^L%d" % (o_norm[i1][0] if i1 < len(o_norm) else (o_norm[-1][0] + 1 if o_norm else o_start)))
            continue
        if tag == "delete":
            cnt["del"] += i2 - i1
        else:
            cnt["rep_o"] += i2 - i1
            cnt["rep_s"] += j2 - j1
        a, b = o_norm[i1][0], o_norm[i2 - 1][0]
        at.append("L%d" % a if a == b else "L%d-%d" % (a, b))
    # 01's own changes to this function, and whether their context lines are found once in the site function
    k = n = 0
    if p_node is not None:
        p_start, p_lines = _func_lines(port_text, p_node)
        p_norm = [x[1] for x in _norm(p_lines, p_start)]
        o_plain = [x[1] for x in o_norm]
        s_plain = [x[1] for x in s_norm]
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, o_plain, p_norm, autojunk=False).get_opcodes():
            if tag == "equal":
                continue
            n += 1
            ctx = [o_plain[i1 - 1]] if i1 > 0 else []
            ctx += [o_plain[i2]] if i2 < len(o_plain) else []
            if ctx and all(s_plain.count(c) == 1 for c in ctx):
                k += 1
    extra = ""
    if qual.endswith("AsyncLLM.add_request"):
        calls = [x for x in ast.walk(fn) if isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute) and x.func.attr == "_add_request"
                 and isinstance(x.func.value, ast.Name) and x.func.value.id == "self"]
        calls.sort(key=lambda x: (x.lineno, x.col_offset))
        extra = " addreq_calls=%d[%s]" % (len(calls), ";".join("pos:%d,owner:%d" % (len(x.args), int(any(k.arg == "owner" for k in x.keywords))) for x in calls))
    a_site, a_orig = _args(fn), _args(o_node)
    res.update(fn=fn, sig="same" if a_site == a_orig else "diff(+%s,-%s)" % (",".join(x for x in a_site if x not in a_orig) or "-", ",".join(x for x in a_orig if x not in a_site) or "-"),
               wraps=_wraps_orig(tree, al, target, fn), lines="%d/%d" % (len(s_lines), len(o_lines)), cnt=cnt, at=at, ctx="%d/%d" % (k, n),
               o_lines=o_lines, s_lines=s_lines, extra=extra)
    return res


SECRET_ASSIGN = re.compile(r"(?i)\b(passw\w*|pwd|token|secret|api_?key|access_?key)\b(\s*[=:]\s*)(['\"]?)[^'\"\s,)]+")


def redact_line(line):
    line = re.sub(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", "<IP>", line)
    line = re.sub(r"/home/[^/\s'\"]+", "/home/<USER>", line)
    line = re.sub(r"/data/[^/\s'\"]+", "/data/<DIR>", line)
    line = re.sub(r"(?<![\w])[a-z]\d{8}(?![\w])", "<ID>", line)
    line = SECRET_ASSIGN.sub(lambda m: m.group(1) + m.group(2) + m.group(3) + "<SECRET>", line)
    for host in {socket.gethostname(), socket.gethostname().split(".")[0]} - {"", "localhost"}:
        if len(host) >= 3:
            line = line.replace(host, "<HOST>")
    return line


# ------------------------------------------------------------------ p2d replay, in memory


def p2d_check(p2d, source):
    raw = base64.b64decode(p2d["file_b64"])
    if hashlib.sha256(raw).hexdigest() != p2d["sha256"]:
        return "p2d_replay script=mismatch"
    ns = {"__name__": "t51_embedded_replay_p2d", "__file__": "replay_p2d.py"}
    # the whole file, byte for byte; with this __name__ its main() is defined but never called, so nothing is
    # written and nothing is compiled to disk
    exec(compile(raw.decode("utf-8"), "replay_p2d.py", "exec"), ns)
    head = "p2d_replay script=%s" % p2d["sha256"][:8]
    if ns["MARKER"] in source:
        return head + " result=already_patched"
    try:
        facts = ns["compute_facts"](source)
    except SyntaxError:
        return head + " result=input_does_not_parse"
    stops, rows = ns["check_facts"](facts)
    if stops:
        found = ["%s=%s" % (fid, json.dumps(value)[:40]) for fid, key, expected, value, ok, sev, why in rows if not ok and sev == "stop"]
        return head + " facts=stop result=not_replayed stops=" + ",".join(found)
    report = []
    try:
        result = ns["replay"](source, None, report)
        compile(result, "mooncake_connector.p2d.py", "exec")
    except ns["ReplayError"] as exc:
        return head + " facts=ok result=fail(exit %d) %s" % (exc.code, str(exc)[:160])
    alt = ["%s:%d" % (c["id"], r) for c, r in report if r not in (None, 1)]
    left = [c["id"] for c, r in report if r is None]
    used = sum(r is not None for _, r in report)
    return head + " facts=ok(%d) changes=%d/%d alt=%s left_out=%s result=ok" % (len(rows), used, len(ns["CHANGES"]), ",".join(alt) or "none", ",".join(left) or "none")


def compact(lines):
    """Only above MAX_LINES (same format as glm53f-1 otherwise): monkey lines into one, then launch lines into one,
    then read= onto the first line."""
    def merge(prefix, out):
        idx = [i for i, l in enumerate(out) if l.startswith(prefix)]
        if len(idx) < 2:
            return out
        joined = prefix + " | ".join(out[i][len(prefix):] for i in idx)
        return out[: idx[0]] + [joined] + [l for i, l in enumerate(out) if i > idx[0] and i not in idx]
    if len(lines) > MAX_LINES:
        lines = merge("monkey ", lines)
    if len(lines) > MAX_LINES:
        lines = merge("launch ", lines)
    if len(lines) > MAX_LINES and lines[-1].startswith("read="):
        lines = [lines[0] + " " + lines[-1]] + lines[1:-1]
    return lines


# ------------------------------------------------------------------ main


def short(rel):
    return rel[len("vllm_ascend/"):] if rel.startswith("vllm_ascend/") else rel


def main():
    missing = [f for f, a in REQUIRED_FLAGS if not getattr(sys.flags, a, 0)]
    if missing:
        sys.stdout.write("t51_field=%s REFUSED missing_flags=%s run: python3 -I -S -B <this script> (nothing read)\n" % (TOOL, ",".join(missing)))
        return 3
    ap = argparse.ArgumentParser(description="T51 glm5.3 small field check (read-only, writes nothing)")
    ap.add_argument("--role", choices=("P", "D"), help="only labels the first line")
    ap.add_argument("--run-dp-template", help="optional: how the serve options are given, and whether the file changed after vLLM started")
    ap.add_argument("--start-script", help="optional: same for start_p.sh / start_d0.sh")
    ap.add_argument("--show-diff", action="store_true", help="also print, redacted, the unified diff of each replaced 01 function against v0.30.0 (at most 60 lines)")
    args = ap.parse_args()
    try:
        os.nice(19)
    except OSError:
        pass
    data = load_data()
    lines = []
    sdirs = site_dirs()
    pkg = find_pkg("vllm_ascend", sdirs)
    if not pkg:
        sys.stdout.write("t51_field=%s vllm_ascend=not_found_in_site_dirs\n" % TOOL)
        return 1
    files = scope_files(pkg)
    hashes = {rel: sha16_file(full) for rel, full in files.items()}
    base = data.get("d4a_scope", {})
    hist = data.get("history", {})
    changed = sorted(r for r in files if r in base and hashes[r] != base[r])
    added = sorted(r for r in files if r not in base)
    missing_f = sorted(r for r in base if r not in files)
    conn_sha = hashes.get(SCOPE_CONN, "-")
    lines.append("t51_field=%s role=%s py=%s scope=%d changed=%d added=%d missing=%d conn_sha16=%s"
                 % (TOOL, args.role or "?", platform.python_version(), len(files), len(changed), len(added), len(missing_f), conn_sha))

    # 1. are the changed / added files public versions (any commit since mid-August on main or a release branch)?
    def where(rel):
        h = hist.get(hashes[rel])
        if not h:
            return "%s=none" % short(rel)
        same_path = "" if h[0] == rel else "@" + short(h[0])
        return "%s=%s/%s[%s]%s" % (short(rel), h[1], h[2], ",".join(h[3]) or "-", same_path)

    if changed:
        lines.append("hist_changed " + " ".join(where(r) for r in changed))
    if added:
        lines.append("hist_added " + " ".join(where(r) for r in added))
    if missing_f:
        lines.append("hist_missing " + " ".join(short(r) for r in missing_f[:8]) + (" +%d" % (len(missing_f) - 8) if len(missing_f) > 8 else ""))

    # 2. every replacement made by a changed or added file under patch/ (AST: attr, def+assign, setattr, dictset,
    #    rebind), as module.Class.name; "*" when the name is in our list of instrumented targets
    targets = data.get("targets") or {}
    tnames = set(targets.get("names", []))
    v01 = data.get("v01") or {"files": {}, "changed": []}
    v01["changed"] = set(v01["changed"])
    scan = [r for r in changed + added if r.startswith("vllm_ascend/patch/")]
    quiet, compares = [], []
    for rel in scan:
        check_time()
        text = (read_bytes(files[rel]) or b"").decode("utf-8", "replace")
        try:
            reps, tree, al, defs = replacements(text, _module_of(rel))
        except SyntaxError:
            lines.append("monkey %s unparsed" % short(rel))
            continue
        seen, items = set(), []
        for target, kind, value, lineno in reps:
            if (target, kind) in seen:
                continue
            seen.add((target, kind))
            parts = target.split(".")
            star = "*" if parts[-1] in tnames or (len(parts) > 1 and parts[-2] in tnames) else ""
            items.append("%s(%s)%s" % (target, kind, star))
            if kind in ("attr", "def+assign", "setattr", "dictset"):
                c = compare_01(target, kind, value, tree, al, defs, text, v01)
                if c is not None:
                    c["by"] = "%s:%d" % (short(rel), lineno)
                    compares.append(c)
        if items:
            lines.append("monkey %s sha16=%s %s" % (short(rel), hashes.get(rel, "-"), ",".join(items)))
        else:
            quiet.append(short(rel))
    lines.append("monkey none: %s" % (",".join(quiet) if quiet else "-"))
    # replaced functions that patch 01 changes: how the site function relates to the v0.30.0 original
    for c in compares:
        if c["fn"] is None:
            lines.append("repl %s by=%s fn=not_found_in_file wraps_orig=%d" % (c["target"], c["by"], c["wraps"]))
            continue
        k = c["cnt"]
        lines.append("repl %s by=%s sig=%s wraps_orig=%d lines=%s eq=%d ins=%d del=%d rep=%d/%d at=%s 01ctx=%s%s"
                     % (c["target"], c["by"], c["sig"], c["wraps"], c["lines"], k["eq"], k["ins"], k["del"], k["rep_o"], k["rep_s"],
                        ",".join(c["at"][:12]) + (",+%d" % (len(c["at"]) - 12) if len(c["at"]) > 12 else "") or "-", c["ctx"], c.get("extra", "")))

    # 3. can the P/D connector patch be replayed on the site file? The p2d tool's own logic (embedded at build
    #    time, its source up to main()) runs in memory exactly as its --check does: stop facts, the 28 changes
    #    located by their anchors, the result checks, an in-memory compile. Nothing is written.
    conn_text = (read_bytes(files[SCOPE_CONN]) or b"").decode("utf-8", "replace") if SCOPE_CONN in files else ""
    p2d = data.get("p2d")
    if not p2d:
        lines.append("p2d_replay=not_embedded")
    elif not conn_text:
        lines.append("p2d_replay=no_connector_file")
    else:
        lines.append(p2d_check(p2d, conn_text))

    xc = data.get("p2d_extra")
    if xc and conn_text:
        raw = base64.b64decode(xc["file_b64"])
        if hashlib.sha256(raw).hexdigest() != xc["sha256"]:
            lines.append("p2d_extra script=mismatch")
        else:
            ns = {"__name__": "t51_embedded_extra_checks", "__file__": "extra_checks.py"}
            exec(compile(raw.decode("utf-8"), "extra_checks.py", "exec"), ns)
            try:
                lines.append(ns["extra_line"](conn_text))
            except Exception as exc:
                lines.append("p2d_extra error=%s" % type(exc).__name__)

    # 4. functions of the P/D connector that differ from d4ad49796, with the statistics that changed
    ref = data.get("conn_funcs", {})
    site_f = functions(conn_text) if conn_text else None
    if site_f is None:
        lines.append("conn_funcs=unparsed")
    else:
        diffs = []
        for q in sorted(set(ref) | set(site_f)):
            if q not in site_f:
                diffs.append("-%s" % q)
            elif q not in ref:
                st = site_f[q][1]
                diffs.append("+%s:%s" % (q, ",".join("%s=%d" % (k, v) for k, v in sorted(st.items()) if v)))
            elif site_f[q][0] != ref[q][0]:
                a, b = ref[q][1], site_f[q][1]
                ch = ",".join("%s %d>%d" % (k, a.get(k, 0), b[k]) for k in sorted(b) if a.get(k, 0) != b[k])
                diffs.append("%s:%s" % (q, ch or "="))
        line = "conn_funcs n=%d " % len(diffs)
        cur = line
        for d in diffs:
            if len(cur) + len(d) > 230 and cur != line:
                lines.append(cur.rstrip())
                cur = "conn_funcs+ "
            cur += d + "; "
        lines.append(cur.rstrip("; "))

    # 6. Mooncake: which binaries hold the client (they get replaced), with their names and links
    try:
        for rel, size, sha, info in mooncake_store(sdirs):
            lines.append("mc_store %s size=%d sha16=%s soname=%s runpath=%s needed=%s"
                         % (rel, size, sha, info["soname"], clean_runpath(info["runpath"]), ",".join(info["needed"])))
            lines.append("mc_syms %s exports=%s exports_sha16=%s imports=%s assert=%s nvme_stub=%d timing=%d"
                         % (os.path.basename(rel), info.get("exports", "?"), info.get("exports_sha16", "?"), info.get("imports", "?"),
                            info.get("assert", "?"), info["nvme_stub"], info["timing"]))
    except Timeout:
        lines.append("mc_store=timeout")
    total, with_dir, gkeys = worker_log_env()
    lines.append("glog_env workers=%d mc_log_dir_set=%d glog_keys=%s" % (total, with_dir, ",".join(k[5:] for k in gkeys) or "none"))
    if args.run_dp_template or args.start_script:
        start = vllm_start_epoch()
        for label, path in (("template", args.run_dp_template), ("start", args.start_script)):
            if path:
                lines.append(launch_line(label, path, start))
    lines.append("read=%.1fMiB elapsed=%.1fs" % (_IO["read"] / 1048576.0, time.monotonic() - _T0[0]))

    # self-check: nothing that names this machine or a person
    text = "\n".join(lines)
    bad = []
    for host in {socket.gethostname(), socket.gethostname().split(".")[0]} - {"", "localhost"}:
        if len(host) >= 3 and host in text:
            bad.append("host")
    if re.search(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", text):
        bad.append("ipv4")
    if re.search(r"/home/|/data/|(?<![\w])[a-z]\d{8}(?![\w])", text):
        bad.append("path_or_id")
    if bad:
        sys.stdout.write("t51_field=%s BLOCKED self_check=%s\n" % (TOOL, ",".join(sorted(set(bad)))))
        return 2
    lines = compact(lines)
    if len(lines) > MAX_LINES:
        lines = lines[: MAX_LINES - 1] + ["truncated=%d_lines" % (len(lines) - MAX_LINES + 1)]
    if args.show_diff:
        import difflib

        dl = []
        for c in compares:
            if c.get("fn") is None:
                continue
            dl.append("diff %s (v0.30.0 -> site)" % c["target"])
            import textwrap  # both sides dedented: a method copied out to module level is not "all lines changed"

            o = textwrap.dedent("\n".join(c["o_lines"])).splitlines()
            n = textwrap.dedent("\n".join(c["s_lines"])).splitlines()
            dl += [redact_line(x) for x in difflib.unified_diff(o, n, "v0.30.0", "site", n=1, lineterm="")][2:]
        if len(dl) > 60:
            dl = dl[:59] + ["diff_truncated=%d_lines" % (len(dl) - 59)]
        dtext = "\n".join(dl)
        if re.search(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", dtext) or re.search(r"/home/(?!<USER>)|/data/(?!<DIR>)", dtext):
            dl = ["diff=BLOCKED self_check"]
        lines += dl
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Timeout:
        sys.stdout.write("t51_field=%s timeout=%ds\n" % (TOOL, MAX_SECONDS))
        sys.exit(4)
