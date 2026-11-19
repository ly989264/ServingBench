"""Supplementary code checks (X1-X6) for the p2d replay on a site copy of kv_p2p/mooncake_connector.py.

They look at code the stop facts of replay_p2d.py (F01-F31) do not cover and that the site file is known to
change (risks R1-R5 in field_check/README.zh-CN.md, section 6). A value different from the vllm-ascend
d4ad49796 original does not stop anything; it only says which situation to bring back for review.

Pure functions, standard library only (ast), no file reads or writes, no imports of vLLM or vllm-ascend:

    extra_checks(source: str) -> dict[str, str]   # {"X1": "1", ..., "X6": "ok"}
    extra_line(source: str) -> str                # "p2d_extra X1=1 X2=1/1 X3=2 X4=4/1 X5=1 X6=ok"

In extra_line a value different from EXPECTED is followed by "!". A function that cannot be found, or a
source that does not parse, gives the value "missing" (also marked "!").

| item | function | what is counted | original | risk |
|---|---|---|---|---|
| X1 | KVCacheRecvingThread._transfer_kv_cache_all_groups | raise statements whose exception text contains "Mooncake transfer failed", inside an if whose test compares ret with < 0 | 1 | R2 |
| X2 | MooncakeConnectorScheduler.update_state_after_alloc | assignments params["do_remote_prefill"] = False / assignments to self._reqs_need_recv[...] | 1/1 | R1 |
| X3 | MooncakeConnectorWorker.start_load_kv | calls of .add_not_transfer_request(...) | 2 | R3 |
| X4 | MooncakeConnectorScheduler.request_finished | return statements / those returning a tuple whose second value is not None | 4/1 | R4 |
| X5 | MooncakeConnectorScheduler.request_finished | assignments to self._reqs_need_send[...] | 1 | R4 |
| X6 | KVCacheRecvingThread.add_request | the trans_info dict takes "request_id" and "all_task_done" straight from the parameters of the same names: ok / changed | ok | R5 |

Nested functions inside a method are not counted.
"""

import ast

EXPECTED = {"X1": "1", "X2": "1/1", "X3": "2", "X4": "4/1", "X5": "1", "X6": "ok"}
ITEMS = ("X1", "X2", "X3", "X4", "X5", "X6")


def _methods(tree):
    out = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[f"{node.name}.{item.name}"] = item
    return out


def _own(func):
    """Nodes of a function without the bodies of functions or classes defined inside it."""
    stack = list(func.body)
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(node))


def _parents(func):
    out = {}
    for node in ast.walk(func):
        for child in ast.iter_child_nodes(node):
            out[child] = node
    return out


def _target_text(node):
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        return ast.unparse(node.targets[0])
    if isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        return ast.unparse(node.target)
    return None


def _x1(f):
    parent = _parents(f)
    count = 0
    for node in _own(f):
        if isinstance(node, ast.Raise) and node.exc is not None and "Mooncake transfer failed" in ast.unparse(node.exc):
            up = parent.get(node)
            while up is not None and up is not f:
                if isinstance(up, ast.If):
                    test = ast.unparse(up.test).replace(" ", "")
                    if "ret<0" in test:
                        count += 1
                        break
                up = parent.get(up)
    return str(count)


def _x2(f):
    resets = sum(1 for n in _own(f) if isinstance(n, ast.Assign) and _target_text(n) in (
        "params['do_remote_prefill']", 'params["do_remote_prefill"]') and ast.unparse(n.value) == "False")
    adds = sum(1 for n in _own(f) if (_target_text(n) or "").startswith("self._reqs_need_recv["))
    return f"{resets}/{adds}"


def _x3(f):
    return str(sum(1 for n in _own(f) if isinstance(n, ast.Call) and ast.unparse(n.func).endswith(".add_not_transfer_request")))


def _x4(f):
    returns = [n for n in _own(f) if isinstance(n, ast.Return)]
    handing = sum(1 for n in returns if isinstance(n.value, ast.Tuple) and len(n.value.elts) > 1
                  and ast.unparse(n.value.elts[1]) != "None")
    return f"{len(returns)}/{handing}"


def _x5(f):
    return str(sum(1 for n in _own(f) if (_target_text(n) or "").startswith("self._reqs_need_send[")))


def _x6(f):
    dicts = [n.value for n in _own(f) if isinstance(n, ast.Assign) and _target_text(n) == "trans_info"
             and isinstance(n.value, ast.Dict)]
    if len(dicts) != 1:
        return "missing"
    values = {ast.unparse(k).strip("'\""): ast.unparse(v) for k, v in zip(dicts[0].keys, dicts[0].values) if k is not None}
    return "ok" if values.get("request_id") == "request_id" and values.get("all_task_done") == "all_task_done" else "changed"


CHECKS = {
    "X1": ("KVCacheRecvingThread._transfer_kv_cache_all_groups", _x1),
    "X2": ("MooncakeConnectorScheduler.update_state_after_alloc", _x2),
    "X3": ("MooncakeConnectorWorker.start_load_kv", _x3),
    "X4": ("MooncakeConnectorScheduler.request_finished", _x4),
    "X5": ("MooncakeConnectorScheduler.request_finished", _x5),
    "X6": ("KVCacheRecvingThread.add_request", _x6),
}


def extra_checks(source: str) -> dict:
    """X1-X6 of a mooncake_connector.py source text, as strings; "missing" when a function is absent."""
    try:
        methods = _methods(ast.parse(source))
    except SyntaxError:
        return {item: "missing" for item in ITEMS}
    out = {}
    for item in ITEMS:
        name, check = CHECKS[item]
        out[item] = check(methods[name]) if name in methods else "missing"
    return out


def extra_line(source: str) -> str:
    """One line: p2d_extra X1=.. X2=.. ...; a value different from the d4ad49796 original ends with '!'."""
    values = extra_checks(source)
    return "p2d_extra " + " ".join(
        f"{item}={values[item]}{'' if values[item] == EXPECTED[item] else '!'}" for item in ITEMS)
