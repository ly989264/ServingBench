"""Write simulated field patches: v0.30.0's original AsyncLLM.add_request, copied into a
module-level function, with a change per case. Standard library only.

usage: python3 gen_variants.py ORIGINAL_async_llm.py OUT_DIR
"""
import ast
import builtins
import json
import os
import sys
import textwrap

orig_path, out = sys.argv[1], sys.argv[2]
src = open(orig_path).read()
tree = ast.parse(src)
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AsyncLLM")
fn = next(f for f in cls.body if isinstance(f, ast.AsyncFunctionDef) and f.name == "add_request")
# get_source_segment drops the first line's indentation; restore it before dedenting.
copy_text = textwrap.dedent(" " * fn.col_offset + ast.get_source_segment(src, fn))

# The names the copied body needs, imported from the module it was copied from.
top = set()
for n in tree.body:
    if isinstance(n, (ast.Import, ast.ImportFrom)):
        top.update((a.asname or a.name).split(".")[0] for a in n.names)
    elif isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        top.add(n.name)
    elif isinstance(n, ast.Assign):
        top.update(x.id for t in n.targets for x in ast.walk(t) if isinstance(x, ast.Name))
used = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
imports = sorted((used & top) - set(dir(builtins)))
HEADER = (
    '"""Simulated field patch for the replay tool tests (not the real field file)."""\n'
    f"from vllm.v1.engine.async_llm import AsyncLLM, {', '.join(n for n in imports if n != 'AsyncLLM')}\n"
    "\n_T51_CALLS = []\n\n\n"
)
FOOTER = "\n\n\nAsyncLLM.add_request = add_request\n"


def insert_after(text, anchor, lines, occurrence=1):
    idx = -1
    for _ in range(occurrence):
        idx = text.index(anchor, idx + 1)
    end = text.index("\n", idx + len(anchor) - 1) if not anchor.endswith("\n") else idx + len(anchor) - 1
    col = len(text[: idx].split("\n")[-1])
    block = "".join(" " * col + line + "\n" for line in lines)
    return text[: end + 1] + block + text[end + 1:]


def statement_end(text, anchor):
    """Index just after the full (possibly multi-line) statement that starts with anchor."""
    idx = text.index(anchor)
    depth, k = 0, idx
    while True:
        c = text[k]
        depth += c in "([{"
        depth -= c in ")]}"
        if c == "\n" and depth == 0:
            return idx, k
        k += 1


def after_statement(text, anchor, lines):
    idx, end = statement_end(text, anchor)
    col = len(text[: idx].split("\n")[-1])
    return text[: end + 1] + "".join(" " * col + line + "\n" for line in lines) + text[end + 1:]


VALIDATION = [
    "_T51_CALLS.append(('top', request_id))",
    "if isinstance(prompt, dict) and len(prompt.get('prompt_token_ids') or ()) > 10**9:",
    "    raise VLLMValidationError('prompt longer than the prefill limit')",
]
cases = {}
cases["T1_top_level"] = (after_statement(copy_text, "is_pooling = isinstance(params, PoolingParams)", VALIDATION), "merged")
cases["T2_nested"] = (after_statement(copy_text, "prompt_text, _, _ = extract_prompt_components(self.model_config, prompt)",
                                      ["_T51_CALLS.append(('nested', request_id))"]), "merged")
first = "if self.errored:"
i = copy_text.index(first)
col = len(copy_text[:i].split("\n")[-1])
cases["T3_after_docstring"] = (copy_text[:i] + "_T51_CALLS.append(('start', request_id))\n" + " " * col + copy_text[i:], "merged")
cases["T4_changed"] = (copy_text.replace("and params.prompt_logprobs\n", "and params.prompt_logprobs is not None\n", 1), "refused")
t5 = copy_text.replace("    if reasoning_ended is not None:\n        request.reasoning_ended = reasoning_ended\n", "", 1)
cases["T5_removed"] = (t5, "refused")
cases["T6_anchor_rewritten_by_01"] = (after_statement(copy_text, "await self._add_request(request, prompt_text, None, 0, queue)",
                                                      ["_T51_CALLS.append(('after_add', request_id))"]), "refused")
k = copy_text.index("        return queue\n")  # inside `if is_pooling or params.n == 1:`
cases["T7_ambiguous_anchor"] = (copy_text[: k + len("        return queue\n")]
                                + "        _T51_CALLS.append('unreachable')\n" + copy_text[k + len("        return queue\n"):], "refused")
cases["T8_signature"] = (copy_text.replace("reasoning_parser_kwargs: dict[str, Any] | None = None,\n",
                                           "reasoning_parser_kwargs: dict[str, Any] | None = None,\n    extra: int | None = None,\n", 1), "refused")
cases["T9_multiline_string"] = (after_statement(copy_text, "is_pooling = isinstance(params, PoolingParams)",
                                                ['_T51_NOTE = """line one', 'line two"""']), "refused")
t10 = after_statement(copy_text, "is_pooling = isinstance(params, PoolingParams)", VALIDATION)
t10 = after_statement(t10, "prompt_text, _, _ = extract_prompt_components(self.model_config, prompt)",
                      ["_T51_CALLS.append(('nested', request_id))"])
cases["T10_two_insertions"] = (t10, "merged")

os.makedirs(out, exist_ok=True)
expect = {}
for name, (body, expected) in cases.items():
    if body == copy_text and name != "T0":
        raise SystemExit(f"{name}: the change was not applied")
    with open(os.path.join(out, f"{name}.py"), "w") as f:
        f.write(HEADER + body + FOOTER)
    expect[name] = expected
with open(os.path.join(out, "expected.json"), "w") as f:
    json.dump(expect, f, indent=1)
print("wrote", len(cases), "cases to", out)
