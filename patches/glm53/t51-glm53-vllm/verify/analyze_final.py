"""Comparisons for the final tree (v030start) against db27fd1 (v030fan), in the old and the site-like environment."""
import collections, glob, json, os, subprocess, sys
import xml.etree.ElementTree as ET
SP = sys.argv[1]
E = f"{SP}/src/equiv"; A = f"{SP}/src/ascend"
OLD15, OLD16, OLD20 = f"{SP}/runs15", f"{SP}/runs16", f"{SP}/runs20"
PYF, PYS = f"{SP}/runs21_v030fan", f"{SP}/runs21_v030start"


def junit(prefix):
    res = {}
    for f in sorted(glob.glob(prefix + ".*.xml")):
        for tc in ET.parse(f).getroot().iter("testcase"):
            st = "passed"
            for ch in tc:
                if ch.tag in ("failure", "error"): st = "failed"
                elif ch.tag == "skipped": st = "skipped"
            res[tc.get("classname", "") + "::" + tc.get("name")] = st
    return res


def cmp_tests(label, a, b, only=None):
    keys = sorted(set(a) | set(b))
    if only:
        keys = [k for k in keys if only(k)]
    diff = [k for k in keys if a.get(k) != b.get(k)]
    print(f"== tests {label}: {len(a)} vs {len(b)} cases; {collections.Counter(b.values())}; differ: {len(diff)}")
    for k in diff[:40]:
        print(f"   {k}: {a.get(k, 'absent')} -> {b.get(k, 'absent')}")


def records(label, x, y, extra=()):
    out = subprocess.run(["python3", f"{E}/compare.py", x, y, "--values", *extra], capture_output=True, text=True).stdout
    ok = sum(1 for l in out.splitlines() if l.endswith("  OK"))
    tail = out.strip().splitlines()[-1] if out.strip() else "?"
    print(f"== records {label}: {ok} scenarios OK, {tail}")
    for l in out.splitlines():
        if l.startswith("==") and not l.endswith("  OK"):
            print("   " + l[:160])
    return out


def steps(label, x):
    out = subprocess.run(["python3", f"{E}/check_steps.py", x], capture_output=True, text=True).stdout
    print(f"== check_steps {label}: {out.strip().splitlines()[-1]}")


def counts(path):
    return [l.split(" sent")[0] for l in open(path) if "replacement calls" in l]


print("######## old environment (Python 3.12.3, transformers 5.19.0, torch 2.13.0+cpu): final vs db27fd1")
cmp_tests("final vs db27fd1", junit(f"{OLD15}/v030fan"), junit(f"{OLD20}/v030start"))
records("final vs db27fd1 (15 scenarios)", f"{OLD15}/v030fan.equiv.jsonl", f"{OLD20}/v030start.equiv.jsonl")
records("v0.32.0+01 vs final", f"{OLD20}/v032p.equiv.jsonl", f"{OLD20}/v030start.equiv.jsonl", ("--unordered-links",))
steps("final", f"{OLD20}/v030start.equiv.jsonl")
for m in ("baseline", "addreq_wrap", "p_both"):
    records(f"sim {m} final vs db27fd1", f"{OLD15}/v030fan.{m}.jsonl", f"{OLD20}/v030start.{m}.jsonl")
for m in ("addreq_wrap_nokw", "addreq_wrap_reject", "stream_wrap", "stream_wrap_force"):
    records(f"sim {m} final vs db27fd1", f"{OLD16}/v030fan.{m}.jsonl", f"{OLD20}/v030start.{m}.jsonl")
for m in ("baseline", "add_copy", "add_wrap", "stream_copy", "stream_wrap", "p_both_copy"):
    records(f"sim copy.{m} final vs db27fd1", f"{OLD16}/v030fan.copy.{m}.jsonl", f"{OLD20}/v030start.copy.{m}.jsonl")
print("== fan-out counts final:", counts(f"{OLD20}/v030start.fanout_count.txt"))

print("######## site-like environment (Python 3.11.10, transformers 5.14.1, torch 2.10.0+cpu)")
print(open(f"{PYS}/env.txt").read().strip())
for d in (PYF, PYS):
    bad = [os.path.basename(f) for f in glob.glob(f"{d}/*.install.txt") if "rc=0" not in open(f).read().splitlines()[-1]]
    print("install failures in", os.path.basename(d), bad)
cmp_tests("final vs db27fd1", junit(f"{PYF}/v030fan"), junit(f"{PYS}/v030start"))
cmp_tests("clean v0.30.0 upstream files, py311 vs old", junit(f"{OLD20}/../runs15/v030") if False else {}, {})
records("final vs db27fd1 (15 scenarios)", f"{PYF}/v030fan.equiv.jsonl", f"{PYS}/v030start.equiv.jsonl")
records("final: site-like vs old environment", f"{OLD20}/v030start.equiv.jsonl", f"{PYS}/v030start.equiv.jsonl")
records("v0.32.0+01 (old env) vs final (site-like)", f"{OLD20}/v032p.equiv.jsonl", f"{PYS}/v030start.equiv.jsonl", ("--unordered-links",))
steps("final site-like", f"{PYS}/v030start.equiv.jsonl")
for m in ("baseline", "addreq_wrap", "addreq_wrap_nokw", "addreq_wrap_reject", "stream_wrap", "stream_wrap_force", "p_both"):
    records(f"sim {m} final vs db27fd1", f"{PYF}/v030fan.{m}.jsonl", f"{PYS}/v030start.{m}.jsonl")
for m in ("baseline", "add_copy", "add_wrap", "stream_copy", "stream_wrap", "p_both_copy"):
    records(f"sim copy.{m} final vs db27fd1", f"{PYF}/v030fan.copy.{m}.jsonl", f"{PYS}/v030start.copy.{m}.jsonl")
for m in ("baseline", "addreq_wrap", "p_both", "stream_wrap_force"):
    records(f"sim {m} final: site-like vs old", f"{OLD20}/v030start.{m}.jsonl" if m != "stream_wrap_force" else f"{OLD20}/v030start.{m}.jsonl", f"{PYS}/v030start.{m}.jsonl")
print("== fan-out counts clean v0.30.0:", counts(f"{PYS}/v030.fanout_count.txt"))
print("== fan-out counts final:", counts(f"{PYS}/v030start.fanout_count.txt"))
