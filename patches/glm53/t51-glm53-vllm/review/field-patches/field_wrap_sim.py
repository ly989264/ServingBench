"""The two site patches as f-3b describes them, as wrappers, against pd_equiv's HTTP scenarios F1-F8.

usage: python field_wrap_sim.py OUT.jsonl MODE
  baseline            no replacement
  addreq_wrap         AsyncLLM._add_request = (self, request, *args, **kwargs): length check, then the
                      original with *args, **kwargs
  addreq_wrap_nokw    same, but the original is called with *args only (keywords dropped)
  addreq_wrap_reject  addreq_wrap with a limit of 5 prompt tokens (requests over it are rejected)
  stream_wrap         completion_stream_generator = closure over `original`: result_generator wrapped by
                      record_load_failures (records KV-load-failure stop reasons, yields unchanged), then
                      the original's chunks, rewritten where a choice was recorded
  stream_wrap_force   same, but every finished output is recorded, so the rewrite path runs
  p_both              addreq_wrap and stream_wrap_force together
Calls of the wrappers are written as a last record (scenario "field_calls").
"""
import asyncio
import functools
import json
import sys
from typing import Any

OUT, MODE = sys.argv[1], sys.argv[2]
sys.argv = [sys.argv[0], OUT + ".unused"]
sys.path.insert(0, "/src/equiv")
import pd_equiv as eq  # noqa: E402

import vllm.entrypoints.openai.completion.serving as serving_mod  # noqa: E402
import vllm.v1.engine.async_llm as async_llm_mod  # noqa: E402
from vllm.exceptions import VLLMValidationError  # noqa: E402

CALLS = []
AsyncLLM = async_llm_mod.AsyncLLM

if MODE in ("addreq_wrap", "addreq_wrap_nokw", "addreq_wrap_reject", "p_both"):
    LIMIT = 5 if MODE == "addreq_wrap_reject" else 10**9
    _orig_add_request = AsyncLLM._add_request

    def _check_prefill_length(request):
        n = len(getattr(request, "prompt_token_ids", None) or ())
        if n > LIMIT:
            raise VLLMValidationError(f"prompt has {n} tokens, over the prefill limit {LIMIT}")

    if MODE == "addreq_wrap_nokw":
        async def _add_request(self, request, *args, **kwargs):
            CALLS.append((eq.SCENARIO[0], "_add_request", request.request_id, sorted(kwargs)))
            _check_prefill_length(request)
            return await _orig_add_request(self, request, *args)
    else:
        async def _add_request(self, request, *args, **kwargs):
            CALLS.append((eq.SCENARIO[0], "_add_request", request.request_id, sorted(kwargs)))
            _check_prefill_length(request)
            return await _orig_add_request(self, request, *args, **kwargs)

    AsyncLLM._add_request = _add_request

if MODE in ("stream_wrap", "stream_wrap_force", "p_both"):
    FORCE = MODE != "stream_wrap"
    _KV_LOAD_FAILURE_REASON_PREFIXES = ("kv_load_failed",)
    from vllm.entrypoints.openai.completion.protocol import (
        CompletionResponseStreamChoice,
        CompletionStreamResponse,
    )

    def _make_stream_generator(original):
        @functools.wraps(original)
        async def completion_stream_generator(
            self: Any,
            request: Any,
            engine_inputs: Any,
            result_generator: Any,
            request_id: str,
            created_time: int,
            model_name: str,
            num_prompts: int,
            tokenizer: Any,
            request_metadata: Any,
            **kwargs: Any,
        ):
            num_choices = 1 if getattr(request, "n", None) is None else request.n
            # choice index -> marker stop_reason recorded from engine outputs.
            pending: dict[int, str] = {}

            async def record_load_failures(stream: Any):
                async for prompt_idx, res in stream:
                    for output in res.outputs:
                        reason = output.stop_reason
                        if output.finish_reason is not None and (
                            FORCE or (isinstance(reason, str) and reason.startswith(_KV_LOAD_FAILURE_REASON_PREFIXES))
                        ):
                            pending[output.index + prompt_idx * num_choices] = reason or "kv_load_failed:simulated"
                    yield prompt_idx, res

            CALLS.append((eq.SCENARIO[0], "completion_stream_generator", request_id))
            async for chunk in original(
                self, request, engine_inputs, record_load_failures(result_generator), request_id,
                created_time, model_name, num_prompts, tokenizer, request_metadata, **kwargs,
            ):
                if not pending or not chunk.startswith("data: {"):
                    yield chunk
                    continue
                data = json.loads(chunk[len("data: "):])
                choices, changed = [], False
                for c in data.get("choices") or []:
                    if c.get("finish_reason") is not None and c.get("index") in pending:
                        c = dict(c, finish_reason="error", stop_reason=pending.pop(c["index"]))
                        changed = True
                    choices.append(c)
                if not changed:
                    yield chunk
                    continue
                CALLS.append((eq.SCENARIO[0], "rewritten_chunk", request_id))
                out = CompletionStreamResponse(
                    id=request_id,
                    object="text_completion",
                    created=created_time,
                    model=model_name,
                    choices=[CompletionResponseStreamChoice.model_validate(c) for c in choices],
                )
                yield f"data: {out.model_dump_json(exclude_unset=True)}\n\n"

        return completion_stream_generator

    cls = serving_mod.OpenAIServingCompletion
    cls.completion_stream_generator = _make_stream_generator(cls.completion_stream_generator)

try:
    asyncio.run(eq.frontend_scenarios())
except Exception:
    import traceback

    eq.RECORDS.append({"scenario": eq.SCENARIO[0], "driver_error": traceback.format_exc()})
eq.RECORDS.append({"scenario": "field_calls", "field_calls": [list(c) for c in CALLS]})
with open(OUT, "w") as f:
    for r in eq.RECORDS:
        f.write(json.dumps(r, sort_keys=True, default=str) + "\n")
print("records", len(eq.RECORDS), flush=True)
