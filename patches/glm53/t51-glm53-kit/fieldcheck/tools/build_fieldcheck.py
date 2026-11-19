#!/usr/bin/env python3
"""Build the single-file field check: embed d4ad49796 hashes of the in-scope files, the history index, the
d4ad49796 function statistics of the P/D connector, and (when given) the monkey-patch targets and p2d anchors.

Usage: build_fieldcheck.py --ascend-base TREE --history fc_history.json [--targets targets.txt]
                           [--anchors anchors.json] --src t51_glm53_fieldcheck.py --out dist/t51_glm53_fieldcheck.py
"""
import argparse
import base64
import gzip
import hashlib
import importlib.util
import json
import os
import sys

ap = argparse.ArgumentParser()
ap.add_argument("--ascend-base", required=True)
ap.add_argument("--history", required=True)
ap.add_argument("--targets")
ap.add_argument("--anchors")
ap.add_argument("--p2d-script", help="replay_p2d.py, embedded byte for byte and run in memory on site (its main() is never called)")
ap.add_argument("--p2d-extra", help="extra_checks.py (p2d), embedded byte for byte; extra_line(source) is called on site")
ap.add_argument("--vllm-base", help="vLLM v0.30.0 tree (the originals of the functions patch 01 changes)")
ap.add_argument("--vllm-port", help="the same tree with the vLLM port applied (patch b22adf23)")
ap.add_argument("--src", required=True)
ap.add_argument("--out", required=True)
a = ap.parse_args()

spec = importlib.util.spec_from_file_location("fc", a.src)
fc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fc)

pkg = os.path.join(a.ascend_base, "vllm_ascend")
scope = {rel: hashlib.sha256(open(full, "rb").read()).hexdigest()[:16] for rel, full in fc.scope_files(pkg).items()}
conn = open(os.path.join(a.ascend_base, fc.SCOPE_CONN), encoding="utf-8").read()
funcs = fc.functions(conn)
data = {"d4a_scope": scope, "history": json.load(open(a.history)), "conn_funcs": funcs}
if a.targets:
    names, modules = [], []
    for line in open(a.targets, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("module:"):
            modules.append(line.split(":", 1)[1].strip())
        else:
            names.append(line)
    data["targets"] = {"names": names, "modules": modules}
if a.anchors:
    data["anchors"] = json.load(open(a.anchors, encoding="utf-8"))
if a.p2d_script:
    raw = open(a.p2d_script, "rb").read()  # embedded byte for byte
    data["p2d"] = {"file_b64": base64.b64encode(raw).decode("ascii"), "sha256": hashlib.sha256(raw).hexdigest()}
if a.p2d_extra:
    raw = open(a.p2d_extra, "rb").read()
    data["p2d_extra"] = {"file_b64": base64.b64encode(raw).decode("ascii"), "sha256": hashlib.sha256(raw).hexdigest()}
if a.vllm_base and a.vllm_port:
    v01 = {"files": {}, "changed": []}
    for dirpath, _d, names in os.walk(os.path.join(a.vllm_port, "vllm")):
        for fn in names:
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, a.vllm_port).replace(os.sep, "/")
            other = os.path.join(a.vllm_base, rel)
            if not os.path.exists(other):
                continue
            new, old = open(full, encoding="utf-8").read(), open(other, encoding="utf-8").read()
            if new == old:
                continue
            fo, fp = fc.functions(old) or {}, fc.functions(new) or {}
            ch = sorted(q for q in fp if q not in fo or fp[q][0] != fo[q][0])
            if ch:
                v01["files"][rel] = {"orig": old, "port": new}
                v01["changed"] += ["%s::%s" % (rel, q) for q in ch]
    data["v01"] = v01
blob = base64.b64encode(gzip.compress(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8"), 9)).decode("ascii")
src = open(a.src, encoding="utf-8").read()
assert src.count("DATA_B64 = None") == 1
out = src.replace("DATA_B64 = None", "DATA_B64 = (\n" + "\n".join('    "%s"' % blob[i : i + 100] for i in range(0, len(blob), 100)) + "\n)", 1)
os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
open(a.out, "w", encoding="utf-8").write(out)
print(a.out, hashlib.sha256(out.encode("utf-8")).hexdigest(), "scope", len(scope), "history", len(data["history"]), "funcs", len(funcs),
      "targets", len((data.get("targets") or {}).get("names", [])), "anchors", len(data.get("anchors") or []), "p2d", (data.get("p2d") or {}).get("sha256", "-")[:16], "v01_files", len((data.get("v01") or {}).get("files", {})), "v01_funcs", len((data.get("v01") or {}).get("changed", [])))
