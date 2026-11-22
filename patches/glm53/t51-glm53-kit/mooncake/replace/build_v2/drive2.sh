#!/bin/bash
# Resume of drive.sh in a container from t58-mcbuild-img:1 (setup and msgpack already done): the store library from the
# existing build directory (ninja continues where it stopped; client_service.cpp is recompiled because the source in
# /build/src is now the final 04b), then the relink to /out/libmooncake_store.so.
set -uo pipefail
step() { echo "=== $(date +%H:%M:%S) $*"; }
export SRC=/build/src TAG=repl58 BUILD_TYPE=RelWithDebInfo UNIT_TESTS=ON JOBS=1
export CMAKE_EXTRA="-DMOONCAKE_LIBURING_LIBRARY=MOONCAKE_LIBURING_LIBRARY-NOTFOUND -DMOONCAKE_LIBURING_INCLUDE_DIR=MOONCAKE_LIBURING_INCLUDE_DIR-NOTFOUND"
step "library (resume)"; export TARGETS=mooncake_store; bash /build/scripts/build2.sh > /out/build_lib2.log 2>&1; rc=$?; tail -2 /out/build_lib2.log; [ $rc = 0 ] || { grep -m5 -B2 "FAILED\|Killed" /out/repl58/build.log | tail -12; exit 1; }
step relink; L=/out/repl58/build/mooncake-store/src/libmooncake_store.so
N=$(patchelf --print-needed /v1/libmooncake_store.so | paste -sd, -); R=$(patchelf --print-rpath /v1/libmooncake_store.so); S=$(patchelf --print-soname /v1/libmooncake_store.so)
echo "v1: needed=$N rpath=$R soname=$S"
python3 /build/scripts/relink_like_site.py $L /out/libmooncake_store.so --needed "$N" --runpath "$R" --soname "$S"
ls -l /out/libmooncake_store.so; sha256sum /out/libmooncake_store.so
step done
