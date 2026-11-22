"""Guards of replay_p2d.py, standard library only: run as
`python3 -I test_replay_guards.py <replay_p2d.py> <original.py> <replacement.py> <outdir> [<t51 replay_p2d.py> <t51 replacement.py>
[<t56 replay_p2d.py> <t56 replacement.py>]]`.

1. On the original: exit 0 and a result byte-identical to the replacement file.
2. Every alternative anchor of every change, forced on the original: it locates, the result checks pass.
   One file per change set is written to <outdir>/alt_*.py for the follow-up test run (all last alternatives).
3. Edits that keep the instrumentation valid (an except variable renamed, an extra argument in the READ call,
   a decorator on the method the receiver methods are inserted before): exit 0.
4. Edits that would make the records wrong: exit 3 naming the stop fact, nothing written.
5. An anchor gone with no alternative left: exit 2 naming the change; an already patched file: exit 6.
6. T56 (C29, F32): the call of update_done_task_count written with a keyword argument still locates C29 (its
   prefix alternative); the call moved out of `if <done>:` or split over several lines stops (F32).
7. Upgrading from T51 (with the T51 script and replacement): the T51 script replays the original into the T51
   replacement byte for byte (the proof step of the upgrade); this script refuses the T51 file and its own result
   with exit 6 and says which version the file has.
8. Upgrading from T56 (with the T56 script and replacement): the T56 script replays the original into the T56
   replacement byte for byte (the proof step); this script refuses the T56 file with exit 6, says T56, names the
   T56 script and says to restore the original first; it refuses its own result naming T58.
"""

import os
import re
import subprocess
import sys

replay, original_path, replacement_path, outdir = sys.argv[1:5]
t51_replay, t51_replacement_path, t56_replay, t56_replacement_path = (sys.argv[5:9] + [None] * 4)[:4]
original = open(original_path).read()
replacement = open(replacement_path).read()
os.makedirs(outdir, exist_ok=True)
failures = []


def run(source, *extra, name="case", script=None):
    path = os.path.join(outdir, f"in_{name}.py")
    out = os.path.join(outdir, f"out_{name}.py")
    with open(path, "w") as handle:
        handle.write(source)
    if os.path.exists(out):
        os.remove(out)
    proc = subprocess.run([sys.executable, "-I", script or replay, path, "--out", out, *extra], capture_output=True, text=True)
    text = proc.stdout + proc.stderr
    return proc.returncode, text, (open(out).read() if os.path.exists(out) else None)


def expect(cond, label, detail=""):
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        failures.append(label)
        if detail:
            print(detail)


def edit(source, old, new, count=1):
    assert source.count(old) == count, (old, source.count(old))
    return source.replace(old, new)


# 1
code, text, out = run(original, name="original")
expect(code == 0 and out == replacement, "original: exit 0, result == replacement", text)

# 2
spec = __import__("importlib.util").util.spec_from_file_location("replay", replay)
module = __import__("importlib.util").util.module_from_spec(spec)
spec.loader.exec_module(module)
last = []
for change in module.CHANGES:
    for rank in range(2, len(change["alts"]) + 1):
        code, text, out = run(original, "--force-alt", f"{change['id']}={rank}", name=f"{change['id']}_{rank}")
        expect(code == 0 and out is not None, f"{change['id']} alternative {rank} locates on the original", text)
    if len(change["alts"]) > 1:
        last.append(f"{change['id']}={len(change['alts'])}")
args = [item for force in last for item in ("--force-alt", force)]
code, text, out = run(original, *args, name="all_last_alternatives")
expect(code == 0, "all changes on their last alternative at once", text)
if out is not None:
    with open(os.path.join(outdir, "alt_all_last.py"), "w") as handle:
        handle.write(out)

# 3
renamed = edit(original, "                except Exception as e:\n                    transfer_failed = True",
               "                except Exception as transfer_error:\n                    transfer_failed = True")
renamed = edit(renamed, 'logger.exception("Failed to transfer KV cache for request %s: %s", remote_request_id, e)',
               'logger.exception("Failed to transfer KV cache for request %s: %s", remote_request_id, transfer_error)')
code, text, out = run(renamed, name="except_renamed")
expect(code == 0 and out is not None and "type(transfer_error).__name__" in out,
       "except variable renamed: the inserted line uses the new name", text)
extra_arg = edit(original, "ret = self.engine.batch_transfer_sync_read(session_id, src_list, dst_list, length_list)",
                 "ret = self.engine.batch_transfer_sync_read(session_id, src_list, dst_list, list(length_list))")
code, text, out = run(extra_arg, name="read_args")
expect(code == 0 and out is not None and
       "self._transfer_batch(req_meta, session_id, src_list, dst_list, list(length_list), req_start_time)" in out,
       "READ arguments edited: kept inside _transfer_batch", text)
decorated = edit(original, "    def _send_done_signal_to_free_remote_port(",
                 "    @staticmethod_placeholder\n    def _send_done_signal_to_free_remote_port(")
code, text, out = run(decorated, name="decorated")
expect(code == 0 and out is not None and
       "    @staticmethod_placeholder\n    def _send_done_signal_to_free_remote_port(" in out,
       "decorated anchor method: receiver methods inserted above the decorator", text)

# 4
STOPS = [
    ("F01", "second READ in another method",
     ("    def _send_done_recv_signal(", "    def _site_read(self):\n        return self.engine.batch_transfer_sync_read('s', [], [], [])\n\n    def _send_done_recv_signal(")),
    ("F03", "READ inside a loop",
     ("        ret = self.engine.batch_transfer_sync_read(session_id, src_list, dst_list, length_list)\n",
      "        for _attempt in range(1):\n            ret = self.engine.batch_transfer_sync_read(session_id, src_list, dst_list, length_list)\n")),
    ("F05", "extra early return in _transfer_kv_cache_all_groups",
     ("        if not src_list:\n            return\n", "        if not src_list:\n            return\n        if os.environ.get('SITE_SKIP'):\n            return\n")),
    ("F08", "second enqueue path",
     ("    def get_and_clear_finished_requests(self) -> set[str]:\n        \"\"\"\n        Get and clear the requests that have been completed.\n        Returns:\n            A set of request IDs that have been completed.\n        \"\"\"\n        return self.task_tracker.get_and_clear_finished_requests()\n\n    def get_and_clear_invalid",
      "    def get_and_clear_finished_requests(self) -> set[str]:\n        \"\"\"\n        Get and clear the requests that have been completed.\n        Returns:\n            A set of request IDs that have been completed.\n        \"\"\"\n        return self.task_tracker.get_and_clear_finished_requests()\n\n    def requeue(self, item):\n        self.request_queue.put(item)\n\n    def get_and_clear_invalid")),
    ("F12", "add_request called outside the shard / remote_tp_offset loops",
     ("            pd_offset_marker = None\n", None)),
    ("F13", "loop variable meta renamed",
     ("        for req_id, meta in metadata.requests.items():\n            if meta.do_virtual:",
      "        for req_id, req_meta in metadata.requests.items():\n            meta = req_meta\n            if meta.do_virtual:")),
    ("F14", "second add_new_req",
     ("        # Clear the list once workers start the transfers\n", "        meta.add_new_req(request_id='x', local_block_ids=(), num_external_tokens=0, kv_transfer_params={}) if False else None\n        # Clear the list once workers start the transfers\n")),
    ("F18", "_handle_request transfers twice (a retry)",
     ("                    self._transfer_kv_cache_all_groups(req_meta)\n",
      "                    self._transfer_kv_cache_all_groups(req_meta)\n                    if os.environ.get('SITE_RETRY'):\n                        self._transfer_kv_cache_all_groups(req_meta)\n")),
    ("F25", "a bare except in _handle_request",
     ("                except Exception as e:\n                    transfer_failed = True", "                except:  # noqa\n                    e = None\n                    transfer_failed = True")),
    ("F27", "request_task_counts_lock removed",
     ("        self.request_task_counts_lock = threading.Lock()\n", "        self.request_task_counts_lock_site = threading.Lock()\n")),
    ("F29", "a second kv_transfer_params return",
     ("        num_prompt_blocks = math.ceil(len(request.prompt_token_ids) / self.block_size)\n",
      "        num_prompt_blocks = math.ceil(len(request.prompt_token_ids) / self.block_size)\n        if os.environ.get('SITE_EARLY'):\n            return delay_free_blocks, None\n")),
]
for fact, label, (old, new) in STOPS:
    if new is None:
        continue
    code, text, out = run(edit(original, old, new), name=f"stop_{fact}")
    expect(code == 3 and out is None and re.search(rf"STOP {fact}\b", text) is not None,
           f"stop {fact}: {label} -> exit 3, nothing written", text)
# F12: add_request call moved out of the inner loop
moved = re.sub(r"(\n {20}self\.kv_recv_thread\.add_request\()", r"\n                    pass\n        if False:\n            self.kv_recv_thread.add_request(", original, count=1)
code, text, out = run(moved, name="stop_F12")
expect(code in (1, 3) and out is None, "stop F12/F10: add_request call moved out of the loops -> nothing written", text)

# 5
no_anchor = edit(original, "        # Clear the list once workers start the transfers\n        self._reqs_need_recv.clear()\n",
                 "        self._reqs_need_recv = {}\n")
code, text, out = run(no_anchor, name="anchor_C24")
expect(code == 2 and out is None and "C24" in text, "anchor C24 gone -> exit 2 naming C24, nothing written", text)
code, text, out = run(replacement, name="already_patched")
expect(code == 6 and out is None and "(T58)" in text, "already patched input -> exit 6, names the T58 version", text)

# 6
NEXT = "                with self.proc_not_transfer_request_lock:\n"  # makes the receiver's call unique in the file
UPDATE = "                self.task_tracker.update_done_task_count(request_id)\n" + NEXT
code, text, out = run(edit(original, UPDATE, "                self.task_tracker.update_done_task_count(request_id=request_id)\n" + NEXT), name="c29_keyword")
expect(code == 0 and out is not None and "C29 alternative_2" in text
       and "update_done_task_count(request_id=request_id)\n                _pd_mark_request_done(req_meta)\n" in out,
       "C29: keyword argument in the call -> prefix alternative, mark right after it", text)
for label, new in (
    ("moved out of the if on the done result",
     "                pass\n            if all_tasks_done or os.environ.get('SITE_DONE'):\n                self.task_tracker.update_done_task_count(request_id)\n"
     "            if all_tasks_done:\n" + NEXT),
    ("split over two lines", "                self.task_tracker.update_done_task_count(\n                    request_id)\n" + NEXT),
):
    code, text, out = run(edit(original, UPDATE, new), name="stop_F32_" + label.split()[0])
    expect(code == 3 and out is None and re.search(r"STOP F32\b", text) is not None,
           f"stop F32: update_done_task_count {label} -> exit 3, nothing written", text)

# 7
if t51_replay and t51_replacement_path:
    t51_replacement = open(t51_replacement_path).read()
    code, text, out = run(original, name="t51_proof", script=t51_replay)
    expect(code == 0 and out == t51_replacement, "upgrade proof: the T51 script replays the original into the T51 replacement", text)
    code, text, out = run(t51_replacement, name="t51_file")
    expect(code == 6 and out is None and "T51 p2d diagnostics" in text and "restore the original" in text,
           "a T51-patched file -> exit 6, says T51 and to restore the original first", text)
else:
    print("skip 7: no T51 script / replacement given")

# 8
if t56_replay and t56_replacement_path:
    t56_replacement = open(t56_replacement_path).read()
    code, text, out = run(original, name="t56_proof", script=t56_replay)
    expect(code == 0 and out == t56_replacement, "upgrade proof: the T56 script replays the original into the T56 replacement", text)
    code, text, out = run(t56_replacement, name="t56_file")
    expect(code == 6 and out is None and "T56 p2d diagnostics" in text and "t56/replay_p2d.py 9b12baa6" in text
           and "restore the original" in text,
           "a T56-patched file -> exit 6, says T56, names the T56 script and to restore the original first", text)
    code, text, out = run(t56_replacement, "--check", name="t56_file_check")
    expect(code == 6 and out is None, "a T56-patched file with --check -> exit 6 too", text)
else:
    print("skip 8: no T56 script / replacement given")

print(f"{len(failures)} failed")
sys.exit(1 if failures else 0)
