#!/bin/sh
# Inside the test image. $1 = v1 | hybrid. Mounts: /ro (vllm-ascend tree), /vllm, /harness, /tools,
# /p2dtests (v1 only), /out (output directory).
set -e
cp -r /ro /work
printf '__device_type__ = "A3"\n' > /work/vllm_ascend/_build_info.py
if [ "$1" = v1 ]; then
  dir=tests/ut/distributed/kv_transfer/kv_p2p
  cp /p2dtests/test_mooncake_connector_pd_diag.py /tools/compare/test_emit_v1.py /work/$dir/
  target=$dir/test_emit_v1.py
else
  dir=tests/ut/kv_offload
  cp /tools/compare/test_emit_hybrid.py /work/$dir/
  target=$dir/test_emit_hybrid.py
fi
cd /work
P2D_EXTRA=${P2D_EXTRA:-} P2D_OUT=/out P2D_STUB=scipy PYTHONPATH=/work:/vllm:/harness python -m pytest -p no:cacheprovider -p p2d_stub_missing -q -W ignore::UserWarning "$target" 2>&1 | tail -${TAIL_LINES:-5}
