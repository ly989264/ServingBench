"""Final tree (v030final) against c9ca251 (v030start): old image runs20 vs runs24_old, site-like runs21_v030start vs runs24_py311."""
import collections, glob, os, subprocess, sys
import xml.etree.ElementTree as ET
SP = sys.argv[1]; E = f"{SP}/src/equiv"; A = f"{SP}/src/ascend"
def junit(prefix, exclude=()):
    res = {}
    for f in sorted(glob.glob(prefix + ".*.xml")):
        if any(e in f for e in exclude): continue
        for tc in ET.parse(f).getroot().iter("testcase"):
            st = "passed"
            for ch in tc:
                if ch.tag in ("failure", "error"): st = "failed"
                elif ch.tag == "skipped": st = "skipped"
            res[tc.get("classname", "") + "::" + tc.get("name")] = st
    return res
def tests(label, a, b):
    d = [k for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
    print(f"== tests {label}: {len(a)} vs {len(b)} {dict(collections.Counter(b.values()))}; differ {len(d)}")
    for k in d: print("   ", k, a.get(k, "absent"), "->", b.get(k, "absent"))
def rec(label, x, y, *extra):
    out = subprocess.run(["python3", f"{E}/compare.py", x, y, "--values", *extra], capture_output=True, text=True).stdout
    ok = sum(1 for l in out.splitlines() if l.endswith("  OK"))
    print(f"== records {label}: {ok} OK, {out.strip().splitlines()[-1] if out.strip() else '?'}")
def steps(label, x):
    out = subprocess.run(["python3", f"{E}/check_steps.py", x], capture_output=True, text=True).stdout
    print(f"== check_steps {label}: {out.strip().splitlines()[-1]}")
def counts(p):
    return [l.split(" sent")[0] for l in open(p) if "replacement calls" in l] if os.path.exists(p) else "missing"
for env, base, new in (("old image", f"{SP}/runs24_old", f"{SP}/runs25_old"), ("site-like image", f"{SP}/runs24_py311", f"{SP}/runs25_py311")):
    print(f"######## {env}: split-built tree vs e29f1f9 tree")
    bad = [os.path.basename(f) for f in glob.glob(f"{new}/*install*.txt") if "rc=0" not in (open(f).read().splitlines() or [""])[-1] and "Successfully" not in open(f).read()]
    print("install problems:", bad)
    tests("split-built tree vs e29f1f9 tree", junit(f"{base}/v030final", ("upstream",)), junit(f"{new}/v030split", ("upstream",)))
    if os.path.exists(f"{new}/v030split.tests_v1_core_test_scheduler_upstream.py.xml"):
        tests("upstream test_scheduler split-built tree vs e29f1f9 tree", junit(f"{base}/v030final.tests_v1_core_test_scheduler_upstream"), junit(f"{new}/v030split.tests_v1_core_test_scheduler_upstream"))
    rec("15 scenarios split-built tree vs e29f1f9 tree", f"{base}/v030final.equiv.jsonl", f"{new}/v030split.equiv.jsonl")
    v032 = f"{SP}/runs25_old/v032p.equiv.jsonl"
    rec("v0.32.0+01 vs final", v032, f"{new}/v030split.equiv.jsonl", "--unordered-links")
    steps("final", f"{new}/v030split.equiv.jsonl")
    for m in ("baseline", "addreq_wrap", "addreq_wrap_nokw", "addreq_wrap_reject", "stream_wrap", "stream_wrap_force", "p_both"):
        if os.path.exists(f"{new}/v030split.{m}.jsonl"): rec(f"sim {m}", f"{base}/v030final.{m}.jsonl", f"{new}/v030split.{m}.jsonl")
    for m in ("baseline", "add_copy", "add_wrap", "stream_copy", "stream_wrap", "p_both_copy"):
        if os.path.exists(f"{new}/v030split.copy.{m}.jsonl"): rec(f"sim copy.{m}", f"{base}/v030final.copy.{m}.jsonl", f"{new}/v030split.copy.{m}.jsonl")
    print("== fan-out counts final:", counts(f"{new}/v030split.fanout_count.txt"))
    if os.path.exists(f"{new}/sched.vllm.jsonl"):
        for p in ("vllm balanceP", "original_vllm ported_vllm", "original_balance ported_balance"):
            a, b = p.split()
            out = subprocess.run(["python3", f"{A}/compare_decisions.py", f"{new}/sched.{a}.jsonl", f"{new}/sched.{b}.jsonl"], capture_output=True, text=True).stdout
            print(f"== sched decisions {a} vs {b}: {out.strip().splitlines()[-1]}")
        for v in ("vllm", "balanceP", "ported_vllm", "ported_balance"):
            rec(f"sched records {v} split-built tree vs e29f1f9 tree", f"{base}/sched.{v}.jsonl", f"{new}/sched.{v}.jsonl")
