"""Field-by-field comparison of the PD_DIAG records the p2d tests write: a base run against a run at one level.

Usage: python3 compare_levels.py base.jsonl other.jsonl --level N [--skip-prefix test_t58_] > report.md

Both files come from test_mooncake_connector_pd_diag.py run with P2D_RECORD_DUMP: the base once (for example on the
T56 replacement), the other on the T58 replacement with P2D_TEST_DIAG_LEVEL=N (or without it: N=3). Tests whose
name starts with a --skip-prefix are left out (tests the base file cannot run).

Records are grouped by (test, event, scope, transfer_role) and paired after sorting each group by its semantic
content. In tests on the fake clock every value is compared, except ids, compared by their order of appearance in
the test, and the fields taken from the real wall and perf_counter clocks. In the other tests ids and *_ns values
are compared by JSON type only, everything else by value. The order of the top-level fields is compared too.
Which task completes a request (tracker_completed, request_done_ns on task records) is a race between threads in
the real-clock tests; there it is compared per request instead: the same number of completing tasks.
At level 1 the four request-level fields of LEVEL1_ABSENT must be present in every request_generation record of the
base and absent from the other run; anything else that differs is a difference. Exits 1 on any difference.
"""

import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compare_records import flatten, jtype  # noqa: E402

LEVEL1_ABSENT = ("task_max_queue_wait_ns", "task_max_meta_fetch_ns", "task_max_prepare_ns", "transfer_envelope_ns")
IDS = {"connector_id", "task_id", "request_generation", "call_id", "record_id"}
RACE = {"tracker_completed", "request_done_ns"}  # task records in real-clock tests: compared per request
REAL_CLOCK = {"timestamp_ns", "native_tid", "transfer_scope_duration_ns", "wall_utc_offset_seconds", "host", "pid",
              "run_id"}


def leaf(path):
    return path.rsplit(".", 1)[-1].split("[", 1)[0]


def exact(path):
    """Compared by value in a fake-clock test."""
    name = leaf(path)
    return name not in REAL_CLOCK and not name.endswith("_wall_ns") and name not in IDS


def semantic(path):
    """Compared by value in a real-clock test."""
    return exact(path) and not leaf(path).endswith("_ns")


def load(path, skip):
    groups = defaultdict(list)
    fake = {}
    for line in open(path):
        row = json.loads(line)
        if any(row["scenario"].startswith(prefix) for prefix in skip):
            continue
        record = row["record"]
        fake[row["scenario"]] = row.get("fake_clock", False)
        groups[(row["scenario"], record.get("event"), record.get("scope"), record.get("transfer_role"))].append(record)
    return groups, fake


def ids_in_order(records):
    """Map each id string to its order of first appearance in one test's records."""
    seen = {}
    for record in records:
        for path, value in flatten(record).items():
            if leaf(path) in IDS and isinstance(value, str):
                seen.setdefault(value, len(seen))
    return seen


def sort_key(record, fake):
    flat = flatten(record)
    keep = exact if fake else semantic
    race = RACE if not fake and record.get("scope") == "task" else set()
    return json.dumps({p: v for p, v in flat.items() if keep(p) and p not in LEVEL1_ABSENT and p not in race},
                      sort_keys=True, default=str)


def completing(records):
    """Per request: how many task records say they completed it."""
    count = defaultdict(int)
    for record in records:
        count[record.get("request_id")] += bool(record.get("tracker_completed")) and record.get("request_done_ns") is not None
    return dict(count)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("base")
    parser.add_argument("other")
    parser.add_argument("--level", type=int, required=True)
    parser.add_argument("--skip-prefix", action="append", default=[])
    args = parser.parse_args()
    base, fake_base = load(args.base, args.skip_prefix)
    other, fake_other = load(args.other, args.skip_prefix)
    by_test = defaultdict(lambda: ([], []))
    for (test, *_), records in base.items():
        by_test[test][0].extend(records)
    for (test, *_), records in other.items():
        by_test[test][1].extend(records)
    id_maps = {test: (ids_in_order(a), ids_in_order(b)) for test, (a, b) in by_test.items()}
    print(f"# {os.path.basename(args.base)} vs {os.path.basename(args.other)} at level {args.level}")
    print()
    print("| test | record | field | base | other |")
    print("|---|---|---|---|---|")
    diffs = compared = absent_ok = 0
    for key in sorted(set(base) | set(other), key=str):
        test, scope = key[0], key[2] + (f"/{key[3]}" if key[3] else "")
        fake = fake_base.get(test, False) and fake_other.get(test, False)
        left = sorted(base.get(key, []), key=lambda r: sort_key(r, fake))
        right = sorted(other.get(key, []), key=lambda r: sort_key(r, fake))
        if len(left) != len(right):
            print(f"| {test} | {scope} | (number of records) | {len(left)} | {len(right)} |")
            diffs += 1
        map_a, map_b = id_maps.get(test, ({}, {}))
        race = RACE if not fake and key[2] == "task" else set()
        if race and completing(left) != completing(right):
            print(f"| {test} | {scope} | (completing tasks per request) | {completing(left)} | {completing(right)} |")
            diffs += 1
        for a, b in zip(left, right):
            compared += 1
            fa, fb = flatten(a), flatten(b)
            for path in race:
                fa.pop(path, None)
                fb.pop(path, None)
            dropped = set(LEVEL1_ABSENT) if args.level == 1 and key[2] == "request_generation" else set()
            for path in dropped:
                if path in fa and path not in fb:
                    absent_ok += 1
                    fa.pop(path)
                else:
                    print(f"| {test} | {scope} | `{path}` | {'present' if path in fa else '(none)'} | "
                          f"{'present' if path in fb else '(none)'} |")
                    diffs += 1
                    fa.pop(path, None)
                    fb.pop(path, None)
            order_a = [k for k in a if k not in dropped]
            if order_a != list(b):
                print(f"| {test} | {scope} | (field order) | {order_a} | {list(b)} |")
                diffs += 1
            for path in sorted(set(fa) | set(fb)):
                va, vb = fa.get(path), fb.get(path)
                if path not in fa or path not in fb:
                    shown = (json.dumps(va) if path in fa else "(none)", json.dumps(vb) if path in fb else "(none)")
                elif jtype(va) != jtype(vb):
                    shown = (jtype(va), jtype(vb))
                elif leaf(path) in IDS and fake and isinstance(va, str):
                    if map_a.get(va) == map_b.get(vb):
                        continue
                    shown = (f"id #{map_a.get(va)}", f"id #{map_b.get(vb)}")
                elif (exact(path) if fake else semantic(path)) and va != vb:
                    shown = (json.dumps(va), json.dumps(vb))
                else:
                    continue
                print(f"| {test} | {scope} | `{path}` | {shown[0]} | {shown[1]} |")
                diffs += 1
    fake_tests = sum(1 for t in by_test if fake_base.get(t) and fake_other.get(t))
    print()
    print(f"{len(by_test)} tests ({fake_tests} on the fake clock), {compared} pairs of records compared, "
          f"{diffs} differences" + (f"; the four level-1 fields absent as expected in {absent_ok} places" if args.level == 1 else ""))
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main())
