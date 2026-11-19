"""Lab cases for field_check/extra_checks.py: `python3 -I -B test_extra_checks.py <extra_checks.py> <original.py> <replacement.py> <edited_C.py>`.

The original, the replacement file and the scenario-C edited file (behaviour-neutral edits) give the
original values with no "!". Each simulated site change for R1-R5 is reported on the item it affects.
"""

import importlib.util
import sys

path, original_path, replacement_path, edited_path = sys.argv[1:5]
spec = importlib.util.spec_from_file_location("extra_checks", path)
ec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ec)
original = open(original_path).read()
failures = []


def edit(source, old, new, count=1):
    assert source.count(old) == count, (old, source.count(old))
    return source.replace(old, new)


def expect(label, source, line):
    got = ec.extra_line(source)
    ok = got == line
    print(("ok   " if ok else "FAIL ") + f"{label}: {got}")
    if not ok:
        print(f"     expected {line}")
        failures.append(label)


BASE = "p2d_extra X1=1 X2=1/1 X3=2 X4=4/1 X5=1 X6=ok"
expect("original d4ad49796", original, BASE)
expect("replacement (original + p2d patch)", open(replacement_path).read(), BASE)
expect("scenario C (behaviour-neutral site edits)", open(edited_path).read(), BASE)

RESET = '            # Only trigger 1 KV transfer per request.\n            params["do_remote_prefill"] = False\n'
expect("R1 the per-request reset removed (a request can be read again)",
       edit(original, RESET, "            # site: reset moved elsewhere\n"),
       "p2d_extra X1=1 X2=0/1! X3=2 X4=4/1 X5=1 X6=ok")
expect("R1 a second write to _reqs_need_recv (a request re-queued)",
       edit(original, RESET, RESET + "            if params.get(\"site_retry\"):\n"
                                     "                self._reqs_need_recv[request.request_id] = self._reqs_need_recv.get(request.request_id)\n"),
       "p2d_extra X1=1 X2=1/2! X3=2 X4=4/1 X5=1 X6=ok")
expect("R2 READ failure no longer raises",
       edit(original, '            raise RuntimeError(f"Mooncake transfer failed, ret: {ret}")\n',
            "            self.site_failure_reason = ret\n"),
       "p2d_extra X1=0! X2=1/1 X3=2 X4=4/1 X5=1 X6=ok")
expect("R3 one more request marked received without a read",
       edit(original, "            remote_req_id = meta.remote_request_id\n",
            "            if getattr(meta, \"site_skip\", False):\n"
            "                self.kv_recv_thread.task_tracker.add_not_transfer_request(req_id)\n"
            "                continue\n            remote_req_id = meta.remote_request_id\n"),
       "p2d_extra X1=1 X2=1/1 X3=3! X4=4/1 X5=1 X6=ok")
expect("R4 another return handing kv_transfer_params to D",
       edit(original, "        num_prompt_blocks = math.ceil(len(request.prompt_token_ids) / self.block_size)\n",
            "        num_prompt_blocks = math.ceil(len(request.prompt_token_ids) / self.block_size)\n"
            "        if params.get(\"site_reason\"):\n            return False, dict(do_remote_prefill=True)\n"),
       "p2d_extra X1=1 X2=1/1 X3=2 X4=5/2! X5=1 X6=ok")
expect("R4 a second write to _reqs_need_send",
       edit(original, "            self._reqs_need_send[request.request_id] = time.time()\n",
            "            self._reqs_need_send[request.request_id] = time.time()\n"
            "            self._reqs_need_send[request.request_id + \"-site\"] = time.time()\n"),
       "p2d_extra X1=1 X2=1/1 X3=2 X4=4/1 X5=2! X6=ok")
expect("R5 trans_info request_id no longer the parameter",
       edit(original, '            "request_id": request_id,\n            "local_block_ids": local_block_ids,\n',
            '            "request_id": f"{request_id}-site",\n            "local_block_ids": local_block_ids,\n'),
       "p2d_extra X1=1 X2=1/1 X3=2 X4=4/1 X5=1 X6=changed!")
expect("a checked function renamed (missing)",
       edit(original, "    def update_state_after_alloc(self, request: \"Request\", blocks: \"KVCacheBlocks\", num_external_tokens: int):\n        params",
            "    def update_state_after_alloc_site(self, request: \"Request\", blocks: \"KVCacheBlocks\", num_external_tokens: int):\n        params"),
       "p2d_extra X1=1 X2=missing! X3=2 X4=4/1 X5=1 X6=ok")
expect("source that does not parse", "def broken(:\n",
       "p2d_extra X1=missing! X2=missing! X3=missing! X4=missing! X5=missing! X6=missing!")
values = ec.extra_checks(original)
print(("ok   " if values == ec.EXPECTED else "FAIL ") + f"extra_checks(original) == EXPECTED: {values}")
if values != ec.EXPECTED:
    failures.append("EXPECTED")
print(f"{len(failures)} failed")
sys.exit(1 if failures else 0)
