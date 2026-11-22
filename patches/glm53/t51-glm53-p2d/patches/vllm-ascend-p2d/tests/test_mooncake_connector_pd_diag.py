"""PD_DIAG records written by MooncakeConnectorV1 (pd_transfer_summary link / task / request_generation).

Place at tests/ut/distributed/kv_transfer/kv_p2p/. Needs vllm.v1.metrics.pd_diagnostics
(the vLLM diagnostics patch) and runs on CPU like the other kv_p2p unit tests.
"""

import json
import logging
import os
import sys
import threading
import time
import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import torch
from vllm.v1.metrics import pd_diagnostics as diag
from vllm.v1.request import RequestStatus

from vllm_ascend.distributed.kv_transfer.kv_p2p.mooncake_connector import (
    GroupPull,
    KVCacheRecvingThread,
    MooncakeConnectorScheduler,
    MooncakeConnectorWorker,
)

MODULE = sys.modules[KVCacheRecvingThread.__module__]
REMOTE_ENGINE = "prefill-engine"
REMOTE_HOST = "192.0.2.10"
LAYER = "model.layers.0.self_attn"
LOCAL_ADDRS = [[0x1000, 0x2000]]
REMOTE_ADDRS = [[0x3000, 0x4000]]
BLOCK_LENS = [[64, 64]]
LOCAL_STRIDES = [[80, 80]]
REMOTE_STRIDES = [[96, 96]]
TE_PORT_OFFSET = 10000


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.records: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def emit(self, record):
        message = record.getMessage()
        if message.startswith("[PD_DIAG] "):
            with self._lock:
                self.records.append(json.loads(message.split("[PD_DIAG] ", 1)[1]))

    def of(self, scope=None, role=None):
        with self._lock:
            records = list(self.records)
        return [
            r
            for r in records
            if r["event"] == "pd_transfer_summary"
            and (scope is None or r["scope"] == scope)
            and (role is None or r.get("transfer_role") == role)
        ]


@pytest.fixture
def pd_records(request):
    handler = _Capture()
    old_level = diag._LOG.level
    diag._LOG.setLevel(logging.INFO)
    diag._LOG.addHandler(handler)
    try:
        yield handler
    finally:
        diag._LOG.removeHandler(handler)
        diag._LOG.setLevel(old_level)
        dump = os.environ.get("P2D_RECORD_DUMP")  # optional: keep every record for the field check
        if dump:
            with open(dump, "a") as out:
                for record in handler.records:
                    out.write(json.dumps({"scenario": request.node.name, "fake_clock": "clock" in request.fixturenames,
                                          "record": record}) + "\n")


class MockVllmConfig:
    def __init__(self, dp_rank=0):
        self.model_config = MagicMock()
        self.attention_config = types.SimpleNamespace(indexer_kv_dtype="auto", hisparse_config=None)
        self.parallel_config = MagicMock()
        self.cache_config = MagicMock()
        self.kv_transfer_config = MagicMock()
        self.scheduler_config = MagicMock(disable_hybrid_kv_cache_manager=True)
        self.speculative_config = None
        self.model_config.use_mla = False
        self.model_config.is_deepseek_mla = False
        self.model_config.hf_text_config = types.SimpleNamespace(
            num_key_value_heads=1, num_hidden_layers=1, head_dim=16, model_type="qwen2"
        )
        self.model_config.get_total_num_hidden_layers.return_value = 1
        self.parallel_config.tensor_parallel_size = 1
        self.parallel_config.data_parallel_rank = dp_rank
        self.parallel_config.data_parallel_size = 1
        self.parallel_config.data_parallel_size_local = 1
        self.parallel_config.pipeline_parallel_size = 1
        self.parallel_config.data_parallel_rank_local = 0
        self.parallel_config.prefill_context_parallel_size = 1
        self.parallel_config.decode_context_parallel_size = 1
        self.cache_config.block_size = 16
        self.kv_transfer_config.kv_port = 30100
        self.kv_transfer_config.kv_role = "kv_consumer"
        self.kv_transfer_config.engine_id = "decode-engine"
        self.kv_transfer_config.get_from_extra_config = MagicMock(side_effect=lambda key, default: default)


class MockRequest:
    def __init__(self, request_id, kv_transfer_params=None, status=None, prompt_len=4):
        self.request_id = request_id
        self.prompt_token_ids = list(range(prompt_len))
        self.kv_transfer_params = kv_transfer_params or {}
        self.status = status or "running"
        self.output_token_ids = [101]
        self.num_prompt_tokens = prompt_len
        self._all_token_ids = list(self.prompt_token_ids)
        self.max_tokens = 8
        self.prompt_embeds = None


class MockKVCacheConfig:
    def __init__(self):
        self.kv_cache_groups = [types.SimpleNamespace(layer_names=[LAYER], kv_cache_spec=MagicMock())]
        self.num_blocks = 10


def make_scheduler(dp_rank=0):
    with (
        patch.object(MODULE, "init_ascend_config"),
        patch.object(MODULE, "get_ascend_config", return_value=MagicMock()),
    ):
        return MooncakeConnectorScheduler(MockVllmConfig(dp_rank), "decode-engine", MockKVCacheConfig())


def remote_params(remote_request_id="prefill-req"):
    return {
        "do_remote_prefill": True,
        "remote_block_ids": ([3, 4],),
        "remote_engine_id": REMOTE_ENGINE,
        "remote_request_id": remote_request_id,
        "remote_host": REMOTE_HOST,
        "remote_port": 30000,
    }


def make_receiver(tp_rank=0, dp_rank=0, engine=None):
    kv_group2layeridx = {0: ({"kv_cache_spec_type": "FullAttentionSpec", "kv_cache_group_id": 0}, [0])}
    receiver = KVCacheRecvingThread(
        tp_rank=tp_rank,
        tp_size=2,
        _prefill_pp_size=1,
        engine=engine or MagicMock(),
        local_engine_id="decode-engine",
        local_handshake_port=30100 + tp_rank,
        side_channel_port=30100,
        local_kv_caches_base_addr=LOCAL_ADDRS,
        block_len_per_addr=BLOCK_LENS,
        block_stride_per_addr=LOCAL_STRIDES,
        vllm_config=MockVllmConfig(dp_rank),
        kv_caches={LAYER: (torch.empty(1), torch.empty(1))},
        kv_group2layeridx=kv_group2layeridx,
        block_size_scale=[[1, 1]],
    )
    receiver._send_done_recv_signal = MagicMock()
    return receiver


def seed_peer(receiver, port):
    """What _get_remote_metadata stores after a GET_META round trip, without a socket."""
    with receiver.remote_metadata_lock:
        receiver.kv_caches_base_addr[REMOTE_ENGINE][port] = REMOTE_ADDRS
        receiver.remote_te_port[REMOTE_ENGINE][port] = port + TE_PORT_OFFSET
        receiver.remote_block_size_scale[REMOTE_ENGINE][port] = [[1, 1]]
        receiver.remote_block_stride_per_addr[REMOTE_ENGINE][port] = REMOTE_STRIDES
        receiver.remote_kv_group2layeridx[REMOTE_ENGINE][port] = receiver.kv_group2layeridx


def make_worker(receiver, ports, tp_rank=0):
    """Worker with the routing helpers fixed: one shard, one READ task per remote port."""
    worker = MooncakeConnectorWorker.__new__(MooncakeConnectorWorker)
    worker.kv_send_thread = None
    worker.kv_recv_thread = receiver
    worker.tp_rank = tp_rank
    worker.pcp_size = 1
    worker.dcp_size = 1
    worker._prefill_tp_size = len(ports)
    worker.kv_group2layeridx = receiver.kv_group2layeridx
    worker.remote_port_send_num = {}
    worker.vllm_config = receiver.vllm_config
    worker._get_sfa_replicate_k_block_ids = MagicMock(return_value=((), ()))
    worker._get_kv_split_metadata = MagicMock(return_value=([list(ports)], [([1, 2],)], [([3, 4],)]))
    worker._get_group_pulls_metadata = MagicMock(
        return_value=[
            [[GroupPull(group_id=0, remote_tp_offset=k, num_group_pulls=1)] for k in range(len(ports))]
        ]
    )
    for port in ports:
        seed_peer(receiver, port)
    return worker


def scheduled_metadata(scheduler, execution_id, requests):
    for request_id, remote_request_id in requests:
        request = MockRequest(request_id, kv_transfer_params=remote_params(remote_request_id))
        scheduler._reqs_need_recv[request_id] = (request, ([1, 2],), ([1, 2],), 32)
        scheduler._reqs_in_batch.add(request_id)
    return scheduler.build_connector_meta(types.SimpleNamespace(pd_execution_id=execution_id))


def drain(receiver):
    items = []
    while not receiver.request_queue.empty():
        items.append(receiver.request_queue.get_nowait())
    return items


class TimedEngine:
    """batch_transfer_sync_read with a fixed duration and return code per remote port."""

    def __init__(self, durations_s=None, returns=None, raises=None, barrier=None):
        self.durations_s = durations_s or {}
        self.returns = returns or {}
        self.raises = raises or {}
        self.barrier = barrier
        self.calls: list[tuple[str, list[int]]] = []
        self.lock = threading.Lock()

    def batch_transfer_sync_read(self, session_id, src_list, dst_list, length_list):
        port = int(session_id.rsplit(":", 1)[1]) - TE_PORT_OFFSET
        with self.lock:
            self.calls.append((session_id, list(length_list)))
        if self.barrier is not None:
            self.barrier.wait(timeout=5)
        time.sleep(self.durations_s.get(port, 0.001))
        if port in self.raises:
            raise self.raises[port]
        return self.returns.get(port, 0)


def port_of(task_record):
    return int(task_record["native_calls"][0]["session_id"].rsplit(":", 1)[1]) - TE_PORT_OFFSET


# 1. Mapping record: request R and execution X (C2), P-side offer.


def test_consumer_link_maps_each_request_to_this_execution(pd_records):
    scheduler = make_scheduler(dp_rank=3)
    with diag.scope(kv_role="kv_consumer"):
        meta = scheduled_metadata(scheduler, "execution-run-1-7", [("decode-a", "prefill-a"), ("decode-b", "prefill-b")])
    links = pd_records.of("link", "consumer")
    assert [r["request_id"] for r in links] == ["decode-a", "decode-b"]
    for link, remote in zip(links, ("prefill-a", "prefill-b")):
        assert link["version"] == 2
        assert link["execution_id"] == "execution-run-1-7"
        assert link["engine_id"] == "decode-engine"
        assert link["dp_rank"] == 3
        assert link["links"] == [{"kind": "producer", "request_id": remote, "engine_id": REMOTE_ENGINE}]
        assert (link["status"], link["complete"], link["incomplete"]) == ("linked", True, False)
        assert link["kv_role"] == "kv_consumer"  # schedule() context fields are kept
        assert "tp_rank" not in link
    assert {rid: m.pd_execution_id for rid, m in meta.requests.items()} == {
        "decode-a": "execution-run-1-7",
        "decode-b": "execution-run-1-7",
    }


def test_consumer_link_without_execution_id_does_not_break_scheduling(pd_records):
    scheduler = make_scheduler()
    request = MockRequest("decode-a", kv_transfer_params=remote_params())
    scheduler._reqs_need_recv["decode-a"] = (request, ([1],), ([1],), 16)
    meta = scheduler.build_connector_meta(types.SimpleNamespace())  # SchedulerOutput without pd_execution_id
    assert meta.requests["decode-a"].pd_execution_id is None
    (link,) = pd_records.of("link", "consumer")
    assert link["execution_id"] is None


def test_task_records_carry_the_link_execution_id(pd_records):
    scheduler = make_scheduler()
    meta = scheduled_metadata(scheduler, "execution-run-1-9", [("decode-a", "prefill-a")])
    engine = TimedEngine()
    receiver = make_receiver(engine=engine)
    try:
        make_worker(receiver, [30000, 30001]).start_load_kv(meta)
        for item in drain(receiver):
            receiver._handle_request(item)
    finally:
        receiver.executor.shutdown(wait=True)
    (link,) = pd_records.of("link", "consumer")
    tasks = pd_records.of("task")
    assert len(tasks) == 2
    assert {t["execution_id"] for t in tasks} == {link["execution_id"]} == {"execution-run-1-9"}
    assert {t["request_id"] for t in tasks} == {link["request_id"]}
    assert {t["remote_request_id"] for t in tasks} == {"prefill-a"}
    assert [t["offset"] for t in tasks] == [0, 1]


def test_producer_offer_on_remote_decode_finish(pd_records):
    scheduler = make_scheduler(dp_rank=1)
    scheduler.engine_id = "prefill-engine"
    scheduler.group_transfer_info = [types.SimpleNamespace(tokens_per_block=16, blocks_per_window=0, is_state_group=False)]
    request = MockRequest(
        "prefill-a", kv_transfer_params={"do_remote_decode": True}, status=RequestStatus.FINISHED_LENGTH_CAPPED
    )
    delay_free, params = scheduler.request_finished(request, ([10],))
    assert delay_free and params["remote_request_id"] == "prefill-a"
    (offer,) = pd_records.of("link", "producer")
    assert offer["request_id"] == "prefill-a"
    assert offer["engine_id"] == "prefill-engine"
    assert offer["dp_rank"] == 1
    assert offer["delayed_free"] is True
    assert offer["links"] == [{"kind": "producer", "request_id": "prefill-a", "engine_id": "prefill-engine"}]
    assert (offer["status"], offer["complete"]) == ("offered", True)
    # Requests that are not handed to a D instance write nothing.
    scheduler.request_finished(MockRequest("plain"), ([1],))
    assert len(pd_records.of("link")) == 1


# 2. Several READs for one request: envelope = first READ start -> last READ end (C6).


def test_envelope_spans_sequential_reads_with_gaps(pd_records):
    ports = [30000, 30001, 30002]
    engine = TimedEngine(durations_s={30000: 0.004, 30001: 0.001, 30002: 0.002})
    receiver = make_receiver(engine=engine)
    try:
        worker = make_worker(receiver, ports)
        meta = scheduled_metadata(make_scheduler(), "execution-run-1-1", [("decode-a", "prefill-a")])
        worker.start_load_kv(meta)
        for item in drain(receiver):
            receiver._handle_request(item)
            time.sleep(0.003)  # gap between READs that the envelope covers and a sum would not
    finally:
        receiver.executor.shutdown(wait=True)
    tasks = pd_records.of("task")
    (summary,) = pd_records.of("request_generation")
    assert pd_records.records[-1]["scope"] == "request_generation"  # written once, after the last task
    assert [port_of(t) for t in tasks] == ports
    calls = [t["native_calls"][0] for t in tasks]
    for task, call in zip(tasks, calls):
        assert task["native_call_count"] == 1
        assert task["transfer_envelope_ns"] == call["duration_ns"] == task["stages"]["wait_for_transfer"]["duration_ns"]
        assert task["stages"]["wait_for_transfer"]["calls"] == 1
        assert task["status"] == "succeeded" and task["transfer_status"] == "succeeded"
        assert call["submitted_bytes"] == task["submitted_bytes"] == task["succeeded_bytes"] > 0
        assert task["request_generation"] == summary["request_generation"]
    envelope = max(c["end_ns"] for c in calls) - min(c["start_ns"] for c in calls)
    assert summary["transfer_envelope_ns"] == envelope
    assert envelope > sum(c["duration_ns"] for c in calls)
    assert summary["timing_scope"] == "native_read_envelope_not_task_sum_or_pure_wait"
    assert (summary["status"], summary["submitted_tasks"], summary["succeeded_tasks"]) == ("succeeded", 3, 3)
    assert summary["submitted_bytes"] == sum(t["submitted_bytes"] for t in tasks)
    assert summary["execution_id"] is None and summary["remote_request_id"] is None and summary["offset"] is None


def test_envelope_of_overlapping_reads_is_not_their_sum(pd_records):
    ports = [30000, 30001, 30002]
    engine = TimedEngine(durations_s={30000: 0.006, 30001: 0.002, 30002: 0.004}, barrier=threading.Barrier(3))
    receiver = make_receiver(engine=engine)
    try:
        worker = make_worker(receiver, ports)
        meta = scheduled_metadata(make_scheduler(), "execution-run-1-2", [("decode-a", "prefill-a")])
        worker.start_load_kv(meta)
        receiver.task_tracker.add_req_to_process("decode-a")
        for item in drain(receiver):
            receiver._submit_request(item)  # different peers: handled in parallel
    finally:
        receiver.executor.shutdown(wait=True)
    tasks = pd_records.of("task")
    (summary,) = pd_records.of("request_generation")
    calls = [t["native_calls"][0] for t in tasks]
    assert sorted(port_of(t) for t in tasks) == ports
    envelope = max(c["end_ns"] for c in calls) - min(c["start_ns"] for c in calls)
    assert summary["transfer_envelope_ns"] == envelope
    assert envelope < sum(c["duration_ns"] for c in calls)
    assert all(t["queue_wait_ns"] >= 0 and t["submitted_ns"] >= t["queued_ns"] for t in tasks)
    assert receiver.task_tracker.get_and_clear_finished_requests() == {"decode-a"}
    assert sum(t["tracker_completed"] for t in tasks) == 1 and summary["tracker_completed"] is True


# 3. READ failure (kv_load_failure_policy=fail acts later in the vLLM scheduler on the invalid blocks).


def test_failed_read_marks_task_failed_and_skips_the_rest(pd_records):
    ports = [30000, 30001]
    engine = TimedEngine(returns={30000: -1})
    receiver = make_receiver(engine=engine)
    try:
        worker = make_worker(receiver, ports)
        meta = scheduled_metadata(make_scheduler(), "execution-run-1-3", [("decode-a", "prefill-a")])
        worker.start_load_kv(meta)
        receiver.task_tracker.add_req_to_process("decode-a")
        for item in drain(receiver):
            receiver._handle_request(item)
    finally:
        receiver.executor.shutdown(wait=True)
    failed, skipped = pd_records.of("task")
    (summary,) = pd_records.of("request_generation")
    assert (failed["status"], failed["error_type"], failed["transfer_status"]) == ("failed", "RuntimeError", "failed")
    assert failed["native_calls"][0]["native_return"] == -1
    assert failed["native_calls"][0]["status"] == "failed"
    assert failed["failed_bytes"] == failed["submitted_bytes"] > 0 and failed["succeeded_bytes"] == 0
    assert failed["stages"]["wait_for_transfer"]["status"] == "failed"
    assert failed["stages"]["wait_for_transfer"]["exceptions"] == 1
    assert (skipped["status"], skipped["error_type"], skipped["transfer_status"]) == ("skipped", None, "not_submitted")
    assert skipped["native_calls"] == [] and skipped["transfer_envelope_ns"] is None
    assert skipped["stages"]["wait_for_transfer"]["calls"] == 0
    # "unmeasured" passed to Batch.add_stage without a duration is written as "missing" (same as hybrid).
    assert skipped["stages"]["wait_for_transfer"]["status"] == "missing"
    assert summary["status"] == "failed"
    assert (summary["failed_tasks"], summary["skipped_tasks"], summary["succeeded_tasks"]) == (1, 1, 0)
    assert summary["transfer_envelope_ns"] == failed["native_calls"][0]["duration_ns"]
    assert len(engine.calls) == 1
    # Business behaviour is unchanged: the request completes with its blocks reported invalid.
    assert receiver.get_and_clear_invalid_block_ids() == {1, 2}
    assert receiver.task_tracker.get_and_clear_finished_requests() == {"decode-a"}
    assert receiver._send_done_recv_signal.call_count == 2


def test_read_exception_is_recorded_on_the_native_call(pd_records):
    engine = TimedEngine(raises={30000: OSError("link down")})
    receiver = make_receiver(engine=engine)
    try:
        worker = make_worker(receiver, [30000])
        worker.start_load_kv(scheduled_metadata(make_scheduler(), "execution-run-1-4", [("decode-a", "prefill-a")]))
        for item in drain(receiver):
            receiver._handle_request(item)
    finally:
        receiver.executor.shutdown(wait=True)
    (task,) = pd_records.of("task")
    (summary,) = pd_records.of("request_generation")
    call = task["native_calls"][0]
    assert (call["status"], call["error_type"], call["native_return"]) == ("failed", "OSError", None)
    assert (task["status"], task["error_type"]) == ("failed", "OSError")
    assert task["associated_stages"]["prepare"]["status"] == "ok"
    assert summary["status"] == "failed" and summary["failed_tasks"] == 1


def test_metadata_failure_closes_open_stage_as_exception(pd_records):
    receiver = make_receiver(engine=TimedEngine())
    receiver._get_remote_metadata = MagicMock(side_effect=RuntimeError("no peer"))
    try:
        worker = make_worker(receiver, [30000])
        with receiver.remote_metadata_lock:
            receiver.kv_caches_base_addr[REMOTE_ENGINE].pop(30000)
        worker.start_load_kv(scheduled_metadata(make_scheduler(), "execution-run-1-5", [("decode-a", "prefill-a")]))
        for item in drain(receiver):
            receiver._handle_request(item)
    finally:
        receiver.executor.shutdown(wait=True)
    (task,) = pd_records.of("task")
    assert (task["status"], task["error_type"]) == ("failed", "RuntimeError")
    stages = task["associated_stages"]
    assert stages["metadata_request"]["status"] == stages["meta_fetch"]["status"] == "exception"
    assert "prepare" not in stages and task["native_calls"] == []


def test_reformat_failure_after_last_pull_fails_that_task(pd_records):
    receiver = make_receiver(engine=TimedEngine())
    receiver._apply_kv_cache_reformat = MagicMock(side_effect=RuntimeError("reformat"))
    try:
        worker = make_worker(receiver, [30000, 30001])
        worker._get_group_pulls_metadata.return_value = [
            [
                [GroupPull(group_id=0, remote_tp_offset=k, num_group_pulls=1, is_group_transfer_end=True)]
                for k in range(2)
            ]
        ]
        worker.start_load_kv(scheduled_metadata(make_scheduler(), "execution-run-1-6", [("decode-a", "prefill-a")]))
        for item in drain(receiver):
            receiver._handle_request(item)
    finally:
        receiver.executor.shutdown(wait=True)
    first, last = pd_records.of("task")
    (summary,) = pd_records.of("request_generation")
    assert first["status"] == "succeeded" and "reformat" not in first["associated_stages"]
    assert (last["status"], last["error_type"]) == ("failed", "RuntimeError")
    assert last["associated_stages"]["reformat"]["status"] == "exception"
    assert summary["status"] == "failed" and receiver.get_and_clear_invalid_block_ids() == {1, 2}


# 4. TP > 1: each rank's receive thread writes its own task records and request summary.


def test_each_tp_rank_writes_its_own_records(pd_records):
    ports = [30000, 30001]
    meta = scheduled_metadata(make_scheduler(dp_rank=2), "execution-run-1-8", [("decode-a", "prefill-a")])
    receivers = [make_receiver(tp_rank=rank, dp_rank=2, engine=TimedEngine()) for rank in (0, 1)]
    try:
        for rank, receiver in enumerate(receivers):
            make_worker(receiver, ports, tp_rank=rank).start_load_kv(meta)
        for receiver in receivers:
            for item in drain(receiver):
                receiver._handle_request(item)
    finally:
        for receiver in receivers:
            receiver.executor.shutdown(wait=True)
    tasks = pd_records.of("task")
    summaries = pd_records.of("request_generation")
    assert sorted((t["tp_rank"], t["offset"]) for t in tasks) == [(0, 0), (0, 1), (1, 0), (1, 1)]
    assert {t["execution_id"] for t in tasks} == {"execution-run-1-8"}
    assert {t["dp_rank"] for t in tasks} == {2}
    assert len({t["task_id"] for t in tasks}) == 4
    assert len({t["connector_id"] for t in tasks}) == 2
    assert sorted(s["tp_rank"] for s in summaries) == [0, 1]
    by_rank = {s["tp_rank"]: s["request_generation"] for s in summaries}
    assert len(set(by_rank.values())) == 2
    assert all(t["request_generation"] == by_rank[t["tp_rank"]] for t in tasks)


# 5. Other paths.


def test_full_prefix_hit_is_a_skipped_task(pd_records):
    engine = TimedEngine()
    receiver = make_receiver(engine=engine)
    try:
        worker = make_worker(receiver, [30000])
        worker._get_kv_split_metadata.return_value = ([[30000]], [([],)], [([3, 4],)])
        worker.start_load_kv(scheduled_metadata(make_scheduler(), "execution-run-1-10", [("decode-a", "prefill-a")]))
        for item in drain(receiver):
            receiver._handle_request(item)
    finally:
        receiver.executor.shutdown(wait=True)
    (task,) = pd_records.of("task")
    (summary,) = pd_records.of("request_generation")
    assert (task["status"], task["transfer_status"]) == ("skipped", "skipped")
    assert task["stages"]["wait_for_transfer"]["calls"] == 0
    assert task["stages"]["wait_for_transfer"]["status"] == "skipped"
    assert task["transfer_envelope_ns"] is None and summary["transfer_envelope_ns"] is None
    assert summary["status"] == "skipped" and engine.calls == []
    assert receiver._send_done_recv_signal.call_count == 1


def test_summary_waits_for_a_slow_done_ack(pd_records):
    receiver = make_receiver(engine=TimedEngine())
    first_waiting = threading.Event()
    release = threading.Event()

    def done_ack(remote_request_id, remote_host, port, *_):
        if port == 30000:
            first_waiting.set()
            assert release.wait(timeout=5)

    receiver._send_done_recv_signal = done_ack
    try:
        make_worker(receiver, [30000, 30001]).start_load_kv(
            scheduled_metadata(make_scheduler(), "execution-run-1-11", [("decode-a", "prefill-a")])
        )
        receiver.task_tracker.add_req_to_process("decode-a")
        first, second = drain(receiver)
        pending = receiver.executor.submit(receiver._handle_request, first)
        assert first_waiting.wait(timeout=5)
        receiver.executor.submit(receiver._handle_request, second).result(timeout=5)
        assert pd_records.of("request_generation") == []
        assert receiver.task_tracker.get_and_clear_finished_requests() == {"decode-a"}
        release.set()
        pending.result(timeout=5)
    finally:
        release.set()
        receiver.executor.shutdown(wait=True)
    tasks = pd_records.of("task")
    assert [t["tracker_completed"] for t in tasks] == [True, False]
    (summary,) = pd_records.of("request_generation")
    assert summary["tracker_completed"] is True and summary["succeeded_tasks"] == 2


def test_virtual_request_writes_link_but_no_read_records(pd_records):
    scheduler = make_scheduler()
    params = remote_params()
    params["do_virtual"] = True
    scheduler._reqs_need_recv["decode-v"] = (MockRequest("decode-v", kv_transfer_params=params), ([1],), ([1],), 16)
    meta = scheduler.build_connector_meta(types.SimpleNamespace(pd_execution_id="execution-run-1-12"))
    receiver = make_receiver(engine=TimedEngine())
    try:
        make_worker(receiver, [30000]).start_load_kv(meta)
        assert receiver.request_queue.empty()
    finally:
        receiver.executor.shutdown(wait=True)
    assert len(pd_records.of("link", "consumer")) == 1
    assert pd_records.of("task") == [] and pd_records.of("request_generation") == []


def test_queue_items_without_diagnostics_are_still_transferred(pd_records):
    engine = TimedEngine()
    receiver = make_receiver(engine=engine)
    seed_peer(receiver, 30000)
    try:
        receiver.request_queue.put(
            {
                "request_id": "raw",
                "remote_request_id": "prefill-raw",
                "local_block_ids": ([1, 2],),
                "remote_block_ids": ([3, 4],),
                "group_pulls": [GroupPull(group_id=0, remote_tp_offset=0, num_group_pulls=1)],
                "remote_engine_id": REMOTE_ENGINE,
                "remote_host": REMOTE_HOST,
                "remote_handshake_port": 30000,
                "remote_port_send_num": {},
                "all_task_done": True,
            }
        )
        receiver._handle_request(receiver.request_queue.get_nowait())
    finally:
        receiver.executor.shutdown(wait=True)
    assert len(engine.calls) == 1 and pd_records.records == []


def test_offsets_number_pulls_across_shards(pd_records):
    receiver = make_receiver(engine=TimedEngine())
    try:
        worker = make_worker(receiver, [30000, 30001, 30002, 30003])
        worker._get_kv_split_metadata.return_value = (
            [[30000, 30001], [30002, 30003]],
            [([1],), ([2],)],
            [([3],), ([4],)],
        )
        worker._get_group_pulls_metadata.return_value = [
            [[GroupPull(group_id=0, remote_tp_offset=k, num_group_pulls=1)] for k in range(2)] for _ in range(2)
        ]
        worker.start_load_kv(scheduled_metadata(make_scheduler(), "execution-run-1-13", [("decode-a", "prefill-a")]))
        for item in drain(receiver):
            receiver._handle_request(item)
    finally:
        receiver.executor.shutdown(wait=True)
    tasks = pd_records.of("task")
    (summary,) = pd_records.of("request_generation")
    assert [(t["offset"], port_of(t), t["final_task"]) for t in tasks] == [
        (0, 30000, False),
        (1, 30001, False),
        (2, 30002, False),
        (3, 30003, True),
    ]
    assert summary["submitted_tasks"] == summary["succeeded_tasks"] == 4


# 6. Exact timing with a controlled clock: every *_ns below is an exact value, not a bound.
#    The clock only moves inside the fake READ, the fake done signal, retry sleeps and the
#    test's own gaps, so the expected envelope can be written down by hand.

import msgspec  # noqa: E402
import zmq  # noqa: E402
from vllm.utils.network_utils import make_zmq_path  # noqa: E402

from vllm_ascend.distributed.kv_transfer.kv_p2p.mooncake_connector import MooncakeAgentMetadata  # noqa: E402

DONE_NS = 10  # time spent sending the done signal to P after each task
GAP_NS = 500  # time between two tasks on the receive side


class FakeClock:
    def __init__(self):
        self.now = 1_000_000

    def monotonic_ns(self):
        return self.now

    def advance(self, ns):
        self.now += ns

    def sleep(self, seconds):
        self.advance(int(round(seconds * 1_000_000_000)))


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(time, "monotonic_ns", fake.monotonic_ns)
    monkeypatch.setattr(time, "sleep", fake.sleep)
    return fake


class ClockEngine:
    """READ that takes exactly durations[port] ns of the fake clock."""

    def __init__(self, clock, durations, returns=None, raises=None):
        self.clock, self.durations = clock, durations
        self.returns, self.raises = returns or {}, raises or {}
        self.calls: list[int] = []

    def batch_transfer_sync_read(self, session_id, src_list, dst_list, length_list):
        port = int(session_id.rsplit(":", 1)[1]) - TE_PORT_OFFSET
        self.calls.append(port)
        self.clock.advance(self.durations[port])
        if port in self.raises:
            raise self.raises[port]
        return self.returns.get(port, 0)


def run_sequential(clock, shards, durations, returns=None, raises=None):
    """One request whose pulls are shards x ports, handled one after another with a fixed gap."""
    ports = [port for shard in shards for port in shard]
    engine = ClockEngine(clock, durations, returns, raises)
    receiver = make_receiver(engine=engine)
    receiver._send_done_recv_signal = lambda *args: clock.advance(DONE_NS)
    try:
        worker = make_worker(receiver, ports)
        worker._get_kv_split_metadata.return_value = (
            [list(shard) for shard in shards],
            [([1],) for _ in shards],
            [([3],) for _ in shards],
        )
        worker._get_group_pulls_metadata.return_value = [
            [[GroupPull(group_id=0, remote_tp_offset=k, num_group_pulls=1)] for k in range(len(shard))]
            for shard in shards
        ]
        worker.start_load_kv(scheduled_metadata(make_scheduler(), "execution-run-1-20", [("decode-a", "prefill-a")]))
        receiver.task_tracker.add_req_to_process("decode-a")
        for item in drain(receiver):
            receiver._handle_request(item)
            clock.advance(GAP_NS)
    finally:
        receiver.executor.shutdown(wait=True)
    return receiver, engine


def test_exact_envelope_over_several_remote_tp_offsets(pd_records, clock):
    t0 = clock.now
    run_sequential(clock, [[30000, 30001, 30002]], {30000: 1000, 30001: 2000, 30002: 3000})
    tasks = pd_records.of("task")
    (summary,) = pd_records.of("request_generation")
    # READ k starts after the previous READs, their done signals and the gaps.
    assert [t["native_calls"][0]["start_ns"] - t0 for t in tasks] == [0, 1510, 4020]
    assert [t["native_calls"][0]["end_ns"] - t0 for t in tasks] == [1000, 3510, 7020]
    assert [t["transfer_envelope_ns"] for t in tasks] == [1000, 2000, 3000]
    assert [t["stages"]["wait_for_transfer"]["duration_ns"] for t in tasks] == [1000, 2000, 3000]
    assert [t["processing_duration_ns"] for t in tasks] == [1010, 2010, 3010]  # READ + done signal
    assert [t["queue_wait_ns"] for t in tasks] == [0, 1510, 4020]
    assert summary["transfer_envelope_ns"] == 7020  # first READ start -> last READ end, not 1000+2000+3000
    assert summary["status"] == "succeeded"


def test_exact_envelope_over_several_shards(pd_records, clock):
    run_sequential(clock, [[30000], [30001]], {30000: 4000, 30001: 1000})
    tasks = pd_records.of("task")
    (summary,) = pd_records.of("request_generation")
    assert [(t["offset"], port_of(t), t["final_task"]) for t in tasks] == [(0, 30000, False), (1, 30001, True)]
    assert summary["transfer_envelope_ns"] == 4000 + DONE_NS + GAP_NS + 1000


def test_exact_envelope_over_shards_and_remote_tp_offsets(pd_records, clock):
    run_sequential(clock, [[30000, 30001], [30002, 30003]], {30000: 100, 30001: 200, 30002: 300, 30003: 400})
    tasks = pd_records.of("task")
    (summary,) = pd_records.of("request_generation")
    assert [t["offset"] for t in tasks] == [0, 1, 2, 3]
    assert summary["transfer_envelope_ns"] == 100 + 200 + 300 + 400 + 3 * (DONE_NS + GAP_NS)
    assert summary["submitted_tasks"] == summary["succeeded_tasks"] == 4


def test_exact_envelope_when_a_read_fails_midway(pd_records, clock):
    t0 = clock.now
    _, engine = run_sequential(clock, [[30000, 30001, 30002]], {30000: 1000, 30001: 2000, 30002: 3000}, returns={30001: -1})
    first, failed, skipped = pd_records.of("task")
    (summary,) = pd_records.of("request_generation")
    assert engine.calls == [30000, 30001]  # the failed READ is not retried; the third task does not read
    assert (first["status"], failed["status"], skipped["status"]) == ("succeeded", "failed", "skipped")
    assert failed["native_calls"][0]["native_return"] == -1
    assert failed["native_calls"][0]["end_ns"] - t0 == 3510
    assert failed["transfer_envelope_ns"] == 2000 and skipped["transfer_envelope_ns"] is None
    assert skipped["processing_duration_ns"] == DONE_NS
    # The envelope ends at the end of the last READ that ran, the failed one.
    assert summary["transfer_envelope_ns"] == 3510
    assert (summary["status"], summary["succeeded_tasks"], summary["failed_tasks"], summary["skipped_tasks"]) == (
        "failed",
        1,
        1,
        1,
    )


def test_exact_envelope_when_a_read_raises_midway(pd_records, clock):
    run_sequential(clock, [[30000, 30001]], {30000: 1000, 30001: 2000}, raises={30001: OSError("reset")})
    first, failed = pd_records.of("task")
    (summary,) = pd_records.of("request_generation")
    call = failed["native_calls"][0]
    assert (call["status"], call["error_type"], call["native_return"], call["duration_ns"]) == ("failed", "OSError", None, 2000)
    assert (failed["status"], failed["error_type"], failed["transfer_status"]) == ("failed", "OSError", "failed")
    assert summary["transfer_envelope_ns"] == 1000 + DONE_NS + GAP_NS + 2000 and summary["status"] == "failed"


# 7. Retries. The connector never repeats a READ (see the two tests above). The only retries are
#    the ZMQ send/receive retries (3 attempts, 0.1 s apart) when fetching P's metadata and when
#    sending the done signal; their time is in associated_stages / processing_duration_ns and never
#    in the READ envelope. hybrid has no retry-count field, so none is written.


def _peer_metadata(receiver):
    return MooncakeAgentMetadata(
        engine_id=REMOTE_ENGINE,
        te_rpc_port=30000 + TE_PORT_OFFSET,
        kv_group2layeridx=receiver.kv_group2layeridx,
        block_size=16,
        kv_caches_base_addr=REMOTE_ADDRS,
        block_size_scale=[[1, 1]],
        num_blocks=10,
        block_lens=BLOCK_LENS,
        block_strides=REMOTE_STRIDES,
    )


def _uncached_peer(receiver, sock):
    with receiver.remote_metadata_lock:
        receiver.kv_caches_base_addr[REMOTE_ENGINE].pop(30000, None)
    receiver.remote_sockets[make_zmq_path("tcp", REMOTE_HOST, 30000)].append(sock)


def test_metadata_send_retry_is_timed_outside_the_envelope(pd_records, clock):
    engine = ClockEngine(clock, {30000: 1000})
    receiver = make_receiver(engine=engine)
    receiver._send_done_recv_signal = lambda *args: clock.advance(DONE_NS)
    sock = MagicMock()
    sock.send.side_effect = [zmq.ZMQError(), None]
    sock.recv.return_value = msgspec.msgpack.encode(_peer_metadata(receiver))
    try:
        make_worker(receiver, [30000]).start_load_kv(
            scheduled_metadata(make_scheduler(), "execution-run-1-21", [("decode-a", "prefill-a")])
        )
        _uncached_peer(receiver, sock)
        with patch.object(MODULE.logger, "warning") as warning:
            for item in drain(receiver):
                receiver._handle_request(item)
    finally:
        receiver.executor.shutdown(wait=True)
    (task,) = pd_records.of("task")
    (summary,) = pd_records.of("request_generation")
    stages = task["associated_stages"]
    assert sock.send.call_count == 2 and "Send failed" in warning.call_args_list[0].args[0]
    assert (stages["metadata_request"]["calls"], stages["metadata_request"]["status"]) == (1, "ok")
    assert stages["metadata_request"]["duration_ns"] == 100_000_000  # one 0.1 s retry wait
    assert stages["meta_fetch"]["duration_ns"] == 100_000_000
    assert task["status"] == "succeeded" and task["transfer_envelope_ns"] == 1000
    assert summary["transfer_envelope_ns"] == 1000
    assert task["processing_duration_ns"] == 100_000_000 + 1000 + DONE_NS


def test_metadata_retries_exhausted_fail_the_task_without_a_read(pd_records, clock):
    engine = ClockEngine(clock, {30000: 1000})
    receiver = make_receiver(engine=engine)
    receiver._send_done_recv_signal = MagicMock()
    sock = MagicMock()
    sock.send.side_effect = zmq.ZMQError()
    try:
        make_worker(receiver, [30000]).start_load_kv(
            scheduled_metadata(make_scheduler(), "execution-run-1-22", [("decode-a", "prefill-a")])
        )
        receiver.task_tracker.add_req_to_process("decode-a")
        _uncached_peer(receiver, sock)
        for item in drain(receiver):
            receiver._handle_request(item)
    finally:
        receiver.executor.shutdown(wait=True)
    (task,) = pd_records.of("task")
    (summary,) = pd_records.of("request_generation")
    stages = task["associated_stages"]
    assert sock.send.call_count == 3 and engine.calls == []
    assert (task["status"], task["error_type"], task["transfer_status"]) == ("failed", "RuntimeError", "not_submitted")
    assert stages["metadata_request"]["status"] == stages["meta_fetch"]["status"] == "exception"
    assert stages["metadata_request"]["duration_ns"] == 200_000_000  # two 0.1 s waits before giving up
    assert task["transfer_envelope_ns"] is None and summary["transfer_envelope_ns"] is None
    assert summary["status"] == "failed" and receiver.get_and_clear_invalid_block_ids() == {1, 2}
    assert receiver._send_done_recv_signal.call_count == 1
    assert receiver.task_tracker.get_and_clear_finished_requests() == {"decode-a"}


def test_done_signal_retry_is_in_processing_time_not_in_the_envelope(pd_records, clock):
    engine = ClockEngine(clock, {30000: 1000})
    receiver = make_receiver(engine=engine)
    del receiver._send_done_recv_signal  # use the real done signal over the fake socket
    sock = MagicMock()
    sock.recv.side_effect = [zmq.ZMQError(), b"ACK"]
    receiver.remote_sockets[make_zmq_path("tcp", REMOTE_HOST, 30000)].append(sock)
    try:
        make_worker(receiver, [30000]).start_load_kv(
            scheduled_metadata(make_scheduler(), "execution-run-1-23", [("decode-a", "prefill-a")])
        )
        for item in drain(receiver):
            receiver._handle_request(item)
    finally:
        receiver.executor.shutdown(wait=True)
    (task,) = pd_records.of("task")
    assert sock.recv.call_count == 2
    assert task["transfer_envelope_ns"] == 1000
    assert task["processing_duration_ns"] == 1000 + 100_000_000


def test_a_retried_request_is_a_new_request_with_its_own_records(pd_records, clock):
    # vLLM v0.30 appends 8 random characters to every arriving request id, so a proxy retry
    # reaches D under a new internal id: its own link, tasks and request_generation.
    engine = ClockEngine(clock, {30000: 1000}, returns={30000: -1})
    receiver = make_receiver(engine=engine)
    receiver._send_done_recv_signal = MagicMock()
    scheduler = make_scheduler()
    try:
        worker = make_worker(receiver, [30000])
        for request_id, execution_id in (("chat-1-aaaaaaaa", "execution-run-1-24"), ("chat-1-bbbbbbbb", "execution-run-1-25")):
            worker.start_load_kv(scheduled_metadata(scheduler, execution_id, [(request_id, "prefill-a")]))
            for item in drain(receiver):
                receiver._handle_request(item)
            engine.returns = {}
    finally:
        receiver.executor.shutdown(wait=True)
    links = pd_records.of("link", "consumer")
    tasks = pd_records.of("task")
    summaries = pd_records.of("request_generation")
    assert [(r["request_id"], r["execution_id"]) for r in links] == [
        ("chat-1-aaaaaaaa", "execution-run-1-24"),
        ("chat-1-bbbbbbbb", "execution-run-1-25"),
    ]
    assert [(t["request_id"], t["status"], t["execution_id"]) for t in tasks] == [
        ("chat-1-aaaaaaaa", "failed", "execution-run-1-24"),
        ("chat-1-bbbbbbbb", "succeeded", "execution-run-1-25"),
    ]
    assert [(s["request_id"], s["status"], s["transfer_envelope_ns"]) for s in summaries] == [
        ("chat-1-aaaaaaaa", "failed", 1000),
        ("chat-1-bbbbbbbb", "succeeded", 1000),
    ]
    assert summaries[0]["request_generation"] != summaries[1]["request_generation"]


# 8. The new optional arguments: without them add_request queues exactly what it did before.


def test_add_request_without_new_arguments_queues_the_original_item(pd_records):
    receiver = make_receiver()
    pulls = [GroupPull(group_id=0, remote_tp_offset=0, num_group_pulls=1)]
    try:
        receiver.add_request(
            request_id="decode-a",
            remote_request_id="prefill-a",
            local_block_ids=([1, 2],),
            remote_block_ids=([3, 4],),
            group_pulls=pulls,
            remote_engine_id=REMOTE_ENGINE,
            remote_host=REMOTE_HOST,
            remote_handshake_port=30000,
            all_task_done=True,
        )
        with diag.scope(execution_id="execution-run-1-26"):  # hybrid-style scope still supplies X
            receiver.add_request(
                request_id="decode-b",
                remote_request_id="prefill-b",
                local_block_ids=([1],),
                remote_block_ids=([3],),
                group_pulls=pulls,
                remote_engine_id=REMOTE_ENGINE,
                remote_host=REMOTE_HOST,
                remote_handshake_port=30000,
                all_task_done=True,
            )
        first, second = drain(receiver)
    finally:
        receiver.executor.shutdown(wait=True)
    first_diag = first.pop("_pd_diag")
    assert first == {
        "request_id": "decode-a",
        "local_block_ids": ([1, 2],),
        "remote_block_ids": ([3, 4],),
        "local_block_ids_replicate_k": tuple(),
        "remote_block_ids_replicate_k": tuple(),
        "group_pulls": pulls,
        "remote_engine_id": REMOTE_ENGINE,
        "remote_request_id": "prefill-a",
        "remote_host": REMOTE_HOST,
        "remote_handshake_port": 30000,
        "num_computed_tokens": 0,
        "remote_port_send_num": {},
        "all_task_done": True,
        "shard_idx": 0,
        "remote_block_size": None,
    }
    assert first_diag["identity"]["offset"] is None and first_diag["identity"]["execution_id"] is None
    assert second["_pd_diag"]["identity"]["execution_id"] == "execution-run-1-26"


def test_empty_descriptor_list_is_no_work_without_a_read(pd_records):
    engine = TimedEngine()
    receiver = make_receiver(engine=engine)
    try:
        worker = make_worker(receiver, [30000])
        worker._get_group_pulls_metadata.return_value = [[[]]]  # nothing to pull: src_list stays empty
        worker.start_load_kv(scheduled_metadata(make_scheduler(), "execution-run-1-27", [("decode-a", "prefill-a")]))
        for item in drain(receiver):
            receiver._handle_request(item)
    finally:
        receiver.executor.shutdown(wait=True)
    (task,) = pd_records.of("task")
    (summary,) = pd_records.of("request_generation")
    assert (task["status"], task["transfer_status"], task["native_calls"]) == ("skipped", "no_work", [])
    assert task["stages"]["wait_for_transfer"]["calls"] == 0
    assert task["stages"]["wait_for_transfer"]["status"] == "skipped"
    assert task["transfer_envelope_ns"] is None and summary["transfer_envelope_ns"] is None
    assert summary["status"] == "skipped" and engine.calls == []
    assert task["associated_stages"]["prepare"]["status"] == "ok"


# 9. Diagnostics never replace a business exception or break scheduling.


def test_finish_task_with_a_popped_summary_does_not_raise(pd_records):
    receiver = make_receiver(engine=TimedEngine())
    try:
        make_worker(receiver, [30000]).start_load_kv(
            scheduled_metadata(make_scheduler(), "execution-run-1-30", [("decode-a", "prefill-a")])
        )
        (item,) = drain(receiver)
        receiver.pd_request_tasks.clear()  # the request summary is already gone
        assert receiver._pd_finish_task(item, "succeeded", None, time.monotonic_ns(), True, open_stage_failed=False) is None
    finally:
        receiver.executor.shutdown(wait=True)
    assert len(pd_records.of("task")) == 1 and pd_records.of("request_generation") == []


def test_a_diagnostics_error_does_not_replace_the_propagating_exception(pd_records):
    receiver = make_receiver(engine=TimedEngine(returns={30000: -1}))  # kv_load_failure_policy=fail path
    receiver._send_done_recv_signal = MagicMock(side_effect=RuntimeError("ack lost"))
    try:
        make_worker(receiver, [30000]).start_load_kv(
            scheduled_metadata(make_scheduler(), "execution-run-1-31", [("decode-a", "prefill-a")])
        )
        (item,) = drain(receiver)
        item["_pd_diag"]["batch"].finish = MagicMock(side_effect=KeyError("diagnostics"))
        with pytest.raises(RuntimeError, match="ack lost"):
            receiver._handle_request(item)
    finally:
        receiver.executor.shutdown(wait=True)
    assert receiver.get_and_clear_invalid_block_ids() == {1, 2}  # the failed READ was still reported


def test_link_records_never_break_scheduling(pd_records, monkeypatch):
    monkeypatch.setattr(MODULE.diag, "Batch", MagicMock(side_effect=RuntimeError("diagnostics down")))
    scheduler = make_scheduler()
    meta = scheduled_metadata(scheduler, "execution-run-1-32", [("decode-a", "prefill-a")])
    assert list(meta.requests) == ["decode-a"] and meta.requests["decode-a"].remote_request_id == "prefill-a"
    scheduler.group_transfer_info = [types.SimpleNamespace(tokens_per_block=16, blocks_per_window=0, is_state_group=False)]
    request = MockRequest(
        "prefill-a", kv_transfer_params={"do_remote_decode": True}, status=RequestStatus.FINISHED_LENGTH_CAPPED
    )
    delay_free, params = scheduler.request_finished(request, ([10],))
    assert delay_free is True and params["remote_request_id"] == "prefill-a"
    assert pd_records.records == []


class CancelledError(BaseException):  # same name as asyncio's and concurrent.futures', different class
    pass


@pytest.mark.parametrize("error", [__import__("asyncio").CancelledError(), CancelledError()], ids=["asyncio", "by_name"])
def test_cancelled_read_is_recorded_as_cancelled(pd_records, error):
    receiver = make_receiver(engine=TimedEngine(raises={30000: error}))
    try:
        make_worker(receiver, [30000]).start_load_kv(
            scheduled_metadata(make_scheduler(), "execution-run-1-33", [("decode-a", "prefill-a")])
        )
        (item,) = drain(receiver)
        with pytest.raises(type(error)):
            receiver._handle_request(item)  # BaseException: not caught by the transfer's except Exception
    finally:
        receiver.executor.shutdown(wait=True)
    (task,) = pd_records.of("task")
    assert (task["status"], task["error_type"]) == ("cancelled", "CancelledError")
    assert (task["native_calls"][0]["status"], task["transfer_status"]) == ("cancelled", "cancelled")
    assert task["associated_stages"]["prepare"]["status"] == "ok"


def test_open_stage_is_failed_only_when_the_task_failed(pd_records):
    receiver = make_receiver(engine=TimedEngine())
    receiver._get_remote_metadata = MagicMock(side_effect=KeyboardInterrupt())  # escapes except Exception
    try:
        worker = make_worker(receiver, [30000, 30001])
        with receiver.remote_metadata_lock:
            receiver.kv_caches_base_addr[REMOTE_ENGINE].pop(30000)
        worker.start_load_kv(scheduled_metadata(make_scheduler(), "execution-run-1-34", [("decode-a", "prefill-a")]))
        first, second = drain(receiver)
        with pytest.raises(KeyboardInterrupt):
            receiver._handle_request(first)
        MODULE._pd_stage_begin(second, "prepare")  # a stage left open on a task that then succeeds
        receiver._pd_finish_task(second, "succeeded", None, time.monotonic_ns(), True, open_stage_failed=False)
    finally:
        receiver.executor.shutdown(wait=True)
    failed, succeeded = pd_records.of("task")
    assert (failed["status"], failed["error_type"]) == ("failed", "KeyboardInterrupt")
    assert failed["associated_stages"]["meta_fetch"]["status"] == "exception"
    assert failed["associated_stages"]["metadata_request"]["status"] == "exception"
    assert succeeded["associated_stages"]["prepare"]["status"] == "ok"


# 10. kv_load_failure_policy=fail end to end: the connector's records and reports on a failed READ, then
#     vLLM's own Scheduler code (v0.30) turning the reported blocks into FINISHED_ERROR. The scheduler object
#     is a stub carrying only what _handle_invalid_blocks and finish_requests read; the two lines of
#     Scheduler.update_from_output that join them (scheduler.py, "if failed_kv_load_req_ids and not
#     self.recompute_kv_load_failures: error_req_ids.update(...)" and finish_requests(error_req_ids,
#     RequestStatus.FINISHED_ERROR)) are repeated here, not called.


class _Queue(list):
    def remove_requests(self, requests):
        for request in requests:
            if request in self:
                self.remove(request)


class _Request:
    def __init__(self, request_id, num_computed_tokens):
        self.request_id = request_id
        self.status = RequestStatus.WAITING_FOR_REMOTE_KVS
        self.num_computed_tokens = num_computed_tokens

    def is_finished(self):
        return RequestStatus.is_finished(self.status)

    def get_finished_reason(self):
        return RequestStatus.get_finished_reason(self.status)


def _scheduler_stub(recompute, request, block_ids):
    from vllm.v1.core.sched.scheduler import Scheduler

    stub = Scheduler.__new__(Scheduler)
    stub.recompute_kv_load_failures = recompute
    stub.block_size = 16
    stub.running = []
    stub.waiting = _Queue()
    stub.skipped_waiting = _Queue([request])
    stub.requests = {request.request_id: request}
    stub.finished_recving_kv_req_ids = set()
    stub.failed_recving_kv_req_ids = set()
    stub.num_waiting_for_streaming_input = 0
    stub.kv_cache_manager = types.SimpleNamespace(
        get_block_ids=lambda request_id: (list(block_ids),),
        block_pool=types.SimpleNamespace(null_block=types.SimpleNamespace(block_id=0)),
        evict_blocks=MagicMock(),
    )
    stub._free_request = MagicMock(return_value=(None, None))
    return Scheduler, stub


@pytest.mark.parametrize("recompute", [False, True], ids=["policy_fail", "policy_recompute"])
def test_failed_read_ends_the_request_with_error_under_policy_fail(pd_records, recompute):
    from vllm.v1.engine import FinishReason

    receiver = make_receiver(engine=TimedEngine(returns={30000: -1}))
    try:
        make_worker(receiver, [30000]).start_load_kv(
            scheduled_metadata(make_scheduler(), "execution-run-1-35", [("decode-a", "prefill-a")])
        )
        receiver.task_tracker.add_req_to_process("decode-a")
        for item in drain(receiver):
            receiver._handle_request(item)
    finally:
        receiver.executor.shutdown(wait=True)
    # What the D worker hands to the scheduler: the request as received, its blocks as invalid.
    invalid = receiver.get_and_clear_invalid_block_ids()
    assert receiver.task_tracker.get_and_clear_finished_requests() == {"decode-a"} and invalid == {1, 2}
    (task,) = pd_records.of("task")
    (summary,) = pd_records.of("request_generation")
    assert (task["status"], summary["status"]) == ("failed", "failed")  # the same whatever the policy

    request = _Request("decode-a", num_computed_tokens=32)  # 32 tokens = the two 16-token blocks 1 and 2
    Scheduler, stub = _scheduler_stub(recompute, request, block_ids=[1, 2])
    failed_kv_load_req_ids = Scheduler._handle_invalid_blocks(stub, invalid, {})
    error_req_ids = set(failed_kv_load_req_ids) if not stub.recompute_kv_load_failures else set()
    finished = Scheduler.finish_requests(stub, error_req_ids, RequestStatus.FINISHED_ERROR) if error_req_ids else []
    if recompute:
        assert failed_kv_load_req_ids == set() and finished == []
        assert stub.failed_recving_kv_req_ids == {"decode-a"} and request.num_computed_tokens == 0
        assert request.status == RequestStatus.WAITING_FOR_REMOTE_KVS  # recomputed later, not failed
    else:
        assert failed_kv_load_req_ids == {"decode-a"} and finished == [request]
        assert request.status == RequestStatus.FINISHED_ERROR
        assert request.get_finished_reason() == FinishReason.ERROR
        assert request not in stub.skipped_waiting and stub._free_request.call_count == 1


# 10. T56: the KV-load envelope (request_generation.kv_load_envelope_ns, task.request_done_ns) and the per-task
#     maxima of the three receive stages (request_generation.task_max_*). The envelope runs from the earliest
#     queued_ns of the request's tasks to the moment the task tracker marked the request done (right after
#     update_done_task_count), which is before the done signal to P; the maxima are merged under the same lock
#     as the task counts.

ENVELOPE_SCOPE = "first_task_queued_to_request_marked_done"
MAX_FIELDS = (
    ("task_max_queue_wait_ns", None),
    ("task_max_meta_fetch_ns", "meta_fetch"),
    ("task_max_prepare_ns", "prepare"),
)


def check_envelope(tasks, summary):
    """The relations that hold for every request (contract 1.4.0): exact recomputation from the task records,
    lower bounds by the native-read envelope and by each task's own stages, upper bound by the last end_ns."""
    first_queued = min(t["queued_ns"] for t in tasks)
    done = [t for t in tasks if t["request_done_ns"] is not None]
    assert len(done) == 1 and done[0]["tracker_completed"] is True
    assert all("request_done_ns" in t for t in tasks)  # null on the other tasks, the key is kept
    envelope = summary["kv_load_envelope_ns"]
    assert envelope == done[0]["request_done_ns"] - first_queued
    assert summary["kv_load_timing_scope"] == ENVELOPE_SCOPE
    if summary["transfer_envelope_ns"] is not None:
        assert envelope >= summary["transfer_envelope_ns"]
    for t in tasks:
        stages = t["associated_stages"]
        own = t["queue_wait_ns"] + sum((stages.get(name) or {}).get("duration_ns") or 0 for name in ("meta_fetch", "prepare"))
        own += t["stages"]["wait_for_transfer"]["duration_ns"] or 0
        assert envelope >= own
    assert envelope <= max(t["end_ns"] for t in tasks) - first_queued
    assert summary["task_max_queue_wait_ns"] == max(t["queue_wait_ns"] for t in tasks)
    for key, stage in MAX_FIELDS[1:]:
        values = [t["associated_stages"][stage]["duration_ns"] for t in tasks if stage in t["associated_stages"]]
        assert summary[key] == (max(values) if values else None)
    for key, _ in MAX_FIELDS:
        assert summary[key] is None or summary[key] <= envelope
    assert "first_queued_ns" not in summary and "request_done_ns" not in summary  # raw moments stay internal
    return envelope


def queue_direct(receiver, clock, ports, gaps_ns, request_id="decode-a", last=True):
    """Queue one task per port with add_request, advancing the clock by gaps_ns[i] before task i."""
    for port in ports:
        seed_peer(receiver, port)
    receiver.task_tracker.add_req_to_process(request_id)
    for i, (port, gap) in enumerate(zip(ports, gaps_ns)):
        clock.advance(gap)
        receiver.add_request(
            request_id=request_id,
            remote_request_id="prefill-a",
            local_block_ids=([1],),
            remote_block_ids=([3],),
            group_pulls=[GroupPull(group_id=0, remote_tp_offset=i, num_group_pulls=1)],
            remote_engine_id=REMOTE_ENGINE,
            remote_host=REMOTE_HOST,
            remote_handshake_port=port,
            all_task_done=last and i == len(ports) - 1,
            pd_execution_id="execution-run-1-56",
            pd_offset=i,
        )


def run_direct(clock, durations, gaps_ns, before_ns, order=None, returns=None, raises=None, done_ns=DONE_NS):
    engine = ClockEngine(clock, durations, returns, raises)
    receiver = make_receiver(engine=engine)
    receiver._send_done_recv_signal = lambda *args: clock.advance(done_ns)
    t0 = clock.now
    try:
        queue_direct(receiver, clock, list(durations), gaps_ns)
        items = drain(receiver)
        for item in items:  # the counting _submit_request does, so the last task to finish completes the request
            receiver._mark_request_task_submitted(item)
        clock.advance(before_ns)
        for i in order or range(len(items)):
            receiver._handle_request(items[i])
            clock.advance(GAP_NS)
    finally:
        receiver.executor.shutdown(wait=True)
    return receiver, t0


def test_t56_envelope_runs_from_first_queued_to_request_marked_done(pd_records, clock):
    receiver, t0 = run_direct(clock, {30000: 1000, 30001: 2000}, gaps_ns=[0, 300], before_ns=1000)
    first, second = pd_records.of("task")
    (summary,) = pd_records.of("request_generation")
    # first: queued t0, starts t0+1300, READ to t0+2300, done signal, gap; second: queued t0+300, starts
    # t0+2810, READ to t0+4810, marked done there, then its done signal.
    assert (first["queue_wait_ns"], second["queue_wait_ns"]) == (1300, 2510)
    assert first["request_done_ns"] is None and second["request_done_ns"] == t0 + 4810
    assert check_envelope([first, second], summary) == 4810
    assert summary["transfer_envelope_ns"] == 3510
    assert (summary["task_max_queue_wait_ns"], summary["task_max_meta_fetch_ns"], summary["task_max_prepare_ns"]) == (2510, 0, 0)
    assert second["end_ns"] - t0 == 4810 + DONE_NS
    assert receiver.task_tracker.get_and_clear_finished_requests() == {"decode-a"}


def test_t56_envelope_when_the_tasks_finish_in_reverse_order(pd_records, clock):
    _, t0 = run_direct(clock, {30000: 1000, 30001: 2000}, gaps_ns=[0, 300], before_ns=1000, order=[1, 0])
    tasks = pd_records.of("task")
    (summary,) = pd_records.of("request_generation")
    by_port = {port_of(t): t for t in tasks}
    # The second-queued task is handled first; the first-queued one completes the request.
    assert by_port[30001]["request_done_ns"] is None
    assert by_port[30000]["request_done_ns"] == t0 + 1300 + 2000 + DONE_NS + GAP_NS + 1000
    assert check_envelope(tasks, summary) == 4810  # still from the earliest queued_ns
    assert summary["task_max_queue_wait_ns"] == by_port[30000]["queue_wait_ns"] == 3810


def test_t56_slow_done_signal_is_outside_the_envelope(pd_records, clock):
    run_direct(clock, {30000: 1000}, gaps_ns=[0], before_ns=0, done_ns=50_000)
    (task,) = pd_records.of("task")
    (summary,) = pd_records.of("request_generation")
    assert check_envelope([task], summary) == 1000
    assert task["processing_duration_ns"] == 1000 + 50_000  # the ACK wait is in the task, not in the envelope
    assert task["end_ns"] - task["request_done_ns"] == 50_000


def test_t56_reformat_is_inside_the_envelope(pd_records, clock):
    engine = ClockEngine(clock, {30000: 1000, 30001: 2000})
    receiver = make_receiver(engine=engine)
    receiver._send_done_recv_signal = lambda *args: clock.advance(DONE_NS)
    receiver._apply_kv_cache_reformat = lambda *args, **kwargs: clock.advance(700)
    try:
        worker = make_worker(receiver, [30000, 30001])
        worker._get_group_pulls_metadata.return_value = [
            [[GroupPull(group_id=0, remote_tp_offset=k, num_group_pulls=1, is_group_transfer_end=True)] for k in range(2)]
        ]
        worker.start_load_kv(scheduled_metadata(make_scheduler(), "execution-run-1-57", [("decode-a", "prefill-a")]))
        receiver.task_tracker.add_req_to_process("decode-a")
        for item in drain(receiver):
            receiver._handle_request(item)
            clock.advance(GAP_NS)
    finally:
        receiver.executor.shutdown(wait=True)
    first, last = pd_records.of("task")
    (summary,) = pd_records.of("request_generation")
    reformat = last["associated_stages"]["reformat"]
    assert reformat["status"] == "ok" and reformat["duration_ns"] >= 700
    assert reformat["end_ns"] <= last["request_done_ns"]  # marked done after the reformat
    assert check_envelope([first, last], summary) == 1000 + DONE_NS + GAP_NS + 2000 + reformat["duration_ns"]


def test_t56_failed_read_and_skipped_tasks_still_get_an_envelope(pd_records, clock):
    receiver, _ = run_sequential(clock, [[30000, 30001, 30002]], {30000: 1000, 30001: 2000, 30002: 3000}, returns={30001: -1})
    tasks = pd_records.of("task")
    (summary,) = pd_records.of("request_generation")
    assert [t["status"] for t in tasks] == ["succeeded", "failed", "skipped"]
    assert summary["status"] == "failed" and summary["transfer_envelope_ns"] == 3510
    # The skipped third task completes the request: marked done at its start (t0 + 4020).
    assert tasks[2]["request_done_ns"] is not None
    assert check_envelope(tasks, summary) == 4020
    assert summary["task_max_prepare_ns"] == 0 and "prepare" not in tasks[2]["associated_stages"]
    assert receiver.get_and_clear_invalid_block_ids() == {1}


def test_t56_read_exception_on_the_last_task_still_gets_an_envelope(pd_records, clock):
    run_sequential(clock, [[30000, 30001]], {30000: 1000, 30001: 2000}, raises={30001: OSError("reset")})
    tasks = pd_records.of("task")
    (summary,) = pd_records.of("request_generation")
    assert [t["status"] for t in tasks] == ["succeeded", "failed"] and summary["status"] == "failed"
    assert check_envelope(tasks, summary) == 1000 + DONE_NS + GAP_NS + 2000


def test_t56_full_prefix_hit_has_an_envelope_without_stages(pd_records, clock):
    engine = ClockEngine(clock, {30000: 1000})
    receiver = make_receiver(engine=engine)
    receiver._send_done_recv_signal = lambda *args: clock.advance(DONE_NS)
    try:
        worker = make_worker(receiver, [30000])
        worker._get_kv_split_metadata.return_value = ([[30000]], [([],)], [([3, 4],)])
        worker.start_load_kv(scheduled_metadata(make_scheduler(), "execution-run-1-58", [("decode-a", "prefill-a")]))
        receiver.task_tracker.add_req_to_process("decode-a")
        clock.advance(400)
        for item in drain(receiver):
            receiver._handle_request(item)
    finally:
        receiver.executor.shutdown(wait=True)
    (task,) = pd_records.of("task")
    (summary,) = pd_records.of("request_generation")
    assert task["status"] == "skipped" and summary["transfer_envelope_ns"] is None and engine.calls == []
    assert check_envelope([task], summary) == 400
    assert (summary["task_max_queue_wait_ns"], summary["task_max_meta_fetch_ns"], summary["task_max_prepare_ns"]) == (400, None, None)


def test_t56_without_the_final_task_there_is_no_envelope(pd_records, clock):
    engine = ClockEngine(clock, {30000: 1000})
    receiver = make_receiver(engine=engine)
    try:
        queue_direct(receiver, clock, [30000], [0], last=False)
        for item in drain(receiver):
            receiver._handle_request(item)
    finally:
        receiver.executor.shutdown(wait=True)
    (task,) = pd_records.of("task")
    assert task["tracker_completed"] is False and task["request_done_ns"] is None
    assert pd_records.of("request_generation") == []
    assert receiver.task_tracker.get_and_clear_finished_requests() == set()


class _BrokenDiag(dict):
    def __setitem__(self, key, value):
        if key == "request_done_ns":
            raise RuntimeError("diagnostics broken")
        super().__setitem__(key, value)


def test_t56_marking_the_request_done_never_breaks_the_transfer(pd_records):
    assert MODULE._pd_mark_request_done({}) is None
    assert MODULE._pd_mark_request_done({"_pd_diag": _BrokenDiag(x=1)}) is None
    receiver = make_receiver(engine=TimedEngine())
    try:
        make_worker(receiver, [30000]).start_load_kv(
            scheduled_metadata(make_scheduler(), "execution-run-1-59", [("decode-a", "prefill-a")])
        )
        receiver.task_tracker.add_req_to_process("decode-a")
        (item,) = drain(receiver)
        item["_pd_diag"] = _BrokenDiag(item["_pd_diag"])
        receiver._handle_request(item)
    finally:
        receiver.executor.shutdown(wait=True)
    # The tracker, the queue and the done signal behave as without diagnostics; only the envelope is missing.
    assert receiver.task_tracker.get_and_clear_finished_requests() == {"decode-a"}
    assert receiver._send_done_recv_signal.call_count == 1
    (task,) = pd_records.of("task")
    (summary,) = pd_records.of("request_generation")
    assert task["status"] == "succeeded" and task["request_done_ns"] is None
    assert summary["kv_load_envelope_ns"] is None and summary["kv_load_timing_scope"] == ENVELOPE_SCOPE


def test_t56_maxima_take_meta_fetch_alone_and_each_tasks_prepare(pd_records, clock, monkeypatch):
    # Port 30000's metadata is fetched (one 0.1 s ZMQ retry wait inside metadata_request, which lies inside
    # meta_fetch); port 30001 is cached. Building the read list is made slower on the second task.
    engine = ClockEngine(clock, {30000: 1000, 30001: 2000})
    receiver = make_receiver(engine=engine)
    receiver._send_done_recv_signal = lambda *args: clock.advance(DONE_NS)
    sock = MagicMock()
    sock.send.side_effect = [zmq.ZMQError(), None]
    sock.recv.return_value = msgspec.msgpack.encode(_peer_metadata(receiver))
    per_call_ns = [300, 900]  # each call of the block grouping inside "build the read list" costs this, per task
    state = {"ns": 0, "calls": 0}
    original = MODULE.group_concurrent_contiguous

    def slow_grouping(*args, **kwargs):
        clock.advance(state["ns"])
        state["calls"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(MODULE, "group_concurrent_contiguous", slow_grouping)
    try:
        make_worker(receiver, [30000, 30001]).start_load_kv(
            scheduled_metadata(make_scheduler(), "execution-run-1-60", [("decode-a", "prefill-a")])
        )
        receiver.task_tracker.add_req_to_process("decode-a")
        _uncached_peer(receiver, sock)
        calls = []
        with patch.object(MODULE.logger, "warning"):
            for item, ns in zip(drain(receiver), per_call_ns):
                state.update(ns=ns, calls=0)
                receiver._handle_request(item)
                calls.append(state["calls"])
                clock.advance(GAP_NS)
    finally:
        receiver.executor.shutdown(wait=True)
    assert calls[0] == calls[1] > 0
    first, second = pd_records.of("task")
    (summary,) = pd_records.of("request_generation")
    assert first["associated_stages"]["metadata_request"]["duration_ns"] == 100_000_000
    assert first["associated_stages"]["meta_fetch"]["duration_ns"] == 100_000_000  # contains metadata_request
    assert summary["task_max_meta_fetch_ns"] == 100_000_000  # not meta_fetch + metadata_request
    assert [t["associated_stages"]["prepare"]["duration_ns"] for t in (first, second)] == [300 * calls[0], 900 * calls[1]]
    assert summary["task_max_prepare_ns"] == 900 * calls[1]
    check_envelope([first, second], summary)


# 11. Multi-peer walk with real threads: 16 P ports handled in parallel by the 32-thread executor, done signals
#     acknowledged over real ZMQ on the loopback, and a second request queued behind the first on every port.

WALK_PORTS = list(range(45000, 45016))
ACK_DELAY_S = {port: 0.005 for port in WALK_PORTS} | {45003: 0.02}


def _ack_port(context, port, stop, ready):
    """P's side channel for one port: answer every done signal with ACK after ACK_DELAY_S[port]."""
    sock = context.socket(zmq.REP)
    try:
        sock.bind(f"tcp://127.0.0.1:{port}")
        ready.release()
        while not stop.is_set():
            if sock.poll(50):
                sock.recv()
                time.sleep(ACK_DELAY_S[port])
                sock.send(b"ACK")
    finally:
        sock.close(linger=0)


def _walk(pd_records):
    """Run the walk; return the request summaries and the task records by request id."""
    durations = {port: 0.01 + 0.002 * (i % 5) for i, port in enumerate(WALK_PORTS)}
    engine = TimedEngine(durations_s=durations)
    receiver = make_receiver(engine=engine)
    del receiver._send_done_recv_signal  # the real done signal, over ZMQ to the ACK server
    stop, ready, context = threading.Event(), threading.Semaphore(0), zmq.Context()
    servers = [threading.Thread(target=_ack_port, args=(context, port, stop, ready), daemon=True) for port in WALK_PORTS]
    for server in servers:
        server.start()
    for _ in servers:
        assert ready.acquire(timeout=5)
    try:
        worker = make_worker(receiver, WALK_PORTS)
        worker._get_remote_host_info_by_port = lambda remote_port, port, host, engine_id, mapping: ("127.0.0.1", engine_id)
        scheduler = make_scheduler()
        for request_id, execution_id in (("decode-a", "execution-run-1-61"), ("decode-b", "execution-run-1-62")):
            worker.start_load_kv(scheduled_metadata(scheduler, execution_id, [(request_id, "prefill-" + request_id)]))
        for item in drain(receiver):  # all 32 tasks reach the executor before any is handled
            receiver._submit_request(item)
        deadline = time.monotonic() + 20
        while len(pd_records.of("request_generation")) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        stop.set()
        for server in servers:
            server.join(timeout=5)
        receiver.executor.shutdown(wait=True)
        context.term()
    summaries = {s["request_id"]: s for s in pd_records.of("request_generation")}
    tasks = {rid: [t for t in pd_records.of("task") if t["request_id"] == rid] for rid in ("decode-a", "decode-b")}
    return summaries, tasks


def test_t56_walk_16_peers_in_parallel_with_real_done_acks(pd_records):
    summaries, tasks = _walk(pd_records)
    assert set(summaries) == {"decode-a", "decode-b"}
    for rid in ("decode-a", "decode-b"):
        assert len(tasks[rid]) == 16 and summaries[rid]["succeeded_tasks"] == 16
        envelope = check_envelope(tasks[rid], summaries[rid])
        reads = [t["stages"]["wait_for_transfer"]["duration_ns"] for t in tasks[rid]]
        assert envelope >= max(reads) and envelope < sum(reads)  # parallel peers: at least the slowest, less than serial
        done_task = next(t for t in tasks[rid] if t["request_done_ns"] is not None)
        assert done_task["end_ns"] - done_task["request_done_ns"] >= 0.005e9  # the ACK wait is after the envelope
    # One handler per peer: on every port, b's task starts only after a's task (with its ACK) ended.
    a_by_port = {t["native_calls"][0]["session_id"]: t for t in tasks["decode-a"]}
    for t in tasks["decode-b"]:
        before = a_by_port[t["native_calls"][0]["session_id"]]
        assert t["start_ns"] >= before["end_ns"]
        assert t["queue_wait_ns"] >= before["processing_duration_ns"]
    assert summaries["decode-b"]["task_max_queue_wait_ns"] > summaries["decode-a"]["task_max_queue_wait_ns"]


# 12. Diagnostics level (T58): the level comes from pd_diagnostics.diag_level(), read once when the receiving thread
#     is created; anything but 1, 2 or 3, or no such function (an older vLLM patch), is 3. Level 1 leaves out the
#     four request-level fields below, and does not merge them; levels 2 and 3 write what T56 writes. Task and link
#     records are the same at every level.

LEVEL2_FIELDS = ("task_max_queue_wait_ns", "task_max_meta_fetch_ns", "task_max_prepare_ns", "transfer_envelope_ns")
# Differ between two runs in one process: ids, and values from the real wall and perf_counter clocks (the fake
# clock only replaces time.monotonic_ns).
PER_RUN = {"timestamp_ns", "record_id", "native_tid", "transfer_scope_duration_ns", "wall_utc_offset_seconds"}
PER_RUN_IDS = ("connector_id", "task_id", "request_generation", "call_id")


def set_level(monkeypatch, level):
    """level None: pd_diagnostics without diag_level (an older vLLM patch)."""
    if level is None:
        monkeypatch.delattr(diag, "diag_level", raising=False)
    else:
        monkeypatch.setattr(diag, "diag_level", lambda: level, raising=False)


def normalised(records):
    """The records of one run, ids replaced by their order of appearance, wall-clock and per-run fields left out."""
    seen: dict[str, str] = {}

    def norm(value, key=""):
        if isinstance(value, dict):
            return {k: norm(v, k) for k, v in value.items() if k not in PER_RUN and not k.endswith("_wall_ns")}
        if isinstance(value, list):
            return [norm(v) for v in value]
        if key in PER_RUN_IDS and isinstance(value, str):
            return seen.setdefault(value, f"{key}#{len(seen)}")
        return value

    return [norm(r) for r in records]


def _two_tasks(clock):
    run_direct(clock, {30000: 1000, 30001: 2000}, gaps_ns=[0, 300], before_ns=1000)


def _shards(clock):
    run_sequential(clock, [[30000, 30001], [30002]], {30000: 1000, 30001: 3000, 30002: 2000})


def _failed_read(clock):
    run_sequential(clock, [[30000, 30001]], {30000: 1000, 30001: 2000}, returns={30000: -1})


def _reverse_order(clock):
    run_direct(clock, {30000: 1000, 30001: 2000}, gaps_ns=[0, 300], before_ns=1000, order=[1, 0])


SCENARIOS = [_two_tasks, _shards, _failed_read, _reverse_order]


def records_at(level, scenario, pd_records, clock, monkeypatch):
    set_level(monkeypatch, level)
    clock.now = 1_000_000
    pd_records.records.clear()
    scenario(clock)
    return list(pd_records.records)


@pytest.mark.parametrize("value, level", [
    (None, 3), (3, 3), (2, 2), (1, 1), (0, 3), (4, 3), ("2", 3), (" 2 ", 3), (True, 3), (2.0, 3), ("abc", 3),
])
def test_t58_level_is_1_2_or_3_and_anything_else_is_3(monkeypatch, value, level):
    set_level(monkeypatch, value)
    if value is None:
        assert not hasattr(diag, "diag_level")
    assert MODULE._pd_diag_level() == level
    receiver = make_receiver()
    try:
        assert receiver.pd_diag_level == level
    finally:
        receiver.executor.shutdown(wait=True)


def test_t58_an_error_in_diag_level_is_level_3(monkeypatch):
    monkeypatch.setattr(diag, "diag_level", MagicMock(side_effect=RuntimeError("broken")), raising=False)
    assert MODULE._pd_diag_level() == 3


def test_t58_level_is_read_once_per_receiving_thread(pd_records, clock, monkeypatch):
    calls = []
    monkeypatch.setattr(diag, "diag_level", lambda: calls.append(1) or 2, raising=False)
    receiver, _ = run_direct(clock, {30000: 1000, 30001: 2000}, gaps_ns=[0, 300], before_ns=1000)
    assert len(calls) == 1 and receiver.pd_diag_level == 2
    assert len(pd_records.of("task")) == 2 and len(pd_records.of("request_generation")) == 1


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda f: f.__name__.strip("_"))
def test_t58_levels_2_and_3_write_exactly_what_no_level_writes(pd_records, clock, monkeypatch, scenario):
    base = records_at(None, scenario, pd_records, clock, monkeypatch)
    assert base and any(r["scope"] == "request_generation" for r in base)
    for level in (3, 2):
        got = records_at(level, scenario, pd_records, clock, monkeypatch)
        assert normalised(got) == normalised(base), level
        assert [list(r) for r in got] == [list(r) for r in base], level  # same fields, same order


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda f: f.__name__.strip("_"))
def test_t58_level_1_leaves_out_exactly_the_four_request_fields(pd_records, clock, monkeypatch, scenario):
    base = normalised(records_at(3, scenario, pd_records, clock, monkeypatch))
    assert base and any(r["scope"] == "request_generation" for r in base)
    raw = records_at(1, scenario, pd_records, clock, monkeypatch)
    got = normalised(raw)
    assert len(got) == len(base)
    for want, have, record in zip(base, got, raw):
        if want["scope"] == "request_generation":
            assert set(LEVEL2_FIELDS) <= set(want) and not set(LEVEL2_FIELDS) & set(record)
            assert have == {key: value for key, value in want.items() if key not in LEVEL2_FIELDS}
            assert [k for k in record if k not in PER_RUN] == [k for k in want if k not in LEVEL2_FIELDS]  # order kept
            assert have["kv_load_envelope_ns"] == want["kv_load_envelope_ns"]
        else:
            assert have == want  # task and link records: the same at every level


def test_t58_level_1_does_not_merge_the_request_maxima_or_transfer_envelope(clock, monkeypatch):
    for level, merged in ((1, False), (3, True)):
        set_level(monkeypatch, level)
        engine = ClockEngine(clock, {30000: 1000, 30001: 2000})
        receiver = make_receiver(engine=engine)
        receiver._send_done_recv_signal = lambda *args: clock.advance(DONE_NS)
        try:
            queue_direct(receiver, clock, [30000, 30001], [0, 300])
            items = drain(receiver)
            for item in items:
                receiver._mark_request_task_submitted(item)
            receiver._handle_request(items[0])  # the request is still open: one task left
            summary = receiver.pd_request_tasks["decode-a"]
            maxima = [summary[key] for key in LEVEL2_FIELDS[:3]]
            envelope = (summary["transfer_start_ns"], summary["transfer_end_ns"])
            assert summary["first_queued_ns"] is not None  # the KV-load envelope is merged at every level
            if merged:
                assert None not in maxima and None not in envelope
            else:
                assert maxima == [None, None, None] and envelope == (None, None)
        finally:
            receiver.executor.shutdown(wait=True)


def test_t58_walk_16_peers_at_level_1(pd_records, monkeypatch):
    set_level(monkeypatch, 1)
    summaries, tasks = _walk(pd_records)
    assert set(summaries) == {"decode-a", "decode-b"}
    for rid in ("decode-a", "decode-b"):
        summary = summaries[rid]
        assert len(tasks[rid]) == 16 and summary["succeeded_tasks"] == 16
        assert not set(LEVEL2_FIELDS) & set(summary)
        first_queued = min(t["queued_ns"] for t in tasks[rid])
        (done,) = [t for t in tasks[rid] if t["request_done_ns"] is not None]
        envelope = summary["kv_load_envelope_ns"]
        assert envelope == done["request_done_ns"] - first_queued
        reads = [t["stages"]["wait_for_transfer"]["duration_ns"] for t in tasks[rid]]
        assert envelope >= max(reads) and envelope < sum(reads)
        for t in tasks[rid]:  # task records keep every timing at level 1
            assert t["queue_wait_ns"] is not None and t["transfer_envelope_ns"] is not None
            assert {"meta_fetch", "prepare"} <= set(t["associated_stages"])
