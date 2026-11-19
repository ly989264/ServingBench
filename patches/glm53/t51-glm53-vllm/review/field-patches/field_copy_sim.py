"""What happens to the port when a field patch replaces AsyncLLM.add_request or
OpenAIServingCompletion.completion_stream_generator.

usage: python field_copy_sim.py OUT.jsonl MODE CLEAN_V030_ROOT
  baseline         the port as is
  add_copy         AsyncLLM.add_request = a copy of v0.30.0's original add_request
  add_copy_owner   same, and _add_request accepts a missing owner (owner=None)
  add_wrap         AsyncLLM.add_request = a new function that calls the port's add_request
  stream_copy      OpenAIServingCompletion.completion_stream_generator = a copy of v0.30.0's original
Runs pd_equiv's HTTP scenarios F1-F8 only.
"""
import ast
import asyncio
import json
import sys
import textwrap

OUT, MODE, CLEAN = sys.argv[1], sys.argv[2], sys.argv[3]
sys.argv = [sys.argv[0], OUT + ".unused"]
sys.path.insert(0, "/src/equiv")
import pd_equiv as eq  # noqa: E402

import vllm.entrypoints.openai.completion.serving as serving_mod  # noqa: E402
import vllm.v1.engine.async_llm as async_llm_mod  # noqa: E402


def copy_of_original(rel_path, cls_name, fn_name, module):
    """Compile v0.30.0's own function into the port's module namespace, as a field
    patch that copies the original body would."""
    src = open(f"{CLEAN}/{rel_path}").read()
    tree = ast.parse(src)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls_name)
    fn = next(f for f in cls.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name == fn_name)
    fn.decorator_list = []
    code = textwrap.dedent(ast.get_source_segment(src, fn))
    namespace = {}
    exec(compile(code, f"<copy of {cls_name}.{fn_name}>", "exec"), module.__dict__, namespace)
    return namespace[fn_name]


AsyncLLM = async_llm_mod.AsyncLLM
if MODE in ("add_copy", "add_copy_owner"):
    AsyncLLM.add_request = copy_of_original("vllm/v1/engine/async_llm.py", "AsyncLLM", "add_request", async_llm_mod)
if MODE == "add_copy_owner":
    _orig = AsyncLLM._add_request

    async def _add_request(self, request, prompt, parent_req, index, queue, *, owner=None):
        return await _orig(self, request, prompt, parent_req, index, queue, owner=owner)

    AsyncLLM._add_request = _add_request
if MODE == "add_wrap":
    _port_add_request = AsyncLLM.add_request

    async def add_request(self, *args, **kwargs):
        # stand-in for a validation step, then the port's own function
        return await _port_add_request(self, *args, **kwargs)

    AsyncLLM.add_request = add_request
if MODE == "stream_copy":
    serving_mod.OpenAIServingCompletion.completion_stream_generator = copy_of_original(
        "vllm/entrypoints/openai/completion/serving.py",
        "OpenAIServingCompletion",
        "completion_stream_generator",
        serving_mod,
    )

try:
    asyncio.run(eq.frontend_scenarios())
except Exception:
    import traceback

    eq.RECORDS.append({"scenario": eq.SCENARIO[0], "driver_error": traceback.format_exc()})
with open(OUT, "w") as f:
    for r in eq.RECORDS:
        f.write(json.dumps(r, sort_keys=True, default=str) + "\n")
print("records", len(eq.RECORDS), flush=True)
