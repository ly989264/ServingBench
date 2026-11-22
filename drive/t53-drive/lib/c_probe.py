"""t53drive container probe: read only. Run inside the vLLM container as root:  python3 -I -S -B - <mode> [args] < c_probe.py

Reads /proc, file contents and file metadata only. Writes no file, starts no process, sends no signal, opens no
network connection, imports only the standard library. Every output line is "key=value" or "S <text>".

  procs                 the vLLM processes: ports, start script, launcher files, log files, environment, command lines
  noproc                how many vLLM processes are left (stage ③)
  devino <path>         device and inode of <path> (compared with the host's)
  mounttype <path>      file system type of <path> and whether it is mounted read-only
  dfree <path>          free GB below <path>
  writable <dir>        whether every vLLM process's user can create files in <dir> and its subfolders (computed)
  exports <script>      the two exports in the start script: absent (with the line to insert before), ok, or bad
  sizes <file> ...      current sizes of the vLLM log files (offsets for the later scans)
  scan <file:offset> …  after the offsets: PD_DIAG_STARTUP lines (counted) and tracebacks that name this kit's modules
  files <kit patches>   the files the kit will replace: same as the public versions, new files absent, site patches
  mcmaps                which libmooncake_store each process maps (new / old = deleted), by instance port
  proxy <file>          the proxy main process: count, PD_PROXY_DIAG* environment, start time, file sha256
  sha <file> ...        sha256 of files
"""
import hashlib
import os
import re
import sys
import sysconfig

PAT = ("vllm" + " serve", "vllm.entrypoints" + ".openai.api_server")
ME = os.getpid()
NEED = ("VLLM_LOGGING_CONFIG_PATH", "PYTHONPATH", "PD_DIAG_LEVEL", "MC_LOG_DIR", "MC_PD_TIMING", "LD_LIBRARY_PATH",
        "VLLM_PD_DEVICE_STAGES", "MOONCAKE_CONFIG_PATH")
MODS = ("pd_diagnostics", "pd_device_timing", "worker_base", "ascend_store", "mooncake_connector", "recompute_scheduler")


def out(k, v):
    print("%s=%s" % (k, v))


def sha(path, n=64):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()[:n]


def readb(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def argv_of(pid):
    b = readb("/proc/%s/cmdline" % pid)
    return [x.decode("utf-8", "replace") for x in b.split(b"\0") if x] if b else []


def env_of(pid):
    b = readb("/proc/%s/environ" % pid)
    d = {}
    for x in (b or b"").split(b"\0"):
        k, _, v = x.decode("utf-8", "replace").partition("=")
        if k:
            d[k] = v
    return d


def status_of(pid):
    st = {}
    try:
        for line in open("/proc/%s/status" % pid):
            k, _, v = line.partition(":")
            st[k] = v.split()
    except OSError:
        pass
    return st


def start_ticks(pid):
    b = readb("/proc/%s/stat" % pid)
    if not b:
        return 0
    return int(b.rsplit(b")", 1)[1].split()[19])


def all_pids():
    return [int(p) for p in os.listdir("/proc") if p.isdigit() and int(p) != ME]


def is_vllm(argv):
    a = " ".join(argv)
    return any(x in a for x in PAT)


def vllm_pids():
    return [p for p in all_pids() if is_vllm(argv_of(p))]


def port_in(argv):
    for i, x in enumerate(argv):
        if x == "--port" and i + 1 < len(argv):
            return argv[i + 1]
        if x.startswith("--port="):
            return x.split("=", 1)[1]
    return None


def ppid(pid):
    st = status_of(pid)
    return int(st["PPid"][0]) if "PPid" in st else 0


def ancestors(pid):
    chain, p = [], ppid(pid)
    while p > 1 and len(chain) < 40:
        chain.append(p)
        p = ppid(p)
    return chain


def resolve(pid, path):
    if path.startswith("/"):
        return path
    try:
        return os.path.normpath(os.path.join(os.readlink("/proc/%d/cwd" % pid), path))
    except OSError:
        return path


def fd_target(pid, fd):
    """The file this fd writes to; through a pipe, the file a `tee` at the other end writes (template `| tee log`)."""
    try:
        t = os.readlink("/proc/%d/fd/%d" % (pid, fd))
    except OSError:
        return None
    if t.startswith("pipe:"):
        for q in all_pids():
            av = argv_of(q)
            if not av or os.path.basename(av[0]) != "tee":
                continue
            try:
                if os.readlink("/proc/%d/fd/0" % q) != t:
                    continue
            except OSError:
                continue
            files = [resolve(q, x) for x in av[1:] if not x.startswith("-")]
            files = [f for f in files if os.path.isfile(f)]
            return files[0] if files else None
        return None
    return t if t.startswith("/") and os.path.isfile(t) else None


def m_procs():
    pids = vllm_pids()
    out("vllm_procs", len(pids))
    if not pids:
        return
    api = [p for p in pids if port_in(argv_of(p))]
    ports = sorted({int(port_in(argv_of(p))) for p in api if (port_in(argv_of(p)) or "").isdigit()})
    out("ports", ",".join(map(str, ports)))
    # The start script, launcher and template: the files in the ancestors of the API servers.
    outer, launch, tmpl = set(), set(), set()
    for p in api:
        last = None
        for a in ancestors(p):          # nearest first; the last .sh seen is the outermost one (closest to pid 1)
            av = argv_of(a)
            for x in av[1:3]:
                if x.endswith("launch_online_dp.py"):
                    launch.add(resolve(a, x))
                elif x.endswith("run_dp_template.sh"):
                    tmpl.add(resolve(a, x))
                elif x.endswith(".sh") and av and os.path.basename(av[0]) in ("bash", "sh", "dash"):
                    last = resolve(a, x)
        if last:
            outer.add(last)
    out("start_scripts", ";".join(sorted(outer)) or "-")
    out("launchers", ";".join(sorted(launch)) or "-")
    out("templates", ";".join(sorted(tmpl)) or "-")
    logs = sorted({t for p in api for t in (fd_target(p, 1), fd_target(p, 2)) if t})
    out("logs", ";".join(logs) or "-")
    envs = {}
    for p in pids:
        e = env_of(p)
        for k in NEED:
            envs.setdefault(k, set()).add(e.get(k, "<unset>"))
    for k in NEED:
        v = envs.get(k, {"<unset>"})
        out("env_" + k, "|".join(sorted(v)) if len(v) <= 3 else "%d different values" % len(v))
    ids = {}
    for p in pids:
        st = status_of(p)
        try:
            key = "%s:%s:%s" % (st["Uid"][3], st["Gid"][3], ",".join(sorted(set(st.get("Groups", [])))) or "-")
        except (KeyError, IndexError):
            continue
        ids[key] = ids.get(key, 0) + 1
    out("ids", ";".join("%s*%d" % kv for kv in sorted(ids.items())))
    cl = {}
    for p in pids:
        c = " ".join(argv_of(p))
        cl[c] = cl.get(c, 0) + 1
    for c, n in sorted(cl.items()):
        print("S cmd %d %s" % (n, c))
    out("cmdline_sha16", hashlib.sha256("\n".join("%d %s" % (n, c) for c, n in sorted(cl.items())).encode()).hexdigest()[:16])
    out("start_ticks_max", max(start_ticks(p) for p in pids))
    out("purelib", sysconfig.get_paths()["purelib"])


def m_noproc():
    out("vllm_procs", len(vllm_pids()))


def m_devino(path):
    try:
        s = os.stat(path)
        out("devino", "%d:%d" % (s.st_dev, s.st_ino))
    except OSError as e:
        out("devino", "missing:" + type(e).__name__)


def mount_of(path):
    R = os.path.realpath(path)
    mp, ro, fs = "", False, "?"
    for line in open("/proc/self/mountinfo"):
        f = line.split(" - ")
        a = f[0].split()
        m = re.sub(r"\\([0-7]{3})", lambda g: chr(int(g.group(1), 8)), a[4])
        if (R == m or R.startswith(m.rstrip("/") + "/")) and len(m) >= len(mp):
            mp = m
            ro = "ro" in a[5].split(",") or (len(f) > 1 and "ro" in f[1].split()[-1].split(","))
            fs = f[1].split()[0] if len(f) > 1 else "?"
    return mp, ro, fs


def m_mounttype(path):
    mp, ro, fs = mount_of(path)
    out("fstype", fs)
    out("readonly", int(ro))


def m_dfree(path):
    s = os.statvfs(path)
    out("free_gb", "%.1f" % (s.f_bavail * s.f_frsize / 1e9))


def m_writable(D):
    """Old step 15: computed from /proc/<pid>/status and the permission bits, never tried."""
    who = {}
    for p in vllm_pids():
        st = status_of(p)
        try:
            k = (int(st["Uid"][3]), int(st["Gid"][3]), tuple(sorted(set(int(g) for g in st.get("Groups", [])))))
        except (KeyError, IndexError, ValueError):
            continue
        who[k] = who.get(k, 0) + 1
    if not who:
        out("writable", "no_vllm")
        return
    if not os.path.isdir(D):
        out("writable", "missing")
        return
    R = os.path.realpath(D)
    chain, x = [], os.path.dirname(R)
    while x != "/":
        chain.insert(0, (x, 1))
        x = os.path.dirname(x)
    chain.append((R, 3))
    chain += [(os.path.join(R, n), 3) for n in sorted(os.listdir(R)) if os.path.isdir(os.path.join(R, n))]
    info = [(d, need, os.stat(d)) for d, need in chain]
    acl = []
    for d, need, s in info:
        try:
            if any(a.startswith("system.posix_acl") for a in os.listxattr(d)):
                acl.append(d)
        except OSError:
            pass
    mp, ro, fs = mount_of(R)
    verdicts = []
    for (u, g, gs), n in sorted(who.items()):
        bad, unsure = [], []
        for d, need, s in info:
            if u == 0:
                continue
            if s.st_uid == u:
                ok = ((s.st_mode >> 6) & need) == need
            elif d in acl:
                unsure.append(d)
                continue
            elif s.st_gid == g or s.st_gid in gs:
                ok = ((s.st_mode >> 3) & need) == need
            else:
                ok = (s.st_mode & need) == need
            if not ok:
                bad.append(d)
        if ro:
            bad.append("readonly_mount")
        verdicts.append("bad" if bad else "unsure" if unsure else "ok")
        print("S writable uid=%d gid=%d procs=%d %s %s" % (u, g, n, verdicts[-1], ",".join(bad + unsure)[:200]))
    out("writable", "bad" if "bad" in verdicts else "unsure" if "unsure" in verdicts else "ok")


EXP_LINES = ("export VLLM_LOGGING_CONFIG_PATH=/data/pd-diag/conf/pd_diag_logging.json",
             "export PYTHONPATH=/data/pd-diag/conf${PYTHONPATH:+:$PYTHONPATH}")
ANCHOR = re.compile(r"(launch_online_dp\.py|run_dp_template\.sh|\bvllm\s+serve\b|vllm\.entrypoints\.openai\.api_server)")


def m_exports(path):
    try:
        lines = open(path, encoding="utf-8").read().split("\n")
    except (OSError, UnicodeDecodeError) as e:
        out("exports", "bad unreadable:" + type(e).__name__)
        return
    have = [i for i, l in enumerate(lines) if "VLLM_LOGGING_CONFIG_PATH" in l or re.search(r"PYTHONPATH=.*pd-diag/conf", l)]
    anchors = [i for i, l in enumerate(lines) if ANCHOR.search(l) and not l.lstrip().startswith("#")]
    out("script_sha16", sha(path, 16))
    if not anchors:
        out("exports", "bad no_launch_line")
        return
    first = anchors[0]
    if not have:
        out("exports", "absent")
        out("insert_before", first + 1)
        out("anchor_sha8", hashlib.sha256(lines[first].encode()).hexdigest()[:8])
        return
    got = [lines[i].strip() for i in have]
    if got == list(EXP_LINES) and have == [have[0], have[0] + 1] and have[1] < first:
        out("exports", "ok")
        out("at_line", have[0] + 1)
        return
    out("exports", "bad different_or_after_launch lines=%s" % ",".join(str(i + 1) for i in have))


def m_sizes(files):
    for f in files:
        try:
            print("S size %s %d" % (f, os.stat(f).st_size))
        except OSError:
            print("S size %s missing" % f)


def m_scan(specs):
    """Lines written after the offsets: PD_DIAG_STARTUP kinds, and tracebacks naming this kit's modules."""
    kinds, tb, total, raw = {}, 0, 0, 0
    for spec in specs:
        f, _, off = spec.rpartition(":")
        try:
            with open(f, "rb") as fh:
                off = int(off)
                if os.fstat(fh.fileno()).st_size < off:   # the file was written anew at the restart (tee without -a)
                    off = 0
                    print("S scan_rewritten %s" % f)
                fh.seek(off)
                data = fh.read(512 << 20)
        except (OSError, ValueError):
            print("S scan_missing %s" % f)
            continue
        lines = data.decode("utf-8", "replace").split("\n")
        total += len(lines)
        for i, l in enumerate(lines):
            if "PD_DIAG_STARTUP" in l:
                k = l.split("PD_DIAG_STARTUP", 1)[1].strip()
                kinds[k] = kinds.get(k, 0) + 1
            if "[PD_DIAG]" in l:
                raw += 1
            if "ImportError" in l or "Traceback" in l:
                window = "\n".join(lines[i:i + 13])
                if any(m in window for m in MODS):
                    tb += 1
    for k, n in sorted(kinds.items()):
        print("S startup %d %s" % (n, k))
    out("tracebacks", tb)
    out("pd_diag_raw", raw)
    out("scanned_lines", total)


def m_files(K):
    """Old step 18 (read only): the files the kit replaces are the public ones, the new ones absent, site patches."""
    lib = sysconfig.get_paths()["purelib"]
    bad = []

    def sums(root, listfile):
        n = 0
        for line in open(listfile):
            h, name = line.split(None, 1)
            name = name.strip().lstrip("*")
            p = os.path.join(root, name)
            try:
                if sha(p) != h:
                    bad.append("changed:" + name)
            except OSError:
                bad.append("missing:" + name)
            n += 1
        return n
    nv = sums(lib, K + "/vllm/SHA256SUMS.before")
    na = sums(lib, K + "/vllm-ascend/SHA256SUMS.before")
    for listfile in (K + "/vllm/NEW_FILES.txt", K + "/vllm-ascend/NEW_FILES.txt"):
        for name in open(listfile).read().split():
            if os.path.exists(os.path.join(lib, name)):
                bad.append("exists:" + name)
    keep = []
    for line in open(K + "/vllm-ascend/SHA256SUMS.d4ad49796-unchanged"):
        h, name = line.split(None, 1)
        name = name.strip().lstrip("*")
        try:
            if sha(os.path.join(lib, name)) != h:
                keep.append(os.path.basename(name))
        except OSError:
            keep.append(os.path.basename(name) + "(missing)")
    out("before_vllm", nv)
    out("before_ascend", na)
    out("before_bad", ";".join(bad[:6]) or "none")
    out("unchanged_bad", ";".join(keep) or "none")
    F = os.path.join(lib, "vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py")
    out("connector_sha16", sha(F, 16) if os.path.exists(F) else "missing")
    for f in ("__init__.py", "platform/__init__.py", "platform/patch_kv_load_failure_reason.py",
              "platform/patch_prefill_length_validation.py"):
        p = os.path.join(lib, "vllm_ascend/patch", f)
        out("site_" + f.replace("/", "_").replace(".py", ""), sha(p, 16) if os.path.exists(p) else "absent")
    kw = 0
    p = os.path.join(lib, "vllm_ascend/patch/platform/patch_prefill_length_validation.py")
    if os.path.exists(p):
        kw = int("**kwargs" in open(p, encoding="utf-8", errors="replace").read())
    out("prefill_kwargs", kw)


def m_mcmaps():
    def port_of(p):
        while p > 1:
            q = port_in(argv_of(p))
            if q:
                return q
            p = ppid(p)
        return "?"
    seen = {}
    for p in all_pids():
        b = readb("/proc/%d/maps" % p)
        if not b or b"libmooncake_store" not in b:
            continue
        maps = {l.split(None, 5)[-1] for l in b.decode("utf-8", "replace").split("\n") if "libmooncake_store" in l}
        u = "old" if any("(deleted)" in m for m in maps) else "new"
        k = "port=%s %s" % (port_of(p), u)
        seen[k] = seen.get(k, 0) + 1
    for k, n in sorted(seen.items()):
        print("S mc %d %s" % (n, k))
    out("mc_old", sum(n for k, n in seen.items() if k.endswith(" old")))
    out("mc_procs", sum(seen.values()))


def m_proxy(path):
    base = os.path.basename(path)
    tops = []
    for p in all_pids():
        av = argv_of(p)
        if av and re.match(r"python[0-9.]*$", os.path.basename(av[0])) and any(os.path.basename(x) == base for x in av[1:3]):
            tops.append(p)
    out("proxy_procs", len(tops))
    if tops:
        e = env_of(tops[0])
        out("proxy_env", " ".join("%s=%s" % (k, e[k]) for k in sorted(e) if k.startswith("PD_PROXY_DIAG")) or "unset")
        out("proxy_start_ticks", start_ticks(tops[0]))
        st = os.stat("/proc/%d" % tops[0])
        out("proxy_uid", st.st_uid)
    try:
        out("proxy_file_sha", sha(path))
        out("proxy_diag_count", open(path, encoding="utf-8", errors="replace").read().count("PD_PROXY_DIAG"))
    except OSError:
        out("proxy_file_sha", "missing")


def m_mcpid():
    for p in sorted(all_pids()):
        b = readb("/proc/%d/maps" % p)
        if b and b"libmooncake_store" in b:
            out("mc_pid", p)
            return
    out("mc_pid", "none")


def m_mem():
    """Room for the byte comparison (peak about 130 MB): the cgroup limit minus usage, else MemAvailable."""
    def num(path):
        b = readb(path)
        return b.decode().strip() if b else None
    lim, cur = num("/sys/fs/cgroup/memory.max"), num("/sys/fs/cgroup/memory.current")
    if lim is None:
        lim, cur = num("/sys/fs/cgroup/memory/memory.limit_in_bytes"), num("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    avail = None
    for l in (readb("/proc/meminfo") or b"").decode().split("\n"):
        if l.startswith("MemAvailable:"):
            avail = int(l.split()[1]) * 1024
    if lim and lim.isdigit() and int(lim) < 10 ** 15 and cur and cur.isdigit():
        room, need, how = int(lim) - int(cur), 1 << 30, "cgroup"
    else:
        room, need, how = avail or 0, 2 << 30, "meminfo"
    out("mem_room_mb", room >> 20)
    out("mem_from", how)
    out("mem_ok", 1 if room >= need else 0)


def main():
    if sys.flags.isolated != 1 or sys.flags.no_site != 1 or sys.flags.dont_write_bytecode != 1:
        print("REFUSED run with python3 -I -S -B")
        sys.exit(3)
    mode, args = sys.argv[1], sys.argv[2:]
    {"procs": lambda: m_procs(), "noproc": lambda: m_noproc(), "devino": lambda: m_devino(args[0]),
     "mounttype": lambda: m_mounttype(args[0]), "dfree": lambda: m_dfree(args[0]), "writable": lambda: m_writable(args[0]),
     "exports": lambda: m_exports(args[0]), "sizes": lambda: m_sizes(args), "scan": lambda: m_scan(args),
     "files": lambda: m_files(args[0]), "mcmaps": lambda: m_mcmaps(), "proxy": lambda: m_proxy(args[0]), "mcpid": lambda: m_mcpid(), "mem": lambda: m_mem(),
     "sha": lambda: [print("S sha %s %s" % (sha(f) if os.path.exists(f) else "missing", f)) for f in args]}[mode]()


main()
