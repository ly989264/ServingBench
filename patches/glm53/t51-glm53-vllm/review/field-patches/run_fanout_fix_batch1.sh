#!/bin/bash
# Fan-out fix (scratch 44daff3f24) against ea04aa1. /src ro; /out.
set -u
export PATH=/opt/venv/bin:$PATH
ENVS="PYTHONHASHSEED=0 HF_HUB_OFFLINE=1 VLLM_TARGET_DEVICE=cpu"
for T in v030 v030port v030fan; do
  mkdir -p /w/$T && tar -xf /src/$T.tar -C /w/$T
  (cd /w/$T && SETUPTOOLS_SCM_PRETEND_VERSION=0.30.0 VLLM_TARGET_DEVICE=empty pip install --no-deps --no-build-isolation -e . > /out/$T.install.txt 2>&1)
  (cd /w/$T && env $ENVS timeout 300 python -W ignore /src/equiv/fanout_count.py > /out/$T.fanout_count.txt 2>&1; echo "rc=$?" >> /out/$T.fanout_count.txt)
  [ $T = v030 ] && continue
  (cd /w/$T && env $ENVS timeout 600 python -W ignore /src/equiv/pd_equiv.py /out/$T.equiv.jsonl > /out/$T.equiv.log 2>&1; echo "rc=$?" >> /out/$T.equiv.log)
  for M in baseline addreq_wrap p_both; do
    (cd /w/$T && env $ENVS timeout 600 python -W ignore /src/equiv/field_wrap_sim.py /out/$T.$M.jsonl $M > /out/$T.$M.log 2>&1; echo "rc=$?" >> /out/$T.$M.log)
  done
  for t in tests/v1/metrics/test_pd_compat.py tests/v1/worker/test_pd_finish_claim.py tests/v1/metrics/test_pd_diagnostics.py tests/v1/metrics/test_pd_compact_core.py tests/v1/metrics/test_pd_compact_batch.py tests/tools/test_pd_compact.py tests/v1/engine/test_async_llm_output_handler.py tests/v1/core/test_scheduler.py tests/v1/engine/test_output_processor.py tests/v1/metrics/test_stats.py; do
    n=$(echo $t | tr / _)
    (cd /w/$T && HF_HUB_OFFLINE=1 T51_SUBST_TOKENIZER=facebook/opt-125m VLLM_TARGET_DEVICE=cpu PYTHONPATH=/src/plug timeout 1500 python -m pytest -p t51_cpu_env -p no:cacheprovider -q -rA --timeout=300 --junitxml=/out/$T.$n.xml $t > /out/$T.$n.log 2>&1; echo "rc=$?" >> /out/$T.$n.log)
  done
done
echo done > /out/DONE
