"""Per-test outcomes of run.sh: T51 tree vs T56 tree, and T56 vs T56 with D-class schedulers (t56_d_plugin).
usage: python3 t56_junit.py OUT_DIR"""
import collections
import glob
import os
import sys
import xml.etree.ElementTree as ET


def load(paths):
    res = {}
    for f in paths:
        for tc in ET.parse(f).getroot().iter("testcase"):
            name = tc.get("classname", "") + "::" + tc.get("name")
            st, msg = "passed", ""
            for ch in tc:
                if ch.tag in ("failure", "error"):
                    st, msg = ch.tag, (ch.get("message") or "").split("\n")[0][:110]
                elif ch.tag == "skipped":
                    st, msg = "skipped", (ch.get("message") or "")[:80]
            res[name] = (st, msg)
    return res


D = sys.argv[1]
xml = sorted(glob.glob(os.path.join(D, "*.xml")))
t51 = load([f for f in xml if os.path.basename(f).startswith("t51.")])
t56 = load([f for f in xml if os.path.basename(f).startswith("t56.") and not f.endswith(".D.xml")])
t56d = load([f for f in xml if f.endswith(".D.xml")])


def summary(res, names=None):
    return dict(collections.Counter(res[n][0] for n in (names if names is not None else res)))


def diff(a, b, la, lb):
    common = sorted(set(a) & set(b))
    changed = [n for n in common if a[n][0] != b[n][0]]
    print(f"== {la} vs {lb}: {len(common)} tests in both, {len(changed)} with a different result")
    for n in changed:
        print(f"  {n}: {la}={a[n][0]} {lb}={b[n][0]} | {b[n][1] or a[n][1]}")
    only_b = sorted(set(b) - set(a))
    only_a = sorted(set(a) - set(b))
    print(f"  only in {lb}: {len(only_b)} {summary(b, only_b)}; only in {la}: {len(only_a)}")
    return only_b


print("T51:", summary(t51), " T56:", summary(t56), " T56 with D classes:", summary(t56d))
new = diff(t51, t56, "T51", "T56")
for n in new:
    if t56[n][0] != "passed":
        print(f"  new test not passing: {n}: {t56[n]}")
d_names = {n: v for n, v in t56.items() if n in t56d}
diff(d_names, t56d, "T56", "T56+D")
fails = sorted(n for n in t56 if t56[n][0] in ("failure", "error"))
print(f"== failing on T56: {len(fails)}")
for n in fails:
    print(f"  {n}: T51={t51.get(n, ('absent',))[0]} | {t56[n][1]}")
