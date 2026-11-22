"""What the T56 stages put into real [PD_DIAG] worker_summary records, with the real vLLM machinery.

The vLLM side is the patched tree (diag.Batch, diag.span, worker_base's claim and record wrapper); only the NPU
events are stubs on a scripted timeline. The vLLM patch's own T56 part (is_pd_decode plus the four runner.* spans)
may not be in the tree yet, so this script installs it when it is missing and says so -- that part is 01's, and
this check is about what vllm-ascend writes once it is there.

    PYTHONPATH=<vllm tree>:<vllm-ascend tree> python3 tools/t56_stage_records.py

Prints one line per case and exits non-zero if any case does not match what the T56 convention asks for.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
from types import SimpleNamespace

MS = 1_000_000
STREAM = object()
CASES = (
    # (name, env, decode node, stages expected in execute/sample, record-level fields expected)
    ("decode node, switch unset", {}, True,
     ({"prepare_inputs", "forward", "lm_head"}, {"sampling", "bookkeeping", "draft"}),
     ({"cudagraph_runtime_mode"}, {"step_device_span_ns"})),
    ("decode node, VLLM_PD_DEVICE_STAGES=none", {"VLLM_PD_DEVICE_STAGES": "none"}, True,
     ({"forward"}, {"sampling"}), (set(), set())),
    ("decode node, VLLM_PD_DEVICE_STAGES=lm_head,draft", {"VLLM_PD_DEVICE_STAGES": "lm_head,draft"}, True,
     ({"forward", "lm_head"}, {"sampling", "draft"}), ({"cudagraph_runtime_mode"}, set())),
    ("prefill node (is_pd_decode false), switch unset", {}, False,
     ({"forward"}, {"sampling"}), (set(), set())),
    ("prefill node, every stage named anyway", {"VLLM_PD_DEVICE_STAGES": "prepare_inputs,lm_head,draft,bookkeeping"},
     False, ({"forward"}, {"sampling"}), (set(), set())),
)


class Device:
    def __init__(self):
        self.now_ms = 0.0
        self.completed = -1
        self.count = 0

    def advance(self, ms):
        self.now_ms += ms

    def complete_all(self):
        self.completed = self.count

    def event(self):
        return Event(self)


class Event:
    def __init__(self, device):
        self.device = device
        self.index = None
        self.ts = None

    def record(self, stream):
        assert stream is STREAM
        self.device.count += 1
        self.index = self.device.count
        self.ts = self.device.now_ms

    def query(self):
        return self.index is not None and self.index <= self.device.completed

    def elapsed_time(self, other):
        return other.ts - self.ts


class Records(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[dict] = []

    def emit(self, record):
        message = record.getMessage()
        if "[PD_DIAG] " in message:
            self.records.append(json.loads(message.split("[PD_DIAG] ", 1)[1]))


def install_vllm_t56_part(diag) -> bool:
    """01's part of T56. The spans are added only when the tree does not have them yet; is_pd_decode is always
    replaced, so the cases below can pick the node kind (the real condition is exercised by real_gate()).
    True when this script had to add the spans, i.e. the tree is still the T51 one."""
    missing = not hasattr(diag, "is_pd_decode") or "runner.lm_head" not in diag._SPANS
    for name in ("prepare_inputs", "lm_head", "draft", "bookkeeping"):
        diag._SPANS.setdefault("runner." + name, name)
    diag.is_pd_decode = lambda vllm_config=None: _DECODE[0]
    return missing


_DECODE = [True]


def run_case(diag, worker_base, pdt, env, decode):
    """One step, the way the runner measures it, inside the real worker_summary records."""
    os.environ.pop("VLLM_PD_DEVICE_STAGES", None)
    os.environ.update(env)
    _DECODE[0] = decode
    device = Device()
    stages, source, decode = pdt.DeviceTiming.switches(diag, None)
    timing = pdt.DeviceTiming(
        enabled=True,
        new_event=device.event,
        current_stream=lambda: STREAM,
        capturing=lambda: False,
        claim_finish=worker_base.pd_claim_finish,
        current_context=diag.current,
        current_batch=diag.current_batch,
        stages=stages,
        stages_source=source,
        decode_node=decode,
    )
    runner = SimpleNamespace(_pd_device_timing=timing, vllm_config=None)
    context = {"execution_id": "e-t56", "request_ids": ["r1"], "tp_rank": 0, "pp_rank": 0, "dp_rank": 0,
               "request_id": None, "runner": "test", "timing_domain": "host_monotonic"}
    with diag.scope(**context), worker_base._pd_worker_batch("execute", context):
        timing.begin_execute()
        with pdt.stage(runner, "prepare_inputs"):
            device.advance(1)
        with diag.span("runner.forward", completion="host_return"), timing.measure("forward"):
            device.advance(2)
        with pdt.stage(runner, "lm_head"):
            device.advance(3)
        pdt.note_cudagraph_mode(runner, SimpleNamespace(runtime_mode="FULL"), None)
    with diag.scope(**context), worker_base._pd_worker_batch("sample", context):
        timing.begin_sample()
        with diag.span("runner.sampling", algorithm="rejection", completion="host_return"), timing.measure("sampling"):
            device.advance(4)
        with pdt.stage(runner, "bookkeeping"):
            device.advance(5)
        with pdt.stage(runner, "draft"):
            device.advance(6)
        device.complete_all()
        timing.end_sample(read_now=True)
    return timing



def real_gate(diag, worker_base, pdt):
    """vllm-ascend's own condition, the one patch 01's is_pd_decode() forwards to. Includes the case the delivery
    review asks for: a prefill node that has recompute_scheduler_enable configured anyway."""
    import types

    sys.modules.setdefault("torch_npu", types.ModuleType("torch_npu"))  # vllm_ascend.utils imports it at module level
    try:
        import vllm_ascend.utils as au
    except Exception as exc:
        print(f"SKIP real gate: {type(exc).__name__}: {exc}")
        return 1, None
    from types import SimpleNamespace as NS

    saved = au.get_ascend_config
    failures = 0
    cases = (
        ("kv_consumer, recompute on  -> decode node", "kv_consumer", True, True),
        ("kv_producer, recompute on  -> NOT a decode node", "kv_producer", True, False),
        ("kv_consumer, recompute off -> NOT a decode node", "kv_consumer", False, False),
    )
    producer_config = None
    for name, role, recompute, expected in cases:
        au.get_ascend_config = lambda value=recompute: NS(scheduler_config=NS(recompute_scheduler_enable=value))
        config = NS(kv_transfer_config=NS(is_kv_consumer=role == "kv_consumer", is_kv_producer=role == "kv_producer"))
        got = au.is_pd_decode_recompute_scheduler_enabled(config)
        if role == "kv_producer":
            producer_config = config
        if got is not expected:
            failures += 1
            print(f"FAIL real gate: {name}: got {got}")
        else:
            print(f"OK   real gate: {name}")

    # And the records that prefill node writes, with every switch turned on.
    au.get_ascend_config = lambda: NS(scheduler_config=NS(recompute_scheduler_enable=True))
    saved_is_pd_decode = diag.is_pd_decode
    diag.is_pd_decode = lambda vllm_config=None: au.is_pd_decode_recompute_scheduler_enabled(producer_config)
    try:
        timing = run_case(diag, worker_base, pdt,
                          {"VLLM_PD_DEVICE_STAGES": "prepare_inputs,lm_head,draft,bookkeeping"}, True)
    finally:
        diag.is_pd_decode = saved_is_pd_decode
        au.get_ascend_config = saved
    return failures, timing


def main() -> int:
    diag = importlib.import_module("vllm.v1.metrics.pd_diagnostics")
    worker_base = importlib.import_module("vllm.v1.worker.worker_base")
    pdt = importlib.import_module("vllm_ascend.worker.pd_device_timing")
    added = install_vllm_t56_part(diag)
    print("vLLM tree: " + ("without patch 01's T56 part (spans simulated here)" if added
                           else "with patch 01's T56 part (real spans)"))
    sink = Records()
    diag._LOG.addHandler(sink)
    diag._LOG.setLevel(logging.INFO)
    failures = 0
    for name, env, decode, (execute_stages, sample_stages), (execute_fields, sample_fields) in CASES:
        del sink.records[:]
        timing = run_case(diag, worker_base, pdt, env, decode)
        got = {record.get("phase"): record for record in sink.records if record.get("event") == "worker_summary"}
        problems = []
        for phase, stages, fields in (("execute", execute_stages, execute_fields),
                                      ("sample", sample_stages, sample_fields)):
            record = got.get(phase)
            if record is None:
                problems.append(f"no {phase} record")
                continue
            if set(record.get("stages", {})) != stages:
                problems.append(f"{phase} stages {sorted(record.get('stages', {}))} != {sorted(stages)}")
            extra = {key for key in ("cudagraph_runtime_mode", "step_device_span_ns") if key in record}
            if extra != fields:
                problems.append(f"{phase} fields {sorted(extra)} != {sorted(fields)}")
            for stage, values in record.get("stages", {}).items():
                if values.get("device_duration_ns") is None:
                    problems.append(f"{phase}.{stage} has no device value")
        if not problems:
            span = got.get("sample", {}).get("step_device_span_ns")
            print(f"OK   {name}: runner_stages={','.join(timing.runner_stages) or '-'}"
                  f" source={timing.stages_source}" + (f" step_device_span_ns={span}" if span else ""))
        else:
            failures += 1
            print(f"FAIL {name}: " + "; ".join(problems))
    del sink.records[:]
    gate_failures, timing = real_gate(diag, worker_base, pdt)
    failures += gate_failures
    if timing is not None:
        got = {record.get("phase"): record for record in sink.records if record.get("event") == "worker_summary"}
        problems = []
        for phase, stages in (("execute", {"forward"}), ("sample", {"sampling"})):
            record = got.get(phase, {})
            if set(record.get("stages", {})) != stages:
                problems.append(f"{phase} stages {sorted(record.get('stages', {}))} != {sorted(stages)}")
            extra = {key for key in ("cudagraph_runtime_mode", "step_device_span_ns") if key in record}
            if extra:
                problems.append(f"{phase} has {sorted(extra)}")
        if timing.runner_stages:
            problems.append(f"runner_stages={timing.runner_stages} on a node that is not a decode node")
        if problems:
            failures += 1
            print("FAIL real gate: records of the kv_producer node: " + "; ".join(problems))
        else:
            print("OK   real gate: the kv_producer node writes T51 records and no runner_stages, every stage named")
    print(f"{'all cases' if not failures else 'NOT all cases'} as the convention asks")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
