#!/bin/bash
# T58 vLLM patch checks, one image. /src ro = the T56 check directory (t51.tar, t56.tar, equiv/, ascend/, plug/,
# d_equiv.py, upstream_test_scheduler_v030.py); /t58 ro: t58.tar; /out.
set -u
export PATH=/opt/venv/bin:$PATH
ENVS="PYTHONHASHSEED=0 HF_HUB_OFFLINE=1 VLLM_TARGET_DEVICE=cpu"
{ python --version; pip list 2>/dev/null | grep -iE "^(torch|transformers|tokenizers|numpy|pydantic|msgspec) "; } > /out/env.txt 2>&1
K=/src/ascend/kit-ef5bd17
mkdir -p /w/ascend && tar -xf /src/ascend/ascend-d4ad.tar -C /w/ascend
[ -f /w/ascend/vllm_ascend/_build_info.py ] || printf '__device_type__ = "A3"\n' > /w/ascend/vllm_ascend/_build_info.py
cp /w/ascend/vllm_ascend/core/recompute_scheduler.py /w/original_recompute.py
cp $K/files/vllm_ascend/core/recompute_scheduler.py /w/ported_recompute.py
cp -r /w/ascend /w/ascend_ef && cp /w/ported_recompute.py /w/ascend_ef/vllm_ascend/core/recompute_scheduler.py
BAL=/w/ascend/vllm_ascend/patch/platform/patch_balance_schedule.py
PD="tests/v1/metrics/test_pd_compat.py tests/v1/worker/test_pd_finish_claim.py tests/v1/metrics/test_pd_diagnostics.py tests/v1/metrics/test_pd_compact_core.py tests/v1/metrics/test_pd_compact_batch.py tests/tools/test_pd_compact.py tests/v1/engine/test_async_llm_output_handler.py tests/v1/engine/test_output_processor.py tests/v1/metrics/test_stats.py"
U=tests/v1/kv_connector/unit
SCHED="tests/v1/core/test_scheduler.py tests/v1/core/test_scheduler_upstream.py tests/v1/core/test_async_scheduler.py $U/test_remote_prefill_lifecycle.py $U/test_remote_decode_lifecycle.py $U/test_kv_load_failure_recovery.py $U/test_invalid_blocks_correctness.py $U/test_error_propagation.py $U/test_cache_pollution_prevention.py $U/test_bidirectional_kv_transfer.py $U/test_scheduler_kv_connector_override.py $U/test_kv_connector_lifecycle.py"
STARTUP="engine_vllm engine_balance engine_balance_on engine_recompute engine_recompute_async worker_cpu worker_npu_events worker_v2 worker_no_timing"
pyt() {  # tree test [suffix] [extra pytest args]
  n=$(echo $2 | tr / _)${3:-}
  (cd /w/$1 && HF_HUB_OFFLINE=1 T51_SUBST_TOKENIZER=facebook/opt-125m VLLM_TARGET_DEVICE=cpu PYTHONPATH=/src/plug T56_D_COUNTS=/out/$1.$n.counts timeout 1800 python -m pytest -p t51_cpu_env ${4:-} -p no:cacheprovider -q -rA --timeout=300 --junitxml=/out/$1.$n.xml $2 > /out/$1.$n.log 2>&1; echo "rc=$?" >> /out/$1.$n.log)
}
drivers() {  # tree tag [PD_DIAG_LEVEL value or "unset"]
  TR=$1; TAG=$2; LV=${3:-unset}
  if [ "$LV" = unset ]; then E=(env -u PD_DIAG_LEVEL $ENVS); else E=(env "PD_DIAG_LEVEL=$LV" $ENVS); fi
  (cd /w/$TR && "${E[@]}" timeout 900 python -W ignore /src/equiv/pd_equiv.py /out/$TAG.equiv.jsonl > /out/$TAG.equiv.log 2>&1; echo "rc=$?" >> /out/$TAG.equiv.log)
  for V in vllm balanceP original_vllm ported_vllm original_balance ported_balance; do
    (cd /w/$TR && T51_RECOMPUTE_ORIGINAL=/w/original_recompute.py T51_RECOMPUTE_PORTED=/w/ported_recompute.py \
      T51_RECOMPUTE_PREV=$K/prev_recompute_scheduler_63036ac.py T51_BALANCE_FILE=$BAL \
      PYTHONPATH=/w/ascend "${E[@]}" timeout 600 python -W ignore /src/ascend/ascend_equiv.py /out/$TAG.sched.$V.jsonl $V > /out/$TAG.sched.$V.log 2>&1; echo "rc=$?" >> /out/$TAG.sched.$V.log)
  done
  [ $TR = t51 ] && return
  for B in vllm balance; do
    (cd /w/$TR && T51_RECOMPUTE_PORTED=/w/ported_recompute.py T51_BALANCE_FILE=$BAL PYTHONPATH=/w/ascend \
      "${E[@]}" timeout 600 python -W ignore /t58/d_equiv.py /out/$TAG.d.$B.jsonl $B > /out/$TAG.d.$B.log 2>&1; echo "rc=$?" >> /out/$TAG.d.$B.log)
  done
}
startup() {  # tree tag [PD_DIAG_LEVEL value or "unset"] scenarios...
  TR=$1; TAG=$2; LV=$3; shift 3
  if [ "$LV" = unset ]; then E=(env -u PD_DIAG_LEVEL $ENVS); else E=(env "PD_DIAG_LEVEL=$LV" $ENVS); fi
  for SC in "$@"; do
    (cd /w/$TR && T51_ASCEND=/w/ascend_ef T51_TIMING=/src/ascend/pd_device_timing.py PYTHONPATH=/w/ascend_ef \
      "${E[@]}" timeout 300 python -W ignore /src/equiv/startup_check.py $SC > /out/$TAG.startup.$SC.txt 2>&1; echo "rc=$?" >> /out/$TAG.startup.$SC.txt)
  done
}
for TR in t51 t56 t58; do
  mkdir -p /w/$TR
  if [ $TR = t58 ]; then tar -xf /t58/t58.tar -C /w/$TR; else tar -xf /src/$TR.tar -C /w/$TR; fi
  cp /src/upstream_test_scheduler_v030.py /w/$TR/tests/v1/core/test_scheduler_upstream.py
  (cd /w/$TR && SETUPTOOLS_SCM_PRETEND_VERSION=0.30.0 VLLM_TARGET_DEVICE=empty pip install --no-deps --no-build-isolation -e . > /out/$TR.install.txt 2>&1; echo "rc=$?" >> /out/$TR.install.txt)
  drivers $TR $TR unset
  [ $TR = t51 ] && continue
  startup $TR $TR unset $STARTUP
  for t in $SCHED $PD; do pyt $TR $t; done
  pyt $TR tests/v1/metrics/test_pd_kv_ready_wait.py
  for t in $SCHED; do pyt $TR $t .D "-p t56_d_plugin"; done
  if [ $TR = t58 ]; then
    pyt t58 tests/v1/metrics/test_pd_diag_level.py
    for L in 1 2; do drivers t58 t58.L$L $L; startup t58 t58.L$L $L $STARTUP; done
    startup t58 t58.L3env 3 engine_recompute_async worker_npu_events
    startup t58 t58.Lempty "" engine_recompute_async worker_npu_events
    for X in 0 4 abc " 2 " 2x; do startup t58 "t58.Linv$(echo "$X" | tr -d ' ')" "$X" engine_recompute_async worker_npu_events; done
  fi
done
echo done > /out/DONE
