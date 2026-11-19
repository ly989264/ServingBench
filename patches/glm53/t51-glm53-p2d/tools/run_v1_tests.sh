#!/bin/sh
# Run inside the test image: copies the read-only vllm-ascend tree, adds the p2d test, runs pytest.
# Mounts: /ro (vllm-ascend tree), /vllm (vLLM tree with vllm/v1/metrics/pd_diagnostics.py),
#         /harness (tools/ut_harness), /p2dtests (patches/vllm-ascend-p2d/tests),
#         optional /replace/mooncake_connector.py (a replayed file to test instead of the tree's),
#         optional /orig/mooncake_connector.py (the d4ad49796 original, enables test_behavior_vs_original.py).
# Env P2D_COPY_TESTS: more test files to copy next to the p2d tests.
set -e
cp -r /ro /src
printf '__device_type__ = "A3"\n' > /src/vllm_ascend/_build_info.py
cp /p2dtests/test_mooncake_connector_pd_diag.py /src/tests/ut/distributed/kv_transfer/kv_p2p/
for extra in ${P2D_COPY_TESTS:-}; do cp "$extra" /src/tests/ut/distributed/kv_transfer/kv_p2p/; done
if [ -f /orig/mooncake_connector.py ]; then
  cp /tools/compare/test_behavior_vs_original.py /src/tests/ut/distributed/kv_transfer/kv_p2p/
  export P2D_ORIGINAL=/orig/mooncake_connector.py
fi
if [ -f /replace/mooncake_connector.py ]; then
  cp /replace/mooncake_connector.py /src/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py
  echo "using replaced mooncake_connector.py $(sha256sum /replace/mooncake_connector.py | cut -c1-16)"
fi
cd /src
P2D_STUB=scipy PYTHONPATH=/src:/vllm:/harness python -m pytest -p no:cacheprovider -p p2d_stub_missing -q -rf -W ignore::UserWarning "$@" 2>&1 | grep -vE '^(INFO|WARNING|DEBUG|ERROR) [0-9]{2}-[0-9]{2} '
