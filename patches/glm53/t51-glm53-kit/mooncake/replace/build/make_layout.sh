#!/bin/bash
# Lab layout like the site's Mooncake wheel, from the unpatched 78566d45 build (stands in for the site's original).
# Site facts (field check f1 / detector): mooncake/libmooncake_store.so, soname libmooncake_store.so, RUNPATH $ORIGIN,
# NEEDED with the bundled system libraries under hashed names. Because the RUNPATH is only $ORIGIN, the hashed
# libraries are placed in mooncake/ next to it (lab assumption; the original and the replacement see the same layout).
# Output: /out/lay/site/mooncake/
set -euo pipefail
A=/out/orig_art; L=/out/lay/site/mooncake
rm -rf /out/lay; mkdir -p $L
cp /build/src/mooncake-wheel/mooncake/*.py $L/
SITE_NEEDED=libzstd-2a4af8ca.so.1.5.5,libtransfer_engine.so,libxxhash-0bb4316f.so.0.8.2,libnuma.so.1,libascendcl.so,libmooncake_common.so,libyaml-cpp-f6e0e2ab.so.0.7.0,libasio.so,libibverbs.so.1,libgflags-0650a09e.so.2.2.2,libjsoncpp-598fe3ba.so.1.9.5,ascend_transport.so,libglog-afa88425.so.0.6.0,libstdc++.so.6,libm.so.6,libgcc_s.so.1,libc.so.6
declare -A MAP=([libzstd.so.1]=libzstd-2a4af8ca.so.1.5.5 [libxxhash.so.0]=libxxhash-0bb4316f.so.0.8.2
  [libyaml-cpp.so.0.7]=libyaml-cpp-f6e0e2ab.so.0.7.0 [libgflags.so.2.2]=libgflags-0650a09e.so.2.2.2
  [libjsoncpp.so.25]=libjsoncpp-598fe3ba.so.1.9.5 [libglog.so.1]=libglog-afa88425.so.0.6.0)
rename_args() {  # one patchelf call per file (each call that grows .dynstr adds a LOAD segment)
  local args=()
  for old in "${!MAP[@]}"; do
    if patchelf --print-needed "$1" | grep -qx "$old"; then args+=(--replace-needed "$old" "${MAP[$old]}"); fi
  done
  echo "${args[@]}"
}
# bundled system libraries under the site's names
for old in "${!MAP[@]}"; do
  real=$(readlink -f /usr/lib64/$old); new=${MAP[$old]}
  cp "$real" "$L/$new"; patchelf $(rename_args "$L/$new") --set-soname "$new" --set-rpath '$ORIGIN' "$L/$new"
done
# Mooncake libraries other than libmooncake_store.so. STORE_MODULE: another unpatched store module to use instead of
# $A's (e.g. the one built with msgpack 9.0.0 by pathx.sh)
for f in libtransfer_engine.so libmooncake_common.so libasio.so ascend_transport.so store.cpython-311-aarch64-linux-gnu.so mooncake_master; do
  src="$A/$f"; [ "$f" = store.cpython-311-aarch64-linux-gnu.so ] && src="${STORE_MODULE:-$src}"
  cp "$src" "$L/$f"; strip --strip-all "$L/$f"
  patchelf $(rename_args "$L/$f") --set-rpath '$ORIGIN:/usr/local/Ascend/cann-9.2.0/aarch64-linux/lib64' "$L/$f"
done
# the stand-in for the site's original libmooncake_store.so
python3 /build/relink_like_site.py "$A/libmooncake_store.so" "$L/libmooncake_store.so" --needed "$SITE_NEEDED" --runpath '$ORIGIN' --soname libmooncake_store.so
ls -l $L | awk '{print $5, $9}'
