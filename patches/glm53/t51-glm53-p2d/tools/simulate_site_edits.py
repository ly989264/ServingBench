"""Simulated local edits on the original mooncake_connector.py, to test replaying the patch onto a site file.

Usage: python3 simulate_site_edits.py <in> <out> A|B|C
A: lines added away from every patch hunk.
B: A, plus edits on context lines next to two patched spots.
C: B, plus edits on the very lines most changes anchor on, in <module>, add_request,
   _transfer_kv_cache_all_groups, _handle_request, build_connector_meta and request_finished
   (the functions the site's file is known to change). Every edit keeps the code's behaviour.
"""

import sys

src, dst, mode = sys.argv[1:4]
lines = open(src).read().split("\n")


def index_of(text):
    hits = [n for n, line in enumerate(lines) if text in line]
    assert len(hits) == 1, (text, len(hits))
    return hits[0]


def insert_after(text, block):
    i = index_of(text)
    lines[i + 1:i + 1] = block


def replace_in(text, old, new):
    i = index_of(text)
    assert old in lines[i], (text, old)
    lines[i] = lines[i].replace(old, new)


insert_after("from vllm_ascend.utils import (", ["    # site-local import note"])
insert_after("    def reformat_kv_cache_with_fused_op(", ["        # site-local note %d" % k for k in range(40)])
insert_after("    def _model_uses_compress(self) -> bool:", ["        # site-local scheduler note"])
if mode in ("B", "C"):
    insert_after("KV_CACHE_BUFFER_ALIGNMENT = 2 * 1024 * 1024", ["SITE_LOCAL_FLAG = 0"])
    replace_in('logger.debug("Starting to transfer KV cache for request %s.", remote_request_id)',
               "Starting to transfer", "Start transfer (site)")
if mode == "C":
    # <module>
    replace_in("KV_CACHE_BUFFER_ALIGNMENT = 2 * 1024 * 1024", "2 * 1024 * 1024", "2 << 20")
    insert_after("import time", ["import site_local_module_note  # noqa  (never imported: see below)"])
    lines[index_of("import site_local_module_note")] = "# site-local: extra import removed"
    # add_request: the parameter line and the debug message both changed
    replace_in("remote_block_ids_replicate_k: BlockIds | None = None,", ",", ",  # site: replicate-k")
    replace_in('logger.debug("Adding request %s to the queue.Trans info:%s", request_id, trans_info)',
               "Adding request %s to the queue.Trans info:%s", "Queue request %s: %s")
    # _transfer_kv_cache_all_groups: comment gone, conditions and READ arguments rewritten
    lines.pop(index_of("        # Check if we have the remote metadata cached."))
    replace_in("if num_local_blocks == 0 and not has_replicate_k_blocks:", ":", " and True:")
    i = index_of("ret = self.engine.batch_transfer_sync_read(session_id, src_list, dst_list, length_list)")
    lines[i:i + 1] = [
        "        site_lengths = list(length_list)",
        "        ret = self.engine.batch_transfer_sync_read(session_id, src_list, dst_list, site_lengths)",
    ]
    replace_in("remote_layer_name_to_idx = build_layer_name_to_metadata_idx(remote_kv_group2layeridx)",
               "remote_layer_name_to_idx =", "remote_layer_name_to_idx  =")
    # _handle_request: message changed
    replace_in('logger.warning("Skipping KV cache transfer for request. remote_request_id=%s. ", remote_request_id)',
               "Skipping KV cache transfer for request.", "Skip KV transfer (earlier pull failed).")
    # build_connector_meta: comment gone; request_finished: the anchor line carries a comment
    lines.pop(index_of("        # Clear the list once workers start the transfers"))
    replace_in("self._reqs_need_send[request.request_id] = time.time()", "time.time()", "time.time()  # site")
    # _submit_request: first statement rewritten
    replace_in('peer_key = (request_data["remote_host"], request_data["remote_handshake_port"])',
               "peer_key = (", "peer_key: tuple[str, int] = (")
open(dst, "w").write("\n".join(lines))
