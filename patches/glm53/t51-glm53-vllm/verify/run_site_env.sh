#!/bin/bash
# Everything again on the site-like image (Python 3.11.10, transformers 5.14.1, torch 2.10.0 CPU).
# /src ro: v030.tar, ${TREE}.tar (final port), equiv/, ascend/ (driver, d4ad49796 tar, kit-ef5bd17), plug/,
#          upstream_test_scheduler_v030.py; /audit ro: trees and api_audit.py; /out.
set -u
TREE=${TREE:-v030fan}
export PATH=/opt/venv/bin:$PATH
ENVS="PYTHONHASHSEED=0 HF_HUB_OFFLINE=1 VLLM_TARGET_DEVICE=cpu"
{ python --version; pip list 2>/dev/null | grep -iE "^(torch|transformers|tokenizers|numpy|fastapi|pydantic|msgspec|pyzmq|huggingface.hub|starlette) "; } > /out/env.txt 2>&1
inst() { mkdir -p /w/$1 && tar -xf /src/$1.tar -C /w/$1 && (cd /w/$1 && SETUPTOOLS_SCM_PRETEND_VERSION=0.30.0 VLLM_TARGET_DEVICE=empty pip install --no-deps --no-build-isolation -e . > /out/$1.install.txt 2>&1; echo "rc=$?" >> /out/$1.install.txt); }
pyt() {  # tree name test-file
  n=$(echo $2 | tr / _)
  (cd /w/$1 && HF_HUB_OFFLINE=1 T51_SUBST_TOKENIZER=facebook/opt-125m VLLM_TARGET_DEVICE=cpu PYTHONPATH=/src/plug timeout 1500 python -m pytest -p t51_cpu_env -p no:cacheprovider -q -rA --timeout=300 --junitxml=/out/$1.$n.xml $2 > /out/$1.$n.log 2>&1; echo "rc=$?" >> /out/$1.$n.log)
}
# --- clean v0.30.0: environment baseline for the upstream tests, and the site-wrapper call counts
inst v030
for t in tests/v1/core/test_scheduler.py tests/v1/engine/test_output_processor.py tests/v1/metrics/test_stats.py; do pyt v030 $t; done
(cd /w/v030 && env $ENVS timeout 300 python -W ignore /src/equiv/fanout_count.py > /out/v030.fanout_count.txt 2>&1; echo "rc=$?" >> /out/v030.fanout_count.txt)
# --- final port
inst ${TREE}
for t in tests/v1/metrics/test_pd_compat.py tests/v1/worker/test_pd_finish_claim.py tests/v1/metrics/test_pd_diagnostics.py tests/v1/metrics/test_pd_compact_core.py tests/v1/metrics/test_pd_compact_batch.py tests/tools/test_pd_compact.py tests/v1/engine/test_async_llm_output_handler.py tests/v1/core/test_scheduler.py tests/v1/engine/test_output_processor.py tests/v1/metrics/test_stats.py; do pyt ${TREE} $t; done
cp /src/upstream_test_scheduler_v030.py /w/${TREE}/tests/v1/core/test_scheduler_upstream.py
pyt ${TREE} tests/v1/core/test_scheduler_upstream.py
(cd /w/${TREE} && env $ENVS timeout 300 python -W ignore /src/equiv/fanout_count.py > /out/${TREE}.fanout_count.txt 2>&1; echo "rc=$?" >> /out/${TREE}.fanout_count.txt)
(cd /w/${TREE} && env $ENVS timeout 600 python -W ignore /src/equiv/pd_equiv.py /out/${TREE}.equiv.jsonl > /out/${TREE}.equiv.log 2>&1; echo "rc=$?" >> /out/${TREE}.equiv.log)
for M in baseline add_copy add_wrap stream_copy stream_wrap p_both_copy; do
  (cd /w/${TREE} && env $ENVS timeout 600 python -W ignore /src/equiv/field_copy_sim.py /out/${TREE}.copy.$M.jsonl $M /w/v030 > /out/${TREE}.copy.$M.log 2>&1; echo "rc=$?" >> /out/${TREE}.copy.$M.log)
done
for M in baseline addreq_wrap addreq_wrap_nokw addreq_wrap_reject stream_wrap stream_wrap_force p_both; do
  (cd /w/${TREE} && env $ENVS timeout 600 python -W ignore /src/equiv/field_wrap_sim.py /out/${TREE}.$M.jsonl $M > /out/${TREE}.$M.log 2>&1; echo "rc=$?" >> /out/${TREE}.$M.log)
done
# --- schedulers of the site: P = BalanceScheduler (balance off), D = (Async)RecomputeScheduler, ascend ef5bd17
K=/src/ascend/kit-ef5bd17
mkdir -p /w/ascend && tar -xf /src/ascend/ascend-d4ad.tar -C /w/ascend
[ -f /w/ascend/vllm_ascend/_build_info.py ] || printf '__device_type__ = "A3"\n' > /w/ascend/vllm_ascend/_build_info.py
cp /w/ascend/vllm_ascend/core/recompute_scheduler.py /w/original_recompute.py
cp $K/files/vllm_ascend/core/recompute_scheduler.py /w/ported_recompute.py
for V in vllm balanceP original_vllm ported_vllm original_balance ported_balance; do
  (cd /w/${TREE} && T51_RECOMPUTE_ORIGINAL=/w/original_recompute.py T51_RECOMPUTE_PORTED=/w/ported_recompute.py \
    T51_RECOMPUTE_PREV=$K/prev_recompute_scheduler_63036ac.py \
    T51_BALANCE_FILE=/w/ascend/vllm_ascend/patch/platform/patch_balance_schedule.py \
    PYTHONPATH=/w/ascend env $ENVS timeout 600 python -W ignore /src/ascend/ascend_equiv.py /out/sched.$V.jsonl $V > /out/sched.$V.log 2>&1; echo "rc=$?" >> /out/sched.$V.log)
done
# --- API audit with Python 3.11's standard library and this image's third-party packages
(cd /audit && timeout 600 python -I -W ignore api_audit.py base patched_fan v032 final_runtime.diff > /out/api_audit.txt 2>&1; echo "rc=$?" >> /out/api_audit.txt)
echo done > /out/DONE
