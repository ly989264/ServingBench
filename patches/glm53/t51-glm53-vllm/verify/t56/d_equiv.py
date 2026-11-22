"""D on site through a real EngineCore: vllm-ascend's (ported) RecomputeScheduler classes, top-level kv_role
kv_consumer, vllm-ascend's config initialized with recompute_scheduler_enable (its real ascend_config and utils,
so is_pd_decode() takes vllm-ascend's own condition), an async-loading KV connector.

usage: python d_equiv.py OUT.jsonl BASE      BASE: vllm | balance (the class RecomputeScheduler derives from)
  env T51_RECOMPUTE_PORTED: recompute_scheduler.py (t51-glm53-ascend ef5bd17), T51_BALANCE_FILE: patch_balance_schedule.py

Scenarios (pd_equiv's EngineCore driver and fake executor):
  D1_sync_remote_kv_spec5        RecomputeScheduler, async remote KV, MTP k=5 drafts (E2's setting)
  D2_async_sched_remote_kv_spec5 AsyncRecomputeScheduler, batch queue of 2, async remote KV, MTP k=5 (D on site)
  D3_async_abort_waiting         as D2; one request aborted while WAITING_FOR_REMOTE_KVS, the rest run
  D4_async_shutdown_waiting      as D2; shutdown with one request after its KV ready was seen, one before
  P1_producer_with_recompute     not D: kv_producer, recompute configured (vllm-ascend's config singleton keeps it
                                 on in EngineCore), vLLM's Scheduler as platform.py leaves it, async loads anyway
Same script and inputs on the T51 tree and on the T56 tree: records, per-step scheduling decisions.
"""

import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

OUT, BASE = sys.argv[1], sys.argv[2]


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
        root = name.split(".")[0]
        if root in self.ROOTS and root not in _REAL:
            return importlib.machinery.ModuleSpec(name, _Stub(), is_package=True)
        return None


try:
    import numba  # noqa: F401
except Exception:
    pass
_REAL = {n for n in _Finder.ROOTS if importlib.util.find_spec(n) is not None}
sys.meta_path.insert(0, _Finder())

sys.argv = [sys.argv[0], OUT + ".unused"]
sys.path.insert(0, "/src/equiv")
import pd_equiv as eq  # noqa: E402

import tests.v1.core.utils as core_utils  # noqa: E402
import vllm.v1.core.sched.scheduler as sched_mod  # noqa: E402
from tests.v1.kv_connector.unit.utils import MockKVConfig  # noqa: E402
from vllm.v1.core.sched.scheduler import Scheduler  # noqa: E402


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if BASE == "balance":
    balance = load(os.environ["T51_BALANCE_FILE"], "vllm_ascend.patch.platform.patch_balance_schedule")
    assert sched_mod.Scheduler is balance.BalanceScheduler
rs = load(os.environ["T51_RECOMPUTE_PORTED"], "vllm_ascend.core.recompute_scheduler")
sched_mod.Scheduler = Scheduler
rs.get_ascend_config = lambda: SimpleNamespace(
    finegrained_tp_config=SimpleNamespace(oproj_tensor_parallel_size=0, mlp_tensor_parallel_size=0)
)
core_utils.Scheduler, core_utils.AsyncScheduler = rs.RecomputeScheduler, rs.AsyncRecomputeScheduler
import copy  # noqa: E402

import vllm_ascend.ascend_config as ascend_config  # noqa: E402
import vllm_ascend.utils as ascend_utils  # noqa: E402
from vllm.config import VllmConfig  # noqa: E402

# Every config carries recompute_scheduler_enable in additional_config, as on site (D, and a P configured with it),
# and vllm-ascend's config singleton is initialized from it before the scheduler is built, as vllm-ascend's
# run_engine_core patch does in the EngineCore process (patch/platform/patch_engine_core.py). BalanceScheduler
# initializes it again from the same config after Scheduler.__init__.
ADDITIONAL = {"scheduler_config": {"recompute_scheduler_enable": True}}


def vllm_config_as_on_site(*args, **kwargs):
    config = VllmConfig(*args, **kwargs)
    config.additional_config = copy.deepcopy(ADDITIONAL)
    ascend_config.clear_ascend_config()
    ascend_config.init_ascend_config(config)
    return config


core_utils.VllmConfig = vllm_config_as_on_site
_create = core_utils.create_scheduler
DECODE = {}  # scenario -> [_pd_decode (T56 tree only), vllm-ascend's condition]; written to OUT.decode.json


def create_scheduler(*args, kv_role="kv_consumer", **kwargs):
    """D's top-level role by default."""
    sched = _create(*args, kv_role=kv_role, **kwargs)
    DECODE.setdefault(eq.SCENARIO[0], [getattr(sched, "_pd_decode", "absent"),
                                       ascend_utils.is_pd_decode_recompute_scheduler_enabled(sched.vllm_config)])
    return sched


core_utils.create_scheduler = create_scheduler
MRO = {c.__name__: [f"{k.__module__}.{k.__qualname__}" for k in c.__mro__[:6]]
       for c in (rs.RecomputeScheduler, rs.AsyncRecomputeScheduler)}


def digest(out):
    cached = out.scheduled_cached_reqs
    return {
        "num_scheduled_tokens": dict(out.num_scheduled_tokens),
        "total": out.total_num_scheduled_tokens,
        "new": [(r.req_id, r.block_ids, r.num_computed_tokens) for r in out.scheduled_new_reqs],
        "cached": [list(cached.req_ids), list(cached.num_computed_tokens),
                   sorted(getattr(cached, "resumed_req_ids", ()) or ())],
        "spec": {k: list(v) for k, v in out.scheduled_spec_decode_tokens.items()},
        "finished": sorted(out.finished_req_ids),
        "preempted": sorted(out.preempted_req_ids or ()),
    }


# pd_equiv's fake executor reports every WAITING_FOR_REMOTE_KVS request as loaded in each step it executes.
# With two steps in flight (batch queue) that reports one load twice, which a connector never does (vLLM
# asserts on it). Here each load is reported once; a request that leaves the state and loads again is
# reported again. Same on both trees.
_execute_model = eq.FakeExecutor.execute_model


def execute_model(self, scheduler_output, non_block=False):
    from vllm.v1.request import RequestStatus

    reported = self.__dict__.setdefault("t56_reported", set())
    waiting = {r.request_id: r for r in self.core.scheduler.requests.values()
               if r.status == RequestStatus.WAITING_FOR_REMOTE_KVS}
    reported &= set(waiting)
    hidden = [r for rid, r in waiting.items() if rid in reported]
    for r in hidden:
        r.status = RequestStatus.WAITING  # not seen by the original as waiting, restored right after
    try:
        reported |= set(waiting) - {r.request_id for r in hidden}
        return _execute_model(self, scheduler_output, non_block)
    finally:
        for r in hidden:
            r.status = RequestStatus.WAITING_FOR_REMOTE_KVS


eq.FakeExecutor.execute_model = execute_model
_make_core = eq.make_core


def make_core(scheduler, *args, **kwargs):
    assert type(scheduler) in (rs.RecomputeScheduler, rs.AsyncRecomputeScheduler), type(scheduler)
    return make_core_any(scheduler, *args, **kwargs)


def make_core_any(scheduler, *args, **kwargs):
    core = _make_core(scheduler, *args, **kwargs)
    inner = core.scheduler.schedule

    def schedule(*a, **k):
        out = inner(*a, **k)
        eq.RECORDS.append({"scenario": eq.SCENARIO[0], "decision": digest(out)})
        return out

    core.scheduler.schedule = schedule
    return core


make_core.__wrapped__ = make_core_any
eq.make_core = make_core


def async_d(num_requests):
    sched = create_scheduler(
        async_scheduling=True,
        use_kv_connector=MockKVConfig(matched_tokens=32, is_async=True),
        num_speculative_tokens=5,
        speculative_method="ngram_gpu",
        block_size=16,
    )
    core = eq.make_core(sched, batch_queue_size=2, spec_k=5, async_scheduling=True)
    reqs = core_utils.create_requests(num_requests=num_requests, num_tokens=48, max_tokens=8)
    return sched, core, reqs


def statuses(sched):
    return {r.request_id: r.status.name for r in sched.requests.values()}


try:
    eq.scenario("D1_sync_remote_kv_spec5")
    sched = create_scheduler(
        use_kv_connector=MockKVConfig(matched_tokens=32, is_async=True),
        num_speculative_tokens=5,
        block_size=16,
    )
    core = eq.make_core(sched, spec_k=5)
    for r in core_utils.create_requests(num_requests=2, num_tokens=48, max_tokens=8):
        core.add_request(r)
    eq.dump_outputs("D1_sync_remote_kv_spec5", eq.run_core(core))

    eq.scenario("D2_async_sched_remote_kv_spec5")
    sched, core, reqs = async_d(3)
    for r in reqs:
        core.add_request(r)
    eq.dump_outputs("D2_async_sched_remote_kv_spec5", eq.run_core(core))

    eq.scenario("D3_async_abort_waiting")
    sched, core, reqs = async_d(2)
    for r in reqs:
        core.add_request(r)
    core.step_with_batch_queue()  # both go to WAITING_FOR_REMOTE_KVS; KV ready not processed yet
    eq.RECORDS.append({"scenario": eq.SCENARIO[0], "before_abort": statuses(sched)})
    core.abort_requests([reqs[0].request_id])
    eq.dump_outputs("D3_async_abort_waiting", eq.run_core(core))

    eq.scenario("D4_async_shutdown_waiting")
    sched, core, reqs = async_d(2)
    for r in reqs:
        core.add_request(r)
    core.model_executor.t56_reported = {reqs[1].request_id}  # its load never completes
    core.step_with_batch_queue()  # both -> WAITING_FOR_REMOTE_KVS
    core.step_with_batch_queue()  # the first step's result: reqs[0]'s KV ready seen, not yet scheduled
    eq.RECORDS.append({"scenario": eq.SCENARIO[0], "before_shutdown": statuses(sched),
                       "ready_seen": sorted(sched.finished_recving_kv_req_ids)})
    sched.shutdown()

    eq.scenario("P1_producer_with_recompute")
    p_cls = balance.BalanceScheduler if BASE == "balance" else Scheduler
    core_utils.Scheduler = p_cls
    sched = create_scheduler(use_kv_connector=MockKVConfig(matched_tokens=32, is_async=True),
                             num_speculative_tokens=5, block_size=16, kv_role="kv_producer")
    assert type(sched) is p_cls
    core = eq.make_core.__wrapped__(sched, spec_k=5)
    for r in core_utils.create_requests(num_requests=2, num_tokens=48, max_tokens=8):
        core.add_request(r)
    eq.dump_outputs("P1_producer_with_recompute", eq.run_core(core))
except Exception:
    import traceback

    eq.RECORDS.append({"scenario": eq.SCENARIO[0], "driver_error": traceback.format_exc()})
with open(OUT, "w") as f:
    for r in eq.RECORDS:
        f.write(json.dumps(r, sort_keys=True, default=str) + "\n")
    f.write(json.dumps({"scenario": "meta", "meta": {"base": BASE, "mro": MRO}}, default=str) + "\n")
with open(OUT + ".decode.json", "w") as f:
    json.dump({"decode": DECODE}, f, sort_keys=True)
print("records", len(eq.RECORDS), "->", OUT, "decode", DECODE, flush=True)
