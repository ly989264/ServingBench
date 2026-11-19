#!/bin/bash
set -u
export PATH=/opt/venv/bin:$PATH
mkdir -p /w/${TREE:-v030start} && tar -xf /src/${TREE:-v030start}.tar -C /w/${TREE:-v030start}
(cd /w/${TREE:-v030start} && SETUPTOOLS_SCM_PRETEND_VERSION=0.30.0 VLLM_TARGET_DEVICE=empty pip install --no-deps --no-build-isolation -e . > /out/install.txt 2>&1)
mkdir -p /w/ascend && tar -xf /src/ascend/ascend-d4ad.tar -C /w/ascend
[ -f /w/ascend/vllm_ascend/_build_info.py ] || printf '__device_type__ = "A3"\n' > /w/ascend/vllm_ascend/_build_info.py
cp /src/ascend/kit-ef5bd17/files/vllm_ascend/core/recompute_scheduler.py /w/ascend/vllm_ascend/core/recompute_scheduler.py
for S in engine_vllm engine_balance engine_balance_on engine_recompute engine_recompute_async worker_cpu worker_npu_events worker_v2 worker_no_timing; do
  (cd /w/${TREE:-v030start} && T51_ASCEND=/w/ascend T51_TIMING=/src/ascend/pd_device_timing.py PYTHONPATH=/w/ascend PYTHONHASHSEED=0 HF_HUB_OFFLINE=1 VLLM_TARGET_DEVICE=cpu timeout 300 python -W ignore /src/equiv/startup_check.py $S > /out/$S.log 2>&1; echo "rc=$?" >> /out/$S.log)
done
(cd /w/${TREE:-v030start} && HF_HUB_OFFLINE=1 VLLM_TARGET_DEVICE=cpu PYTHONPATH=/src/plug timeout 900 python -m pytest -p t51_cpu_env -p no:cacheprovider -q -rA --timeout=300 tests/v1/metrics/test_pd_compat.py > /out/compat.log 2>&1; echo "rc=$?" >> /out/compat.log)
echo done > /out/DONE
