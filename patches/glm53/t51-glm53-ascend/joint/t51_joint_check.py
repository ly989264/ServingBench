"""Joint check of the vLLM v0.30.0 port (patch 01) and the vllm-ascend d4ad49796 port (patch 02 + T51 additions).

Run inside the container (see run_joint_check.sh) with both patched trees on PYTHONPATH:
    python t51_joint_check.py OUT_DIR
Three parts, each reported PASS/FAIL per item:
  1. imports: the vLLM modules patch 02 depends on, and every vllm-ascend module the port changed;
  2. names: every attribute the vllm-ascend port uses on vllm.v1.metrics.pd_diagnostics, the worker_base
     decorators, SchedulerOutput.pd_execution_id, and their signatures against patch 01's reference;
  3. call chain: a P step (load_async=false) and a D step (load_async=true) through the real vLLM pieces
     (pd_execution / pd_sample, KV connector mixin, MultiConnector) and the real vllm-ascend pieces
     (AscendMultiConnector, AscendStoreConnector, KVPoolWorker, KVCacheStore send/receive threads,
     MooncakeBackend, KVPoolScheduler._emit_load_links); the records are checked against the glm5.3
     contract. torch_npu, NPU events and the Mooncake store object are stubs. The runner is a minimal class
     that calls these pieces in the order model_runner_v1.NPUModelRunner.execute_model does
     (handle_preemptions, then the KV connector context around the forward); that order is checked from the
     runner's source in part 1.
Parts 3 and 5 start with a hard gate: a worker_summary or core_summary[execution] record whose execution_id is null
fails the part, and the part's other checks are not run. JOINT_NULL_EXECUTION_IDS=1 drops the steps'
pd_execution_id, as the negative control of that gate. JOINT_RECOMPUTE_BASE=balance runs D's
AsyncRecomputeScheduler with BalanceScheduler as its base (see below).
"""

from __future__ import annotations

import ast
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import inspect
import json
import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/out")
ASCEND_ROOT = Path(os.environ.get("ASCEND_ROOT", "/ascend"))
REFERENCE_DIAG = Path(os.environ.get("REFERENCE_DIAG", "/reference/pd_diagnostics.py"))
RESULTS: list[dict] = []
RECORDS: list[dict] = []
# Negative control for the execution_id gate: the steps of parts 3 and 5 carry no pd_execution_id.
NULL_EXECUTION_IDS = os.environ.get("JOINT_NULL_EXECUTION_IDS") == "1"


def diag_level() -> int:
    """The granularity this run is at, from the same place the code under test reads it. The full level records
    everything; below it the native call list is not kept; at the lowest one the Pool task stages are not kept
    either. Checks that assert what a level leaves out say so instead."""
    try:
        from vllm.v1.metrics import pd_diagnostics as diag

        read = getattr(diag, "diag_level", None)
        # It answers with (level, source); the source is for the line the vLLM side prints, not for here.
        answer = 3 if read is None else read()
        value = getattr(answer, "level", answer)
        if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 3:
            return value
    except Exception:
        pass
    return 3


def check(part, item, ok, detail=""):
    RESULTS.append({"part": part, "item": item, "ok": bool(ok), "detail": str(detail)[:400]})
    print(("PASS " if ok else "FAIL ") + f"[{part}] {item}" + (f"  -- {detail}" if detail and not ok else ""))
    return ok


# ---------------------------------------------------------------- stubs (NPU-only packages)
STUB_ROOTS = [name for name in os.environ.get("JOINT_STUBS", "torch_npu,torchair,mooncake,acl").split(",") if name]
STUB_ROOTS = [name for name in STUB_ROOTS if importlib.util.find_spec(name) is None]


class _StubLoader(importlib.abc.Loader):
    def create_module(self, spec):
        module = MagicMock(name=spec.name)
        module.__spec__ = spec
        module.__path__ = []
        module.__name__ = spec.name
        module.__version__ = "0.0.0"  # some importers read it
        return module

    def exec_module(self, module):
        pass


class _StubFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in STUB_ROOTS:
            return importlib.machinery.ModuleSpec(name, _StubLoader(), is_package=True)
        return None


sys.meta_path.insert(0, _StubFinder())

import torch  # noqa: E402  (real CPU torch)


class ScriptDevice:
    """Scripted NPU timeline for the stub events: the check advances device time and decides completion."""

    def __init__(self):
        self.now_ms = 0.0
        self.count = 0
        self.completed = 0
        self.held = set()
        self.lock = threading.Lock()

    def advance(self, ms):
        self.now_ms += ms

    def complete_all(self):
        self.completed = self.count


DEVICE = ScriptDevice()
COMPUTE_STREAM = object()


class FakeNpuEvent:
    def __init__(self, *args, **kwargs):
        self.index = None
        self.ts = None

    def record(self, *args, **kwargs):
        with DEVICE.lock:
            DEVICE.count += 1
            self.index = DEVICE.count
        self.ts = DEVICE.now_ms

    def query(self):
        return self.index is not None and self.index <= DEVICE.completed and self.index not in DEVICE.held

    def elapsed_time(self, other):
        return other.ts - self.ts

    def synchronize(self):
        pass

    def wait(self, *args, **kwargs):
        pass


if not hasattr(torch, "npu"):
    torch.npu = MagicMock(name="torch.npu")
torch.npu.Event = FakeNpuEvent
torch.npu.current_stream = lambda *args, **kwargs: COMPUTE_STREAM
torch.npu.is_current_stream_capturing = lambda: False


# ---------------------------------------------------------------- part 1: imports
VLLM_MODULES = [
    "vllm",
    "vllm.v1.metrics.pd_diagnostics",
    "vllm.v1.worker.worker_base",
    "vllm.v1.core.sched.output",
    "vllm.v1.worker.kv_connector_model_runner_mixin",
    "vllm.distributed.kv_transfer.kv_connector.v1.multi_connector",
]
ASCEND_MODULES = [
    "vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.diagnostics",
    "vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.metadata",
    "vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.kv_transfer",
    "vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.pool_worker",
    "vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.pool_scheduler",
    "vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.backend.mooncake_backend",
    "vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.backend.mooncake_layerwise",
    "vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.ascend_store_connector",
    "vllm_ascend.distributed.kv_transfer.ascend_multi_connector",
    "vllm_ascend.worker.model_runner_v1",
    "vllm_ascend.worker.v2.model_runner",
    "vllm_ascend.core.recompute_scheduler",
    # the P/D read-KV port (t51-glm53-p2d), when its patch is applied to the same tree
    "vllm_ascend.distributed.kv_transfer.kv_p2p.mooncake_connector",
]
# scipy is not in the image. Only the runners' import graph needs it (vllm_ascend/attention/indexer.py and
# others); numba, imported earlier by the pool modules, rejects a stub scipy, so the stub goes in last.
LATE_STUBS = {"vllm_ascend.worker.model_runner_v1": ["scipy"]}
# D's RecomputeScheduler on either base: in a vllm-ascend process, patch_balance_schedule replaces the name
# vllm.v1.core.sched.scheduler.Scheduler with BalanceScheduler (patch_balance_schedule.py:897); a recompute_scheduler
# imported after that derives from BalanceScheduler. JOINT_RECOMPUTE_BASE=balance loads that module (from its file, not
# the rest of vllm_ascend.patch.platform) before recompute_scheduler is imported.
RECOMPUTE_BASE = os.environ.get("JOINT_RECOMPUTE_BASE", "vllm")
if RECOMPUTE_BASE == "balance":
    _balance_spec = importlib.util.spec_from_file_location(
        "t51_patch_balance_schedule", ASCEND_ROOT / "vllm_ascend/patch/platform/patch_balance_schedule.py")
    _balance = importlib.util.module_from_spec(_balance_spec)
    sys.modules[_balance_spec.name] = _balance
    _balance_spec.loader.exec_module(_balance)
else:  # imported before anything can replace the name, so its base is vLLM's own Scheduler
    importlib.import_module("vllm_ascend.core.recompute_scheduler")
import vllm.v1.core.sched.scheduler as _sched_mod  # noqa: E402

_VLLM_SCHEDULER = next(c for c in _sched_mod.Scheduler.__mro__
                       if c.__module__ == "vllm.v1.core.sched.scheduler" and c.__qualname__ == "Scheduler")
SCHEDULER_NAME_REPLACED_BY = [None]
MODULES = {}
for name in VLLM_MODULES + ASCEND_MODULES:
    for late in LATE_STUBS.get(name, []):
        if importlib.util.find_spec(late) is None:
            STUB_ROOTS.append(late)
    try:
        MODULES[name] = importlib.import_module(name)
        check("imports", name, True)
        if SCHEDULER_NAME_REPLACED_BY[0] is None and _sched_mod.Scheduler is not _VLLM_SCHEDULER:
            SCHEDULER_NAME_REPLACED_BY[0] = name
            print(f"INFO vllm.v1.core.sched.scheduler.Scheduler is {_sched_mod.Scheduler.__qualname__} after importing "
                  f"{name}", flush=True)
    except BaseException as exc:  # report every failing import, keep going
        check("imports", name, False, f"{type(exc).__name__}: {exc}")
check("imports", "stubbed NPU-only packages", True, ",".join(STUB_ROOTS))
LEVEL = diag_level()
NATIVE_SAYS = "recorded" if LEVEL >= 3 else "not recorded at this level"
check("level", f"granularity of this run: {LEVEL}", True,
      "everything" if LEVEL >= 3 else ("no native call list" if LEVEL == 2 else "no native call list, no Pool stages"))


def runner_order_check():
    """execute_model in model_runner_v1 calls handle_preemptions, then enters the KV connector context
    that wraps _model_forward (the order the minimal runner below reproduces)."""
    path = ASCEND_ROOT / "vllm_ascend/worker/model_runner_v1.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "NPUModelRunner")
    execute = next(f for f in cls.body if isinstance(f, ast.FunctionDef) and f.name == "execute_model")
    preempt_line = None
    context_line = None
    forward_inside = False
    for node in ast.walk(execute):
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "get_kv_transfer_group().handle_preemptions":
            preempt_line = preempt_line or node.lineno
        if isinstance(node, ast.With):
            for item in node.items:
                if "maybe_get_kv_connector_output" in ast.unparse(item.context_expr):
                    context_line = context_line or node.lineno
                    forward_inside = forward_inside or any(
                        isinstance(sub, ast.Call) and ast.unparse(sub.func) == "self._model_forward"
                        for sub in ast.walk(node)
                    )
    check(
        "imports",
        "model_runner_v1.execute_model: handle_preemptions before the KV connector context, forward inside it",
        preempt_line is not None and context_line is not None and preempt_line < context_line and forward_inside,
        f"handle_preemptions line {preempt_line}, context line {context_line}, forward inside {forward_inside}",
    )
    decorators = [ast.unparse(d.func if isinstance(d, ast.Call) else d) for d in execute.decorator_list]
    check("imports", "model_runner_v1.execute_model is decorated with pd_execution", "pd_execution" in decorators,
          ",".join(decorators))


runner_order_check()

# ---------------------------------------------------------------- part 2: names and signatures
diag = MODULES.get("vllm.v1.metrics.pd_diagnostics")


def used_diag_names():
    """Attributes the vllm-ascend port reads on the vLLM diagnostics module, found by AST."""
    names = set()
    batch_methods = set()
    for path in ASCEND_ROOT.glob("vllm_ascend/**/*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "pd_diagnostics as diag" not in text and "pool_diag" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "diag":
                names.add(node.attr)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in (
                "batch",
                "link",
            ):
                batch_methods.add(node.attr)
    return names, batch_methods


if diag is not None:
    names, batch_methods = used_diag_names()
    reference = None
    if REFERENCE_DIAG.exists():
        spec = importlib.util.spec_from_file_location("reference_pd_diagnostics", REFERENCE_DIAG)
        reference = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reference)
    for name in sorted(names):
        present = hasattr(diag, name)
        detail = ""
        same = True
        if present and reference is not None and callable(getattr(diag, name)) and hasattr(reference, name):
            try:
                ours, theirs = inspect.signature(getattr(diag, name)), inspect.signature(getattr(reference, name))
                same = str(ours) == str(theirs)
                detail = f"port {ours} vs patch 01 {theirs}"
            except (TypeError, ValueError):
                pass
        check("names", f"pd_diagnostics.{name}", present and same, detail if not same else "")
    for method in sorted(batch_methods & {"add_stage", "finish", "update"}):
        ours = inspect.signature(getattr(diag.Batch, method))
        theirs = inspect.signature(getattr(reference.Batch, method)) if reference is not None else ours
        check("names", f"pd_diagnostics.Batch.{method}{ours}", str(ours) == str(theirs), f"patch 01 {theirs}")
    check("names", "pd_diagnostics logger name", diag._LOG.name == "vllm.v1.metrics.pd_diagnostics", diag._LOG.name)
    for event, stage in (("runner.forward", "forward"), ("runner.sampling", "sampling"), ("kv.load_submit", "submit"),
                         ("kv.save_wait", "wait_for_save")):
        check("names", f"span {event} -> stages.{stage}", diag._SPANS.get(event) == stage, diag._SPANS.get(event))
    if reference is not None:
        check("names", "pd_diagnostics.py identical to patch 01",
              Path(diag.__file__).read_bytes() == REFERENCE_DIAG.read_bytes(), diag.__file__)

worker_base = MODULES.get("vllm.v1.worker.worker_base")
if worker_base is not None:
    for name in ("pd_execution", "pd_sample", "pd_execution_context"):
        ok = hasattr(worker_base, name)
        check("names", f"worker_base.{name}{inspect.signature(getattr(worker_base, name)) if ok else ''}", ok)
sched_output = MODULES.get("vllm.v1.core.sched.output")
if sched_output is not None:
    fields = getattr(sched_output.SchedulerOutput, "__dataclass_fields__", {})
    field = fields.get("pd_execution_id")
    check("names", "SchedulerOutput.pd_execution_id (default None)", field is not None and field.default is None)


# ---------------------------------------------------------------- D's scheduler: AsyncRecomputeScheduler
D_SCHEDULER = {}


def recompute_scheduler_output(request_ids, num_tokens=32):
    """One step of D's scheduler as the site runs it (recompute_scheduler_enable + --async-scheduling):
    vllm_ascend's AsyncRecomputeScheduler, built by vLLM's own test helper (facebook/opt-125m config, offline), with
    the given requests waiting. Returns its real SchedulerOutput; pd_execution_id is whatever the scheduler set."""
    import functools

    from tests.v1.core import utils as core_utils
    from vllm.config import DeviceConfig, VllmConfig

    from vllm_ascend.core import recompute_scheduler as rs

    saved = core_utils.AsyncScheduler, core_utils.VllmConfig
    core_utils.AsyncScheduler = rs.AsyncRecomputeScheduler
    core_utils.VllmConfig = functools.partial(VllmConfig, device_config=DeviceConfig(device="cpu"))
    try:
        scheduler = core_utils.create_scheduler(async_scheduling=True, block_size=16)
    finally:
        core_utils.AsyncScheduler, core_utils.VllmConfig = saved
    D_SCHEDULER["class"] = type(scheduler).__qualname__
    D_SCHEDULER["base"] = rs.RecomputeScheduler.__bases__[0].__qualname__
    for request_id, request in zip(request_ids, core_utils.create_requests(num_requests=len(request_ids),
                                                                          num_tokens=num_tokens)):
        request.request_id = request_id
        scheduler.add_request(request)
    output = scheduler.schedule()
    if NULL_EXECUTION_IDS:
        output.pd_execution_id = None
    return output


def check_scheduler_record(part, records, output, request_ids):
    """The step was scheduled by the ported AsyncRecomputeScheduler: its own execution record has the output's id."""
    check(part, f"D's step scheduled by vllm_ascend AsyncRecomputeScheduler, base {RECOMPUTE_BASE}",
          D_SCHEDULER.get("class") == "AsyncRecomputeScheduler" and D_SCHEDULER.get("base") ==
          ("BalanceScheduler" if RECOMPUTE_BASE == "balance" else "Scheduler"), D_SCHEDULER)
    executions = find(records, "core_summary", scope="execution")
    check(part, "the scheduler's core_summary[execution] record: execution_id == SchedulerOutput.pd_execution_id, "
          "stages.schedule and members written for the step's requests",
          len(executions) == 1 and executions[0]["execution_id"] == output.pd_execution_id
          and "schedule" in executions[0]["stages"] and executions[0]["request_ids"] == request_ids
          and [m["request_id"] for m in executions[0].get("members", [])] == request_ids,
          [(r.get("execution_id"), r.get("request_ids"), sorted(r.get("stages", {}))) for r in executions])


# ---------------------------------------------------------------- part 3: call chain
class Collect(logging.Handler):
    def emit(self, record):
        message = record.getMessage()
        if "[PD_DIAG] " in message:
            RECORDS.append(json.loads(message.split("[PD_DIAG] ", 1)[1]))


def run_chain():
    import vllm.distributed as vdist
    from vllm.distributed.kv_transfer import kv_transfer_state
    from vllm.distributed.kv_transfer.kv_connector.v1.base import KVConnectorBase_V1
    from vllm.distributed.kv_transfer.kv_connector.v1.multi_connector import MultiKVConnectorMetadata
    from vllm.distributed.kv_transfer.kv_connector.v1.metrics import KVConnectorStats  # noqa: F401
    from vllm.v1.core.sched.output import SchedulerOutput
    from vllm.v1.worker import kv_connector_model_runner_mixin as mixin
    from vllm.v1.worker.worker_base import pd_execution, pd_sample

    from vllm_ascend.distributed.kv_transfer.ascend_multi_connector import AscendMultiConnector
    from vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.ascend_store_connector import AscendStoreConnector
    from vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.backend import mooncake_backend as mb
    from vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.kv_transfer import (
        KVCacheStoreRecvingThread,
        KVCacheStoreSendingThread,
    )
    from vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.metadata import (
        AscendConnectorMetadata,
        ChunkedTokenDatabase,
        LoadSpec,
        ReqMeta,
    )
    from vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.pool_scheduler import KVPoolScheduler
    from vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.pool_worker import KVPoolWorker

    diag._LOG.addHandler(Collect())
    diag._LOG.setLevel(logging.INFO)
    diag._LOG.propagate = False  # keep the records out of the console; they are written to OUT
    vdist.get_tp_group = lambda: SimpleNamespace(rank_in_group=0)
    vdist.get_pp_group = lambda: SimpleNamespace(rank_in_group=0)
    mixin.get_forward_context = lambda: SimpleNamespace(name="forward-context")

    class TokenDatabase(ChunkedTokenDatabase):
        """Real (timing-wrapped) prepare_value / process_token_key_strings_with_block_ids."""

        def __init__(self):  # noqa: super-init-not-called
            self.group_block_len = {0: [8]}
            self.group_kv_caches_base_addr = {0: [1000]}
            self.group_block_stride = {}
            self.group_num_layers = {"kv": {0: 1}}
            self.partitions = None
            self.hash_block_size = 16

        def store_mask(self, token_len, num_prompt_tokens):
            return None

        def load_mask(self, block_hashes, token_len):
            return None

        def mask_allows_chunk(self, masks, group_id, start):
            return True

        def _get_key_prefix(self, kv_cache_group_id=0):
            return "model@"

        def _iter_token_chunks(self, token_len, block_hashes, mask_num=0, kv_cache_group_id=0, *args, **kwargs):
            block_ids = kwargs.get("block_ids") or [0, 0]
            for index in range(min(2, token_len // 16)):
                if index * 16 >= mask_num:
                    yield index * 16, (index + 1) * 16, f"h{index}", block_ids[index]

        def get_block_size(self, kv_cache_group_id=0):
            return 16

    class Native:
        """Mooncake store object: put takes 30 ms, get 5 ms."""

        def batch_is_exist(self, keys):
            return [0] * len(keys)

        def batch_put_from_multi_buffers(self, keys, addrs, sizes, config):
            time.sleep(0.030)
            return [sum(size) for size in sizes]

        def batch_get_into_multi_buffers(self, keys, addrs, sizes):
            time.sleep(0.005)
            return [sum(size) for size in sizes]

    def backend():
        value = object.__new__(mb.MooncakeBackend)
        value.store = Native()
        value._lazy_init = False
        value._store_initialized = True
        value.ensure_initialized = lambda: None
        value._build_replicate_config = lambda: None
        value.set_device = lambda: None
        return value

    class P2PStub(KVConnectorBase_V1):
        """Stands in for MooncakeConnectorV1 (another worker's port); does nothing."""

        def __init__(self):  # noqa: super-init-not-called
            self._connector_metadata = None

        def start_load_kv(self, forward_context, **kwargs):
            pass

        def wait_for_layer_load(self, layer_name):
            pass

        def save_kv_layer(self, *args, **kwargs):
            pass

        def wait_for_save(self):
            pass

        def handle_preemptions(self, kv_connector_metadata):
            pass

        def get_finished(self, finished_req_ids):
            return set(), set()

        def get_num_new_matched_tokens(self, *args):
            return 0, False

        def update_state_after_alloc(self, *args):
            pass

        def build_connector_meta(self, *args):
            return None

        def get_kv_connector_stats(self):
            return None

        def get_kv_connector_kv_cache_events(self):
            return None

        def build_connector_worker_meta(self):
            return None

    def store_connector(load_async):
        database = TokenDatabase()
        worker = KVPoolWorker.__new__(KVPoolWorker)
        worker.tp_rank = 0
        worker.use_layerwise = False
        worker.load_async = load_async
        worker.tp_mismatch = False
        worker.cache_transfer_granularity = 16
        worker.group_uses_align_state = []
        worker.token_database = database
        worker.grouped_block_size = [16]
        worker.m_store = backend()
        worker._invalid_block_ids = set()
        worker._invalid_block_ids_lock = threading.Lock()
        worker._record_kv_connector_operation = lambda *args: None
        # Bookkeeping outside the measured chain:
        worker.get_finished = lambda finished, metadata: (set(), set())
        worker.ensure_store_initialized = lambda: None
        worker.get_stats = lambda: None
        worker.get_kv_events = lambda: []
        worker.build_connector_worker_meta = lambda: None
        worker.kv_send_thread = KVCacheStoreSendingThread(worker.m_store, database, 16, 0, 1)
        worker.kv_send_thread.start()
        worker.kv_recv_thread = None
        if load_async:
            worker.kv_recv_thread = KVCacheStoreRecvingThread(worker.m_store, database, 16, 0, 1)
            worker.kv_recv_thread.start()
        connector = AscendStoreConnector.__new__(AscendStoreConnector)
        connector.connector_worker = worker
        connector.connector_scheduler = None
        connector.use_layerwise = False
        connector.kv_role = "kv_both"
        connector.consumer_is_to_put = False
        connector._current_step_has_real_forward = False
        connector._mamba_copy_bufs = None
        connector._connector_metadata = None
        multi = AscendMultiConnector.__new__(AscendMultiConnector)
        multi._connectors = [P2PStub(), connector]
        multi._extra_async_saves = {}
        multi._connector_metadata = None
        kv_transfer_state._KV_CONNECTOR_AGENT = multi
        return worker

    class Runner(mixin.KVConnectorModelRunnerMixin):
        """Calls the connector pieces in model_runner_v1.execute_model's order."""

        def __init__(self):
            self.parallel_config = SimpleNamespace(data_parallel_index=0, data_parallel_rank=0)

        @pd_execution
        def execute_model(self, scheduler_output, intermediate_tensors=None):
            from vllm.distributed.kv_transfer import get_kv_transfer_group

            if scheduler_output.kv_connector_metadata is not None:
                get_kv_transfer_group().handle_preemptions(scheduler_output.kv_connector_metadata)
            with self.maybe_get_kv_connector_output(scheduler_output) as output:
                with diag.span("runner.forward", completion="host_return"):
                    time.sleep(0.004)
            return output

        @pd_sample
        def sample_tokens(self, grammar_output):
            with diag.span("runner.sampling", algorithm="sampler", completion="host_return"):
                time.sleep(0.002)

    def request(req_id, *, load=False, save=True, tokens=32):
        spec = LoadSpec(vllm_cached_tokens=0, kvpool_cached_tokens=tokens, can_load=True) if load else None
        return ReqMeta(req_id=req_id, token_len_chunk=tokens, block_ids=[1, 2], block_hashes=["h0", "h1"],
                       can_save=save, load_spec=spec, is_last_chunk=True)

    def step(runner, execution_id, requests, released=(), sync_loads=False, output=None):
        if output is None:  # P: a hand-built step
            output = SchedulerOutput.make_empty()
            output.num_scheduled_tokens = {item.req_id: item.token_len_chunk for item in requests}
            output.total_num_scheduled_tokens = sum(output.num_scheduled_tokens.values())
            output.pd_execution_id = None if NULL_EXECUTION_IDS else execution_id
        # as KVPoolScheduler.build_connector_meta takes it (pool_scheduler.py, getattr on the scheduler output)
        meta = AscendConnectorMetadata(set(), set(), pd_execution_id=getattr(output, "pd_execution_id", None))
        for item in requests:
            meta.add_request(item)
        meta.released_req_ids = set(released)
        output.has_sync_kv_loads = sync_loads
        output.kv_connector_metadata = MultiKVConnectorMetadata(metadata=(None, meta))
        runner.execute_model(output)
        runner.sample_tokens(None)
        return meta, output

    def drain(*threads):
        for thread in threads:
            if thread is None:
                continue
            deadline = time.monotonic() + 5
            while thread.request_queue.unfinished_tasks and time.monotonic() < deadline:
                time.sleep(0.005)

    runner = Runner()
    # P: kv_both, load_async=false. Step 1 queues R0's save; step 2 releases R0 and sync-loads R1.
    worker = store_connector(load_async=False)
    step(runner, "exec-p1", [request("P-R0")])
    step(runner, "exec-p2", [request("P-R1", load=True)], released=["P-R0"], sync_loads=True)
    drain(worker.kv_send_thread)
    p_records = list(RECORDS)
    # D: kv_both, load_async=true. One request with a pool hit, loaded in the background.
    RECORDS.clear()
    worker = store_connector(load_async=True)
    d_output = recompute_scheduler_output(["D-R2"])
    D_SCHEDULER["chain_output"] = d_output
    meta, output = step(runner, d_output.pd_execution_id, [request("D-R2", load=True)], sync_loads=False,
                        output=d_output)
    drain(worker.kv_send_thread, worker.kv_recv_thread)
    scheduler = object.__new__(KVPoolScheduler)
    scheduler.load_async = True
    scheduler.use_layerwise = False
    scheduler.vllm_config = SimpleNamespace(
        kv_transfer_config=SimpleNamespace(engine_id="1"), parallel_config=SimpleNamespace(data_parallel_rank=0)
    )
    scheduler._emit_load_links(meta, output)
    d_records = list(RECORDS)
    return p_records, d_records


COMMON = {"event", "version", "host", "pid", "native_tid", "run_id", "timestamp_ns", "record_id"}
STAGE_KEYS = {"calls", "duration_ns", "start_ns", "end_ns", "status", "exceptions", "measured_calls", "skipped_calls",
              "unmeasured_calls"}
LOAD_STATUSES = {"succeeded", "backend_failure", "unknown_result", "all_hit", "no_task", "skipped",
                 "missing_load_spec", "exception", "not_processed"}


def find(records, event, **where):
    return [r for r in records if r["event"] == event and all(r.get(k) == v for k, v in where.items())]


def check_common(part, records):
    bad = [r for r in records if not COMMON <= set(r) or r["version"] != 2]
    check(part, f"every record carries the common fields and version 2 ({len(records)} records)", not bad, bad[:1])


GATED = (("worker_summary", None), ("core_summary", "execution"))


def gate_execution_ids(part, records):
    """Hard gate: a step record without execution_id fails the part, and its other checks are not run.

    A null id is not a missing value downstream: the filter falls back to record_id, so the step's stages land in the
    wrong place (D's first step reads 0, the rest of the request is counted from the second step) with no visible gap.
    """
    gated = [r for r in records if any(r["event"] == event and (scope is None or r.get("scope") == scope)
                                       for event, scope in GATED)]
    null = [(r["event"], r.get("scope"), r.get("phase")) for r in gated if r.get("execution_id") is None]
    return check(part, f"GATE: every worker_summary and core_summary[execution] record has an execution_id "
                       f"({len(gated)} records)", gated and not null,
                 f"{len(null)} with execution_id null: {null[:4]}; remaining checks of this part not run" if gated
                 else "no step records")


def check_step_record(part, step, execution_id, request_ids, expect_load_submit):
    check(part, f"{execution_id}: worker_summary[execute] identity",
          step["execution_id"] == execution_id and step["request_ids"] == request_ids and step["tp_rank"] == 0
          and step["pp_rank"] == 0 and step["dp_rank"] == 0 and step["runner"].endswith("Runner"),
          {k: step.get(k) for k in ("execution_id", "request_ids", "tp_rank", "pp_rank", "dp_rank", "runner")})
    for name, stage in step["stages"].items():
        check(part, f"{execution_id}: stages.{name} has the stage keys", STAGE_KEYS <= set(stage), sorted(stage))
    wait = step["stages"].get("wait_for_put")
    check(part, f"{execution_id}: stages.wait_for_put written (contract 1)", wait is not None)
    if wait is not None:
        check(part, f"{execution_id}: wait_for_put values",
              wait["calls"] == 1 and wait["status"] == "ok" and wait["exceptions"] == 0 and wait["measured_calls"] == 1
              and wait["skipped_calls"] == 0 and wait["unmeasured_calls"] == 0
              and wait["timing_scope"] == "released_request_save_fence", wait)
    # stages.submit comes from the vLLM side's kv.load_submit span, which it leaves out on a decode node at
    # level 1. This harness decides that before the chain runs and without a decode node's config, so at level 1
    # the D step is not held to either answer here; what the vLLM side decides is driven in "kv load submit".
    submit_undecided = part.startswith("chain D") and LEVEL == 1
    check(part, f"{execution_id}: stages.forward written" + ("" if submit_undecided else " and stages.submit"),
          "forward" in step["stages"] and (submit_undecided or "submit" in step["stages"]), sorted(step["stages"]))
    return wait


def check_chain(p_records, d_records):
    part = "chain P"
    check_common(part, p_records)
    if gate_execution_ids(part, p_records):
        check_chain_p(part, p_records)
    part = "chain D"
    check_common(part, d_records)
    if gate_execution_ids(part, d_records):
        check_chain_d(part, d_records)


def check_chain_p(part, p_records):
    steps = {r["execution_id"]: r for r in find(p_records, "worker_summary", phase="execute")}
    samples = {r["execution_id"]: r for r in find(p_records, "worker_summary", phase="sample")}
    check(part, "one worker_summary[execute] and one [sample] per step",
          set(steps) == set(samples) == {"exec-p1", "exec-p2"}, (sorted(steps), sorted(samples)))
    for execution_id in samples:
        check(part, f"{execution_id}: sampling in phase=sample with the step's execution_id",
              "sampling" in samples[execution_id]["stages"], sorted(samples[execution_id]["stages"]))
    wait1 = check_step_record(part, steps["exec-p1"], "exec-p1", ["P-R0"], False)
    wait2 = check_step_record(part, steps["exec-p2"], "exec-p2", ["P-R1"], False)
    if wait1 and wait2:
        check(part, "exec-p1: nothing released, released_requests=0", wait1["released_requests"] == 0, wait1)
        check(part, "exec-p2: waits for P-R0's in-flight 30 ms put, released_requests=1",
              wait2["released_requests"] == 1 and wait2["duration_ns"] >= 10_000_000, wait2["duration_ns"])
        released_save = [r for r in find(p_records, "pool_task", operation="save") if r["request_id"] == "P-R0"]
        check(part, "exec-p2: wait_for_put returns only after P-R0's save task finished",
              released_save and released_save[0]["end_ns"] <= wait2["end_ns"],
              (released_save[0]["end_ns"] if released_save else None, wait2["end_ns"]))
    loads = find(p_records, "pool_task", operation="load")
    by_request = {r["request_id"]: r for r in loads}
    check(part, "one pool_task[load] per request in each step (contract 2.1)",
          sorted(by_request) == ["P-R0", "P-R1"] and len(loads) == 2, [(r["request_id"], r["status"]) for r in loads])
    check(part, "no load_submit on P (contract 2.1)", not find(p_records, "pool_task", operation="load_submit"))
    check(part, "no pool_load link on P (contract 3.3)", not find(p_records, "pd_transfer_summary", scope="link"))
    for load in loads:
        check(part, f"{load['request_id']}: sync load fields",
              load["scope"] == "task" and load["member_count"] == 1 and load["queue_enter_ns"] is None
              and "queue_wait_ns" not in load["counters"] and "queue_entries" not in load["counters"]
              and load["tp_rank"] == 0 and load["status"] in LOAD_STATUSES,
              {k: load.get(k) for k in ("scope", "member_count", "queue_enter_ns", "tp_rank", "status")})
    if "P-R0" in by_request:
        check(part, "P-R0 (no pool hit): status missing_load_spec, execution exec-p1",
              by_request["P-R0"]["status"] == "missing_load_spec" and by_request["P-R0"]["execution_id"] == "exec-p1")
    if "P-R1" in by_request:
        load = by_request["P-R1"]
        submit = steps["exec-p2"]["stages"].get("submit", {})
        forward = steps["exec-p2"]["stages"].get("forward", {})
        native = [c["operation"] for c in load["native_calls"]]
        wanted = ["mooncake_get"] if LEVEL >= 3 else []
        check(part, f"P-R1: succeeded, execution exec-p2, backend_get inside, Mooncake get {NATIVE_SAYS}",
              load["status"] == "succeeded" and load["execution_id"] == "exec-p2"
              and load["associated_stages"].get("backend_get", {}).get("calls") == 1
              and native == wanted, (load["status"], native))
        check(part, "P-R1: the load lies inside the step's stages.submit, before the forward (C11)",
              submit and submit["start_ns"] <= load["start_ns"] and load["end_ns"] <= submit["end_ns"]
              and submit["end_ns"] <= forward.get("start_ns", 0), (submit.get("start_ns"), load["start_ns"]))
        if wait2:
            check(part, "exec-p2: wait_for_put ends before the sync load starts",
                  wait2["end_ns"] <= load["start_ns"], (wait2["end_ns"], load["start_ns"]))
    saves = find(p_records, "pool_task", operation="save")
    def put_operations(record):
        return [c["operation"] for c in record["native_calls"]]

    wanted_put = ["mooncake_put"] if LEVEL >= 3 else []
    check(part, f"pool_task[save] for both requests, succeeded, mooncake_put {NATIVE_SAYS}",
          sorted(r["request_id"] for r in saves) == ["P-R0", "P-R1"]
          and all(r["status"] == "succeeded" and put_operations(r) == wanted_put for r in saves),
          [(r["request_id"], r["status"], put_operations(r)) for r in saves])
    wanted_stages = ["build_key", "lookup", "prepare", "put", "sync"] if LEVEL >= 2 else []
    check(part, "pool_task[save].stages: " + ("the five host stages" if LEVEL >= 2 else "none at this level"),
          all(sorted(r["stages"]) == wanted_stages for r in saves),
          [(r["request_id"], sorted(r["stages"])) for r in saves])
    submits = find(p_records, "pool_task", operation="save_submit")
    check(part, "pool_task[save_submit] per step, submission_returned",
          sorted(r["execution_id"] for r in submits) == ["exec-p1", "exec-p2"]
          and all(r["status"] == "submission_returned" for r in submits))



def check_chain_d(part, d_records):
    output = D_SCHEDULER["chain_output"]
    exec_d1 = output.pd_execution_id
    check_scheduler_record(part, d_records, output, ["D-R2"])
    (step,) = find(d_records, "worker_summary", phase="execute")
    check_step_record(part, step, exec_d1, ["D-R2"], True)
    submits = find(d_records, "pool_task", operation="load_submit")
    loads = find(d_records, "pool_task", operation="load")
    check(part, "load_submit (submission_returned) and a background load (contract 2.2)",
          len(submits) == 1 and submits[0]["status"] == "submission_returned" and len(loads) == 1, (len(submits), len(loads)))
    if submits:
        submit_stage = step["stages"].get("submit", {})
        check(part, "load_submit lies inside the step's stages.submit (after the forward: no sync loads)",
              submit_stage and submit_stage["start_ns"] <= submits[0]["start_ns"]
              and submits[0]["end_ns"] <= submit_stage["end_ns"]
              and step["stages"]["forward"]["end_ns"] <= submit_stage["start_ns"])
    if loads:
        load = loads[0]
        check(part, "background load: queued, queue_wait_ns, succeeded, same request and execution",
              isinstance(load["queue_enter_ns"], int) and load["counters"].get("queue_entries") == 1
              and load["counters"].get("queue_wait_ns", -1) >= 0 and load["status"] == "succeeded"
              and (load["request_id"], load["execution_id"]) == ("D-R2", exec_d1), load["status"])
    links = find(d_records, "pd_transfer_summary", scope="link", transfer_role="pool_load")
    check(part, "one pool_load link (contract 3.3)", len(links) == 1, len(links))
    if links:
        link = links[0]
        expected = {"request_id": "D-R2", "execution_id": exec_d1, "engine_id": "1", "dp_rank": 0, "load_async": True,
                    "vllm_cached_tokens": 0, "kvpool_cached_tokens": 32, "links": [], "status": "linked",
                    "complete": True, "incomplete": False}
        check(part, "pool_load link fields", all(link.get(k) == v for k, v in expected.items()),
              {k: link.get(k) for k in expected})


try:
    p_records, d_records = run_chain()
    check_chain(p_records, d_records)
except BaseException as exc:
    check("chain", "call chain ran", False, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}")
    p_records, d_records = list(RECORDS), []


# ---------------------------------------------------------------- part 4: device timing (C14) through A2 claims
def run_device_timing():
    import vllm.distributed as vdist
    from vllm.distributed.kv_transfer import kv_transfer_state
    from vllm.v1.worker import kv_connector_model_runner_mixin as mixin
    from vllm.v1.worker.worker_base import pd_execution, pd_sample

    from vllm_ascend.worker import pd_device_timing

    kv_transfer_state._KV_CONNECTOR_AGENT = None  # timing only: no KV connector in these steps
    vdist.get_pp_group = lambda: SimpleNamespace(rank_in_group=0)

    class CopyOutput:
        """Stands in for vLLM's AsyncGPUModelRunnerOutput: get_output() returns after the copy."""

        def __init__(self, value):
            self.value = value

        def get_output(self):
            return self.value

    class TimedRunner(mixin.KVConnectorModelRunnerMixin):
        """Calls the device-timing hooks where model_runner_v1.NPUModelRunner calls them."""

        def __init__(self, asynchronous, tp_rank=0):
            self.parallel_config = SimpleNamespace(data_parallel_index=0, data_parallel_rank=0)
            self.asynchronous = asynchronous
            self.tp_rank = tp_rank
            self.step_ms = None
            self.fail_in = None  # "execute" or "sample": raise after the measured call

        @pd_execution
        def execute_model(self, scheduler_output, intermediate_tensors=None):
            pd_device_timing.begin_execute(self)
            with diag.span("runner.forward", completion="host_return"), pd_device_timing.measure(self, "forward"):
                DEVICE.advance(self.step_ms[0])
            if self.fail_in == "execute":
                raise RuntimeError("execute sentinel")
            return None

        @pd_sample
        def sample_tokens(self, grammar_output):
            pd_device_timing.begin_sample(self)
            with diag.span("runner.sampling", algorithm="sampler", completion="host_return"), \
                    pd_device_timing.measure(self, "sampling"):
                DEVICE.advance(self.step_ms[1])
            if self.fail_in == "sample":
                raise ValueError("sample sentinel")
            if not self.asynchronous:
                DEVICE.complete_all()  # the host copied the sampled tokens back
                pd_device_timing.end_sample(self, read_now=True)
                return "output"
            output = pd_device_timing.async_output_class(CopyOutput)("output-" + self._pd_execution_context["execution_id"])
            pd_device_timing.end_sample(self, read_now=False, async_output=output)
            return output

    def make_output(execution_id):
        from vllm.v1.core.sched.output import SchedulerOutput

        output = SchedulerOutput.make_empty()
        output.num_scheduled_tokens = {"T-" + execution_id: 1}
        output.pd_execution_id = execution_id
        return output

    def run(runner, execution_id, forward_ms, sampling_ms):
        vdist.get_tp_group = lambda: SimpleNamespace(rank_in_group=runner.tp_rank)
        runner.step_ms = (forward_ms, sampling_ms)
        runner.execute_model(make_output(execution_id))
        return runner.sample_tokens(None)

    scenarios = {}
    # Synchronous (P): two steps, read at the end of sample_tokens.
    RECORDS.clear()
    runner = TimedRunner(asynchronous=False)
    run(runner, "exec-t1", 7.0, 2.0)
    run(runner, "exec-t2", 9.0, 3.0)
    scenarios["sync"] = list(RECORDS)
    # Asynchronous (D): three steps launched before any output is consumed; a consumer thread then reads them.
    RECORDS.clear()
    runner = TimedRunner(asynchronous=True)
    outputs = [run(runner, f"exec-a{n}", 10.0 * n, 1.0 * n) for n in (1, 2, 3)]
    written_before = len([r for r in RECORDS if r["event"] == "worker_summary"])
    DEVICE.complete_all()
    returned = []
    consumer = threading.Thread(target=lambda: returned.extend(o.get_output() for o in outputs))
    consumer.start()
    consumer.join()
    scenarios["async"] = list(RECORDS)
    scenarios["async_meta"] = {"written_before_consumption": written_before, "returned": returned}
    # Asynchronous, the diagnostics fail inside get_output: the output is unchanged.
    RECORDS.clear()
    runner = TimedRunner(asynchronous=True)
    output = run(runner, "exec-x1", 5.0, 1.0)
    DEVICE.complete_all()

    class Broken:
        def collect(self):
            raise RuntimeError("diagnostics sentinel")

    output.pd_device_timing = Broken()
    scenarios["broken_meta"] = {"returned": output.get_output()}
    runner._pd_device_timing.collect()
    scenarios["broken"] = list(RECORDS)
    # Asynchronous, the first step's events never complete: written without device values once 4 newer steps began.
    RECORDS.clear()
    runner = TimedRunner(asynchronous=True)
    first = DEVICE.count
    run(runner, "exec-n1", 4.0, 1.0)
    DEVICE.held = set(range(first + 1, DEVICE.count + 1))
    for n in range(2, 7):
        DEVICE.complete_all()
        run(runner, f"exec-n{n}", 4.0, 1.0)
    DEVICE.complete_all()
    runner._pd_device_timing.collect()
    DEVICE.held = set()
    scenarios["never"] = list(RECORDS)
    # Errors: a step that raises in execute_model, then one that raises in sample_tokens (both asynchronous).
    for where, error in (("execute", RuntimeError), ("sample", ValueError)):
        RECORDS.clear()
        runner = TimedRunner(asynchronous=True)
        run(runner, f"exec-{where}-0", 3.0, 1.0)
        runner.fail_in = where
        raised = None
        try:
            run(runner, f"exec-{where}-1", 5.0, 1.0)
        except error as exc:
            raised = exc
        at_raise = [dict(r) for r in RECORDS if r["event"] == "worker_summary"]
        runner.fail_in = None
        DEVICE.complete_all()
        output = run(runner, f"exec-{where}-2", 7.0, 1.0)
        DEVICE.complete_all()
        output.get_output()
        runner._pd_device_timing.collect()
        scenarios[f"error_{where}"] = {"raised": type(raised).__name__ if raised else None, "at_raise": at_raise,
                                       "records": list(RECORDS)}
    # tp_rank 1: no device timing, records written at once.
    RECORDS.clear()
    runner = TimedRunner(asynchronous=True, tp_rank=1)
    run(runner, "exec-r1", 4.0, 1.0)
    scenarios["rank1"] = list(RECORDS)
    return scenarios


def device_values(records):
    out = {}
    for r in records:
        if r["event"] != "worker_summary":
            continue
        for stage in ("forward", "sampling"):
            if stage in r["stages"]:
                out[(r["execution_id"], stage)] = r["stages"][stage].get("device_duration_ns")
    return out


def summary_order(records):
    return [(r["execution_id"], r["phase"]) for r in records if r["event"] == "worker_summary"]


def check_device_timing(scenarios):
    part = "device timing"
    sync = scenarios["sync"]
    values = device_values(sync)
    check(part, "sync: forward and sampling carry device_duration_ns, attributed to their own step",
          values == {("exec-t1", "forward"): 7_000_000, ("exec-t1", "sampling"): 2_000_000,
                     ("exec-t2", "forward"): 9_000_000, ("exec-t2", "sampling"): 3_000_000}, values)
    check(part, "sync: records written in step order", summary_order(sync) == [
        ("exec-t1", "execute"), ("exec-t1", "sample"), ("exec-t2", "execute"), ("exec-t2", "sample")], summary_order(sync))
    for r in [r for r in sync if r["event"] == "worker_summary"]:
        stage = r["stages"]["forward" if r["phase"] == "execute" else "sampling"]
        check(part, f"sync {r['execution_id']} {r['phase']}: host duration_ns kept, stage keys plus device_duration_ns only",
              isinstance(stage["duration_ns"], int) and stage["status"] == "ok"
              and set(stage) - {"device_duration_ns"} == STAGE_KEYS | ({"completion"} if r["phase"] == "execute" else
                                                                       {"completion", "algorithm"}),
              sorted(stage))
    asynchronous = scenarios["async"]
    meta = scenarios["async_meta"]
    check(part, "async: nothing written before the outputs were consumed (records held by the claims)",
          meta["written_before_consumption"] == 0, meta)
    check(part, "async: get_output returns the outputs unchanged, in order",
          meta["returned"] == ["output-exec-a1", "output-exec-a2", "output-exec-a3"], meta["returned"])
    values = device_values(asynchronous)
    check(part, "async: three overlapping steps, each with its own device values",
          all(values.get((f"exec-a{n}", "forward")) == n * 10_000_000
              and values.get((f"exec-a{n}", "sampling")) == n * 1_000_000 for n in (1, 2, 3)), values)
    check(part, "async: records written in step order", summary_order(asynchronous) == [
        (f"exec-a{n}", phase) for n in (1, 2, 3) for phase in ("execute", "sample")], summary_order(asynchronous))
    check(part, "async: diagnostics raising inside get_output leave its return value unchanged",
          scenarios["broken_meta"]["returned"] == "output-exec-x1", scenarios["broken_meta"])
    check(part, "async: that step is still written (by the next read point)",
          summary_order(scenarios["broken"]) == [("exec-x1", "execute"), ("exec-x1", "sample")],
          summary_order(scenarios["broken"]))
    never = scenarios["never"]
    values = device_values(never)
    check(part, "never completing: the step is written without device_duration_ns, after 4 newer steps began",
          values.get(("exec-n1", "forward")) is None and values.get(("exec-n1", "sampling")) is None
          and summary_order(never)[:2] == [("exec-n1", "execute"), ("exec-n1", "sample")], summary_order(never)[:4])
    check(part, "never completing: later steps keep their own values and order",
          all(values.get((f"exec-n{n}", "forward")) == 4_000_000 for n in range(2, 6))
          and [e for e, _ in summary_order(never)] == sorted([e for e, _ in summary_order(never)], key=lambda e: int(e[6:])),
          values)
    for where, error_type in (("execute", "RuntimeError"), ("sample", "ValueError")):
        sc = scenarios[f"error_{where}"]
        phase = "execute" if where == "execute" else "sample"
        failed = f"exec-{where}-1"
        at_raise = [(r["execution_id"], r["phase"], r["status"], r.get("error_type")) for r in sc["at_raise"]]
        check(part, f"{where} raises: the exception reaches the caller unchanged", sc["raised"] == error_type, sc["raised"])
        check(part, f"{where} raises: that record is written at once, status error, error_type kept",
              (failed, phase, "error", error_type) in at_raise, at_raise)
        records = [r for r in sc["records"] if r["event"] == "worker_summary"]
        same = [r for r in records if r["execution_id"] == failed and r["phase"] == phase]
        check(part, f"{where} raises: the failed record is written exactly once and carries no device_duration_ns",
              len(same) == 1 and all("device_duration_ns" not in st for st in same[0]["stages"].values()), len(same))
        order = summary_order(records)
        expected = [(f"exec-{where}-{n}", p) for n in range(3) for p in ("execute", "sample")]
        if where == "execute":  # sample_tokens never runs after a failed execute_model
            expected.remove((failed, "sample"))
        check(part, f"{where} raises: every record of the three steps is written once (the failed one first, at once)",
              sorted(order) == sorted(expected) and len(order) == len(expected) and order[0] == (failed, phase)
              if where == "execute" else sorted(order) == sorted(expected) and len(order) == len(expected), order)
        values = device_values(records)
        check(part, f"{where} raises: the next step keeps its own device values",
              values.get((f"exec-{where}-2", "forward")) == 7_000_000, values)
    rank1 = scenarios["rank1"]
    check(part, "tp_rank 1: no device_duration_ns, records written at once",
          summary_order(rank1) == [("exec-r1", "execute"), ("exec-r1", "sample")]
          and all(v is None for v in device_values(rank1).values()), summary_order(rank1))


try:
    check_device_timing(run_device_timing())
except BaseException as exc:
    check("device timing", "device timing scenarios ran", False,
          f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}")


# ---------------------------------------------------------------- part 5: D reads KV from P (MooncakeConnectorV1)
def run_d_read_kv():
    """D's first-candidate step: the EngineCore maps the request to the step's execution_id (consumer link); the
    Worker's start_load_kv, inside the real pd_execution / mixin / MultiConnector, queues the reads; the receiving
    thread's work then writes task and request_generation records. Transfer engine and P are stubs."""
    from unittest.mock import patch

    import vllm.distributed as vdist
    from vllm.distributed.kv_transfer import kv_transfer_state
    from vllm.distributed.kv_transfer.kv_connector.v1.base import KVConnectorBase_V1
    from vllm.distributed.kv_transfer.kv_connector.v1.multi_connector import MultiKVConnectorMetadata
    from vllm.v1.worker import kv_connector_model_runner_mixin as mixin
    from vllm.v1.worker.worker_base import pd_execution

    from vllm_ascend.distributed.kv_transfer.ascend_multi_connector import AscendMultiConnector
    from vllm_ascend.distributed.kv_transfer.kv_p2p import mooncake_connector as mc

    check("D read KV", "mooncake_connector.py carries the read-KV diagnostics (p2d patch applied)",
          "pd_execution_id" in inspect.signature(mc.KVCacheRecvingThread.add_request).parameters)
    vdist.get_tp_group = lambda: SimpleNamespace(rank_in_group=0)
    vdist.get_pp_group = lambda: SimpleNamespace(rank_in_group=0)
    mixin.get_forward_context = lambda: SimpleNamespace(name="forward-context")
    layer = "model.layers.0.self_attn"
    remote_engine, remote_host, te_offset = "prefill-engine", "192.0.2.10", 10000
    ports = [30000, 30001]

    def vllm_config():
        config = SimpleNamespace(
            model_config=MagicMock(), parallel_config=MagicMock(), cache_config=MagicMock(),
            kv_transfer_config=MagicMock(), scheduler_config=MagicMock(disable_hybrid_kv_cache_manager=True),
            speculative_config=None,
            attention_config=SimpleNamespace(indexer_kv_dtype="auto", hisparse_config=None),
        )
        config.model_config.use_mla = False
        config.model_config.is_deepseek_mla = False
        config.model_config.hf_text_config = SimpleNamespace(num_key_value_heads=1, num_hidden_layers=1, head_dim=16,
                                                             model_type="qwen2")
        config.model_config.get_total_num_hidden_layers.return_value = 1
        parallel = config.parallel_config
        parallel.tensor_parallel_size, parallel.data_parallel_rank, parallel.data_parallel_size = 1, 0, 1
        parallel.data_parallel_size_local, parallel.pipeline_parallel_size, parallel.data_parallel_rank_local = 1, 1, 0
        parallel.prefill_context_parallel_size = parallel.decode_context_parallel_size = 1
        config.cache_config.block_size = 16
        transfer = config.kv_transfer_config
        transfer.kv_port, transfer.kv_role, transfer.engine_id = 30100, "kv_consumer", "decode-engine"
        transfer.get_from_extra_config = MagicMock(side_effect=lambda key, default: default)
        return config

    class Engine:
        """batch_transfer_sync_read: 3 ms per READ, success."""

        def __init__(self):
            self.calls = []

        def batch_transfer_sync_read(self, session_id, src_list, dst_list, length_list):
            start = time.monotonic_ns()
            time.sleep(0.003)
            self.calls.append((session_id, start, time.monotonic_ns()))
            return 0

    # EngineCore (D): build_connector_meta with the real SchedulerOutput carrying pd_execution_id.
    with patch.object(mc, "init_ascend_config"), patch.object(mc, "get_ascend_config", return_value=MagicMock()):
        kv_cache_config = SimpleNamespace(kv_cache_groups=[SimpleNamespace(layer_names=[layer],
                                                                           kv_cache_spec=MagicMock())], num_blocks=10)
        scheduler = mc.MooncakeConnectorScheduler(vllm_config(), "decode-engine", kv_cache_config)
    request = SimpleNamespace(
        request_id="D-K1", prompt_token_ids=[0, 1, 2, 3], status="running", output_token_ids=[101], num_prompt_tokens=4,
        _all_token_ids=[0, 1, 2, 3], max_tokens=8, prompt_embeds=None,
        kv_transfer_params={"do_remote_prefill": True, "remote_block_ids": ([3, 4],), "remote_engine_id": remote_engine,
                            "remote_request_id": "P-K1", "remote_host": remote_host, "remote_port": 30000},
    )
    scheduler._reqs_need_recv["D-K1"] = (request, ([1, 2],), ([1, 2],), 32)
    scheduler._reqs_in_batch.add("D-K1")
    RECORDS.clear()
    # D's scheduler schedules another request (D-K0) in this step; D-K1 only starts its read.
    output = recompute_scheduler_output(["D-K0"])
    output.has_sync_kv_loads = False
    p2d_meta = scheduler.build_connector_meta(output)

    # Worker (D): receiving thread and worker as the p2d tests build them, one READ task per remote port.
    group2layer = {0: ({"kv_cache_spec_type": "FullAttentionSpec", "kv_cache_group_id": 0}, [0])}
    engine = Engine()
    receiver = mc.KVCacheRecvingThread(
        tp_rank=0, tp_size=2, _prefill_pp_size=1, engine=engine, local_engine_id="decode-engine",
        local_handshake_port=30100, side_channel_port=30100, local_kv_caches_base_addr=[[0x1000, 0x2000]],
        block_len_per_addr=[[64, 64]], block_stride_per_addr=[[80, 80]], vllm_config=vllm_config(),
        kv_caches={layer: (torch.empty(1), torch.empty(1))}, kv_group2layeridx=group2layer, block_size_scale=[[1, 1]],
    )
    receiver._send_done_recv_signal = MagicMock()
    for port in ports:
        with receiver.remote_metadata_lock:
            receiver.kv_caches_base_addr[remote_engine][port] = [[0x3000, 0x4000]]
            receiver.remote_te_port[remote_engine][port] = port + te_offset
            receiver.remote_block_size_scale[remote_engine][port] = [[1, 1]]
            receiver.remote_block_stride_per_addr[remote_engine][port] = [[96, 96]]
            receiver.remote_kv_group2layeridx[remote_engine][port] = group2layer
    worker = mc.MooncakeConnectorWorker.__new__(mc.MooncakeConnectorWorker)
    worker.kv_send_thread, worker.kv_recv_thread, worker.tp_rank, worker.kv_role = None, receiver, 0, "kv_consumer"
    worker.pcp_size = worker.dcp_size = 1
    worker._prefill_tp_size = len(ports)
    worker.kv_group2layeridx = group2layer
    worker.remote_port_send_num = {}
    worker.vllm_config = receiver.vllm_config
    worker._get_sfa_replicate_k_block_ids = MagicMock(return_value=((), ()))
    worker._get_kv_split_metadata = MagicMock(return_value=([list(ports)], [([1, 2],)], [([3, 4],)]))
    worker._get_group_pulls_metadata = MagicMock(return_value=[
        [[mc.GroupPull(group_id=0, remote_tp_offset=k, num_group_pulls=1)] for k in range(len(ports))]])
    connector = mc.MooncakeConnector.__new__(mc.MooncakeConnector)
    connector.connector_worker, connector.connector_scheduler = worker, None
    connector._connector_metadata = mc.MooncakeConnectorMetadata()

    class PoolStub(KVConnectorBase_V1):
        def __init__(self):  # noqa: super-init-not-called
            self._connector_metadata = None

        def start_load_kv(self, forward_context, **kwargs):
            pass

        def wait_for_layer_load(self, layer_name):
            pass

        def save_kv_layer(self, *args, **kwargs):
            pass

        def wait_for_save(self):
            pass

        def get_finished(self, finished_req_ids):
            return set(), set()

        def get_num_new_matched_tokens(self, *args):
            return 0, False

        def update_state_after_alloc(self, *args):
            pass

        def build_connector_meta(self, *args):
            return None

    multi = AscendMultiConnector.__new__(AscendMultiConnector)
    multi._connectors = [connector, PoolStub()]
    multi._extra_async_saves = {}
    multi._connector_metadata = None
    kv_transfer_state._KV_CONNECTOR_AGENT = multi
    output.kv_connector_metadata = MultiKVConnectorMetadata(metadata=(p2d_meta, None))

    class Runner(mixin.KVConnectorModelRunnerMixin):
        def __init__(self):
            self.parallel_config = SimpleNamespace(data_parallel_index=0, data_parallel_rank=0)

        @pd_execution
        def execute_model(self, scheduler_output, intermediate_tensors=None):
            with self.maybe_get_kv_connector_output(scheduler_output) as kv_output:
                with diag.span("runner.forward", completion="host_return"):
                    time.sleep(0.002)
            return kv_output

    try:
        Runner().execute_model(output)
        while not receiver.request_queue.empty():  # what the receiving thread does with each queued read
            receiver._handle_request(receiver.request_queue.get_nowait())
    finally:
        receiver.executor.shutdown(wait=True)
        kv_transfer_state._KV_CONNECTOR_AGENT = None
    return list(RECORDS), output, engine


def check_d_read_kv(records, output, engine):
    part = "D read KV"
    check_common(part, records)
    if not gate_execution_ids(part, records):
        return
    check_scheduler_record(part, records, output, ["D-K0"])
    links = find(records, "pd_transfer_summary", scope="link", transfer_role="consumer")
    tasks = find(records, "pd_transfer_summary", scope="task")
    summaries = find(records, "pd_transfer_summary", scope="request_generation")
    steps = find(records, "worker_summary", phase="execute")
    expected = output.pd_execution_id
    check(part, "EngineCore: one consumer link for the request, execution_id == SchedulerOutput.pd_execution_id",
          len(links) == 1 and links[0]["request_id"] == "D-K1" and links[0]["execution_id"] == expected
          and links[0]["links"][0].get("request_id") == "P-K1", [(l["request_id"], l["execution_id"]) for l in links])
    check(part, "Worker: one read task per remote port, each with execution_id == SchedulerOutput.pd_execution_id",
          len(tasks) == 2 and all(t["execution_id"] == expected and t["request_id"] == "D-K1" for t in tasks),
          [(t.get("request_id"), t.get("execution_id")) for t in tasks])
    check(part, "read tasks: tp_rank 0, the request's P side, a READ native call each, stages.wait_for_transfer",
          len(tasks) == 2
          and all(t.get("tp_rank") == 0 and t.get("remote_request_id") == "P-K1" and len(t.get("native_calls", [])) == 1
                  and t["stages"]["wait_for_transfer"]["calls"] == 1 for t in tasks), [t.get("status") for t in tasks])
    check(part, "request_generation: envelope covers both 3 ms READs, execution_id null by design",
          len(summaries) == 1 and summaries[0]["execution_id"] is None
          and summaries[0]["transfer_envelope_ns"] >= 3_000_000, [(s.get("transfer_envelope_ns"), s.get("execution_id"))
                                                                for s in summaries])
    check(part, "C2 chain: link == the step's worker_summary == every read task (same execution_id), submit recorded",
          len(steps) == 1 and steps[0]["execution_id"] == expected and steps[0]["request_ids"] == ["D-K0"]
          and "submit" in steps[0]["stages"] and len(links) == 1 and len(tasks) == 2
          and {links[0]["execution_id"], *(t["execution_id"] for t in tasks)} == {steps[0]["execution_id"]},
          [(s["execution_id"], s["request_ids"], sorted(s["stages"])) for s in steps])
    check(part, "the stub transfer engine served exactly the two READs", len(engine.calls) == 2, len(engine.calls))


try:
    d_kv_records, d_kv_output, d_kv_engine = run_d_read_kv()
    check_d_read_kv(d_kv_records, d_kv_output, d_kv_engine)
except BaseException as exc:
    check("D read KV", "D read-KV scenario ran", False, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}")
    d_kv_records = list(RECORDS)

# ---------------------------------------------------------------- part 6: PD_DIAG_STARTUP lines
STARTUP_LINES: list[str] = []


def run_startup_lines():
    """The site checks which classes it runs from three plain log lines (not [PD_DIAG] records), each written once
    per process by vLLM's own code: EngineCore's scheduler line (vllm/v1/engine/core.py, called right after
    EngineCore.__init__ builds the scheduler), the Worker's model_runner line (WorkerWrapperBase.init_device) and its
    device_timing line (WorkerWrapperBase.execute_model, after the first step that ran a forward). The once-per-process
    flags are reset here before each simulated instance. P: the scheduler the name vllm.v1.core.sched.scheduler.Scheduler
    gives (BalanceScheduler), synchronous, default vllm-ascend switches. D: AsyncRecomputeScheduler with
    recompute_scheduler_enable. Worker: the V1 NPUModelRunner class; device timing through a runner that calls
    pd_device_timing at the V1 positions, tp_rank 0 and 1."""
    import functools

    import vllm.distributed as vdist
    import vllm.v1.core.sched.scheduler as sched_mod
    from tests.v1.core import utils as core_utils
    from vllm.config import DeviceConfig, VllmConfig
    from vllm.v1.core.sched.output import SchedulerOutput
    from vllm.v1.engine import core as engine_core
    from vllm.v1.worker import worker_base
    from vllm.v1.worker.worker_base import WorkerWrapperBase, pd_execution

    from vllm_ascend.ascend_config import init_ascend_config
    from vllm_ascend.core import recompute_scheduler as rs
    from vllm_ascend.worker import pd_device_timing
    from vllm_ascend.worker.model_runner_v1 import NPUModelRunner

    class Grab(logging.Handler):
        def emit(self, record):
            message = record.getMessage()
            if message.startswith("PD_DIAG_STARTUP"):
                STARTUP_LINES.append(message)

    grab = Grab()
    for name in ("vllm.v1.engine.core", "vllm.v1.worker.worker_base"):
        logging.getLogger(name).addHandler(grab)
        logging.getLogger(name).setLevel(logging.INFO)

    def scheduler_line(scheduler_cls, async_scheduling, additional_config):
        saved = core_utils.Scheduler, core_utils.AsyncScheduler, core_utils.VllmConfig
        core_utils.Scheduler = core_utils.AsyncScheduler = scheduler_cls
        core_utils.VllmConfig = functools.partial(VllmConfig, device_config=DeviceConfig(device="cpu"),
                                                  additional_config=additional_config)
        try:
            scheduler = core_utils.create_scheduler(async_scheduling=async_scheduling, block_size=16)
        finally:
            core_utils.Scheduler, core_utils.AsyncScheduler, core_utils.VllmConfig = saved
        init_ascend_config(scheduler.vllm_config)  # the instance's own switches, as its process has them
        engine_core._PD_STARTUP_LOGGED = False
        before = len(STARTUP_LINES)
        engine_core._pd_startup_log_scheduler(scheduler)
        return STARTUP_LINES[before:], scheduler

    p_lines, p_scheduler = scheduler_line(sched_mod.Scheduler, False, {"refresh": True})
    d_lines, _ = scheduler_line(rs.AsyncRecomputeScheduler, True,
                                {"refresh": True, "scheduler_config": {"recompute_scheduler_enable": True}})

    def worker_lines(tp_rank):
        vdist.get_tp_group = lambda: SimpleNamespace(rank_in_group=tp_rank)
        vdist.get_pp_group = lambda: SimpleNamespace(rank_in_group=0)

        class SiteRunner:  # pd_device_timing hooks at the positions model_runner_v1.execute_model has them
            parallel_config = SimpleNamespace(data_parallel_index=0, data_parallel_rank=0)

            @pd_execution
            def execute_model(self, scheduler_output, intermediate_tensors=None):
                pd_device_timing.begin_execute(self)
                with diag.span("runner.forward", completion="host_return"), pd_device_timing.measure(self, "forward"):
                    pass
                return None

        runner = SiteRunner()
        wrapper = WorkerWrapperBase.__new__(WorkerWrapperBase)
        wrapper.vllm_config = p_scheduler.vllm_config
        wrapper.mm_receiver_cache = None
        wrapper.worker = SimpleNamespace(init_device=lambda: None, model_runner=object.__new__(NPUModelRunner),
                                         parallel_config=runner.parallel_config)
        worker_base._PD_STARTUP_RUNNER_LOGGED = False
        worker_base._PD_STARTUP_TIMING_LOGGED = False
        before = len(STARTUP_LINES)
        wrapper.init_device()
        wrapper.worker = SimpleNamespace(model_runner=runner, execute_model=runner.execute_model,
                                         parallel_config=runner.parallel_config)
        output = SchedulerOutput.make_empty()
        output.pd_execution_id = f"exec-startup-{tp_rank}"
        output.num_scheduled_tokens = {"R-startup": 4}
        output.total_num_scheduled_tokens = 4
        wrapper.execute_model(output)
        wrapper.execute_model(output)  # a second step logs nothing more
        timing = getattr(runner, "_pd_device_timing", None)
        if timing is not None:
            timing.release_all()  # write the held records
        return STARTUP_LINES[before:]

    return p_lines, d_lines, worker_lines(0), worker_lines(1)


def check_startup_lines(p_lines, d_lines, w0_lines, w1_lines):
    part = "startup lines"
    switches = "dyntra_lb=False batch_job=False profiling_chunk=False"
    p_expected = ("PD_DIAG_STARTUP scheduler=vllm_ascend.patch.platform.patch_balance_schedule.BalanceScheduler "
                  f"balance=False recompute=False {switches}")
    d_expected = ("PD_DIAG_STARTUP scheduler=vllm_ascend.core.recompute_scheduler.AsyncRecomputeScheduler "
                  f"balance=False recompute=True {switches}")
    check(part, "P (EngineCore): one scheduler line, BalanceScheduler, balance and recompute off", p_lines == [p_expected],
          p_lines)
    check(part, "D (EngineCore): one scheduler line, AsyncRecomputeScheduler, recompute on", d_lines == [d_expected],
          d_lines)
    # A vLLM patch that knows about the granularity prints a line of its own; it is theirs, and it is checked
    # separately so that the lines this package is responsible for are compared exactly, as before.
    granularity = [line for line in p_lines + d_lines + w0_lines + w1_lines if line.startswith("PD_DIAG_STARTUP diag ")]
    p_lines, d_lines, w0_lines, w1_lines = (
        [line for line in lines if not line.startswith("PD_DIAG_STARTUP diag ")]
        for lines in (p_lines, d_lines, w0_lines, w1_lines))
    runner_line = "PD_DIAG_STARTUP worker model_runner=vllm_ascend.worker.model_runner_v1.NPUModelRunner tp_rank={}"
    check(part, "Worker tp_rank 0: model_runner line, then device_timing=on after the first step, once each",
          w0_lines == [runner_line.format(0), "PD_DIAG_STARTUP worker device_timing=on tp_rank=0"], w0_lines)
    check(part, "Worker tp_rank 1: model_runner line, then device_timing=off (C14 is tp_rank 0 only), once each",
          w1_lines == [runner_line.format(1), "PD_DIAG_STARTUP worker device_timing=off tp_rank=1"], w1_lines)
    if granularity:
        wanted = [f"PD_DIAG_STARTUP diag level={LEVEL} source={source}" for source in ("default", "env", "invalid")]
        check(part, f"the vLLM side's granularity line says level {LEVEL}, the level this run measured at",
              all(line in wanted for line in granularity), granularity)


def check_kv_load_submit():
    """The vLLM side decides once per process whether the kv.load_submit span is opened: not on a PD decode
    instance at level 1, opened everywhere else. Driving its real decision needs a config its own is_pd_decode()
    calls a decode node; when this harness cannot present one, that is said rather than passed over."""
    part = "kv load submit"
    decide = getattr(diag, "decide_kv_load_submit", None)
    if decide is None:
        check(part, "the vLLM side does not offer this decision (an older patch): the span is always opened", True)
        return
    saved = getattr(diag, "_RECORDS_KV_LOAD_SUBMIT", None)
    try:
        config = SimpleNamespace(kv_transfer_config=SimpleNamespace(is_kv_consumer=True, is_kv_producer=False))
        if not diag.is_pd_decode(config):
            check(part, "a decode node could not be presented here, so the decision is not driven", True,
                  "is_pd_decode said no; see the vllm-ascend condition")
            return
        diag._RECORDS_KV_LOAD_SUBMIT = None
        on_decode = decide(config)
        check(part, f"decode node at level {LEVEL}: the span is " + ("opened" if LEVEL != 1 else "left out"),
              on_decode == (LEVEL != 1), on_decode)
        if not on_decode:
            diag._RECORDS_KV_LOAD_SUBMIT = None
            opened = diag.kv_load_submit_span(config)
            check(part, "and the span it hands back does nothing",
                  type(opened).__name__ == "nullcontext", type(opened).__name__)
        prefill = SimpleNamespace(kv_transfer_config=SimpleNamespace(is_kv_consumer=False, is_kv_producer=True))
        diag._RECORDS_KV_LOAD_SUBMIT = None
        check(part, f"prefill node at level {LEVEL}: the span is opened", decide(prefill) is True)
    finally:
        diag._RECORDS_KV_LOAD_SUBMIT = saved


try:
    check_startup_lines(*run_startup_lines())
    check_kv_load_submit()
except BaseException as exc:
    check("startup lines", "startup lines ran", False, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}")

OUT.mkdir(parents=True, exist_ok=True)
(OUT / "joint-check.json").write_text(json.dumps({"results": RESULTS}, indent=1, ensure_ascii=False))
for name, records in (("joint-records-P.json", p_records), ("joint-records-D.json", d_records),
                      ("joint-records-D-read-kv.json", d_kv_records)):
    for record in records:
        record["host"] = "<container>"
    (OUT / name).write_text(json.dumps(records, indent=1, sort_keys=True))
(OUT / "startup-lines.txt").write_text("\n".join(STARTUP_LINES) + "\n")
failed = [r for r in RESULTS if not r["ok"]]
print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
sys.stdout.flush()
sys.stderr.flush()
# Background KV threads are daemons blocked on their queues; leave without joining them.
os._exit(1 if failed else 0)
