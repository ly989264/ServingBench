"""pytest plugin: run the tests with vLLM's own Scheduler as RecomputeScheduler's base.

tests/ut/conftest.py (loaded before this hook) can already have replaced the name
vllm.v1.core.sched.scheduler.Scheduler with BalanceScheduler (patch_balance_schedule.py:897). This hook puts vLLM's
class back under the name, imports recompute_scheduler afresh, and restores whatever the name was, so only
RecomputeScheduler's base changes. Counterpart of t51_balance_base.py.
"""

import importlib
import sys


def pytest_configure(config):
    import vllm.v1.core.sched.scheduler as sched_mod

    current = sched_mod.Scheduler
    vllm_scheduler = next(c for c in current.__mro__
                          if c.__module__ == "vllm.v1.core.sched.scheduler" and c.__qualname__ == "Scheduler")
    sys.modules.pop("vllm_ascend.core.recompute_scheduler", None)
    sched_mod.Scheduler = vllm_scheduler
    try:
        importlib.import_module("vllm_ascend.core.recompute_scheduler")
    finally:
        sched_mod.Scheduler = current


def pytest_collection_finish(session):
    from vllm_ascend.core.recompute_scheduler import RecomputeScheduler

    base = RecomputeScheduler.__bases__[0]
    print(f"\nt51_vllm_base: RecomputeScheduler base = {base.__module__}.{base.__qualname__}")
    assert (base.__module__, base.__qualname__) == ("vllm.v1.core.sched.scheduler", "Scheduler"), base
