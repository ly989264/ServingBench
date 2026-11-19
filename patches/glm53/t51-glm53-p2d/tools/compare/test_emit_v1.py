"""Emit the V1 connector's PD_DIAG records for the comparison scenarios (written to $P2D_OUT/v1.jsonl).

Copied next to test_mooncake_connector_pd_diag.py and run with pytest so the UT conftest stubs apply.
"""

import json
import logging
import os
import types

import test_mooncake_connector_pd_diag as h
from vllm.v1.metrics import pd_diagnostics as diag
from vllm.v1.request import RequestStatus

X = "execution-cmp-1"


def _dump(name, capture):
    with open(os.path.join(os.environ["P2D_OUT"], "v1.jsonl"), "a") as out:
        for record in capture.records:
            out.write(json.dumps({"scenario": name, "record": record}) + "\n")
    capture.records.clear()


def _read(capture, name, engine, local_blocks=([1, 2],)):
    receiver = h.make_receiver(engine=engine)
    try:
        worker = h.make_worker(receiver, [30000])
        worker._get_kv_split_metadata.return_value = ([[30000]], [local_blocks], [([3, 4],)])
        meta = h.scheduled_metadata(h.make_scheduler(), X, [("decode-a", "prefill-a")])
        capture.records.clear()  # the link record is its own scenario
        worker.start_load_kv(meta)
        receiver.task_tracker.add_req_to_process("decode-a")
        for item in h.drain(receiver):
            receiver._submit_request(item)
    finally:
        receiver.executor.shutdown(wait=True)
    _dump(name, capture)


def test_emit():
    pd_records = h._Capture()
    diag._LOG.setLevel(logging.INFO)
    diag._LOG.addHandler(pd_records)
    try:
        _emit(pd_records)
    finally:
        diag._LOG.removeHandler(pd_records)


def _emit(pd_records):
    h.scheduled_metadata(h.make_scheduler(), X, [("decode-a", "prefill-a")])
    _dump("link", pd_records)

    scheduler = h.make_scheduler()
    scheduler.engine_id = "prefill-engine"
    scheduler.group_transfer_info = [types.SimpleNamespace(tokens_per_block=16, blocks_per_window=0, is_state_group=False)]
    request = h.MockRequest(
        "prefill-a", kv_transfer_params={"do_remote_decode": True}, status=RequestStatus.FINISHED_LENGTH_CAPPED
    )
    scheduler.request_finished(request, ([10],))
    _dump("offer", pd_records)

    _read(pd_records, "read_success", h.TimedEngine())
    _read(pd_records, "read_empty", h.TimedEngine(), local_blocks=([],))
    _read(pd_records, "read_failure", h.TimedEngine(returns={30000: -1}))
