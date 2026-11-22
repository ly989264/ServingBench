python3 -I -S -B -c '
import os, re, struct, hashlib, zlib, base64
STORE = "libmooncake_store.so"
LAB = ("GCC: (GNU) 12.3.1 (openEuler 12.3.1-110.oe2403sp4)", "GCC: (GNU) 12.3.1 (openEuler 12.3.1-112.oe2403sp4)")
OK = (r"GCC: \(GNU\) \d{1,2}\.\d{1,2}\.\d{1,2}( \d{8})? \(openEuler \d{1,2}\.\d{1,2}\.\d{1,2}-\d{1,4}\.oe\d{4}(sp\d{1,2})?\)",
      r"GCC: \(GNU\) \d{1,2}\.\d{1,2}\.\d{1,2}( \d{8})?( \(Red Hat \d{1,2}\.\d{1,2}\.\d{1,2}-\d{1,3}\))?",
      r"GCC: \(Ubuntu \d{1,2}\.\d{1,2}\.\d{1,2}-\d{1,2}ubuntu\d{1,2}(~\d{2}\.\d{2}(\.\d{1,2})?)?\) \d{1,2}\.\d{1,2}\.\d{1,2}",
      r"clang version \d{1,2}\.\d{1,2}\.\d{1,2}", r"Linker: LLD \d{1,2}\.\d{1,2}\.\d{1,2}")
LAB_N, LAB_NAMES = 9906, "9fc3fc3b0613f98b"
LAB_SIZES = """
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
"""
me = os.getpid()
want = os.environ.get("PID", "").strip()
if want and not want.isdigit():
    raise SystemExit("mc_comment result=STOP PID=%s 不是进程号" % want[:20])
pids = [int(want)] if want else sorted((int(p) for p in os.listdir("/proc") if p.isdigit() and int(p) != me), reverse=True)
pid, path = None, None
for p in pids:
    try:
        cmd = open("/proc/%d/cmdline" % p, "rb").read(4096)
        if not want and b"vl" b"lm" not in cmd.lower():
            continue
        for l in open("/proc/%d/maps" % p):
            f = l.split(None, 5)
            if len(f) == 6 and f[5].strip().startswith("/") and os.path.basename(f[5].strip()) == STORE:
                pid, path = p, f[5].strip()
                break
    except OSError:
        continue
    if pid:
        break
if not pid:
    raise SystemExit("mc_comment result=STOP " + ("进程 %s 不存在或没有映射 %s" % (want, STORE) if want else "没有找到映射 %s 的 vLLM 进程（vLLM 没在运行，或者没加载 Mooncake）" % STORE))
def sections(fh):
    fh.seek(0)
    e = fh.read(64)
    if e[:4] != b"\x7fELF" or e[4] != 2 or e[5] != 1:
        raise SystemExit("mc_comment result=STOP 不是 64 位小端 ELF")
    shoff, = struct.unpack_from("<Q", e, 0x28)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", e, 0x3A)
    fh.seek(shoff)
    sh = fh.read(shentsize * shnum)
    ss = [struct.unpack_from("<IIQQQQIIQQ", sh, i * shentsize) for i in range(shnum)]
    fh.seek(ss[shstrndx][4])
    nm = fh.read(ss[shstrndx][5])
    return [(nm[s[0]:nm.index(b"\0", s[0])].decode("ascii", "replace"), s) for s in ss]
def read(fh, s):
    fh.seek(s[4])
    return fh.read(s[5])
with open(path, "rb") as fh:
    h = hashlib.sha256()
    for b in iter(lambda: fh.read(1 << 20), b""):
        h.update(b)
    size = fh.tell()
    secs = sections(fh)
    by = dict(secs)
    com = read(fh, by[".comment"]) if ".comment" in by else b""
    funcs = []
    if ".dynsym" in by:
        sym = by[".dynsym"]
        st = read(fh, secs[sym[6]][1])
        d = read(fh, sym)
        for i in range(0, len(d) - 23, 24):
            n, info, other, shndx, value, sz = struct.unpack_from("<IBBHQQ", d, i)
            if (info & 15) in (2, 10) and shndx != 0:
                funcs.append((st[n:st.index(b"\0", n)], sz))
print("mc_comment pid=%d%s file=%s/%s size=%d sha16=%s comment_bytes=%d flags=%s"
      % (pid, "(PID=)" if want else "", "mooncake" if os.path.basename(os.path.dirname(path)) == "mooncake" else "<其他目录>", STORE,
         size, h.hexdigest()[:16], len(com), hex(by[".comment"][2]) if ".comment" in by else "-"))
for i, s in enumerate([x for x in com.split(b"\0") if x], 1):
    t = s.decode("ascii", "replace")
    if any(re.fullmatch(r, t) for r in OK):
        lab = [str(j) for j, x in enumerate(LAB, 1) if x == t]
        print("mc_comment s%d len=%d text=%s lab=%s" % (i, len(s), t, ("同实验室第 " + lab[0] + " 条") if lab else "实验室没有"))
    else:
        print("mc_comment s%d len=%d sha16=%s text=不打印（不是已知的编译器标识格式）" % (i, len(s), hashlib.sha256(s).hexdigest()[:16]))
print("mc_comment lab_bytes=102 lab_s1=%s lab_s2=%s" % LAB)
raw = zlib.decompress(base64.b64decode("".join(LAB_SIZES.split())))
lab_sizes, v, sh = [], 0, 0
for c in raw:
    v |= (c & 0x7F) << sh
    sh += 7
    if not c & 0x80:
        lab_sizes.append(v)
        v, sh = 0, 0
funcs.sort()
names = [n for n, _ in funcs]
nh = hashlib.sha256(b"\n".join(names)).hexdigest()[:16]
symtab = "yes" if ".symtab" in by else "none"
if len(lab_sizes) != LAB_N:
    raise SystemExit("mc_func result=STOP 内嵌的实验室数据不完整")
if nh != LAB_NAMES or len(funcs) != LAB_N:
    print("mc_func table=.dynsym symtab=%s total=%d(lab %d) names=differ(%s, lab %s)" % (symtab, len(funcs), LAB_N, nh, LAB_NAMES))
    print("mc_func result=names_differ")
else:
    bad = [n for (n, s), ls in zip(funcs, lab_sizes) if s != ls]
    print("mc_func table=.dynsym symtab=%s total=%d(lab %d) names=same same=%d differ=%d" % (symtab, len(funcs), LAB_N, LAB_N - len(bad), len(bad)))
    if bad:
        print("mc_func differ_sha16=" + ",".join(hashlib.sha256(n).hexdigest()[:16] for n in bad[:20]) + (" …" if len(bad) > 20 else ""))
    print("mc_func result=" + ("all_same" if not bad else "differs"))
'
