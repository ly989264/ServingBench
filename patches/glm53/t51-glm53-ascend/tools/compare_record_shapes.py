"""Compare the [PD_DIAG] records two trees wrote for the same scenarios (t51_record_shapes.py output).

    python3 compare_record_shapes.py REFERENCE.json CANDIDATE.json

Every field is compared by path. Values that differ on every run (clocks, ids, pids) are compared by
type and presence only. Prints one line per difference, then a per-scenario summary.
"""

from __future__ import annotations

import json
import sys

VOLATILE = {
    "timestamp_ns",
    "record_id",
    "pid",
    "native_tid",
    "host",
    "run_id",
    "task_id",
    "call_id",
    "start_ns",
    "end_ns",
    "duration_ns",
    "queue_enter_ns",
    "queue_wait_ns",
    "start_wall_ns",
    "end_wall_ns",
    "wall_utc_offset_seconds",
}


def kind(value):
    return "null" if value is None else type(value).__name__


def walk(path, left, right, out):
    name = path.rsplit(".", 1)[-1].split("[", 1)[0]
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            child = f"{path}.{key}" if path else key
            if key not in left:
                out.append((child, "<absent>", json.dumps(right[key], sort_keys=True)[:120]))
            elif key not in right:
                out.append((child, json.dumps(left[key], sort_keys=True)[:120], "<absent>"))
            else:
                walk(child, left[key], right[key], out)
        return
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            out.append((path + ".length", len(left), len(right)))
        for index, (a, b) in enumerate(zip(left, right)):
            walk(f"{path}[{index}]", a, b, out)
        return
    if name in VOLATILE:
        if (left is None) != (right is None) or (left is not None and kind(left) != kind(right)):
            out.append((path, kind(left), kind(right)))
        return
    if left != right:
        out.append((path, json.dumps(left)[:120], json.dumps(right)[:120]))


def main():
    reference = {item["scenario"]: item for item in json.load(open(sys.argv[1]))}
    candidate = {item["scenario"]: item for item in json.load(open(sys.argv[2]))}
    total = 0
    for scenario in reference:
        ref, cand = reference[scenario], candidate.get(scenario)
        diffs = []
        if cand is None:
            diffs.append(("<scenario>", "present", "<absent>"))
        else:
            if ref["error"] or cand["error"]:
                diffs.append(("<error>", ref["error"], cand["error"]))
            walk("records", ref["records"], cand["records"], diffs)
        events = [(r["event"], r.get("operation") or r.get("phase") or r.get("transfer_role")) for r in ref["records"]]
        print(f"## {scenario}: records {events}; differences {len(diffs)}")
        for path, left, right in diffs:
            print(f"  {path}: reference={left} candidate={right}")
        total += len(diffs)
    print(f"total differences: {total}")


if __name__ == "__main__":
    main()
