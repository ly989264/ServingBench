"""How often a replacement of AsyncLLM._add_request (written like the site's: (self, request, *args, **kwargs),
then the original) is called for one request with n = 1, 2 and 4. Stubs only, no model; works on clean v0.30.0 and on the port."""
import asyncio
from types import SimpleNamespace

from vllm.sampling_params import SamplingParams
from vllm.v1.engine import EngineCoreRequest
from vllm.v1.engine.async_llm import AsyncLLM

original = AsyncLLM._add_request
seen, sent = [], []


async def _add_request(self, request, *args, **kwargs):
    seen.append(request.request_id)
    return await original(self, request, *args, **kwargs)


AsyncLLM._add_request = _add_request


async def add_request_async(request):
    sent.append(request.request_id)


for n in (1, 2, 4):
    seen.clear()
    sent.clear()
    engine = AsyncLLM.__new__(AsyncLLM)
    engine.engine_core = SimpleNamespace(add_request_async=add_request_async, resources=SimpleNamespace(engine_dead=False))
    engine.output_handler = None
    engine.vllm_config = SimpleNamespace(cache_config=SimpleNamespace(kv_sharing_fast_prefill=False))
    engine.check_admission = lambda *args, **kwargs: None
    engine.input_processor = SimpleNamespace(assign_request_id=lambda r: setattr(r, "external_req_id", r.request_id))
    engine._run_output_handler = lambda: None
    engine.output_processor = SimpleNamespace(has_request=lambda request_id: False, add_request=lambda *args: None)
    engine.log_requests = False
    request = EngineCoreRequest(
        request_id=f"req-n{n}", prompt_token_ids=[1, 2, 3], mm_features=None,
        sampling_params=SamplingParams(n=n, max_tokens=2), pooling_params=None, arrival_time=0,
        lora_request=None, cache_salt=None, data_parallel_rank=None,
    )
    asyncio.run(engine.add_request(f"req-n{n}", request, request.sampling_params))
    print(f"n={n} replacement calls {len(seen)} {seen} sent {len(sent)} {sent}", flush=True)
