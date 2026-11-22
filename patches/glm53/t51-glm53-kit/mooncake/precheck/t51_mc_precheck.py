#!/usr/bin/env python3
"""T51 glm5.3: read-only check of the site's Mooncake package before libmooncake_store.so is replaced.

The replacement library (Mooncake 78566d4 + patch 04, built in the lab with the site wheel's options) is described by
data embedded at build time (build_precheck.py): the exported names of the lab's unpatched check build, what patch 04
changes in them, the replacement's imports and symbol-version needs. This script reads the site's package and
decides, by rules fixed before it runs:

  1. the site's libmooncake_store.so is still the file the field check saw (size and sha16), with no second copy of
     the store library in the package;
  1b. the compiled contents are those of the lab's unpatched check build. Every SHT_PROGBITS section other than
     .rodata (.text, .eh_frame, .gcc_except_table, .data.rel.ro, ...) has exactly the lab's (sh_type, sh_flags,
     sh_size): patchelf moves these sections but never changes their sizes (lab: one and two patchelf calls give the
     same values), while a different compiler, header or optimization level does. .rodata also holds the source
     paths glog's LOG() records (__FILE__), so it depends on the build directory: its strings ending in one of the
     lab's source-relative paths give the site's path prefix length L' and their number N'; N' must equal the lab's
     N, every lab path occurs once, all of them have one and the same prefix (one build, one source directory; two
     different prefixes of the same length stop too), and the .rodata difference must equal the prediction exactly
     (no tolerance):
     each path string occupies an 8-byte slot, pad8(length + 1), so the strings grow by
     pad8 = sum over strings of pad8(L' + len(rel) + 1) - pad8(L + len(rel) + 1); the data after the strings is
     16-byte aligned and moves to the next multiple of 16, align16 = (-(E + pad8)) mod 16 - (-E) mod 16, E being where
     the lab's strings end, mod 16 (from the lab builds at longer source paths). No such strings at all: the model
     does not apply -> STOP_model_inapplicable. Anything else -> STOP_build_differs. The site's path itself is never
     printed: only L', N' and a short hash. The total file size is printed for reference only: about 2.2 MB of it is
     patchelf's padding;
  2. its exported names, sorted into families (mooncake::, std::, msgpack::, boost::, asio::, google::, YAML::,
     Json::, tl::, yalantinglibs, type info/vtables _ZTI/_ZTV/_ZTS, unmangled, other), are compared with the lab
     build family by family:
       - a difference in mooncake::                              -> STOP_report (bring the output back)
       - a difference in _ZTI/_ZTV/_ZTS, or in a third-party versioned inline namespace (msgpack::vN, fmt::vN, ...)
                                                                 -> STOP_align (align that dependency, rebuild)
       - any other family may differ;
  3. every name the package's other libraries import from libmooncake_store.so is exported by the replacement, and no
     ELF file of the package (libraries, Python modules, executables) refers to a name the replacement no longer
     exports (patch 04 changes two private member functions' parameters) -> otherwise STOP_consumer_refs_removed;
  4. every name the replacement imports (strong) is exported by a library it loads, and every symbol version it needs
     is defined there;
  5. the replacement's exported names differ from the site library's by exactly what patch 04 adds: none removed, and
     added exactly the three expected names (two overloads that take the timing data, and the timing writer)
     -> otherwise STOP_export_delta.
  All of them hold: result ok_replace. Anything else: STOP, do not replace, bring the output back.

Read-only: standard library only, imports nothing of Mooncake, writes nothing (no .pyc either), reads only ELF headers
and symbol tables (plus one pass over libmooncake_store.so for its sha256), /proc/<pid>/cmdline and maps to find the
libraries a running vLLM loads. Output: at most 20 lines. Run:
    python3 -I -S -B t51_mc_precheck.py [--pid <pid of a vLLM process>]
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import base64
import gzip
import hashlib
import json
import os
import re
import struct
import sysconfig
import time

TOOL = "glm53mc-6"
DATA_B64 = None  # filled in by build_precheck.py
REQUIRED_FLAGS = (("-I", "isolated"), ("-S", "no_site"), ("-B", "dont_write_bytecode"))
MAX_TABLE = 96 * 1024 * 1024
DEADLINE = time.monotonic() + 60
STORE = "libmooncake_store.so"
SYS_DIRS = ("/lib64", "/usr/lib64", "/lib/aarch64-linux-gnu", "/usr/lib/aarch64-linux-gnu", "/lib", "/usr/lib")
ALIGN_FAMILIES = ("rtti", "msgpack", "fmt")
VERSIONED_NS = re.compile(r"(?:7msgpack|3fmt)\d?v\d+|json_abi_v[0-9_]+|\d+[a-z_]+_v\d+_\d+")
YLT = ("ylt", "async_simple", "coro_rpc", "coro_io", "coro_http", "cinatra", "struct_pack", "struct_pb", "iguana",
       "easylog", "struct_json", "struct_yaml", "struct_xml", "frozen")
NS_FAMILY = {"mooncake": "mooncake", "msgpack": "msgpack", "fmt": "fmt", "boost": "boost", "asio": "asio",
             "google": "google", "YAML": "YAML", "Json": "Json", "tl": "tl"}
FAMILIES = ("mooncake", "std", "msgpack", "fmt", "boost", "asio", "google", "YAML", "Json", "tl", "ylt", "rtti", "c",
            "other")


def boolstr(v):
    return "1" if v else "0"


def check_time():
    if time.monotonic() > DEADLINE:
        raise TimeoutError("time limit")


# ------------------------------------------------------------------------------------------------ ELF reading
def _pread(f, off, n):
    f.seek(off)
    return f.read(n)


def elf_info(path):
    """Dynamic symbols, NEEDED / SONAME / RPATH / RUNPATH, symbol-version needs and definitions, PT_TLS memsz."""
    check_time()
    with open(path, "rb") as f:
        hdr = _pread(f, 0, 64)
        if len(hdr) < 64 or hdr[:4] != b"\x7fELF" or hdr[4] != 2 or hdr[5] != 1:
            return None
        e_phoff, e_shoff = struct.unpack_from("<QQ", hdr, 0x20)
        e_phentsize, e_phnum, e_shentsize, e_shnum = struct.unpack_from("<HHHH", hdr, 0x36)
        if not e_shoff or e_shnum > 65535:
            return None
        sh = _pread(f, e_shoff, e_shentsize * e_shnum)
        secs = [struct.unpack_from("<IIQQQQII", sh, i * e_shentsize) for i in range(e_shnum)]

        def table(sec):
            if sec[5] > MAX_TABLE:
                raise ValueError("table too large")
            return _pread(f, sec[4], sec[5])

        def cstr(buf, off):
            return buf[off: buf.find(b"\0", off)].decode("utf-8", "replace")

        out = {"exp": set(), "imp_strong": set(), "imp_weak": set(), "needed": [], "soname": None, "path": None,
               "verneed": {}, "verdef": set(), "tls": 0, "progbits": []}
        e_shstrndx = struct.unpack_from("<H", hdr, 0x3E)[0]
        if e_shstrndx < len(secs):
            shnames = table(secs[e_shstrndx])
            out["progbits"] = sorted((x[1], x[2], x[5], cstr(shnames, x[0])) for x in secs if x[1] == 1)
        dyn = [x for x in secs if x[1] == 11]
        if dyn:
            syms, strs = table(dyn[0]), table(secs[dyn[0][6]])
            for i in range(len(syms) // 24):
                st_name, st_info, _o, st_shndx = struct.unpack_from("<IBBH", syms, i * 24)
                name = cstr(strs, st_name)
                bind = st_info >> 4
                if not name or bind not in (1, 2):
                    continue
                if st_shndx != 0:
                    out["exp"].add(name)
                elif bind == 1:
                    out["imp_strong"].add(name)
                else:
                    out["imp_weak"].add(name)
        ds = [x for x in secs if x[1] == 6]
        if ds:
            dd, dstr = table(ds[0]), table(secs[ds[0][6]])
            for j in range(len(dd) // 16):
                tag, val = struct.unpack_from("<qQ", dd, j * 16)
                if tag == 0:
                    break
                if tag == 1:
                    out["needed"].append(cstr(dstr, val))
                elif tag == 14:
                    out["soname"] = cstr(dstr, val)
                elif tag in (15, 29):
                    out["path"] = cstr(dstr, val)
        for sec in secs:
            if sec[1] == 0x6FFFFFFE:      # SHT_GNU_verneed
                buf, s = table(sec), table(secs[sec[6]])
                off = 0
                for _ in range(sec[7] or 64):
                    if off + 16 > len(buf):
                        break
                    _v, cnt, fofs, aux, nxt = struct.unpack_from("<HHIII", buf, off)
                    vers = []
                    a = off + aux
                    for _k in range(cnt):
                        _h, _fl, _oth, nm, anext = struct.unpack_from("<IHHII", buf, a)
                        vers.append(cstr(s, nm))
                        if not anext:
                            break
                        a += anext
                    out["verneed"][cstr(s, fofs)] = sorted(vers)
                    if not nxt:
                        break
                    off += nxt
            elif sec[1] == 0x6FFFFFFD:    # SHT_GNU_verdef
                buf, s = table(sec), table(secs[sec[6]])
                off = 0
                for _ in range(sec[7] or 4096):
                    if off + 20 > len(buf):
                        break
                    _v, _fl, _nd, cnt, _h, aux, nxt = struct.unpack_from("<HHHHIII", buf, off)
                    if cnt:
                        nm = struct.unpack_from("<I", buf, off + aux)[0]
                        out["verdef"].add(cstr(s, nm))
                    if not nxt:
                        break
                    off += nxt
        if e_phoff and e_phnum < 4096:
            ph = _pread(f, e_phoff, e_phentsize * e_phnum)
            for i in range(e_phnum):
                p_type = struct.unpack_from("<I", ph, i * e_phentsize)[0]
                if p_type == 7:            # PT_TLS
                    out["tls"] = struct.unpack_from("<Q", ph, i * e_phentsize + 40)[0]
        return out


def sha16(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            check_time()
            block = f.read(1 << 20)
            if not block:
                break
            h.update(block)
    return h.hexdigest()[:16]


def rodata_paths(path, rels):
    """Strings of .rodata ending in "/" + one of the lab's source-relative paths: (prefix, relative path) pairs."""
    with open(path, "rb") as f:
        hdr = _pread(f, 0, 64)
        e_shoff = struct.unpack_from("<Q", hdr, 0x28)[0]
        e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", hdr, 0x3A)
        sh = _pread(f, e_shoff, e_shentsize * e_shnum)
        secs = [struct.unpack_from("<IIQQQQII", sh, i * e_shentsize) for i in range(e_shnum)]
        names = _pread(f, secs[e_shstrndx][4], min(secs[e_shstrndx][5], 1 << 20))
        ro = [x for x in secs if names[x[0]: names.find(b"\0", x[0])] == b".rodata"]
        if not ro or ro[0][5] > MAX_TABLE:
            return []
        data = _pread(f, ro[0][4], ro[0][5])
    suffixes = sorted({("/" + r).encode() for r in rels}, key=len, reverse=True)   # longest first
    out = []
    for chunk in data.split(b"\0"):
        if len(chunk) < 8 or b"/" not in chunk:
            continue
        for suf in suffixes:
            if chunk.endswith(suf):
                out.append((chunk[: len(chunk) - len(suf) + 1], suf[1:].decode("utf-8", "replace")))   # prefix with "/"
                break
    return out


def pad8(n):
    return (n + 7) // 8 * 8


def rodata_predict(pairs, lab_rels, lab_l, end16):
    """The exact .rodata difference the build path alone makes (see 1b): (pad8 part, align16 part)."""
    g = sum(pad8(len(pre) + len(rel) + 1) for pre, rel in pairs) - sum(pad8(lab_l + len(rel) + 1) for rel in lab_rels)
    return g, (-(end16 + g)) % 16 - (-end16) % 16


def progbits_hash(rows):
    return hashlib.sha256(repr(sorted((t, fl, sz) for t, fl, sz, *_ in rows)).encode()).hexdigest()[:16]


def hset(names):
    return hashlib.sha256("\n".join(sorted(names)).encode()).hexdigest()[:16]


# ------------------------------------------------------------------------------------------------ families
def family(name):
    if name.startswith(("_ZTI", "_ZTV", "_ZTS", "_ZTT")):
        return "rtti"
    if not name.startswith("_Z"):
        return "c"
    s = name[2:]
    if s.startswith(("Th", "Tv", "Tc")):          # thunks: classify by the function they jump to
        m = re.match(r"T[hv]n?\d+_(?:n?\d+_)?|Tc(?:h|v)n?\d+_(?:n?\d+_)?(?:h|v)n?\d+_(?:n?\d+_)?", s)
        s = s[m.end():] if m else s
        if s.startswith("_Z"):
            s = s[2:]
    if s.startswith(("GV", "GR")):                # guard variables, reference temporaries
        s = s[2:].lstrip("_Z")
    if s.startswith("N"):
        s = s[1:].lstrip("rVK").lstrip("RO")
    if s.startswith(("St", "Sa", "Sb", "Ss", "Si", "So", "Sd")):
        return "std"
    m = re.match(r"(\d+)", s)
    if not m:
        return "other"
    n = int(m.group(1))
    ident = s[m.end(): m.end() + n]
    if ident in YLT:
        return "ylt"
    if ident.startswith("fL") and len(ident) <= 5:  # gflags' flag namespaces fLB, fLI, fLS, fLI64, fLU64
        return "google"
    return NS_FAMILY.get(ident, "other")


def buckets(names):
    out = {f: [] for f in FAMILIES}
    for n in names:
        out[family(n)].append(n)
    return {f: (len(v), hset(v)) for f, v in out.items()}, out


# ------------------------------------------------------------------------------------------------ locating files
def find_mooncake():
    import site  # with -S this only defines functions

    dirs = []
    try:
        dirs += site.getsitepackages()
    except Exception:
        pass
    for key in ("purelib", "platlib"):
        v = sysconfig.get_paths().get(key)
        if v:
            dirs.append(v)
    for d in dirs:
        p = os.path.join(d, "mooncake")
        if os.path.isfile(os.path.join(p, STORE)):
            return p
    return None


def vllm_maps(want_pid):
    """Paths of the libraries a vLLM process loads (the first process that maps libmooncake_store.so)."""
    pids = [want_pid] if want_pid else sorted((int(p) for p in os.listdir("/proc") if p.isdigit()), reverse=True)
    for pid in pids:
        check_time()
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                cmd = f.read(4096)
            if not want_pid and b"vllm" not in cmd.lower():
                continue
            with open("/proc/%d/maps" % pid) as f:
                paths = {line.split(None, 5)[5].strip() for line in f if len(line.split(None, 5)) == 6}
        except OSError:
            continue
        paths = {p for p in paths if p.startswith("/") and ".so" in p}
        if any(os.path.basename(p) == STORE for p in paths):
            return pid, paths
    return None, set()


def rel(p, base):
    try:
        return os.path.relpath(p, base)
    except ValueError:
        return os.path.basename(p)


# ------------------------------------------------------------------------------------------------ main
def main():
    missing = [f for f, a in REQUIRED_FLAGS if not getattr(sys.flags, a, 0)]
    if missing:
        print("t51_mc_precheck=%s REFUSED missing_flags=%s run: python3 -I -S -B <this script>" % (TOOL, ",".join(missing)))
        return 3
    try:
        os.nice(19)
    except OSError:
        pass
    want_pid = None
    if "--pid" in sys.argv:
        want_pid = int(sys.argv[sys.argv.index("--pid") + 1])
    show = 0
    if "--show-names" in sys.argv:   # more lines: up to N differing names per family and side
        show = int(sys.argv[sys.argv.index("--show-names") + 1])
    data = json.loads(gzip.decompress(base64.b64decode(DATA_B64)).decode()) if DATA_B64 else None
    lines = []
    say = lines.append
    pkg = find_mooncake()
    if not pkg or not data:
        print("t51_mc_precheck=%s mooncake=%s data=%s" % (TOOL, "found" if pkg else "not_found", "ok" if data else "none"))
        print("mc_pre result=STOP_missing")
        return 1
    site_dir = os.path.dirname(pkg)
    pid, maps = vllm_maps(want_pid)
    stop = []
    say("t51_mc_precheck=%s build=%s pid=%s loaded=%d" % (TOOL, data["build"], pid if pid else "none", len(maps)))

    # 1. the site's store library and any second copy of it
    store = os.path.join(pkg, STORE)
    st = elf_info(store)
    size, s16 = os.path.getsize(store), sha16(store)
    exp_f1 = data["site_f1"]
    same_f1 = size == exp_f1["size"] and s16 == exp_f1["sha16"]
    # glm53mc-6: the loaded library may be the earlier replacement (v1) instead of the site's original: the upgrade
    # v1 -> v2. The original's checks (build identity, export families) were done before v1 went in; here only the
    # exports against v2, the consumers and v2's imports are checked, and the result is ok_upgrade.
    uf = data.get("upgrade_from") or {}
    is_v1 = bool(uf) and size == uf.get("size") and s16 == uf.get("sha16")
    if not same_f1 and not is_v1:
        stop.append("STOP_changed")
    roots = [pkg] + [os.path.join(site_dir, d) for d in sorted(os.listdir(site_dir))
                     if d.startswith("mooncake") and d.endswith(".libs")]
    files, second = [], []
    for root in roots:
        for dirpath, dirnames, names in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for n in names:
                p = os.path.join(dirpath, n)
                if ".so" in n and os.path.isfile(p) and not os.path.islink(p):
                    files.append(p)
                    if p != store and (n.startswith("libmooncake_store") or "libmooncake_store" in n):
                        second.append(p)
    for p in maps:
        if os.path.basename(p).startswith("libmooncake_store") and os.path.realpath(p) != os.path.realpath(store):
            second.append(p)
    if second:
        stop.append("STOP_second_copy")
    say("mc_orig size=%d sha16=%s f1=%s soname=%s path=%s needed=%d tls=%d(lab %d) so_files=%d second_store=%s"
        % (size, s16, "same" if same_f1 else ("replacement_%s" % uf.get("label", "v1") if is_v1 else "DIFFERS"), st["soname"], st["path"], len(st["needed"]), st["tls"],
           data["lab_tls"], len(files), ",".join(sorted({rel(p, site_dir) for p in second})) or "none"))
    lab_pb = [tuple(r) for r in data["lab_progbits"]]
    s_by = {r[3]: tuple(r[:3]) for r in st["progbits"]}
    l_by = {r[3]: tuple(r[:3]) for r in lab_pb}
    other = sorted(n for n in set(s_by) | set(l_by) if n != ".rodata" and s_by.get(n) != l_by.get(n))
    lp = data["lab_paths"]
    pairs = rodata_paths(store, lp["rels"])
    prefixes = [pre for pre, _ in pairs]
    n_site = len(prefixes)
    lens = sorted({len(x) for x in prefixes})
    l_site = max(lens, key=lambda v: sum(1 for x in prefixes if len(x) == v)) if prefixes else 0
    p_hash = hashlib.sha256(sorted(set(prefixes))[0]).hexdigest()[:8] if prefixes else "-"
    r_site = s_by.get(".rodata", (0, 0, 0))[2]
    r_lab = l_by.get(".rodata", (0, 0, 0))[2]
    diff = r_site - r_lab
    p_pad8, p_align16 = rodata_predict(pairs, lp["rels"], lp["L"], data["rodata_end16"])
    predicted = p_pad8 + p_align16
    same_rels = sorted(rel for _, rel in pairs) == sorted(lp["rels"])
    if is_v1:
        verdict = "skipped_upgrade"
    elif not prefixes:
        stop.append("STOP_model_inapplicable")
        verdict = "model_inapplicable"
    elif other or n_site != lp["N"] or not same_rels or len(set(prefixes)) != 1 or diff != predicted:
        stop.append("STOP_build_differs")
        verdict = "differs"
    else:
        verdict = "same_build"
    # diagnostics only (no part of the verdict): where a difference is. The path strings sit in 8-byte slots, so a
    # difference that comes from the build path alone is also a multiple of 8; div4/div8 cannot tell it from code.
    hint = ""
    if is_v1:   # not judged: the loaded library is v1, which is not the lab check build
        pass
    elif other:
        hint = " hint=codegen(sections_other)"
    elif verdict == "differs" and (len(set(prefixes)) != 1 or n_site != lp["N"] or not same_rels):
        hint = " hint=paths"
    elif verdict == "differs":
        hint = " hint=rodata_only"
    say("mc_build sections_other=%s rodata site=%d lab=%d actual=%+d predicted=%+d(pad8 %+d,align16 %+d) paths N'=%d(lab %d) "
        "L'=%d(lab %d) prefixes=%d hash=%s div4=%s div8=%s result=%s%s"
        % ("same" if not other else ",".join("%s:%s/%s" % (n, (s_by.get(n) or ("-",) * 3)[2], (l_by.get(n) or ("-",) * 3)[2]) for n in other[:4]),
           r_site, r_lab, diff, predicted, p_pad8, p_align16, n_site, lp["N"], l_site, lp["L"], len(set(prefixes)), p_hash,
           boolstr(diff % 4 == 0), boolstr(diff % 8 == 0), verdict, hint))
    say("mc_size total=%d (lab after build_wheel.sh steps: %s; about 2.2 MB of each patchelf call is padding, not compiled code)"
        % (size, ",".join("%s:%d" % kv for kv in sorted(data["lab_wheel_sizes"].items()))))

    # 2. exported names by family
    lab = set(data["lab_exports"])
    repl = (lab - set(data["patch_removed"])) | set(data["patch_added"])
    site_b, site_names = buckets(st["exp"])
    lab_b, lab_names = buckets(lab)
    diff_f = [f for f in FAMILIES if site_b[f] != lab_b[f]]
    if is_v1:   # the loaded library is v1, which differs from the lab check build by patch 04's 3 names only
        diff_f = []
    removed_vs_site = st["exp"] - repl
    added_vs_site = repl - st["exp"]
    if is_v1:   # upgrade: v2 exports exactly what the loaded v1 exports
        delta_ok = not removed_vs_site and not added_vs_site
        want_added = 0
    else:
        delta_ok = not removed_vs_site and added_vs_site == set(data["patch_added"]) and len(added_vs_site) == data["expected_added_count"]
        want_added = data["expected_added_count"]
    if not delta_ok:
        stop.append("STOP_export_delta")
    say("mc_exports site=%d/%s lab=%d/%s replacement=%d differing_families=%s replacement_vs_site=-%d/+%d(expected -0/+%d)%s"
        % (len(st["exp"]), hset(st["exp"]), len(lab), hset(lab), len(repl), ",".join(diff_f) or "none",
           len(removed_vs_site), len(added_vs_site), want_added, "" if delta_ok else " DIFFERS"))
    fam = ["%s=%d/%s%s" % (f, site_b[f][0], site_b[f][1][:8], "" if f not in diff_f else "!lab=%d/%s" % (lab_b[f][0], lab_b[f][1][:8]))
           for f in FAMILIES]
    say("mc_fam " + " ".join(fam[:7]))
    say("mc_fam " + " ".join(fam[7:]))
    versioned = []
    diffs = []
    for f in diff_f:
        s_only = sorted(set(site_names[f]) - set(lab_names[f]), key=len)
        l_only = sorted(set(lab_names[f]) - set(site_names[f]), key=len)
        if any(VERSIONED_NS.search(n) for n in s_only + l_only) and f != "std":
            versioned.append(f)
        diffs.append((len(s_only) + len(l_only), f, s_only, l_only))
    extra = []
    for k, (_n, f, s_only, l_only) in enumerate(sorted(diffs, reverse=True)):
        line = ("mc_diff %s site_only=%d lab_only=%d e.g. site:%s lab:%s"
                % (f, len(s_only), len(l_only), (s_only[0][:90] if s_only else "-"), (l_only[0][:90] if l_only else "-")))
        if k < 7:
            say(line)
        for n in (s_only[:show] if show else []):
            extra.append("mc_name %s site_only %s" % (f, n))
        for n in (l_only[:show] if show else []):
            extra.append("mc_name %s lab_only %s" % (f, n))
    if "mooncake" in diff_f:
        stop.append("STOP_report(mooncake)")
    align = sorted(set([f for f in diff_f if f in ALIGN_FAMILIES] + versioned))
    if align:
        stop.append("STOP_align(%s)" % "+".join(align))

    # 3. what the package's other libraries (and the loaded ones) import from the store library
    revdeps = sorted({p for p in files + [m for m in maps if "mooncake" in m] if p != store and
                      os.path.isfile(p) and not os.path.basename(p).startswith("libmooncake_store")})
    rd_lines, rd_missing = [], 0
    for p in revdeps:
        try:
            info = elf_info(p)
        except (OSError, ValueError, struct.error, IndexError):
            info = None
        if not info or not any(n.startswith("libmooncake_store") for n in info["needed"]):
            continue
        need = (info["imp_strong"] | info["imp_weak"]) & st["exp"]
        miss = need - repl
        rd_missing += len(miss)
        rd_lines.append("%s=%d/%s covered=%d" % (rel(p, site_dir), len(need), hset(need), len(need) - len(miss)))
    if rd_missing:
        stop.append("STOP_missing_exports")
    # every ELF file of the package (not only those that list the store library in NEEDED): undefined references to
    # the names patch 04 removes from the exports
    removed = set(data["patch_removed"])
    n_elf, consumers = 0, []
    for root in roots:
        for dirpath, dirnames, names in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for n in names:
                p = os.path.join(dirpath, n)
                if p == store or os.path.islink(p) or not os.path.isfile(p):
                    continue
                try:
                    with open(p, "rb") as fh:
                        if fh.read(4) != b"\x7fELF":
                            continue
                    info = elf_info(p)
                except (OSError, ValueError, struct.error, IndexError):
                    info = None
                if info is None:
                    continue
                n_elf += 1
                hit = (info["imp_strong"] | info["imp_weak"]) & removed
                if hit:
                    consumers.append("%s:%s" % (rel(p, site_dir), "+".join(hashlib.sha256(x.encode()).hexdigest()[:8] for x in sorted(hit))))
    if consumers:
        stop.append("STOP_consumer_refs_removed")
        say("mc_consumers refs_removed=" + ",".join(consumers[:6]) + ("" if len(consumers) <= 6 else " +%d" % (len(consumers) - 6)))
    else:
        say("consumers_ok n_elf=%d removed_names=%d" % (n_elf, len(removed)))
    say("mc_revdep " + (" ".join(rd_lines[:4]) if rd_lines else "none") + ("" if len(rd_lines) <= 4 else " +%d" % (len(rd_lines) - 4)))

    # 4. the replacement's imports and symbol versions against the libraries it would load on site
    by_name = {}
    for p in sorted(maps):
        by_name.setdefault(os.path.basename(p), p)
    origin = pkg
    search = [origin] + roots[1:] + [d for d in os.environ.get("LD_LIBRARY_PATH", "").split(":") if d] + list(SYS_DIRS)

    def locate(lib):
        if lib in by_name:
            return by_name[lib]
        for d in search:
            p = os.path.join(d, lib)
            if os.path.isfile(p):
                return p
        return None

    providers, unchecked, queue, seen = {}, [], list(data["repl_needed"]), set()
    while queue:
        lib = queue.pop(0)
        if lib in seen:
            continue
        seen.add(lib)
        p = locate(lib)
        try:
            info = elf_info(p) if p else None
        except (OSError, ValueError, struct.error, IndexError):
            info = None
        if not info:
            unchecked.append(lib)
            continue
        providers[lib] = info
        queue.extend(n for n in info["needed"] if n not in seen)
    have = set()
    for info in providers.values():
        have |= info["exp"]
    unresolved = sorted(set(data["repl_imports_strong"]) - have)
    vmiss = []
    for lib, vers in data["repl_verneed"].items():
        info = providers.get(lib)
        if info is None:
            continue
        vmiss += ["%s:%s" % (lib, v) for v in vers if v not in info["verdef"]]
    if unresolved or vmiss:
        stop.append("STOP_unresolved")
    if unchecked:
        stop.append("STOP_incomplete")
    say("mc_imports replacement_strong=%d resolved=%d unresolved=%d%s verneed_missing=%s libs=%d unchecked=%s"
        % (len(data["repl_imports_strong"]), len(data["repl_imports_strong"]) - len(unresolved), len(unresolved),
           (" e.g. " + unresolved[0][:80]) if unresolved else "", ",".join(vmiss[:3]) or "none", len(providers),
           ",".join(unchecked) or "none"))

    # siblings against the lab build (information only; their effect is covered by the import check above)
    sib = []
    for name, want in sorted(data["lab_siblings"].items()):
        p = os.path.join(pkg, name)
        try:
            info = elf_info(p) if os.path.isfile(p) else None
        except (OSError, ValueError, struct.error, IndexError):
            info = None
        if info is None:
            sib.append("%s=missing" % name)
            continue
        lab_s = set(want)
        if info["exp"] == lab_s:
            sib.append("%s=%d/%s" % (name, len(info["exp"]), hset(info["exp"])[:8]))
            continue
        sb, _ = buckets(info["exp"])
        lb, _ = buckets(lab_s)
        fams = [f for f in FAMILIES if sb[f] != lb[f]]
        sib.append("%s=%d/%s!lab=%d(%s)" % (name, len(info["exp"]), hset(info["exp"])[:8], len(lab_s), "+".join(fams)))
    say("mc_sib " + " ".join(sib))
    say("mc_pre result=%s" % (("ok_upgrade" if is_v1 else "ok_replace") if not stop else ",".join(stop)))
    out = lines[:-1][:19] + [lines[-1]]
    print("\n".join(out + extra))
    return 0 if not stop else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TimeoutError:
        print("mc_pre result=STOP_timeout")
        sys.exit(4)
