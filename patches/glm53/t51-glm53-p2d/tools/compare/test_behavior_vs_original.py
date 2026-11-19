"""The patched MooncakeConnectorV1 behaves exactly like the original d4ad49796 file apart from writing PD_DIAG.

Loads the original file ($P2D_ORIGINAL) next to the patched module and runs the same scenarios on both.
Compared per scenario: every READ call (session, src, dst, lengths), every done signal sent to P, the
finished set, the invalid-block set, queue and task counters, failure flags, pending reformat state,
connector log lines (numbers masked), the exception raised (type and message), the items put on the
receive queue (without the diagnostics entry), the scheduler's metadata and request_finished results.
Copied next to test_mooncake_connector_pd_diag.py; needs its helpers for configs and requests.
"""

import dataclasses
import importlib.util
import logging
import os
import re
import sys
import types
from unittest.mock import MagicMock, patch

import pytest
import test_mooncake_connector_pd_diag as h
import torch
from vllm.v1.request import RequestStatus

from vllm_ascend.distributed.kv_transfer.kv_p2p import mooncake_connector as patched

_spec = importlib.util.spec_from_file_location("mooncake_connector_original", os.environ["P2D_ORIGINAL"])
original = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = original
_spec.loader.exec_module(original)
assert "pd_diagnostics" not in open(os.environ["P2D_ORIGINAL"]).read()


class _Logs(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        if record.name.startswith("vllm.v1.metrics.pd_diagnostics"):
            return
        self.lines.append((record.levelname, re.sub(r"\d+(\.\d+)?", "<n>", record.getMessage())))


def normalize(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return (type(value).__name__, normalize(dataclasses.asdict(value)))
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items() if k not in ("_pd_diag", "pd_execution_id")}
    if isinstance(value, (list, tuple)):
        return type(value)(normalize(v) for v in value)
    if isinstance(value, set):
        return sorted(value)
    return value


def receiver_for(mod, read):
    receiver = mod.KVCacheRecvingThread(
        tp_rank=0,
        tp_size=1,
        _prefill_pp_size=1,
        engine=MagicMock(**{"batch_transfer_sync_read.side_effect": read}),
        local_engine_id="decode-engine",
        local_handshake_port=30100,
        side_channel_port=30100,
        local_kv_caches_base_addr=h.LOCAL_ADDRS,
        block_len_per_addr=h.BLOCK_LENS,
        block_stride_per_addr=h.LOCAL_STRIDES,
        vllm_config=h.MockVllmConfig(),
        kv_caches={h.LAYER: (torch.empty(1), torch.empty(1))},
        kv_group2layeridx={0: ({"kv_cache_spec_type": "FullAttentionSpec", "kv_cache_group_id": 0}, [0])},
        block_size_scale=[[1, 1]],
    )
    receiver._send_done_recv_signal = MagicMock()
    return receiver


def worker_for(mod, receiver, shards, local_blocks, transfer_end=False, pulls=True):
    worker = mod.MooncakeConnectorWorker.__new__(mod.MooncakeConnectorWorker)
    worker.kv_send_thread = None
    worker.kv_recv_thread = receiver
    worker.tp_rank = 0
    worker.pcp_size = worker.dcp_size = 1
    worker._prefill_tp_size = 2
    worker.kv_group2layeridx = receiver.kv_group2layeridx
    worker.remote_port_send_num = {}
    worker.vllm_config = receiver.vllm_config
    worker._get_sfa_replicate_k_block_ids = MagicMock(return_value=((), ()))
    worker._get_kv_split_metadata = MagicMock(
        return_value=([list(s) for s in shards], [local_blocks for _ in shards], [([3, 4],) for _ in shards])
    )
    worker._get_group_pulls_metadata = MagicMock(
        return_value=[
            [
                ([mod.GroupPull(group_id=0, remote_tp_offset=k, num_group_pulls=1, is_group_transfer_end=transfer_end)]
                 if pulls else [])
                for k in range(len(s))
            ]
            for s in shards
        ]
    )
    for shard in shards:
        for port in shard:
            with receiver.remote_metadata_lock:
                receiver.kv_caches_base_addr[h.REMOTE_ENGINE][port] = h.REMOTE_ADDRS
                receiver.remote_te_port[h.REMOTE_ENGINE][port] = port + h.TE_PORT_OFFSET
                receiver.remote_block_size_scale[h.REMOTE_ENGINE][port] = [[1, 1]]
                receiver.remote_block_stride_per_addr[h.REMOTE_ENGINE][port] = h.REMOTE_STRIDES
                receiver.remote_kv_group2layeridx[h.REMOTE_ENGINE][port] = receiver.kv_group2layeridx
    return worker


def scheduler_for(mod):
    with patch.object(mod, "init_ascend_config"), patch.object(mod, "get_ascend_config", return_value=MagicMock()):
        return mod.MooncakeConnectorScheduler(h.MockVllmConfig(), "decode-engine", h.MockKVCacheConfig())


def metadata_for(mod, request_id="decode-a", virtual=False):
    scheduler = scheduler_for(mod)
    params = h.remote_params("prefill-a")
    if virtual:
        params["do_virtual"] = True
    scheduler._reqs_need_recv[request_id] = (h.MockRequest(request_id, kv_transfer_params=params), ([1, 2],), ([1, 2],), 32)
    scheduler._reqs_in_batch.add(request_id)
    return scheduler.build_connector_meta(types.SimpleNamespace(pd_execution_id="execution-run-1-1"))


READ_SCENARIOS = {
    "success_2_shards_x_2_ports": dict(shards=[[30000, 30001], [30002, 30003]]),
    "read_returns_-1_midway": dict(shards=[[30000, 30001, 30002]], returns={30001: -1}),
    "read_raises_first": dict(shards=[[30000, 30001]], raises={30000: OSError("reset")}),
    "full_prefix_hit": dict(shards=[[30000]], local_blocks=([],)),
    "empty_descriptor_list": dict(shards=[[30000]], pulls=False),
    "metadata_fetch_fails": dict(shards=[[30000]], uncached={30000}),
    "reformat_fails": dict(shards=[[30000, 30001]], transfer_end=True, reformat_error=True),
    "reformat_runs": dict(shards=[[30000, 30001]], transfer_end=True),
    "done_signal_raises": dict(shards=[[30000]], done_error=True),
    "virtual_request": dict(shards=[[30000]], virtual=True),
}


def run_read_scenario(mod, spec):
    def read(session_id, src, dst, lengths):
        port = int(session_id.rsplit(":", 1)[1]) - h.TE_PORT_OFFSET
        if port in spec.get("raises", {}):
            raise spec["raises"][port]
        return spec.get("returns", {}).get(port, 0)

    receiver = receiver_for(mod, read)
    logs = _Logs()
    vllm_logger = logging.getLogger("vllm")
    vllm_logger.addHandler(logs)
    old_level = vllm_logger.level
    vllm_logger.setLevel(logging.DEBUG)
    errors = []
    try:
        worker = worker_for(
            mod, receiver, spec["shards"], spec.get("local_blocks", ([1, 2],)), spec.get("transfer_end", False),
            spec.get("pulls", True),
        )
        for port in spec.get("uncached", ()):
            receiver.kv_caches_base_addr[h.REMOTE_ENGINE].pop(port)
        receiver._get_remote_metadata = MagicMock(side_effect=RuntimeError("no peer"))
        if spec.get("reformat_error"):
            receiver._apply_kv_cache_reformat = MagicMock(side_effect=RuntimeError("reformat"))
        else:
            receiver._apply_kv_cache_reformat = MagicMock()
        if spec.get("done_error"):
            receiver._send_done_recv_signal.side_effect = RuntimeError("ack lost")
        meta = metadata_for(mod, virtual=spec.get("virtual", False))
        receiver.task_tracker.add_req_to_process("decode-a")
        worker.start_load_kv(meta)
        items = []
        while not receiver.request_queue.empty():
            items.append(receiver.request_queue.get_nowait())
        queued = normalize(items)
        for item in items:
            try:
                receiver._handle_request(item)
            except BaseException as exc:  # compare what escapes to the caller
                errors.append((type(exc).__name__, str(exc)))
    finally:
        vllm_logger.removeHandler(logs)
        vllm_logger.setLevel(old_level)
        receiver.executor.shutdown(wait=True)
    return {
        "queued": queued,
        "reads": [c.args for c in receiver.engine.batch_transfer_sync_read.call_args_list],
        "done_signals": [c.args for c in receiver._send_done_recv_signal.call_args_list],
        "reformat_calls": [normalize(c.args) for c in receiver._apply_kv_cache_reformat.call_args_list],
        "finished": sorted(receiver.task_tracker.get_and_clear_finished_requests()),
        "not_transfer": sorted(receiver.task_tracker.finished_requests),
        "invalid_blocks": sorted(receiver.get_and_clear_invalid_block_ids()),
        "unfinished_queue_tasks": receiver.request_queue.unfinished_tasks,
        "task_counts": dict(receiver.request_task_counts),
        "markers": sorted(receiver.finished_request_markers),
        "failed_flags": sorted(receiver.failed_recv_requests),
        "pending_reformat": normalize(dict(receiver.pending_reformat)),
        "proc_not_transfer": dict(receiver.proc_not_transfer_request),
        "reqs_to_process": sorted(receiver.task_tracker.reqs_to_process),
        "errors": errors,
        "logs": logs.lines,
    }


@pytest.mark.parametrize("name", sorted(READ_SCENARIOS))
def test_receive_path_matches_original(name):
    spec = READ_SCENARIOS[name]
    before, after = run_read_scenario(original, spec), run_read_scenario(patched, spec)
    assert before == after
    if name == "success_2_shards_x_2_ports":
        assert len(before["reads"]) == 4  # the scenario really transfers


def run_scheduler(mod):
    out = {}
    scheduler = scheduler_for(mod)
    scheduler.group_transfer_info = [types.SimpleNamespace(tokens_per_block=16, blocks_per_window=0, is_state_group=False)]
    capped = h.MockRequest("p-1", kv_transfer_params={"do_remote_decode": True}, status=RequestStatus.FINISHED_LENGTH_CAPPED)
    out["finished_capped"] = normalize(scheduler.request_finished(capped, ([10, 11],)))
    out["finished_plain"] = normalize(scheduler.request_finished(h.MockRequest("plain"), ([1],)))
    rejected = h.MockRequest("d-rejected", kv_transfer_params=h.remote_params(), status=RequestStatus.FINISHED_ABORTED)
    out["finished_rejected"] = normalize(scheduler.request_finished(rejected, ([],)))
    scheduler._reqs_need_recv["d-1"] = (h.MockRequest("d-1", kv_transfer_params=h.remote_params()), ([1, 2],), ([1, 2],), 32)
    scheduler._reqs_in_batch.add("d-1")
    meta = scheduler.build_connector_meta(types.SimpleNamespace(pd_execution_id="execution-run-1-2"))
    out["meta"] = normalize({"requests": meta.requests, "to_send": sorted(meta.requests_to_send), "in_batch": meta.reqs_in_batch})
    out["after"] = normalize({"need_recv": scheduler._reqs_need_recv, "need_send": scheduler._reqs_need_send, "in_batch": scheduler._reqs_in_batch})
    meta = scheduler.build_connector_meta(types.SimpleNamespace())  # SchedulerOutput without pd_execution_id
    out["meta_without_execution_id"] = normalize({"requests": meta.requests})
    return out


def test_scheduler_matches_original():
    before, after = run_scheduler(original), run_scheduler(patched)
    assert before == after
    assert list(before["meta"]["requests"]) == ["d-rejected", "d-1"]
