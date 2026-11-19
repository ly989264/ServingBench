#!/bin/bash
# Lab test in quay.io/ascend/cann:9.2.0-a3-openeuler24.03-py3.11 (docker run --rm, no NPU):
#   the site-like layout (/lay/site/mooncake, unpatched 78566d45 build = stand-in for the site's original), then only
#   libmooncake_store.so replaced by /r/libmooncake_store.so (patch 04, stripped, relinked).
#   For each: ldd (unresolved libraries), import mooncake.store, put_batch/get through mooncake_master with
#   MC_FORCE_TCP=1 (put_batch goes through Client::BatchPut), [BatchPutTiming] lines, glog before-init warnings.
#   MC_LOG_DIR is not set (as on site).
set -uo pipefail
mkdir -p /etc/yum.repos.d/orig && mv /etc/yum.repos.d/*.repo /etc/yum.repos.d/orig/ 2>/dev/null
B=https://mirrors.tuna.tsinghua.edu.cn/openeuler/openEuler-24.03-LTS-SP4
printf '[OS]\nname=OS\nbaseurl=%s/OS/aarch64/\nenabled=1\ngpgcheck=0\n[everything]\nname=everything\nbaseurl=%s/everything/aarch64/\nenabled=1\ngpgcheck=0\n' $B $B > /etc/yum.repos.d/tuna.repo
dnf -y -q --setopt=install_weak_deps=False --setopt=retries=20 install libibverbs numactl-libs libcurl procps-ng >/tmp/dnf.log 2>&1; echo "runtime packages: rc=$? (libibverbs numactl-libs libcurl procps-ng)"
set +u; source /usr/local/Ascend/cann/set_env.sh >/dev/null 2>&1; set -u
unset MC_LOG_DIR
SITE=/tmp/site; rm -rf $SITE; cp -a /lay/site $SITE
cat > /tmp/client.py <<'PY'
import sys, threading
sys.path.insert(0, "/tmp/site")
from mooncake.store import MooncakeDistributedStore
s = MooncakeDistributedStore()
rc = s.setup("127.0.0.1", "http://127.0.0.1:8080/metadata", 64 << 20, 64 << 20, "tcp", "", "127.0.0.1:50051")
print("setup rc", rc)
keys = ["t51-key-%d" % i for i in range(4)]
vals = [bytes([65 + i]) * 4096 for i in range(4)]
print("put_batch rc", s.put_batch(keys, vals))
got = [s.get(k) for k in keys]
print("get ok", all(g == v for g, v in zip(got, vals)), [len(g) for g in got])
print("maps libmooncake_store:", sorted({l.split()[-1].replace("/tmp/site/", "") for l in open("/proc/self/maps") if "libmooncake_store" in l}))
PY
run() {
  echo "== $1"
  echo "   libmooncake_store.so sha256: $(sha256sum $SITE/mooncake/libmooncake_store.so | cut -c1-16) size: $(stat -c %s $SITE/mooncake/libmooncake_store.so)"
  for f in store.cpython-311-aarch64-linux-gnu.so libmooncake_store.so; do
    echo "   ldd $f: not found = [$(ldd $SITE/mooncake/$f 2>&1 | grep 'not found' | awk '{print $1}' | sort -u | paste -sd' ' -)]"
  done
  echo "   store module resolves libmooncake_store to: $(ldd $SITE/mooncake/store.cpython-311-aarch64-linux-gnu.so | grep 'libmooncake_store' | awk '{print $3}' | sed "s#$SITE/##")"
  (cd /tmp && python3 -c "import sys; sys.path.insert(0,'/tmp/site'); import mooncake.store as s; print('   import ok', 'MooncakeDistributedStore' in dir(s))" 2>&1 | tail -1)
  "$SITE/mooncake/mooncake_master" --port 50051 --enable_http_metadata_server=true --http_metadata_server_port 8080 > /tmp/master.log 2>&1 &
  MP=$!; sleep 3
  (cd /tmp && MC_FORCE_TCP=1 timeout 120 python3 /tmp/client.py 2> /tmp/client.err | sed 's/^/   /'; echo "   client exit: ${PIPESTATUS[0]}")
  echo "   glog before-init warnings: $(grep -c 'Logging before InitGoogleLogging' /tmp/client.err)"
  echo "   [BatchPutTiming] lines: $(grep -c '\[BatchPutTiming\]' /tmp/client.err)"
  grep -m1 '\[BatchPutTiming\]' /tmp/client.err | cut -c1-400 | sed 's/^/   first: /'
  grep -m3 -E '^[EF][0-9]{4}' /tmp/client.err | cut -c1-200 | sed 's/^/   err: /'
  kill $MP; wait $MP 2>/dev/null
}
run "stand-in for the site's original (unpatched 78566d45, site options, site names)"
cp /r/libmooncake_store.so "$SITE/mooncake/libmooncake_store.so"
run "after replacing only libmooncake_store.so (patch 04)"
