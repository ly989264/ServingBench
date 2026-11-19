"""Does runtime code under vllm/ import or reference tools/, tests/, docs/? usage: check_runtime_refs.py PATCH FILES_DIR"""
import os, re, sys
patch, files = sys.argv[1], sys.argv[2]
IMPORT = re.compile(r"^\s*(from|import)\s+(tools|tests|docs)\b")
STRPATH = re.compile(r"[\"'](?:\.\./)*(?:tools|tests|docs)/")
DYN = re.compile(r"__file__|\bsubprocess\b|\bPath\(|\bos\.path\b|\bimportlib\b|\bexec\(|\b__import__\(|\brunpy\b")
WORD = re.compile(r"\b(tools|tests|docs)\b")
def check(label, rows):
    hits = [(w, n, l.rstrip()[:150]) for w, n, l in rows if IMPORT.search(l) or STRPATH.search(l) or DYN.search(l)]
    words = [(w, n, l.rstrip()[:150]) for w, n, l in rows if WORD.search(l) and not l.lstrip().startswith("#")]
    print(f"== {label}: import / string path / dynamic loading: {len(hits)}")
    for h in hits: print("   ", h)
    print(f"   lines with the bare words tools/tests/docs (not comments), for reading: {len(words)}")
    for h in words: print("   ", h)
rows = []
for d, _, fs in os.walk(files):
    for x in fs:
        p = os.path.join(d, x)
        rows += [(os.path.relpath(p, files), n, l) for n, l in enumerate(open(p), 1)]
check("25 runtime files, whole files", rows)
cur, added = None, []
for l in open(patch):
    if l.startswith("+++ b/"): cur = l[6:].strip(); continue
    if l.startswith("+") and not l.startswith("+++"): added.append((cur, 0, l[1:]))
check("site patch, added lines", added)
