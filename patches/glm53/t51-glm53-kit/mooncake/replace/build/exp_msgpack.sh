#!/bin/bash
# Rebuild the unpatched check library (site options) against msgpack-c headers of tag $1 and print its fingerprint.
set -euo pipefail
V=$1; H=/out/msgpack_hdrs
mkdir -p $H
if [ ! -d $H/cpp-7.0.0 ]; then mkdir -p $H/cpp-7.0.0 && cp -a /usr/local/include/msgpack /usr/local/include/msgpack.hpp $H/cpp-7.0.0/; fi
if [ ! -d $H/$V ]; then
  case $V in cpp_master) U="https://github.com/msgpack/msgpack-c/archive/refs/heads/cpp_master.tar.gz";; *) U="https://github.com/msgpack/msgpack-c/archive/refs/tags/$V.tar.gz";; esac
  curl -fsSL --retry 5 -o /tmp/$V.tar.gz "$U"
  echo "tarball $V sha256 $(sha256sum /tmp/$V.tar.gz | cut -c1-64)"
  rm -rf /tmp/mp && mkdir -p /tmp/mp && tar xzf /tmp/$V.tar.gz -C /tmp/mp
  mkdir -p $H/$V && cp -a /tmp/mp/*/include/msgpack /tmp/mp/*/include/msgpack.hpp $H/$V/
fi
rm -rf /usr/local/include/msgpack /usr/local/include/msgpack.hpp
cp -a $H/$V/msgpack $H/$V/msgpack.hpp /usr/local/include/
# new mtimes, so that ninja sees the change (cp -a keeps the archive's older times)
find /usr/local/include/msgpack /usr/local/include/msgpack.hpp -exec touch {} +
echo "msgpack $V: $(grep -E 'define MSGPACK_VERSION_(MAJOR|MINOR|REVISION)' /usr/local/include/msgpack/version_master.hpp | awk '{print $3}' | paste -sd. -)"
set +u; source /usr/local/Ascend/cann/set_env.sh >/dev/null 2>&1; set -u
cd /out/rwdi_orig/build
start=$(date +%s); ninja -j2 mooncake_store > /out/exp/build_$V.log 2>&1 || { tail -30 /out/exp/build_$V.log; exit 1; }
echo "rebuilt $(grep -c 'Building CXX' /out/exp/build_$V.log) objects in $(( $(date +%s) - start )) s"
python3 -c "
import importlib.util,sys
s=importlib.util.spec_from_file_location('fp','/out/exp/fp.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
print('$V', m.fingerprint('/out/rwdi_orig/build/mooncake-store/src/libmooncake_store.so'))"
cp /out/rwdi_orig/build/mooncake-store/src/libmooncake_store.so /tmp/store_$V.so; strip --strip-unneeded /tmp/store_$V.so; echo "$V strip-unneeded size $(stat -c %s /tmp/store_$V.so)"; rm -f /tmp/store_$V.so /tmp/$V.tar.gz; rm -rf /tmp/mp
