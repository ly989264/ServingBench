#!/usr/bin/env python3
"""Every internal attribute the package's added code uses exists in d4ad49796 / vLLM v0.30.0 (+ patch 01): AST only.

usage: api_existence_check.py ASCEND_BASE ASCEND_PATCHED VLLM_TREE FILE...

For each added line (difflib against the base file; all lines of a new file), every attribute read or written on a
name (root.attr, first attribute only) is resolved to a class or module and looked up:
  self                -> the enclosing class (with its bases, followed across vllm_ascend and vllm files)
  names in TYPE_MAP   -> the class the code receives there (read off the code; see TYPE_MAP)
  module aliases      -> the module's top-level names
Result per access: base (exists in d4ad49796 or in vLLM's tree, which carries patch 01), patch (added by this
package), NOT FOUND, or skipped (builtins, locals, third-party runtime objects; listed with the reason).
A class whose bases cannot all be resolved is reported as such for attributes not found.
"""

import ast
import difflib
import sys
from pathlib import Path

ASC = "vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store"
REQ_META = (f"{ASC}.metadata", "ReqMeta")
LOAD_SPEC = (f"{ASC}.metadata", "LoadSpec")
BATCH = ("vllm.v1.metrics.pd_diagnostics", "Batch")
TYPE_MAP = {
    "vllm_ascend/core/recompute_scheduler.py": {
        "request": ("vllm.v1.request", "Request"),
        "scheduler_output": ("vllm_ascend.core.recompute_scheduler", "RecomputeSchedulerOutput"),
    },
    f"{ASC.replace('.', '/')}/backend/mooncake_backend.py": {"call": (f"{ASC}.diagnostics", "BackendCall")},
    f"{ASC.replace('.', '/')}/backend/mooncake_layerwise.py": {
        "request": REQ_META, "view": REQ_META,
        "worker": (f"{ASC}.pool_worker", "KVPoolWorker"),
        "tracker": (f"{ASC}.backend.session_tracker", "LayerwiseSessionTracker"),
    },
    f"{ASC.replace('.', '/')}/diagnostics.py": {
        "call": (f"{ASC}.diagnostics", "BackendCall"), "task": (f"{ASC}.diagnostics", "Task"),
        "snapshot": (f"{ASC}.diagnostics", "Snapshot"),
        "store": (f"{ASC}.backend.mooncake_backend", "MooncakeBackend"),
    },
    f"{ASC.replace('.', '/')}/kv_transfer.py": {
        "req_meta": REQ_META, "batch": BATCH, "snapshot": (f"{ASC}.diagnostics", "Snapshot"),
        "task": (f"{ASC}.diagnostics", "Task"), "data": (f"{ASC}.metadata", "LayerLoadTask"),
        "attention_start_gate": (f"{ASC}.attention_fence", "AttentionComputeStartGate"),
    },
    f"{ASC.replace('.', '/')}/metadata.py": {"req_meta": REQ_META},
    f"{ASC.replace('.', '/')}/pool_scheduler.py": {
        "load_spec": LOAD_SPEC, "meta": (f"{ASC}.metadata", "AscendConnectorMetadata"), "req_meta": REQ_META,
        "scheduler_output": ("vllm.v1.core.sched.output", "SchedulerOutput"), "link": BATCH,
    },
    f"{ASC.replace('.', '/')}/pool_worker.py": {
        "load_spec": LOAD_SPEC, "req_meta": REQ_META, "request": REQ_META,
        "send_thread": (f"{ASC}.kv_transfer", "KVCacheStoreSendingThread"),
    },
    "vllm_ascend/worker/pd_device_timing.py": {
        "claim": ("vllm.v1.worker.worker_base", "PdFinishClaim"),
        "step": ("vllm_ascend.worker.pd_device_timing", "_Step"),
        "timing": ("vllm_ascend.worker.pd_device_timing", "DeviceTiming"),
    },
}
# Runtime objects of third-party libraries, checked elsewhere (named in the report).
RUNTIME = {
    "npu": "torch.npu API (Event(enable_timing), current_stream, is_current_stream_capturing): torch_npu, see probe/",
    "start": "torch.npu.Event (query, elapsed_time): torch_npu, see probe/", "end": "torch.npu.Event: see probe/",
    "current_event": "torch.npu.Event.synchronize, already called by d4ad49796 on the same object",
    "split_sizes": "numpy array", "split_gvas": "numpy array", "split_addrs": "numpy array",
    "local": "time.struct_time", "_active": "contextvars.ContextVar", "cls": "a class in type(self).__mro__",
}


class Index:
    def __init__(self, roots):
        self.roots = roots  # ordered: first wins
        self.cache = {}

    def path(self, module):
        rel = module.replace(".", "/")
        for root in self.roots:
            for cand in (root / f"{rel}.py", root / rel / "__init__.py"):
                if cand.exists():
                    return cand
        return None

    def module(self, module):
        if module not in self.cache:
            p = self.path(module)
            self.cache[module] = ast.parse(p.read_text()) if p else None
        return self.cache[module]

    def imports(self, module):
        tree, out = self.module(module), {}
        if tree is None:
            return out
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for a in node.names:
                    out[a.asname or a.name] = (node.module, a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    out[a.asname or a.name.split(".")[0]] = (a.name if a.asname else a.name.split(".")[0], None)
        return out

    def top_names(self, module):
        tree = self.module(module)
        names = set()
        if tree is None:
            return None
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names.update((a.asname or a.name).split(".")[0] for a in node.names)
        return names

    def find_class(self, module, name):
        tree = self.module(module)
        if tree is None:
            return None, module
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == name:
                return node, module
        target = self.imports(module).get(name)  # re-exported
        if target and target[1]:
            return self.find_class(*target)
        return None, module

    def class_attrs(self, module, name, seen=None):
        """(attributes, unresolved base names)"""
        seen = seen or set()
        if (module, name) in seen:
            return set(), set()
        seen.add((module, name))
        node, mod = self.find_class(module, name)
        if node is None:
            return set(), {f"{module}.{name}"}
        attrs, unresolved = set(), set()
        for stmt in node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                attrs.add(stmt.name)
            elif isinstance(stmt, ast.Assign):
                attrs.update(t.id for t in stmt.targets if isinstance(t, ast.Name))
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                attrs.add(stmt.target.id)
        for n in ast.walk(node):
            if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store) and isinstance(n.value, ast.Name) \
                    and n.value.id == "self":
                attrs.add(n.attr)
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "setattr" and n.args \
                    and isinstance(n.args[0], ast.Name) and n.args[0].id == "self" and isinstance(n.args[1], ast.Constant):
                attrs.add(n.args[1].value)
        if any(isinstance(d, ast.Call) and "NamedTuple" in ast.unparse(d) for d in node.bases) or \
                any(ast.unparse(b) in ("NamedTuple", "tuple") for b in node.bases):
            attrs.update({"_replace", "_asdict", "_fields"})
        imports = self.imports(mod)
        for base in node.bases:
            text = ast.unparse(base)
            if text in ("object", "ABC", "Generic", "Protocol", "NamedTuple") or text.startswith(("Generic[", "Protocol[")):
                continue
            if text in ("threading.Thread",):
                attrs.update({"start", "join", "is_alive", "name", "daemon", "run"})
                continue
            if isinstance(base, ast.Subscript):
                base = base.value
                text = ast.unparse(base)
            if isinstance(base, ast.Name):
                if self.find_class(mod, base.id)[0] is not None and base.id not in imports:
                    a, u = self.class_attrs(mod, base.id, seen)
                elif base.id in imports and imports[base.id][1]:
                    a, u = self.class_attrs(imports[base.id][0], imports[base.id][1], seen)
                else:
                    a, u = set(), {text}
            elif isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name) and base.value.id in imports:
                a, u = self.class_attrs(imports[base.value.id][0], base.attr, seen)
            else:
                a, u = set(), {text}
            attrs |= a
            unresolved |= u
        return attrs, unresolved


def added_lines(base_file, new_file):
    new_lines = new_file.read_text().splitlines()
    if not base_file.exists():
        return set(range(1, len(new_lines) + 1)), True
    old_lines = base_file.read_text().splitlines()
    added = set()
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            added.update(range(j1 + 1, j2 + 1))
    return added, False


def main():
    asc_base, asc_new, vllm_tree = (Path(p) for p in sys.argv[1:4])
    files = sys.argv[4:]
    base_ix = Index([asc_base, vllm_tree])
    new_ix = Index([asc_new, vllm_tree])
    totals = {"base": 0, "patch": 0, "NOT FOUND": 0, "skipped": 0}
    for f in files:
        module = f[:-3].replace("/", ".")
        added, is_new = added_lines(asc_base / f, asc_new / f)
        tree = ast.parse((asc_new / f).read_text())
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        imports = new_ix.imports(module)
        tmap = TYPE_MAP.get(f, {})
        rows = {}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and getattr(node, "lineno", None) in added):
                continue
            root, attr = node.value.id, node.attr
            key = f"{root}.{attr}"
            if key in rows:
                continue
            cls = None
            p = node
            while p in parents:
                p = parents[p]
                if isinstance(p, ast.ClassDef):
                    cls = p.name
                    break
            if root == "self" and cls:
                target = (module, cls)
            elif root in tmap:
                target = tmap[root]
            elif root in imports and imports[root][1] is None:  # module alias
                mod = imports[root][0]
                names_new = new_ix.top_names(mod)
                if names_new is None:
                    rows[key] = ("skipped", f"module {mod} (outside the trees: stdlib / third party)")
                    continue
                names_base = base_ix.top_names(mod)
                status = "base" if names_base and attr in names_base else ("patch" if attr in names_new else "NOT FOUND")
                rows[key] = (status, f"module {mod}")
                continue
            elif root in imports and imports[root][1]:  # an imported module-level object, e.g. diagnostics as pool_diag
                mod, name = imports[root]
                full = f"{mod}.{name}"
                names_new = new_ix.top_names(full)
                if names_new is not None:
                    names_base = base_ix.top_names(full)
                    status = "base" if names_base and attr in names_base else (
                        "patch" if attr in names_new else "NOT FOUND")
                    rows[key] = (status, f"module {full}")
                else:
                    rows[key] = ("skipped", f"imported object {full}")
                continue
            elif root in RUNTIME:
                rows[key] = ("skipped", RUNTIME[root])
                continue
            else:
                rows[key] = ("skipped", "local / builtin value")
                continue
            attrs_base, unres_base = base_ix.class_attrs(*target)
            attrs_new, unres_new = new_ix.class_attrs(*target)
            if attr in attrs_base:
                rows[key] = ("base", f"{target[0]}.{target[1]}")
            elif attr in attrs_new:
                rows[key] = ("patch", f"{target[0]}.{target[1]}")
            else:
                rows[key] = ("NOT FOUND", f"{target[0]}.{target[1]}" +
                             (f" (unresolved bases: {sorted(unres_new)})" if unres_new else ""))
        print(f"== {f}: {'new file' if is_new else f'{len(added)} added lines'}, {len(rows)} root.attr")
        for key, (status, where) in sorted(rows.items(), key=lambda kv: (kv[1][0] != "NOT FOUND", kv[1][0], kv[0])):
            totals[status] += 1
            print(f"  {status:9} {key:45} {where}")
    print("\ntotals:", totals)
    return 1 if totals["NOT FOUND"] else 0


if __name__ == "__main__":
    sys.exit(main())
