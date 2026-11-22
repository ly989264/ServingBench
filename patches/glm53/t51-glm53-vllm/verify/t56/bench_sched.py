"""Host time of Scheduler.schedule() and update_from_output() per call, T51 vs T56 tree (same script).
usage: python bench_sched.py ROLE OUT.json     ROLE: p (vLLM Scheduler) | d (class in vllm_ascend.core.recompute_scheduler)
64 requests at a time, async remote KV for every request (MockKV, 32 of 48 prompt tokens), 16 output tokens;
new requests replace finished ones; 600 steps after 100 warm-up steps, 5 repetitions. [PD_DIAG] records are
built and formatted as on site, then dropped by a NullHandler.
vllm-ascend's utils module is stood in for as on site: is_pd_decode_recompute_scheduler_enabled() is True for d,
False for p, so the T56 tree takes its D path for d (asserted: _pd_decode). After the timed runs, one untimed
run of 60 steps counts the request records and those that carry stages.kv_ready_wait (written to OUT as check)."""
import json
import logging
import os
import statistics
import sys
import time
import types

sys.path.insert(0, os.getcwd())
from tests.v1.core.utils import create_requests, create_scheduler, mock_kv  # noqa: E402
from tests.v1.kv_connector.unit.utils import create_model_runner_output  # noqa: E402
from vllm.v1.core.sched.scheduler import Scheduler  # noqa: E402
from vllm.v1.outputs import KVConnectorOutput  # noqa: E402
from vllm.v1.request import RequestStatus  # noqa: E402
from vllm.v1.structured_output import StructuredOutputManager  # noqa: E402

ROLE, OUT = sys.argv[1], sys.argv[2]
_utils = types.ModuleType("vllm_ascend.utils")
_utils.is_pd_decode_recompute_scheduler_enabled = lambda vllm_config: ROLE == "d"
sys.modules["vllm_ascend.utils"] = _utils
lg = logging.getLogger("vllm.v1.metrics.pd_diagnostics")
lg.handlers[:] = [logging.NullHandler()]
lg.setLevel(logging.INFO)
lg.propagate = False


def build():
    base = create_scheduler(enable_prefix_caching=False, use_kv_connector=mock_kv(32, True), block_size=16,
                            max_num_seqs=64, max_num_batched_tokens=8192, num_blocks=20000)
    if ROLE == "p":
        return base
    cls = type("AsyncRecomputeScheduler", (Scheduler,), {})
    cls.__module__ = "vllm_ascend.core.recompute_scheduler"
    s = cls(vllm_config=base.vllm_config, kv_cache_config=base.kv_cache_config, block_size=16, log_stats=True,
            structured_output_manager=StructuredOutputManager(base.vllm_config))
    s.use_v2_model_runner = base.use_v2_model_runner
    return s


PD_DECODE = []


def run(steps=int(os.environ.get("BENCH_STEPS", 700)), warm=int(os.environ.get("BENCH_WARM", 100))):
    s = build()
    PD_DECODE.append(getattr(s, "_pd_decode", None))  # None: the T51 tree has no such attribute
    counter = [0]

    def add(n):
        for r in create_requests(num_requests=n, num_tokens=48, max_tokens=16, block_size=16,
                                 req_ids=[f"q{counter[0] + i}" for i in range(n)]):
            s.add_request(r)
        counter[0] += n

    add(64)
    t_sched, t_upd = [], []
    for step in range(steps):
        a = time.perf_counter_ns()
        out = s.schedule()
        b = time.perf_counter_ns()
        waiting = {r.request_id for r in s.requests.values() if r.status == RequestStatus.WAITING_FOR_REMOTE_KVS
                   and r.request_id not in s.finished_recving_kv_req_ids}
        reqs = [s.requests[rid] for rid in out.num_scheduled_tokens]
        mro = create_model_runner_output(reqs, token_id=5)
        mro.kv_connector_output = KVConnectorOutput(finished_recving=waiting or None)
        c = time.perf_counter_ns()
        s.update_from_output(out, mro)
        d = time.perf_counter_ns()
        done = 64 - sum(1 for r in s.requests.values() if not r.is_finished())
        if done > 0:
            add(done)
        if step >= warm:
            t_sched.append(b - a)
            t_upd.append(d - c)
    return statistics.median(t_sched), statistics.median(t_upd), counter[0]


reps = [run() for _ in range(int(os.environ.get("BENCH_REPS", 5)))]
assert len(set(PD_DECODE)) == 1, PD_DECODE
if PD_DECODE[0] is not None:  # T56 tree
    assert PD_DECODE[0] is (ROLE == "d"), (ROLE, PD_DECODE)

COUNT = {"request_records": 0, "with_kv_ready_wait": 0, "kv_ready_wait_ok": 0}


class Count(logging.Handler):
    def emit(self, record):
        msg = record.getMessage()
        if not msg.startswith("[PD_DIAG] "):
            return
        values = json.loads(msg[len("[PD_DIAG] "):])
        if values.get("scope") != "request":
            return
        COUNT["request_records"] += 1
        stage = (values.get("stages") or {}).get("kv_ready_wait")
        if stage is not None:
            COUNT["with_kv_ready_wait"] += 1
            COUNT["kv_ready_wait_ok"] += stage.get("status") == "ok"


lg.handlers[:] = [Count()]
run(steps=60, warm=59)  # one timed sample, unused
json.dump({"role": ROLE, "schedule_ns": [r[0] for r in reps], "update_ns": [r[1] for r in reps],
           "requests": reps[0][2], "pd_decode": PD_DECODE[0], "check": COUNT}, open(OUT, "w"))
print(ROLE, "schedule median per rep", [r[0] for r in reps], "update", [r[1] for r in reps], "requests", reps[0][2],
      "pd_decode", PD_DECODE[0], "check", COUNT)
