"""Drive the same Pool scenarios on one vllm-ascend tree and print the [PD_DIAG] records they write.

Run from the tree root inside the test container (see run_record_shapes.sh):
    python /harness/t51_record_shapes.py OUT.json
The output lists every record of every scenario as written, so two trees can be compared field by field
(compare_record_shapes.py). Only the code under test is real; store, token database and NPU events are fakes.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import tests.ut.distributed.ascend_store._mock_deps  # noqa: F401  (stubs torch/vllm when absent)

sys.path.insert(0, "/harness")
import t51_realdiag  # noqa: E402,F401  (real vllm pd_diagnostics)

try:
    import t51_glm_stubs  # noqa: E402,F401
except Exception:  # the f9c8e0bdb tree does not need them
    pass

from vllm.v1.metrics import pd_diagnostics as diag  # noqa: E402

from vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store import kv_transfer as kvt  # noqa: E402
from vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store import pool_worker as pw  # noqa: E402
from vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.metadata import (  # noqa: E402
    AscendConnectorMetadata,
    LoadSpec,
    ReqMeta,
)
from vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.pool_scheduler import KVPoolScheduler  # noqa: E402

RECORDS: list[dict] = []


class Collect(logging.Handler):
    def emit(self, record):
        message = record.getMessage()
        if "[PD_DIAG] " in message:
            RECORDS.append(json.loads(message.split("[PD_DIAG] ", 1)[1]))


diag._LOG.addHandler(Collect())
diag._LOG.setLevel(logging.INFO)
diag._LOG.propagate = False


class FakeStore:
    requires_exists_before_put = True

    def exists(self, keys):
        return [0] * len(keys)

    def put(self, keys, addrs, sizes):
        return [True] * len(keys)

    def get(self, keys, addrs, sizes):
        return [0] * len(keys)

    def set_device(self):
        pass


class FakeTokenDatabase:
    group_block_len = {0: [8]}
    group_num_layers = {"kv": {0: 1}}
    group_kv_caches_base_addr = {0: [1000]}
    hash_block_size = 16
    partitions = None

    def store_mask(self, token_len, num_prompt_tokens):
        return None

    def load_mask(self, block_hashes, token_len):
        return None

    def mask_allows_chunk(self, masks, group_id, start):
        return True

    def process_token_key_strings_with_block_ids(self, token_len, block_hashes, block_ids, mask_num=0, **kwargs):
        chunk_filter = kwargs.get("chunk_filter")
        for index, block_id in enumerate(block_ids):
            start, end = index * 16, (index + 1) * 16
            if end > token_len or start < mask_num:
                continue
            if chunk_filter is not None and not chunk_filter(start):
                continue
            yield start, end, f"key-{index}", f"hash-{index}", block_id

    def prepare_value(self, start, end, block_ids, kv_cache_group_id=0, cache_role="kv", block_id=None):
        return [1000 + start], [8], block_id


def make_request(req_id, execution_id, *, can_save=None, load_spec=None):
    return ReqMeta(
        req_id=req_id,
        token_len_chunk=32,
        block_ids=[1, 2],
        block_hashes=["h0", "h1"],
        can_save=can_save,
        load_spec=load_spec,
        is_last_chunk=True,
        pd_execution_id=execution_id,
    )


def sending_thread():
    thread = kvt.KVCacheStoreSendingThread(FakeStore(), FakeTokenDatabase(), 16, 0, 1)
    return thread


def drain(thread):
    while not thread.request_queue.empty():
        thread._dispatch_diagnostic_request(thread.request_queue.get())


def scenario(name):
    def decorate(function):
        def run():
            start = len(RECORDS)
            try:
                function()
                error = None
            except Exception as exc:  # report, keep going
                error = f"{type(exc).__name__}: {exc}"
            return {"scenario": name, "error": error, "records": RECORDS[start:]}

        run.scenario = name
        return run

    return decorate


@scenario("save")
def save():
    thread = sending_thread()
    thread.add_save_batch([make_request("r-save", "exec-save", can_save=True)])
    drain(thread)


@scenario("save_twice")
def save_twice():
    """Two save chunks of one request: shows each tree's store-job bookkeeping in members/completions."""
    thread = sending_thread()
    thread.add_save_batch([make_request("r-save2", "exec-save2-a", can_save=True)])
    drain(thread)
    thread.add_save_batch([make_request("r-save2", "exec-save2-b", can_save=True)])
    drain(thread)


class FakeMooncakeNative:
    """Stands in for the Mooncake store object inside the real MooncakeBackend."""

    def batch_is_exist(self, keys):
        return [0] * len(keys)

    def batch_put_from_multi_buffers(self, keys, addrs, sizes, config):
        return [sum(size) for size in sizes]

    def batch_get_into_multi_buffers(self, keys, addrs, sizes):
        return [sum(size) for size in sizes]


def mooncake_backend():
    from vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.backend import mooncake_backend as mb

    backend = object.__new__(mb.MooncakeBackend)
    backend.store = FakeMooncakeNative()
    backend._lazy_init = False
    backend.ensure_initialized = lambda: None
    backend._build_replicate_config = lambda: None
    backend.set_device = lambda: None
    backend._store_initialized = True
    backend._prefetch_conf = SimpleNamespace(enabled=False, options=None)  # f9c8e0bdb exists() reads it
    return backend


@scenario("save_mooncake_backend")
def save_mooncake_backend():
    """Whole-key save through the real MooncakeBackend.put (native call record and task status)."""
    thread = kvt.KVCacheStoreSendingThread(mooncake_backend(), FakeTokenDatabase(), 16, 0, 1)
    thread.add_save_batch([make_request("r-mc", "exec-mc", can_save=True)])
    drain(thread)


@scenario("load_async_mooncake_backend")
def load_async_mooncake_backend():
    worker = make_load_worker(True)
    worker.kv_recv_thread = kvt.KVCacheStoreRecvingThread(mooncake_backend(), FakeTokenDatabase(), 16, 0, 1)
    worker.start_load_kv(load_meta("exec-mc-load"))
    drain(worker.kv_recv_thread)


@scenario("save_submit")
def save_submit():
    worker = pw.KVPoolWorker.__new__(pw.KVPoolWorker)
    worker.tp_rank = 0
    worker.kv_send_thread = sending_thread()
    worker.group_uses_align_state = []
    meta = AscendConnectorMetadata(set(), set(), pd_execution_id="exec-submit")
    meta.add_request(make_request("r-submit", None, can_save=True))
    with patch.object(pw.torch, "npu", MagicMock(), create=True):
        worker.wait_for_save(meta)


def make_load_worker(load_async):
    worker = pw.KVPoolWorker.__new__(pw.KVPoolWorker)
    worker.tp_rank = 0
    worker.use_layerwise = False
    worker.load_async = load_async
    worker.tp_mismatch = False
    worker.cache_transfer_granularity = 16
    worker.group_uses_align_state = []
    worker.token_database = FakeTokenDatabase()
    worker.grouped_block_size = [16]
    worker.m_store = FakeStore()
    worker.backend_name = "mooncake"
    worker._invalid_block_ids = set()
    worker._invalid_block_ids_lock = threading.Lock()
    worker._record_kv_connector_operation = lambda *args: None
    worker._record_failed_request = lambda request: None
    worker.kv_recv_thread = kvt.KVCacheStoreRecvingThread(FakeStore(), FakeTokenDatabase(), 16, 0, 1)
    return worker


def load_meta(execution_id):
    meta = AscendConnectorMetadata(set(), set(), pd_execution_id=execution_id)
    meta.add_request(
        make_request("r-load", None, load_spec=LoadSpec(vllm_cached_tokens=0, kvpool_cached_tokens=32, can_load=True))
    )
    return meta


@scenario("load_async")
def load_async():
    worker = make_load_worker(True)
    worker.start_load_kv(load_meta("exec-load-async"))
    drain(worker.kv_recv_thread)


@scenario("load_sync")
def load_sync():
    worker = make_load_worker(False)
    worker.start_load_kv(load_meta("exec-load-sync"))


@scenario("wait_for_put")
def wait_for_put():
    thread = sending_thread()
    context = {"execution_id": "exec-fence", "request_id": None, "request_ids": ["r-a"], "tp_rank": 0}
    with diag.scope(**context), diag.batch("worker_summary", phase="execute", **context):
        thread.wait_for_requests_saved({"r-released"})


@scenario("pool_load_link")
def pool_load_link():
    scheduler = object.__new__(KVPoolScheduler)
    scheduler.load_async = True
    scheduler.use_layerwise = False
    scheduler.vllm_config = SimpleNamespace(
        kv_transfer_config=SimpleNamespace(engine_id="1"),
        parallel_config=SimpleNamespace(data_parallel_rank=3),
    )
    meta = load_meta("exec-link")
    scheduler._emit_load_links(meta, SimpleNamespace(pd_execution_id="exec-link"))


def main():
    results = [run() for run in (save, save_twice, save_mooncake_backend, load_async_mooncake_backend, save_submit, load_async, load_sync, wait_for_put, pool_load_link)]
    with open(sys.argv[1], "w") as handle:
        json.dump(results, handle, indent=1, sort_keys=True)
    for result in results:
        events = [(r["event"], r.get("operation") or r.get("phase") or r.get("transfer_role")) for r in result["records"]]
        print(result["scenario"], "error=" + str(result["error"]), events)


if __name__ == "__main__":
    main()
