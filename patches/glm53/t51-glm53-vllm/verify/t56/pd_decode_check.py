"""is_pd_decode() against vllm-ascend's own condition (utils.py of d4ad49796, real code) in a process where
vllm_ascend is loaded the way a Worker has it. usage: python pd_decode_check.py   (PYTHONPATH: the ascend tree)"""
import importlib.abc
import importlib.machinery
import importlib.util
import os
import sys
from unittest.mock import MagicMock


class _Stub(importlib.abc.Loader):
    def create_module(self, spec):
        m = MagicMock(name=spec.name)
        m.__spec__, m.__path__, m.__name__, m.__version__ = spec, [], spec.name, "0.0.0"
        return m

    def exec_module(self, module):
        pass


class _Finder(importlib.abc.MetaPathFinder):
    ROOTS = {"torch_npu", "torchair", "mooncake", "acl", "scipy"}

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in self.ROOTS and name.split(".")[0] not in _REAL:
            return importlib.machinery.ModuleSpec(name, _Stub(), is_package=True)
        return None


try:
    import numba  # noqa: F401
except Exception:
    pass
_REAL = {n for n in _Finder.ROOTS if importlib.util.find_spec(n) is not None}
sys.meta_path.insert(0, _Finder())
sys.path.insert(0, os.getcwd())

from tests.v1.core.utils import create_scheduler, mock_kv  # noqa: E402
from vllm.v1.metrics import pd_diagnostics as diag  # noqa: E402

print("vllm_ascend loaded before:", any(n.startswith("vllm_ascend") for n in sys.modules))
print("no vllm_ascend:", diag.is_pd_decode(create_scheduler(use_kv_connector=mock_kv(32, True),
                                                         kv_role="kv_consumer").vllm_config))
import vllm_ascend.ascend_config as ac  # noqa: E402
import vllm_ascend.utils  # noqa: E402,F401

CASES = [
    ("D: kv_consumer, recompute on", "kv_consumer", True, None),
    ("D: same, scheduler_cls as platform.py sets it", "kv_consumer", True,
     "vllm_ascend.core.recompute_scheduler.AsyncRecomputeScheduler"),
    ("P: kv_producer, recompute configured", "kv_producer", True, None),
    ("P: kv_producer, recompute off", "kv_producer", False, None),
    ("kv_consumer, recompute off", "kv_consumer", False, None),
    ("kv_both, recompute on", "kv_both", True, None),
    ("no kv_transfer_config, recompute on", None, True, None),
]
for name, role, recompute, cls in CASES:
    kw = dict(use_kv_connector=mock_kv(32, True), kv_role=role) if role else {}
    config = create_scheduler(**kw).vllm_config
    config.additional_config = {"scheduler_config": {"recompute_scheduler_enable": recompute}}
    config.scheduler_config.scheduler_cls = cls
    ac.clear_ascend_config()
    try:
        ac.init_ascend_config(config)
        how = "init_ascend_config"
    except Exception as exc:
        how = f"init_ascend_config failed ({type(exc).__name__}: {str(exc)[:80]})"
    real = vllm_ascend.utils.is_pd_decode_recompute_scheduler_enabled(config)
    print(f"{name}: is_pd_decode={diag.is_pd_decode(config)} ascend={real} [{how}]")
