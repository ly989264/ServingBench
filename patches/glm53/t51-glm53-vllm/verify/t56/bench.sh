#!/bin/bash
set -u
export PATH=/opt/venv/bin:$PATH
for TR in t51 t56; do
  rm -rf /w/$TR; mkdir -p /w/$TR && tar -xf /src/$TR.tar -C /w/$TR
  (cd /w/$TR && SETUPTOOLS_SCM_PRETEND_VERSION=0.30.0 VLLM_TARGET_DEVICE=empty pip install --no-deps --no-build-isolation -e . > /out/$TR.install.txt 2>&1)
  for ROLE in p d; do
    (cd /w/$TR && PYTHONHASHSEED=0 HF_HUB_OFFLINE=1 VLLM_TARGET_DEVICE=cpu timeout 900 python -W ignore /src/bench_sched.py $ROLE /out/$TR.$ROLE.json > /out/$TR.$ROLE.log 2>&1; echo "rc=$?" >> /out/$TR.$ROLE.log)
  done
done
