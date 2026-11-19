#!/usr/bin/env python3
"""Targets for the monkey-patch scan, derived from the patches themselves: every function or method whose body
a port changes (ported tree vs public tree, function by function), every function a p2d change sits in, and the
modules that define them. Output: targets.txt (one name per line, 'module: <dotted>' lines for modules).
Usage: derive_targets.py OUT --pair TOP:BASE_TREE:PORT_TREE [--pair ...] [--anchors anchors.json] [--extra targets.txt]
"""
import argparse, importlib.util, os, sys

ap = argparse.ArgumentParser()
ap.add_argument("out")
ap.add_argument("--pair", action="append", default=[])
ap.add_argument("--anchors")
ap.add_argument("--extra")
a = ap.parse_args()
spec = importlib.util.spec_from_file_location("fc", os.path.join(os.path.dirname(__file__), "..", "t51_glm53_fieldcheck.py"))
fc = importlib.util.module_from_spec(spec); spec.loader.exec_module(fc)
names, modules = set(), set()
for pair in a.pair:
    top, base, port = pair.split(":", 2)
    for dirpath, _d, files in os.walk(os.path.join(port, top)):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, port)
            other = os.path.join(base, rel)
            new = open(full, encoding="utf-8").read()
            old = open(other, encoding="utf-8").read() if os.path.exists(other) else None
            if old == new:
                continue
            fn_new, fn_old = fc.functions(new) or {}, (fc.functions(old) or {}) if old is not None else {}
            changed = [q for q in fn_new if q not in fn_old or fn_new[q][0] != fn_old[q][0]]
            if old is None:
                continue  # a new file of the port defines no site function
            for q in changed:
                names.add(q.rsplit(".", 1)[-1])
            if changed:
                modules.add(rel[:-3].replace(os.sep, ".").replace(".__init__", ""))
if a.anchors:
    import json
    for c in json.load(open(a.anchors))["changes"]:
        for f in (c.get("function"), c.get("function_in_unpatched_file")):
            if f and f != "<module>":
                names.add(f.rsplit(".", 1)[-1])
    modules.add("vllm_ascend.distributed.kv_transfer.kv_p2p.mooncake_connector")
if a.extra:
    for line in open(a.extra):
        line = line.split("#", 1)[0].strip()
        if line.startswith("module:"):
            modules.add(line.split(":", 1)[1].strip())
        elif line:
            names.add(line)
names.discard("__init__")
with open(a.out, "w") as f:
    f.write("# derived from the ports (changed functions) and the p2d changes; one name per line\n")
    for n in sorted(names):
        f.write(n + "\n")
    for m in sorted(modules):
        f.write("module: " + m + "\n")
print("names", len(names), "modules", len(modules))
