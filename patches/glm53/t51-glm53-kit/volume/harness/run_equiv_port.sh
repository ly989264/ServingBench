# pd_equiv.py (t51-glm53-vllm verify/, unmodified) on the patched v0.30.0 tree, by PYTHONPATH instead of pip install.
set -e
mkdir -p /work && cp -r /vllm /work/vllm && cd /work
PYTHONHASHSEED=0 HF_HUB_OFFLINE=1 VLLM_TARGET_DEVICE=cpu PYTHONPATH=/work/vllm:/equiv timeout 600 python /equiv/pd_equiv.py /out/equiv_port.jsonl
