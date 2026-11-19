"""Same requests through vLLM's Scheduler, the original RecomputeScheduler and the ported one; compare the records.

Run inside the vLLM test image (see run_sched_fieldset.sh): python t51_sched_fieldset.py OUT_DIR
  /work/vllm   vLLM v0.30.0 + the port (patch 01), current directory, first on sys.path
  /work/ascend vllm-ascend d4ad49796 + this kit + the RecomputeScheduler port
  ORIGINAL_RECOMPUTE  the unmodified d4ad49796 recompute_scheduler.py, loaded under another module name

The engine side is vendor/pd_equiv.py from the vLLM port's verification, unchanged: the real EngineCore.step /
step_with_batch_queue, KVCacheManager, Request and the worker_base decorators, with a fake executor that returns
seeded random tokens. Each scenario runs once per scheduler with the same seed. Schedulers: vLLM's Scheduler;
BalanceScheduler with balance off (the default class in a vllm-ascend process); the original and the ported
RecomputeScheduler, each on vLLM's Scheduler and on BalanceScheduler as base (which one it gets in a vllm-ascend
process depends on import order). Checks:
  A. field sets: for every record kind, the set of key paths written with the ported RecomputeScheduler (both bases)
     and with BalanceScheduler equals the set written with vLLM's Scheduler; the original RecomputeScheduler is the
     negative control (it must differ);
  B. scheduling behaviour: the ported and the original RecomputeScheduler on the same base make the same decisions
     every step and return the same outputs to the client; so do the two bases;
  C. values: where the decisions are the same as vLLM's Scheduler, the records are the same once ids and times are
     masked;
  D. the step id chain: the scheduler sets SchedulerOutput.pd_execution_id, and the step's execution record carries
     the same id; the queue stage of every request record is measured; a failing connector lookup is recorded as in
     vLLM's Scheduler.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import inspect
import json
import os
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/out")
ORIGINAL = Path(os.environ.get("ORIGINAL_RECOMPUTE", "/original/recompute_scheduler.py"))
HERE = Path(__file__).resolve().parent
RESULTS: list[dict] = []


def check(part, item, ok, detail="", expected=False):
    """expected=True: a difference the contract records as expected (reported apart, not a failure)."""
    RESULTS.append({"part": part, "item": item, "ok": bool(ok), "expected": bool(expected),
                    "detail": str(detail)[:2000]})
    label = "EXPECTED " if expected else ("PASS " if ok else "FAIL ")
    print(label + f"[{part}] {item}" + (f"  -- {str(detail)[:600]}" if detail and (not ok or expected) else ""),
          flush=True)
    return ok


# ---------------------------------------------------------------- stubs for NPU-only packages (as the joint check)
STUB_ROOTS = [n for n in ("torch_npu", "torchair", "mooncake", "acl") if importlib.util.find_spec(n) is None]


class _StubLoader(importlib.abc.Loader):
    def create_module(self, spec):
        module = MagicMock(name=spec.name)
        module.__spec__ = spec
        module.__path__ = []
        module.__name__ = spec.name
        module.__version__ = "0.0.0"
        return module

    def exec_module(self, module):
        pass


class _StubFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in STUB_ROOTS:
            return importlib.machinery.ModuleSpec(name, _StubLoader(), is_package=True)
        return None


sys.meta_path.insert(0, _StubFinder())

# ---------------------------------------------------------------- engine side: the vLLM port's own driver, unchanged
sys.argv = [sys.argv[0], str(OUT / "pd_equiv-unused.jsonl")]
sys.path.insert(0, str(HERE / "vendor"))
import pd_equiv as eq  # noqa: E402

from tests.v1.core import utils as core_utils  # noqa: E402
from tests.v1.kv_connector.unit.utils import MockKVConfig  # noqa: E402
from vllm.v1.core.sched.async_scheduler import AsyncScheduler  # noqa: E402
from vllm.v1.core.sched.scheduler import Scheduler  # noqa: E402
from vllm.v1.outputs import KVConnectorOutput  # noqa: E402
from vllm.v1.request import RequestStatus  # noqa: E402

import vllm.v1.core.sched.scheduler as sched_mod  # noqa: E402
import vllm_ascend.core.recompute_scheduler as ported  # noqa: E402  (imported first: derives from vLLM's Scheduler)


def load_as(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # its dataclasses look the module up
    spec.loader.exec_module(module)
    return module


original = load_as(ORIGINAL, "t51_original_recompute_scheduler")

# In a vllm-ascend process, vllm_ascend/patch/platform/__init__.py imports patch_balance_schedule, which replaces the
# name vllm.v1.core.sched.scheduler.Scheduler with BalanceScheduler (patch_balance_schedule.py:897). A
# recompute_scheduler imported after that derives from BalanceScheduler, whose schedule() calls super().schedule()
# while balance scheduling is off (it is refused on PD-disaggregated nodes). Both bases are run. The balance module is
# loaded from its file, so the rest of vllm_ascend.patch.platform is not applied.
BALANCE_FILE = Path(ported.__file__).parents[1] / "patch" / "platform" / "patch_balance_schedule.py"
balance = load_as(BALANCE_FILE, "t51_patch_balance_schedule")
ported_on_balance = load_as(Path(ported.__file__), "t51_ported_recompute_on_balance")
original_on_balance = load_as(ORIGINAL, "t51_original_recompute_on_balance")
sched_mod.Scheduler = Scheduler  # restore the name for everything else

# _preempt_or_recompute reads the AscendConfig singleton; off, as the unit tests' fixture does.
_FTPC = SimpleNamespace(finegrained_tp_config=SimpleNamespace(oproj_tensor_parallel_size=0, mlp_tensor_parallel_size=0))
for module in (ported, original, ported_on_balance, original_on_balance):
    module.get_ascend_config = lambda: _FTPC

VARIANTS = {
    "vllm": (Scheduler, AsyncScheduler),
    "balance_off": (balance.BalanceScheduler, AsyncScheduler),  # the default sync class in a vllm-ascend process
    "original": (original.RecomputeScheduler, original.AsyncRecomputeScheduler),
    "ported": (ported.RecomputeScheduler, ported.AsyncRecomputeScheduler),
    "original_on_balance": (original_on_balance.RecomputeScheduler, original_on_balance.AsyncRecomputeScheduler),
    "ported_on_balance": (ported_on_balance.RecomputeScheduler, ported_on_balance.AsyncRecomputeScheduler),
}
PORTED = ("ported", "ported_on_balance")
ORIGINALS = {"ported": "original", "ported_on_balance": "original_on_balance"}
WRAPPED = ("ported", "ported_on_balance", "balance_off")  # expected to write the same records as vLLM's Scheduler

check("setup", "ported module is the tree's vllm_ascend.core.recompute_scheduler, with _schedule",
      "_schedule" in vars(ported.RecomputeScheduler) and "_schedule" not in vars(original.RecomputeScheduler))
check("setup", "ported and original derive from vLLM's own Scheduler",
      all(cls.__bases__ == (Scheduler,) for cls in (ported.RecomputeScheduler, original.RecomputeScheduler))
      and Scheduler.__module__ == "vllm.v1.core.sched.scheduler" and "_schedule" in vars(Scheduler))
check("setup", "*_on_balance derive from BalanceScheduler, which derives from vLLM's Scheduler",
      all(cls.__bases__ == (balance.BalanceScheduler,)
          for cls in (ported_on_balance.RecomputeScheduler, original_on_balance.RecomputeScheduler))
      and balance.BalanceScheduler.__bases__ == (Scheduler,) and "_schedule" not in vars(balance.BalanceScheduler))
# Counting hooks on the names vllm-ascend's ms_service_metric / ms_service_profiler hook
# (observability/config/base_metrics.yaml, profiling_config.py): BalanceScheduler.schedule and each
# RecomputeScheduler.schedule. The original RecomputeScheduler never runs BalanceScheduler.schedule; the port must not
# either. Installed after the class checks below read the class bodies.
HOOK_CALLS: dict[str, int] = {}


def count_calls(cls, label):
    inner = vars(cls)["schedule"]

    def schedule(self, *args, **kwargs):
        HOOK_CALLS[label] = HOOK_CALLS.get(label, 0) + 1
        return inner(self, *args, **kwargs)

    cls.schedule = schedule


check("setup", "ported schedule() does not call super().schedule(); it calls vLLM's own Scheduler.schedule",
      "super().schedule" not in inspect.getsource(vars(ported.RecomputeScheduler)["schedule"])
      and "cls.schedule(self" in inspect.getsource(vars(ported.RecomputeScheduler)["schedule"]))
check("setup", "RecomputeScheduler.schedule stays defined in its own class (ported and original)",
      "schedule" in vars(ported.RecomputeScheduler) and "schedule" in vars(original.RecomputeScheduler))


count_calls(balance.BalanceScheduler, "BalanceScheduler.schedule")
for module in (ported, original, ported_on_balance, original_on_balance):
    count_calls(module.RecomputeScheduler, "RecomputeScheduler.schedule")

# ---------------------------------------------------------------- scenarios
def decision(output):
    cached = output.scheduled_cached_reqs
    return {
        "num_scheduled_tokens": dict(output.num_scheduled_tokens),
        "total": output.total_num_scheduled_tokens,
        "new": [(r.req_id, r.block_ids, r.num_computed_tokens) for r in output.scheduled_new_reqs],
        "cached": {
            "req_ids": list(cached.req_ids),
            "new_block_ids": [list(b) if b is not None else None for b in cached.new_block_ids],
            "num_computed_tokens": list(cached.num_computed_tokens),
            "resumed": sorted(getattr(cached, "resumed_req_ids", ()) or ()),
        },
        "spec": {k: list(v) for k, v in output.scheduled_spec_decode_tokens.items()},
        "finished": sorted(output.finished_req_ids),
        "preempted": sorted(output.preempted_req_ids or ()),
        "recomputed": [(r.request_id, r.client_index) for r in (getattr(output, "recomputed_reqs", None) or [])],
        "has_sync_kv_loads": bool(getattr(output, "has_sync_kv_loads", False)),
        "num_common_prefix_blocks": list(output.num_common_prefix_blocks),
    }


def make(variant, *, async_scheduling=False, batch_queue_size=1, spec_k=0, **kwargs):
    sync_cls, async_cls = VARIANTS[variant]
    core_utils.Scheduler, core_utils.AsyncScheduler = sync_cls, async_cls
    try:
        sched = core_utils.create_scheduler(async_scheduling=async_scheduling, **kwargs)
    finally:
        core_utils.Scheduler, core_utils.AsyncScheduler = Scheduler, AsyncScheduler
    expected = async_cls if async_scheduling else sync_cls
    assert type(sched) is expected, (type(sched), expected)
    HOOK_CALLS["class"] = expected.__qualname__
    core = eq.make_core(sched, batch_queue_size=batch_queue_size, spec_k=spec_k, async_scheduling=async_scheduling)
    inner = sched.schedule  # pd_equiv's recorder around the scheduler's own schedule()

    def schedule_and_decide(*args, **kw):
        output = inner(*args, **kw)
        eq.RECORDS.append({"scenario": eq.SCENARIO[0], "decision": decision(output)})
        return output

    sched.schedule = schedule_and_decide
    return core, sched


def s1(variant):
    core, _ = make(variant, enable_prefix_caching=True, max_num_batched_tokens=64, block_size=16)
    for i, r in enumerate(eq.create_requests(num_requests=3, num_tokens=40, max_tokens=3, same_prompt=True)):
        r.pd_identity = {"observation_id": f"api-x-{i}", "submission_id": f"sub-{i}"}
        core.add_request(r)
    return eq.run_core(core)


def s2(variant):
    core, _ = make(variant, use_kv_connector=MockKVConfig(matched_tokens=32, is_async=True), block_size=16)
    for r in eq.create_requests(num_requests=2, num_tokens=48, max_tokens=8):
        core.add_request(r)
    return eq.run_core(core)


def s3(variant):
    core, _ = make(variant, use_kv_connector=MockKVConfig(matched_tokens=32, is_async=True),
                   num_speculative_tokens=5, block_size=16, spec_k=5)
    for r in eq.create_requests(num_requests=2, num_tokens=48, max_tokens=8):
        core.add_request(r)
    return eq.run_core(core)


def s4(variant):
    core, _ = make(variant, block_size=16, batch_queue_size=2)
    for r in eq.create_requests(num_requests=2, num_tokens=20, max_tokens=4):
        core.add_request(r)
    return eq.run_core(core)


def s5(variant):
    core, sched = make(variant, block_size=16)
    rs = eq.create_requests(num_requests=2, num_tokens=20, max_tokens=50)
    for r in rs:
        core.add_request(r)
    core.step()
    core.abort_requests([rs[0].request_id])
    core.step()
    sched.shutdown()
    return []


def s6(variant):
    core, _ = make(variant, async_scheduling=True, block_size=16, batch_queue_size=2)
    for r in eq.create_requests(num_requests=3, num_tokens=24, max_tokens=5):
        core.add_request(r)
    return eq.run_core(core)


def s7(variant):
    core, _ = make(variant, use_kv_connector=MockKVConfig(matched_tokens=32, is_async=False), block_size=16)
    core.connector_fake.load_delay_s = 0.02
    for r in eq.create_requests(num_requests=2, num_tokens=48, max_tokens=3):
        core.add_request(r)
    return eq.run_core(core)


def s8(variant):
    # Too few KV blocks for both requests' decodes: vLLM preempts and resumes, RecomputeScheduler (no offload
    # connector) sends the request back to P. Different decisions by design; only the field sets are compared.
    core, _ = make(variant, block_size=16, num_blocks=8, max_num_batched_tokens=256)
    for r in eq.create_requests(num_requests=2, num_tokens=48, max_tokens=24):
        core.add_request(r)
    return eq.run_core(core)


class OnceRecvExecutor(eq.FakeExecutor):
    """pd_equiv's executor, except that each remote KV is reported received once.

    With async scheduling two steps are in flight; the original reports a request still waiting for remote KV in
    both, and vLLM's own Scheduler then asserts (_update_from_kv_xfer_finished). Otherwise the same as
    FakeExecutor.execute_model.
    """

    def execute_model(self, scheduler_output, non_block=False):
        reported = self.__dict__.setdefault("reported", set())
        waiting = [r.request_id for r in self.core.scheduler.requests.values()
                   if r.status == RequestStatus.WAITING_FOR_REMOTE_KVS and r.request_id not in reported]
        reported.update(waiting)
        self.connector.pending_recv |= set(waiting)
        eq.RECORDS.append({"scenario": eq.SCENARIO[0], "step": {
            "seen_by": "execute_model", "execution_id": scheduler_output.pd_execution_id,
            "request_ids": list(scheduler_output.num_scheduled_tokens),
            "total_tokens": scheduler_output.total_num_scheduled_tokens,
            "has_sync_kv_loads": bool(getattr(scheduler_output, "has_sync_kv_loads", False))}})
        out = self.wrapper.execute_model(scheduler_output)
        self.pending_so = scheduler_output
        if out is not None:
            out.kv_connector_output = KVConnectorOutput(finished_recving=set(waiting) or None)
        return self._done(out) if non_block else out


def s10(variant):
    # The site's D: async scheduling (AsyncRecomputeScheduler), batch queue 2, async remote KV.
    core, _ = make(variant, async_scheduling=True, batch_queue_size=2, block_size=16,
                   use_kv_connector=MockKVConfig(matched_tokens=32, is_async=True))
    core.model_executor.__class__ = OnceRecvExecutor
    for r in eq.create_requests(num_requests=3, num_tokens=48, max_tokens=6):
        core.add_request(r)
    return eq.run_core(core)


def s11(variant):
    # As S10, with MTP k=5 drafts (GPU-side drafting, which async scheduling requires).
    core, _ = make(variant, async_scheduling=True, batch_queue_size=2, block_size=16, spec_k=5,
                   num_speculative_tokens=5, speculative_method="ngram_gpu",
                   use_kv_connector=MockKVConfig(matched_tokens=32, is_async=True))
    core.model_executor.__class__ = OnceRecvExecutor
    for r in eq.create_requests(num_requests=2, num_tokens=48, max_tokens=8):
        core.add_request(r)
    return eq.run_core(core)


def s12(variant):
    # The site's P: synchronous scheduling, MTP k=1, synchronous KV load (load_async=0).
    core, _ = make(variant, use_kv_connector=MockKVConfig(matched_tokens=32, is_async=False), num_speculative_tokens=1,
                   block_size=16, spec_k=1)
    core.connector_fake.load_delay_s = 0.02
    for r in eq.create_requests(num_requests=2, num_tokens=48, max_tokens=4):
        core.add_request(r)
    return eq.run_core(core)


def s9(variant):
    # The KV connector lookup raises inside schedule(): the step fails, the scheduler record is written with the error.
    core, sched = make(variant, use_kv_connector=MockKVConfig(matched_tokens=32, is_async=True), block_size=16)
    for r in eq.create_requests(num_requests=1, num_tokens=48, max_tokens=2):
        core.add_request(r)

    def lookup_fails(*args, **kwargs):
        raise RuntimeError("lookup failed")

    sched.connector.get_num_new_matched_tokens = lookup_fails
    try:
        core.step()
    except RuntimeError:
        return []
    raise AssertionError("the step did not raise")


SCENARIOS = {
    "S1_sync_prefix_cache_chunked_prefill": (s1, False),
    "S2_D_remote_kv_async": (s2, False),
    "S3_D_remote_kv_async_mtp5": (s3, False),
    "S4_batch_queue": (s4, True),
    "S5_abort_shutdown": (s5, False),
    "S6_async_scheduling": (s6, True),
    "S7_sync_kv_load": (s7, False),
    "S8_kv_blocks_exhausted": (s8, False),
    "S9_lookup_raises": (s9, False),
    "S10_D_async_scheduling_remote_kv": (s10, True),
    "S11_D_async_scheduling_remote_kv_mtp5": (s11, True),
    "S12_P_sync_mtp1_sync_kv_load": (s12, False),
}
RAISES = {"S9_lookup_raises"}


def run(name, fn, variant):
    eq.RNG.seed(1234)
    eq.RECORDS.clear()
    HOOK_CALLS.clear()
    eq.scenario(f"{name}:{variant}")
    try:
        eq.dump_outputs(eq.SCENARIO[0], fn(variant))
        error = None
    except Exception:
        error = traceback.format_exc()
    return list(eq.RECORDS) + [{"hooks": dict(HOOK_CALLS)}], error


# ---------------------------------------------------------------- comparison helpers
MASK_KEYS = {"record_id", "execution_id", "last_execution_id", "engine_id", "output_batch_id", "pd_execution_id"}


def kind(rec):
    return (rec.get("event"), rec.get("scope"), rec.get("phase"))


def paths(obj, prefix=""):
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(f"{prefix}.{k}")
            out |= paths(v, f"{prefix}.{k}")
    elif isinstance(obj, list):
        for v in obj:
            out |= paths(v, prefix + "[]")
    return out


def field_sets(records):
    sets: dict[str, set] = {}
    for item in records:
        if "rec" in item:
            sets.setdefault(json.dumps(kind(item["rec"])), set()).update(paths(item["rec"]))
    return sets


def mask(obj, key=None):
    if isinstance(obj, dict):
        return {k: mask(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask(v, key) for v in obj]
    if key is not None and (key.endswith("_ns") or key in MASK_KEYS or key.endswith("_ts")):
        return None if obj is None else "<masked>"
    return obj


def masked_records(records):
    return sorted(json.dumps(mask(i["rec"]), sort_keys=True) for i in records if "rec" in i)


def decisions(records):
    return [i["decision"] for i in records if "decision" in i]


def transports(records):
    return [mask(i["transport"]) for i in records if "transport" in i]


def first_diff(a, b):
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return f"item {i}: {str(x)[:500]} != {str(y)[:500]}"
    return f"lengths {len(a)} != {len(b)}" if len(a) != len(b) else ""


def chain_problems(records):
    """Every schedule() output carries the scheduler's own id, and that step's execution record has it."""
    problems = []
    seen = [i["step"] for i in records if "step" in i and i["step"]["seen_by"] == "scheduler.schedule"]
    execution_records = {i["rec"].get("execution_id"): i["rec"] for i in records
                         if "rec" in i and kind(i["rec"]) == ("core_summary", "execution", None)}
    ids = [step["execution_id"] for step in seen if step["execution_id"] is not None]
    if len(ids) != len(set(ids)):
        problems.append(f"execution_id repeated across steps: {len(ids) - len(set(ids))} duplicates")
    for step in seen:
        eid = step["execution_id"]
        if eid is None:
            problems.append("SchedulerOutput.pd_execution_id is None when schedule() returns")
        elif eid not in execution_records:
            problems.append(f"no core_summary[execution] record for {eid}")
        elif execution_records[eid].get("request_ids") != step["request_ids"]:
            problems.append(f"{eid}: record request_ids {execution_records[eid].get('request_ids')} != {step['request_ids']}")
    return problems, len(seen)


def queue_statuses(records):
    return sorted({(i["rec"].get("request_id"), ((i["rec"].get("stages") or {}).get("queue") or {}).get("status"))
                   for i in records if "rec" in i and kind(i["rec"]) == ("core_summary", "request", None)})


# ---------------------------------------------------------------- run
def set_diff(a, b):
    diff = {k: {"only_vllm": sorted(a.get(k, set()) - b.get(k, set())),
                "only_other": sorted(b.get(k, set()) - a.get(k, set()))} for k in set(a) | set(b)}
    return {k: v for k, v in diff.items() if v["only_vllm"] or v["only_other"]}


REQUEST_KIND = json.dumps(("core_summary", "request", None))
REPORT = {"scenarios": {}}
for name, (fn, timing_dependent) in SCENARIOS.items():
    runs = {}
    for variant in VARIANTS:
        records, error = run(name, fn, variant)
        runs[variant] = records
        check("run", f"{name}: {variant} ran without driver error", error is None, error or "")
    sets = {v: field_sets(r) for v, r in runs.items()}
    entry = REPORT["scenarios"][name] = {
        "records": {v: sum("rec" in i for i in r) for v, r in runs.items()},
        "steps": {v: len(decisions(r)) for v, r in runs.items()},
        "kinds": sorted(set().union(*[set(s) for s in sets.values()])),
    }
    note = " (async steps overlap; decisions may depend on timing)" if timing_dependent else ""
    d_vllm = decisions(runs["vllm"])

    for variant in WRAPPED:
        # A. field sets
        diff = set_diff(sets["vllm"], sets[variant])
        title = (f"{name}: {variant} == vLLM Scheduler, every record kind ({len(sets['vllm'])} kinds, "
                 f"{sum(len(s) for s in sets['vllm'].values())} key paths)")
        if not diff:
            check("A field sets", title, bool(sets["vllm"]), "no records")
        else:
            # Contract (a): a request that RecomputeScheduler ends inside schedule() (sent back to P) has its request
            # record written inside the scheduler's diag scope, and Batch takes current() as its base: compared with
            # a request that finishes in the EngineCore step's scope, it has role ("unknown") and lacks no_forward.
            # vLLM's own aborted / shutdown requests lack no_forward too, so only role is new.
            requests = [i["rec"] for i in runs[variant] if "rec" in i and json.dumps(kind(i["rec"])) == REQUEST_KIND]
            recomputed = {rid for d in decisions(runs[variant]) for rid, _ in d["recomputed"]}
            inside = [r for r in requests if r["request_id"] in recomputed]
            normal = [r for r in requests if r["request_id"] not in recomputed and r.get("completion") ==
                      "FINISHED_LENGTH_CAPPED"]
            key_diff = [(sorted(set(r) - set(normal[0])), sorted(set(normal[0]) - set(r))) for r in inside] if normal \
                else []
            pinned = (set(diff) == {REQUEST_KIND} and diff[REQUEST_KIND] == {"only_vllm": [], "only_other": [".role"]}
                      and bool(inside) and {r["request_id"] for r in requests if "role" in r} == recomputed
                      and all(r["role"] == "unknown" for r in inside)
                      and key_diff and all(k == (["role"], ["no_forward"]) for k in key_diff))
            check("A field sets", title, pinned, {"diff": diff, "requests_sent_back_to_P": sorted(recomputed)},
                  expected=pinned)
            check("A field sets", f"{name}: {variant}: cause pinned: only the request records of the requests "
                  f"RecomputeScheduler ended inside schedule() ({sorted(recomputed)}) differ, by +role ('unknown') "
                  f"and -no_forward against a normally finished request", pinned,
                  {"diff": diff, "key_diff": key_diff})
            entry.setdefault("field_set_difference", {})[variant] = diff

        # E. hooks: the names ms_service_metric / ms_service_profiler hook
        hooks = next(i["hooks"] for i in runs[variant] if "hooks" in i)
        steps = len(decisions(runs[variant]))
        if variant == "balance_off" and hooks.get("class") == "BalanceScheduler":
            check("E hooks", f"{name}: balance_off (BalanceScheduler): the counting hook on BalanceScheduler.schedule "
                  f"works, one call per step ({steps} steps)",
                  hooks.get("BalanceScheduler.schedule") == steps or name in RAISES, hooks)
        elif variant == "balance_off":  # async scenarios: the default async class is vLLM's AsyncScheduler
            check("E hooks", f"{name}: balance_off ({hooks.get('class')}): BalanceScheduler.schedule not called",
                  hooks.get("BalanceScheduler.schedule", 0) == 0, hooks)
        else:
            check("E hooks", f"{name}: {variant}: BalanceScheduler.schedule never called, RecomputeScheduler.schedule "
                  f"once per step ({steps} steps)", hooks.get("BalanceScheduler.schedule", 0) == 0
                  and (hooks.get("RecomputeScheduler.schedule") == steps or name in RAISES), hooks)

        # C. values against vLLM's Scheduler where the decisions are the same
        d_variant = decisions(runs[variant])
        if d_variant == d_vllm:
            m_variant, m_vllm = masked_records(runs[variant]), masked_records(runs["vllm"])
            check("C values", f"{name}: {variant}: same decisions as vLLM's Scheduler, so the same records once ids "
                  f"and times are masked ({len(m_vllm)} records)", bool(m_vllm) and m_variant == m_vllm,
                  first_diff(m_variant, m_vllm) or "no records")
        else:
            entry.setdefault("first_decision_difference_vs_vllm", {})[variant] = first_diff(d_variant, d_vllm)
            print(f"INFO [C values] {name}: {variant}: decisions differ from vLLM's Scheduler (RecomputeScheduler's "
                  f"own policy), values not compared: {first_diff(d_variant, d_vllm)[:300]}", flush=True)

    for variant in ORIGINALS.values():
        hooks = next(i["hooks"] for i in runs[variant] if "hooks" in i)
        check("E hooks", f"{name}: {variant}: BalanceScheduler.schedule never called (the original's behaviour)",
              hooks.get("BalanceScheduler.schedule", 0) == 0, hooks)

    # A. negative control: the unported RecomputeScheduler on either base
    for variant in ORIGINALS.values():
        neg = {k: v["only_vllm"] for k, v in set_diff(sets["vllm"], sets[variant]).items() if v["only_vllm"]}
        entry.setdefault("original_missing_vs_vllm", {})[variant] = neg
        check("A field sets", f"{name}: negative control, {variant} misses scheduler fields", bool(neg),
              "no difference found")

    # B. behaviour of the ported RecomputeScheduler vs the original, on the same base; and across the two bases
    for new, old in list(ORIGINALS.items()) + [("ported_on_balance", "ported")]:
        d_new, d_old = decisions(runs[new]), decisions(runs[old])
        t_new, t_old = transports(runs[new]), transports(runs[old])
        check("B behaviour", f"{name}: {new} == {old}, every step's decision ({len(d_new)} steps){note}",
              (bool(d_new) or name in RAISES) and d_new == d_old, first_diff(d_new, d_old) or "no steps")
        check("B behaviour", f"{name}: {new} == {old}, outputs to the client ({len(t_new)} batches)",
              t_new == t_old, first_diff(t_new, t_old))

    if name in RAISES:
        for variant in WRAPPED + ("vllm",):
            failed = [i["rec"] for i in runs[variant] if "rec" in i and kind(i["rec"]) == ("core_summary", "execution", None)]
            lookup = [(r.get("stages") or {}).get("lookup") or {} for r in failed]
            check("D error", f"{name}: {variant}: one execution record, status error, stages.lookup status error, "
                  f"error_type RuntimeError", len(failed) == 1 and failed[0].get("status") == "error"
                  and lookup[0].get("status") == "error" and lookup[0].get("error_type") == "RuntimeError"
                  and failed[0].get("execution_id") is not None, failed[:1])
        with open(OUT / f"records-{name}.json", "w") as f:
            json.dump({v: r for v, r in runs.items()}, f, indent=1, sort_keys=True, default=str)
        continue

    # D. step id chain and queue stage
    for variant in WRAPPED + ("vllm",):
        problems, n = chain_problems(runs[variant])
        check("D step id", f"{name}: {variant}: schedule() sets pd_execution_id on every step, unique per step, and "
              f"the step's execution record carries it ({n} steps)", n > 0 and not problems, problems[:3])
        statuses = queue_statuses(runs[variant])
        check("D queue", f"{name}: {variant}: every request record's queue stage measured (status ok)",
              statuses and all(s == "ok" for _, s in statuses), statuses)
    if name == "S5_abort_shutdown":
        # Reference for the S8 note: in vLLM's own Scheduler, requests ended by abort or shutdown lack no_forward too.
        ended = [i["rec"] for i in runs["vllm"] if "rec" in i and kind(i["rec"]) == ("core_summary", "request", None)]
        check("A field sets", f"{name}: vLLM Scheduler: the aborted and the shutdown request records have no "
              f"no_forward either (so only role is new in S8)", len(ended) == 2 and not any("no_forward" in r for r in ended),
              [(r.get("completion"), "no_forward" in r) for r in ended])
    for variant in ORIGINALS.values():
        problems, n = chain_problems(runs[variant])
        entry.setdefault("original_chain_problems", {})[variant] = sorted(set(problems))
        entry.setdefault("original_queue_statuses", {})[variant] = queue_statuses(runs[variant])
    with open(OUT / f"records-{name}.json", "w") as f:
        json.dump({v: r for v, r in runs.items()}, f, indent=1, sort_keys=True, default=str)

OUT.mkdir(parents=True, exist_ok=True)
with open(OUT / "sched-fieldset.json", "w") as f:
    json.dump({"results": RESULTS, "report": REPORT}, f, indent=1, sort_keys=True, default=str)
expected = sum(r["expected"] for r in RESULTS)
passed = sum(r["ok"] and not r["expected"] for r in RESULTS)
failed = sum(not r["ok"] for r in RESULTS)
print(f"\n{passed} passed, {expected} expected differences (contract), {failed} failed", flush=True)
sys.stdout.flush()
os._exit(0 if failed == 0 else 1)
