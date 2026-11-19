# Reference implementation of the A2 interface (T51 C14) on top of patch 01's worker_base.py.
# Default off: records are written exactly as before unless a runner calls pd_claim_finish().
CLAIMS = '''

# ---- T51 C14 (A2): a runner may take over writing this phase's worker_summary ------------------------------------
_PD_CLAIMS_LOCK = threading.Lock()
_PD_CLAIMS: dict[int, "PdFinishClaim"] = {}
_PD_HELD: dict[int, "PdFinishClaim"] = {}


class PdFinishClaim:
    """Holds one worker_summary record open past the wrapped method's return, until release()."""

    def __init__(self, batch: "diag.Batch"):
        self._batch = batch
        self._lock = threading.Lock()
        self._result: tuple[str, str | None] | None = None  # (status, error_type) once the method returned
        self._released = False
        self._written = False

    def set_stage_fields(self, stage: str, **fields: Any) -> bool:
        """Add fields to stages[stage] of the held record; False when the stage is absent or it was written."""
        batch = self._batch
        with batch._lock:
            if batch.closed or stage not in batch.stages:
                return False
            batch.stages[stage].update(fields)
            return True

    def release(self) -> None:
        """Idempotent, any thread. Before the wrapped method returned: written at its return, as without a claim.
        After: written now, with the status the wrapper determined."""
        with self._lock:
            if self._released or self._written:
                return
            self._released = True
            result = self._result
        if result is not None:
            self._write(result)

    def _returned(self, status: str, error_type: str | None) -> None:
        with self._lock:
            if self._written:
                return
            self._result = (status, error_type)
            write_now = self._released
        if write_now:
            self._write((status, error_type))
        else:
            with _PD_CLAIMS_LOCK:
                _PD_HELD[id(self)] = self

    def _write(self, result: tuple[str, str | None]) -> None:
        with self._lock:
            if self._written:
                return
            self._written = True
        with _PD_CLAIMS_LOCK:
            _PD_HELD.pop(id(self), None)
        self._batch.finish(status=result[0], error_type=result[1])


def pd_claim_finish() -> PdFinishClaim | None:
    """Inside a method wrapped by pd_execution or pd_sample (same thread): keep this phase's worker_summary open after
    the method returns, until the returned claim is released. None when no worker_summary record is open here.
    Calling it again for the same record returns the same claim. If the method raises, the record is written at once
    with the error status, as without a claim."""
    batch = diag.current_batch()
    if batch is None or batch.event != "worker_summary":
        return None
    with _PD_CLAIMS_LOCK:
        claim = _PD_CLAIMS.get(id(batch))
        if claim is None:
            claim = _PD_CLAIMS[id(batch)] = PdFinishClaim(batch)
        return claim


def pd_release_claims() -> None:
    """Write every claimed record that is still held (process exit)."""
    with _PD_CLAIMS_LOCK:
        held = list(_PD_HELD.values())
    for claim in held:
        claim.release()


atexit.register(pd_release_claims)


@contextlib.contextmanager
def _pd_worker_batch(phase: str, context: dict[str, Any]) -> Iterator["diag.Batch"]:
    """diag.batch("worker_summary", phase=...) that honours pd_claim_finish(); same record, same statuses."""
    value = diag.Batch("worker_summary", phase=phase, **context)
    status = "ok"
    error_type = None
    try:
        with diag.collect(value):
            yield value
    except BaseException as exc:
        status = (
            "cancelled"
            if isinstance(exc, (KeyboardInterrupt, GeneratorExit))
            or type(exc).__name__ == "CancelledError"
            else "error"
        )
        error_type = type(exc).__name__
        raise
    finally:
        with _PD_CLAIMS_LOCK:
            claim = _PD_CLAIMS.pop(id(value), None)
        if claim is None or status != "ok":
            if claim is not None:
                with claim._lock:
                    claim._written = True
            value.finish(status=status, error_type=error_type)
        else:
            claim._returned(status, error_type)
'''

EDITS = [
 ("from collections.abc import Callable\nfrom functools import wraps\n",
  "import atexit\nimport contextlib\nimport threading\nfrom collections.abc import Callable, Iterator\nfrom functools import wraps\n", 1),
 ("\n\ndef pd_execution_context(owner: Any, scheduler_output: Any) -> dict[str, Any]:",
  CLAIMS + "\n\ndef pd_execution_context(owner: Any, scheduler_output: Any) -> dict[str, Any]:", 1),
 ('                diag.batch("worker_summary", phase="execute", **context),\n',
  '                _pd_worker_batch("execute", context),\n', 1),
 ('                diag.batch("worker_summary", phase="sample", **(context or {})),\n',
  '                _pd_worker_batch("sample", context or {}),\n', 1),
]
