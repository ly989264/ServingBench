#!/bin/bash
# Lab check (CANN base image, docker run --rm, no NPU): the OBS wheel's own mooncake_master plus a Python
# client over TCP (MC_FORCE_TCP=1: the transfer engine skips the Ascend transport, which needs an NPU).
# put_batch goes through Client::BatchPut. Run once with the original store library and
# once after replacing it: the replacement must talk to the unchanged master and write [BatchPutTiming].
# Mounts: /w/<the OBS wheel>, /r/ (replacement files laid out like the wheel)
set -uo pipefail
python3 -m pip install -q --no-deps --no-index /w/*.whl 2>&1 | grep -v 'Running pip as' | tail -1
apt-get update -qq -o Acquire::Retries=5 >/dev/null 2>&1 && apt-get install -y -qq -o Acquire::Retries=5 libibverbs1 libnuma1 >/dev/null 2>&1
set +u; source /usr/local/Ascend/cann/set_env.sh >/dev/null 2>&1; set -u
SITE=$(python3 -c "import sysconfig; print(sysconfig.get_paths()['platlib'])")
cat > /tmp/client.py <<'PY'
from mooncake.store import MooncakeDistributedStore
s = MooncakeDistributedStore()
rc = s.setup("127.0.0.1", "http://127.0.0.1:8080/metadata", 64 << 20, 64 << 20, "tcp", "", "127.0.0.1:50051")
print("setup rc", rc)
keys = ["t51-key-%d" % i for i in range(4)]
vals = [bytes([65 + i]) * 4096 for i in range(4)]
import threading
print("python native_tid", threading.get_native_id())
print("put_batch rc", s.put_batch(keys, vals))
got = [s.get(k) for k in keys]
print("get ok", all(g == v for g, v in zip(got, vals)), [len(g) for g in got])
PY
run() {  # $1 label
  echo "== $1"
  "$SITE/mooncake/mooncake_master" --port 50051 --enable_http_metadata_server=true --http_metadata_server_port 8080 --enable_metric_reporting=false > /tmp/master.log 2>&1 &
  M=$!
  sleep 3
  MC_FORCE_TCP=1 GLOG_logtostderr=1 timeout 120 python3 /tmp/client.py 2> /tmp/client.err | sed 's/^/   /'
  echo "   client exit: ${PIPESTATUS[0]}"
  echo "   [BatchPutTiming] lines: $(grep -c '\[BatchPutTiming\]' /tmp/client.err)"
  grep -m2 '\[BatchPutTiming\]' /tmp/client.err | awk '{print "   glog level+date=" substr($1,1,1) "<mmdd> time=" $2 " tid=" $3 " source=" $4}'
  grep -m2 '\[BatchPutTiming\]' /tmp/client.err | sed 's/^.*\] \[BatchPutTiming\]/   [BatchPutTiming]/'
  grep -m3 -E '^[EF][0-9]{4}' /tmp/client.err | cut -c1-200 | sed 's/^/   err: /'
  kill $M; wait $M 2>/dev/null
}
run "original OBS library"
cp /r/mooncake/libmooncake_store.so "$SITE/mooncake/libmooncake_store.so"
cp /r/mooncake_transfer_engine.libs/libmooncake_store-2a608938.so "$SITE/mooncake_transfer_engine.libs/libmooncake_store-2a608938.so"
run "replacement library (patch 04)"
