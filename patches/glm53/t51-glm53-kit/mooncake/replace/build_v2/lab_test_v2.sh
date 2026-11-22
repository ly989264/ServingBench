#!/bin/bash
# Lab test of replacement v2 (patch 04b) in quay.io/ascend/cann:9.2.0-a3-openeuler24.03-py3.11 (docker run --rm, no NPU):
# the site-like layout (/lay/site/mooncake, unpatched check build as the stand-in for the site's original) with only
# libmooncake_store.so replaced by /r/libmooncake_store.so. put_batch/get through mooncake_master with MC_FORCE_TCP=1
# (put_batch goes through Client::BatchPut). Checks: no timing lines on stderr; the records in files; MC_PD_TIMING and
# PD_DIAG_LEVEL; exit() while the writer is busy, 100 times; fork with records queued (parent side); a directory that cannot be
# created. With /r/small_relinked.so (post_build.sh: 20000-byte files, keep 20 s, clean every 3 s, stats every 2 s,
# 4 ring slots): rotation, cleaning of old files (also other processes'). The full ring is tested in oldsig_test_v2.sh.
set -uo pipefail
mkdir -p /etc/yum.repos.d/orig && mv /etc/yum.repos.d/*.repo /etc/yum.repos.d/orig/ 2>/dev/null
B=https://mirrors.tuna.tsinghua.edu.cn/openeuler/openEuler-24.03-LTS-SP4
printf '[OS]\nname=OS\nbaseurl=%s/OS/aarch64/\nenabled=1\ngpgcheck=0\n[everything]\nname=everything\nbaseurl=%s/everything/aarch64/\nenabled=1\ngpgcheck=0\n' $B $B > /etc/yum.repos.d/tuna.repo
dnf -y -q --setopt=install_weak_deps=False --setopt=retries=20 install libibverbs numactl-libs libcurl procps-ng >/tmp/dnf.log 2>&1; echo "runtime packages: rc=$?"
set +u; source /usr/local/Ascend/cann/set_env.sh >/dev/null 2>&1; set -u
unset MC_LOG_DIR
SITE=/tmp/site; rm -rf $SITE; cp -a /lay/site $SITE; cp /r/libmooncake_store.so $SITE/mooncake/libmooncake_store.so
echo "libmooncake_store.so sha256 $(sha256sum $SITE/mooncake/libmooncake_store.so | cut -c1-16) size $(stat -c %s $SITE/mooncake/libmooncake_store.so)"
echo "ldd not found: [$(ldd $SITE/mooncake/libmooncake_store.so 2>&1 | grep 'not found' | awk '{print $1}' | sort -u | paste -sd' ' -)]"
cat > /tmp/client.py <<'PY'
import os, sys, time
sys.path.insert(0, "/tmp/site")
from mooncake.store import MooncakeDistributedStore
mode = sys.argv[1] if len(sys.argv) > 1 else "once"
s = MooncakeDistributedStore()
rc = s.setup("127.0.0.1", "http://127.0.0.1:8080/metadata", 64 << 20, 64 << 20, "tcp", "", "127.0.0.1:50051")
tag = "%d-%d" % (os.getpid(), time.time_ns())
def put(n):
    keys = ["k-%s-%d" % (tag, i) for i in range(n)]
    return s.put_batch(keys, [bytes([65 + i % 26]) * 4096 for i in range(n)])
if mode == "once":
    print("setup", rc, "put_batch", put(4), "native_tid", __import__("threading").get_native_id())
elif mode == "busy_exit":
    for _ in range(20): put(2)
    sys.exit(0)          # normal exit while the writer may still be writing
elif mode == "many":
    for _ in range(int(sys.argv[2])):
        put(1); time.sleep(float(sys.argv[3]))
    time.sleep(0.6)
elif mode == "fork_parent":  # fork with records queued; the child only exits (the store client is not fork-safe); the
    put(2); put(2)           # parent goes on writing. The child's own file is tested in oldsig_test_v2.sh (C++).
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0); print("child_pid", pid, "put after fork", put(2)); time.sleep(0.6)
PY
master() { "$SITE/mooncake/mooncake_master" --port 50051 --enable_http_metadata_server=true --http_metadata_server_port 8080 > /tmp/master.log 2>&1 & MP=$!; sleep 3; }
scenario() {  # name, env assignments..., then client mode
  local name=$1; shift; local mode=${!#}; local envs=("${@:1:$#-1}")
  local D=/tmp/mct-$name; rm -rf $D
  (cd /tmp && env MC_FORCE_TCP=1 MC_PD_TIMING_DIR=$D "${envs[@]}" timeout 120 python3 /tmp/client.py $mode > /tmp/out-$name.txt 2> /tmp/err-$name.txt; echo $? > /tmp/rc-$name)
  sleep 0.6
  local files=$(ls $D/mc.*.log 2>/dev/null | wc -l)
  local recs=$(cat $D/mc.*.log 2>/dev/null | grep -c '"event":"mc_batch_put"')
  local opens=$(cat $D/mc.*.log 2>/dev/null | grep -c '"event":"pd_file_open"')
  echo "== $name [${envs[*]:-no env}] $mode: exit=$(cat /tmp/rc-$name) files=$files records=$recs file_open=$opens stderr_timing=$(grep -c 'BatchPutTiming\|BatchUpsertTiming' /tmp/err-$name.txt)"
}
master
scenario default once
D=/tmp/mct-default; f=$(ls $D/mc.*.log | head -1); echo "   file: $(basename $f)"
sed -n 1,2p $f | cut -c1-420 | sed 's/^/   /'
echo "   native_tid printed by the client: $(grep -o 'native_tid [0-9]*' /tmp/out-default.txt)"
for v in "" 3 2 1 0 4 abc " 2 " 2x; do scenario "level_$(echo "$v" | tr -c 'a-z0-9' '_')" "PD_DIAG_LEVEL=$v" once; done
scenario timing_off MC_PD_TIMING=0 once
scenario timing_1 MC_PD_TIMING=1 once
bad=0; for i in $(seq 1 100); do
  (cd /tmp && MC_FORCE_TCP=1 MC_PD_TIMING_DIR=/tmp/mct-exit timeout 60 python3 /tmp/client.py busy_exit >/dev/null 2>/tmp/err-exit.txt); rc=$?
  [ $rc = 0 ] || { bad=$((bad+1)); echo "   exit run $i: rc=$rc $(tail -1 /tmp/err-exit.txt | cut -c1-120)"; }
done
echo "== busy_exit x100: non-zero exits=$bad files=$(ls /tmp/mct-exit/mc.*.log | wc -l) records=$(cat /tmp/mct-exit/mc.*.log | grep -c mc_batch_put)"
scenario fork_parent fork_parent
echo "   $(grep -o 'child_pid [0-9]* put after fork [-0-9]*' /tmp/out-fork_parent.txt); records of the parent $(cat /tmp/mct-fork_parent/mc.*.log | grep -c mc_batch_put) (expected 3)"
# a directory that cannot be created (a path under a regular file): records dropped, one warning, the put succeeds
(cd /tmp && MC_FORCE_TCP=1 MC_PD_TIMING_DIR=/etc/hostname/mc timeout 60 python3 /tmp/client.py once > /tmp/out-nodir.txt 2> /tmp/err-nodir.txt; echo $? > /tmp/rc-nodir)
echo "== no_dir [MC_PD_TIMING_DIR=/etc/hostname/mc] once: exit=$(cat /tmp/rc-nodir) $(grep -o 'put_batch [-0-9]*' /tmp/out-nodir.txt) warnings=$(grep -c 'mooncake timing records dropped' /tmp/err-nodir.txt) other_stderr_timing=$(grep -c 'BatchPutTiming\|mc_batch_put' /tmp/err-nodir.txt)"
grep -m1 'mooncake timing records dropped' /tmp/err-nodir.txt | sed 's/^.*\[PD_DIAG\]/   warning: [PD_DIAG]/'
if [ -f /r/small_relinked.so ]; then
  kill $MP; wait $MP 2>/dev/null
  cp /r/small_relinked.so $SITE/mooncake/libmooncake_store.so
  echo "-- small test library: sha256 $(sha256sum $SITE/mooncake/libmooncake_store.so | cut -c1-16)"
  master
  D=/tmp/mct-many; rm -rf $D; mkdir -p $D
  touch -d '-1 hour' $D/mc.20260101T000000.1.0000.log $D/notes.txt $D/mc.20260101T000000.2.0000.log.gz
  touch $D/mc.20260101T000000.3.0000.log
  (cd /tmp && MC_FORCE_TCP=1 MC_PD_TIMING_DIR=$D timeout 120 python3 /tmp/client.py many 120 0.1 > /tmp/out-many.txt 2> /tmp/err-many.txt; echo "== many 120 x 0.1 s: exit=$?")
  own=$(ls $D | grep -v '^mc\.20260101' | grep -v notes | paste -sd' ' -)
  echo "   files now: $(ls $D | paste -sd' ' -)"
  echo "   check rotation: $(ls $D/mc.*.log | grep -vc 20260101) own files; each starts with pd_file_open: $(for f in $(ls $D/mc.*.log | grep -v 20260101); do head -1 $f | grep -c pd_file_open; done | sort -u | paste -sd, -); seq 0000..$(ls $D | grep -v 20260101 | grep -o '\.[0-9]\{4\}\.log' | sort | tail -1)"
  echo "   check clean at open: old mc.*.log gone=$([ -e $D/mc.20260101T000000.1.0000.log ] && echo no || echo yes) fresh mc.*.log kept=$([ -e $D/mc.20260101T000000.3.0000.log ] && echo yes || echo no) other names kept=$([ -e $D/notes.txt ] && [ -e $D/mc.20260101T000000.2.0000.log.gz ] && echo yes || echo no)"
  n_before=$(ls $D/mc.*.log | wc -l)
  (cd /tmp && MC_FORCE_TCP=1 MC_PD_TIMING_DIR=$D timeout 120 python3 /tmp/client.py many 30 1.0 > /dev/null 2>&1; echo "== second process, 30 x 1 s (other process's files older than 20 s are cleaned every 3 s): exit=$?")
  first_pid_files=$(ls $D | grep -v 20260101 | awk -F. '{print $3}' | sort | uniq -c | paste -sd' ' -)
  echo "   files per pid now: $first_pid_files; mc.*.log before $n_before, now $(ls $D/mc.*.log | wc -l); fresh fake (now > 20 s old) gone=$([ -e $D/mc.20260101T000000.3.0000.log ] && echo no || echo yes)"
fi
kill $MP; wait $MP 2>/dev/null
