#!/bin/bash
# Negative test of patch 04 (option i), in the build container (quay.io/ascend/cann:9.2.0-a3-openeuler24.03-py3.11)
# after "ninja mooncake_store" in /out/rwdi_orig/build:
#   compile t51_oldsig_test.cpp with the flags of client_service.cpp, link it against the patched (unstripped) store
#   library of that build, start mooncake_master with its HTTP metadata server, run the test with MC_FORCE_TCP=1.
# Output: /out/oldsig/stdout.txt (T51OLDSIG lines) and /out/oldsig/stderr.txt (glog lines, incl. the timing lines).
set -uo pipefail
B=/out/rwdi_orig/build; O=/out/oldsig; T=/build/t51_oldsig_test.cpp
rm -rf $O; mkdir -p $O
set +u; source /usr/local/Ascend/cann/set_env.sh >/dev/null 2>&1; set -u
FLAGS=$(python3 - <<'PY'
import json, shlex
d = json.load(open("/out/rwdi_orig/build/compile_commands.json"))
e = [x for x in d if x["file"].endswith("mooncake-store/src/client_service.cpp")][0]
a = shlex.split(e["command"])[1:]
out = []
skip = False
for x in a:
    if skip: skip = False; continue
    if x in ("-o", "-c"): skip = True; continue
    out.append(x)
print(" ".join(shlex.quote(x) for x in out))
PY
)
cd $B
eval c++ $FLAGS -Wno-unused-variable -c $T -o $O/t51_oldsig_test.o > $O/compile.log 2>&1 || { echo "compile failed"; tail -20 $O/compile.log; exit 1; }
LIB=$B/mooncake-store/src/libmooncake_store.so
DIRS=$(dirname $LIB):$B/mooncake-transfer-engine/src:$B/mooncake-common/src:$B/mooncake-common
c++ $O/t51_oldsig_test.o -o $O/t51_oldsig_test $LIB \
  -L$B/mooncake-transfer-engine/src -L$B/mooncake-common/src -L$B/mooncake-common \
  -ltransfer_engine -lmooncake_common -lasio -lglog -lgflags -lpthread \
  -Wl,-rpath,$DIRS -Wl,--allow-shlib-undefined > $O/link.log 2>&1 || { echo "link failed"; tail -20 $O/link.log; exit 1; }
echo "built: $(stat -c %s $O/t51_oldsig_test) bytes; store library $(sha256sum $LIB | cut -c1-16)"
MASTER=$(ls $B/mooncake-store/src/mooncake_master 2>/dev/null || echo /out/orig_art/mooncake_master)
$MASTER --port 50051 --enable_http_metadata_server=true --http_metadata_server_port 8080 > $O/master.log 2>&1 &
MP=$!
for i in $(seq 1 30); do (exec 3<>/dev/tcp/127.0.0.1/50051) 2>/dev/null && break; sleep 1; done
unset MC_LOG_DIR
(cd /tmp && MC_FORCE_TCP=1 timeout 120 $O/t51_oldsig_test > $O/stdout.txt 2> $O/stderr.txt; echo "test exit: $?")
kill $MP 2>/dev/null; wait $MP 2>/dev/null
cat $O/stdout.txt
grep -E '\[Batch(Put|Upsert)Timing\]' $O/stderr.txt | sed 's/^/timing: /'
