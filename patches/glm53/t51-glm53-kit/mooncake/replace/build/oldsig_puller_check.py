"""Negative test, panel side: feed the timing lines of oldsig_test.sh to panel 2's puller (scripts/native_put.py,
parse_native and Associator._emit, unchanged) and show what the panel reads from them.

usage: python3 -I oldsig_puller_check.py <panel2 dir> <stderr.txt of oldsig_test.sh> [--drop-year]

--drop-year: remove the year from the glog prefix first ("I20261119 08:36:03..." -> "I1119 08:36:03..."). glog 0.6.0,
the version the site's Mooncake wheel bundles, writes the year by default; older glog versions do not.

For each timing line: the phase fields the puller parses (None = absent), and the stages of the pd_native_put record
it writes for a save task with that one call. Then the pd_native_put fields that the glm5.3 segments.json reads.
Checks (exit 1 if one fails):
  - the line of the original-signature wrapper (complete=false) has no phase1 and no total; its record has no
    start_batch_put stage (absent, not 0) and has submit / wait_for_transfer / finalize;
  - the complete line has all four phases and complete=true;
  - no segment of the glm5.3 segments.json reads start_batch_put.
"""
import importlib.util
import json
import os
import re
import sys

YEAR = re.compile(r"^([IWEF])\d{4}(\d{4} )")


def load(path):
    spec = importlib.util.spec_from_file_location("native_put", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fields_read(seg_json):
    out = set()

    def walk(x):
        if isinstance(x, dict):
            if x.get("event") == "pd_native_put" and "field" in x:
                out.add(x["field"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(seg_json)
    return sorted(out)


def main():
    panel, stderr_file = sys.argv[1], sys.argv[2]
    drop_year = "--drop-year" in sys.argv[3:]
    np = load(os.path.join(panel, "scripts", "native_put.py"))
    lines = [l for l in open(stderr_file, encoding="utf-8", errors="replace") if "Timing]" in l]
    with_year = sum(1 for l in lines if YEAR.match(l))
    print("timing lines=%d with_year_prefix=%d drop_year=%s" % (len(lines), with_year, "yes" if drop_year else "no"))
    if drop_year:
        lines = [YEAR.sub(r"\1\2", l) for l in lines]
    written = []
    assoc = np.Associator(lambda node, inst, line: written.append(json.loads(line)))
    ok = True
    seen_incomplete = seen_complete = 0
    for line in lines:
        n = np.parse_native(line)
        if n is None:
            print("unparsed:", line.strip()[:160])
            ok = False
            continue
        phases = {f: n[f] for _, f in np.PHASES}
        task = np._Task("lab", "0", {"request_id": "r", "operation": "save"}, [(n["tid"], n["keys"], n["bytes"])], 0.0)
        task.matched = [n]
        before = len(written)
        assoc._emit(task)
        rec = written[before]
        has_total = " total_us=" in line
        print("line complete=%s scope=%s total_us=%s phases=%s" % (
            str(n["complete"]).lower(), n["scope"], "present" if has_total else "absent",
            ",".join("%s=%s" % (k.split("_")[0], "absent" if v is None else v) for k, v in phases.items())))
        print("  record stages=%s" % ",".join("%s:%d" % (k, v["duration_ns"]) for k, v in rec["stages"].items()))
        if not n["complete"]:
            seen_incomplete += 1
            if (phases["phase1_start_batch_put_us"] is not None or has_total or "start_batch_put" in rec["stages"]
                    or not all(s in rec["stages"] for s in ("submit", "wait_for_transfer", "finalize"))):
                ok = False
        else:
            seen_complete += 1
            if any(v is None for v in phases.values()) or not has_total or len(rec["stages"]) != 4:
                ok = False
    seg = json.load(open(os.path.join(panel, "profiles", "glm53", "segments.json"), encoding="utf-8"))
    used = fields_read(seg)
    print("glm53 segments.json reads from pd_native_put: %s" % ", ".join(used))
    if any("start_batch_put" in f for f in used):
        ok = False
    if seen_incomplete < 2 or seen_complete < 1:
        ok = False
    print("lines incomplete=%d complete=%d result=%s" % (seen_incomplete, seen_complete, "PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
