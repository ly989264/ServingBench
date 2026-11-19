"""pytest plugin: extra import stubs so the d4ad49796 ascend_store unit tests can be collected without torch/vLLM.

The tree's own _mock_deps covers what f9c8e0bdb needed; d4ad49796 also imports torch.nn, regex (served by the stdlib re) and a few
vLLM submodules at import time. Only names listed here are stubbed; a test that really calls into them is
not meaningful, and those tests are reported as they fail.
"""
import sys
from unittest.mock import MagicMock

import tests.ut.distributed.ascend_store._mock_deps  # noqa: F401

EXTRA = [name for name in __import__("os").environ.get("T51_EXTRA_STUBS", "").split(",") if name]
DEFAULT = [
    "torch.nn",
    "torch.nn.functional",
    "vllm.v1.core.sched.scheduler",
    "vllm.v1.kv_cache_layout",
    "vllm_ascend.spec_decode",
    "vllm_ascend.spec_decode.dspark_utils",
]


def _stub(name: str) -> None:
    if name in sys.modules:
        return
    module = MagicMock(name=name)
    sys.modules[name] = module
    parent, _, child = name.rpartition(".")
    if parent and parent in sys.modules:
        try:
            setattr(sys.modules[parent], child, module)
        except Exception:
            pass


# The tree only uses regex for plain patterns that the stdlib re accepts as well.
if "regex" not in sys.modules:
    import importlib.util
    import re

    if importlib.util.find_spec("regex") is None:
        sys.modules["regex"] = re

for _name in DEFAULT + EXTRA:
    _stub(_name)
