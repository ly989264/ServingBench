"""Compare two pd_equiv.py dumps: record kinds, field sets and masked values.

usage: python3 compare.py A.jsonl B.jsonl [--values]
A = v0.32.0 + patch 01, B = v0.30.0 + port.
"""
import collections
import json
import re
import sys

A, B = sys.argv[1], sys.argv[2]
VALUES = "--values" in sys.argv
UNORDERED = "--unordered-links" in sys.argv
# Lists whose order comes from Python sets or from call interleaving, not content.
SET_LISTS = {"finished_request_ids", "finished_sending", "finished_recving",
             "failed_recving", "preempted_request_ids"}
HEX = re.compile(r"[0-9a-f]{32}")
RID = re.compile(r"-[0-9a-f]{8}(?=$|[^0-9a-f])")


def norm_str(v):
    if HEX.search(v):
        v = HEX.sub("<id>", re.sub(r"-\d+-\d+$", "-<pid>-<n>", v))
    return RID.sub("-<r>", v)
VOLATILE_KEYS = {"pid", "native_tid", "thread_id", "host", "timestamp_ns", "run_id"}


def kind(rec):
    return (rec["event"], rec.get("scope") or rec.get("phase") or "")


def load(path):
    out = collections.defaultdict(list)
    other = collections.defaultdict(list)
    for line in open(path):
        o = json.loads(line)
        if "rec" in o:
            out[o["scenario"]].append(o["rec"])
        else:
            other[o["scenario"]].append({k: v for k, v in o.items() if k != "scenario"})
    return out, other


def shape(v, depth=0):
    """Structure only: dict -> {key: shape}, list -> [union of item shapes]."""
    if isinstance(v, dict):
        return {k: shape(x, depth + 1) for k, x in sorted(v.items())}
    if isinstance(v, list):
        items = {json.dumps(shape(x, depth + 1), sort_keys=True) for x in v}
        return ["|".join(sorted(items))] if items else []
    return type(v).__name__ if v is not None else "null"


def mask(v, key=""):
    if isinstance(v, dict):
        return {norm_str(k): mask(x, k) for k, x in sorted(v.items())}
    if isinstance(v, list):
        items = [mask(x, key) for x in v]
        if key in SET_LISTS or (UNORDERED and key == "links"):
            items = sorted(items, key=lambda x: json.dumps(x, sort_keys=True))
        return items
    if key in VOLATILE_KEYS:
        return "<v>"
    if isinstance(v, str):
        return norm_str(v)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if key.endswith("_ns") or key in ("start_ns", "end_ns") or key in (
            "time_to_first_token", "e2e_latency", "queued_time", "prefill_time",
            "inference_time", "decode_time", "mean_time_per_output_token",
            "response_time_to_first_token", "created",
        ):
            return "<t>"
    return v


def keyset(rec):
    """Flattened set of key paths, list items merged."""
    paths = set()

    def walk(v, p):
        if isinstance(v, dict):
            for k, x in v.items():
                # stage/link/member names are data; keep them in the path
                walk(x, p + (norm_str(k),))
            if not v:
                paths.add(p + ("{}",))
        elif isinstance(v, list):
            if not v:
                paths.add(p + ("[]",))
            for x in v:
                walk(x, p + ("[]",))
        else:
            paths.add(p)

    walk(rec, ())
    return paths


def deep_diff(a, b, p=()):
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            yield from deep_diff(a.get(k, "<absent>"), b.get(k, "<absent>"), p + (k,))
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            yield from deep_diff(x, y, p + (i,))
    elif a != b:
        yield p, a, b


ra, oa = load(A)
rb, ob = load(B)
scen = sorted(set(ra) | set(rb), key=lambda s: (s[0], int(re.sub(r"\D", "", s.split("_")[0]) or 0)))
total_diff = 0
for s in scen:
    ca = collections.Counter(kind(r) for r in ra[s])
    cb = collections.Counter(kind(r) for r in rb[s])
    lines = []
    for k in sorted(set(ca) | set(cb)):
        if ca[k] != cb[k]:
            lines.append(f"  count {k}: 01={ca[k]} port={cb[k]}")
        ka = set().union(*(keyset(r) for r in ra[s] if kind(r) == k)) if ca[k] else set()
        kb = set().union(*(keyset(r) for r in rb[s] if kind(r) == k)) if cb[k] else set()
        for p in sorted(ka - kb):
            lines.append(f"  field only in 01   {k}: {'.'.join(p)}")
        for p in sorted(kb - ka):
            lines.append(f"  field only in port {k}: {'.'.join(p)}")
        if VALUES and ca[k] == cb[k]:
            ma = sorted(json.dumps(mask(r), sort_keys=True) for r in ra[s] if kind(r) == k)
            mb = sorted(json.dumps(mask(r), sort_keys=True) for r in rb[s] if kind(r) == k)
            if ma != mb:
                lines.append(f"  masked values differ {k}: {sum(x != y for x, y in zip(ma, mb))} of {len(ma)}")
                for x, y in zip(ma, mb):
                    if x != y:
                        for path, va, vb in deep_diff(json.loads(x), json.loads(y)):
                            lines.append(f"      {'.'.join(map(str, path))}: 01={json.dumps(va)[:120]} port={json.dumps(vb)[:120]}")
    if json.dumps(mask(oa[s]), sort_keys=True, default=str) != json.dumps(mask(ob[s]), sort_keys=True, default=str):
        lines.append(f"  non-record output differs: 01={json.dumps(oa[s])[:300]} port={json.dumps(ob[s])[:300]}")
    total_diff += len(lines)
    print(f"== {s}: 01 {sum(ca.values())} records, port {sum(cb.values())} records" + ("  OK" if not lines else ""))
    print("\n".join(lines)) if lines else None
print("difference lines:", total_diff)
