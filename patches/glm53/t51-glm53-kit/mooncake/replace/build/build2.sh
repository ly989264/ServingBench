#!/bin/bash
# Build libmooncake_store (Mooncake 78566d45 + patch 04) inside quay.io/ascend/cann:9.2.0-a3-openeuler24.03-py3.11.
# BUILD_TYPE default Release, UNIT_TESTS default OFF; the glm5.3 replacement was built with BUILD_TYPE=RelWithDebInfo,
# UNIT_TESTS=ON and liburing forced not found (see ../README.zh-CN.md, section 3). ETCD off (the site library links no etcd).
set -euo pipefail
set +u
for f in /usr/local/Ascend/cann/set_env.sh /usr/local/Ascend/ascend-toolkit/set_env.sh; do [ -f "$f" ] && source "$f" && echo "sourced $f" && break; done
set -u
git config --global --add safe.directory '*'
echo "source commit: $(git -C ${SRC:-/build/src} rev-parse --short HEAD)"
gcc --version | head -1; cmake --version | head -1
OUT=/out/${TAG:-rel}
mkdir -p $OUT
cmake -S ${SRC:-/build/src} -B $OUT/build -G Ninja \
  -DUSE_ASCEND_DIRECT=ON -DUSE_ETCD=OFF -DBUILD_UNIT_TESTS="${UNIT_TESTS:-OFF}" -DBUILD_EXAMPLES=OFF -DBUILD_BENCHMARK=OFF \
  -DWITH_STORE_RUST=OFF -DWITH_RUST_EXAMPLE=OFF -DCMAKE_BUILD_TYPE="${BUILD_TYPE:-Release}" \
  -DPython3_EXECUTABLE="$(command -v python3)" \
  -DFETCHCONTENT_SOURCE_DIR_YALANTINGLIBS=/build/deps/yalantinglibs \
  -DFETCHCONTENT_SOURCE_DIR_GOOGLETEST=/build/deps/googletest ${CMAKE_EXTRA:-} > $OUT/cmake.log 2>&1 || { tail -40 $OUT/cmake.log; exit 1; }
cmake --build $OUT/build --target ${TARGETS:-mooncake_store} -j"${JOBS:-2}" > $OUT/build.log 2>&1 || { grep -m20 -E 'error|Error' $OUT/build.log; tail -20 $OUT/build.log; exit 1; }
find $OUT/build -name 'libmooncake_store*.so*' -exec ls -l {} \;
