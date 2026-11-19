#!/usr/bin/env bash
# Reproduce every verification in results/. Needs docker and the images python:3, t51g-vllmtest:v1
# (torch CPU + msgspec + pyzmq + pytest; no vLLM installed). Inputs, all prepared outside this repo:
#   WORK      scratch directory holding the trees below
#   $WORK/src/d4ad    vllm-ascend checkout at d4ad49796 (clean)
#   $WORK/src/d4adp   same + t51-vllm-ascend-d4ad49796-p2d-diag.patch
#   $WORK/src/rc1p    vllm-ascend checkout at 24487f4f (RC1) + 02-ascend-v0.32.0rc1-diagnostics.patch
#   $WORK/vllm030g    `git archive v0.30.0 vllm` of vLLM + t51-glm53-vllm d3e84e2's t51-vllm-v0.30.0-pd-diag.patch
#                     (the glm5.3 port of patch 01; all V1-side runs use it)
#   $WORK/vllm030     `git archive v0.30.0 vllm` + only vllm/v1/metrics/pd_diagnostics.py from patch 01 (reference run)
#   $WORK/vllm032     same for v0.32.0
#   $WORK/d4ad49796.tar  `git archive --prefix=vllm-ascend/ d4ad49796`
set -euo pipefail
: "${WORK:?set WORK}"
R="$(cd "$(dirname "$0")/.." && pwd)"
RES="$R/results"
mkdir -p "$RES" "$WORK/out_replay" "$WORK/out_cmp" "$WORK/out_fields" "$WORK/repB" "$WORK/repC" "$WORK/origC" "$WORK/orig"
rm -f "$WORK/out_cmp/hybrid.jsonl" "$WORK/out_cmp/v1.jsonl" "$WORK/out_fields/"*.jsonl
tar -xOf "$WORK/d4ad49796.tar" vllm-ascend/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py > "$WORK/orig/mooncake_connector.py"
M=(-v "$WORK/vllm030g:/vllm:ro" -v "$R/tools/ut_harness:/harness:ro" -v "$R/tools:/tools:ro"
   -v "$R/patches/vllm-ascend-p2d/tests:/p2dtests:ro")
run() { docker run --rm --network none "$@"; }
filt() { grep -E '^(FAILED|ERROR) tests/|^=* ?[0-9]+ (passed|failed)|using replaced' || true; }

{ echo "# upstream tests/ut/distributed/kv_transfer/kv_p2p on clean d4ad49796 (no p2d test file)"
  run -v "$WORK/src/d4ad:/ro:ro" -v "$WORK/vllm030g:/vllm:ro" -v "$R/tools/ut_harness:/harness:ro" t51g-vllmtest:v1 \
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

{ echo "# colleague's tests/ut/kv_offload/test_mooncake_hybrid_connector.py on RC1 24487f4f + patch 02 (vLLM v0.32.0 + pd_diagnostics), count per line"
  run -e VLLM_VERSION=0.32.0 -v "$WORK/src/rc1p:/ro:ro" -v "$WORK/vllm032:/vllm:ro" -v "$R/tools/ut_harness:/harness:ro" t51g-vllmtest:v1 \
    bash -c 'cp -r /ro /src && printf "__device_type__ = \"A3\"\n" > /src/vllm_ascend/_build_info.py && cd /src && P2D_STUB=scipy PYTHONPATH=/src:/vllm:/harness timeout 600 python -m pytest -p no:cacheprovider -p p2d_stub_missing -q -rf -W ignore::UserWarning tests/ut/kv_offload/test_mooncake_hybrid_connector.py 2>&1; true' \
    | grep -E '^(FAILED|SUBFAILED)|^=* ?[0-9]+ (passed|failed)|^E +(KeyError|AttributeError)' | sed -E 's/ in [0-9.]+s//' | sort | uniq -c
} > "$RES/07_hybrid_tests_rc1_02.txt"

run -v "$WORK/src/d4adp:/ro:ro" "${M[@]}" -v "$WORK/out_cmp:/out" t51g-vllmtest:v1 timeout 300 sh /tools/compare/run_emit.sh v1 > /dev/null 2>&1
run -e VLLM_VERSION=0.32.0 -v "$WORK/src/rc1p:/ro:ro" -v "$WORK/vllm032:/vllm:ro" -v "$R/tools/ut_harness:/harness:ro" \
  -v "$R/tools:/tools:ro" -v "$WORK/out_cmp:/out" t51g-vllmtest:v1 timeout 300 sh /tools/compare/run_emit.sh hybrid > /dev/null 2>&1
cp "$WORK/out_cmp/hybrid.jsonl" "$RES/08_records_hybrid.jsonl"
cp "$WORK/out_cmp/v1.jsonl" "$RES/08_records_v1.jsonl"
python3 "$R/tools/compare/compare_records.py" "$RES/08_records_hybrid.jsonl" "$RES/08_records_v1.jsonl" > "$RES/08_compare_hybrid_v1.md"
{ echo "# behaviour of d4ad49796 + p2d vs the original d4ad49796 file, same scenarios on both (tools/compare/test_behavior_vs_original.py)"
  run -v "$WORK/src/d4adp:/ro:ro" "${M[@]}" -v "$WORK/orig:/orig:ro" t51g-vllmtest:v1 timeout 600 sh /tools/run_v1_tests.sh -vv tests/ut/distributed/kv_transfer/kv_p2p/test_behavior_vs_original.py 2>&1 \
    | grep -E '::test_|^=* ?[0-9]+ (passed|failed)|^FAILED' | sed -E 's/ +\[ *[0-9]+%\]$//' || true
} > "$RES/09_behavior_vs_original.txt"

run -e P2D_EXTRA=1 -e VLLM_VERSION=0.32.0 -v "$WORK/src/rc1p:/ro:ro" -v "$WORK/vllm032:/vllm:ro" -v "$R/tools/ut_harness:/harness:ro" \
  -v "$R/tools:/tools:ro" -v "$WORK/out_fields:/out" t51g-vllmtest:v1 timeout 300 sh /tools/compare/run_emit.sh hybrid > /dev/null 2>&1
run -e P2D_RECORD_DUMP=/out/v1_tests.jsonl -v "$WORK/src/d4adp:/ro:ro" "${M[@]}" -v "$WORK/out_fields:/out" t51g-vllmtest:v1 timeout 600 \
  sh /tools/run_v1_tests.sh tests/ut/distributed/kv_transfer/kv_p2p/test_mooncake_connector_pd_diag.py > /dev/null 2>&1
cp "$WORK/out_fields/hybrid.jsonl" "$RES/10_records_hybrid_all_paths.jsonl"
cp "$WORK/out_fields/v1_tests.jsonl" "$RES/10_records_v1_all_tests.jsonl"
python3 "$R/tools/compare/check_field_subset.py" "$RES/10_records_hybrid_all_paths.jsonl" "$RES/10_records_v1_all_tests.jsonl" \
  "$RES/08_records_v1.jsonl" > "$RES/10_field_subset.md" || true
{ for py in cp311-cp311 cp39-cp39; do
    echo "== python ${py%%-*}"
    rm -rf "$WORK/out_guards_$py"; mkdir -p "$WORK/out_guards_$py"
    run -v "$WORK/orig/mooncake_connector.py:/in/clean.py:ro" -v "$R:/r:ro" -v "$WORK/out_guards_$py:/out" \
      quay.io/pypa/manylinux_2_28_aarch64:latest /opt/python/$py/bin/python3 -I /r/tools/test_replay_guards.py \
      /r/patches/vllm-ascend-p2d/replay_p2d.py /in/clean.py \
      /r/patches/vllm-ascend-p2d/files/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py /out 2>&1 | grep -E '^(ok|FAIL)|failed$' || true
  done
  mkdir -p "$WORK/altlast" && cp "$WORK/out_guards_cp311-cp311/alt_all_last.py" "$WORK/altlast/mooncake_connector.py"
  echo "# kv_p2p (upstream + p2d tests + behaviour vs original) on the file replayed with every change on its last alternative anchor"
  run -v "$WORK/src/d4ad:/ro:ro" "${M[@]}" -v "$WORK/altlast:/replace:ro" -v "$WORK/orig:/orig:ro" t51g-vllmtest:v1 timeout 600 sh /tools/run_v1_tests.sh tests/ut/distributed/kv_transfer/kv_p2p/ 2>&1 | filt
} > "$RES/11_replay_guards.txt"

rm -f "$WORK/out_bytes/record_bytes.json"; mkdir -p "$WORK/out_bytes"
run -e P2D_OUT=/out -e P2D_COPY_TESTS=/tools/compare/test_measure_record_bytes.py -v "$WORK/src/d4adp:/ro:ro" "${M[@]}" \
  -v "$WORK/out_bytes:/out" t51g-vllmtest:v1 timeout 300 sh /tools/run_v1_tests.sh \
  tests/ut/distributed/kv_transfer/kv_p2p/test_measure_record_bytes.py > /dev/null 2>&1 || true
cp "$WORK/out_bytes/record_bytes.json" "$RES/12_record_bytes.json"

echo "done: $RES"
