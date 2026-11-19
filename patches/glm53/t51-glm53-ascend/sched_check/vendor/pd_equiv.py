"""Drive one fixed set of synthetic calls through an installed vLLM and dump every
[PD_DIAG] record it writes.

Same script, same inputs, for v0.32.0 + patch 01 and v0.30.0 + port. Real code:
OpenAI routers and serving classes, renderer, InputProcessor, AsyncLLM,
AsyncMPClient._send_input, msgpack transport, EngineCore.step /
step_with_batch_queue, Scheduler, KVCacheManager, Request, OutputProcessor,
IterationStats, WorkerWrapperBase, the KV connector model-runner mixin and the
V2 ActiveKVConnector. Fake: the model (sampled token ids), ZMQ sockets, process
boundaries, distributed groups.

usage: python pd_equiv.py OUT.jsonl
"""

import asyncio
import faulthandler
import json
import logging
import os
import queue
import random
import sys
import threading
import types
from collections import deque
from concurrent.futures import Future
from contextlib import contextmanager
from types import SimpleNamespace

OUT = sys.argv[1]
faulthandler.enable()
SCENARIO = ["init"]
RECORDS: list = []


def say(*a):
    print("[equiv]", *a, file=sys.stderr, flush=True)


class _Capture(logging.Handler):
    def emit(self, record):
        msg = record.getMessage()
        if "[PD_DIAG] " in msg:
            RECORDS.append(
                {"scenario": SCENARIO[0], "rec": json.loads(msg.split("[PD_DIAG] ", 1)[1])}
            )



sys.path.insert(0, os.getcwd())  # repo root, for tests.* helpers

import torch  # noqa: E402

import vllm.distributed as vdist  # noqa: E402

_GROUP = SimpleNamespace(rank_in_group=0, world_size=1)
vdist.get_tp_group = lambda: _GROUP
vdist.get_pp_group = lambda: _GROUP

from tests.v1.core.utils import create_requests, create_scheduler  # noqa: E402
from tests.v1.kv_connector.unit.utils import MockKVConfig  # noqa: E402
from vllm.sampling_params import SamplingParams  # noqa: E402
from vllm.v1.engine import (  # noqa: E402
    EngineCoreOutputs,
    EngineCoreRequest,
    EngineCoreRequestType,
)
from vllm.v1.engine.core import EngineCore  # noqa: E402
from vllm.v1.metrics import pd_diagnostics as diag  # noqa: E402
from vllm.v1.outputs import (  # noqa: E402
    DraftTokenIds,
    KVConnectorOutput,
    ModelRunnerOutput,
)
from vllm.v1.request import RequestStatus  # noqa: E402
from vllm.v1.serial_utils import MsgpackDecoder, MsgpackEncoder  # noqa: E402

RNG = random.Random(1234)

# vLLM may (re)run dictConfig, which clears handlers on child loggers; attach
# again at every scenario switch.
_CAPTURE = _Capture()


def scenario(name):
    SCENARIO[0] = name
    lg = logging.getLogger("vllm.v1.metrics.pd_diagnostics")
    if _CAPTURE not in lg.handlers:
        lg.addHandler(_CAPTURE)
    lg.setLevel(logging.INFO)
    lg.disabled = False


scenario("init")


# ---------------------------------------------------------------- worker side
class FakeKVConnector:
    """Minimal V1 connector worker surface used by the mixin and V2 adapter."""

    def __init__(self):
        self.pending_recv: set[str] = set()
        self.calls = []
        self.load_delay_s = 0.0

    # mixin / adapter surface
    def bind_connector_metadata(self, meta):
        self.calls.append("bind")

    def clear_connector_metadata(self):
        self.calls.append("clear")

    def handle_preemptions(self, meta):
        self.calls.append("preempt")

    def start_load_kv(self, forward_context, **kwargs):
        self.calls.append("start_load_kv")
        if self.load_delay_s:
            import time

            time.sleep(self.load_delay_s)

    def wait_for_save(self):
        self.calls.append("wait_for_save")

    def finish_forward(self):
        self.calls.append("finish_forward")

    def get_finished(self, finished_req_ids):
        return None, None

    def get_transfer_results(self, finished_req_ids):
        done, self.pending_recv = self.pending_recv, set()
        return SimpleNamespace(
            finished_sending=None, finished_recving=done or None, failed_recving=set()
        )

    # odds and ends the mixin may touch
    def get_block_ids_with_load_errors(self):
        return set()

    def get_kv_connector_stats(self):
        return None

    def get_kv_connector_kv_cache_events(self):
        return None

    def build_connector_worker_meta(self):
        return None

    def get_kv_connector_worker_meta(self):
        return None


def _mixin_module():
    import vllm.v1.worker.kv_connector_model_runner_mixin as mixin

    return mixin


def _make_v1_runner(connector):
    """A runner whose execute_model carries the real pd_execution decorator and
    runs the real V1 KV connector mixin; forward/sampling are fake spans."""
    mixin = _mixin_module()
    from vllm.distributed.kv_transfer.kv_connector.base import KVConnectorBase
    from vllm.v1.worker.worker_base import pd_execution, pd_sample

    # isinstance(kv_connector, KVConnectorBase) must hold for the mixin.
    class _Conn(FakeKVConnector, KVConnectorBase):
        def __init__(self):
            FakeKVConnector.__init__(self)

    _Conn.__abstractmethods__ = frozenset()
    conn = _Conn.__new__(_Conn)
    _Conn.__init__(conn)
    mixin.has_kv_transfer_group = lambda: True
    mixin.get_kv_transfer_group = lambda: conn

    from vllm.forward_context import set_forward_context

    class Runner(mixin.KVConnectorModelRunnerMixin):
        parallel_config = SimpleNamespace(data_parallel_index=0)

        def __init__(self, vllm_config):
            self.vllm_config = vllm_config
            self.last = None

        @pd_execution
        def execute_model(self, scheduler_output, intermediate_tensors=None):
            from vllm.v1.outputs import EMPTY_MODEL_RUNNER_OUTPUT

            self.last = scheduler_output
            has_kv = scheduler_output.kv_connector_metadata is not None
            mixin.has_kv_transfer_group = lambda: has_kv
            self.kv_out = None
            if scheduler_output.total_num_scheduled_tokens == 0:
                if not has_kv:
                    return EMPTY_MODEL_RUNNER_OUTPUT
                return self.kv_connector_no_forward(scheduler_output, self.vllm_config)
            with set_forward_context(None, self.vllm_config):
                with self.maybe_get_kv_connector_output(scheduler_output) as kv_out:
                    with diag.span("runner.forward", mode="eager", completion="host_return"):
                        pass
            self.kv_out = kv_out
            return None

        @pd_sample
        def sample_tokens(self, grammar_output):
            with diag.span("runner.sampling", algorithm="sampler", completion="host_return"):
                pass
            return None

    return Runner, conn


class FakeExecutor:
    """Executor surface used by EngineCore.step*; returns synthetic tokens."""

    def __init__(self, core, runner, connector, spec_k=0, late_futures=False):
        self.late_futures = late_futures
        self.core = core
        self.runner = runner
        self.connector = connector
        self.spec_k = spec_k
        self.pending_so = None
        from vllm.v1.worker.worker_base import WorkerWrapperBase

        self.wrapper = WorkerWrapperBase.__new__(WorkerWrapperBase)
        self.wrapper.worker = SimpleNamespace(
            execute_model=runner.execute_model,
            sample_tokens=runner.sample_tokens,
            parallel_config=runner.parallel_config,
        )
        self.wrapper.mm_receiver_cache = None
        self.wrapper._apply_mm_cache = lambda so: None

    def _done(self, value):
        f = Future()
        if self.late_futures:
            # Complete after a short delay from another thread, so the engine
            # schedules and submits the next step while this one is in flight.
            threading.Timer(0.005, f.set_result, args=(value,)).start()
        else:
            f.set_result(value)
        return f

    def execute_model(self, scheduler_output, non_block=False):
        waiting = [
            r.request_id
            for r in self.core.scheduler.requests.values()
            if r.status == RequestStatus.WAITING_FOR_REMOTE_KVS
        ]
        self.connector.pending_recv |= set(waiting)
        RECORDS.append(
            {
                "scenario": SCENARIO[0],
                "step": {
                    "seen_by": "execute_model",
                    "execution_id": scheduler_output.pd_execution_id,
                    "request_ids": list(scheduler_output.num_scheduled_tokens),
                    "total_tokens": scheduler_output.total_num_scheduled_tokens,
                    "has_sync_kv_loads": bool(
                        getattr(scheduler_output, "has_sync_kv_loads", False)
                    ),
                },
            }
        )
        out = self.wrapper.execute_model(scheduler_output)
        self.pending_so = scheduler_output
        if out is not None:
            out.kv_connector_output = KVConnectorOutput(
                finished_recving=set(waiting) or None
            )
        return self._done(out) if non_block else out

    def sample_tokens(self, grammar_output, non_block=False):
        so = self.pending_so
        self.wrapper.sample_tokens(grammar_output)
        req_ids = list(so.num_scheduled_tokens)
        sched = self.core.scheduler
        sampled = []
        for rid in req_ids:
            req = sched.requests.get(rid)
            n = so.num_scheduled_tokens[rid]
            if req is None:
                sampled.append([])
                continue
            # num_computed_tokens is already advanced by _update_after_schedule.
            done_prefill = req.num_computed_tokens >= req.num_prompt_tokens
            if not done_prefill:
                sampled.append([])
                continue
            spec = len(so.scheduled_spec_decode_tokens.get(rid, ()))
            accepted = RNG.randint(0, spec) if spec else 0
            sampled.append([RNG.randint(5, 60) for _ in range(1 + accepted)])
        kv_out = getattr(self.runner, "kv_out", None)
        out = ModelRunnerOutput(
            req_ids=req_ids,
            req_id_to_index={r: i for i, r in enumerate(req_ids)},
            sampled_token_ids=sampled,
            kv_connector_output=kv_out,
        )
        return self._done(out) if non_block else out

    def take_draft_token_ids(self):
        if not self.spec_k:
            return None
        ids = [r for r in self.core.scheduler.running]
        return DraftTokenIds(
            [r.request_id for r in ids],
            [[RNG.randint(5, 60) for _ in range(self.spec_k)] for _ in ids],
        )

    def shutdown(self):
        pass


def make_core(scheduler, batch_queue_size=1, spec_k=0, async_scheduling=False):
    core = EngineCore.__new__(EngineCore)
    core.scheduler = scheduler
    core.vllm_config = scheduler.vllm_config
    core.log_stats = False
    core.batch_queue_size = batch_queue_size
    core.batch_queue = deque(maxlen=batch_queue_size) if batch_queue_size > 1 else None
    core.aborts_queue = queue.Queue()
    core.output_queue = queue.Queue()
    core.is_pooling_model = False
    core.is_ec_consumer = True  # v0.30.0 name
    core.is_mm_encoder_only = False  # v0.32.0 name
    core.check_for_draft_tokens = bool(spec_k)
    core.async_scheduling = async_scheduling
    core._weight_version = None
    core._iteration_index = 0
    core.mm_receiver_cache = None
    core.structured_output_manager = SimpleNamespace(clear_backend=lambda: None)
    core.request_block_hasher = None
    Runner, conn = _make_v1_runner(None)
    runner = Runner(core.vllm_config)
    core.model_executor = FakeExecutor(
        core, runner, conn, spec_k=spec_k, late_futures=async_scheduling
    )
    core.get_supported_tasks = lambda: ("generate",)
    core.connector_fake = conn
    original_schedule = scheduler.schedule

    def schedule_and_record(*args, **kwargs):
        output = original_schedule(*args, **kwargs)
        # Taken before EngineCore's fallback id, i.e. what the scheduler set.
        RECORDS.append(
            {
                "scenario": SCENARIO[0],
                "step": {
                    "seen_by": "scheduler.schedule",
                    "execution_id": output.pd_execution_id,
                    "request_ids": list(output.num_scheduled_tokens),
                },
            }
        )
        return output

    scheduler.schedule = schedule_and_record
    return core


def run_core(core, max_steps=200):
    """Step until idle; return the transported EngineCoreOutputs."""
    enc, dec = MsgpackEncoder(), MsgpackDecoder(EngineCoreOutputs)
    sent = []
    step = core.step_with_batch_queue if core.batch_queue is not None else core.step
    for i in range(max_steps):
        if not core.scheduler.has_requests() and not core.batch_queue:
            break
        say(SCENARIO[0], "step", i)
        outputs, executed = step()
        for ci, eco in (outputs or {}).items():
            core._enqueue_output(ci, eco)
        while not core.output_queue.empty():
            ci, eco = core.output_queue.get_nowait()
            sent.append((ci, dec.decode(enc.encode(eco))))
        core.post_step(executed)
    return sent


def engine_scenarios():
    # E1: P-like, prefix caching (hash), chunked prefill, one finished per step.
    scenario("E1_engine_sync_prefill_decode")
    sched = create_scheduler(
        enable_prefix_caching=True, max_num_batched_tokens=64, block_size=16
    )
    core = make_core(sched)
    reqs = create_requests(num_requests=3, num_tokens=40, max_tokens=3, same_prompt=True)
    for i, r in enumerate(reqs):
        r.pd_identity = {"observation_id": f"api-x-{i}", "submission_id": f"sub-{i}"}
        core.add_request(r)
    outs = run_core(core)
    dump_outputs("E1_engine_sync_prefill_decode", outs)

    # E2: D-like, async remote KV (WAITING_FOR_REMOTE_KVS), MTP k=5 drafts.
    scenario("E2_engine_remote_kv_spec5")
    sched = create_scheduler(
        use_kv_connector=MockKVConfig(matched_tokens=32, is_async=True),
        num_speculative_tokens=5,
        block_size=16,
    )
    core = make_core(sched, spec_k=5)
    for r in create_requests(num_requests=2, num_tokens=48, max_tokens=8):
        core.add_request(r)
    outs = run_core(core)
    dump_outputs("E2_engine_remote_kv_spec5", outs)

    # E3: batch queue (size 2), plain decode.
    scenario("E3_engine_batch_queue")
    sched = create_scheduler(block_size=16)
    core = make_core(sched, batch_queue_size=2)
    for r in create_requests(num_requests=2, num_tokens=20, max_tokens=4):
        core.add_request(r)
    outs = run_core(core)
    dump_outputs("E3_engine_batch_queue", outs)

    # E4: abort while running, then shutdown closes the rest.
    scenario("E4_engine_abort_shutdown")
    sched = create_scheduler(block_size=16)
    core = make_core(sched)
    rs = create_requests(num_requests=2, num_tokens=20, max_tokens=50)
    for r in rs:
        core.add_request(r)
    core.step()
    core.abort_requests([rs[0].request_id])
    core.step()
    sched.shutdown()

    # E5: async scheduling (AsyncScheduler + batch queue of 2), steps overlap.
    scenario("E5_engine_async_scheduling")
    sched = create_scheduler(async_scheduling=True, block_size=16)
    core = make_core(sched, batch_queue_size=2, async_scheduling=True)
    for r in create_requests(num_requests=3, num_tokens=24, max_tokens=5):
        core.add_request(r)
    outs = run_core(core)
    dump_outputs("E5_engine_async_scheduling", outs)

    # E6: async scheduling with MTP k=5 drafts.
    scenario("E6_engine_async_spec5")
    try:
        sched = create_scheduler(
            async_scheduling=True,
            num_speculative_tokens=5,
            # The config allows async scheduling only with GPU-side drafting;
            # on the scheduler side this is k=5 placeholder tokens either way.
            speculative_method="ngram_gpu",
            block_size=16,
        )
        core = make_core(sched, batch_queue_size=2, spec_k=5, async_scheduling=True)
        for r in create_requests(num_requests=2, num_tokens=24, max_tokens=8):
            core.add_request(r)
        outs = run_core(core)
        dump_outputs("E6_engine_async_spec5", outs)
    except Exception:
        import traceback

        RECORDS.append({"scenario": SCENARIO[0], "driver_error": traceback.format_exc()})

    # E7: P-like synchronous KV load (has_sync_kv_loads): start_load_kv before
    # forward, made to take 20 ms so the submit range is unambiguous.
    scenario("E7_engine_sync_kv_load")
    sched = create_scheduler(
        use_kv_connector=MockKVConfig(matched_tokens=32, is_async=False),
        block_size=16,
    )
    core = make_core(sched)
    core.connector_fake.load_delay_s = 0.02
    for r in create_requests(num_requests=2, num_tokens=48, max_tokens=3):
        core.add_request(r)
    outs = run_core(core)
    dump_outputs("E7_engine_sync_kv_load", outs)


def dump_outputs(name, outs):
    for ci, eco in outs:
        RECORDS.append(
            {
                "scenario": name,
                "transport": {
                    "client_index": ci,
                    "output_batch_id": eco.output_batch_id,
                    "outputs": [
                        {
                            "request_id": o.request_id,
                            "pd_execution_id": o.pd_execution_id,
                            "output_batch_id": o.output_batch_id,
                            "n_tokens": len(o.new_token_ids),
                            "finished": o.finished,
                        }
                        for o in eco.outputs
                    ],
                    "finished_requests": sorted(eco.finished_requests or ()),
                },
            }
        )


# -------------------------------------------------------------- frontend side
CHAT_TEMPLATE = (
    "{% for m in messages %}{{ m['role'] }}: {{ m['content'] }}\n{% endfor %}"
    "{% if add_generation_prompt %}assistant:{% endif %}"
)


def build_frontend():
    from vllm.engine.arg_utils import AsyncEngineArgs
    from vllm.v1.engine import core_client as cc
    from vllm.v1.engine.async_llm import AsyncLLM

    vllm_config = AsyncEngineArgs(
        model="facebook/opt-125m", max_model_len=256, enforce_eager=True
    ).create_engine_config()

    sched = create_scheduler(block_size=16)
    core = make_core(sched)
    out_q: asyncio.Queue = asyncio.Queue()
    req_dec = MsgpackDecoder(EngineCoreRequest)

    class FakeClient(cc.AsyncMPClient):
        def ensure_alive(self):
            pass

        def _send_input_message(self, message, engine):
            kind = message[0]
            if kind == EngineCoreRequestType.ADD.value:
                req = req_dec.decode(message[1:])
                core.add_request(*core.preprocess_add_request(req))
            elif kind == EngineCoreRequestType.ABORT.value:
                pass
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            loop.call_soon(fut.set_result, None)
            return fut

        def _ensure_output_queue_task(self):
            pass

        async def get_output_async(self):
            return await out_q.get()

        async def abort_requests_async(self, request_ids):
            core.abort_requests(list(request_ids))

        async def get_supported_tasks_async(self):
            return ("generate",)

        def shutdown(self, *args, **kwargs):
            pass

    client = FakeClient.__new__(FakeClient)
    client.client_index = 0
    client.encoder = MsgpackEncoder()
    client.core_engine = (0).to_bytes(2, "little")
    client.resources = SimpleNamespace(engine_dead=False)
    client.engine_ranks_managed = [0]
    cc.EngineCoreClient.make_async_mp_client = staticmethod(lambda *a, **k: client)

    engine = AsyncLLM(
        vllm_config=vllm_config,
        executor_class=object,
        log_stats=True,
        log_requests=False,
        start_engine_loop=False,
    ) if "start_engine_loop" in AsyncLLM.__init__.__code__.co_varnames else AsyncLLM(
        vllm_config=vllm_config, executor_class=object, log_stats=True, log_requests=False
    )
    engine.logger_manager = None
    if hasattr(engine, "_logger_ref"):
        engine._logger_ref = [None]

    async def pump():
        enc, dec = MsgpackEncoder(), MsgpackDecoder(EngineCoreOutputs)
        while True:
            if core.scheduler.has_requests():
                outputs, executed = core.step()
                for ci, eco in (outputs or {}).items():
                    core._enqueue_output(ci, eco)
                while not core.output_queue.empty():
                    ci, eco = core.output_queue.get_nowait()
                    await out_q.put(dec.decode(enc.encode(eco)))
                core.post_step(executed)
            await asyncio.sleep(0.001)

    return engine, vllm_config, pump


async def frontend_scenarios():
    from vllm.entrypoints.launchers.api_server.app_state import init_app_state
    from vllm.entrypoints.launchers.app import build_app
    from vllm.entrypoints.launchers.cli_args import make_arg_parser
    from vllm.utils.argparse_utils import FlexibleArgumentParser
    import httpx

    say("build frontend")
    engine, vllm_config, pump = build_frontend()
    say("frontend built")
    args = make_arg_parser(FlexibleArgumentParser()).parse_args(
        ["--model", "facebook/opt-125m", "--chat-template", CHAT_TEMPLATE]
    )
    app = build_app(args, supported_tasks=("generate",))
    await init_app_state(engine, app.state, args, supported_tasks=("generate",))
    pump_task = asyncio.create_task(pump())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        cases = [
            ("F1_completion_text", "/v1/completions",
             {"model": "facebook/opt-125m", "prompt": "hello world, this is", "max_tokens": 4}),
            ("F2_completion_stream", "/v1/completions",
             {"model": "facebook/opt-125m", "prompt": "a b c d e", "max_tokens": 3, "stream": True}),
            ("F3_completion_n2", "/v1/completions",
             {"model": "facebook/opt-125m", "prompt": "fan out", "max_tokens": 2, "n": 2}),
            ("F4_completion_token_ids", "/v1/completions",
             {"model": "facebook/opt-125m", "prompt": [2, 100, 200, 300], "max_tokens": 2}),
            ("F5_chat", "/v1/chat/completions",
             {"model": "facebook/opt-125m", "messages": [{"role": "user", "content": "hi there"}], "max_tokens": 3}),
            ("F6_chat_stream_usage", "/v1/chat/completions",
             {"model": "facebook/opt-125m", "messages": [{"role": "user", "content": "stream me"}],
              "max_tokens": 3, "stream": True, "stream_options": {"include_usage": True}}),
            ("F7_rejected", "/v1/completions",
             {"model": "facebook/opt-125m", "prompt": "too long", "max_tokens": 100000}),
            ("F8_kv_transfer_params", "/v1/completions",
             {"model": "facebook/opt-125m", "prompt": "prefill side", "max_tokens": 1,
              "kv_transfer_params": {"do_remote_decode": True}}),
        ]
        for name, path, body in cases:
            scenario(name)
            faulthandler.dump_traceback_later(45, exit=False)
            say(name, "post")
            r = await asyncio.wait_for(
                http.post(path, json=body, headers={"X-Request-Id": f"req-{name}"}), 30
            )
            faulthandler.cancel_dump_traceback_later()
            say(name, "status", r.status_code, r.text[:200])
            await asyncio.sleep(0.05)
            RECORDS.append({"scenario": name, "http": {"status": r.status_code,
                                                       "stream": bool(body.get("stream"))}})
    pump_task.cancel()


def main():
    try:
        engine_scenarios()
    except Exception as exc:  # keep going, record the failure
        import traceback

        RECORDS.append({"scenario": SCENARIO[0], "driver_error": traceback.format_exc()})
    try:
        asyncio.run(frontend_scenarios())
    except Exception:
        import traceback

        RECORDS.append({"scenario": SCENARIO[0], "driver_error": traceback.format_exc()})
    with open(OUT, "w") as f:
        for r in RECORDS:
            f.write(json.dumps(r, sort_keys=True, default=str) + "\n")
    print("records", len(RECORDS), "->", OUT)


if __name__ == "__main__":
    main()
