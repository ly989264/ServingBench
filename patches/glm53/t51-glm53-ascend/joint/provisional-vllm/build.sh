#!/bin/sh
# Build the provisional vLLM tree used before the real v0.30.0 port arrives:
#   v0.30.0 + the three files of patch 01 that apply as-is (pd_diagnostics.py, worker_base.py, sched/output.py)
#   + the kv.load_submit / kv.save_wait spans of patch 01's mixin hunk (mixin_spans_spec.py)
#   + the reference A2 claim interface in worker_base.py (worker_base_a2_spec.py, T51 C14).
# Usage: sh build.sh VLLM_GIT_REPO PATCH_01 OUT_DIR
set -e
repo=$1; patch01=$2; out=$3; here=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$out"
git -C "$repo" archive v0.30.0 | tar -x -C "$out"
cd "$out"
git init -q && git add -A >/dev/null && git -c user.name=build -c user.email=build@localhost commit -qm v0.30.0
git apply --include=vllm/v1/worker/worker_base.py --include=vllm/v1/metrics/pd_diagnostics.py \
  --include=vllm/v1/core/sched/output.py "$patch01"
python3 -I "$here/sub.py" vllm/v1/worker/kv_connector_model_runner_mixin.py "$here/mixin_spans_spec.py"
python3 -I "$here/sub.py" vllm/v1/worker/worker_base.py "$here/worker_base_a2_spec.py"
echo "provisional vLLM tree ready: $out"
