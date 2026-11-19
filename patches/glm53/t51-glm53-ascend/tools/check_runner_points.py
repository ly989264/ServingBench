"""Static check of the runner measurement points on a patched vllm-ascend tree (d4ad49796 layout).

    python3 -I check_runner_points.py VLLM_ASCEND_ROOT

Checks, by AST, that:
  V1 worker/model_runner_v1.py
    - execute_model carries @pd_execution under @torch.inference_mode(); sample_tokens carries @pd_sample
    - handle_preemptions (the wait_for_put call site) is inside execute_model
    - every run_model() call in _model_forward sits in a `with diag.span("runner.forward", ...)`
    - both sampler calls in _sample sit in `with diag.span("runner.sampling", ...)`
    - _sample is called from sample_tokens, and propose_draft_token_ids is not inside any span
  V2 worker/v2/model_runner.py
    - execute_model @pd_execution, sample_tokens @pd_sample, sample() wraps sampler/rejection_sampler
Prints one line per check and exits non-zero on a failed check.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def decorators(function):
    names = []
    for node in function.decorator_list:
        target = node.func if isinstance(node, ast.Call) else node
        names.append(ast.unparse(target))
    return names


def methods(tree, cls_name):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls_name:
            return {f.name: f for f in node.body if isinstance(f, ast.FunctionDef)}
    raise SystemExit(f"class {cls_name} not found")


def span_name(with_node):
    for item in with_node.items:
        call = item.context_expr
        if isinstance(call, ast.Call) and ast.unparse(call.func) == "diag.span" and call.args:
            return ast.literal_eval(call.args[0])
    return None


def calls_with_spans(function, call_text):
    """Return the enclosing span name (or None) for every call whose func unparses to call_text."""
    found = []

    def visit(node, span):
        if isinstance(node, ast.With):
            span = span_name(node) or span
        if isinstance(node, ast.Call) and ast.unparse(node.func) == call_text:
            found.append(span)
        for child in ast.iter_child_nodes(node):
            visit(child, span)

    visit(function, None)
    return found


def main():
    root = Path(sys.argv[1])
    results = []

    def check(label, ok, detail=""):
        results.append(ok)
        print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail else ""))

    v1 = methods(ast.parse((root / "vllm_ascend/worker/model_runner_v1.py").read_text()), "NPUModelRunner")
    check("V1 execute_model decorators", decorators(v1["execute_model"])[:2] == ["torch.inference_mode", "pd_execution"],
          ",".join(decorators(v1["execute_model"])))
    check("V1 sample_tokens decorators", decorators(v1["sample_tokens"])[:2] == ["torch.inference_mode", "pd_sample"],
          ",".join(decorators(v1["sample_tokens"])))
    preempt = calls_with_spans(v1["execute_model"], "get_kv_transfer_group().handle_preemptions")
    check("V1 handle_preemptions inside execute_model", len(preempt) == 1, f"calls={len(preempt)}")
    forward = calls_with_spans(v1["_model_forward"], "run_model")
    check("V1 run_model() all inside runner.forward", forward and all(s == "runner.forward" for s in forward),
          f"spans={forward}")
    sampler = calls_with_spans(v1["_sample"], "self.sampler") + calls_with_spans(v1["_sample"], "self.rejection_sampler")
    check("V1 sampler calls inside runner.sampling", len(sampler) == 2 and all(s == "runner.sampling" for s in sampler),
          f"spans={sampler}")
    sample_sites = {name for name, f in v1.items() if calls_with_spans(f, "self._sample")}
    check("V1 _sample called from sample_tokens only", sample_sites == {"sample_tokens"}, ",".join(sorted(sample_sites)))
    draft = calls_with_spans(v1["sample_tokens"], "self.propose_draft_token_ids")
    check("V1 draft proposal outside forward/sampling spans", draft and all(s is None for s in draft),
          f"calls={len(draft)}")
    forward_sites = {name for name, f in v1.items() if calls_with_spans(f, "self._model_forward")}
    check("V1 _model_forward callers", forward_sites <= {"execute_model", "_dummy_run"}, ",".join(sorted(forward_sites)))

    v2 = methods(ast.parse((root / "vllm_ascend/worker/v2/model_runner.py").read_text()), "NPUModelRunner")
    check("V2 execute_model decorators", "pd_execution" in decorators(v2["execute_model"]),
          ",".join(decorators(v2["execute_model"])))
    check("V2 sample_tokens decorators", "pd_sample" in decorators(v2["sample_tokens"]),
          ",".join(decorators(v2["sample_tokens"])))
    sampler2 = calls_with_spans(v2["sample"], "self.sampler") + calls_with_spans(v2["sample"], "self.rejection_sampler")
    check("V2 sampler calls inside runner.sampling", len(sampler2) == 2 and all(s == "runner.sampling" for s in sampler2),
          f"spans={sampler2}")
    preempt2 = calls_with_spans(v2["execute_model"], "get_kv_transfer_group().handle_preemptions")
    check("V2 handle_preemptions inside execute_model", len(preempt2) == 1, f"calls={len(preempt2)}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
