#!/bin/bash
set -u
export PATH=/opt/venv/bin:$PATH
mkdir -p /w/port /w/clean && tar -xf /src/v030port.tar -C /w/port && tar -xf /src/v030.tar -C /w/clean
(cd /w/port && SETUPTOOLS_SCM_PRETEND_VERSION=0.30.0 VLLM_TARGET_DEVICE=empty pip install --no-deps --no-build-isolation -e . > /out/install.txt 2>&1)
(cd /w/port && HF_HUB_OFFLINE=1 VLLM_TARGET_DEVICE=cpu PYTHONPATH=/src/plug timeout 900 python -m pytest -p t51_cpu_env -p no:cacheprovider -q --timeout=300 tests/v1/metrics/test_pd_compat.py > /out/test_pd_compat.log 2>&1; echo "rc=$?" >> /out/test_pd_compat.log)
for M in baseline; do (cd /w/port && PYTHONHASHSEED=0 HF_HUB_OFFLINE=1 VLLM_TARGET_DEVICE=cpu timeout 600 python -W ignore /src/equiv/field_copy_sim.py /out/$M.jsonl $M /w/clean > /out/$M.log 2>&1); done
for f in /src/replay/T*.py; do n=$(basename $f .py)
  (cd /w/port && T51_FIELD_FILE=$f PYTHONHASHSEED=0 HF_HUB_OFFLINE=1 VLLM_TARGET_DEVICE=cpu timeout 600 python -W ignore /src/equiv/field_copy_sim.py /out/$n.jsonl field_module /w/clean > /out/$n.log 2>&1; echo "rc=$?" >> /out/$n.log)
done
echo done > /out/DONE
