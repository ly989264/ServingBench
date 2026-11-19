#!/bin/bash
# Give the freshly built libmooncake_store.so the same dependency names, RPATH and SONAME as the two
# copies in the OBS v0.3.14.rc1 wheel (auditwheel renamed the bundled libraries), so it can replace them.
# Run inside the CANN base image (needs patchelf and readelf).
# Mounts: /in/libmooncake_store.so (built), /out (writable)
# strip_and_compare.sh runs this after stripping the build output into /in/libmooncake_store.so.
set -euo pipefail
command -v patchelf >/dev/null || { apt-get update -qq -o Acquire::Retries=5 >/dev/null && apt-get install -y -qq -o Acquire::Retries=5 patchelf >/dev/null; }
# bundled names in mooncake_transfer_engine.libs/ of the OBS aarch64 cp312 wheel (from its RECORD)
declare -A MAP=(
  [libzstd.so.1]=libzstd-a1561916.so.1.4.8
  [libtransfer_engine.so]=libtransfer_engine-9fe35722.so
  [libxxhash.so.0]=libxxhash-83d27ce3.so.0.8.1
  [liburing.so.2]=liburing-f0ec91e5.so.2.1.0
  [libmooncake_common.so]=libmooncake_common-342bf32e.so
  [libyaml-cpp.so.0.7]=libyaml-cpp-1f8382c1.so.0.7.0
  [libasio.so]=libasio-ade03112.so
  [libgflags.so.2.2]=libgflags-8661e189.so.2.2.2
  [libjsoncpp.so.25]=libjsoncpp-b4b91bd8.so.1.9.5
  [libglog.so.0]=libglog-bb9621e6.so.0.4.0
)
mk() {  # $1 output path, $2 soname, $3 rpath
  mkdir -p "$(dirname "$1")"
  cp /in/libmooncake_store.so "$1"
  for old in "${!MAP[@]}"; do
    if readelf -d "$1" | grep -q "NEEDED.*\[$old\]"; then patchelf --replace-needed "$old" "${MAP[$old]}" "$1"; fi
  done
  patchelf --set-soname "$2" "$1"
  patchelf --remove-rpath "$1"
  patchelf --force-rpath --set-rpath "$3" "$1"
  echo "== $1"; readelf -d "$1" | grep -E "NEEDED|RPATH|RUNPATH|SONAME" | sed "s/.*(\(.*\)).*\[\(.*\)\]/\1 \2/"
}
mk /out/mooncake/libmooncake_store.so libmooncake_store.so '$ORIGIN/../mooncake_transfer_engine.libs'
mk /out/mooncake_transfer_engine.libs/libmooncake_store-2a608938.so libmooncake_store-2a608938.so '$ORIGIN'
