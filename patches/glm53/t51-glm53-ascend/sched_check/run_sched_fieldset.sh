# Runs inside the vLLM test image (python 3.12, CPU torch, vLLM's dependencies, no vLLM installed), --network none.
# Optional T51_EXTRA_PATH: extra entries appended to PYTHONPATH (packages missing from the image).
# Mounts: /vllm (vLLM v0.30.0 + the port, ro), /ascend (vllm-ascend d4ad49796 tree whose recompute_scheduler.py is the
#         original, ro), /pkg (patches/vllm-ascend, ro), /check (this directory, ro),
#         /root/.cache/huggingface (config and tokenizer of facebook/opt-125m, ro), /out.
set -e
F=vllm_ascend/core/recompute_scheduler.py
mkdir -p /work/original && cp -r /vllm /work/vllm && cp -r /ascend /work/ascend
[ -f /work/ascend/vllm_ascend/_build_info.py ] || printf '__device_type__ = "A3"\n' > /work/ascend/vllm_ascend/_build_info.py
cd /work/ascend
grep "  $F$" /pkg/SHA256SUMS.before | sha256sum -c
cp vllm_ascend/core/recompute_scheduler.py /work/original/recompute_scheduler.py
git apply --unsafe-paths --directory=. /pkg/per-file/11-core-recompute_scheduler.patch
grep "  $F$" /pkg/SHA256SUMS.after | sha256sum -c
cmp $F /pkg/files/$F
cd /work/vllm
ORIGINAL_RECOMPUTE=/work/original/recompute_scheduler.py HF_HUB_OFFLINE=1 PYTHONHASHSEED=0 VLLM_TARGET_DEVICE=cpu \
  PYTHONPATH=/work/vllm:/work/ascend${T51_EXTRA_PATH:+:$T51_EXTRA_PATH} python -u -W ignore /check/t51_sched_fieldset.py /out
