#!/usr/bin/env python3
"""Write two lab patch files that replace functions patch 01 changes (vLLM v0.30.0 AsyncLLM):
patch_site_copy01.py: AsyncLLM.add_request copied whole from v0.30.0, three lines edited, assigned back (no call
to the original; wraps_orig=0); it also carries an IP and a /data path to test --show-diff redaction.
patch_site_wrap01.py: AsyncLLM._add_request wrapped: saves the original, calls it (wraps_orig=1).
Usage: make_01_patches.py VLLM_V0300_TREE OUT_DIR"""
import ast, os, sys, textwrap

tree_root, out = sys.argv[1], sys.argv[2]
src = open(os.path.join(tree_root, "vllm/v1/engine/async_llm.py"), encoding="utf-8").read()
cls = next(n for n in ast.parse(src).body if isinstance(n, ast.ClassDef) and n.name == "AsyncLLM")
fn = next(n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "add_request")
lines = src.splitlines()
start = min([fn.lineno] + [d.lineno for d in fn.decorator_list])
body = textwrap.dedent("\n".join(lines[start - 1 : fn.end_lineno]))
blines = body.splitlines()
# edits: one line inserted after the def line's docstring/first statement, one line changed, one line deleted
stmts = fn.body[1:] if isinstance(fn.body[0], ast.Expr) and isinstance(getattr(fn.body[0], "value", None), ast.Constant) else fn.body
first = stmts[0].lineno - start  # first statement after the signature (and docstring)
ind = blines[first][: len(blines[first]) - len(blines[first].lstrip())]
blines.insert(first, ind + 'site_mirror = "http://10.1.2.3:8000/data/example/notes"  # site edit')
# a second inserted line before the last statement (whole statements only, so the copy still parses)
last_stmt = stmts[-1].lineno - start
blines.insert(last_stmt + 1, ind + "site_counter = 0  # site edit")
copy = "from vllm.v1.engine.async_llm import AsyncLLM\n\n\n" + "\n".join(blines) + "\n\n\nAsyncLLM.add_request = add_request\n"
open(os.path.join(out, "patch_site_copy01.py"), "w").write(copy)
wrap = '''from vllm.v1.engine import async_llm

_orig_add_request = async_llm.AsyncLLM._add_request


def _add_request(self, *args, **kwargs):
    kwargs.pop("site_flag", None)
    return _orig_add_request(self, *args, **kwargs)


async_llm.AsyncLLM._add_request = _add_request
'''
open(os.path.join(out, "patch_site_wrap01.py"), "w").write(wrap)
print("copy01 lines", len(copy.splitlines()), "wrap01 lines", len(wrap.splitlines()))
