#!/usr/bin/env python3
"""Replay the p2d diagnostics changes onto a site copy of vllm-ascend's kv_p2p/mooncake_connector.py.

    python3 -I replay_p2d.py <site mooncake_connector.py> [--out /tmp/mooncake_connector.p2d.py] [--check]
    python3 -I replay_p2d.py --export-anchors anchors.json
    python3 -I replay_p2d.py --export-criteria criteria.json

Standard library only (Python 3.9+). The input file is never modified. Steps, in order:

1. Facts: structural facts of the input that the inserted code relies on (calls, loops, returns, names) are
   compared with the vllm-ascend d4ad49796 original. A difference in a "stop" fact means the replayed
   diagnostics could record wrong values, so nothing is written (exit 3). "note" facts are only printed.
2. Changes C01..C29 are located by anchor lines, never by line numbers. They are applied in file order;
   C29 (the moment the request is marked done, added for T56) sits between C14 and C15, so the numbers of
   C15..C28 are those of the T51 version. Each change has a primary anchor and possibly alternatives, tried
   in order; an alternative must match exactly one line inside its function. Any change none of whose
   anchors matches exactly once stops the run (exit 2), naming the change.
3. Checks on the result: it parses; the READ goes through _transfer_batch; the inserted records sit in the
   intended loops and branches; no function uses a name that it did not use before and that is not defined
   (exit 4).
4. The result is written (default /tmp/mooncake_connector.p2d.py), compiled with py_compile (exit 5 on
   failure) and its sha256 printed.

Exit codes: 0 written or check passed, 1 usage or I/O, 2 anchor, 3 stop fact, 4 result check, 5 py_compile,
6 input already patched.

T58 adds the diagnostics level (pd_diagnostics.diag_level(), read once per receiving thread; level 1 leaves out
four request-level fields) inside code the patch already inserts: the anchors, facts and change numbers are
those of T56.

Upgrading a file that already carries the T51 or T56 p2d diagnostics (replay_p2d.py 02304c8f, 9b12baa6): exit 6
says which. Restore the original kept when T51 was applied, prove it with the script of the version on site
(t51/ or t56/: its replay of the backup must give the current file byte for byte), then replay this script on
the backup (field_check/操作说明A_T58).
"""

import argparse
import ast
import builtins
import hashlib
import json
import os
import py_compile
import re
import sys
import tempfile

BASE = "vllm-ascend d4ad49796, vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py"
BASE_SHA256 = "4e476a50afb5a7f13c274c071efc909ee823577383843b04e952bbdad21d1453"
PATCHED_SHA256 = "01ce3b2a7dd5cdb10b630daa43c805d72f7d518407494d5199559ee78072c1de"
MARKER = "from vllm.v1.metrics import pd_diagnostics as diag"
T56_MARKER = "kv_load_envelope_ns"  # written by T56 and later; its absence after MARKER means T51
T58_MARKER = "_pd_diag_level"  # written only by this version (T58)

# --- BEGIN GENERATED TEXTS (tools/regen_replay_texts.py, from the replacement file) ---
TEXT_HELPERS = '''MAX_TRANSFER_NATIVE_CALLS = 64


def _pd_task(req_meta: dict[str, Any]) -> dict[str, Any] | None:
    """Diagnostics of a receive task; None when it was queued without them."""
    return req_meta.get("_pd_diag") or None


def _pd_record_stage(task: dict[str, Any], name: str, started: int, ended: int, failed: bool) -> None:
    metric = task["associated_stages"].setdefault(
        name, {"calls": 0, "duration_ns": 0, "exceptions": 0, "start_ns": started, "end_ns": ended, "status": "ok"}
    )
    metric["calls"] += 1
    metric["duration_ns"] += ended - started
    metric["end_ns"] = ended
    metric["exceptions"] += int(failed)
    if failed:
        metric["status"] = "exception"


@contextlib.contextmanager
def _pd_transfer_stage(req_meta: dict[str, Any], name: str):
    """Keep ancillary host timings within the owning receive task."""
    task = _pd_task(req_meta)
    if task is None:
        yield
        return
    started = time.monotonic_ns()
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        _pd_record_stage(task, name, started, time.monotonic_ns(), failed)


def _pd_stage_begin(req_meta: dict[str, Any], name: str) -> None:
    """Open an ancillary stage over a code block without re-indenting it."""
    task = _pd_task(req_meta)
    if task is not None:
        task.setdefault("open_stages", {})[name] = time.monotonic_ns()


def _pd_stage_end(req_meta: dict[str, Any], name: str | None = None, *, failed: bool) -> None:
    """Close one open stage, or with name=None every stage still open; the caller says whether it failed."""
    task = _pd_task(req_meta)
    if task is None:
        return
    open_stages = task.get("open_stages", {})
    ended = time.monotonic_ns()
    for key in [name] if name is not None else list(open_stages):
        started = open_stages.pop(key, None)
        if started is not None:
            _pd_record_stage(task, key, started, ended, failed)


def _pd_mark_request_done(req_meta: dict[str, Any]) -> None:
    """Note when the task tracker marked the request done: the end of the KV-load envelope. Never raises."""
    try:
        task = _pd_task(req_meta)
        if task is not None:
            task["request_done_ns"] = time.monotonic_ns()
    except Exception:
        pass


# Request-level fields left out at diagnostics level 1; the per-task records keep their own timings.
_PD_LEVEL2_REQUEST_FIELDS = (
    "task_max_queue_wait_ns",
    "task_max_meta_fetch_ns",
    "task_max_prepare_ns",
    "transfer_envelope_ns",
)


def _pd_diag_level() -> int:
    """This process's diagnostics level: pd_diagnostics.diag_level() when the vLLM patch has it, else 3.

    Only 1, 2 and 3 are levels; any other value, or any error, is 3. Call once at initialization and keep it.
    """
    try:
        level_of = getattr(diag, "diag_level", None)
        level = level_of() if callable(level_of) else 3
        return level if type(level) is int and level in (1, 2, 3) else 3
    except Exception:
        return 3'''

TEXT_RECEIVER_STATE = '''self.pd_connector_id = diag.new_id("pd_connector")
self.pd_diag_level = _pd_diag_level()
self.pd_request_tasks: dict[str, dict[str, Any]] = {}'''

TEXT_ADD_REQUEST_PARAMS = '''pd_execution_id: str | None = None,
pd_offset: int | None = None,'''

TEXT_ADD_REQUEST_DIAG = '''context = diag.current()
identity = {
    "request_id": request_id,
    "remote_request_id": remote_request_id,
    "remote_engine_id": remote_engine_id,
    "engine_id": self.local_engine_id,
    "connector_id": self.pd_connector_id,
    "task_id": diag.new_id("pd_transfer"),
    "execution_id": pd_execution_id if pd_execution_id is not None else context.get("execution_id"),
    "tp_rank": self.tp_rank,
    "dp_rank": self.vllm_config.parallel_config.data_parallel_rank,
    "direction": "read",
    "offset": pd_offset,
}
with self.request_task_counts_lock:
    summary = self.pd_request_tasks.get(request_id)
    if summary is None:
        summary = {
            "request_id": request_id,
            "tp_rank": self.tp_rank,
            "dp_rank": self.vllm_config.parallel_config.data_parallel_rank,
            "request_generation": diag.new_id("pd_request"),
            "pending_tasks": 0,
            "final_task_queued": False,
            "submitted_tasks": 0,
            "succeeded_tasks": 0,
            "failed_tasks": 0,
            "skipped_tasks": 0,
            "submitted_bytes": 0,
            "succeeded_bytes": 0,
            "failed_bytes": 0,
            "transfer_start_ns": None,
            "transfer_end_ns": None,
            "first_queued_ns": None,
            "request_done_ns": None,
            "task_max_queue_wait_ns": None,
            "task_max_meta_fetch_ns": None,
            "task_max_prepare_ns": None,
            "tracker_completed": False,
            "native_calls_dropped": 0,
        }
        self.pd_request_tasks[request_id] = summary
    summary["pending_tasks"] += 1
    summary["submitted_tasks"] += 1
    summary["final_task_queued"] |= all_task_done
    identity["request_generation"] = summary["request_generation"]
trans_info["_pd_diag"] = {
    "identity": identity,
    "queued_ns": time.monotonic_ns(),
    "submitted_bytes": 0,
    "succeeded_bytes": 0,
    "failed_bytes": 0,
    "transfer_status": "not_submitted",
    "batch": diag.Batch("pd_transfer_summary", scope="task", **identity),
    "native_calls": [],
    "native_calls_dropped": 0,
    "associated_stages": {},
    "native_call_count": 0,
    "transfer_start_ns": None,
    "transfer_end_ns": None,
}
trans_info["_pd_diag"]["batch"].add_link("producer", request_id=remote_request_id, engine_id=remote_engine_id)'''

TEXT_SUBMIT = '''if _pd_task(request_data) is not None:
    request_data["_pd_diag"]["submitted_ns"] = time.monotonic_ns()'''

TEXT_HANDLE_WRAPPER = '''    outcome: dict[str, Any] = {"status": "interrupted", "error_type": None, "tracker_completed": False}
    task = _pd_task(req_meta)
    if task is None:
        self._handle_request_body(req_meta, outcome)
        return
    start_ns = time.monotonic_ns()
    with diag.scope(**task["identity"]), diag.collect(task["batch"]):
        task["batch"].update(
            start_ns=start_ns,
            queued_ns=task["queued_ns"],
            submitted_ns=task.get("submitted_ns"),
            queue_wait_ns=start_ns - task["queued_ns"],
            final_task=req_meta["all_task_done"],
        )
        try:
            self._handle_request_body(req_meta, outcome)
        except BaseException as exc:
            outcome["status"] = "cancelled" if type(exc).__name__ == "CancelledError" else "failed"
            outcome["error_type"] = type(exc).__name__
            raise
        finally:
            # A stage still open here was cut short by an exception that escaped the body; count it as
            # failed only when the task did not end as succeeded or skipped.
            self._pd_finish_task(
                req_meta,
                outcome["status"],
                outcome["error_type"],
                start_ns,
                outcome["tracker_completed"],
                open_stage_failed=outcome["status"] not in ("succeeded", "skipped"),
            )

def _handle_request_body(self, req_meta: dict[str, Any], outcome: dict[str, Any]):'''

TEXT_SKIPPED = '''outcome["status"] = "skipped"'''

TEXT_TRANSFER_OUTCOME = '''outcome["status"] = "succeeded"
if (_pd_task(req_meta) or {}).get("transfer_status") in ("skipped", "no_work"):
    outcome["status"] = "skipped"'''

TEXT_TRANSFER_EXCEPTION = '''_pd_stage_end(req_meta, failed=True)  # stages the transfer left open ended in this exception
outcome["status"], outcome["error_type"] = "failed", type({exc}).__name__'''

TEXT_TRACKER = '''outcome["tracker_completed"] = all_tasks_done'''

TEXT_REFORMAT_STAGE = '''with self._pd_reformat_stage(req_meta, {arg}):
    {line}'''

TEXT_REFORMAT_FAILURE = '''outcome["status"], outcome["error_type"] = "failed", type({exc}).__name__'''

TEXT_REQUEST_DONE = '''_pd_mark_request_done(req_meta)'''

TEXT_RECEIVER_METHODS = '''def _pd_reformat_stage(self, req_meta: dict[str, Any], request_id: str):
    """Time the post-pull reformat only when this request stashed reformat work."""
    with self.pending_reformat_lock:
        pending = bool(self.pending_reformat.get(request_id))
    return _pd_transfer_stage(req_meta, "reformat") if pending else contextlib.nullcontext()

def _pd_finish_task(
    self,
    req_meta: dict[str, Any],
    status: str,
    error_type: str | None,
    start_ns: int,
    tracker_completed: bool,
    *,
    open_stage_failed: bool,
) -> None:
    """Write the task record, and the request summary after its last task.

    Runs in the finally block of _handle_request: it must never raise, or it would replace the
    exception already propagating there.
    """
    try:
        _pd_stage_end(req_meta, failed=open_stage_failed)
        task = req_meta["_pd_diag"]
        end_ns = time.monotonic_ns()
        record = task["batch"]
        if not task["native_call_count"]:
            record.add_stage(
                "wait_for_transfer",
                calls=0,
                status=("skipped" if task["transfer_status"] in ("skipped", "no_work") else "unmeasured"),
            )
        record.finish(
            status=status,
            error_type=error_type,
            end_ns=end_ns,
            processing_duration_ns=end_ns - start_ns,
            timing_scope="task_processing_envelope",
            transfer_envelope_ns=(
                task["transfer_end_ns"] - task["transfer_start_ns"]
                if task["transfer_start_ns"] is not None
                else None
            ),
            transfer_timing_scope="native_read_envelope_not_call_sum",
            submitted_bytes=task["submitted_bytes"],
            succeeded_bytes=task["succeeded_bytes"],
            failed_bytes=task["failed_bytes"],
            transfer_status=task["transfer_status"],
            tracker_completed=tracker_completed,
            request_done_ns=task.get("request_done_ns"),
            native_calls=task["native_calls"],
            native_calls_limit=MAX_TRANSFER_NATIVE_CALLS,
            native_call_count=task["native_call_count"],
            native_calls_dropped=task["native_calls_dropped"],
            associated_stages=task["associated_stages"],
            incomplete=bool(task["native_calls_dropped"]),
            complete=not task["native_calls_dropped"],
        )
        completed = None
        with self.request_task_counts_lock:
            summary = self.pd_request_tasks.get(req_meta["request_id"])
            if summary is None:
                return
            summary["tracker_completed"] |= tracker_completed
            summary["pending_tasks"] -= 1
            result = status if status in ("succeeded", "skipped") else "failed"
            summary[f"{result}_tasks"] += 1
            for key in ("submitted_bytes", "succeeded_bytes", "failed_bytes"):
                summary[key] += task[key]
            summary["native_calls_dropped"] += task["native_calls_dropped"]
            # Envelope ends and per-task maxima, merged under the same lock as the counts above.
            if summary["first_queued_ns"] is None or task["queued_ns"] < summary["first_queued_ns"]:
                summary["first_queued_ns"] = task["queued_ns"]
            if task.get("request_done_ns") is not None:
                summary["request_done_ns"] = task["request_done_ns"]
            if self.pd_diag_level >= 2:
                stages = task["associated_stages"]
                for key, value in (
                    ("task_max_queue_wait_ns", start_ns - task["queued_ns"]),
                    ("task_max_meta_fetch_ns", (stages.get("meta_fetch") or {}).get("duration_ns")),
                    ("task_max_prepare_ns", (stages.get("prepare") or {}).get("duration_ns")),
                ):
                    if value is not None and (summary[key] is None or value > summary[key]):
                        summary[key] = value
            if summary["pending_tasks"] == 0 and summary["final_task_queued"]:
                completed = self.pd_request_tasks.pop(req_meta["request_id"])
        if completed is not None:
            request_status = (
                "failed"
                if completed["failed_tasks"]
                else ("skipped" if completed["skipped_tasks"] == completed["submitted_tasks"] else "succeeded")
            )
            # A request can span several executions and remote engines. Task events
            # carry those identities; do not attribute the whole request to the last.
            fields = dict(
                {
                    key: value
                    for key, value in completed.items()
                    if key not in ("transfer_start_ns", "transfer_end_ns", "first_queued_ns", "request_done_ns")
                },
                status=request_status,
                scope="request_generation",
                connector_id=self.pd_connector_id,
                engine_id=self.local_engine_id,
                remote_request_id=None,
                task_id=None,
                execution_id=None,
                observation_id=None,
                remote_engine_id=None,
                offset=None,
                transfer_envelope_ns=(
                    completed["transfer_end_ns"] - completed["transfer_start_ns"]
                    if completed["transfer_start_ns"] is not None
                    else None
                ),
                timing_scope="native_read_envelope_not_task_sum_or_pure_wait",
                kv_load_envelope_ns=(
                    completed["request_done_ns"] - completed["first_queued_ns"]
                    if completed["request_done_ns"] is not None and completed["first_queued_ns"] is not None
                    else None
                ),
                kv_load_timing_scope="first_task_queued_to_request_marked_done",
                complete=not completed["native_calls_dropped"],
                incomplete=bool(completed["native_calls_dropped"]),
            )
            if self.pd_diag_level < 2:
                for key in _PD_LEVEL2_REQUEST_FIELDS:
                    del fields[key]
            diag.emit("pd_transfer_summary", **fields)
    except Exception:
        pass

def _transfer_batch(
    self,
    req_meta: dict[str, Any],
    session_id: str,
    src_list: list[int],
    dst_list: list[int],
    length_list: list[int],
    req_start_time: float,
) -> int:
    """Observe one native batch without changing its return/exception behavior."""
    task = _pd_task(req_meta)
    if task is None:
        return self.engine.batch_transfer_sync_read(session_id, src_list, dst_list, length_list)
    submitted_bytes = sum(length_list)
    segments = len(length_list)
    task["submitted_bytes"] += submitted_bytes
    ret = None
    status = "failed"
    error_type = None
    native_tid = threading.get_native_id()
    start_wall_ns = time.time_ns()
    start_ns = time.monotonic_ns()
    try:
        ret = self.engine.batch_transfer_sync_read(session_id, src_list, dst_list, length_list)
        status = "failed" if ret < 0 else ("succeeded" if segments else "no_work")
        return ret
    except BaseException as exc:
        status = "cancelled" if type(exc).__name__ == "CancelledError" else "failed"
        error_type = type(exc).__name__
        raise
    finally:
        end_time = time.perf_counter()
        end_ns = time.monotonic_ns()
        end_wall_ns = time.time_ns()
        succeeded_bytes = submitted_bytes if status == "succeeded" else 0
        failed_bytes = submitted_bytes if status in ("failed", "cancelled") else 0
        task["succeeded_bytes"] += succeeded_bytes
        task["failed_bytes"] += failed_bytes
        task["transfer_status"] = status
        if task["transfer_start_ns"] is None:
            task["transfer_start_ns"] = start_ns
        task["transfer_end_ns"] = end_ns
        with self.request_task_counts_lock:
            summary = self.pd_request_tasks.get(req_meta["request_id"])
            if summary is not None and self.pd_diag_level >= 2:
                if summary["transfer_start_ns"] is None:
                    summary["transfer_start_ns"] = start_ns
                else:
                    summary["transfer_start_ns"] = min(summary["transfer_start_ns"], start_ns)
                summary["transfer_end_ns"] = max(summary["transfer_end_ns"] or end_ns, end_ns)
        task["native_call_count"] += 1
        task["batch"].add_stage(
            "wait_for_transfer",
            start_ns=start_ns,
            end_ns=end_ns,
            status=status,
            exceptions=int(error_type is not None),
            completion="native_read_return",
        )
        if len(task["native_calls"]) < MAX_TRANSFER_NATIVE_CALLS:
            local = time.localtime(start_wall_ns // 1_000_000_000)
            offset = getattr(local, "tm_gmtoff", -(time.altzone if local.tm_isdst > 0 else time.timezone))
            task["native_calls"].append(
                {
                    "call_id": diag.new_id("pd_read"),
                    "operation": "batch_transfer_sync_read",
                    "native_tid": native_tid,
                    "session_id": session_id,
                    "status": status,
                    "error_type": error_type,
                    "native_return": ret,
                    "start_ns": start_ns,
                    "end_ns": end_ns,
                    "duration_ns": end_ns - start_ns,
                    "start_wall_ns": start_wall_ns,
                    "end_wall_ns": end_wall_ns,
                    "wall_utc_offset_seconds": offset,
                    "transfer_scope_duration_ns": int((end_time - req_start_time) * 1_000_000_000),
                    "transfer_scope": "original_prepare_and_native_read",
                    "submitted_bytes": submitted_bytes,
                    "succeeded_bytes": succeeded_bytes,
                    "failed_bytes": failed_bytes,
                    "submitted_segments": segments,
                    "result_scope": "native_batch",
                    "per_segment_status": "unavailable",
                    "byte_semantics": "submitted_lengths_by_batch_status_not_wire_bytes",
                }
            )
        else:
            task["native_calls_dropped"] += 1
'''

TEXT_PREFIX_HIT = '''if _pd_task(req_meta) is not None:
    req_meta["_pd_diag"]["transfer_status"] = "skipped"'''

TEXT_META_BEGIN = '''_pd_stage_begin(req_meta, "meta_fetch")'''

TEXT_META_REQUEST = '''with _pd_transfer_stage(req_meta, "metadata_request"):
    {line}'''

TEXT_META_END = '''_pd_stage_end(req_meta, "meta_fetch", failed=False)'''

TEXT_PREPARE_BEGIN = '''_pd_stage_begin(req_meta, "prepare")'''

TEXT_PREPARE_END = '''_pd_stage_end(req_meta, "prepare", failed=False)'''

TEXT_NO_WORK = '''if _pd_task(req_meta) is not None:
    req_meta["_pd_diag"]["transfer_status"] = "no_work"'''

TEXT_CONSUMER_LINK = '''try:  # diagnostics must never break scheduling
    req_meta = meta.requests[req_id]
    req_meta.pd_execution_id = getattr(scheduler_output, "pd_execution_id", None)
    link = diag.Batch(
        "pd_transfer_summary",
        scope="link",
        request_id=req_id,
        execution_id=req_meta.pd_execution_id,
        engine_id=self.engine_id,
        dp_rank=self.vllm_config.parallel_config.data_parallel_rank,
        transfer_role="consumer",
    )
    link.add_link("producer", request_id=req_meta.remote_request_id, engine_id=req_meta.remote_engine_id)
    link.finish(status="linked", complete=True, incomplete=False)
except Exception:
    pass'''

TEXT_PRODUCER_OFFER = '''try:  # diagnostics must never break scheduling
    offer = diag.Batch(
        "pd_transfer_summary",
        scope="link",
        request_id=request.request_id,
        engine_id=self.engine_id,
        transfer_role="producer",
        dp_rank=self.vllm_config.parallel_config.data_parallel_rank,
        delayed_free=delay_free_blocks,
    )
    offer.add_link("producer", request_id=request.request_id, engine_id=self.engine_id)
    offer.finish(status="offered", complete=True, incomplete=False)
except Exception:
    pass'''

TEXT_KWARGS = '''pd_execution_id=getattr(meta, "pd_execution_id", None),
pd_offset=pd_offset,'''
# --- END GENERATED TEXTS ---


def A(match, anchor=None, op="after", **options):
    """One way to locate a change. match: equal / prefix / regex / scope_end (see MATCH_MODES)."""
    return dict(match=match, anchor=anchor, op=op, **options)


MATCH_MODES = {
    "equal": "the line with leading and trailing white space removed equals the anchor",
    "prefix": "the line with leading and trailing white space removed starts with the anchor",
    "regex": "Python re.search of the anchor on the raw line (indentation included)",
    "scope_end": "no anchor: after the last non-blank line of the function or class",
}

T = "KVCacheRecvingThread"
CHANGES = [
    dict(id="C01", name="import pd_diagnostics", function="<module>", text=MARKER, module_level=True, alts=[
        A("equal", "from vllm.v1.request import RequestStatus", "before"),
        A("prefix", "from vllm.v1.kv_cache_interface import (", "before")]),
    dict(id="C02", name="module helpers", function="<module>", text=TEXT_HELPERS, module_level=True, alts=[
        A("equal", "KV_CACHE_BUFFER_ALIGNMENT = 2 * 1024 * 1024", "after"),
        A("prefix", "MAX_REQUESTS_PER_PEER_HANDLER =", "after"),
        A("prefix", "class RemotePortInfo(", "before", before_blank=True, before_decorators=True)]),
    dict(id="C03", name="ReqMeta.pd_execution_id", function="ReqMeta", text="pd_execution_id: str | None = None", alts=[
        A("scope_end")]),
    dict(id="C04", name="receiver state", function=f"{T}.__init__", text=TEXT_RECEIVER_STATE, alts=[
        A("equal", "self.request_task_counts_lock = threading.Lock()", "after"),
        A("prefix", "super().__init__(", "after")]),
    dict(id="C05", name="add_request parameters", function=f"{T}.add_request", text=TEXT_ADD_REQUEST_PARAMS, alts=[
        A("equal", "remote_block_ids_replicate_k: BlockIds | None = None,", "after"),
        A("prefix", "def add_request(", "before", indent=4, walk=("fwd", "prefix", ")"))]),
    dict(id="C06", name="add_request task identity", function=f"{T}.add_request", text=TEXT_ADD_REQUEST_DIAG, alts=[
        A("equal", 'logger.debug("Adding request %s to the queue.Trans info:%s", request_id, trans_info)', "after"),
        A("equal", "self.request_queue.put(trans_info)", "before")]),
    dict(id="C07", name="_submit_request submitted_ns", function=f"{T}._submit_request", text=TEXT_SUBMIT, alts=[
        A("equal", 'peer_key = (request_data["remote_host"], request_data["remote_handshake_port"])', "after"),
        A("prefix", "def _submit_request(", "before", walk=("fwd", "statement", None))]),
    dict(id="C08", name="_handle_request wrapper", function=T, text=TEXT_HANDLE_WRAPPER, alts=[
        A("equal", "def _handle_request(self, req_meta: dict[str, Any]):", "after"),
        A("prefix", "def _handle_request(self", "after", walk=("fwd", "regex", r":\s*(#.*)?$", "inclusive"))]),
    dict(id="C09", site_function=f"{T}._handle_request", name="skip after failure", function=f"{T}._handle_request_body", text=TEXT_SKIPPED, alts=[
        A("equal", 'logger.warning("Skipping KV cache transfer for request. remote_request_id=%s. ", remote_request_id)',
          "after"),
        A("equal", "if transfer_failed:", "after", indent=4)]),
    dict(id="C10", site_function=f"{T}._handle_request", name="transfer outcome", function=f"{T}._handle_request_body", text=TEXT_TRANSFER_OUTCOME, alts=[
        A("equal", "self._transfer_kv_cache_all_groups(req_meta)", "after")]),
    dict(id="C11", site_function=f"{T}._handle_request", name="transfer exception", function=f"{T}._handle_request_body", text=TEXT_TRANSFER_EXCEPTION,
         alts=[A("equal", "self._transfer_kv_cache_all_groups(req_meta)", "after", indent=4,
                 walk=("fwd", "prefix", "except Exception"), capture=(r"except\s+Exception\s+as\s+(\w+)\s*:", "exc"))]),
    dict(id="C12", site_function=f"{T}._handle_request", name="tracker_completed", function=f"{T}._handle_request_body", text=TEXT_TRACKER, alts=[
        A("equal", "all_tasks_done = self._mark_request_task_done(request_id, all_task_done)", "after"),
        A("prefix", "all_tasks_done = self._mark_request_task_done(", "after")]),
    dict(id="C13", site_function=f"{T}._handle_request", name="reformat stage", function=f"{T}._handle_request_body", text=TEXT_REFORMAT_STAGE, alts=[
        A("prefix", "self._reformat_pending_kv_caches(", "replace", keep_line=True,
          capture=(r"self\._reformat_pending_kv_caches\((\w+)\)", "arg"))]),
    dict(id="C14", site_function=f"{T}._handle_request", anchor_written_by="C13", name="reformat failure", function=f"{T}._handle_request_body", text=TEXT_REFORMAT_FAILURE, alts=[
        A("regex", r"^\s*with self\._pd_reformat_stage\(req_meta, \w+\):\s*$", "after", indent=4,
          walk=("fwd", "prefix", "except Exception"), capture=(r"except\s+Exception\s+as\s+(\w+)\s*:", "exc"))]),
    dict(id="C29", site_function=f"{T}._handle_request", name="request marked done", function=f"{T}._handle_request_body", text=TEXT_REQUEST_DONE, alts=[
        A("equal", "self.task_tracker.update_done_task_count(request_id)", "after"),
        A("prefix", "self.task_tracker.update_done_task_count(", "after")]),
    dict(id="C15", name="receiver methods", function=T, text=TEXT_RECEIVER_METHODS, alts=[
        A("prefix", "def _send_done_signal_to_free_remote_port(", "before", before_decorators=True),
        A("prefix", "def _transfer_kv_cache_all_groups(", "before", before_decorators=True),
        A("prefix", "def _send_done_recv_signal(", "before", before_decorators=True)]),
    dict(id="C16", name="full prefix hit", function=f"{T}._transfer_kv_cache_all_groups", text=TEXT_PREFIX_HIT, alts=[
        A("equal", "if num_local_blocks == 0 and not has_replicate_k_blocks:", "after", indent=4),
        A("prefix", "if num_local_blocks == 0", "after", indent=4)]),
    dict(id="C17", name="meta_fetch begin", function=f"{T}._transfer_kv_cache_all_groups", text=TEXT_META_BEGIN,
         alts=[A("equal", "# Check if we have the remote metadata cached.", "after"),
               A("prefix", "has_remote_metadata = (", "before", walk=("back", "equal", "with self.remote_metadata_lock:"))]),
    dict(id="C18", name="metadata_request", function=f"{T}._transfer_kv_cache_all_groups", text=TEXT_META_REQUEST,
         alts=[A("prefix", "self._get_remote_metadata(", "replace", keep_line=True)]),
    dict(id="C19", name="meta_fetch end", function=f"{T}._transfer_kv_cache_all_groups", text=TEXT_META_END, alts=[
        A("equal", "remote_layer_name_to_idx = build_layer_name_to_metadata_idx(remote_kv_group2layeridx)", "before"),
        A("prefix", "session_id = ", "before"),
        A("prefix", "req_start_time = time.perf_counter()", "before")]),
    dict(id="C20", name="prepare begin", function=f"{T}._transfer_kv_cache_all_groups", text=TEXT_PREPARE_BEGIN, alts=[
        A("prefix", "req_start_time = time.perf_counter()", "after")]),
    dict(id="C21", name="prepare end", function=f"{T}._transfer_kv_cache_all_groups", text=TEXT_PREPARE_END, alts=[
        A("equal", "if not src_list:", "before"),
        A("regex", r"self\.engine\.batch_transfer_sync_read\(", "before")]),
    dict(id="C22", name="no work", function=f"{T}._transfer_kv_cache_all_groups", text=TEXT_NO_WORK, optional=True,
         alts=[A("equal", "if not src_list:", "after", indent=4)]),
    dict(id="C23", name="observed READ", function=f"{T}._transfer_kv_cache_all_groups", text=None, alts=[
        A("regex", r"self\.engine\.batch_transfer_sync_read\(.*\)\s*$", "sub",
          pattern=r"self\.engine\.batch_transfer_sync_read\((.*)\)\s*$",
          repl=r"self._transfer_batch(req_meta, \1, req_start_time)")]),
    dict(id="C24", name="consumer link", function="MooncakeConnectorScheduler.build_connector_meta",
         text=TEXT_CONSUMER_LINK, alts=[
             A("equal", "# Clear the list once workers start the transfers", "before", before_blank=True, indent=4),
             A("equal", "self._reqs_need_recv.clear()", "before", before_blank=True, indent=4)]),
    dict(id="C25", name="producer offer", function="MooncakeConnectorScheduler.request_finished",
         text=TEXT_PRODUCER_OFFER, alts=[
             A("equal", "self._reqs_need_send[request.request_id] = time.time()", "after", indent=-4),
             A("prefix", "return delay_free_blocks, ", "before", before_blank=True)]),
    dict(id="C26", name="pull offset counter", function="MooncakeConnectorWorker.start_load_kv", text="pd_offset = 0",
         alts=[A("prefix", "for shard_idx, remote_ports in enumerate(", "before")]),
    dict(id="C27", name="add_request kwargs", function="MooncakeConnectorWorker.start_load_kv", text=TEXT_KWARGS, alts=[
        A("equal", "remote_block_ids_replicate_k=remote_block_ids_replicate_k_for_port,", "after"),
        A("prefix", "self.kv_recv_thread.add_request(", "before", indent=4, walk=("fwd", "equal", ")"))]),
    dict(id="C28", name="pull offset increment", function="MooncakeConnectorWorker.start_load_kv", text="pd_offset += 1",
         alts=[A("prefix", "self.kv_recv_thread.add_request(", "after", walk=("fwd", "equal", ")"))]),
]


class ReplayError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ----------------------------------------------------------------------------------------------- locating


def indent_of(line):
    return len(line) - len(line.lstrip(" "))


def matches(mode, anchor, line):
    if mode == "statement":  # any non-blank, non-comment line
        return bool(line.strip()) and not line.strip().startswith("#")
    if mode == "prefix":
        return line.strip().startswith(anchor)
    if mode == "regex":
        return re.search(anchor, line) is not None
    return line.strip() == anchor


def block_range(lines, start):
    """The def/class at index start: up to the next statement at the same or a lower indent."""
    base = indent_of(lines[start])
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() and indent_of(line) <= base and not line.lstrip().startswith((")", "]", "}", "#")):
            break
        end += 1
    return start, end


def scope_range(lines, scope):
    if scope == "<module>":
        return 0, len(lines)
    lo, hi = 0, len(lines)
    for part in scope.split("."):
        hits = [
            i for i in range(lo, hi)
            if lines[i].lstrip().startswith((f"class {part}(", f"class {part}:", f"def {part}("))
            and (lo == 0 or indent_of(lines[i]) > indent_of(lines[lo]))
        ]
        if len(hits) != 1:
            raise ReplayError(2, f"function {scope}: {part!r} found {len(hits)} times")
        lo, hi = block_range(lines, hits[0])
    return lo, hi


def locate(lines, lo, hi, alt):
    if alt["match"] == "scope_end":
        index = hi - 1
        while index > lo and not lines[index].strip():
            index -= 1
        return index
    hits = [i for i in range(lo, hi) if matches(alt["match"], alt["anchor"], lines[i])]
    if len(hits) != 1:
        raise ReplayError(2, f"{alt['match']} anchor matched {len(hits)} lines: {alt['anchor']!r}")
    index = hits[0]
    if alt.get("walk"):
        direction, mode, target = alt["walk"][:3]
        step = -1 if direction == "back" else 1
        if not (len(alt["walk"]) > 3 and alt["walk"][3] == "inclusive" and matches(mode, target, lines[index])):
            index += step
        while lo <= index < hi and not matches(mode, target, lines[index]):
            index += step
        if not lo <= index < hi:
            raise ReplayError(2, f"walking {direction} from {alt['anchor']!r}: {target!r} not found")
    return index


def reindent(text, base):
    return [" " * base + line if line.strip() else "" for line in text.split("\n")]


def apply_change(lines, change, force=None):
    lo, hi = scope_range(lines, change["function"])
    alts = list(enumerate(change["alts"], 1))
    if force is not None:
        alts = [alts[force - 1]]
    errors = []
    for rank, alt in alts:
        try:
            index = locate(lines, lo, hi, alt)
            if change.get("module_level") and indent_of(lines[index]):
                raise ReplayError(2, f"anchor is not at module level: {lines[index].strip()!r}")
            fields = {}
            if alt.get("capture"):
                pattern, key = alt["capture"]
                found = re.search(pattern, lines[index])
                if not found:
                    raise ReplayError(2, f"cannot read {key!r} from {lines[index].strip()!r}")
                fields[key] = found.group(1)
        except ReplayError as exc:
            errors.append(f"alternative {rank}: {exc}")
            continue
        if alt["op"] == "sub":
            new_line, count = re.subn(alt["pattern"], alt["repl"], lines[index])
            if count != 1:
                errors.append(f"alternative {rank}: pattern did not match {lines[index].strip()!r}")
                continue
            lines[index] = new_line
            return rank
        if alt.get("keep_line"):
            fields["line"] = lines[index].strip()
        text = change["text"]
        for key, value in fields.items():
            text = text.replace("{" + key + "}", value)
        base = max(0, indent_of(lines[index]) + alt.get("indent", 0))
        new = reindent(text, base)
        if alt["op"] == "after":
            lines[index + 1:index + 1] = new
        elif alt["op"] == "replace":
            lines[index:index + 1] = new
        else:
            if alt.get("before_decorators"):
                while index > lo and lines[index - 1].strip().startswith("@"):
                    index -= 1
            if alt.get("before_blank"):
                while index > lo and not lines[index - 1].strip():
                    index -= 1
            lines[index:index] = new
        return rank
    if change.get("optional"):
        return None
    raise ReplayError(2, f"{change['id']} {change['name']} ({change['function']}): no anchor matched exactly once; "
                         + "; ".join(errors))


# ------------------------------------------------------------------------------------------------- facts


def functions(tree):
    """Module-level functions and classes, and the methods of each class, by qualified name."""
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[node.name] = node
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        out[f"{node.name}.{item.name}"] = item
    return out


def own_nodes(func):
    """Nodes of a function, without the bodies of functions defined inside it."""
    stack = list(func.body)
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(node))


def parents(func):
    out = {}
    for node in ast.walk(func):
        for child in ast.iter_child_nodes(node):
            out[child] = node
    return out


def calls(node, suffix, own=True):
    nodes = own_nodes(node) if own else ast.walk(node)
    return [n for n in nodes if isinstance(n, ast.Call) and ast.unparse(n.func).endswith(suffix)]


def loops_above(node, parent, stop):
    count = 0
    while node is not stop and node in parent:
        node = parent[node]
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While, ast.comprehension)):
            count += 1
    return count


def self_assigned(func):
    names = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
                names.add(target.attr)
    return names


def _update_done_after_last_task(func):
    """The update_done_task_count call is a one-line statement in the block of `if <x>:` where <x> is the name bound
    to the result of self._mark_request_task_done(...): it runs once, when the last task of the request is done."""
    parent = parents(func)
    done_names = {ast.unparse(n.targets[0]) for n in own_nodes(func) if isinstance(n, ast.Assign) and len(n.targets) == 1
                  and isinstance(n.value, ast.Call) and ast.unparse(n.value.func) == "self._mark_request_task_done"}
    found = calls(func, "self.task_tracker.update_done_task_count")
    if len(found) != 1:
        return False
    call = found[0]
    stmt = parent.get(call)
    block = parent.get(stmt)
    return (isinstance(stmt, ast.Expr) and call.lineno == call.end_lineno and isinstance(block, ast.If)
            and stmt in block.body and ast.unparse(block.test) in done_names)


def compute_facts(source):
    tree = ast.parse(source)
    funcs = functions(tree)
    facts = {}

    def put(key, value):
        facts[key] = value

    put("file.read_calls", len(calls(tree, ".batch_transfer_sync_read", own=False)))
    f = funcs.get(f"{T}._transfer_kv_cache_all_groups")
    if f is not None:
        reads = calls(f, "self.engine.batch_transfer_sync_read")
        put("transfer.read_calls", len(reads))
        parent = parents(f)
        put("transfer.read_in_loop", any(loops_above(c, parent, f) for c in reads))
        put("transfer.read_single_line_assign", all(
            c.lineno == c.end_lineno and isinstance(parent.get(c), ast.Assign) for c in reads))
        put("transfer.returns", sum(isinstance(n, ast.Return) for n in own_nodes(f)))
        put("transfer.has_req_start_time", any(
            isinstance(n, ast.Name) and n.id == "req_start_time" and isinstance(n.ctx, ast.Store) for n in own_nodes(f)))
        put("transfer.first_param", f.args.args[1].arg if len(f.args.args) > 1 else None)
    puts = [(name, len(calls(node, "self.request_queue.put"))) for name, node in funcs.items()
            if name.startswith(T + ".")]
    put("receiver.queue_put_calls", sum(n for _, n in puts))
    put("receiver.queue_put_in", sorted(name for name, n in puts if n))
    adds = [(name, c) for name, node in funcs.items() if "." in name for c in calls(node, ".kv_recv_thread.add_request")]
    put("file.add_request_calls", len(adds))
    put("file.add_request_in", sorted({name for name, _ in adds}))
    w = funcs.get("MooncakeConnectorWorker.start_load_kv")
    if w is not None:
        parent = parents(w)
        put("start_load_kv.add_request_loop_depth", sorted({loops_above(c, parent, w) for c in calls(w, ".kv_recv_thread.add_request")}))
        put("start_load_kv.meta_loop", any(
            isinstance(n, ast.For) and ast.unparse(n.target) == "(req_id, meta)" and ast.unparse(n.iter) == "metadata.requests.items()"
            for n in own_nodes(w)))
        put("start_load_kv.do_virtual_check", any(
            isinstance(n, ast.If) and ast.unparse(n.test) == "meta.do_virtual" for n in own_nodes(w)))
    news = [(name, c) for name, node in funcs.items() if "." in name for c in calls(node, ".add_new_req")]
    put("file.add_new_req_calls", len(news))
    put("file.add_new_req_in", sorted({name for name, _ in news}))
    b = funcs.get("MooncakeConnectorScheduler.build_connector_meta")
    if b is not None:
        parent = parents(b)
        loops = []
        for c in calls(b, ".add_new_req"):
            node = c
            while node in parent and not isinstance(node, ast.For):
                node = parent[node]
            loops.append(ast.unparse(node.iter) if isinstance(node, ast.For) else None)
        put("build_connector_meta.add_new_req_loop", loops)
        put("build_connector_meta.param", b.args.args[1].arg if len(b.args.args) > 1 else None)
    h = funcs.get(f"{T}._handle_request")
    if h is not None:
        for name in ("self._transfer_kv_cache_all_groups", "self._mark_request_task_done", "self._reformat_pending_kv_caches",
                     "self._send_done_recv_signal", "self._mark_failed_recv_request", "self.task_tracker.update_done_task_count",
                     "self.request_queue.task_done"):
            put(f"handle_request.calls.{name}", len(calls(h, name)))
        put("handle_request.update_done_after_last_task", _update_done_after_last_task(h))
        handlers = [n for n in own_nodes(h) if isinstance(n, ast.ExceptHandler)]
        put("handle_request.except_handlers", sorted(
            f"{ast.unparse(n.type) if n.type else '<bare>'} as {n.name}" for n in handlers))
        put("handle_request.param", h.args.args[1].arg if len(h.args.args) > 1 else None)
    i = funcs.get(f"{T}.__init__")
    if i is not None:
        needed = ["request_task_counts_lock", "pending_reformat_lock", "pending_reformat", "local_engine_id", "tp_rank",
                  "vllm_config", "engine", "request_queue"]
        put("receiver_init.missing_attrs", sorted(set(needed) - self_assigned(i)))
    s = funcs.get("MooncakeConnectorScheduler.__init__")
    if s is not None:
        put("scheduler_init.missing_attrs", sorted({"engine_id", "vllm_config", "_reqs_need_recv"} - self_assigned(s)))
    r = funcs.get("MooncakeConnectorScheduler.request_finished")
    if r is not None:
        rets = [n for n in own_nodes(r) if isinstance(n, ast.Return) and isinstance(n.value, ast.Tuple)
                and n.value.elts and ast.unparse(n.value.elts[0]) == "delay_free_blocks"]
        put("request_finished.delay_free_returns", len(rets))
        put("request_finished.returns_at_body_level", all(n in r.body for n in rets))
    m = funcs.get("ReqMeta")
    if m is not None:
        put("reqmeta.is_dataclass", any(ast.unparse(d).split("(")[0].endswith("dataclass") for d in m.decorator_list))
    put("functions.lines", {name: node.end_lineno - node.lineno + 1 for name, node in funcs.items()})
    return facts


# Facts of the d4ad49796 original, with what each one protects. "stop": a different value on the site file means
# the replayed diagnostics could record wrong values or fail; nothing is written. "note": printed only.
CRITERIA = [
    ("F01", "file.read_calls", 1, "stop", "整个文件只有一处原生 READ（batch_transfer_sync_read）。多出来的 READ 不经过 _transfer_batch，不进 envelope"),
    ("F02", "transfer.read_calls", 1, "stop", "_transfer_kv_cache_all_groups 里只有一处 READ；C23 只改写这一处"),
    ("F03", "transfer.read_in_loop", False, "stop", "READ 不在循环里。在循环里时一个任务会有多次 READ，约定写的「V1 每个任务至多一次」不成立"),
    ("F04", "transfer.read_single_line_assign", True, "stop", "READ 是一行内的 ret = ... 赋值；C23 用正则改写这一行"),
    ("F05", "transfer.returns", 2, "stop", "_transfer_kv_cache_all_groups 只有两处提前返回（全前缀命中、描述符为空），各有对应的标记。多出的返回路径会把没读的任务记成 succeeded"),
    ("F06", "transfer.has_req_start_time", True, "stop", "READ 之前有 req_start_time；_transfer_batch 用它算 transfer_scope_duration_ns"),
    ("F07", "transfer.first_param", "req_meta", "stop", "插入的代码用参数名 req_meta"),
    ("F08", "receiver.queue_put_calls", 1, "stop", "接收线程只有一处入队，在 add_request 里。别处入队的任务没有 _pd_diag，不写记录，envelope 会漏掉它们"),
    ("F09", "receiver.queue_put_in", [f"{T}.add_request"], "stop", "同上"),
    ("F10", "file.add_request_calls", 1, "stop", "只有一处调用 kv_recv_thread.add_request；别处调用不带 pd_execution_id、pd_offset"),
    ("F11", "file.add_request_in", ["MooncakeConnectorWorker.start_load_kv"], "stop", "同上"),
    ("F12", "start_load_kv.add_request_loop_depth", [3], "stop", "调用在 for req_id, meta → for shard_idx → for remote_tp_offset 三层循环里；offset 按后两层展平"),
    ("F13", "start_load_kv.meta_loop", True, "stop", "外层循环是 for req_id, meta in metadata.requests.items()；插入的代码用 meta"),
    ("F14", "file.add_new_req_calls", 1, "stop", "只有一处 add_new_req；别处加入的请求没有 consumer link（C2）和 pd_execution_id"),
    ("F15", "file.add_new_req_in", ["MooncakeConnectorScheduler.build_connector_meta"], "stop", "同上"),
    ("F16", "build_connector_meta.add_new_req_loop", ["self._reqs_need_recv.items()"], "stop", "add_new_req 在遍历 _reqs_need_recv 的循环里；consumer link 插在同一循环里"),
    ("F17", "build_connector_meta.param", "scheduler_output", "stop", "插入的代码用参数名 scheduler_output"),
    ("F18", "handle_request.calls.self._transfer_kv_cache_all_groups", 1, "stop", "_handle_request 里各调用的次数和原版相同，status 的对应关系才成立"),
    ("F19", "handle_request.calls.self._mark_request_task_done", 1, "stop", "同上；tracker_completed 取它的返回值"),
    ("F20", "handle_request.calls.self._reformat_pending_kv_caches", 1, "stop", "同上；reformat 计时包住这一处"),
    ("F21", "handle_request.calls.self._send_done_recv_signal", 1, "stop", "同上；task 记录在完成信号之后写出"),
    ("F22", "handle_request.calls.self._mark_failed_recv_request", 3, "stop", "同上；失败、失败后跳过两条路径"),
    ("F23", "handle_request.calls.self.task_tracker.update_done_task_count", 1, "stop", "同上"),
    ("F24", "handle_request.calls.self.request_queue.task_done", 1, "stop", "同上"),
    ("F25", "handle_request.except_handlers", ["Exception as e", "Exception as e"], "stop", "两处 except Exception as <名字>：传输失败、reformat 失败；插入的代码从这两行取异常变量名"),
    ("F26", "handle_request.param", "req_meta", "stop", "插入的代码用参数名 req_meta"),
    ("F27", "receiver_init.missing_attrs", [], "stop", "插入的代码用到接收线程的这些属性：request_task_counts_lock、pending_reformat_lock、pending_reformat、local_engine_id、tp_rank、vllm_config、engine、request_queue"),
    ("F28", "scheduler_init.missing_attrs", [], "stop", "插入的代码用到调度端的 engine_id、vllm_config、_reqs_need_recv"),
    ("F29", "request_finished.delay_free_returns", 1, "stop", "request_finished 只有一处返回 kv_transfer_params（return delay_free_blocks, ...），producer offer 插在它之前"),
    ("F30", "request_finished.returns_at_body_level", True, "stop", "这一处返回在函数体最外层，offer 不会落进某个分支"),
    ("F31", "reqmeta.is_dataclass", True, "stop", "ReqMeta 仍是 dataclass；pd_execution_id 作为最后一个带默认值的字段加入"),
    ("F32", "handle_request.update_done_after_last_task", True, "stop", "task_tracker.update_done_task_count 是一行语句，在 if <_mark_request_task_done 的结果>: 的块里，即请求最后一个任务完成时才调用；C29 紧跟其后记下 request_done_ns，kv_load_envelope_ns 的终点就是这一刻"),
]
NOTES = [
    ("N01", "functions.lines", "note", "各函数的行数。现场改过的函数行数不同属于正常；只打印和原版不同的函数，供核对哪 13 个函数改过"),
]
# Expected values of functions.lines on the original are embedded so the note can show differences.
BASE_FUNCTION_LINES = {'RemotePortInfo': 3, 'MooncakeAgentMetadata': 12, 'ReqMeta': 18, 'GroupPull': 6, 'GroupTransferInfo': 4, 'SizedDict': 17, 'SizedDict.__init__': 3, 'SizedDict.__setitem__': 4, 'SizedDict.__getitem__': 7, 'KVCacheTaskTracker': 77, 'KVCacheTaskTracker.__init__': 11, 'KVCacheTaskTracker.add_req_to_process': 2, 'KVCacheTaskTracker.add_not_transfer_request': 4, 'KVCacheTaskTracker.update_done_task_count': 14, 'KVCacheTaskTracker.get_and_clear_finished_requests': 12, 'KVCacheTaskTracker.add_delayed_request': 5, 'KVCacheTaskTracker._retrieve_expired_requests': 22, 'KVCacheSendingThread': 162, 'KVCacheSendingThread.__init__': 30, 'KVCacheSendingThread.get_and_clear_finished_requests': 7, 'KVCacheSendingThread.add_not_transfer_request': 2, 'KVCacheSendingThread.add_delayed_request': 2, 'KVCacheSendingThread.run': 30, 'KVCacheSendingThread.run_busy_loop': 85, 'KVCacheRecvingThread': 1152, 'KVCacheRecvingThread.__init__': 146, 'KVCacheRecvingThread.add_request': 40, 'KVCacheRecvingThread.get_and_clear_finished_requests': 7, 'KVCacheRecvingThread.get_and_clear_invalid_block_ids': 6, 'KVCacheRecvingThread._is_failed_recv_request': 3, 'KVCacheRecvingThread._mark_failed_recv_request': 4, 'KVCacheRecvingThread._clear_failed_recv_request': 3, 'KVCacheRecvingThread.run': 13, 'KVCacheRecvingThread._submit_request': 12, 'KVCacheRecvingThread._handle_peer_requests': 32, 'KVCacheRecvingThread._mark_request_task_submitted': 6, 'KVCacheRecvingThread._mark_request_task_done': 15, 'KVCacheRecvingThread._handle_request': 51, 'KVCacheRecvingThread._send_done_signal_to_free_remote_port': 16, 'KVCacheRecvingThread._transfer_kv_cache_all_groups': 284, 'KVCacheRecvingThread._stash_pending_reformat': 8, 'KVCacheRecvingThread._reformat_pending_kv_caches': 10, 'KVCacheRecvingThread._apply_kv_cache_reformat': 71, 'KVCacheRecvingThread.reformat_kv_cache_hybrid_linear_torch': 27, 'KVCacheRecvingThread._append_mamba_transfer_meta': 93, 'KVCacheRecvingThread._get_group_kv_caches': 16, 'KVCacheRecvingThread._get_kv_cache_dims_from_tensors': 4, 'KVCacheRecvingThread.reformat_kv_cache_with_fused_op': 25, 'KVCacheRecvingThread.reformat_kv_cache': 76, 'KVCacheRecvingThread._cat_kv_cache': 30, 'KVCacheRecvingThread._nz_kv_cache': 19, 'KVCacheRecvingThread._get_remote_metadata': 35, 'KVCacheRecvingThread._send_done_recv_signal': 35, 'KVCacheRecvingThread._get_remote_socket': 23, 'KVCacheRecvingThread._return_remote_socket': 10, 'MooncakeConnectorMetadata': 32, 'MooncakeConnectorMetadata.__init__': 4, 'MooncakeConnectorMetadata.add_new_req': 26, 'MooncakeConnector': 127, 'MooncakeConnector.supports_divergent_local_hybrid_hits': 2, 'MooncakeConnector.__init__': 16, 'MooncakeConnector.on_new_request': 3, 'MooncakeConnector.get_num_new_matched_tokens': 3, 'MooncakeConnector.update_state_after_alloc': 3, 'MooncakeConnector.build_connector_meta': 6, 'MooncakeConnector.request_finished': 7, 'MooncakeConnector.request_finished_all_groups': 7, 'MooncakeConnector.register_kv_caches': 3, 'MooncakeConnector.get_finished': 4, 'MooncakeConnector.get_block_ids_with_load_errors': 4, 'MooncakeConnector.start_load_kv': 4, 'MooncakeConnector.wait_for_layer_load': 3, 'MooncakeConnector.save_kv_layer': 5, 'MooncakeConnector.wait_for_save': 3, 'MooncakeConnector.get_handshake_metadata': 12, 'MooncakeConnector.set_xfer_handshake_metadata': 11, 'MooncakeConnector.set_xfer_handshake_metadata_pp_aware': 5, 'MooncakeConnectorScheduler': 378, 'MooncakeConnectorScheduler.__init__': 42, 'MooncakeConnectorScheduler._model_uses_compress': 4, 'MooncakeConnectorScheduler._get_group_transfer_info': 18, 'MooncakeConnectorScheduler._get_group_unique_specs': 10, 'MooncakeConnectorScheduler._get_transfer_block_ids': 39, 'MooncakeConnectorScheduler._get_swa_transfer_block_ids': 15, 'MooncakeConnectorScheduler._state_prefill_token_count': 6, 'MooncakeConnectorScheduler._truncate_request_for_prefill': 25, 'MooncakeConnectorScheduler.on_new_request': 6, 'MooncakeConnectorScheduler.get_num_new_matched_tokens': 34, 'MooncakeConnectorScheduler.update_state_after_alloc': 28, 'MooncakeConnectorScheduler.build_connector_meta': 28, 'MooncakeConnectorScheduler.request_finished': 63, 'MooncakeConnectorScheduler._port_offset_from_handshake_metadata': 12, 'MooncakeConnectorScheduler.set_xfer_handshake_metadata_from_workers': 25, 'MooncakeConnectorScheduler.set_xfer_handshake_metadata': 5, 'MooncakeConnectorWorker': 1990, 'MooncakeConnectorWorker.__init__': 101, 'MooncakeConnectorWorker._get_prefill_decode_size': 21, 'MooncakeConnectorWorker._serialize_kv_group_spec': 53, 'MooncakeConnectorWorker._is_index_cache_layer': 8, 'MooncakeConnectorWorker._build_layer_specs_from_kv_cache_config': 14, 'MooncakeConnectorWorker._get_spec_num_key_value_heads': 6, 'MooncakeConnectorWorker._get_kv_transfer_spec_key': 13, 'MooncakeConnectorWorker._group_skip_kv_reformat': 3, 'MooncakeConnectorWorker._get_spec_total_num_kv_heads': 20, 'MooncakeConnectorWorker._build_kv_group2layeridx': 61, 'MooncakeConnectorWorker._has_mamba_group': 2, 'MooncakeConnectorWorker._requires_group_aware_attention_transfer': 7, 'MooncakeConnectorWorker._as_kv_cache_tuple': 9, 'MooncakeConnectorWorker._get_layer_spec': 5, 'MooncakeConnectorWorker._recover_aligned_kv_tensor_base': 22, 'MooncakeConnectorWorker._merge_overlapping_register_regions': 24, 'MooncakeConnectorWorker._get_registered_kv_tensor_buffers': 69, 'MooncakeConnectorWorker._get_registered_kv_tensor_buffers_hybrid': 21, 'MooncakeConnectorWorker._get_registered_layer_buffers': 10, 'MooncakeConnectorWorker.register_kv_caches': 139, 'MooncakeConnectorWorker.get_finished': 21, 'MooncakeConnectorWorker.get_block_ids_with_load_errors': 4, 'MooncakeConnectorWorker._expand_block_ids': 4, 'MooncakeConnectorWorker._local_kernel_ids_for_shard': 65, 'MooncakeConnectorWorker._group_compress_ratio': 10, 'MooncakeConnectorWorker._get_kv_cache_group_id': 2, 'MooncakeConnectorWorker._is_m3_index_cache_group': 6, 'MooncakeConnectorWorker._get_kernel_block_scale': 5, 'MooncakeConnectorWorker._get_kernel_block_ids': 32, 'MooncakeConnectorWorker._get_group_kernel_params': 16, 'MooncakeConnectorWorker._get_local_remote_cp_params': 27, 'MooncakeConnectorWorker._get_decode_only_dcp_metadata': 60, 'MooncakeConnectorWorker._get_kv_split_metadata': 438, 'MooncakeConnectorWorker._get_dcp_shard_pulls': 48, 'MooncakeConnectorWorker._get_group_pulls_metadata': 88, 'MooncakeConnectorWorker._get_hybrid_remote_rank_group_pulls': 76, 'MooncakeConnectorWorker._get_attention_group_num_need_pulls': 2, 'MooncakeConnectorWorker._get_attention_group_num_need_pulls_for_decode_tp': 10, 'MooncakeConnectorWorker._group_use_mla_rank_routing': 9, 'MooncakeConnectorWorker._get_attention_group_num_key_value_heads': 29, 'MooncakeConnectorWorker._get_attention_group_remote_rank': 15, 'MooncakeConnectorWorker._get_sfa_replicate_k_block_ids': 76, 'MooncakeConnectorWorker.start_load_kv': 116, 'MooncakeConnectorWorker._get_tp_num_need_pulls': 14, 'MooncakeConnectorWorker._get_selected_pcp_rank': 6, 'MooncakeConnectorWorker._get_remote_host_info_by_port': 20, 'MooncakeConnectorWorker._prefill_get_remote_rank': 8, 'MooncakeConnectorWorker._get_prefill_ranks_for_group': 22, 'MooncakeConnectorWorker._get_remote_rank': 2, 'MooncakeConnectorWorker._get_remote_tp_ranks': 26, 'MooncakeConnectorWorker._get_remote_ranks_for_req': 60, 'zmq_ctx': 13, 'group_concurrent_contiguous': 24, 'split_if_not_byte_contiguous': 19, 'string_to_int64_hash': 8, 'ensure_zmq_send': 19, 'ensure_zmq_recv': 17, 'transfer_groups_need_independent_block_ids': 22, 'build_layer_name_to_metadata_idx': 9, 'resolve_remote_layer_idx': 17, 'get_prefill_pp_indices': 17}


def check_facts(facts):
    stops, rows = [], []
    for fid, key, expected, severity, reason in CRITERIA:
        value = facts.get(key, "<missing>")
        ok = value == expected
        if key == "handle_request.except_handlers":  # any name after "as" is accepted
            ok = isinstance(value, list) and len(value) == 2 and all(re.fullmatch(r"Exception as \w+", v) for v in value)
        rows.append((fid, key, expected, value, ok, severity, reason))
        if not ok and severity == "stop":
            stops.append(fid)
    return stops, rows


# ------------------------------------------------------------------------------------------- result checks


def module_names(tree):
    names = set(dir(builtins)) | {"__file__", "__name__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        for target in getattr(node, "targets", []) + ([node.target] if isinstance(node, ast.AnnAssign) else []):
            for n in ast.walk(target):
                if isinstance(n, ast.Name):
                    names.add(n.id)
    return names


def unresolved(tree):
    """Per function: names loaded that are neither bound in it (or in functions inside it) nor module-level."""
    known = module_names(tree)
    out = {}
    for qual, func in functions(tree).items():
        if isinstance(func, ast.ClassDef):
            continue
        bound, used = set(), set()
        for node in ast.walk(func):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                a = node.args
                for arg in a.posonlyargs + a.args + a.kwonlyargs + [x for x in (a.vararg, a.kwarg) if x]:
                    bound.add(arg.arg)
                if not isinstance(node, ast.Lambda):
                    bound.add(node.name)
            elif isinstance(node, ast.Name):
                (bound if isinstance(node.ctx, (ast.Store, ast.Del)) else used).add(node.id)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bound.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    bound.add((alias.asname or alias.name).split(".")[0])
        missing = used - bound - known
        if missing:
            out[qual] = missing
    return out


def check_result(before, after):
    problems = []
    tree = ast.parse(after)
    funcs = functions(tree)
    if after.count(MARKER) != 1:
        problems.append("pd_diagnostics import present %d times" % after.count(MARKER))
    for name in ("_pd_task", "_pd_record_stage", "_pd_transfer_stage", "_pd_stage_begin", "_pd_stage_end",
                 "_pd_mark_request_done", "_pd_diag_level"):
        if name not in funcs:
            problems.append(f"module-level helper {name} missing")
    i = funcs.get(f"{T}.__init__")
    if i is None or "pd_diag_level" not in self_assigned(i):
        problems.append(f"{T}.__init__ does not set self.pd_diag_level")
    f = funcs.get(f"{T}._transfer_kv_cache_all_groups")
    if f is None or calls(f, "self.engine.batch_transfer_sync_read") or len(calls(f, "self._transfer_batch")) != 1:
        problems.append("the READ in _transfer_kv_cache_all_groups does not go through exactly one _transfer_batch")
    for name in ("_handle_request_body", "_pd_reformat_stage", "_pd_finish_task", "_transfer_batch"):
        if f"{T}.{name}" not in funcs:
            problems.append(f"{T}.{name} missing")
    h = funcs.get(f"{T}._handle_request_body")
    if h is not None and len(calls(h, "self._transfer_kv_cache_all_groups")) != 1:
        problems.append("_handle_request_body does not call _transfer_kv_cache_all_groups once")
    if h is not None and not _done_mark_follows_update(h):
        problems.append("_pd_mark_request_done(req_meta) is not the statement right after update_done_task_count")
    b = funcs.get("MooncakeConnectorScheduler.build_connector_meta")
    if b is not None:
        parent = parents(b)
        add_loop = {id(_loop(c, parent)) for c in calls(b, ".add_new_req")}
        link_loop = {id(_loop(c, parent)) for c in calls(b, "diag.Batch")}
        if len(link_loop) != 1 or link_loop != add_loop:
            problems.append("consumer link is not in the loop that calls add_new_req")
    r = funcs.get("MooncakeConnectorScheduler.request_finished")
    if r is not None:
        offers = [n for n in r.body if isinstance(n, ast.Try) and calls(n, "diag.Batch", own=False)]
        rets = [n for n in r.body if isinstance(n, ast.Return) and isinstance(n.value, ast.Tuple)
                and ast.unparse(n.value.elts[0]) == "delay_free_blocks"]
        if len(offers) != 1 or len(rets) != 1 or r.body.index(offers[0]) > r.body.index(rets[0]):
            problems.append("producer offer is not a body-level statement before the delay_free_blocks return")
    w = funcs.get("MooncakeConnectorWorker.start_load_kv")
    if w is not None:
        parent = parents(w)
        call_stmt = [parent[c] for c in calls(w, ".kv_recv_thread.add_request")]
        incs = [n for n in own_nodes(w) if isinstance(n, ast.AugAssign) and ast.unparse(n.target) == "pd_offset"]
        inits = [n for n in own_nodes(w) if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "pd_offset"]
        if len(call_stmt) != 1 or len(incs) != 1 or parent.get(incs[0]) is not parent.get(call_stmt[0]):
            problems.append("pd_offset += 1 is not in the block that calls add_request")
        elif len(inits) != 1 or not _directly_before_loop(inits[0], parent, call_stmt[0]):
            problems.append("pd_offset = 0 is not right before the loops that call add_request")
    allowed = set().union(*unresolved(ast.parse(before)).values())
    new_missing = {q: sorted(v - allowed) for q, v in unresolved(tree).items()}
    new_missing = {q: v for q, v in new_missing.items() if v}
    if new_missing:
        problems.append(f"names used but not defined after the replay: {new_missing}")
    return problems


def _done_mark_follows_update(func):
    parent = parents(func)
    marks = calls(func, "_pd_mark_request_done")
    updates = calls(func, "self.task_tracker.update_done_task_count")
    if len(marks) != 1 or len(updates) != 1:
        return False
    mark_stmt, update_stmt = parent.get(marks[0]), parent.get(updates[0])
    block = parent.get(update_stmt)
    if block is None or parent.get(mark_stmt) is not block or ast.unparse(marks[0]) != "_pd_mark_request_done(req_meta)":
        return False
    for field in ("body", "orelse", "finalbody"):
        body = getattr(block, field, None)
        if isinstance(body, list) and update_stmt in body:
            i = body.index(update_stmt)
            return i + 1 < len(body) and body[i + 1] is mark_stmt
    return False


def _loop(node, parent):
    while node in parent and not isinstance(node, ast.For):
        node = parent[node]
    return node


def _directly_before_loop(init, parent, call_stmt):
    block = parent.get(init)
    if block is None:
        return False
    for field in ("body", "orelse"):
        body = getattr(block, field, [])
        if init in body:
            following = body[body.index(init) + 1] if body.index(init) + 1 < len(body) else None
            node = call_stmt
            while node in parent and node is not following:
                node = parent[node]
            return isinstance(following, ast.For) and node is following
    return False


# ---------------------------------------------------------------------------------------------- replay


def replay(source, force=None, report=None):
    if MARKER in source:
        raise ReplayError(6, _already_patched(source))
    lines = source.split("\n")
    for change in CHANGES:
        rank = apply_change(lines, change, (force or {}).get(change["id"]))
        if report is not None:
            report.append((change, rank))
    result = "\n".join(lines)
    try:
        ast.parse(result)
    except SyntaxError as exc:
        raise ReplayError(4, f"the result does not parse: {exc}")
    problems = check_result(source, result)
    if problems:
        raise ReplayError(4, "; ".join(problems))
    return result


def _already_patched(source):
    if T58_MARKER in source:
        return "the input already has these p2d diagnostics (T58); refusing to apply twice"
    version, script = ("T56", "t56/replay_p2d.py 9b12baa6") if T56_MARKER in source else ("T51", "t51/replay_p2d.py 02304c8f")
    return (f"the input already has the {version} p2d diagnostics; restore the original kept when T51 was applied, "
            f"check it with the {version} script ({script}), then replay this script on it (field_check/操作说明A_T58)")


def sha256(data):
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def export_anchors(path):
    rows = []
    for change in CHANGES:
        alts = []
        for rank, alt in enumerate(change["alts"], 1):
            walk = alt.get("walk")
            alts.append({
                "rank": rank,
                "match": alt["match"],
                "anchor": alt["anchor"],
                "op": alt["op"],
                "indent_relative_to_anchor": alt.get("indent", 0),
                "walk": None if not walk else {"direction": walk[0], "match": walk[1], "anchor": walk[2],
                                               "may_stop_on_anchor_line": len(walk) > 3},
                "before_blank_lines": bool(alt.get("before_blank")),
                "before_decorators": bool(alt.get("before_decorators")),
                "reads_from_line": alt["capture"][1] if alt.get("capture") else None,
                "keeps_line": bool(alt.get("keep_line")),
            })
        rows.append({"id": change["id"], "name": change["name"], "function": change["function"],
                     "function_in_unpatched_file": change.get("site_function", change["function"]),
                     "primary_anchor_written_by": change.get("anchor_written_by"),
                     "required": not change.get("optional"), "module_level": bool(change.get("module_level")),
                     "primary": alts[0], "alternatives": alts[1:]})
    doc = {
        "script": "replay_p2d.py", "script_sha256": _self_sha256(), "base": BASE, "base_sha256": BASE_SHA256,
        "patched_sha256": PATCHED_SHA256,
        "rule": "changes are applied in id order; each uses the first entry (primary, then alternatives in order) "
                "whose anchor matches exactly one line inside its function in the file as changed so far; a walk then "
                "moves from that line to the first line matching the walk anchor; a required change with no such "
                "entry stops the replay. C08 turns the body of _handle_request into _handle_request_body, so C09-C14 "
                "look there (function_in_unpatched_file names where the lines are before the replay); C14's anchor is "
                "the line C13 writes. The authoritative check is `replay_p2d.py --check` on the site file",
        "match_modes": MATCH_MODES, "changes": rows,
    }
    _write_json(path, doc)


def export_criteria(path):
    doc = {
        "script": "replay_p2d.py", "script_sha256": _self_sha256(), "base": BASE, "base_sha256": BASE_SHA256,
        "how": "facts are computed with Python's ast module on the site file before the replay (compute_facts); "
               "every 'stop' fact must equal the value of the d4ad49796 original, or the replay writes nothing",
        "stop": [{"id": fid, "fact": key, "expected": expected, "why": reason}
                 for fid, key, expected, severity, reason in CRITERIA],
        "note": [{"id": nid, "fact": key, "why": reason} for nid, key, severity, reason in NOTES],
        "base_function_lines": BASE_FUNCTION_LINES,
        "after_replay_checks": [
            "the result parses (ast.parse) and compiles (py_compile)",
            "pd_diagnostics is imported once; the seven module-level helpers exist (T56 adds _pd_mark_request_done, "
            "T58 _pd_diag_level)",
            "(T58) KVCacheRecvingThread.__init__ sets self.pd_diag_level",
            "_transfer_kv_cache_all_groups has no direct batch_transfer_sync_read and exactly one _transfer_batch",
            "_handle_request_body, _pd_reformat_stage, _pd_finish_task, _transfer_batch exist in KVCacheRecvingThread",
            "(T56) in _handle_request_body, _pd_mark_request_done(req_meta) is the statement right after "
            "update_done_task_count",
            "the consumer link is created in the same loop as add_new_req",
            "the producer offer is a body-level statement of request_finished before the delay_free_blocks return",
            "pd_offset = 0 is right before the loops that call add_request, and pd_offset += 1 is in the block of that call",
            "no function uses a name that it did not use before and that is not defined",
        ],
    }
    _write_json(path, doc)


def _self_sha256():
    with open(os.path.abspath(__file__), "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _write_json(path, doc):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"replay_p2d: wrote {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("path", nargs="?")
    parser.add_argument("--out", default="/tmp/mooncake_connector.p2d.py")
    parser.add_argument("--check", action="store_true", help="locate and check everything, write nothing")
    parser.add_argument("--export-anchors")
    parser.add_argument("--export-criteria")
    parser.add_argument("--force-alt", action="append", default=[], help="testing only: C05=2 uses alternative 2")
    args = parser.parse_args()
    if args.export_anchors or args.export_criteria:
        if args.export_anchors:
            export_anchors(args.export_anchors)
        if args.export_criteria:
            export_criteria(args.export_criteria)
        return 0
    if not args.path:
        parser.error("the site mooncake_connector.py is required")
    try:
        with open(args.path, encoding="utf-8") as handle:
            source = handle.read()
    except OSError as exc:
        print(f"replay_p2d: cannot read input: {exc}", file=sys.stderr)
        return 1
    print(f"replay_p2d: input={args.path} sha256={sha256(source)} same_as_d4ad49796={sha256(source) == BASE_SHA256}")
    if MARKER in source:
        print(f"replay_p2d: FAILED (exit 6): {_already_patched(source)}; nothing written", file=sys.stderr)
        return 6
    try:
        facts = compute_facts(source)
    except SyntaxError as exc:
        print(f"replay_p2d: input does not parse: {exc}", file=sys.stderr)
        return 1
    stops, rows = check_facts(facts)
    for fid, key, expected, value, ok, severity, reason in rows:
        if not ok:
            print(f"replay_p2d: {'STOP' if severity == 'stop' else 'note'} {fid} {key}: expected {expected!r}, found {value!r} ({reason})")
    base_lines = BASE_FUNCTION_LINES if isinstance(BASE_FUNCTION_LINES, dict) else {}
    changed = sorted(k for k, v in facts.get("functions.lines", {}).items() if base_lines.get(k) != v)
    print(f"replay_p2d: note N01 functions whose line count differs from d4ad49796: {len(changed)}"
          + (": " + ", ".join(f"{k}({base_lines.get(k, '-')}->{facts['functions.lines'][k]})" for k in changed) if changed else ""))
    if stops:
        print(f"replay_p2d: FAILED: stop facts differ: {', '.join(stops)}; nothing written", file=sys.stderr)
        return 3
    print(f"replay_p2d: facts ok ({len(CRITERIA)} stop facts equal the original)")
    force = {}
    for item in args.force_alt:
        cid, rank = item.split("=")
        force[cid] = int(rank)
    report = []
    try:
        result = replay(source, force, report)
    except ReplayError as exc:
        print(f"replay_p2d: FAILED (exit {exc.code}): {exc}; nothing written", file=sys.stderr)
        return exc.code
    for change, rank in report:
        if rank is None:
            print(f"replay_p2d: {change['id']} left_out (optional, its code path is absent): {change['name']}")
        else:
            alt = change["alts"][rank - 1]
            kind = "primary" if rank == 1 else f"alternative_{rank}"
            print(f"replay_p2d: {change['id']} {kind} {alt['match']} {alt['anchor']!r}: {change['name']}")
    used = sum(rank is not None for _, rank in report)
    if args.check:
        compile(result, args.path, "exec")
        print(f"replay_p2d: check ok: {used} of {len(CHANGES)} changes located, result checks passed, nothing written")
        return 0
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(result)
    try:
        with tempfile.TemporaryDirectory(prefix="replay_p2d_") as tmp:
            py_compile.compile(args.out, cfile=os.path.join(tmp, "out.pyc"), doraise=True)
    except py_compile.PyCompileError as exc:
        print(f"replay_p2d: FAILED (exit 5): py_compile: {exc}", file=sys.stderr)
        return 5
    same = sha256(result) == PATCHED_SHA256
    print(f"replay_p2d: written out={args.out} sha256={sha256(result)} changes={used}/{len(CHANGES)} "
          f"py_compile=ok same_as_replacement={same}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
