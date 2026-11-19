"""Scan of the site patch (every line: added, removed, context) and of files/ for personal data.
usage: scan_site.py PATCH FILES_DIR [--name NAME ...]
  --name: further strings to look for, given at run time so that they are not stored here (for example the name of
          a colleague's personal build directory); reported under one generic label."""
import collections, os, re, sys
patch, files = sys.argv[1], sys.argv[2]
NAMES = [sys.argv[i + 1] for i in range(3, len(sys.argv) - 1) if sys.argv[i] == "--name"]
CATS = [
    ("personal dir under /data, /home, /root", re.compile(r"/(?:data|home|root)/[A-Za-z0-9_.*-]+")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("id-style string (letter + 8 digits)", re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]\d{8}(?![0-9])")),
    ("IPv4", re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")),
    ("host name", re.compile(r"\b(?:[a-z0-9-]+\.)+(?:huawei\.com|huaweicloud\.com|corp|internal|intra|lan)\b|\b(?:node|host|server)[-_]?\d{2,}\b", re.I)),
]
if NAMES:
    CATS.insert(1, ("names given at run time (colleague's personal build directory etc.)",
                    re.compile("|".join(re.escape(n) for n in NAMES), re.I)))
GH = re.compile(r"github\.com/([A-Za-z0-9_.-]+)")
sources = []
for n, l in enumerate(open(patch, errors="replace"), 1):
    sources.append(("patch", n, l))
for d, _, fs in os.walk(files):
    for x in sorted(fs):
        p = os.path.join(d, x)
        for n, l in enumerate(open(p, errors="replace"), 1):
            sources.append(("files/" + os.path.relpath(p, files), n, l))
hits = collections.defaultdict(list)
gh = collections.Counter()
for where, n, l in sources:
    for name, p in CATS:
        for m in p.finditer(l):
            v = m.group(0)
            if name == "IPv4" and all(int(x) <= 255 for x in v.split(".")) and (v.startswith("127.") or v == "0.0.0.0" or v.startswith(("192.0.2.", "198.51.100.", "203.0.113."))):
                hits["IPv4 (loopback or reserved documentation range, allowed)"].append((where, n, v))
                continue
            hits[name].append((where, n, v, l.strip()[:110]))
    for m in GH.finditer(l):
        gh[(m.group(1), "patch" if where == "patch" else "files")] += 1
for name, _ in CATS:
    print(f"== {name}: {len(hits[name])}")
    for h in hits[name][:20]:
        print("   ", " | ".join(map(str, h)))
k = "IPv4 (loopback or reserved documentation range, allowed)"
print(f"== {k}: {len(hits[k])}", sorted({h[2] for h in hits[k]}))
print("== github.com/<account> (count per account, in patch / in files/)")
accts = sorted({a for a, _ in gh})
for a in accts:
    print(f"    {a}: patch {gh[(a, 'patch')]}, files {gh[(a, 'files')]}")
