#!/usr/bin/env python3
"""glm53mc-6: take the lab data embedded in the delivered glm53mc-5 file, replace the replacement-side fields by those of
replacement v2 (patch 04b), add the v1 entry for the upgrade v1 -> v2, and embed the result into the glm53mc-6 source.
The lab-side fields (check build sections, path strings, exports, siblings, site f-1) are kept as they are: the
check build and the site's original did not change.
usage: build_precheck_v6.py --from DIST_glm53mc-5.py --v1 V1.so --v2 V2.so --src t51_mc_precheck.py --out OUT.py
         --patch04b 04b.patch [--site-size N --site-sha16 HEX]   (test builds: other site values, e.g. the lab file's)"""
import argparse, base64, gzip, hashlib, importlib.util, json, os, re

ap = argparse.ArgumentParser()
for a in ("--from", "--v1", "--v2", "--src", "--out"):
    ap.add_argument(a, required=True)
ap.add_argument("--site-size", type=int)
ap.add_argument("--site-sha16")
ap.add_argument("--patch04b", required=True, help="patch 04b file: its sha256 prefix goes into the build label")
a = ap.parse_args()

old = open(getattr(a, "from"), encoding="utf-8").read()
m = re.search(r'^DATA_B64 = \(\n((?:    "[A-Za-z0-9+/=]+"\n)+)\)', old, re.M)
assert m, "no embedded data in the glm53mc-5 file"
blob = "".join(re.findall(r'"([A-Za-z0-9+/=]+)"', m.group(1)))
data = json.loads(gzip.decompress(base64.b64decode(blob)).decode())

spec = importlib.util.spec_from_file_location("pc", a.src)
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)
v1, v2 = pc.elf_info(a.v1), pc.elf_info(a.v2)
lab = set(data["lab_exports"])

# v1 must be the replacement the glm53mc-5 data describes
assert sorted(lab - v1["exp"]) == data["patch_removed"] and sorted(v1["exp"] - lab) == data["patch_added"], "v1 is not the glm53mc-5 replacement"
# v2: no name removed, the same 3 names added (patch 04b keeps the exports of patch 04)
removed, added = sorted(lab - v2["exp"]), sorted(v2["exp"] - lab)
assert not removed, "v2 removes exported names: %s" % removed
assert added == data["patch_added"], "v2 adds other names than v1: %s" % sorted(set(added) ^ set(data["patch_added"]))
assert v2["exp"] == v1["exp"], "v1 and v2 export different names"

def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()

before = {k: data[k] for k in ("repl_needed", "repl_imports_strong", "repl_verneed")}
data["build"] = data["build"] + "+p04b-" + sha(a.patch04b)[:8]   # e.g. 78566d4-rwdi-ut-nouring-mp9.0.0/repl-p04i-0625b51e+p04b-<8>
data["patch_removed"], data["patch_added"] = removed, added
data["repl_needed"] = v2["needed"]
data["repl_imports_strong"] = sorted(v2["imp_strong"])
data["repl_verneed"] = v2["verneed"]
data["upgrade_from"] = {"label": "v1", "size": os.path.getsize(a.v1), "sha16": sha(a.v1)[:16]}
data["replacement"] = {"label": "v2", "size": os.path.getsize(a.v2), "sha16": sha(a.v2)[:16]}
if a.site_size is not None:
    data["site_f1"] = {"size": a.site_size, "sha16": a.site_sha16}

new_imp = set(data["repl_imports_strong"]) - set(before["repl_imports_strong"])
gone_imp = set(before["repl_imports_strong"]) - set(data["repl_imports_strong"])
blob = base64.b64encode(gzip.compress(json.dumps(data, sort_keys=True, separators=(",", ":")).encode(), 9)).decode()
src = open(a.src, encoding="utf-8").read()
assert src.count("DATA_B64 = None") == 1
out = src.replace("DATA_B64 = None", "DATA_B64 = (\n" + "\n".join('    "%s"' % blob[i:i + 100] for i in range(0, len(blob), 100)) + "\n)", 1)
os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
open(a.out, "w", encoding="utf-8").write(out)
print(a.out, hashlib.sha256(out.encode()).hexdigest())
print("upgrade_from", data["upgrade_from"], "replacement", data["replacement"], "site_f1", data["site_f1"])
print("needed v1->v2 same:", before["repl_needed"] == data["repl_needed"],
      "imports_strong v1 %d v2 %d new %s gone %s" % (len(before["repl_imports_strong"]), len(data["repl_imports_strong"]),
                                                    sorted(new_imp)[:12], sorted(gone_imp)[:12]),
      "verneed", {k: len(v) for k, v in data["repl_verneed"].items()})
