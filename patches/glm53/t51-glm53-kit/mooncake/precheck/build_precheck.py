#!/usr/bin/env python3
"""Embed the lab data into t51_mc_precheck.py (glm53mc-5).
usage: build_precheck.py --label LABEL --lab-store UNPATCHED.so --replacement REPLACEMENT.so --siblings DIR
                         --site-size N --site-sha16 HEX --src t51_mc_precheck.py --out OUT.py
--lab-wheel-size LABEL=N (repeatable): sizes of the unpatched check build after build_wheel.sh's steps.
UNPATCHED.so: the lab's check build (78566d4, site options, no patch 04); REPLACEMENT.so: the library delivered for the
site; DIR: the lab build's libtransfer_engine.so, libmooncake_common.so, libasio.so, ascend_transport.so.
--site-size/--site-sha16: the site's libmooncake_store.so as the field check saw it."""
import argparse, base64, gzip, hashlib, importlib.util, json, os

ap = argparse.ArgumentParser()
for a in ("--label", "--lab-store", "--replacement", "--siblings", "--src", "--out", "--site-sha16"):
    ap.add_argument(a, required=True)
ap.add_argument("--site-size", type=int, required=True)
ap.add_argument("--lab-root", required=True, help="the lab build's source directory as the compiler saw it, with a trailing / (e.g. /build/src/)")
ap.add_argument("--path-build", action="append", required=True,
                help="FILE=DIFF: the unpatched check build compiled at a longer source path and its .rodata difference "
                     "from --lab-store in bytes; the prediction must equal it exactly, else nothing is written")
ap.add_argument("--lab-wheel-size", action="append", required=True, help="LABEL=bytes: the unpatched check build after build_wheel.sh's strip and patchelf steps")
a = ap.parse_args()
spec = importlib.util.spec_from_file_location("pc", a.src)
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)
lab, rep = pc.elf_info(a.lab_store), pc.elf_info(a.replacement)
# the lab's path strings: every .rodata string that starts with the lab root; the rest of it is the relative path
with open(a.lab_store, "rb") as f:
    raw = f.read()
root = a.lab_root.encode()
rels = sorted({c[len(root):].decode() for c in raw.split(b"\0") if c.startswith(root) and len(c) > len(root)})
probe = pc.rodata_paths(a.lab_store, rels)
assert probe and all(pre == root for pre, _ in probe), "lab path strings not all under --lab-root"
assert sorted(rel for _, rel in probe) == rels, "a lab path occurs more than once in .rodata"
lab_paths = {"rels": rels, "N": len(probe), "L": len(root)}
# E (where the lab's path strings end, mod 16) is not visible in the linked file: take every E for which the prediction
# equals each path-length build exactly. The strings only ever grow by multiples of 8, so all such E predict alike;
# that is checked too.
lab_ro = dict((r[3], r[2]) for r in lab["progbits"])[".rodata"]
lab_other = sorted(r for r in lab["progbits"] if r[3] != ".rodata")
builds = []
for pb in a.path_build:
    f, want = pb.rsplit("=", 1)
    info = pc.elf_info(f)
    assert sorted(r for r in info["progbits"] if r[3] != ".rodata") == lab_other, "%s: sections other than .rodata differ" % f
    actual = dict((r[3], r[2]) for r in info["progbits"])[".rodata"] - lab_ro
    assert actual == int(want), "%s: .rodata differs by %+d, expected %+d" % (f, actual, int(want))
    builds.append((f, pc.rodata_paths(f, rels), actual))
ok_e = [e for e in range(16) if all(sum(pc.rodata_predict(pairs, rels, len(root), e)) == act for _, pairs, act in builds)]
assert ok_e, "no E predicts every path-length build exactly"
for g8 in range(-64, 1025, 8):
    vals = {(-(e + g8)) % 16 - (-e) % 16 for e in ok_e}
    assert len(vals) == 1, "the consistent E values %s predict differently" % ok_e
end16 = ok_e[0]
for f, pairs, act in builds:
    g, al = pc.rodata_predict(pairs, rels, len(root), end16)
    print("path build %s: L'=%d actual %+d = pad8 %+d + align16 %+d" % (os.path.basename(f), len(pairs[0][0]), act, g, al))
sib = {}
for name in ("libtransfer_engine.so", "libmooncake_common.so", "libasio.so", "ascend_transport.so"):
    info = pc.elf_info(os.path.join(a.siblings, name))
    sib[name] = sorted(info["exp"])
data = {"build": a.label, "site_f1": {"size": a.site_size, "sha16": a.site_sha16}, "lab_tls": lab["tls"],
        "lab_wheel_sizes": {kv.split("=", 1)[0]: int(kv.split("=", 1)[1]) for kv in a.lab_wheel_size},
        "lab_progbits": [list(r) for r in lab["progbits"]], "rodata_end16": end16,
        "expected_added_count": 3,
        "lab_paths": lab_paths,
        "lab_exports": sorted(lab["exp"]), "patch_removed": sorted(lab["exp"] - rep["exp"]),
        "patch_added": sorted(rep["exp"] - lab["exp"]), "repl_needed": rep["needed"],
        "repl_imports_strong": sorted(rep["imp_strong"]), "repl_verneed": rep["verneed"], "lab_siblings": sib}
# patch 04 option (i): the replacement only adds names; exactly the two new overloads and the timing writer
assert not data["patch_removed"], "the replacement removes exported names: %s" % data["patch_removed"]
assert len(data["patch_added"]) == 3 and all("CollectResults" in n or "BatchWriteWhenPreferSameNode" in n or "LogBatchWriteTiming" in n
                                             for n in data["patch_added"]), data["patch_added"]
blob = base64.b64encode(gzip.compress(json.dumps(data, sort_keys=True, separators=(",", ":")).encode(), 9)).decode()
src = open(a.src, encoding="utf-8").read()
assert src.count("DATA_B64 = None") == 1
out = src.replace("DATA_B64 = None", "DATA_B64 = (\n" + "\n".join('    "%s"' % blob[i:i + 100] for i in range(0, len(blob), 100)) + "\n)", 1)
os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
open(a.out, "w", encoding="utf-8").write(out)
print(a.out, hashlib.sha256(out.encode()).hexdigest(), "lab_progbits", pc.progbits_hash(lab["progbits"]), "lab_exports", len(lab["exp"]), "removed", data["patch_removed"],
      "added", len(data["patch_added"]), "repl_imports_strong", len(data["repl_imports_strong"]), "verneed", {k: len(v) for k, v in rep["verneed"].items()})
