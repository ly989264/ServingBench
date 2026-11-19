#!/usr/bin/env python3
"""Static linkage check of a replacement libmooncake_store.so against a layout (lab prototype of the precheck's
option B). Usage: sym_check.py LAYOUT_MOONCAKE_DIR REPLACEMENT.so
- exports removed / added by the replacement (vs the layout's current libmooncake_store.so)
- for every other ELF file of the layout: names it imports that the current library exports must all be exported
  by the replacement
- imports the replacement has and the current library does not: listed, with which layout library exports them
- NEEDED (in order), SONAME, RUNPATH equal"""
import os, struct, sys

def elf(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"\x7fELF":
        return None
    e_shoff = struct.unpack_from("<Q", data, 0x28)[0]
    e_shentsize, e_shnum = struct.unpack_from("<HH", data, 0x3A)
    secs = [struct.unpack_from("<IIQQQQII", data, e_shoff + i * e_shentsize) for i in range(e_shnum)]
    dyn = [x for x in secs if x[1] == 11]
    out = {"exp": set(), "imp": set(), "imp_strong": set(), "needed": [], "soname": None, "runpath": None}
    if dyn:
        _n, _t, _f, _a, off, size, link, _i = dyn[0]
        so, ss = secs[link][4], secs[link][5]
        strs = data[so:so + ss]
        for i in range(size // 24):
            st_name, st_info, _o, st_shndx = struct.unpack_from("<IBBH", data, off + i * 24)
            name = strs[st_name:strs.find(b"\0", st_name)].decode()
            if not name or (st_info >> 4) not in (1, 2):
                continue
            if st_shndx == 0:
                out["imp"].add(name)
                if (st_info >> 4) == 1:
                    out["imp_strong"].add(name)
            else:
                out["exp"].add(name)
    ds = [x for x in secs if x[1] == 6]
    if ds:
        dstr = data[secs[ds[0][6]][4]:secs[ds[0][6]][4] + secs[ds[0][6]][5]]
        s = lambda v: dstr[v:dstr.find(b"\0", v)].decode()
        for j in range(ds[0][5] // 16):
            tag, val = struct.unpack_from("<qQ", data, ds[0][4] + j * 16)
            if tag == 0:
                break
            if tag == 1:
                out["needed"].append(s(val))
            elif tag == 14:
                out["soname"] = s(val)
            elif tag in (15, 29):
                out["runpath"] = ("RPATH:" if tag == 15 else "RUNPATH:") + s(val)
    return out

lay, repl = sys.argv[1], sys.argv[2]
O, R = elf(os.path.join(lay, "libmooncake_store.so")), elf(repl)
print("exports current=%d replacement=%d removed=%d added=%d" % (len(O["exp"]), len(R["exp"]), len(O["exp"] - R["exp"]), len(R["exp"] - O["exp"])))
for n in sorted(O["exp"] - R["exp"]):
    print("  removed", n)
for n in sorted(R["exp"] - O["exp"]):
    print("  added  ", n)
others = {}
for fn in sorted(os.listdir(lay)):
    p = os.path.join(lay, fn)
    if fn == "libmooncake_store.so" or not (".so" in fn or fn == "mooncake_master") or not os.path.isfile(p):
        continue
    e = elf(p)
    if e:
        others[fn] = e
bad = 0
for fn, e in others.items():
    need = e["imp"] & O["exp"]
    miss = need - R["exp"]
    bad += len(miss)
    if need:
        print("importer %s: takes %d names the current library exports, missing in replacement: %d%s" % (fn, len(need), len(miss), (" " + ",".join(sorted(miss))) if miss else ""))
new_imp = R["imp"] - O["imp"]
print("imports current=%d replacement=%d new=%d gone=%d" % (len(O["imp"]), len(R["imp"]), len(new_imp), len(O["imp"] - R["imp"])))
for n in sorted(new_imp):
    by = [fn for fn, e in others.items() if n in e["exp"] and fn in R["needed"]]
    print("  new import %s%s weak=%d by_layout=%s" % (n, "", int(n not in R["imp_strong"]), ",".join(by) or "-(system lib)"))
for k in ("needed", "soname", "runpath"):
    print("%s %s" % (k, "same" if O[k] == R[k] else "DIFFERS current=%s replacement=%s" % (O[k], R[k])))
print("result", "ok" if not bad and all(O[k] == R[k] for k in ("needed", "soname", "runpath")) else "NOT_OK")
