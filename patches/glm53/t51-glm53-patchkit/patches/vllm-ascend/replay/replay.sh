#!/bin/sh
# Re-apply the store and RecomputeScheduler edits by exact-text anchors onto a vllm-ascend tree (e.g. the files exported from site).
# Each edit must match its anchor the stated number of times, otherwise the script stops at that edit.
# Usage: sh replay.sh VLLM_ASCEND_ROOT        (run with a Python 3.10+ interpreter on PATH as python3)
set -e
here=$(cd "$(dirname "$0")" && pwd)
root=$1
S="$root/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store"
cp "$here/../files/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/diagnostics.py" "$S/diagnostics.py"
python3 -I "$here/sub.py" "$S/metadata.py" "$here/specs/01-metadata.py"
python3 -I "$here/sub.py" "$S/pool_scheduler.py" "$here/specs/02-pool_scheduler.py"
python3 -I "$here/sub.py" "$S/pool_scheduler.py" "$here/specs/03-robust1.py"
for s in 04-kv_transfer 05-kv_transfer_fix1 06-kv_transfer_fix2 07-kv_transfer_fix3; do
  python3 -I "$here/sub.py" "$S/kv_transfer.py" "$here/specs/$s.py"
done
python3 -I "$here/port_start_load_kv.py" "$S/pool_worker.py"
for s in 08-pool_worker 09-pool_worker_fix1 10-robust2; do
  python3 -I "$here/sub.py" "$S/pool_worker.py" "$here/specs/$s.py"
done
python3 -I "$here/sub.py" "$root/vllm_ascend/core/recompute_scheduler.py" "$here/specs/11-recompute_scheduler.py"
echo "store and recompute scheduler edits replayed; apply per-file/06..10 (backend, runners) with git apply [--3way]"
