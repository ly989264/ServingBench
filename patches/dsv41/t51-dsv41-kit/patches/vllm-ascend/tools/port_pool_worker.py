#!/usr/bin/env python3
"""Re-apply patch 02's pool_worker.py edits onto the f9c8e0bdb file (the 3-way merge conflicts there).

The edits are the semantic ones of 02 on RC1 (diff -w): one import, a diagnostics scope around
each start_load_kv request, outcome markers, backend calls routed through pool_diag, the
save_submit task decorator and its counters, and the TP-mismatch path counters.
"""
import sys

src, dst = sys.argv[1:3]
s = open(src).read()


def rep(old, new, count=1):
    global s
    n = s.count(old)
    if n != count:
        raise SystemExit("anchor found %d times (want %d): %r" % (n, count, old[:90]))
    s = s.replace(old, new)


rep(
    "from vllm_ascend.core.kv_cache_placement import map_kvpp_layers_to_owners\n",
    "from vllm_ascend.core.kv_cache_placement import map_kvpp_layers_to_owners\n"
    "from vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store import diagnostics as pool_diag\n",
)

# start_load_kv: wrap each request in a diagnostics scope (body indented by 4)
head = "        for request in metadata.requests:\n            load_spec = request.load_spec\n"
start = s.index(head)
end = s.index("    def _record_kv_connector_operation(", start)
body = s[start + len("        for request in metadata.requests:\n") : end]
body = "".join(("    " + line if line.strip() else line) for line in body.splitlines(True))
body = body.rstrip("\n") + "\n\n"
wrapped = (
    "        for request in metadata.requests:\n"
    "            with pool_diag.processing(\n"
    '                pool_diag.capture(request, "load_submit" if self.load_async else "load", self.tp_rank, queued=False)\n'
    "            ):\n" + body
)
s = s[:start] + wrapped + s[end:]
rep(
    "                if load_spec is None or not load_spec.can_load:  # load =0\n",
    "                if load_spec is None or not load_spec.can_load:  # load =0\n"
    '                    pool_diag.outcome("missing_load_spec" if load_spec is None else "skipped")\n',
)
rep(
    "                    self.kv_recv_thread.add_request(  # type: ignore[union-attr]\n"
    "                        request,\n"
    "                    )\n"
    "                    continue\n",
    "                    self.kv_recv_thread.add_request(  # type: ignore[union-attr]\n"
    "                        request,\n"
    "                    )\n"
    '                    pool_diag.outcome("submission_returned")\n'
    "                    continue\n",
)
rep(
    "                if not key_list:\n                    continue\n",
    '                if not key_list:\n                    pool_diag.outcome("no_task")\n                    continue\n',
)
rep(
    "                ret = self.m_store.get(key_list_c, addr_list_c, size_list_c)\n",
    "                ret = pool_diag.get(self.m_store, key_list_c, addr_list_c, size_list_c)\n",
)

# save submission
rep(
    "    def wait_for_save(self, connector_metadata: AscendConnectorMetadata) -> None:\n",
    '    @pool_diag.task("save_submit")\n'
    "    def wait_for_save(self, connector_metadata: AscendConnectorMetadata) -> None:\n",
)
rep(
    "            can_save = request.can_save\n"
    "            if can_save is None or not can_save:\n"
    "                continue\n",
    "            can_save = request.can_save\n"
    "            if can_save is None or not can_save:\n"
    "                pool_diag.add(save_eligibility_unknown=int(can_save is None), save_skipped=int(can_save is False))\n"
    "                continue\n",
)
rep(
    "        if not requests:\n"
    "            return\n"
    "\n"
    "        self._previous_save_batch = send_thread.add_save_batch(requests)\n",
    "        if not requests:\n"
    '            pool_diag.outcome("no_task")\n'
    "            return\n"
    "\n"
    '        with pool_diag.stage("queue_submission"):\n'
    "            self._previous_save_batch = send_thread.add_save_batch(requests)\n"
    '        pool_diag.outcome("submission_returned")\n',
)

# TP-mismatch load and save paths
rep(
    "        if not keys:\n            return None\n        offset = self.tp_rank % len(keys)\n",
    '        if not keys:\n            pool_diag.outcome("no_task")\n            return None\n        offset = self.tp_rank % len(keys)\n',
)
rep(
    "        ret = self.m_store.get(keys_c, addrs_c, sizes_c)\n",
    "        ret = pool_diag.get(self.m_store, keys_c, addrs_c, sizes_c)\n",
)
rep(
    "        if not keys:\n            return True\n        logical_key_count = len(keys)\n",
    '        if not keys:\n            pool_diag.outcome("no_task")\n            return True\n'
    "        pool_diag.add(candidate_keys=len(keys))\n        logical_key_count = len(keys)\n",
)
rep(
    "        missing_indices = [i for i, exists in enumerate(exists_states) if not exists]\n"
    "        if not missing_indices:\n"
    "            return True\n",
    "        missing_indices = [i for i, exists in enumerate(exists_states) if not exists]\n"
    "        pool_diag.add(missing_keys=len(missing_indices), retained_keys=len(keys) - len(missing_indices))\n"
    "        if not missing_indices:\n"
    "            pool_diag.add(all_hit_groups=1)\n"
    "            return True\n",
)
rep(
    "        if req_meta.current_event is not None:\n"
    "            req_meta.current_event.synchronize()\n",
    "        if req_meta.current_event is not None:\n"
    '            with pool_diag.stage("sync"):\n'
    "                req_meta.current_event.synchronize()\n",
)
rep(
    "        put_result = self.m_store.put(missing_keys, missing_addrs, missing_sizes)\n",
    "        put_result = pool_diag.put(self.m_store, missing_keys, missing_addrs, missing_sizes)\n",
)
open(dst, "w").write(s)
print("ported", dst)
