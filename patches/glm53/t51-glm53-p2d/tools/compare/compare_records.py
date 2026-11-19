"""Field-by-field comparison of PD_DIAG records: hybrid (RC1 + patch 02) vs MooncakeConnectorV1 (d4ad49796 + p2d).

Usage: python3 compare_records.py hybrid.jsonl v1.jsonl > report.md
Records are paired by scenario and by (scope, transfer_role) in emission order. For each pair the script
reports every field path whose presence, JSON type or (for semantic fields) value differs. Identity and
clock values (ids, *_ns, host, pid, tid, run_id, session ids) are instance-specific and compared by type
only; byte and segment counts depend on the test's block layout and are compared by type only.
"""

import json
import sys
from collections import defaultdict

INSTANCE = {
    "record_id", "task_id", "connector_id", "request_generation", "call_id", "host", "pid", "native_tid",
    "run_id", "timestamp_ns", "session_id", "wall_utc_offset_seconds",
}
LAYOUT = {"submitted_bytes", "succeeded_bytes", "failed_bytes", "submitted_segments"}


def jtype(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    return "dict"


def flatten(value, prefix=""):
    out = {}
    if isinstance(value, dict):
        for key, item in value.items():
            out.update(flatten(item, f"{prefix}.{key}" if prefix else key))
        if not value and prefix:
            out[prefix] = value
    elif isinstance(value, list) and value and all(isinstance(i, dict) for i in value):
        for index, item in enumerate(value):
            out.update(flatten(item, f"{prefix}[{index}]"))
    else:
        out[prefix] = value
    return out


def semantic(path):
    leaf = path.rsplit(".", 1)[-1].split("[", 1)[0]
    if leaf in INSTANCE or leaf in LAYOUT:
        return False
    return not leaf.endswith("_ns")


def load(path):
    groups = defaultdict(list)
    for line in open(path):
        row = json.loads(line)
        record = row["record"]
        groups[(row["scenario"], record.get("scope"), record.get("transfer_role"))].append(record)
    return groups


def main(hybrid_path, v1_path):
    hybrid, v1 = load(hybrid_path), load(v1_path)
    print("| 场景 | 记录 | 字段 | hybrid | V1 |")
    print("|---|---|---|---|---|")
    total = 0
    compared = 0
    for key in sorted(set(hybrid) | set(v1), key=str):
        left, right = hybrid.get(key, []), v1.get(key, [])
        name = f"{key[1]}" + (f"/{key[2]}" if key[2] else "")
        if len(left) != len(right):
            print(f"| {key[0]} | {name} | （记录条数） | {len(left)} | {len(right)} |")
            total += 1
        for a, b in zip(left, right):
            compared += 1
            fa, fb = flatten(a), flatten(b)
            for path in sorted(set(fa) | set(fb)):
                if path not in fa or path not in fb:
                    shown_a = json.dumps(fa[path]) if path in fa else "（无）"
                    shown_b = json.dumps(fb[path]) if path in fb else "（无）"
                elif jtype(fa[path]) != jtype(fb[path]):
                    shown_a, shown_b = jtype(fa[path]), jtype(fb[path])
                elif semantic(path) and fa[path] != fb[path]:
                    shown_a, shown_b = json.dumps(fa[path]), json.dumps(fb[path])
                else:
                    continue
                total += 1
                print(f"| {key[0]} | {name} | `{path}` | {shown_a} | {shown_b} |")
    print()
    print(f"比较了 {compared} 对记录，差异 {total} 处。")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
