"""The two PD_DIAG_STARTUP lines on the real paths: a real EngineCore.__init__ and a real
WorkerWrapperBase.execute_model. One scenario per process (vllm-ascend's config is per process, and
each line is printed once per process).

usage: python startup_check.py SCENARIO
  engine_vllm          vLLM's own Scheduler, vllm-ascend not loaded
  engine_balance       P on site: vllm-ascend's balance patch swaps the name, BalanceScheduler (balance off)
  engine_balance_on    same, additional_config enables balance (an operation guide must reject this line)
  engine_recompute     D on site, sync: ported RecomputeScheduler on the BalanceScheduler base, recompute on
  engine_recompute_async  D on site: AsyncRecomputeScheduler (async scheduling)
  worker_cpu           WorkerWrapperBase: init_device builds the runner (first line), then 3 steps, the first
                       without a forward; the runner calls vllm-ascend's pd_device_timing.begin_execute at the
                       start of every execute_model, as model_runner_v1 does (no NPU here: device timing off)
  worker_npu_events    same, with stand-in NPU events (device timing on)
  worker_v2            same as worker_cpu with a runner class from vllm_ascend.worker.v2 (must be rejected)
  worker_no_timing     a runner without vllm-ascend's timing hook: the step without a forward does not count,
                       the line comes after the first step with a forward, device_timing=?
env: T51_ASCEND (vllm-ascend tree, ef5bd17's recompute_scheduler applied), T51_TIMING (pd_device_timing.py)
Prints the captured PD_DIAG_STARTUP lines, one per line, prefixed with "LINE ".
"""
import importlib.abc
import importlib.machinery
import importlib.util
import logging
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

SCENARIO = sys.argv[1]


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

LINES = []


class Capture(logging.Handler):
    def emit(self, record):
        msg = record.getMessage()
        if msg.startswith("PD_DIAG_STARTUP "):
            LINES.append(msg)


def attach():
    for name in ("vllm.v1.engine.core", "vllm.v1.worker.worker_base"):
        lg = logging.getLogger(name)
        lg.addHandler(Capture())
        lg.setLevel(logging.INFO)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ASC = os.environ.get("T51_ASCEND", "/w/ascend")
sys.path.insert(0, os.getcwd())  # vLLM's repo root first: vllm-ascend has its own tests package

import tests.v1.core.utils as core_utils  # noqa: E402
import vllm.v1.core.sched.scheduler as sched_mod  # noqa: E402
import vllm.v1.engine.core as core_mod  # noqa: E402

attach()

if SCENARIO.startswith("engine_"):
    sched = core_utils.create_scheduler(async_scheduling=SCENARIO.endswith("_async"))
    vllm_config, kv_cache_config = sched.vllm_config, sched.kv_cache_config
    if SCENARIO != "engine_vllm":
        sched_cfg = {}
        if SCENARIO == "engine_balance_on":
            sched_cfg["enable_balance_scheduling"] = True
        if SCENARIO.startswith("engine_recompute"):
            sched_cfg["recompute_scheduler_enable"] = True
        vllm_config.additional_config = {"scheduler_config": sched_cfg} if sched_cfg else {}
        # As in a vllm-ascend process: the balance patch replaces the name at import.
        balance = load(f"{ASC}/vllm_ascend/patch/platform/patch_balance_schedule.py",
                       "vllm_ascend.patch.platform.patch_balance_schedule")
        assert sched_mod.Scheduler is balance.BalanceScheduler
        if SCENARIO.startswith("engine_recompute"):
            rs = load(f"{ASC}/vllm_ascend/core/recompute_scheduler.py", "vllm_ascend.core.recompute_scheduler")
            vllm_config.scheduler_config.scheduler_cls = (
                rs.AsyncRecomputeScheduler if SCENARIO.endswith("_async") else rs.RecomputeScheduler
            )

    class FakeExecutor:
        def __init__(self, vllm_config):
            self.vllm_config = vllm_config

        def register_failure_callback(self, cb):
            pass

        def shutdown(self):
            pass

    core_mod.EngineCore._initialize_kv_caches = lambda self, cfg: kv_cache_config
    core = core_mod.EngineCore(vllm_config, FakeExecutor, log_stats=False)
    print("built", type(core.scheduler).__module__ + "." + type(core.scheduler).__qualname__, flush=True)
    # once per process: a second construction does not print again
    core_mod.EngineCore(vllm_config, FakeExecutor, log_stats=False)
else:
    import torch
    from vllm.config import set_current_vllm_config
    from vllm.distributed import ensure_model_parallel_initialized, init_distributed_environment
    from vllm.v1.core.sched.output import SchedulerOutput
    from vllm.v1.worker.worker_base import WorkerWrapperBase

    vllm_config = core_utils.create_scheduler().vllm_config
    with set_current_vllm_config(vllm_config):
        init_distributed_environment(world_size=1, rank=0, distributed_init_method="tcp://127.0.0.1:29571",
                                     local_rank=0, backend="gloo")
        ensure_model_parallel_initialized(1, 1)
    timing = load(os.environ["T51_TIMING"], "vllm_ascend.worker.pd_device_timing")
    if SCENARIO == "worker_npu_events":
        class _Event:
            def __init__(self, enable_timing=False):
                pass

            def record(self, stream=None):
                pass

            def query(self):
                return True

            def elapsed_time(self, other):
                return 0.0

        torch.npu = SimpleNamespace(Event=_Event, current_stream=lambda: None,
                                    is_current_stream_capturing=lambda: False)

    class NPUModelRunner:
        def execute_model(self, scheduler_output):
            if SCENARIO != "worker_no_timing":
                timing.begin_execute(self)
            return None

    NPUModelRunner.__module__ = ("vllm_ascend.worker.v2.model_runner" if SCENARIO == "worker_v2"
                                 else "vllm_ascend.worker.model_runner_v1")
    worker = SimpleNamespace(parallel_config=vllm_config.parallel_config)

    def init_device():
        worker.model_runner = NPUModelRunner()
        worker.execute_model = worker.model_runner.execute_model

    worker.init_device = init_device
    wrapper = WorkerWrapperBase(rpc_rank=0)
    wrapper.worker = worker
    wrapper.vllm_config = vllm_config
    wrapper.mm_receiver_cache = None
    wrapper.init_device()
    print("after init_device:", len(LINES), "line(s)", flush=True)
    for step in range(3):
        out = SchedulerOutput.make_empty()
        out.pd_execution_id = f"execution-startup-check-{step}"
        if step > 0:  # the first step runs no forward
            out.num_scheduled_tokens = {"req-0": 4}
            out.total_num_scheduled_tokens = 4
        wrapper.execute_model(out)
        print(f"after step {step}:", len(LINES), "line(s)", flush=True)
    print("timing", getattr(getattr(worker.model_runner, "_pd_device_timing", None), "enabled", None), flush=True)

for line in LINES:
    print("LINE", line, flush=True)
print("lines", len(LINES), flush=True)
