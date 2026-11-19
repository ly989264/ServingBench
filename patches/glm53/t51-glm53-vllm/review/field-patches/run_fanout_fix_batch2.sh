#!/bin/bash
# Second batch for the fan-out fix: wrapper counts n=1/2/4, v0.32.0+01 dump, remaining F1-F8 site simulations.
set -u
export PATH=/opt/venv/bin:$PATH
ENVS="PYTHONHASHSEED=0 HF_HUB_OFFLINE=1 VLLM_TARGET_DEVICE=cpu"
inst() { mkdir -p /w/$1 && tar -xf /src/$1.tar -C /w/$1 && (cd /w/$1 && SETUPTOOLS_SCM_PRETEND_VERSION=$2 VLLM_TARGET_DEVICE=empty pip install --no-deps --no-build-isolation -e . > /out/$1.install.txt 2>&1); }
for T in v030 v030port; do
  inst $T 0.30.0
  (cd /w/$T && env $ENVS timeout 300 python -W ignore /src/equiv/fanout_count.py > /out/$T.fanout_count.txt 2>&1; echo "rc=$?" >> /out/$T.fanout_count.txt)
done
inst v032p 0.32.0
(cd /w/v032p && env $ENVS timeout 600 python -W ignore /src/equiv/pd_equiv.py /out/v032p.equiv.jsonl > /out/v032p.equiv.log 2>&1; echo "rc=$?" >> /out/v032p.equiv.log)
inst v030fan 0.30.0
(cd /w/v030fan && env $ENVS timeout 300 python -W ignore /src/equiv/fanout_count.py > /out/v030fan.fanout_count.txt 2>&1; echo "rc=$?" >> /out/v030fan.fanout_count.txt)
for M in baseline add_copy add_wrap stream_copy stream_wrap p_both_copy; do
  (cd /w/v030fan && env $ENVS timeout 600 python -W ignore /src/equiv/field_copy_sim.py /out/v030fan.copy.$M.jsonl $M /w/v030 > /out/v030fan.copy.$M.log 2>&1; echo "rc=$?" >> /out/v030fan.copy.$M.log)
done
for M in addreq_wrap_nokw addreq_wrap_reject stream_wrap stream_wrap_force; do
  (cd /w/v030fan && env $ENVS timeout 600 python -W ignore /src/equiv/field_wrap_sim.py /out/v030fan.$M.jsonl $M > /out/v030fan.$M.log 2>&1; echo "rc=$?" >> /out/v030fan.$M.log)
done
echo done > /out/DONE
