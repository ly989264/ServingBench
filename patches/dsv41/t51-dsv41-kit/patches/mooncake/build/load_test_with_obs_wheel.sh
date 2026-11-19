#!/bin/bash
# Lab check (CANN base image, docker run --rm): install the OBS wheel, then compare the original and
# the replacement libmooncake_store: dynamic linking (ldd) and Python import of mooncake.store.
# Mounts: /w/<the OBS wheel>, /r/ (replacement files laid out like the wheel)
set -uo pipefail
python3 -m pip install -q --no-deps --no-index /w/*.whl 2>&1 | grep -v 'Running pip as' | tail -1
# runtime libraries the vllm-ascend image has but the bare CANN image lacks
apt-get update -qq -o Acquire::Retries=5 >/dev/null 2>&1 && apt-get install -y -qq -o Acquire::Retries=5 libibverbs1 libnuma1 >/dev/null 2>&1; echo "runtime libs installed: $?"
set +u; source /usr/local/Ascend/cann/set_env.sh >/dev/null 2>&1; set -u
SITE=$(python3 -c "import sysconfig; print(sysconfig.get_paths()['platlib'])")
check() {
  echo "-- ldd: unresolved libraries of store.so and both libmooncake_store copies"
  for f in "$SITE/mooncake/store.so" "$SITE/mooncake/libmooncake_store.so" "$SITE/mooncake_transfer_engine.libs/libmooncake_store-2a608938.so"; do
    echo "   $(basename "$f"): not found = $(ldd "$f" 2>&1 | grep 'not found' | awk '{print $1}' | sort -u | paste -sd' ' -)"
  done
  echo "   store.so resolves libmooncake_store to: $(ldd "$SITE/mooncake/store.so" | grep libmooncake_store | sed "s#$SITE/##" | awk '{print $3}')"
  echo "-- import mooncake.store"
  python3 -c "import mooncake.store as s; print('   import ok;', 'MooncakeDistributedStore' in dir(s))" 2>&1 | tail -2 | sed 's/^/   /'
}
echo "== original OBS files"
sha256sum "$SITE/mooncake/libmooncake_store.so" "$SITE/mooncake_transfer_engine.libs/libmooncake_store-2a608938.so" | sed "s#$SITE/##" | cut -c1-16,65-
check
echo "== libraries in the wheel that import the two functions whose signatures patch 04 changes"
for f in $(find "$SITE/mooncake" "$SITE/mooncake_transfer_engine.libs" -name '*.so*' -type f); do
  n=$(nm -D --undefined-only "$f" 2>/dev/null | c++filt | grep -cE 'Client::(CollectResults|BatchWriteWhenPreferSameNode)')
  [ "$n" != 0 ] && echo "   $(basename "$f"): $n"
done
echo "   (end of list)"
cp /r/mooncake/libmooncake_store.so "$SITE/mooncake/libmooncake_store.so"
cp /r/mooncake_transfer_engine.libs/libmooncake_store-2a608938.so "$SITE/mooncake_transfer_engine.libs/libmooncake_store-2a608938.so"
echo "== after replacement"
sha256sum "$SITE/mooncake/libmooncake_store.so" "$SITE/mooncake_transfer_engine.libs/libmooncake_store-2a608938.so" | sed "s#$SITE/##" | cut -c1-16,65-
check
echo "   [BatchPutTiming] in the loaded copy: $(strings -a "$SITE/mooncake/libmooncake_store.so" | grep -c '\[BatchPutTiming\]')"
