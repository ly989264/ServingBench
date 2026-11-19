"""Wrap each request of KVPoolWorker.start_load_kv in a pool_diag task (patch 02 shape) on d4ad49796."""
import sys
path = sys.argv[1]
src = open(path).read()
head = "    def start_load_kv(self, metadata: AscendConnectorMetadata):\n"
assert src.count(head) == 1
start = src.index(head)
end = src.index("\n    def _record_kv_connector_operation(", start)
body = src[start:end]
loop = "        for request in metadata.requests:\n"
assert body.count(loop) == 1
pre, rest = body.split(loop)
lines = rest.split("\n")
out = []
for line in lines:
    out.append(("    " + line) if line.strip() else line)
rest = "\n".join(out)
wrapped = (
    pre + loop
    + "            with pool_diag.processing(\n"
    + "                pool_diag.capture(request, \"load_submit\" if self.load_async else \"load\", self.tp_rank, queued=False)\n"
    + "            ):\n"
    + rest
)
edits = [
    ("""                if load_spec is None or not load_spec.can_load:  # load =0
                    logger.debug(""",
     """                if load_spec is None or not load_spec.can_load:  # load =0
                    pool_diag.outcome("missing_load_spec" if load_spec is None else "skipped")
                    logger.debug("""),
    ("""                    self.kv_recv_thread.add_request(  # type: ignore[union-attr]
                        request,
                    )
                    continue
""",
     """                    self.kv_recv_thread.add_request(  # type: ignore[union-attr]
                        request,
                    )
                    pool_diag.outcome("submission_returned")
                    continue
"""),
    ("""                if not key_list:
                    continue
""",
     """                if not key_list:
                    pool_diag.outcome("no_task")
                    continue
"""),
    ("                ret = self.m_store.get(key_list_c, addr_list_c, size_list_c)\n",
     "                ret = pool_diag.get(self.m_store, key_list_c, addr_list_c, size_list_c)\n"),
]
for old, new in edits:
    assert wrapped.count(old) == 1, old[:60]
    wrapped = wrapped.replace(old, new)
src = src[:start] + wrapped + src[end:]
open(path, "w").write(src)
print("start_load_kv wrapped")
