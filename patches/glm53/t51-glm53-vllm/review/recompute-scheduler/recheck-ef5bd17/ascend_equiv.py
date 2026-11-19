"""Independent check of the RecomputeScheduler port: one scheduler variant per process.

usage: python ascend_equiv.py OUT.jsonl VARIANT
  VARIANT: vllm | {original,ported,prev}_{vllm,balance}
  env T51_RECOMPUTE_ORIGINAL / T51_RECOMPUTE_PORTED / T51_RECOMPUTE_PREV: recompute_scheduler.py files
  (prev: an earlier port, run as a reverse control)
  env T51_BALANCE_FILE: path of patch_balance_schedule.py

Runs the vLLM port's pd_equiv engine scenarios E1-E7 plus E8 (KV pool exhausted:
vLLM preempts, Recompute sends the request back to P) with the chosen class for
create_scheduler(), sync and async. Writes pd_equiv's records, a per-step digest
of every SchedulerOutput, and how often three schedule() hooks fired.
"""

import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

OUT, VARIANT = sys.argv[1], sys.argv[2]


# NPU-only packages and scipy are absent in this image; nothing here calls them.
class _Stub(importlib.abc.Loader):
    def create_module(self, spec):
        m = MagicMock(name=spec.name)
        m.__spec__, m.__path__, m.__name__, m.__version__ = spec, [], spec.name, "0.0.0"
        return m

    def exec_module(self, module):
        pass


class _Finder(importlib.abc.MetaPathFinder):
    ROOTS = {n for n in ("torch_npu", "torchair", "mooncake", "acl", "scipy")}

    def find_spec(self, name, path=None, target=None):
        root = name.split(".")[0]
        if root in self.ROOTS and root not in _REAL:
            return importlib.machinery.ModuleSpec(name, _Stub(), is_package=True)
        return None


try:
    import numba  # noqa: F401  (rejects a stub scipy, so import it first)
except Exception:
    pass
_REAL = {n for n in _Finder.ROOTS if importlib.util.find_spec(n) is not None}
sys.meta_path.insert(0, _Finder())

sys.argv = [sys.argv[0], OUT + ".unused"]
sys.path.insert(0, "/src/equiv")
import pd_equiv as eq  # noqa: E402  (the vLLM port's driver, unchanged)

import tests.v1.core.utils as core_utils  # noqa: E402
import vllm.v1.core.sched.scheduler as sched_mod  # noqa: E402
from vllm.v1.core.sched.async_scheduler import AsyncScheduler  # noqa: E402
from vllm.v1.core.sched.scheduler import Scheduler  # noqa: E402


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


HOOKS = {"RecomputeScheduler.schedule": 0, "BalanceScheduler.schedule": 0, "vllm Scheduler.schedule": 0}


def counting(cls, attr, key):
    original = getattr(cls, attr)

    def hooked(self, *args, **kwargs):
        HOOKS[key] += 1
        return original(self, *args, **kwargs)

    setattr(cls, attr, hooked)


balance = None
if VARIANT == "vllm":
    sync_cls, async_cls = Scheduler, AsyncScheduler
else:
    kind, base = VARIANT.split("_")
    path = os.environ[{"original": "T51_RECOMPUTE_ORIGINAL", "ported": "T51_RECOMPUTE_PORTED",
                       "prev": "T51_RECOMPUTE_PREV"}[kind]]
    if base == "balance":
        # As in a vllm-ascend process: the balance patch replaces the name first.
        balance = load(os.environ["T51_BALANCE_FILE"], "t51_balance")
        assert sched_mod.Scheduler is balance.BalanceScheduler
    rs = load(path, "t51_recompute")
    sched_mod.Scheduler = Scheduler
    rs.get_ascend_config = lambda: SimpleNamespace(
        finegrained_tp_config=SimpleNamespace(oproj_tensor_parallel_size=0, mlp_tensor_parallel_size=0)
    )
    sync_cls, async_cls = rs.RecomputeScheduler, rs.AsyncRecomputeScheduler
    # The same symbols ms_service_metric / ms_service_profiler hook in d4ad49796.
    counting(rs.RecomputeScheduler, "schedule", "RecomputeScheduler.schedule")
    if balance is not None:
        counting(balance.BalanceScheduler, "schedule", "BalanceScheduler.schedule")
# vLLM's own class: where a hook lands if it is attached before the balance patch swaps the name.
counting(Scheduler, "schedule", "vllm Scheduler.schedule")

MRO = {c.__name__: [f"{k.__module__}.{k.__qualname__}" for k in c.__mro__[:6]] for c in (sync_cls, async_cls)}
core_utils.Scheduler, core_utils.AsyncScheduler = sync_cls, async_cls


def digest(out):
    cached = out.scheduled_cached_reqs
    return {
        "num_scheduled_tokens": dict(out.num_scheduled_tokens),
        "total": out.total_num_scheduled_tokens,
        "new": [(r.req_id, r.block_ids, r.num_computed_tokens) for r in out.scheduled_new_reqs],
        "cached": [list(cached.req_ids), [list(b) if b is not None else None for b in cached.new_block_ids],
                   list(cached.num_computed_tokens), sorted(getattr(cached, "resumed_req_ids", ()) or ())],
        "spec": {k: list(v) for k, v in out.scheduled_spec_decode_tokens.items()},
        "finished": sorted(out.finished_req_ids),
        "preempted": sorted(out.preempted_req_ids or ()),
        "recomputed": [(r.request_id, r.client_index) for r in (getattr(out, "recomputed_reqs", None) or [])],
        "has_sync_kv_loads": bool(getattr(out, "has_sync_kv_loads", False)),
        "num_common_prefix_blocks": list(out.num_common_prefix_blocks),
    }


_make_core = eq.make_core


def make_core(scheduler, *args, **kwargs):
    assert type(scheduler) in (sync_cls, async_cls), type(scheduler)
    core = _make_core(scheduler, *args, **kwargs)
    inner = core.scheduler.schedule

    def schedule(*a, **k):
        out = inner(*a, **k)
        eq.RECORDS.append({"scenario": eq.SCENARIO[0], "decision": digest(out)})
        return out

    core.scheduler.schedule = schedule
    return core


eq.make_core = make_core
hook_counts = {}
try:
    eq.engine_scenarios()
    hook_counts["E1-E7"] = dict(HOOKS)
    # E8: KV pool exhausted while decoding (the setting of vLLM's
    # test_preempt_during_execution, run to completion through EngineCore).
    for k in HOOKS:
        HOOKS[k] = 0
    eq.scenario("E8_engine_kv_exhausted")
    sched = core_utils.create_scheduler(
        max_num_batched_tokens=100, block_size=16, num_blocks=11, enable_prefix_caching=False
    )
    core = eq.make_core(sched)
    for r in core_utils.create_requests(num_requests=2, num_tokens=70, max_tokens=40, block_size=16):
        core.add_request(r)
    eq.dump_outputs("E8_engine_kv_exhausted", eq.run_core(core, max_steps=400))
    hook_counts["E8"] = dict(HOOKS)
except Exception:
    import traceback

    eq.RECORDS.append({"scenario": eq.SCENARIO[0], "driver_error": traceback.format_exc()})
with open(OUT, "w") as f:
    for r in eq.RECORDS:
        f.write(json.dumps(r, sort_keys=True, default=str) + "\n")
    f.write(json.dumps({"scenario": "meta", "meta": {"variant": VARIANT, "mro": MRO, "hooks": hook_counts}},
                       default=str) + "\n")
print("records", len(eq.RECORDS), "->", OUT, flush=True)
