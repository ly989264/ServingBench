#!/bin/bash
# /src: v030port.tar, ascend/ (driver, d4ad49796 tar), equiv/ (pd_equiv); /kit: ascend's prep/recompute-scheduler (ro)
set -u
export PATH=/opt/venv/bin:$PATH
mkdir -p /w/vllm && tar -xf /src/v030port.tar -C /w/vllm
(cd /w/vllm && SETUPTOOLS_SCM_PRETEND_VERSION=0.30.0 VLLM_TARGET_DEVICE=empty pip install --no-deps --no-build-isolation -e . > /out/install.txt 2>&1)
mkdir -p /w/ascend && tar -xf /src/ascend/ascend-d4ad.tar -C /w/ascend
[ -f /w/ascend/vllm_ascend/_build_info.py ] || printf '__device_type__ = "A3"\n' > /w/ascend/vllm_ascend/_build_info.py
cp /w/ascend/vllm_ascend/core/recompute_scheduler.py /w/original_recompute.py
{
  echo "== original file vs SHA256SUMS.before"; (cd /w/ascend && sha256sum -c /kit/SHA256SUMS.before)
  cd /w/ascend && git init -q . && git add -A && git -c user.name=t -c user.email=t@l commit -qm base
  echo "== git apply --check of their patch on d4ad49796"; git apply --check /kit/recompute_scheduler.patch; echo "rc=$?"
  git apply /kit/recompute_scheduler.patch
  echo "== patched file vs SHA256SUMS.after and files/"; sha256sum -c /kit/SHA256SUMS.after
  cmp vllm_ascend/core/recompute_scheduler.py /kit/files/vllm_ascend/core/recompute_scheduler.py && echo "identical to files/"
  cp vllm_ascend/core/recompute_scheduler.py /w/ported_recompute.py
  git checkout -q vllm_ascend/core/recompute_scheduler.py
} > /out/patch_check.txt 2>&1
for V in vllm original_vllm ported_vllm original_balance ported_balance; do
  (cd /w/vllm && T51_RECOMPUTE_ORIGINAL=/w/original_recompute.py T51_RECOMPUTE_PORTED=/w/ported_recompute.py \
    T51_BALANCE_FILE=/w/ascend/vllm_ascend/patch/platform/patch_balance_schedule.py \
    PYTHONHASHSEED=0 HF_HUB_OFFLINE=1 VLLM_TARGET_DEVICE=cpu PYTHONPATH=/w/ascend \
    timeout 600 python -W ignore /src/ascend/ascend_equiv.py /out/$V.jsonl $V > /out/$V.log 2>&1; echo "rc=$?" >> /out/$V.log)
done
echo done > /out/DONE
