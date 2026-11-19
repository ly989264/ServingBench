"""Simulate glm5.3's two site-local vLLM patches, as wrappers applied when the modules are imported.

Active only when P2D_SITE_PATCHES=1 (put this directory first on PYTHONPATH). Like the site's
patch_prefill_length_validation.py and patch_kv_load_failure_reason.py, both wrap and then call the original:
  - AsyncLLM._add_request, with the site's signature (self, request, *args, **kwargs): check the prompt length,
    then call the original with the same arguments (keyword arguments the patched vLLM adds, such as owner=,
    pass through). With n > 1 vLLM v0.30.0 calls _add_request once per child, so every child is checked; each
    call is logged as "p2d_site_patch add_request".
  - OpenAIServingCompletion.completion_stream_generator, functools.wraps(original) and **kwargs as on site: wrap
    the result stream, then call the original; the number of chunks is logged as "p2d_site_patch stream" when the
    stream ends.
A prompt longer than P2D_SITE_MAX_PROMPT tokens (default 100000) is rejected, as a length check would, with ValueError,
or with vllm.exceptions.VLLMValidationError when P2D_SITE_REJECT=validation (which exception the site raises is not
known).

Separately, when P2D_RUNNER_POINTS=1, the measurement points that the vllm-ascend port puts on its V1 NPU runner
(model_runner_v1.py: @torch.inference_mode() over @pd_execution on execute_model, the same over @pd_sample on
sample_tokens, diag.span("runner.forward", completion="host_return") around the model call and
diag.span("runner.sampling", algorithm=..., completion="host_return") around the sampler) are put on vLLM's CPU
runner (CPUModelRunner, a V1 GPUModelRunner). The vLLM patch itself decorates only Model Runner V2, which needs
Triton and is not used on CPU. The decorators and spans are the patched vLLM's own (vllm.v1.worker.worker_base,
vllm.v1.metrics.pd_diagnostics); on an unpatched tree they do not exist and nothing is changed. The NPU device
timing (pd_device_timing.measure) has no CPU counterpart and is not simulated.
"""

import functools
import importlib.abc
import logging
import os
import sys

_LOG = logging.getLogger("vllm.p2d_site_patch")


def _patch_async_llm(module):
    cls = module.AsyncLLM
    original = cls._add_request
    limit = int(os.environ.get("P2D_SITE_MAX_PROMPT", "100000"))

    @functools.wraps(original)
    async def _add_request(self, request, *args, **kwargs):
        # positional order of v0.30.0: prompt, parent_req, index, queue
        parent_req = args[1] if len(args) > 1 else kwargs.get("parent_req")
        index = args[2] if len(args) > 2 else kwargs.get("index")
        n = len(request.prompt_token_ids or [])
        _LOG.warning("p2d_site_patch add_request request_id=%s parent=%s index=%s prompt_tokens=%d kwargs=%s",
                     request.request_id, getattr(parent_req, "request_id", None), index, n, ",".join(sorted(kwargs)) or "-")
        if n > limit:
            _LOG.warning("p2d_site_patch rejected request_id=%s prompt_tokens=%d limit=%d", request.request_id, n, limit)
            message = f"p2d_site_patch: prompt has {n} tokens, more than {limit}"
            if os.environ.get("P2D_SITE_REJECT") == "validation":
                from vllm.exceptions import VLLMValidationError

                raise VLLMValidationError(message)
            raise ValueError(message)
        return await original(self, request, *args, **kwargs)

    cls._add_request = _add_request


def _patch_serving(module):
    cls = module.OpenAIServingCompletion
    original = cls.completion_stream_generator

    @functools.wraps(original)
    async def completion_stream_generator(self, request, engine_inputs, result_generator, *args, **kwargs):
        async def wrapped():
            async for item in result_generator:
                yield item

        chunks = 0
        try:
            async for chunk in original(self, request, engine_inputs, wrapped(), *args, **kwargs):
                chunks += 1
                yield chunk
        finally:
            _LOG.warning("p2d_site_patch stream chunks=%d", chunks)

    cls.completion_stream_generator = completion_stream_generator


def _patch_cpu_runner(module):
    import torch

    try:
        from vllm.v1.metrics import pd_diagnostics as diag
        from vllm.v1.worker.worker_base import pd_execution, pd_sample
    except ImportError:
        _LOG.warning("p2d_site_patch runner_points not applied: this vLLM tree has no pd_execution")
        return
    cls = module.CPUModelRunner
    raw_execute = getattr(cls.execute_model, "__wrapped__", cls.execute_model)
    raw_sample_tokens = getattr(cls.sample_tokens, "__wrapped__", cls.sample_tokens)
    cls.execute_model = torch.inference_mode()(pd_execution(raw_execute))
    cls.sample_tokens = torch.inference_mode()(pd_sample(raw_sample_tokens))
    model_forward = cls._model_forward
    sample = cls._sample

    @functools.wraps(model_forward)
    def _model_forward(self, *args, **kwargs):
        with diag.span("runner.forward", completion="host_return"):
            return model_forward(self, *args, **kwargs)

    @functools.wraps(sample)
    def _sample(self, logits, spec_decode_metadata, *args, **kwargs):
        algorithm = "sampler" if spec_decode_metadata is None else "rejection"
        with diag.span("runner.sampling", algorithm=algorithm, completion="host_return"):
            return sample(self, logits, spec_decode_metadata, *args, **kwargs)

    cls._model_forward = _model_forward
    cls._sample = _sample
    _LOG.warning("p2d_site_patch runner_points applied to %s.%s", cls.__module__, cls.__qualname__)


_TARGETS = {}
if os.environ.get("P2D_SITE_PATCHES") == "1":
    _TARGETS["vllm.v1.engine.async_llm"] = _patch_async_llm
    _TARGETS["vllm.entrypoints.openai.completion.serving"] = _patch_serving
if os.environ.get("P2D_RUNNER_POINTS") == "1":
    _TARGETS["vllm.v1.worker.cpu_model_runner"] = _patch_cpu_runner


class _Loader(importlib.abc.Loader):
    def __init__(self, inner, name):
        self._inner, self._name = inner, name

    def create_module(self, spec):
        return self._inner.create_module(spec)

    def exec_module(self, module):
        self._inner.exec_module(module)
        _TARGETS[self._name](module)

    def __getattr__(self, attr):
        return getattr(self._inner, attr)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name not in _TARGETS:
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(name, path, target)
            if spec is not None and spec.loader is not None:
                spec.loader = _Loader(spec.loader, name)
                return spec
        return None


if _TARGETS:
    sys.meta_path.insert(0, _Finder())
