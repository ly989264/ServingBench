#!/bin/bash
# Build the unpatched check library from a source copy at another path (same options as the check build),
# keep only its stripped store library, then delete the build directory.
set -u
D=$1
export SRC=/build/$D TAG=pathx_$D TARGETS="${TARGETS:-mooncake_store}" BUILD_TYPE=RelWithDebInfo UNIT_TESTS=ON JOBS=${JOBS:-1}
export CMAKE_EXTRA="-DMOONCAKE_LIBURING_LIBRARY=MOONCAKE_LIBURING_LIBRARY-NOTFOUND -DMOONCAKE_LIBURING_INCLUDE_DIR=MOONCAKE_LIBURING_INCLUDE_DIR-NOTFOUND"
start=$(date +%s)
bash /build/build2.sh > /out/exp/pathx_$D.log 2>&1; rc=$?
lib=/out/pathx_$D/build/mooncake-store/src/libmooncake_store.so
if [ $rc = 0 ] && [ -f $lib ]; then cp $lib /out/exp/p_$D.so && strip --strip-unneeded /out/exp/p_$D.so; fi
mod=/out/pathx_$D/build/mooncake-integration/store.cpython-311-aarch64-linux-gnu.so
[ -f $mod ] && cp $mod /out/exp/store_module_$D.so && echo "kept store module (unpatched, msgpack 9.0.0)"
ninja -C /out/pathx_$D/build -t inputs mooncake_store 2>/dev/null | grep -c "\.o$" | sed "s/^/objects linked into the store library: /"
echo "$D rc=$rc $(( $(date +%s) - start ))s size=$(stat -c %s /out/exp/p_$D.so 2>/dev/null)"
if [ $rc = 0 ]; then rm -rf /out/pathx_$D; else echo "build directory kept for a resumed build: /out/pathx_$D"; fi
