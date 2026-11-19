BASE_METHODS = '''
    def add_request(self, request: Any) -> None:
        if self._fatal_error is None:
            with self._diagnostic_lock:
                self._capture_diagnostics(request)
        self.request_queue.put(request)

    def _capture_diagnostics(self, request: Any) -> None:
        snapshot = pool_diag.capture(request, self._diag_operation, self.tp_rank)
        layers = getattr(request, "transfer_tasks", request)
        layer_id = (
            layers[0].layer_id
            if isinstance(layers, list) and layers and hasattr(layers[0], "layer_id")
            else getattr(request, "layer_id", None)
        )
        final_layer_id = self._diagnostic_final_layer_id
        if final_layer_id is None:
            final_layer_id = getattr(self, "final_layer_id", None)
        # Freeze the actual queued tail at submission: the next step may prepare
        # a different layout before this consumer finishes the current one.
        final = layer_id is None or layer_id == final_layer_id
        self._diagnostic_pending.setdefault(id(request), deque()).append((snapshot, final))

    def _discard_diagnostics(self) -> None:
        for pending in self._diagnostic_pending.values():
            for snapshot, _ in pending:
                task = pool_diag.Task(snapshot)
                task.status = "not_processed"
                task.finish(incomplete=True)
        self._diagnostic_pending.clear()

    def _dispatch_diagnostic_request(self, request: Any) -> None:
        with self._diagnostic_lock:
            pending = self._diagnostic_pending.get(id(request))
            entry = pending.popleft() if pending else None
            if pending is not None and not pending:
                self._diagnostic_pending.pop(id(request), None)
        if entry is None:
            # Fences/control work and callers bypassing submission have no measured queue entry.
            self._handle_request(request)
            return
        snapshot, final = entry
        layers = getattr(request, "transfer_tasks", request)
        layer_id = (
            layers[0].layer_id
            if isinstance(layers, list) and layers and hasattr(layers[0], "layer_id")
            else getattr(request, "layer_id", None)
        )
        task = None
        if layer_id is not None:
            executions = tuple(sorted({str(dict(member).get("execution_id")) for member in snapshot.members}))
            key = (
                executions
                if executions != ("None",)
                else tuple(sorted(str(dict(member).get("request_id")) for member in snapshot.members))
            )
            if not snapshot.members and self._diagnostic_layer_task is not None:
                # A reuse-only load still belongs to the preceding queued batch.
                key = self._diagnostic_layer_key
            if self._diagnostic_layer_task is not None and key != self._diagnostic_layer_key:
                self._diagnostic_layer_task.finish(incomplete=True)
                self._diagnostic_layer_task = None
            if self._diagnostic_layer_task is None:
                self._diagnostic_layer_task = pool_diag.Task(snapshot)
                self._diagnostic_layer_key = key
            task = self._diagnostic_layer_task
        try:
            with pool_diag.processing(snapshot, task, final):
                self._handle_request(request)
        except BaseException:
            final = True
            raise
        finally:
            if final and task is self._diagnostic_layer_task:
                self._diagnostic_layer_task = None
'''

WAIT_OLD = '''        if not req_ids:
            return
        pending = set(req_ids)
        wait_start = time.perf_counter()
        while True:
            self.raise_if_failed()
            with self.done_task_lock:
                pending &= self.stored_requests.keys()
                if not pending:
                    break
            time.sleep(SAVE_REQUEST_POLL_INTERVAL_S)
        elapsed = time.perf_counter() - wait_start
        logger.debug(
            "Released-block KV save fence (%s) completed after waiting %.3f ms tp_rank=%d",
            ",".join(sorted(req_ids))[:200],
            elapsed * 1000,
            self.tp_rank,
        )
'''
WAIT_NEW = '''        # The fence wait is recorded as this step's worker_summary stage
        # "wait_for_put"; a step with nothing to wait for records 0.
        batch = diag.current_batch()
        fence_start_ns = time.monotonic_ns()
        fence_status = "ok"
        try:
            if not req_ids:
                return
            pending = set(req_ids)
            wait_start = time.perf_counter()
            while True:
                self.raise_if_failed()
                with self.done_task_lock:
                    pending &= self.stored_requests.keys()
                    if not pending:
                        break
                time.sleep(SAVE_REQUEST_POLL_INTERVAL_S)
            elapsed = time.perf_counter() - wait_start
            logger.debug(
                "Released-block KV save fence (%s) completed after waiting %.3f ms tp_rank=%d",
                ",".join(sorted(req_ids))[:200],
                elapsed * 1000,
                self.tp_rank,
            )
        except BaseException:
            fence_status = "error"
            raise
        finally:
            if batch is not None:
                batch.add_stage(
                    "wait_for_put",
                    start_ns=fence_start_ns,
                    end_ns=time.monotonic_ns(),
                    status=fence_status,
                    released_requests=len(req_ids) if req_ids else 0,
                    timing_scope="released_request_save_fence",
                )
'''

COMPLETION = '''
                    pool_diag.add(completed_store_jobs=1)
                    pool_diag.completion(
                        request_id=req_id,
                        execution_id=req_meta.pd_execution_id,
                        store_job_id=getattr(req_meta, "store_job_id", None),
                        tp_rank=self.tp_rank,
                        remaining_jobs=remaining,
                    )'''

EDITS = [
 # ---- imports
 ("from collections import defaultdict\n", "from collections import defaultdict, deque\n", 1),
 ("from vllm.v1.core.kv_cache_utils import maybe_convert_block_hash\n\nfrom vllm_ascend import envs\n",
  "from vllm.v1.core.kv_cache_utils import maybe_convert_block_hash\nfrom vllm.v1.metrics import pd_diagnostics as diag\n\nfrom vllm_ascend import envs\nfrom vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store import diagnostics as pool_diag\n", 1),
 # ---- LayerBatchBuilder (02: layerwise/kv_transfer.py)
 ("\n    def build_shared(self, task: LayerTransferTask, is_save: bool = True) -> SharedBlockData | None:\n",
  "\n    @pool_diag.timed(\"prepare\")\n    def build_shared(self, task: LayerTransferTask, is_save: bool = True) -> SharedBlockData | None:\n", 1),
 ("\n    def build_addrs(\n        self,\n        shared: SharedBlockData,\n",
  "\n    @pool_diag.timed(\"prepare\")\n    def build_addrs(\n        self,\n        shared: SharedBlockData,\n", 1),
 # ---- KVTransferThread base
 ("class KVTransferThread(threading.Thread):\n    def __init__(\n",
  "class KVTransferThread(threading.Thread):\n    _diag_operation = \"unknown\"\n\n    def __init__(\n", 1),
 ("        self.kv_events: list[BlockStored] = []\n        self._fatal_error: BaseException | None = None\n\n    def _get_block_size(",
  "        self.kv_events: list[BlockStored] = []\n        self._fatal_error: BaseException | None = None\n"
  "        # Pairs each submission's snapshot with its dequeue; the consumer pops\n"
  "        # under the same lock the producer registers under.\n"
  "        self._diagnostic_lock = threading.Lock()\n"
  "        self._diagnostic_pending: dict[int, deque[tuple[pool_diag.Snapshot, bool]]] = {}\n"
  "        self._diagnostic_final_layer_id: int | None = None\n"
  "        self._diagnostic_layer_task: pool_diag.Task | None = None\n"
  "        self._diagnostic_layer_key: tuple | None = None\n\n    def _get_block_size(", 1),
 ("\n    def add_request(self, request: Any) -> None:\n        self.request_queue.put(request)\n",
  BASE_METHODS, 1),
 # batch_copy
 ("""            res = self.m_store.store.batch_copy(
                split_gvas.tolist(),
                split_addrs.tolist(),
                split_sizes.tolist(),
                direction,
            )
            if res != 0:
                logger.error("[KVPOOL] batch_copy %s FAILED res=%d", dir_name, res)""",
  """            pool_diag.add(copy_submitted_ranges=len(split_gvas), copy_submitted_bytes=int(split_sizes.sum()))
            with pool_diag.stage("backend_batch_copy"):
                res = self.m_store.store.batch_copy(
                    split_gvas.tolist(),
                    split_addrs.tolist(),
                    split_sizes.tolist(),
                    direction,
                )
            pool_diag.add(copy_batch_succeeded=int(res == 0), copy_batch_failed=int(res != 0))
            if res != 0:
                logger.error("[KVPOOL] batch_copy %s FAILED res=%d", dir_name, res)""", 1),
 # run loop
 ("""                    self.request_queue.task_done()
                    continue
                self._handle_request(request_data)
            except Exception as e:
                self._fatal_error = e
""",
  """                    self.request_queue.task_done()
                    continue
                self._dispatch_diagnostic_request(request_data)
            except Exception as e:
                with self._diagnostic_lock:
                    self._fatal_error = e
                    self._discard_diagnostics()
""", 1),
 # lookup
 ("""    def lookup(
        self,
        keys: list[str],
    ) -> list[bool]:
        \"\"\"
        Check the existence of all keys from the cache engine.
        :return: A bool list where True means the key exists in store.
        \"\"\"
        try:
            res = self.m_store.exists(keys)  # type: ignore[assignment]
            exists_list = [False] * len(keys)
            for index, value in enumerate(res):  # type: ignore[arg-type]
                exists_list[index] = value == 1
            return exists_list
        except Exception as e:
            logger.error(""",
  """    @pool_diag.timed("lookup")
    def lookup(
        self,
        keys: list[str],
    ) -> list[bool]:
        \"\"\"
        Check the existence of all keys from the cache engine.
        :return: A bool list where True means the key exists in store.
        \"\"\"
        try:
            pool_diag.add(lookup_calls=1, lookup_keys=len(keys))
            res = self.m_store.exists(keys)  # type: ignore[assignment]
            exists_list = [False] * len(keys)
            for index, value in enumerate(res):  # type: ignore[arg-type]
                exists_list[index] = value == 1
            pool_diag.add(lookup_hits=sum(exists_list), lookup_misses=len(keys) - sum(exists_list))
            return exists_list
        except Exception as e:
            pool_diag.add(lookup_failures=1)
            logger.error(""", 1),
 ("""        if not require_exists_check and not self.m_store.requires_exists_before_put:
            return list(range(len(keys)))
""",
  """        if not require_exists_check and not self.m_store.requires_exists_before_put:
            pool_diag.add(lookup_skipped_keys=len(keys))
            return list(range(len(keys)))
""", 1),
 # ---- KVCacheStoreSendingThread
 ("class KVCacheStoreSendingThread(KVTransferThread):\n    def __init__(\n",
  "class KVCacheStoreSendingThread(KVTransferThread):\n    _diag_operation = \"save\"\n\n    def __init__(\n", 1),
 ("""        for request in requests:
            self.add_stored_request(request.req_id)
        for request in requests:
            self.request_queue.put(request)
        self.request_queue.put(save_batch)
""",
  """        for request in requests:
            self.add_stored_request(request.req_id)
        for request in requests:
            if self._fatal_error is None:
                with self._diagnostic_lock:
                    self._capture_diagnostics(request)
            self.request_queue.put(request)
        self.request_queue.put(save_batch)
""", 1),
 (WAIT_OLD, WAIT_NEW, 1),
 # tp-mismatch save path
 ("""            req_id = req_meta.req_id
            try:
                self.worker._store_kv_tp_mismatch(req_meta)
            except Exception:
                logger.exception("Failed to store KV cache for TP-mismatch request %s", req_id)
            finally:
                remaining = self.get_stored_request_count(req_id)
                if remaining == 0:
""",
  """            req_id = req_meta.req_id
            try:
                self.worker._store_kv_tp_mismatch(req_meta)
            except Exception:
                pool_diag.outcome("exception")
                logger.exception("Failed to store KV cache for TP-mismatch request %s", req_id)
            finally:
                remaining = self.get_stored_request_count(req_id)
                if remaining is not None:""" + COMPLETION.replace("\n                    ", "\n                    ", ) + """
                if remaining == 0:
""", 1),
 # normal save path
 ("""            if not tracked_request:
                return
            self._handle_stored_request(req_meta)
        except Exception:
            logger.exception("Failed to store KV cache for request %s", req_id)
        finally:
            remaining = self.dec_stored_request(req_id) if tracked_request else None
""",
  """            if not tracked_request:
                pool_diag.outcome("stale_job")
                return
            self._handle_stored_request(req_meta)
        except Exception:
            pool_diag.outcome("exception")
            logger.exception("Failed to store KV cache for request %s", req_id)
        finally:
            remaining = self.dec_stored_request(req_id) if tracked_request else None
            if remaining is not None:""" + COMPLETION + "\n", 1),
 ("""        \"\"\"Store missing KV chunks for one request.\"\"\"
        token_len = req_meta.token_len_chunk
""",
  """        \"\"\"Store missing KV chunks for one request.\"\"\"
        pool_diag.add(candidate_keys=0)
        token_len = req_meta.token_len_chunk
""", 1),
 ("""            if not keys:
                continue
            missing_indices = self._get_missing_indices(keys, require_exists_check=self.enable_kv_event)
            if not missing_indices:
                continue
""",
  """            if not keys:
                pool_diag.add(empty_groups=1)
                continue
            pool_diag.add(candidate_keys=len(keys))
            missing_indices = self._get_missing_indices(keys, require_exists_check=self.enable_kv_event)
            pool_diag.add(missing_keys=len(missing_indices), retained_keys=len(keys) - len(missing_indices))
            if not missing_indices:
                pool_diag.add(all_hit_groups=1)
                continue
""", 1),
 ("""            if current_event is not None:
                current_event.synchronize()
            self.m_store.put(keys, addrs, sizes)
""",
  """            if current_event is not None:
                with pool_diag.stage("sync"):
                    current_event.synchronize()
            pool_diag.put(self.m_store, keys, addrs, sizes)
""", 1),
 # ---- KVCacheStoreRecvingThread
 ("class KVCacheStoreRecvingThread(KVTransferThread):\n    def __init__(\n",
  "class KVCacheStoreRecvingThread(KVTransferThread):\n    _diag_operation = \"load\"\n\n    def __init__(\n", 1),
 ("""            if load_spec is None:
                logger.error("KV pool async recv request %s has no load spec; skip load.", req_id)
""",
  """            if load_spec is None:
                pool_diag.outcome("missing_load_spec")
                logger.error("KV pool async recv request %s has no load spec; skip load.", req_id)
""", 1),
 ("""            if not key_list:
                self.set_finished_request(req_id)
                return
            key_list_c = key_list[self.tp_rank % len(key_list) :] + key_list[: self.tp_rank % len(key_list)]
""",
  """            if not key_list:
                pool_diag.outcome("no_task")
                self.set_finished_request(req_id)
                return
            key_list_c = key_list[self.tp_rank % len(key_list) :] + key_list[: self.tp_rank % len(key_list)]
""", 1),
 ("            ret = self.m_store.get(key_list_c, addr_list_c, size_list_c)\n            if self._record_operation_cb is not None:\n",
  "            ret = pool_diag.get(self.m_store, key_list_c, addr_list_c, size_list_c)\n            if self._record_operation_cb is not None:\n", 1),
 # ---- Key-layer threads (02: layerwise/kv_transfer.py)
 ("class KVCacheStoreKeyLayerSendingThread(KVTransferThread):\n    def __init__(\n",
  "class KVCacheStoreKeyLayerSendingThread(KVTransferThread):\n    _diag_operation = \"save\"\n\n    def __init__(\n", 1),
 ("\n    def build_cached_process_tokens(self, task: LayerTransferTask) -> dict[int, list[tuple[int, int, list]]] | None:\n",
  "\n    @pool_diag.timed(\"build_key\")\n    def build_cached_process_tokens(self, task: LayerTransferTask) -> dict[int, list[tuple[int, int, list]]] | None:\n", 1),
 ("""            if keys_to_put:
                self.sync_save_events[layer_id].synchronize()
                self.m_store.put(keys_to_put, addrs_to_put, sizes_to_put)
""",
  """            if keys_to_put:
                with pool_diag.stage("sync"):
                    self.sync_save_events[layer_id].synchronize()
                pool_diag.put(self.m_store, keys_to_put, addrs_to_put, sizes_to_put)
""", 1),
 ("class KVCacheStoreKeyLayerRecvingThread(KVTransferThread):\n    def __init__(\n",
  "class KVCacheStoreKeyLayerRecvingThread(KVTransferThread):\n    _diag_operation = \"load\"\n\n    def __init__(\n", 1),
 ("\n    def _wait_for_save(self, layer_id: int) -> None:\n        while not self.layer_save_finished_events[layer_id].wait(timeout=10):\n",
  "\n    @pool_diag.timed(\"wait\")\n    def _wait_for_save(self, layer_id: int) -> None:\n        while not self.layer_save_finished_events[layer_id].wait(timeout=10):\n", 1),
 ("""        if data.attention_start_gate is not None:
            while not data.attention_start_gate.wait(timeout=10):
                logger.info("Layerwise %d load waits for attention compute start", layer_id)

        key_list = []
""",
  """        if data.attention_start_gate is not None:
            with pool_diag.stage("wait"):
                while not data.attention_start_gate.wait(timeout=10):
                    logger.info("Layerwise %d load waits for attention compute start", layer_id)

        key_list = []
""", 1),
 ("            self.m_store.get(key_list_c, addr_list_c, size_list_c)\n\n        if layer_id == self.final_layer_id:\n",
  "            pool_diag.get(self.m_store, key_list_c, addr_list_c, size_list_c)\n\n        if layer_id == self.final_layer_id:\n", 1),
 # ---- GVA / range layer threads
 ("class KVCacheStoreLayerSendingThread(KVTransferThread):\n    def __init__(\n",
  "class KVCacheStoreLayerSendingThread(KVTransferThread):\n    _diag_operation = \"save\"\n\n    def __init__(\n", 1),
 ("""        if active_keys:
            self.sync_save_events[layer_id].synchronize()
            active_buffers = [req_meta.all_buffers[index] for index in active_indices]
""",
  """        if active_keys:
            with pool_diag.stage("sync"):
                self.sync_save_events[layer_id].synchronize()
            active_buffers = [req_meta.all_buffers[index] for index in active_indices]
""", 1),
 ("""        if has_any_save:
            self.sync_save_events[physical_layer].synchronize()
""",
  """        if has_any_save:
            with pool_diag.stage("sync"):
                self.sync_save_events[physical_layer].synchronize()
""", 1),
 ("""            if res != 0:
                if self.raise_on_transfer_failure:
                    raise RuntimeError(f"Layerwise {physical_layer} save batch_copy failed with return code {res}")
""",
  """            if res != 0:
                pool_diag.outcome("failed")
                if self.raise_on_transfer_failure:
                    raise RuntimeError(f"Layerwise {physical_layer} save batch_copy failed with return code {res}")
""", 1),
 ("class KVCacheStoreLayerRecvingThread(KVTransferThread):\n    def __init__(\n",
  "class KVCacheStoreLayerRecvingThread(KVTransferThread):\n    _diag_operation = \"load\"\n\n    def __init__(\n", 1),
 ("""            if wait_for_save is not None:
                while not self.layer_save_finished_events[wait_for_save].wait(timeout=10):
                    if self.save_failure_checker is not None:
                        self.save_failure_checker()
                    logger.info("Layerwise %d save wait timed out, keep waiting before load", wait_for_save)
                if self.save_failure_checker is not None:
                    self.save_failure_checker()
                self.sync_save_events[wait_for_save].synchronize()
                self.layer_save_finished_events[wait_for_save].clear()

            if data.attention_start_gate is not None:
                while not data.attention_start_gate.wait(timeout=10):
                    logger.info("Layerwise %d load waits for attention compute start", layer_id)
""",
  """            if wait_for_save is not None:
                with pool_diag.stage("wait"):
                    while not self.layer_save_finished_events[wait_for_save].wait(timeout=10):
                        if self.save_failure_checker is not None:
                            self.save_failure_checker()
                        logger.info("Layerwise %d save wait timed out, keep waiting before load", wait_for_save)
                if self.save_failure_checker is not None:
                    self.save_failure_checker()
                with pool_diag.stage("sync"):
                    self.sync_save_events[wait_for_save].synchronize()
                self.layer_save_finished_events[wait_for_save].clear()

            if data.attention_start_gate is not None:
                with pool_diag.stage("wait"):
                    while not data.attention_start_gate.wait(timeout=10):
                        logger.info("Layerwise %d load waits for attention compute start", layer_id)
""", 1),
 ("""        if wait_for_save is not None:
            while not self.layer_save_finished_events[wait_for_save].wait(timeout=10):
                if self.save_failure_checker is not None:
                    self.save_failure_checker()
                logger.info("Layerwise %d save wait timed out, keep waiting before load", wait_for_save)
            if self.save_failure_checker is not None:
                self.save_failure_checker()
            # Non-saving TP ranks have no D2H task to synchronize the event.
            # Their CPU save-finished signal only means the event was recorded;
            # wait for the NPU work before reusing the local HBM buffer.
            self.sync_save_events[wait_for_save].synchronize()
""",
  """        if wait_for_save is not None:
            with pool_diag.stage("wait"):
                while not self.layer_save_finished_events[wait_for_save].wait(timeout=10):
                    if self.save_failure_checker is not None:
                        self.save_failure_checker()
                    logger.info("Layerwise %d save wait timed out, keep waiting before load", wait_for_save)
            if self.save_failure_checker is not None:
                self.save_failure_checker()
            # Non-saving TP ranks have no D2H task to synchronize the event.
            # Their CPU save-finished signal only means the event was recorded;
            # wait for the NPU work before reusing the local HBM buffer.
            with pool_diag.stage("sync"):
                self.sync_save_events[wait_for_save].synchronize()
""", 1),
 ("""        if attention_start_gate is not None:
            while not attention_start_gate.wait(timeout=10):
                logger.info("Layerwise %d load waits for attention compute start", layer_id)
""",
  """        if attention_start_gate is not None:
            with pool_diag.stage("wait"):
                while not attention_start_gate.wait(timeout=10):
                    logger.info("Layerwise %d load waits for attention compute start", layer_id)
""", 1),
 ("""        if res != 0:
            if self.raise_on_transfer_failure:
                raise RuntimeError(f"Layerwise {layer_id} load batch_copy failed with return code {res}")
""",
  """        if res != 0:
            pool_diag.outcome("failed")
            if self.raise_on_transfer_failure:
                raise RuntimeError(f"Layerwise {layer_id} load batch_copy failed with return code {res}")
""", 1),
]
