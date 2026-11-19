"""pytest plugin: use the real (stdlib-only) vllm pd_diagnostics module under the ascend_store mocks.

The ascend_store unit tests mock all of vllm when it is not installed, which turns
pd_diagnostics into a MagicMock. Load the mocks first, then put the real module from
VLLM_COMPACT_ROOT (a vLLM v0.32.0 tree with patch 01) in its place.
"""
import importlib.util
import os
import sys

import tests.ut.distributed.ascend_store._mock_deps  # noqa: F401

_root = os.environ["VLLM_COMPACT_ROOT"]
_spec = importlib.util.spec_from_file_location(
    "vllm.v1.metrics.pd_diagnostics", os.path.join(_root, "vllm", "v1", "metrics", "pd_diagnostics.py")
)
_real = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_real)
sys.modules["vllm.v1.metrics.pd_diagnostics"] = _real
setattr(sys.modules["vllm.v1.metrics"], "pd_diagnostics", _real)
