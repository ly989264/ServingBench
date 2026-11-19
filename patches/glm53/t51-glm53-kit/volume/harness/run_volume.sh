# Runs inside the vLLM test image (as run_joint_check.sh of t51-glm53-ascend). Mounts: /vllm, /ascend (patched trees,
# ro), /reference/pd_diagnostics.py, /joint (this directory), /root/.cache/huggingface (opt-125m config), /out.
set -e
mkdir -p /work && cp -r /vllm /work/vllm && cp -r /ascend /work/ascend
[ -f /work/ascend/vllm_ascend/_build_info.py ] || printf '__device_type__ = "A3"\n' > /work/ascend/vllm_ascend/_build_info.py
cd /work
HF_HUB_OFFLINE=1 ASCEND_ROOT=/work/ascend REFERENCE_DIAG=/reference/pd_diagnostics.py PYTHONPATH=/work/vllm:/work/ascend \
  python -u -W ignore /joint/t51_volume.py /out
