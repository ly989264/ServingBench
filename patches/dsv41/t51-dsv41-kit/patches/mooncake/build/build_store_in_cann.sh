#!/bin/bash
# Build libmooncake_store (Mooncake v0.3.14-rc1 + patch 04) inside the CANN base image.
# JOBS (default 2): in a 16 GB Docker VM the compiler was killed for memory at -j10 and -j4 (master_client.cpp).
# BUILD_TYPE (default RelWithDebInfo = -O2 -g -DNDEBUG, Mooncake's own default). The OBS wheel was
#   built with NDEBUG; mooncake-common/common.cmake replaces Release flags with plain -O3 (no NDEBUG).
# /build/src should be a git checkout of 78566d45 (core.abbrev=8) so the library reports "git: 78566d45".
# UNIT_TESTS (default ON, Mooncake's default): the OBS build had it on, which compiles the NVMe KV test
#   stub into libmooncake_store and defines MOONCAKE_ENABLE_NVME_KV_TEST_STUB; only the library target is built.
#   Needs /build/deps/googletest (v1.17.0, the pinned tarball) because CMake would otherwise download it.
# Mounts: /build/src  patched Mooncake source with extern/pybind11 filled in
#         /build/deps/yalantinglibs  the pinned yalantinglibs source (no network needed for it)
#         /build/out  writable output
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
PKGS="build-essential cmake ninja-build git pkg-config patchelf libibverbs-dev libgoogle-glog-dev libgflags-dev
 libjsoncpp-dev libunwind-dev libnuma-dev libpython3-dev libboost-dev libssl-dev libgrpc-dev libgrpc++-dev
 libprotobuf-dev protobuf-compiler-grpc libyaml-cpp-dev libcurl4-openssl-dev libhiredis-dev liburing-dev
 libjemalloc-dev libmsgpack-dev libzmq3-dev libzstd-dev libasio-dev libxxhash-dev"
if [ "${SKIP_APT:-0}" != 1 ]; then
  apt-get update -o Acquire::Retries=5 >/build/out/apt-update.log 2>&1
  # shellcheck disable=SC2086
  apt-get install -y -o Acquire::Retries=5 --no-install-recommends $PKGS >/build/out/apt-install.log 2>&1
fi
set +u
for f in /usr/local/Ascend/cann/set_env.sh /usr/local/Ascend/ascend-toolkit/set_env.sh; do [ -f "$f" ] && source "$f" && echo "sourced $f" && break; done
set -u
# the source is mounted from another user; let git read it so the version string gets the commit id
git config --global --add safe.directory '*'
echo "source commit: $(git -C /build/src rev-parse --short HEAD 2>/dev/null || echo none)"
gcc --version | head -1; cmake --version | head -1; python3 --version
cmake -S /build/src -B /build/out/build -G Ninja \
  -DUSE_ASCEND_DIRECT=ON -DBUILD_UNIT_TESTS="${UNIT_TESTS:-ON}" -DBUILD_EXAMPLES=OFF -DBUILD_BENCHMARK=OFF \
  -DWITH_STORE_RUST=OFF -DWITH_RUST_EXAMPLE=OFF -DCMAKE_BUILD_TYPE="${BUILD_TYPE:-RelWithDebInfo}" \
  -DPython3_EXECUTABLE="$(command -v python3)" \
  -DFETCHCONTENT_SOURCE_DIR_YALANTINGLIBS=/build/deps/yalantinglibs \
  -DFETCHCONTENT_SOURCE_DIR_GOOGLETEST=/build/deps/googletest > /build/out/cmake.log 2>&1 || { tail -40 /build/out/cmake.log; exit 1; }
cmake --build /build/out/build --target mooncake_store -j"${JOBS:-2}" > /build/out/build.log 2>&1 || { grep -m20 -E 'error|Error' /build/out/build.log; tail -20 /build/out/build.log; exit 1; }
find /build/out/build -name 'libmooncake_store*.so*' -exec ls -l {} \;
