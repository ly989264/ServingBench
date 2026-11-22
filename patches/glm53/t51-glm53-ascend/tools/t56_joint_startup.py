"""The seam between the two patches: patch 01 prints the Worker startup line, vllm-ascend decides what goes in it.

Each side's own tests pass with either half wrong, so this drives patch 01's real `_pd_startup_log_timing` with a
real vllm-ascend `DeviceTiming` and compares the whole line, for all three cases the T56 convention names. It also
checks the four `runner.*` spans patch 01 registers, and vllm-ascend's own decode-node condition through patch 01's
`is_pd_decode`.

    PYTHONPATH=<vllm tree with patch 01's T56 part>:<vllm-ascend tree> python3 tools/t56_joint_startup.py

Prints one line per case and exits non-zero if any case does not match.
"""

from __future__ import annotations

import importlib
import logging
import sys
import types
from types import SimpleNamespace

T51_LINE = "PD_DIAG_STARTUP worker device_timing=on tp_rank=0"
ALL_SIX = "forward,sampling,prepare_inputs,lm_head,draft,bookkeeping"


class Lines(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def startup_line(worker_base, timing, tp_rank=0):
    """What patch 01 would log for a worker whose runner carries this DeviceTiming."""
    worker_base._PD_STARTUP_TIMING_LOGGED = False
    sink = Lines()
    logger = worker_base.logger
    logger.addHandler(sink)
    level = logger.level
    logger.setLevel(logging.INFO)
    try:
        worker_base._pd_startup_log_timing(
            SimpleNamespace(model_runner=SimpleNamespace(_pd_device_timing=timing)),
            {"tp_rank": tp_rank},
            SimpleNamespace(total_num_scheduled_tokens=1),
        )
    finally:
        logger.removeHandler(sink)
        logger.setLevel(level)
    return [line for line in sink.lines if line.startswith("PD_DIAG_STARTUP")]



def for_runner_case(diag, pdt, au):
    """The wiring DeviceTiming.for_runner does for itself: tp_rank out of the diag scope, vllm_config off the
    runner, and the gate over both. Everything else here builds DeviceTiming by hand, so this is the one place
    that covers how the runner actually gets one. torch.npu is stubbed; nothing is recorded."""
    import torch

    saved_npu = getattr(torch, "npu", None)
    saved_config = au.get_ascend_config
    torch.npu = SimpleNamespace(
        Event=lambda enable_timing=False: SimpleNamespace(record=lambda stream: None, query=lambda: True,
                                                          elapsed_time=lambda other: 0.0),
        current_stream=lambda: object(),
        is_current_stream_capturing=lambda: False,
    )
    au.get_ascend_config = lambda: SimpleNamespace(
        scheduler_config=SimpleNamespace(recompute_scheduler_enable=True))
    try:
        results = {}
        for name, role in (("decode", "kv_consumer"), ("prefill", "kv_producer")):
            runner = SimpleNamespace(vllm_config=SimpleNamespace(kv_transfer_config=SimpleNamespace(
                is_kv_consumer=role == "kv_consumer", is_kv_producer=role == "kv_producer")))
            with diag.scope(tp_rank=0, execution_id="e-wiring"):
                results[name] = pdt._timing(runner, create=True)
        return results
    finally:
        au.get_ascend_config = saved_config
        if saved_npu is None:
            del torch.npu
        else:
            torch.npu = saved_npu


def main() -> int:
    diag = importlib.import_module("vllm.v1.metrics.pd_diagnostics")
    worker_base = importlib.import_module("vllm.v1.worker.worker_base")
    pdt = importlib.import_module("vllm_ascend.worker.pd_device_timing")
    failures = 0

    # 1. The spans patch 01 registers, which vllm-ascend's four stages are written through.
    expected_spans = {"runner." + name: name for name in pdt.OPTIONAL_STAGES}
    got_spans = {key: diag._SPANS.get(key) for key in expected_spans}
    if got_spans == expected_spans:
        print(f"OK   spans: {', '.join(sorted(expected_spans))}")
    else:
        failures += 1
        print(f"FAIL spans: {got_spans} != {expected_spans}")

    # 2. is_pd_decode, over vllm-ascend's own condition.
    sys.modules.setdefault("torch_npu", types.ModuleType("torch_npu"))
    try:
        import vllm_ascend.utils as au
    except Exception as exc:
        print(f"SKIP is_pd_decode: vllm_ascend.utils not importable: {type(exc).__name__}: {exc}")
        au = None
    if au is not None:
        saved = au.get_ascend_config
        for name, role, recompute, expected in (
            ("kv_consumer + recompute on", "kv_consumer", True, True),
            ("kv_producer + recompute on", "kv_producer", True, False),
            ("kv_consumer + recompute off", "kv_consumer", False, False),
        ):
            au.get_ascend_config = lambda value=recompute: SimpleNamespace(
                scheduler_config=SimpleNamespace(recompute_scheduler_enable=value))
            config = SimpleNamespace(kv_transfer_config=SimpleNamespace(
                is_kv_consumer=role == "kv_consumer", is_kv_producer=role == "kv_producer"))
            got = diag.is_pd_decode(config)
            if got is expected:
                print(f"OK   is_pd_decode: {name} -> {got}")
            else:
                failures += 1
                print(f"FAIL is_pd_decode: {name} -> {got}, expected {expected}")
        au.get_ascend_config = saved
        # Without vllm_ascend loaded at all it must say no, not raise.
        module = sys.modules.pop("vllm_ascend.utils")
        try:
            got = diag.is_pd_decode(None)
        finally:
            sys.modules["vllm_ascend.utils"] = module
        if got is False:
            print("OK   is_pd_decode: vllm_ascend not loaded -> False")
        else:
            failures += 1
            print(f"FAIL is_pd_decode: vllm_ascend not loaded -> {got}")

    # 3. The line itself, for the three cases the convention names.
    def timing(stages, source, decode_node=True, enabled=True):
        return pdt.DeviceTiming(enabled=enabled, stages=stages, stages_source=source, decode_node=decode_node)

    cases = (
        ("D, switch unset", timing(pdt.OPTIONAL_STAGES, "default"),
         0, [f"{T51_LINE} runner_stages={ALL_SIX} source=default"]),
        ("D, VLLM_PD_DEVICE_STAGES=none", timing((), "env"),
         0, [f"{T51_LINE} runner_stages=forward,sampling source=env"]),
        ("D, a subset", timing(("lm_head", "draft"), "env"),
         0, [f"{T51_LINE} runner_stages=forward,sampling,lm_head,draft source=env"]),
        ("not a decode node (P, or the gate said no)", timing(pdt.OPTIONAL_STAGES, "default", decode_node=False),
         0, [T51_LINE]),
        ("tp_rank 1: device timing off", timing(pdt.OPTIONAL_STAGES, "default", enabled=False),
         1, ["PD_DIAG_STARTUP worker device_timing=off tp_rank=1"]),
    )
    for name, value, tp_rank, expected in cases:
        got = startup_line(worker_base, value, tp_rank)
        if got == expected:
            print(f"OK   startup line, {name}: {got[0]}")
        else:
            failures += 1
            print(f"FAIL startup line, {name}: {got} != {expected}")

    # 4. The wiring from the runner to a DeviceTiming, which nothing else here covers.
    if au is not None:
        try:
            built = for_runner_case(diag, pdt, au)
        except Exception as exc:
            failures += 1
            built = None
            print(f"FAIL for_runner: {type(exc).__name__}: {exc}")
        if built is not None:
            decode, prefill = built["decode"], built["prefill"]
            if (decode.decode_node and decode.stages == frozenset(pdt.OPTIONAL_STAGES)
                    and decode.runner_stages == tuple(ALL_SIX.split(","))):
                print(f"OK   for_runner: kv_consumer runner -> runner_stages={','.join(decode.runner_stages)}")
            else:
                failures += 1
                print(f"FAIL for_runner: kv_consumer runner -> decode_node={decode.decode_node} "
                      f"stages={sorted(decode.stages)} runner_stages={decode.runner_stages}")
            if not prefill.decode_node and not prefill.stages and prefill.runner_stages == ():
                print("OK   for_runner: kv_producer runner -> no stages, no runner_stages")
            else:
                failures += 1
                print(f"FAIL for_runner: kv_producer runner -> decode_node={prefill.decode_node} "
                      f"stages={sorted(prefill.stages)} runner_stages={prefill.runner_stages}")

    print(f"{'all cases' if not failures else 'NOT all cases'} as the convention asks")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
