"""Emit MooncakeHybridConnector's PD_DIAG records (RC1 + patch 02) for the comparison scenarios.

Copied next to tests/ut/kv_offload/test_mooncake_hybrid_connector.py and run with pytest; writes
$P2D_OUT/hybrid.jsonl. Thread and scheduler set-up follow that test file (test_group_transfer_and_completion
and _make_scheduler); vllm_config is added because the patched request_finished reads it.
"""

import asyncio
import json
import logging
import os
import queue
import threading
import time
import types
from unittest.mock import MagicMock, patch

from tests.ut.kv_offload import test_mooncake_hybrid_connector as hyb
from vllm.v1.kv_cache_interface import MambaSpec
from vllm.v1.metrics import pd_diagnostics as diag
from vllm.v1.request import RequestStatus

from vllm_ascend.distributed.kv_transfer.kv_p2p import mooncake_hybrid_connector as hyb_module
from vllm_ascend.distributed.kv_transfer.kv_p2p.mooncake_hybrid_connector import KVCacheTaskTracker

X = "execution-cmp-1"
REMOTE_ENGINE = "prefill-engine"
CONFIG = types.SimpleNamespace(parallel_config=types.SimpleNamespace(data_parallel_rank=0))


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.records = []

    def emit(self, record):
        message = record.getMessage()
        if message.startswith("[PD_DIAG] "):
            self.records.append(json.loads(message.split("[PD_DIAG] ", 1)[1]))


def _dump(name, capture):
    with open(os.path.join(os.environ["P2D_OUT"], "hybrid.jsonl"), "a") as out:
        for record in capture.records:
            out.write(json.dumps({"scenario": name, "record": record}) + "\n")
    capture.records.clear()


def _thread():
    thread = hyb.TestHybridKVCacheRecvingThreadDispatch("test_group_transfer_and_completion")._make_thread()
    thread.pd_connector_id = diag.new_id("pd_connector")
    thread.use_hybrid = True
    thread.tp_rank = 0
    thread._prefill_pp_size = 1
    thread.hma_group_size = 2
    thread.kv_cache_specs = [MagicMock(), MagicMock(spec=MambaSpec)]
    thread.task_tracker = KVCacheTaskTracker()
    thread.request_queue = queue.Queue()
    thread.proc_not_transfer_request = {}
    thread.proc_not_transfer_request_lock = threading.Lock()
    thread.side_channel_port = thread.local_handshake_port = 32000
    thread.local_engine_id = "decode-engine"
    thread.remote_metadata_lock = threading.Lock()
    thread.kv_caches_base_addr = {
        "decode-engine": {32000: [0x1000, 0x2000, 0x3000]},
        REMOTE_ENGINE: {31002: [0x4000, 0x5000, 0x6000]},
    }
    thread.remote_te_port = {REMOTE_ENGINE: {31002: 7777}}
    thread.addr_group_idx = [[0], [0], [1]]
    thread.block_len_per_addr = [16, 8, 32]
    thread.block_stride_per_addr = [32, 16, 64]
    thread.engine = MagicMock()
    thread._send_done_recv_signal = MagicMock()
    return thread


def _read(capture, name, ret=0, empty=False, raises=None, uncached=False, metadata_error=None, tasks=1, no_work=False):
    thread = _thread()
    try:
        thread.engine.batch_transfer_sync_read.return_value = ret
        if raises is not None:
            thread.engine.batch_transfer_sync_read.side_effect = raises
        if uncached:
            cached = thread.kv_caches_base_addr[REMOTE_ENGINE].pop(31002)

            def fetch(host, port):
                if metadata_error is not None:
                    raise metadata_error
                thread.kv_caches_base_addr[REMOTE_ENGINE][port] = cached

            thread._get_remote_metadata = MagicMock(side_effect=fetch)
        thread.task_tracker.add_req_to_process("decode-a")
        for index in range(tasks):
            with diag.scope(execution_id=X):
                thread.add_request(
                    request_id="decode-a",
                    remote_request_id="prefill-a",
                    local_block_ids=([], []) if empty else ([2, 3], [4]),
                    remote_block_ids=([], []) if no_work else ([1, 2], [3]),
                    remote_engine_id=REMOTE_ENGINE,
                    remote_host="192.0.2.1",
                    remote_handshake_port=31002,
                    offset=index,
                    tp_num_need_pulls=1,
                    all_task_done=index == tasks - 1,
                )
        while not thread.request_queue.empty():
            try:
                thread._handle_request(_submitted(thread, thread.request_queue.get_nowait()))
            except BaseException as exc:  # a cancelled READ propagates, as in V1
                if type(exc).__name__ != "CancelledError":
                    raise
    finally:
        thread.executor.shutdown(wait=True)
    _dump(name, capture)


def _submitted(thread, item):
    """What _submit_request does before handing the item to a peer handler, run inline."""
    item["_pd_diag"]["submitted_ns"] = time.monotonic_ns()
    thread._mark_request_task_submitted(item)
    return item


def _reformat(capture, name, error=None):
    """Non-hybrid path with two TP pulls: the last pull reformats (need_cat_cache), as patch 02 times it."""
    thread = _thread()
    thread.use_hybrid = False
    thread.kv_caches_base_addr = {"decode-engine": {32000: [0x1000, 0x2000]}, REMOTE_ENGINE: {31002: [0x4000, 0x5000]}}
    thread.block_len_per_addr = [64, 64]
    thread.pp_layer_indices = {0: (0, 1)}
    thread.kv_caches = {"layer": (MagicMock(), MagicMock())}
    thread.vllm_config = types.SimpleNamespace(
        parallel_config=CONFIG.parallel_config, speculative_config=None
    )
    thread.reformat_kv_cache = MagicMock(side_effect=error)
    thread.engine.batch_transfer_sync_read.return_value = 0
    config = types.SimpleNamespace(enable_kv_nz=False, enable_transpose_kv_cache_by_block=False)
    try:
        with patch.object(hyb_module, "get_ascend_config", return_value=config):
            thread.task_tracker.add_req_to_process("decode-a")
            with diag.scope(execution_id=X):
                thread.add_request(
                    request_id="decode-a",
                    remote_request_id="prefill-a",
                    local_block_ids=([2, 3],),
                    remote_block_ids=([1, 2],),
                    remote_engine_id=REMOTE_ENGINE,
                    remote_host="192.0.2.1",
                    remote_handshake_port=31002,
                    offset=1,
                    tp_num_need_pulls=2,
                    all_task_done=True,
                )
            thread._handle_request(_submitted(thread, thread.request_queue.get_nowait()))
    finally:
        thread.executor.shutdown(wait=True)
    _dump(name, capture)


def test_emit():
    capture = _Capture()
    diag._LOG.setLevel(logging.INFO)
    diag._LOG.addHandler(capture)
    try:
        scheduler = hyb.TestMooncakeHybridConnectorScheduler("test_compute_transfer_block_ids_trims_swa_groups")._make_scheduler()
        scheduler.vllm_config = CONFIG
        scheduler.engine_id = "decode-engine"
        scheduler._reqs_in_batch = set()
        request = hyb.MockRequest(
            "decode-a",
            prompt_token_ids=list(range(32)),
            kv_transfer_params={
                "do_remote_prefill": True,
                "remote_block_ids": ([3, 4], []),
                "remote_engine_id": REMOTE_ENGINE,
                "remote_request_id": "prefill-a",
                "remote_host": "192.0.2.1",
                "remote_port": 30000,
            },
            status=RequestStatus.WAITING,
        )
        scheduler._reqs_need_recv = {"decode-a": (request, ([1, 2], []), 32)}
        scheduler.build_connector_meta(types.SimpleNamespace(pd_execution_id=X))
        _dump("link", capture)
        request_link = request

        scheduler = hyb.TestMooncakeHybridConnectorScheduler("test_compute_transfer_block_ids_trims_swa_groups")._make_scheduler()
        scheduler.vllm_config = CONFIG
        scheduler.engine_id = "prefill-engine"
        request = hyb.MockRequest(
            "prefill-a",
            prompt_token_ids=list(range(513)),
            kv_transfer_params={"do_remote_decode": True},
            status=RequestStatus.FINISHED_LENGTH_CAPPED,
        )
        scheduler.request_finished_all_groups(request, ([10, 11, 12], [20, 21]))
        _dump("offer", capture)

        _read(capture, "read_success")
        _read(capture, "read_empty", empty=True)
        _read(capture, "read_failure", ret=-1)
        if os.environ.get("P2D_EXTRA"):  # extra paths for the field-set check, not paired with V1
            scheduler = hyb.TestMooncakeHybridConnectorScheduler("test_compute_transfer_block_ids_trims_swa_groups")._make_scheduler()
            scheduler.vllm_config = CONFIG
            scheduler.engine_id = "decode-engine"
            scheduler._reqs_in_batch = set()
            scheduler._reqs_need_recv = {"decode-a": (request_link, ([1, 2], []), 32)}
            with diag.scope(kv_role="kv_consumer"):
                scheduler.build_connector_meta(types.SimpleNamespace(pd_execution_id=X))
            _dump("x_link_with_schedule_context", capture)
            _read(capture, "x_read_raises", raises=OSError("reset"))
            _read(capture, "x_read_cancelled", raises=asyncio.CancelledError())
            _read(capture, "x_metadata_fetched", uncached=True)
            _read(capture, "x_metadata_fails", uncached=True, metadata_error=RuntimeError("no peer"))
            _read(capture, "x_two_tasks", tasks=2)
            _read(capture, "x_two_tasks_failure", tasks=2, ret=-1)
            _reformat(capture, "x_reformat")
            _reformat(capture, "x_reformat_fails", error=RuntimeError("reformat"))
            _read(capture, "x_no_work", no_work=True)
    finally:
        diag._LOG.removeHandler(capture)
