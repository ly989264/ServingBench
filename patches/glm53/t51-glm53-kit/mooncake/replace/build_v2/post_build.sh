#!/bin/bash
# After the v2 build (in the build container, /out/repl58/build): compile commands, the parsing table test, the class
# layout of mooncake::Client against the original 78566d45 header, the original-signature test, the small-rotation test
# library, and sym_check against the layout. Inputs: /build/src (04b), /orig/client_service.h (78566d45), /lay, /v1.
set -uo pipefail
B=/out/repl58/build; O=/out/post; rm -rf $O; mkdir -p $O
set +u; source /usr/local/Ascend/cann/set_env.sh >/dev/null 2>&1; set -u
step() { echo "=== $(date +%H:%M:%S) $*"; }
# the same configuration as the delivered build (build2.sh with drive.sh's CMAKE_EXTRA): without the two liburing
# NOTFOUND values, find_library searches again, finds liburing and changes every compile line
step "compile commands"; cmake -S /build/src -B $B -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -DMOONCAKE_LIBURING_LIBRARY=MOONCAKE_LIBURING_LIBRARY-NOTFOUND -DMOONCAKE_LIBURING_INCLUDE_DIR=MOONCAKE_LIBURING_INCLUDE_DIR-NOTFOUND > $O/cmake_cc.log 2>&1; ls -l $B/compile_commands.json | awk '{print $5, $NF}'
(cd $B && ninja -n mooncake_store | tail -1)   # must say "no work to do": the delivered library's configuration
FLAGS=$(python3 - <<'PY'
import json, shlex
d = json.load(open("/out/repl58/build/compile_commands.json"))
e = [x for x in d if x["file"].endswith("mooncake-store/src/client_service.cpp")][0]
a = shlex.split(e["command"])[1:]
out, skip = [], False
for x in a:
    if skip: skip = False; continue
    if x in ("-o", "-c"): skip = True; continue
    if x.endswith("client_service.cpp"): continue
    out.append(x)
print(" ".join(shlex.quote(x) for x in out))
PY
)
echo "$FLAGS" > $O/flags.txt
step "parsing table"; c++ -std=c++17 -I/build/src/mooncake-store/include /build/v2/t58_level_test.cpp -o $O/level_test && $O/level_test | tee $O/level_test.txt
step "class layout of mooncake::Client: original 78566d45 header vs 04b header"
printf '#include "client_service.h"\n#include <cstdio>\nint main(){std::printf("sizeof %%zu alignof %%zu\\n", sizeof(mooncake::Client), alignof(mooncake::Client));}\n' > $O/probe.cpp
mkdir -p $O/orighdr && cp /orig/client_service.h $O/orighdr/client_service.h
cd $B/mooncake-store/src
for v in orig v2; do
  inc=""; [ $v = orig ] && inc="-I$O/orighdr"
  eval c++ $inc $FLAGS -fdump-lang-class=$O/$v.class -c $O/probe.cpp -o $O/probe_$v.o > $O/probe_$v.log 2>&1 || { echo "probe $v compile failed"; tail -5 $O/probe_$v.log; }
  python3 - "$O/$v.class" > $O/$v.client.txt <<'PY'
import sys, re
t = open(sys.argv[1], errors="replace").read()
m = re.search(r"^Class mooncake::Client\n.*?(?=^Class |\Z)", t, re.S | re.M)
print(m.group(0) if m else "NOT FOUND")
PY
  echo "$v: $(head -3 $O/$v.client.txt | tr '\n' ' ')"
done
# the dump prints the compiler's own pointer for the class node ("(0x0x…)"), different in every run: masked
for v in orig v2; do sed -E 's/0x0x[0-9a-f]+/ADDR/g' $O/$v.client.txt > $O/$v.client.masked.txt; done
cmp -s $O/orig.client.masked.txt $O/v2.client.masked.txt && echo "class layout of mooncake::Client: identical ($(wc -l < $O/v2.client.txt) lines, compiler addresses masked)" || { echo "class layout DIFFERS"; diff $O/orig.client.masked.txt $O/v2.client.masked.txt | head -20; }
step "small test library (ROTATE_BYTES=20000, KEEP 20 s, CLEAN 3 s, STATS 2 s, SLOTS 4)"
eval c++ $FLAGS -DMC_PD_TIMING_ROTATE_BYTES=20000 -DMC_PD_TIMING_KEEP_SECONDS=20 -DMC_PD_TIMING_CLEAN_SECONDS=3 -DMC_PD_TIMING_STATS_SECONDS=2 -DMC_PD_TIMING_SLOTS=4 -c /build/src/mooncake-store/src/client_service.cpp -o $O/cs_small.o > $O/cs_small.log 2>&1 || { echo "small compile failed"; tail -5 $O/cs_small.log; }
cd $B
LINK=$(ninja -t commands mooncake-store/src/libmooncake_store.so | tail -1)
OBJ=$(echo "$LINK" | tr ' ' '\n' | grep 'mooncake_store_client_objects.dir/client_service.cpp.o' | head -1)
SMALL_LINK=$(echo "$LINK" | sed "s#$OBJ#$O/cs_small.o#; s# -o [^ ]*libmooncake_store.so# -o $O/libmooncake_store_small.so#")
echo "$SMALL_LINK" | grep -q cs_small.o || echo "link line not rewritten"
(cd $B && eval "$SMALL_LINK") > $O/link_small.log 2>&1 || { echo "small link failed"; tail -5 $O/link_small.log; }   # ninja -t commands: paths relative to the build root
ls -l $O/libmooncake_store_small.so 2>/dev/null | awk '{print $5, $NF}'
N=$(patchelf --print-needed /v1/libmooncake_store.so | paste -sd, -); R=$(patchelf --print-rpath /v1/libmooncake_store.so); S=$(patchelf --print-soname /v1/libmooncake_store.so)
python3 /build/scripts/relink_like_site.py $O/libmooncake_store_small.so $O/small_relinked.so --needed "$N" --runpath "$R" --soname "$S" > $O/relink_small.log 2>&1 && echo "small relinked: $(stat -c %s $O/small_relinked.so)"
step "sym_check against the layout (current = unpatched check build)"
python3 /build/scripts/sym_check.py /lay/site/mooncake /out/libmooncake_store.so | tee $O/sym_check.txt | head -30
step "exports v1 vs v2"
python3 - /v1/libmooncake_store.so /out/libmooncake_store.so <<'PY'
import subprocess, sys
def exp(p):
    out = subprocess.run(["nm", "-D", "--defined-only", p], capture_output=True, text=True).stdout.split("\n")
    return {l.split()[-1] for l in out if len(l.split()) >= 3}
a, b = exp(sys.argv[1]), exp(sys.argv[2])
print("v1 exports", len(a), "v2 exports", len(b), "only v1", len(a - b), "only v2", len(b - a))
for n in sorted(a ^ b)[:20]: print("  ", "v1" if n in a else "v2", n)
PY
step "NEEDED / RUNPATH / SONAME of v2"; patchelf --print-needed /out/libmooncake_store.so | paste -sd, -; patchelf --print-rpath /out/libmooncake_store.so; patchelf --print-soname /out/libmooncake_store.so
step "original-signature test (records in files)"; bash /build/v2/oldsig_test_v2.sh 2>&1 | tee $O/oldsig.txt | tail -12
if [ "${SKIP_ITEST:-0}" = 1 ]; then echo "(client_integration_test skipped: SKIP_ITEST=1)"; else
step "client_integration_test compiles (04b capture by files)"; (cd $B && ninja -j1 client_integration_test > $O/itest_build.log 2>&1); echo "rc=$? $(tail -1 $O/itest_build.log)"
step "client_integration_test: the batch-write timing test (records read back from the files)"
IT=$(find $B -name client_integration_test -type f -perm -u+x | head -1)
if [ -n "$IT" ]; then (cd /tmp && MC_FORCE_TCP=1 timeout 600 "$IT" --gtest_filter='*BatchWriteTiming*' > $O/itest_run.txt 2>&1; echo "rc=$?"); grep -E '^\[ *(OK|FAILED|PASSED) *\]|tests? ran' $O/itest_run.txt | head -8; else echo "no test binary"; fi
fi
step done
