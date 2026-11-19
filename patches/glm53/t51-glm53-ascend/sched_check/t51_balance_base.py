"""pytest plugin: run the tests with BalanceScheduler as RecomputeScheduler's base.

In a vllm-ascend process, vllm_ascend/patch/platform/__init__.py imports patch_balance_schedule, which replaces the
name vllm.v1.core.sched.scheduler.Scheduler (patch_balance_schedule.py:897); a recompute_scheduler imported afterwards
derives from BalanceScheduler. vllm-ascend's tests/ut/conftest.py usually gets there already (adapt_patch); if the
name is still vLLM's class here, the module is loaded from its file (the rest of vllm_ascend.patch.platform is not
applied). Counterpart of t51_vllm_base.py.
"""

import importlib.util
import sys
from pathlib import Path


def pytest_configure(config):
    import vllm.v1.core.sched.scheduler as sched_mod

    if sched_mod.Scheduler.__qualname__ == "BalanceScheduler":
        return  # already replaced, by the real module
    if "vllm_ascend.core.recompute_scheduler" in sys.modules:  # it would keep vLLM's Scheduler as its base
        raise RuntimeError("recompute_scheduler was imported before the name was replaced")
    import vllm_ascend

    path = Path(vllm_ascend.__file__).parent / "patch" / "platform" / "patch_balance_schedule.py"
    spec = importlib.util.spec_from_file_location("t51_patch_balance_schedule", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert sched_mod.Scheduler is module.BalanceScheduler


def pytest_collection_finish(session):
    from vllm_ascend.core.recompute_scheduler import RecomputeScheduler

    base = RecomputeScheduler.__bases__[0]
    print(f"\nt51_balance_base: RecomputeScheduler base = {base.__module__}.{base.__qualname__}")
    assert base.__qualname__ == "BalanceScheduler", base
