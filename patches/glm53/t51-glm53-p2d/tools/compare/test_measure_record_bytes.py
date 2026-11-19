"""Bytes of the PD_DIAG records D writes per request through MooncakeConnectorV1, with 1 and with 2 READ tasks.

Copied next to test_mooncake_connector_pd_diag.py and run with pytest; writes $P2D_OUT/record_bytes.json.
Measured: the logged message "[PD_DIAG] <json>" in UTF-8, and the same message formatted by vLLM's own log
formatter (the handler vLLM installs on the "vllm" logger) plus the newline. Process prefixes added by vLLM's
multiprocess launch (e.g. "(EngineCore_DP0 pid=...)") are not included.

Field lengths are set to site-like values, so the numbers do not depend on the test container:
request ids as vLLM v0.30 forms them on D ("chatcmpl-" + 36-char uuid + "-" + 8 random characters), the
execution id format of patch 01 with a 7-digit pid, host name 20 characters, pid and native thread ids 7
digits, monotonic clock around 20 days of uptime, a 2 ms READ, block lengths of 1 MiB. The diagnostic context
around the writes is the one t51-glm53-vllm d3e84e2 sets: schedule()'s identity scope for the consumer link,
and pd_execution's scope for start_load_kv, whose request_ids lists every request scheduled in that step
(step_requests); task records copy that context, as in hybrid.
"""

import json
import logging
import os
import threading
import uuid

import pytest
import test_mooncake_connector_pd_diag as h
from vllm.v1.metrics import pd_diagnostics as diag

RUN_ID = uuid.UUID(int=0x0123456789ABCDEF0123456789ABCDEF).hex
HOST = "d-node-0123456789abc"[:20]
PID = 1234567


def site_request_id(n):
    return f"chatcmpl-{uuid.UUID(int=n)}-{n:08x}"


class _Raw(logging.Handler):
    def __init__(self, formatter):
        super().__init__(level=logging.INFO)
        self.formatter_for_size = formatter
        self.items = []

    def emit(self, record):
        message = record.getMessage()
        if message.startswith("[PD_DIAG] "):
            formatted = self.formatter_for_size.format(record) if self.formatter_for_size else message
            self.items.append((json.loads(message[len("[PD_DIAG] "):]), message, formatted))


@pytest.fixture
def site_like(monkeypatch):
    monkeypatch.setattr(diag, "_RUN", RUN_ID)
    monkeypatch.setattr(diag, "_HOST", HOST)
    monkeypatch.setattr(os, "getpid", lambda: PID)
    monkeypatch.setattr(threading, "get_native_id", lambda: 2345678)
    clock = h.FakeClock()
    clock.now = 1_728_000_000_000_000  # 20 days of uptime
    monkeypatch.setattr(h.time, "monotonic_ns", clock.monotonic_ns)
    return clock


def _vllm_formatter():
    for handler in logging.getLogger("vllm").handlers:
        if handler.formatter is not None:
            return handler.formatter
    return None


def scheduler_context(execution_id):
    """The scope EngineCore's schedule() holds while build_connector_meta runs (t51-glm53-vllm d3e84e2)."""
    return dict(request_id=None, observation_id=None, external_req_id=None, parent_request_id=None,
                api_request_id=None, prompt_index=None, execution_id=execution_id, request_ids=[], engine_id="1",
                batch_shared=True, dp_rank=0, kv_role="kv_both", role="unknown")


def worker_context(execution_id, step_requests):
    """The scope pd_execution sets around execute_model, where start_load_kv runs (d3e84e2 worker_base)."""
    return {"execution_id": execution_id, "request_id": None,
            "request_ids": [site_request_id(1000 + k) for k in range(step_requests)], "tp_rank": 0, "pp_rank": 0,
            "dp_rank": 0, "runner": "vllm_ascend.worker.model_runner_v1.NPUModelRunner", "timing_domain": "host_monotonic"}


@pytest.mark.parametrize("step_requests", [0, 64, 256])
@pytest.mark.parametrize("tasks", [1, 2])
def test_record_bytes(site_like, tasks, step_requests):
    clock = site_like
    formatter = _vllm_formatter()
    raw = _Raw(formatter)
    diag._LOG.setLevel(logging.INFO)
    diag._LOG.addHandler(raw)
    try:
        ports = [30000 + k for k in range(tasks)]
        engine = h.ClockEngine(clock, {p: 2_000_000 for p in ports})
        receiver = h.make_receiver(engine=engine)
        receiver.block_len_per_addr = [[1 << 20, 1 << 20]]
        receiver._send_done_recv_signal = lambda *args: clock.advance(50_000)
        worker = h.make_worker(receiver, ports)
        worker._get_kv_split_metadata.return_value = ([ports], [([1, 2],)], [([3, 4],)])
        for port in ports:
            receiver.remote_block_stride_per_addr[h.REMOTE_ENGINE][port] = [[1 << 20, 1 << 20]]
        receiver.block_stride_per_addr = [[1 << 20, 1 << 20]]
        d_request, p_request = site_request_id(7), site_request_id(9)
        execution_id = f"execution-{RUN_ID}-{PID}-{1234567}"
        scheduler = h.make_scheduler()
        scheduler.engine_id = "1"
        request = h.MockRequest(d_request, kv_transfer_params=dict(h.remote_params(p_request), remote_engine_id="0"))
        scheduler._reqs_need_recv[d_request] = (request, ([1, 2],), ([1, 2],), 32)
        with diag.scope(**scheduler_context(execution_id)):
            meta = scheduler.build_connector_meta(type("Out", (), {"pd_execution_id": execution_id})())
        for k in range(tasks):
            with receiver.remote_metadata_lock:
                receiver.kv_caches_base_addr["0"][ports[k]] = h.REMOTE_ADDRS
                receiver.remote_te_port["0"][ports[k]] = ports[k] + h.TE_PORT_OFFSET
                receiver.remote_block_stride_per_addr["0"][ports[k]] = [[1 << 20, 1 << 20]]
                receiver.remote_kv_group2layeridx["0"][ports[k]] = receiver.kv_group2layeridx
        with diag.scope(**worker_context(execution_id, step_requests)):
            worker.start_load_kv(meta)
        receiver.task_tracker.add_req_to_process(d_request)
        for item in h.drain(receiver):
            receiver._handle_request(item)
            clock.advance(100_000)
    finally:
        diag._LOG.removeHandler(raw)
        receiver.executor.shutdown(wait=True)
    rows = []
    for record, message, formatted in raw.items:
        label = record["scope"] + (f"/{record['transfer_role']}" if record.get("transfer_role") else "")
        rows.append({"record": label, "message_bytes": len(message.encode()), "log_line_bytes": len(formatted.encode()) + 1})
    assert [r["record"] for r in rows] == ["link/consumer"] + ["task"] * tasks + ["request_generation"]
    result = {
        "tasks": tasks,
        "step_requests": step_requests,
        "records": rows,
        "total_message_bytes": sum(r["message_bytes"] for r in rows),
        "total_log_line_bytes": sum(r["log_line_bytes"] for r in rows),
        "log_formatter": None if formatter is None else getattr(formatter, "_fmt", str(formatter)),
        "sample_log_line": raw.items[0][2][:160],
        "assumptions": {
            "d_request_id": d_request, "p_request_id": p_request, "execution_id": execution_id, "host_len": len(HOST),
            "pid": PID, "native_tid_digits": 7, "monotonic_ns_digits": 16, "read_ns": 2_000_000,
            "block_len_bytes": 1 << 20, "segments_per_read": 4,
        },
    }
    out = os.path.join(os.environ.get("P2D_OUT", "/tmp"), "record_bytes.json")
    existing = json.load(open(out)) if os.path.exists(out) else []
    existing = [e for e in existing if (e["tasks"], e["step_requests"]) != (tasks, step_requests)] + [result]
    json.dump(sorted(existing, key=lambda e: (e["tasks"], e["step_requests"])), open(out, "w"), ensure_ascii=False, indent=2)
