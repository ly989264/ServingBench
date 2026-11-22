"""Per-test outcomes of run.sh: T56 vs T58, T56 vs T58 with D-class schedulers (t56_d_plugin), new tests.
usage: python3 t58_junit.py OUT_DIR"""
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
pick = lambda tree, d: load([f for f in xml if os.path.basename(f).startswith(tree + ".")  # noqa: E731
                             and f.endswith(".D.xml") == d])
t56, t58, t56d, t58d = pick("t56", False), pick("t58", False), pick("t56", True), pick("t58", True)


def summary(res, names=None):
    return dict(collections.Counter(res[n][0] for n in (names if names is not None else res)))


def diff(a, b, la, lb):
    common = sorted(set(a) & set(b))
    changed = [n for n in common if a[n][0] != b[n][0]]
    print(f"== {la} vs {lb}: {len(common)} tests in both, {len(changed)} with a different result")
    for n in changed:
        print(f"  {n}: {la}={a[n][0]} {lb}={b[n][0]} | {b[n][1] or a[n][1]}")
    only_b, only_a = sorted(set(b) - set(a)), sorted(set(a) - set(b))
    print(f"  only in {lb}: {len(only_b)} {summary(b, only_b)}; only in {la}: {len(only_a)}")
    for n in only_b:
        if b[n][0] != "passed":
            print(f"  new test not passing: {n}: {b[n]}")


print("T56:", summary(t56), " T58:", summary(t58), " T56+D:", summary(t56d), " T58+D:", summary(t58d))
diff(t56, t58, "T56", "T58")
diff(t56d, t58d, "T56+D", "T58+D")
