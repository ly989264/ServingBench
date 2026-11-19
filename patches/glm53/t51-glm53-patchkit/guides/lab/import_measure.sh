#!/bin/bash
# The import check of guide A step 4.3, timed, in t51g-vllmtest:v1 (Python 3.12.3, CPU torch 2.13.0, no vLLM installed):
# clean v0.30.0 + this kit's 25 vLLM files, clean d4ad49796 + this kit's 11 vllm-ascend files + p2d's replacement
# mooncake_connector.py; torch_npu, torchair, mooncake, acl, scipy are stubs (import_measure.py). Three runs, --rm, no network.
# Usage: import_measure.sh SRC_DIR WORK   (SRC_DIR holds vllm-0300 and asc-d4a, as for walk_A.sh)
set -eu
SRC=$1; M=$2; K=$(cd "$(dirname "$0")/../../patches" && pwd)
rm -rf "$M"; mkdir -p "$M"; cp -R "$SRC/vllm-0300" "$M/vllm"; cp -R "$SRC/asc-d4a" "$M/ascend"
cp -R "$K/vllm/files/vllm/." "$M/vllm/vllm/"; cp -R "$K/vllm-ascend/files/vllm_ascend/." "$M/ascend/vllm_ascend/"
cp "$K/p2d/files/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py" "$M/ascend/vllm_ascend/distributed/kv_transfer/kv_p2p/"
printf '__device_type__ = "A3"\n' > "$M/ascend/vllm_ascend/_build_info.py"
printf '__version__ = version = "0.30.0"\n__version_tuple__ = version_tuple = (0, 30, 0)\n' > "$M/vllm/vllm/_version.py"
cp "$(dirname "$0")/import_measure.py" "$M/meas.py"
(cd "$M/vllm" && shasum -a 256 -c "$K/vllm/SHA256SUMS.after" | grep -vc ': OK$' | sed 's/^/vllm files not OK: /') || true
(cd "$M/ascend" && shasum -a 256 -c "$K/vllm-ascend/SHA256SUMS.after" | grep -vc ': OK$' | sed 's/^/vllm-ascend files not OK: /') || true
echo "mooncake_connector.py $(shasum -a 256 "$M/ascend/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py" | cut -c1-16)"
for i in 1 2 3; do
  docker run --rm --network none -v "$M:/m:ro" t51g-vllmtest:v1 sh -c 'cd /tmp && python3 -V && PYTHONPATH=/m/vllm:/m/ascend python3 -B -W ignore /m/meas.py 2>&1 | tail -1'
done
