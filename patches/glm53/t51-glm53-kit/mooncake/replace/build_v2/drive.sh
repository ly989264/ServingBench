#!/bin/bash
# T58 replacement build (Mooncake 78566d45 + patch 04b) in quay.io/ascend/cann:9.2.0-a3-openeuler24.03-py3.11
set -uo pipefail
step() { echo "=== $(date +%H:%M:%S) $*"; }
step setup; bash /build/scripts/setup.sh > /out/setup.log 2>&1; grep -c MISSING /out/setup.log
step msgpack; cd /tmp && tar -xzf /build/deps_dl/msgpack-c-cpp-7.0.0.tar.gz && cd msgpack-c-cpp-7.0.0 && cmake -S . -B build -DMSGPACK_BUILD_TESTS=OFF -DMSGPACK_BUILD_EXAMPLES=OFF >/out/msgpack_cmake.log 2>&1 && cmake --install build >/out/msgpack_install.log 2>&1 || { echo msgpack7 failed; exit 1; }
curl -fsSL --retry 5 -o /tmp/cpp-9.0.0.tar.gz https://github.com/msgpack/msgpack-c/archive/refs/tags/cpp-9.0.0.tar.gz || { echo download failed; exit 1; }
echo "cpp-9.0.0 tarball sha256 $(sha256sum /tmp/cpp-9.0.0.tar.gz | cut -c1-64)"
rm -rf /tmp/mp && mkdir -p /tmp/mp && tar xzf /tmp/cpp-9.0.0.tar.gz -C /tmp/mp && rm -rf /usr/local/include/msgpack /usr/local/include/msgpack.hpp && cp -a /tmp/mp/*/include/msgpack /tmp/mp/*/include/msgpack.hpp /usr/local/include/ && find /usr/local/include/msgpack /usr/local/include/msgpack.hpp -exec touch {} +
echo "msgpack $(grep -E 'define MSGPACK_VERSION_(MAJOR|MINOR|REVISION)' /usr/local/include/msgpack/version_master.hpp | awk '{print $3}' | paste -sd. -)"
export SRC=/build/src TAG=repl58 BUILD_TYPE=RelWithDebInfo UNIT_TESTS=ON JOBS=1
export CMAKE_EXTRA="-DMOONCAKE_LIBURING_LIBRARY=MOONCAKE_LIBURING_LIBRARY-NOTFOUND -DMOONCAKE_LIBURING_INCLUDE_DIR=MOONCAKE_LIBURING_INCLUDE_DIR-NOTFOUND"
step "client_service.cpp only"; export TARGETS="mooncake-store/src/CMakeFiles/mooncake_store_client_objects.dir/client_service.cpp.o"
bash /build/scripts/build2.sh > /out/build_cs.log 2>&1; rc=$?; tail -3 /out/build_cs.log; [ $rc = 0 ] || { grep -m30 -B2 -A6 "error" /out/repl58/build.log | head -80; exit 1; }
step "library"; export TARGETS=mooncake_store; bash /build/scripts/build2.sh > /out/build_lib.log 2>&1; rc=$?; tail -2 /out/build_lib.log; [ $rc = 0 ] || exit 1
step relink; L=/out/repl58/build/mooncake-store/src/libmooncake_store.so
N=$(patchelf --print-needed /v1/libmooncake_store.so | paste -sd, -); R=$(patchelf --print-rpath /v1/libmooncake_store.so); S=$(patchelf --print-soname /v1/libmooncake_store.so)
echo "v1: needed=$N rpath=$R soname=$S"
python3 /build/scripts/relink_like_site.py $L /out/libmooncake_store.so --needed "$N" --runpath "$R" --soname "$S"
ls -l /out/libmooncake_store.so; sha256sum /out/libmooncake_store.so
step done
