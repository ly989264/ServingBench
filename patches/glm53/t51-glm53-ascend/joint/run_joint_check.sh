# Runs inside the vLLM test image (python 3.12, CPU torch, vLLM's dependencies, no vLLM installed).
# Optional T51_EXTRA_PATH: extra entries appended to PYTHONPATH (packages missing from the image).
# Mounts: /vllm (patched vLLM v0.30.0 tree, ro), /ascend (patched vllm-ascend d4ad49796 tree, ro),
#         /reference/pd_diagnostics.py (patch 01's new file, ro), /joint (this directory, ro), /out (writable),
#         /root/.cache/huggingface (config and tokenizer of facebook/opt-125m, ro: D's AsyncRecomputeScheduler is
#         built with vLLM's test helper, which reads that model config).
set -e
mkdir -p /work && cp -r /vllm /work/vllm && cp -r /ascend /work/ascend
[ -f /work/ascend/vllm_ascend/_build_info.py ] || printf '__device_type__ = "A3"\n' > /work/ascend/vllm_ascend/_build_info.py
cd /work
HF_HUB_OFFLINE=1 ASCEND_ROOT=/work/ascend REFERENCE_DIAG=/reference/pd_diagnostics.py PYTHONPATH=/work/vllm:/work/ascend${T51_EXTRA_PATH:+:$T51_EXTRA_PATH} \
  python -u -W ignore /joint/t51_joint_check.py /out
