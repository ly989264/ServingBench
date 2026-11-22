#!/usr/bin/env bash
# Reproduce every verification in results/. Needs docker and the images python:3, t51g-vllmtest:v1
# (torch CPU + msgspec + pyzmq + pytest; no vLLM installed). Inputs, all prepared outside this repo:
#   WORK      scratch directory holding the trees below
#   $WORK/src/d4ad    vllm-ascend checkout at d4ad49796 (clean)
#   $WORK/src/d4adp   same + t58-vllm-ascend-d4ad49796-p2d-diag.patch (T58; the T51 and T56 patches are in
#                     patches/vllm-ascend-p2d/t51/ and t56/)
#   $WORK/src/rc1p    vllm-ascend checkout at 24487f4f (RC1) + 02-ascend-v0.32.0rc1-diagnostics.patch
#   $WORK/vllm030f    `git archive v0.30.0 vllm` of vLLM + t51-glm53-vllm 40152b2's t51-vllm-v0.30.0-pd-diag.patch
#                     (the final glm5.3 vLLM patch of T51; all V1-side runs use it; V1_VLLM=vllm030g selects the
#                     d3e84e2 tree of the T51 runs)
#   $WORK/vllm030     `git archive v0.30.0 vllm` + only vllm/v1/metrics/pd_diagnostics.py from patch 01 (reference run)
#   $WORK/vllm032     same for v0.32.0 (hybrid side; optional: without it the hybrid records of the T51 run in
#                     results/ are reused, T56 does not change the hybrid connector)
#   $WORK/t51_replacement.py   the T51 replacement file (patches/vllm-ascend-p2d/t51/ replay of the original)
#   $WORK/t56_replacement.py   the T56 replacement file (patches/vllm-ascend-p2d/t56/ replay of the original)
#   $WORK/d4ad49796.tar  `git archive --prefix=vllm-ascend/ d4ad49796`
set -euo pipefail
: "${WORK:?set WORK}"
R="$(cd "$(dirname "$0")/.." && pwd)"
RES="$R/results"
mkdir -p "$RES" "$WORK/out_replay" "$WORK/out_cmp" "$WORK/out_fields" "$WORK/repB" "$WORK/repC" "$WORK/origC" "$WORK/orig"
rm -f "$WORK/out_cmp/hybrid.jsonl" "$WORK/out_cmp/v1.jsonl" "$WORK/out_fields/"*.jsonl
tar -xOf "$WORK/d4ad49796.tar" vllm-ascend/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py > "$WORK/orig/mooncake_connector.py"
V1_VLLM=${V1_VLLM:-vllm030f}
M=(-v "$WORK/$V1_VLLM:/vllm:ro" -v "$R/tools/ut_harness:/harness:ro" -v "$R/tools:/tools:ro"
   -v "$R/patches/vllm-ascend-p2d/tests:/p2dtests:ro")
run() { docker run --rm --network none "$@"; }
filt() { grep -E '^(FAILED|ERROR) tests/|^=* ?[0-9]+ (passed|failed)|using replaced' || true; }

{ echo "# upstream tests/ut/distributed/kv_transfer/kv_p2p on clean d4ad49796 (no p2d test file)"
  run -v "$WORK/src/d4ad:/ro:ro" -v "$WORK/$V1_VLLM:/vllm:ro" -v "$R/tools/ut_harness:/harness:ro" t51g-vllmtest:v1 \
    bash -c 'cp -r /ro /src && printf "__device_type__ = \"A3\"\n" > /src/vllm_ascend/_build_info.py && cd /src && P2D_STUB=scipy PYTHONPATH=/src:/vllm:/harness timeout 600 python -m pytest -p no:cacheprovider -p p2d_stub_missing -q -rf -W ignore::UserWarning tests/ut/distributed/kv_transfer/kv_p2p/ 2>&1; true' | filt
} > "$RES/01_upstream_kv_p2p_clean.txt"

{ echo "# kv_p2p on d4ad49796 + p2d patch: upstream tests + test_mooncake_connector_pd_diag.py (p2d tests listed)"
  run -v "$WORK/src/d4adp:/ro:ro" "${M[@]}" t51g-vllmtest:v1 timeout 600 sh /tools/run_v1_tests.sh -vv tests/ut/distributed/kv_transfer/kv_p2p/ 2>&1 \
    | grep -E '^tests/.*test_mooncake_connector_pd_diag.py::|^=* ?[0-9]+ (passed|failed)|^FAILED' | sed -E 's/ +\[ *[0-9]+%\]$//' || true
} > "$RES/02_kv_p2p_patched.txt"
{ echo "# reference: the same on vLLM v0.30.0 with only patch 01's pd_diagnostics.py"
  run -v "$WORK/src/d4adp:/ro:ro" -v "$WORK/vllm030:/vllm:ro" -v "$R/tools/ut_harness:/harness:ro" -v "$R/tools:/tools:ro" \
    -v "$R/patches/vllm-ascend-p2d/tests:/p2dtests:ro" t51g-vllmtest:v1 timeout 600 sh /tools/run_v1_tests.sh tests/ut/distributed/kv_transfer/kv_p2p/ 2>&1 | filt
} >> "$RES/02_kv_p2p_patched.txt"

{ echo "# p2d tests on clean d4ad49796 (expected to fail: no records without the patch)"
  run -v "$WORK/src/d4ad:/ro:ro" "${M[@]}" t51g-vllmtest:v1 timeout 600 sh /tools/run_v1_tests.sh tests/ut/distributed/kv_transfer/kv_p2p/test_mooncake_connector_pd_diag.py 2>&1 | filt
} > "$RES/03_p2d_tests_on_clean.txt"

{ echo "# p2d tests, 30 repeated runs on the patched tree (count, result)"
  run -v "$WORK/src/d4adp:/ro:ro" "${M[@]}" t51g-vllmtest:v1 sh -c 'for i in $(seq 1 30); do timeout 300 sh /tools/run_v1_tests.sh tests/ut/distributed/kv_transfer/kv_p2p/test_mooncake_connector_pd_diag.py 2>&1 | grep -E "passed|failed" | tail -1 | sed -E "s/ in [0-9.]+s//"; mv /src /src.$i; done' | sort | uniq -c
} > "$RES/04_p2d_tests_repeat30.txt"

run -v "$WORK/d4ad49796.tar:/in/d4ad49796.tar:ro" -v "$R/patches/vllm-ascend-p2d:/p:ro" -v "$R/tools:/tools:ro" \
  -v "$WORK/out_replay:/out" python:3 sh /tools/apply_check.sh > "$RES/05_apply_check_and_replay.txt" 2>&1
cp "$WORK/out_replay/replayed_B.py" "$WORK/repB/mooncake_connector.py"

cp "$WORK/out_replay/replayed_C.py" "$WORK/repC/mooncake_connector.py"
cp "$WORK/out_replay/edited_C.py" "$WORK/origC/mooncake_connector.py"
{ echo "# kv_p2p (upstream + p2d tests) with mooncake_connector.py produced by replay_p2d.py from the scenario-B edited file"
  run -v "$WORK/src/d4ad:/ro:ro" "${M[@]}" -v "$WORK/repB:/replace:ro" t51g-vllmtest:v1 timeout 600 sh /tools/run_v1_tests.sh tests/ut/distributed/kv_transfer/kv_p2p/ 2>&1 | filt
  echo "# upstream kv_p2p tests on the scenario-C edited original (baseline for the next line)"
  run -v "$WORK/src/d4ad:/ro:ro" "${M[@]}" -v "$WORK/origC:/replace:ro" t51g-vllmtest:v1 timeout 600 sh /tools/run_v1_tests.sh \
    tests/ut/distributed/kv_transfer/kv_p2p/test_mooncake_connector.py tests/ut/distributed/kv_transfer/kv_p2p/test_remote_decode_lifecycle.py \
    tests/ut/distributed/kv_transfer/kv_p2p/test_remote_prefill_lifecycle.py 2>&1 | filt
  echo "# kv_p2p (upstream + p2d tests + behaviour vs the scenario-C edited original) on the file replayed from scenario C"
  run -v "$WORK/src/d4ad:/ro:ro" "${M[@]}" -v "$WORK/repC:/replace:ro" -v "$WORK/origC:/orig:ro" t51g-vllmtest:v1 timeout 600 sh /tools/run_v1_tests.sh tests/ut/distributed/kv_transfer/kv_p2p/ 2>&1 | filt
} > "$RES/06_replayed_B_C_kv_p2p.txt"

HYBRID=0; [ -d "$WORK/vllm032" ] && HYBRID=1
[ "$HYBRID" = 1 ] && { echo "# colleague's tests/ut/kv_offload/test_mooncake_hybrid_connector.py on RC1 24487f4f + patch 02 (vLLM v0.32.0 + pd_diagnostics), count per line"
  run -e VLLM_VERSION=0.32.0 -v "$WORK/src/rc1p:/ro:ro" -v "$WORK/vllm032:/vllm:ro" -v "$R/tools/ut_harness:/harness:ro" t51g-vllmtest:v1 \
    bash -c 'cp -r /ro /src && printf "__device_type__ = \"A3\"\n" > /src/vllm_ascend/_build_info.py && cd /src && P2D_STUB=scipy PYTHONPATH=/src:/vllm:/harness timeout 600 python -m pytest -p no:cacheprovider -p p2d_stub_missing -q -rf -W ignore::UserWarning tests/ut/kv_offload/test_mooncake_hybrid_connector.py 2>&1; true' \
    | grep -E '^(FAILED|SUBFAILED)|^=* ?[0-9]+ (passed|failed)|^E +(KeyError|AttributeError)' | sed -E 's/ in [0-9.]+s//' | sort | uniq -c
} > "$RES/07_hybrid_tests_rc1_02.txt"
[ "$HYBRID" = 1 ] || echo "no \$WORK/vllm032: results/07 and the hybrid records in 08, 10 are kept from the T51 run"

run -v "$WORK/src/d4adp:/ro:ro" "${M[@]}" -v "$WORK/out_cmp:/out" t51g-vllmtest:v1 timeout 300 sh /tools/compare/run_emit.sh v1 > /dev/null 2>&1
if [ "$HYBRID" = 1 ]; then
run -e VLLM_VERSION=0.32.0 -v "$WORK/src/rc1p:/ro:ro" -v "$WORK/vllm032:/vllm:ro" -v "$R/tools/ut_harness:/harness:ro" \
  -v "$R/tools:/tools:ro" -v "$WORK/out_cmp:/out" t51g-vllmtest:v1 timeout 300 sh /tools/compare/run_emit.sh hybrid > /dev/null 2>&1
cp "$WORK/out_cmp/hybrid.jsonl" "$RES/08_records_hybrid.jsonl"
fi
cp "$WORK/out_cmp/v1.jsonl" "$RES/08_records_v1.jsonl"
python3 "$R/tools/compare/compare_records.py" "$RES/08_records_hybrid.jsonl" "$RES/08_records_v1.jsonl" > "$RES/08_compare_hybrid_v1.md"
{ echo "# behaviour of d4ad49796 + p2d vs the original d4ad49796 file, same scenarios on both (tools/compare/test_behavior_vs_original.py)"
  run -v "$WORK/src/d4adp:/ro:ro" "${M[@]}" -v "$WORK/orig:/orig:ro" t51g-vllmtest:v1 timeout 600 sh /tools/run_v1_tests.sh -vv tests/ut/distributed/kv_transfer/kv_p2p/test_behavior_vs_original.py 2>&1 \
    | grep -E '::test_|^=* ?[0-9]+ (passed|failed)|^FAILED' | sed -E 's/ +\[ *[0-9]+%\]$//' || true
} > "$RES/09_behavior_vs_original.txt"

[ "$HYBRID" = 1 ] && run -e P2D_EXTRA=1 -e VLLM_VERSION=0.32.0 -v "$WORK/src/rc1p:/ro:ro" -v "$WORK/vllm032:/vllm:ro" -v "$R/tools/ut_harness:/harness:ro" \
  -v "$R/tools:/tools:ro" -v "$WORK/out_fields:/out" t51g-vllmtest:v1 timeout 300 sh /tools/compare/run_emit.sh hybrid > /dev/null 2>&1
run -e P2D_RECORD_DUMP=/out/v1_tests.jsonl -v "$WORK/src/d4adp:/ro:ro" "${M[@]}" -v "$WORK/out_fields:/out" t51g-vllmtest:v1 timeout 600 \
  sh /tools/run_v1_tests.sh tests/ut/distributed/kv_transfer/kv_p2p/test_mooncake_connector_pd_diag.py > /dev/null 2>&1
[ "$HYBRID" = 1 ] && cp "$WORK/out_fields/hybrid.jsonl" "$RES/10_records_hybrid_all_paths.jsonl"
cp "$WORK/out_fields/v1_tests.jsonl" "$RES/10_records_v1_all_tests.jsonl"
python3 "$R/tools/compare/check_field_subset.py" "$RES/10_records_hybrid_all_paths.jsonl" "$RES/10_records_v1_all_tests.jsonl" \
  "$RES/08_records_v1.jsonl" > "$RES/10_field_subset.md" || true
{ for img in python:3.11.10-slim python:3.9-slim; do
    py=${img#python:}; py=${py%-slim}
    echo "== python $py"
    rm -rf "$WORK/out_guards_$py"; mkdir -p "$WORK/out_guards_$py"
    run -v "$WORK/orig/mooncake_connector.py:/in/clean.py:ro" -v "$WORK/t51_replacement.py:/in/t51.py:ro" -v "$R:/r:ro" \
      -v "$WORK/t56_replacement.py:/in/t56.py:ro" -v "$WORK/out_guards_$py:/out" "$img" python3 -I /r/tools/test_replay_guards.py \
      /r/patches/vllm-ascend-p2d/replay_p2d.py /in/clean.py \
      /r/patches/vllm-ascend-p2d/files/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py /out \
      /r/patches/vllm-ascend-p2d/t51/replay_p2d.py /in/t51.py /r/patches/vllm-ascend-p2d/t56/replay_p2d.py /in/t56.py 2>&1 \
      | grep -E '^(ok|FAIL|skip)|failed$' || true
  done
  mkdir -p "$WORK/altlast" && cp "$WORK/out_guards_3.11.10/alt_all_last.py" "$WORK/altlast/mooncake_connector.py"
  echo "# kv_p2p (upstream + p2d tests + behaviour vs original) on the file replayed with every change on its last alternative anchor"
  run -v "$WORK/src/d4ad:/ro:ro" "${M[@]}" -v "$WORK/altlast:/replace:ro" -v "$WORK/orig:/orig:ro" t51g-vllmtest:v1 timeout 600 sh /tools/run_v1_tests.sh tests/ut/distributed/kv_transfer/kv_p2p/ 2>&1 | filt
} > "$RES/11_replay_guards.txt"

rm -f "$WORK/out_bytes/record_bytes.json"; mkdir -p "$WORK/out_bytes"
run -e P2D_OUT=/out -e P2D_COPY_TESTS=/tools/compare/test_measure_record_bytes.py -v "$WORK/src/d4adp:/ro:ro" "${M[@]}" \
  -v "$WORK/out_bytes:/out" t51g-vllmtest:v1 timeout 300 sh /tools/run_v1_tests.sh \
  tests/ut/distributed/kv_transfer/kv_p2p/test_measure_record_bytes.py > /dev/null 2>&1 || true
cp "$WORK/out_bytes/record_bytes.json" "$RES/12_record_bytes.json"
rm -f "$WORK/out_bytes/record_bytes.json"
run -e P2D_OUT=/out -e P2D_TEST_DIAG_LEVEL=1 -e P2D_COPY_TESTS=/tools/compare/test_measure_record_bytes.py -v "$WORK/src/d4adp:/ro:ro" "${M[@]}" \
  -v "$WORK/out_bytes:/out" t51g-vllmtest:v1 timeout 300 sh /tools/run_v1_tests.sh -p p2d_level \
  tests/ut/distributed/kv_transfer/kv_p2p/test_measure_record_bytes.py > /dev/null 2>&1 || true
cp "$WORK/out_bytes/record_bytes.json" "$RES/12_record_bytes_level1.json"

{ echo "# field_check/extra_checks.py lab cases (tools/test_extra_checks.py), docker --read-only, Python 3.11.10 and 3.9; T58 replacement"
  for img in python:3.11.10-slim python:3.9-slim; do
    echo "== ${img#python:}"
    run --read-only -v "$R:/r:ro" -v "$WORK/orig/mooncake_connector.py:/o.py:ro" -v "$WORK/out_replay/edited_C.py:/c.py:ro" "$img" \
      python3 -I -B /r/tools/test_extra_checks.py /r/field_check/extra_checks.py /o.py \
      /r/patches/vllm-ascend-p2d/files/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py /c.py 2>&1 || true
  done
} > "$RES/13_extra_checks.txt"

{ echo "# field_check/span_diff.py lab cases (tools/test_span_diff.py), Python 3.11.10 and 3.9"
  for img in python:3.11.10-slim python:3.9-slim; do
    echo "== ${img#python:}"
    run -v "$R:/r:ro" -v "$WORK/orig/mooncake_connector.py:/o.py:ro" -v "$WORK/t51_replacement.py:/t51.py:ro" \
      -v "$WORK/t56_replacement.py:/t56.py:ro" "$img" sh -c \
      'mkdir /g && python3 -I -B /r/tools/test_span_diff.py /r/field_check/span_diff.py /r/patches/vllm-ascend-p2d/replay_p2d.py /o.py /t51.py /t56.py /g /r/patches/vllm-ascend-p2d/files/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py' 2>&1 || true
  done
  echo "# span_diff.py on the scenario-C edited original (tools/simulate_site_edits.py C) and on its T58 replay: the same counts"
  for f in edited_C replayed_C; do python3 -I -S -B "$R/field_check/span_diff.py" "$WORK/out_replay/$f.py"; done
} > "$RES/14_span_diff.txt"

# T58: the diagnostics level. The p2d tests' records on the T56 replacement against the T58 replacement without a level
# (vLLM v0.30.0 + 40152b2 has no diag_level: 3) and with diag_level() stubbed to 3, 2, 1 (tools/ut_harness/p2d_level.py).
mkdir -p "$WORK/rep56" "$WORK/rep58" "$WORK/out_levels" && rm -f "$WORK/out_levels/"*.jsonl
cp "$WORK/t56_replacement.py" "$WORK/rep56/mooncake_connector.py"
cp "$R/patches/vllm-ascend-p2d/files/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py" "$WORK/rep58/mooncake_connector.py"
dump() {  # name, replacement dir, level ("" = not set)
  run -e "P2D_RECORD_DUMP=/out/$1.jsonl" -e "P2D_TEST_DIAG_LEVEL=$3" -v "$WORK/src/d4ad:/ro:ro" "${M[@]}" -v "$WORK/$2:/replace:ro" \
    -v "$WORK/out_levels:/out" t51g-vllmtest:v1 timeout 600 sh /tools/run_v1_tests.sh -p p2d_level --show-capture=no --tb=line \
    tests/ut/distributed/kv_transfer/kv_p2p/test_mooncake_connector_pd_diag.py 2>&1 \
    | grep -E '^=* ?[0-9]+ (passed|failed)|^E +KeyError' | sed -E 's/ in [0-9.]+s//' | sort | uniq -c | sed "s/^/  $1: /"
}
{ echo "# p2d tests per run (T58 tests fail on the T56 file: it has no level; at level 1 the T51/T56 tests that read the four"
  echo "# level-2 request fields fail with KeyError on them)"
  dump t56a rep56 ""; dump t56b rep56 ""; dump t58none rep58 ""; dump t58l3 rep58 3; dump t58l2 rep58 2; dump t58l1 rep58 1
  for pair in "t56a t56b 3" "t56a t58none 3" "t56a t58l3 3" "t56a t58l2 2" "t56a t58l1 1" "t56a t58l3 1"; do
    set -- $pair
    python3 "$R/tools/compare/compare_levels.py" "$WORK/out_levels/$1.jsonl" "$WORK/out_levels/$2.jsonl" --level "$3" \
      --skip-prefix test_t58_ > "$WORK/out_levels/cmp_$1_$2_$3.md" && code=0 || code=$?
    echo "# $1 vs $2, expected as level $3: exit $code; $(tail -1 "$WORK/out_levels/cmp_$1_$2_$3.md")"
  done
  echo "# (the last line is the negative control: a level-3 run checked as level 1 must differ)"
} > "$RES/15_levels.txt"
cp "$WORK/out_levels/cmp_t56a_t58l1_1.md" "$RES/15_compare_t56_t58_level1.md"

# T58: the two site paths (from the original; from T56 by restore + proof + replay) on simulated site files A-C, then
# the kv_p2p tests on the mode-C result, without a level and at level 1.
{ for img in python:3.11.10-slim python:3.9-slim; do
    echo "== ${img#python:}"
    rm -rf "$WORK/out_paths"; mkdir -p "$WORK/out_paths"
    run -v "$WORK/orig/mooncake_connector.py:/in/clean.py:ro" -v "$R:/r:ro" -v "$WORK/out_paths:/out" "$img" python3 -I \
      /r/tools/test_upgrade_paths.py /r/patches/vllm-ascend-p2d/replay_p2d.py /r/patches/vllm-ascend-p2d/t56/replay_p2d.py \
      /r/tools/simulate_site_edits.py /in/clean.py /out 2>&1 || true
  done
  mkdir -p "$WORK/repC58" "$WORK/origC58"
  cp "$WORK/out_paths/direct_C.py" "$WORK/repC58/mooncake_connector.py"
  cp "$WORK/out_paths/edited_C.py" "$WORK/origC58/mooncake_connector.py"
  for level in "" 1; do
    echo "# kv_p2p (upstream + p2d tests + behaviour vs the mode-C original) on the mode-C T58 file, level ${level:-not set}"
    run -e "P2D_TEST_DIAG_LEVEL=$level" -v "$WORK/src/d4ad:/ro:ro" "${M[@]}" -v "$WORK/repC58:/replace:ro" -v "$WORK/origC58:/orig:ro" \
      t51g-vllmtest:v1 timeout 600 sh /tools/run_v1_tests.sh -p p2d_level --show-capture=no --tb=line tests/ut/distributed/kv_transfer/kv_p2p/ 2>&1 \
      | grep -E '^=* ?[0-9]+ (passed|failed)|^E +KeyError|using replaced' | sed -E 's/ in [0-9.]+s//' | sort | uniq -c
  done
} > "$RES/16_upgrade_paths.txt"

echo "done: $RES"
