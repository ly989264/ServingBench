"""Every field V1 writes is a field hybrid writes; every enumerated value V1 writes is one hybrid writes.

Usage: python3 check_field_subset.py hybrid.jsonl v1.jsonl [more v1 .jsonl ...] > report.md
Records are grouped by (event, scope, transfer_role). Field paths are flattened with list positions
collapsed to [], so native_calls[0].status and native_calls[3].status are the same path. For the
enumerated fields below, the set of values V1 wrote must be contained in the set hybrid wrote.
Exits 1 when V1 has a path or a value that hybrid does not.
"""

import json
import re
import sys
from collections import defaultdict

ENUMERATED = re.compile(
    r"^(event|scope|transfer_role|status|transfer_status|direction|timing_scope|transfer_timing_scope|kv_role"
    r"|stages\.[^.]+\.(status|completion)|associated_stages\.[^.]+\.status"
    r"|native_calls\[\]\.(status|operation|transfer_scope|result_scope|per_segment_status|byte_semantics)"
    r"|links\[\]\.kind|complete|incomplete|final_task|tracker_completed|final_task_queued|delayed_free)$"
)


def flatten(value, prefix=""):
    if isinstance(value, dict):
        out = []
        for key, item in value.items():
            out.extend(flatten(item, f"{prefix}.{key}" if prefix else key))
        return out or ([(prefix, value)] if prefix else [])
    if isinstance(value, list) and value and all(isinstance(i, dict) for i in value):
        out = []
        for item in value:
            out.extend(flatten(item, f"{prefix}[]"))
        return out
    return [(prefix, value)]


def collect(paths):
    fields = defaultdict(set)
    values = defaultdict(lambda: defaultdict(set))
    count = defaultdict(int)
    for path in paths:
        for line in open(path):
            record = json.loads(line)["record"]
            key = (record.get("event"), record.get("scope"), record.get("transfer_role"))
            count[key] += 1
            for name, value in flatten(record):
                fields[key].add(name)
                if ENUMERATED.match(name):
                    values[key][name].add(json.dumps(value))
    return fields, values, count


def main(argv):
    hybrid_fields, hybrid_values, hybrid_count = collect([argv[0]])
    v1_fields, v1_values, v1_count = collect(argv[1:])
    problems = 0
    print("| 记录 | hybrid 条数 | V1 条数 | V1 字段路径 | V1 多出的路径 | V1 多出的枚举取值 | 只在 hybrid 出现的路径 |")
    print("|---|---|---|---|---|---|---|")
    for key in sorted(set(hybrid_fields) | set(v1_fields), key=str):
        extra_paths = sorted(v1_fields[key] - hybrid_fields[key])
        extra_values = [
            f"`{name}`={value}"
            for name in sorted(v1_values[key])
            for value in sorted(v1_values[key][name] - hybrid_values[key][name])
        ]
        hybrid_only = sorted(hybrid_fields[key] - v1_fields[key])
        problems += len(extra_paths) + len(extra_values)
        label = "/".join(str(k) for k in key if k)
        print(
            f"| {label} | {hybrid_count[key]} | {v1_count[key]} | {len(v1_fields[key])} | "
            f"{', '.join(f'`{p}`' for p in extra_paths) or '无'} | {', '.join(extra_values) or '无'} | "
            f"{', '.join(f'`{p}`' for p in hybrid_only) or '无'} |"
        )
    print()
    print("V1 的枚举取值（每种记录）：")
    for key in sorted(v1_values, key=str):
        label = "/".join(str(k) for k in key if k)
        for name in sorted(v1_values[key]):
            print(f"- {label} `{name}`: {', '.join(sorted(v1_values[key][name]))}")
    print()
    print(f"V1 多出的路径和取值共 {problems} 处。")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
