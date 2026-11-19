"""Which segments of the glm53 segments.json get a value from the filter's output of one case.

python3 segments_with_values.py <segments.json> <case dir> [instance [side]]   (reads <case dir>/filter/<instance>/*.log,
all instances by default; side limits the output to sides.prefill or sides.decode)
A segment is "有值" when at least one request has it: an "acc" source has a pd_seg_partial line whose segs[id]
holds a non-null ns (first_ns for part "first"; for part "rest", ns with more than one step); an "event" source has a passthrough record matching event and
where with a non-null field (records are counted by request_id, or observation_id when they have none). Proxy sources are reported as "proxy" (no proxy line in this test); segments without
a source as "无来源"; a parent without its own source shows its children's sum and has a value when a child has.
"""

import glob
import gzip
import json
import os
import re
import sys

seg_path, case = sys.argv[1:3]
instance = sys.argv[3] if len(sys.argv) > 3 else "**"   # e.g. P or D: read only that instance's filter output
only_side = sys.argv[4] if len(sys.argv) > 4 else None  # prefill or decode
cfg = json.load(open(seg_path))
LINE = re.compile(r"\[PD_DIAG\] (\{.*\})\s*$")
partials, passthrough = [], []
for path in glob.glob(os.path.join(case, "filter", instance, "*.log"), recursive=True) + \
        glob.glob(os.path.join(case, "filter", instance, "*.log.gz"), recursive=True):
    for line in (gzip.open(path, "rt", errors="replace") if path.endswith(".gz") else open(path, errors="replace")):
        m = LINE.search(line)
        if not m:
            continue
        rec = json.loads(m.group(1))
        (partials if rec.get("event") == "pd_seg_partial" else passthrough).append(rec)


def get(d, path):
    for part in path.split("."):
        if not isinstance(d, dict) or part not in d:
            return None
        d = d[part]
    return d


def src_requests(src):
    if src is None:
        return None, "无来源"
    if "proxy" in src:
        return None, "proxy"
    if "acc" in src:
        part = src.get("part", "total")

        def has(seg):
            if part == "first":
                return seg.get("first_ns") is not None
            if part == "rest":  # the steps after the first one
                return seg.get("ns") is not None and (seg.get("steps") or 0) > 1
            return seg.get("ns") is not None

        ids = {p["request_id"] for p in partials if has((p.get("segs") or {}).get(src["acc"], {}))}
        return ids, f"acc:{src['acc']}:{src.get('part', 'total')}"
    # {"event", "where"?, "field"} or, for a ranged stage, {"event", "where", "ranges", "total", "part"}:
    # part "first" reads the ranges, otherwise the total
    if "field" in src:
        path = src["field"]
    elif src.get("part") == "first" and "ranges" in src:
        path = src["ranges"]
    else:
        path = src.get("total")
    # http and engine_submission records carry no top-level request_id: count them by observation_id
    ids = {r.get("request_id") or r.get("observation_id") or r.get("record_id") for r in passthrough
           if r.get("event") == src.get("event") and all(r.get(k) == v for k, v in (src.get("where") or {}).items())
           and get(r, path) not in (None, [])}
    return ids, f"{src['event']}{json.dumps(src.get('where') or {}, ensure_ascii=False)}.{path}"


rows = []


def walk(seg, prefix):
    path = f"{prefix}.{seg['id']}"
    srcs = seg.get("src")
    kids = seg.get("children") or []
    child_ids = set()
    for kid in kids:
        child_ids |= walk(kid, path) or set()
    if srcs is None and not kids:
        rows.append((path, "无来源", 0, ""))
        return set()
    ids, notes = set(), []
    for src in (srcs or []):
        got, note = src_requests(src)
        notes.append(note)
        if got:
            ids |= got
    if not srcs and kids:
        ids, notes = child_ids, ["子段之和"]
    status = "有值" if ids else ("proxy" if notes and all(n == "proxy" for n in notes) else "空")
    rows.append((path, status, len(ids), "; ".join(notes)))
    return ids


for side_name, side in cfg["sides"].items():
    if only_side and side_name != only_side:
        continue
    for key, value in side.items():
        if isinstance(value, list):
            for seg in value:
                if isinstance(seg, dict) and "id" in seg:
                    walk(seg, f"{side_name}.{key}")
print(json.dumps({"case": os.path.basename(case.rstrip("/")), "instance": instance, "side": only_side, "partials": len(partials), "passthrough": len(passthrough),
                  "segments": [{"segment": p, "status": s, "requests": n, "sources": src} for p, s, n, src in rows]},
                 ensure_ascii=False, indent=1))
