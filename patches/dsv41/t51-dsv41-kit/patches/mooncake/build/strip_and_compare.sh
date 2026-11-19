#!/bin/bash
# Strip debug info from the built library BEFORE relinking (running strip on a patchelf-rewritten file
# leaves a load segment that is not page-aligned and the library no longer loads), relink it like the
# OBS wheel, then prove that nothing dynamic differs from the unstripped relinked files.
# CANN base image, docker run --rm.
# Mounts: /raw/libmooncake_store.so (build output), /ref (unstripped relinked files), /out (writable),
#         /relink.sh (relink_like_obs_wheel.sh)
set -euo pipefail
mkdir -p /in
strip --strip-debug -o /in/libmooncake_store.so /raw/libmooncake_store.so
echo "raw build output: $(stat -c %s /raw/libmooncake_store.so) bytes, stripped: $(stat -c %s /in/libmooncake_store.so) bytes"
bash /relink.sh > /tmp/relink.log
for rel in mooncake/libmooncake_store.so mooncake_transfer_engine.libs/libmooncake_store-2a608938.so; do
  ref=/ref/$rel; out=/out/$rel
  echo "== $rel"
  echo "   size unstripped $(stat -c %s "$ref")  stripped $(stat -c %s "$out")"
  for what in "dynamic section (NEEDED/RPATH/RUNPATH/SONAME)" "exported dynamic symbols" "imported dynamic symbols" "symbol versions needed"; do
    case "$what" in
      dyn*) a=$(readelf -d "$ref" | grep -E 'NEEDED|RPATH|RUNPATH|SONAME' | sed 's/^ *0x[0-9a-f]* //'); b=$(readelf -d "$out" | grep -E 'NEEDED|RPATH|RUNPATH|SONAME' | sed 's/^ *0x[0-9a-f]* //') ;;
      exp*) a=$(nm -D --defined-only "$ref" | awk '{print $2, $3}' | sort); b=$(nm -D --defined-only "$out" | awk '{print $2, $3}' | sort) ;;
      imp*) a=$(nm -D --undefined-only "$ref" | awk '{print $NF}' | sort); b=$(nm -D --undefined-only "$out" | awk '{print $NF}' | sort) ;;
      sym*) a=$(readelf -V "$ref" | grep -E 'Name: |Version needs|File:' | sed 's/0x[0-9a-f]*//g'); b=$(readelf -V "$out" | grep -E 'Name: |Version needs|File:' | sed 's/0x[0-9a-f]*//g') ;;
    esac
    if [ "$a" = "$b" ]; then echo "   $what: identical ($(printf '%s\n' "$a" | grep -c .) entries)"; else echo "   $what: DIFFERENT"; diff <(printf '%s\n' "$a") <(printf '%s\n' "$b") | head -5; fi
  done
  echo "   debug sections left: $(readelf -S "$out" | grep -c '\.debug_'); [BatchPutTiming] strings: $(strings -a "$out" | grep -c '\[BatchPutTiming\]')"
  echo "   load segments whose offset and address disagree modulo alignment: $(readelf -lW "$out" | python3 -c "import sys; print(sum(1 for l in sys.stdin if l.split()[:1]==['LOAD'] and int(l.split()[1],16) % int(l.split()[-1],16) != int(l.split()[2],16) % int(l.split()[-1],16)))")"
done
