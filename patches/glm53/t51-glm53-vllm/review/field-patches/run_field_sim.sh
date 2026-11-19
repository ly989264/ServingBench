#!/bin/bash
set -u
export PATH=/opt/venv/bin:$PATH
mkdir -p /w/port /w/clean && tar -xf /src/v030port.tar -C /w/port && tar -xf /src/v030.tar -C /w/clean
(cd /w/port && SETUPTOOLS_SCM_PRETEND_VERSION=0.30.0 VLLM_TARGET_DEVICE=empty pip install --no-deps --no-build-isolation -e . > /out/install.txt 2>&1)
for M in baseline add_copy add_copy_owner add_wrap stream_copy; do
  (cd /w/port && PYTHONHASHSEED=0 HF_HUB_OFFLINE=1 VLLM_TARGET_DEVICE=cpu timeout 600 python -W ignore /src/equiv/field_copy_sim.py /out/$M.jsonl $M /w/clean > /out/$M.log 2>&1; echo "rc=$?" >> /out/$M.log)
done
echo done > /out/DONE
