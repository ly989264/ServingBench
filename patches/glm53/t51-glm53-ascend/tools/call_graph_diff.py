#!/usr/bin/env python3
"""Call-graph comparison of the package's changed files: for every function or method, the calls it makes in the
public d4ad49796 file and in the patched file, by AST (no imports).

usage: call_graph_diff.py BASE_TREE PATCHED_TREE [--json OUT] FILE...

A call is written as the source text of the called expression (e.g. self.m_store.put, pool_diag.timed('lookup'),
time.monotonic_ns); calls inside nested functions are attributed to the nested function. Per function it reports the
calls only in the base version ("removed") and only in the patched version ("added"), with counts when the number of
call sites changed. RENAMED maps a base function to the patched function that now holds its body.
"""

import ast
import json
import sys
from collections import Counter
from pathlib import Path

RENAMED = {"RecomputeScheduler.schedule": "RecomputeScheduler._schedule"}


def calls_by_function(src):
    out = {}

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = prefix + child.name
                counter = Counter()
                stack = list(ast.iter_child_nodes(child))
                while stack:
                    n = stack.pop()
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                        continue  # their calls belong to them
                    if isinstance(n, ast.Call):
                        counter[ast.unparse(n.func)] += 1
                    stack.extend(ast.iter_child_nodes(n))
                for deco in child.decorator_list:  # a decorator added is a call made at definition time
                    counter["@" + ast.unparse(deco)] += 1
                out[q] = counter
                visit(child, q + ".<locals>.")
            elif isinstance(child, ast.ClassDef):
                visit(child, prefix + child.name + ".")

    visit(ast.parse(src), "")
    return out


def main():
    args = sys.argv[1:]
    json_out = None
    if "--json" in args:
        i = args.index("--json")
        json_out = args[i + 1]
        del args[i:i + 2]
    base, new, files = Path(args[0]), Path(args[1]), args[2:]
    report = {}
    for f in files:
        new_calls = calls_by_function((new / f).read_text())
        if not (base / f).exists():
            report[f] = {"new_file": sorted(new_calls)}
            print(f"== {f}: new file, {len(new_calls)} functions")
            continue
        base_calls = calls_by_function((base / f).read_text())
        entries = {}
        for q in sorted(set(base_calls) | set(new_calls)):
            target = RENAMED.get(q, q)
            old = base_calls.get(q, Counter())
            cur = new_calls.get(target, Counter()) if q in base_calls else new_calls.get(q, Counter())
            if q in RENAMED.values() and q not in base_calls:
                continue  # reported under the base name
            removed = {c: n for c, n in old.items() if c not in cur}
            added = {c: n for c, n in cur.items() if c not in old}
            recount = {c: (old[c], cur[c]) for c in old if c in cur and old[c] != cur[c]}
            if removed or added or recount:
                entries[q if q not in RENAMED else f"{q} -> {target}"] = {
                    "only_base": removed, "only_patched": added, "count_changed": recount,
                    "new_function": q not in base_calls}
        report[f] = entries
        print(f"== {f}: {len(entries)} functions with a different call set")
        for q, e in entries.items():
            tag = " (new function)" if e["new_function"] else ""
            print(f"  {q}{tag}")
            for c, n in sorted(e["only_base"].items()):
                print(f"    - {c}" + (f" x{n}" if n > 1 else ""))
            for c, n in sorted(e["only_patched"].items()):
                print(f"    + {c}" + (f" x{n}" if n > 1 else ""))
            for c, (a, b) in sorted(e["count_changed"].items()):
                print(f"    ~ {c}: {a} -> {b} call sites")
    if json_out:
        Path(json_out).write_text(json.dumps(report, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
