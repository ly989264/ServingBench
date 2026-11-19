EDITS = [
 ("from vllm_ascend.core.kv_cache_placement import map_kvpp_layers_to_owners\n",
  "from vllm_ascend.core.kv_cache_placement import map_kvpp_layers_to_owners\nfrom vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store import diagnostics as pool_diag\n", 1),
 # ---- wait_for_save -> pool_task[save_submit]
 ("""    def wait_for_save(self, connector_metadata: AscendConnectorMetadata) -> None:
        current_event = None
""", """    @pool_diag.task("save_submit")
    def wait_for_save(self, connector_metadata: AscendConnectorMetadata) -> None:
        current_event = None
""", 1),
 ("""            can_save = request.can_save
            if can_save is None or not can_save:
                continue
            if current_event is None:
                current_event = torch.npu.Event()
""", """            can_save = request.can_save
            if can_save is None or not can_save:
                pool_diag.add(save_eligibility_unknown=int(can_save is None), save_skipped=int(can_save is False))
                continue
            if current_event is None:
                current_event = torch.npu.Event()
""", 1),
 ("""        if not requests:
            return

        if not isinstance(send_thread, KVCacheStoreSendingThread):
            raise TypeError(
                f"Non-layerwise KV save requires KVCacheStoreSendingThread, but got {type(send_thread).__name__}"
            )
        self._previous_save_batch = send_thread.add_save_batch(requests)
""", """        if not requests:
            pool_diag.outcome("no_task")
            return

        if not isinstance(send_thread, KVCacheStoreSendingThread):
            raise TypeError(
                f"Non-layerwise KV save requires KVCacheStoreSendingThread, but got {type(send_thread).__name__}"
            )
        with pool_diag.stage("queue_submission"):
            self._previous_save_batch = send_thread.add_save_batch(requests)
        pool_diag.outcome("submission_returned")
""", 1),
 # ---- tp-mismatch load / store
 ("""            block_hashes, block_ids, token_len, mask_num
        )
        if not keys:
            return
        offset = self.tp_rank % len(keys)
""", """            block_hashes, block_ids, token_len, mask_num
        )
        if not keys:
            pool_diag.outcome("no_task")
            return
        offset = self.tp_rank % len(keys)
""", 1),
 ("        ret = self.m_store.get(keys_c, addrs_c, sizes_c)\n",
  "        ret = pool_diag.get(self.m_store, keys_c, addrs_c, sizes_c)\n", 1),
 ("""            if not keys:
                return
            exists_states = send_thread.lookup(keys)  # type: ignore[attr-defined]
            missing_indices = [i for i, exists in enumerate(exists_states) if not exists]
            if not missing_indices:
                return
""", """            if not keys:
                pool_diag.outcome("no_task")
                return
            pool_diag.add(candidate_keys=len(keys))
            exists_states = send_thread.lookup(keys)  # type: ignore[attr-defined]
            missing_indices = [i for i, exists in enumerate(exists_states) if not exists]
            pool_diag.add(missing_keys=len(missing_indices), retained_keys=len(keys) - len(missing_indices))
            if not missing_indices:
                pool_diag.add(all_hit_groups=1)
                return
""", 1),
 ("""            if req_meta.current_event is not None:
                req_meta.current_event.synchronize()
            logger.debug(
                "KV pool worker tp_mismatch put req=%s keys=%d sample_keys=%s",
                req_id,
                len(keys),
                keys[:3],
            )
            self.m_store.put(keys, addrs, sizes)
""", """            if req_meta.current_event is not None:
                with pool_diag.stage("sync"):
                    req_meta.current_event.synchronize()
            logger.debug(
                "KV pool worker tp_mismatch put req=%s keys=%d sample_keys=%s",
                req_id,
                len(keys),
                keys[:3],
            )
            pool_diag.put(self.m_store, keys, addrs, sizes)
""", 1),
 # ---- layerwise (02: layerwise/pool_worker.py)
 ("""    def _filter_pool_existing_keys(self, keys: list[str]) -> list[str]:
""", """    @pool_diag.timed("lookup")
    def _filter_pool_existing_keys(self, keys: list[str]) -> list[str]:
""", 1),
 ("""        if not keys:
            return []
        if not getattr(self.m_store, "requires_exists_before_put", True):
            return keys
        exists = getattr(self.m_store, "exists", None)
        if not callable(exists):
            exists = getattr(self.m_store, "batch_is_exist", None)
        if not callable(exists):
            return keys
""", """        pool_diag.add(candidate_keys=len(keys))
        if not keys:
            return []
        if not getattr(self.m_store, "requires_exists_before_put", True):
            pool_diag.add(lookup_skipped_keys=len(keys))
            return keys
        exists = getattr(self.m_store, "exists", None)
        if not callable(exists):
            exists = getattr(self.m_store, "batch_is_exist", None)
        if not callable(exists):
            pool_diag.add(lookup_skipped_keys=len(keys))
            return keys
""", 1),
 ("""                states = exists(key_batch)
                if len(states) != len(key_batch):
""", """                states = exists(key_batch)
                pool_diag.add(lookup_calls=1, lookup_keys=len(key_batch),
                              lookup_hits=sum(int(state) == 1 for state in states))
                if len(states) != len(key_batch):
""", 1),
 ("""            except Exception as exc:
                logger.error(
                    "Block-key pool existence check failed keys=%d error=%s; treating all as missing",
                    len(key_batch),
                    exc,
                )
                missing.extend(key_batch)
        return missing
""", """            except Exception as exc:
                pool_diag.add(lookup_failures=1)
                logger.error(
                    "Block-key pool existence check failed keys=%d error=%s; treating all as missing",
                    len(key_batch),
                    exc,
                )
                missing.extend(key_batch)
        pool_diag.add(missing_keys=len(missing), retained_keys=len(keys) - len(missing),
                      all_hit_groups=int(bool(keys) and not missing))
        return missing
""", 1),
 ("""    def _prepare_layerwise_put_session(self, request: ReqMeta) -> None:
        request.save_block_keys = []
        request.save_last_block_key = None
        if not self._is_layerwise_save_owner() or not request.can_save:
            return
""", """    @pool_diag.task("save_prepare")
    @pool_diag.timed("prepare")
    def _prepare_layerwise_put_session(self, request: ReqMeta) -> None:
        request.save_block_keys = []
        request.save_last_block_key = None
        if not self._is_layerwise_save_owner() or not request.can_save:
            pool_diag.outcome("skipped")
            return
""", 1),
 ("""        requested_keys = list(dict.fromkeys(key for key, _, _ in key_slots))
        if not requested_keys:
            return
        with self._put_started_keys_lock:
            previously_started = set(requested_keys) & self._put_started_keys
            new_keys = [key for key in requested_keys if key not in self._put_started_keys]
""", """        requested_keys = list(dict.fromkeys(key for key, _, _ in key_slots))
        pool_diag.add(candidate_keys=len(requested_keys))
        if not requested_keys:
            return
        with self._put_started_keys_lock:
            previously_started = set(requested_keys) & self._put_started_keys
            new_keys = [key for key in requested_keys if key not in self._put_started_keys]
            pool_diag.add(inflight_keys=len(previously_started), missing_keys=len(new_keys))
""", 1),
 ("""    def _prepare_layerwise_get_session(self, request: ReqMeta) -> list[tuple[str, int, int | None]]:
""", """    @pool_diag.task("load_key_prepare")
    @pool_diag.timed("build_key")
    def _prepare_layerwise_get_session(self, request: ReqMeta) -> list[tuple[str, int, int | None]]:
""", 1),
 ("""    def _open_layerwise_get_sessions(self, request_key_slots: list[tuple[ReqMeta, str, int, int | None]]) -> None:
""", """    @pool_diag.task("load_prepare")
    def _open_layerwise_get_sessions(self, request_key_slots: list[tuple[ReqMeta, str, int, int | None]]) -> None:
""", 1),
 ("""                if first_task is None:
                    continue
                shared = self.kv_send_thread.build_shared_data(first_task)
""", """                if first_task is None:
                    continue
                with pool_diag.processing(pool_diag.capture(first_task, "save_prepare", self.tp_rank, queued=False)):
                    shared = self.kv_send_thread.build_shared_data(first_task)
""", 1),
 ("""            cached = self.kv_send_thread.build_cached_process_tokens(first_task)
""", """            with pool_diag.processing(pool_diag.capture(first_task, "save_key_prepare", self.tp_rank, queued=False)):
                cached = self.kv_send_thread.build_cached_process_tokens(first_task)
""", 1),
 ("""                if first_task is None:
                    continue
                shared = self.kv_recv_thread.build_shared_data(first_task)
""", """                if first_task is None:
                    continue
                with pool_diag.processing(pool_diag.capture(first_task, "load_prepare", self.tp_rank, queued=False)):
                    shared = self.kv_recv_thread.build_shared_data(first_task)
""", 1),
 ("""                    if key not in shared_by_ranges:
                        shared = self.kv_recv_thread.build_shared_data(task)
""", """                    if key not in shared_by_ranges:
                        with pool_diag.processing(pool_diag.capture(task, "load_prepare", self.tp_rank, queued=False)):
                            shared = self.kv_recv_thread.build_shared_data(task)
""", 1),
 ("""        else:
            num_local = self.num_layers
        self._wait_for_final_layer_save(num_local, self.kv_send_thread)

    def process_layer_data(self, requests: list[ReqMeta]) -> None:
""", """        else:
            num_local = self.num_layers
        snapshot = self._diagnostic_deferred_save_snapshot
        self._diagnostic_deferred_save_snapshot = None
        with pool_diag.processing(snapshot):
            with pool_diag.stage("save_wait"):
                self._wait_for_final_layer_save(num_local, self.kv_send_thread)
            pool_diag.outcome("wait_returned")

    def process_layer_data(self, requests: list[ReqMeta]) -> None:
""", 1),
 ("""                self._process_load_for_layer_batch(
                    group_requests.get(group_id, requests), local_layer, group_id, layer_idx_in_group
                )
        self._build_shared_load_data()
""", """                self._process_load_for_layer_batch(
                    group_requests.get(group_id, requests), local_layer, group_id, layer_idx_in_group
                )
        self._build_shared_load_data()
        # The last actual queue entry is the completion fence, not necessarily
        # the model's final layer (which may have no reachable transfer rows).
        if self.kv_send_thread is not None:
            self.kv_send_thread._diagnostic_final_layer_id = max(
                (layer_id for layer_id, tasks in enumerate(self.layer_save_tasks) if tasks), default=-1
            )
        if self.kv_recv_thread is not None:
            self.kv_recv_thread._diagnostic_final_layer_id = max(
                (
                    layer_id
                    for layer_id, tasks in enumerate(self.layer_load_tasks)
                    if tasks or layer_id in self.prefetch_layer_map
                ),
                default=-1,
            )
""", 1),
 ("""                self._pending_last_save_drain = True
            else:
                self._wait_for_final_layer_save(num_local, send_thread)
        self.current_layer += 1
""", """                self._pending_last_save_drain = True
                self._diagnostic_deferred_save_snapshot = pool_diag.capture(
                    connector_metadata, "save_wait", self.tp_rank, queued=False
                )
            else:
                with pool_diag.processing(pool_diag.capture(connector_metadata, "save_wait", self.tp_rank, queued=False)):
                    with pool_diag.stage("save_wait"):
                        self._wait_for_final_layer_save(num_local, send_thread)
                    pool_diag.outcome("wait_returned")
        self.current_layer += 1
""", 1),
]
