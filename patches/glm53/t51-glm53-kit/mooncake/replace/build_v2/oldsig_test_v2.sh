#!/bin/bash
# Negative test of patch 04b, in the build container after the v2 build (/out/repl58/build):
#   the patch-04 test calls (t58_oldsig_test.cpp = t51_oldsig_test.cpp + a wait for the writer thread before _Exit),
#   compiled with the flags of client_service.cpp and linked against the v2 store library of that build.
# The records are read from the files in MC_PD_TIMING_DIR; stderr must have no timing lines.
set -uo pipefail
B=/out/repl58/build; O=/out/oldsig58; T=/build/v2/t58_oldsig_test.cpp; D=/tmp/mct-oldsig
rm -rf $O $D; mkdir -p $O
set +u; source /usr/local/Ascend/cann/set_env.sh >/dev/null 2>&1; set -u
FLAGS=$(python3 - <<'PY'
import json, shlex
d = json.load(open("/out/repl58/build/compile_commands.json"))
e = [x for x in d if x["file"].endswith("mooncake-store/src/client_service.cpp")][0]
a = shlex.split(e["command"])[1:]
out = []; skip = False
for x in a:
    if skip: skip = False; continue
    if x in ("-o", "-c"): skip = True; continue
    out.append(x)
print(" ".join(shlex.quote(x) for x in out))
PY
)
cd $B
eval c++ $FLAGS -Wno-unused-variable -c $T -o $O/t58_oldsig_test.o > $O/compile.log 2>&1 || { echo "compile failed"; tail -20 $O/compile.log; exit 1; }
LIB=$B/mooncake-store/src/libmooncake_store.so
DIRS=$(dirname $LIB):$B/mooncake-transfer-engine/src:$B/mooncake-common/src:$B/mooncake-common
c++ $O/t58_oldsig_test.o -o $O/t58_oldsig_test $LIB \
  -L$B/mooncake-transfer-engine/src -L$B/mooncake-common/src -L$B/mooncake-common \
  -ltransfer_engine -lmooncake_common -lasio -lglog -lgflags -lpthread \
  -Wl,-rpath,$DIRS -Wl,--allow-shlib-undefined > $O/link.log 2>&1 || { echo "link failed"; tail -20 $O/link.log; exit 1; }
echo "built: $(stat -c %s $O/t58_oldsig_test) bytes; store library $(sha256sum $LIB | cut -c1-16)"
MASTER=$(ls $B/mooncake-store/src/mooncake_master 2>/dev/null || echo /lay/site/mooncake/mooncake_master)
$MASTER --port 50051 --enable_http_metadata_server=true --http_metadata_server_port 8080 > $O/master.log 2>&1 &
MP=$!
for i in $(seq 1 30); do (exec 3<>/dev/tcp/127.0.0.1/50051) 2>/dev/null && break; sleep 1; done
unset MC_LOG_DIR
(cd /tmp && MC_FORCE_TCP=1 MC_PD_TIMING_DIR=$D timeout 120 $O/t58_oldsig_test > $O/stdout.txt 2> $O/stderr.txt; echo "test exit: $?")
kill $MP 2>/dev/null; wait $MP 2>/dev/null
cat $O/stdout.txt
echo "stderr timing lines: $(grep -c 'BatchPutTiming\|BatchUpsertTiming\|mc_batch_put' $O/stderr.txt)"
echo "files: $(ls $D | paste -sd' ' -)"
cat $D/mc.*.log | grep -v '"pd_file_open"' | sed 's/^.*\[PD_DIAG\] /record: /'
python3 - $D <<'PY'
import glob, json, sys
recs = []
for f in sorted(glob.glob(sys.argv[1] + "/mc.*.log")):
    for line in open(f):
        j = json.loads(line.split("[PD_DIAG] ", 1)[1])
        if j["event"] in ("mc_batch_put", "mc_batch_upsert"): recs.append(j)
def has(j, k): return k in j
ok = []
inc = [j for j in recs if j["complete"] is False and j.get("timing_scope") == "prefer_same_node_full"]
full = [j for j in recs if j["complete"] is True and j.get("timing_scope") == "normal_full"]
ok.append(("incomplete records (wrapper + direct call)", len(inc) >= 2))
ok.append(("incomplete: no phase1/total", all(not has(j, "phase1_start_batch_put_us") and not has(j, "total_us") for j in inc)))
ok.append(("incomplete: phases 2-4 present", all(all(has(j, k) for k in ("phase2_submit_transfers_us", "phase3_wait_transfers_us", "phase4_finalize_batch_put_us")) for j in inc)))
ok.append(("complete record with phase1/total", len(full) == 1 and has(full[0], "phase1_start_batch_put_us") and has(full[0], "total_us")))
for name, v in ok: print("check %-45s %s" % (name, "ok" if v else "FAIL"))
print("RESULT: %s" % ("OK" if all(v for _, v in ok) else "FAIL"))
PY
# patch 04b, fork with records queued: the child (no store calls, only timing records) opens a file of its own; the
# parent's writer goes on after the fork. Must not hang (timeout 60 s).
$MASTER --port 50051 --enable_http_metadata_server=true --http_metadata_server_port 8080 > $O/master_f.log 2>&1 &
MP=$!
for i in $(seq 1 30); do (exec 3<>/dev/tcp/127.0.0.1/50051) 2>/dev/null && break; sleep 1; done
DF=/tmp/mct-fork; rm -rf $DF
(cd /tmp && MC_FORCE_TCP=1 MC_PD_TIMING_DIR=$DF timeout 60 $O/t58_oldsig_test fork > $O/fork_stdout.txt 2> $O/fork_stderr.txt; echo "fork exit: $?")
kill $MP 2>/dev/null; wait $MP 2>/dev/null
cat $O/fork_stdout.txt
python3 - $DF $O/fork_stdout.txt <<'PY'
import glob, json, re, sys
out = open(sys.argv[2]).read()
m = re.search(r"T58FORK parent=(\d+) child=(\d+) child_exit=(-?\d+)", out)
if not m:
    print("check fork: FAIL (no parent line)"); sys.exit()
parent, child, cexit = m.group(1), m.group(2), int(m.group(3))
def recs(pid):
    fs = sorted(glob.glob("%s/mc.*.%s.*.log" % (sys.argv[1], pid))); n = 0; first = []
    for f in fs:
        lines = open(f).read().splitlines()
        first.append(json.loads(lines[0].split("[PD_DIAG] ", 1)[1])["event"] if lines else "")
        n += sum(1 for l in lines if '"mc_batch_' in l)
    return fs, n, first
pf, pn, pfirst = recs(parent); cf, cn, cfirst = recs(child)
print("fork: parent %s files %d records %d; child %s files %d records %d (exit %d)" % (parent, len(pf), pn, child, len(cf), cn, cexit))
ok = cexit == 0 and len(cf) == 1 and cn == 3 and cfirst == ["pd_file_open"] and pn == 55 and all(x == "pd_file_open" for x in pfirst)
print("check fork (child: own file, 3 records; parent: 55 records, no hang): %s" % ("ok" if ok else "FAIL"))
PY
# patch 04b, full ring: the same test program linked against the small test library (post_build.sh: 4 ring slots,
# stats every 2 s), 20000 records written at once from one thread
SM=/out/post/libmooncake_store_small.so
if [ -f $SM ]; then
  mkdir -p $O/small && cp $SM $O/small/libmooncake_store.so
  c++ $O/t58_oldsig_test.o -o $O/t58_burst_small $O/small/libmooncake_store.so \
    -L$B/mooncake-transfer-engine/src -L$B/mooncake-common/src -L$B/mooncake-common \
    -ltransfer_engine -lmooncake_common -lasio -lglog -lgflags -lpthread \
    -Wl,-rpath,$O/small:$B/mooncake-transfer-engine/src:$B/mooncake-common/src:$B/mooncake-common -Wl,--allow-shlib-undefined > $O/link_small.log 2>&1 || { echo "small link failed"; tail -5 $O/link_small.log; }
  echo "burst binary loads: $(ldd $O/t58_burst_small | grep libmooncake_store | awk '{print $3}')"
  $MASTER --port 50051 --enable_http_metadata_server=true --http_metadata_server_port 8080 > $O/master2.log 2>&1 &
  MP=$!
  for i in $(seq 1 30); do (exec 3<>/dev/tcp/127.0.0.1/50051) 2>/dev/null && break; sleep 1; done
  DB=/tmp/mct-burst; rm -rf $DB
  (cd /tmp && MC_FORCE_TCP=1 MC_PD_TIMING_DIR=$DB timeout 120 $O/t58_burst_small burst 20000 > $O/burst_stdout.txt 2> $O/burst_stderr.txt; echo "burst exit: $?")
  kill $MP 2>/dev/null; wait $MP 2>/dev/null
  cat $O/burst_stdout.txt
  python3 - $DB <<'PY'
import glob, json, sys
recs, stats = 0, []
for f in sorted(glob.glob(sys.argv[1] + "/mc.*.log")):
    for line in open(f):
        j = json.loads(line.split("[PD_DIAG] ", 1)[1])
        if j["event"] == "mc_batch_put": recs += 1
        if j["event"] == "mc_timing_stats": stats.append(j)
last = stats[-1] if stats else {}
print("burst: files %d, records in files %d, stats records %d, last stats written=%s dropped=%s queue_max=%s"
      % (len(glob.glob(sys.argv[1] + "/mc.*.log")), recs, len(stats), last.get("written"), last.get("dropped"), last.get("queue_max")))
ok = bool(last) and last["dropped"] > 0 and last["written"] == recs and last["written"] + last["dropped"] == 20000 and last["queue_max"] <= 4
print("check full ring drops and counts (written = records in files, written + dropped = 20000, queue_max <= 4): %s" % ("ok" if ok else "FAIL"))
PY
fi
