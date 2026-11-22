#!/bin/bash
# The 3 objects recompiled under the wrong configuration (the killed integration-test build) are rebuilt with the
# delivered configuration; the library is relinked and compared with the delivered v2 (the build is reproducible).
set -uo pipefail
B=/out/repl58/build
set +u; source /usr/local/Ascend/cann/set_env.sh >/dev/null 2>&1; set -u
cmake -S /build/src -B $B -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -DMOONCAKE_LIBURING_LIBRARY=MOONCAKE_LIBURING_LIBRARY-NOTFOUND -DMOONCAKE_LIBURING_INCLUDE_DIR=MOONCAKE_LIBURING_INCLUDE_DIR-NOTFOUND > /out/rebuild3_cmake.log 2>&1
(cd $B && ninja -j1 mooncake_store > /out/rebuild3.log 2>&1); echo "ninja rc=$?"; grep -o "^\[[0-9]*/[0-9]*\] [A-Za-z]* [^ ]* [^ ]* [^ ]*" /out/rebuild3.log
(cd $B && ninja -n mooncake_store | tail -1)
L=$B/mooncake-store/src/libmooncake_store.so
N=$(patchelf --print-needed /v1/libmooncake_store.so | paste -sd, -); R=$(patchelf --print-rpath /v1/libmooncake_store.so); S=$(patchelf --print-soname /v1/libmooncake_store.so)
python3 /build/scripts/relink_like_site.py $L /out/v2_rebuilt.so --needed "$N" --runpath "$R" --soname "$S" > /out/rebuild3_relink.log 2>&1
sha256sum /out/libmooncake_store.so /out/v2_rebuilt.so
cmp -s /out/libmooncake_store.so /out/v2_rebuilt.so && echo "rebuilt library: byte-identical to the delivered v2" || echo "rebuilt library DIFFERS from the delivered v2"
