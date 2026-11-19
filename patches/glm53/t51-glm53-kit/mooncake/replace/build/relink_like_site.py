#!/usr/bin/env python3
"""Strip (default --strip-all: the site file has no debug info and, by its size, no .symtab), then rename the NEEDED entries and set RUNPATH/SONAME so the library links exactly like the
site's libmooncake_store.so (names from the field check's mc_store line). Runs patchelf/strip inside the build
container. Usage: relink_like_site.py IN OUT --needed a,b,c --runpath R --soname S
Each NEEDED of IN is mapped to the site name with the same stem (the part before the first '-' or '.so');
the result must have exactly the site's NEEDED set, otherwise it stops."""
import argparse, re, subprocess, shutil, sys

ap = argparse.ArgumentParser()
ap.add_argument("inp"); ap.add_argument("out")
ap.add_argument("--strip", default="--strip-all", choices=["--strip-all", "--strip-debug"]); ap.add_argument("--needed", required=True); ap.add_argument("--runpath", required=True); ap.add_argument("--soname", required=True)
a = ap.parse_args()
site = [x for x in a.needed.split(",") if x]
stem = lambda n: re.split(r"-[0-9a-f]{8}\.so|\.so", n, 1)[0]
shutil.copyfile(a.inp, a.out)
subprocess.run(["strip", a.strip, a.out], check=True)
ours = subprocess.run(["patchelf", "--print-needed", a.out], capture_output=True, text=True, check=True).stdout.split()
by_stem = {}
for n in site:
    by_stem.setdefault(stem(n), n)
args = []
for n in ours:
    target = by_stem.get(stem(n))
    if target and target != n:
        args += ["--replace-needed", n, target]
# one patchelf call: every call that grows .dynstr adds a LOAD segment (64 KiB aligned on aarch64)
subprocess.run(["patchelf"] + args + ["--set-rpath", a.runpath, "--set-soname", a.soname, a.out], check=True)
after = subprocess.run(["patchelf", "--print-needed", a.out], capture_output=True, text=True, check=True).stdout.split()
missing, extra = sorted(set(site) - set(after)), sorted(set(after) - set(site))
print("needed:", ",".join(after)); print("rpath:", subprocess.run(["patchelf", "--print-rpath", a.out], capture_output=True, text=True).stdout.strip())
print("soname:", subprocess.run(["patchelf", "--print-soname", a.out], capture_output=True, text=True).stdout.strip())
if missing or extra:
    print("NEEDED differs from the site: missing=%s extra=%s" % (missing, extra)); sys.exit(2)
print("NEEDED set equals the site's")
