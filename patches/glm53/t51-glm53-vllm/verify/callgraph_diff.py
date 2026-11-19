"""Call paths of unpatched v0.30.0 that the patched tree no longer takes. AST only, no imports.

usage: callgraph_diff.py BASE_TREE PATCHED_TREE FILE...      (FILE relative to the trees; new files may be listed)

For every function or method F of a base file that still exists in the patched file, and every callee C: does F
still make at least as many calls to C as in the base file, counting call sites?
  patched count = call sites of C in F's own body, plus call sites of C in the functions the patch added (not in the
  base files) that F reaches through them: code 01 moved out of F into a new helper still counts. Resolution without
  imports: `self.m(...)` / `cls.m(...)` / `super().m(...)` -> method m of F's class or of a class in these files that
  F's class derives from; `f(...)` -> function f of the same module.
A call is keyed by what it names: `self.m` for a method of the instance, `name` for a plain name, `.attr` for any
other attribute call (`self.engine_core.add_request_async(...)` -> `.add_request_async`).
Reported: callees with fewer call sites than in the base file. Each one needs reading (a call that moved into an
existing function, or behind another object, is reported too).

Second list: calls in the patched tree to a function or method that exists in the base files with keyword arguments
that the base definition does not accept (a replacement written with the base signature would reject them).
"""
import ast
import sys
from collections import defaultdict

base_root, patched_root, files = sys.argv[1], sys.argv[2], sys.argv[3:]


def key_of(call):
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        v = f.value
        if isinstance(v, ast.Name) and v.id in ("self", "cls"):
            return "self." + f.attr
        if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "super":
            return "self." + f.attr
        return "." + f.attr
    return None


class Index:
    def __init__(self, root):
        self.funcs = {}            # (file, qual) -> node
        self.methods = defaultdict(list)   # (file, class, name) -> qual
        self.modfuncs = {}         # (file, name) -> qual
        self.bases = {}            # class name -> [base names]
        self.class_file = defaultdict(set)
        for rel in files:
            try:
                tree = ast.parse(open(f"{root}/{rel}").read())
            except FileNotFoundError:
                continue
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.funcs[(rel, node.name)] = node
                    self.modfuncs[(rel, node.name)] = node.name
                elif isinstance(node, ast.ClassDef):
                    self.bases[node.name] = [ast.unparse(b).split(".")[-1] for b in node.bases]
                    self.class_file[node.name].add(rel)
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            q = f"{node.name}.{item.name}"
                            self.funcs[(rel, q)] = item

    def mro(self, cls):
        out, todo = [], [cls]
        while todo:
            c = todo.pop(0)
            if c in out:
                continue
            out.append(c)
            todo += self.bases.get(c, [])
        return out

    def resolve(self, rel, qual, key):
        """Functions in these files that a call key from (rel, qual) reaches without imports."""
        out = []
        cls = qual.split(".")[0] if "." in qual else None
        if key.startswith("self.") and cls:
            name = key[5:]
            for c in self.mro(cls):
                for r in self.class_file.get(c, ()):
                    if (r, f"{c}.{name}") in self.funcs:
                        out.append((r, f"{c}.{name}"))
                if out:
                    break
        elif not key.startswith(".") and (rel, key) in self.funcs:
            out.append((rel, key))
        return out

    def direct(self, node):
        calls = []
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                k = key_of(n)
                if k:
                    calls.append((k, n))
        return calls

    def closure(self, rel, qual):
        seen, todo, keys = set(), [(rel, qual)], set()
        while todo:
            item = todo.pop()
            if item in seen or item not in self.funcs:
                continue
            seen.add(item)
            node = self.funcs[item]
            for k, _ in self.direct(node):
                keys.add(k)
                todo += self.resolve(item[0], item[1], k)
            # A decorator defined in these files wraps this function: its wrapper is on the path too.
            for d in node.decorator_list:
                dk = d.func if isinstance(d, ast.Call) else d
                if isinstance(dk, ast.Name) and (item[0], dk.id) in self.funcs:
                    todo.append((item[0], dk.id))
        return keys


B, P = Index(base_root), Index(patched_root)
print("== callees with fewer call sites than in v0.30.0 (same function, plus helpers the patch added)")
n_missing = 0
for (rel, qual), node in sorted(B.funcs.items(), key=lambda x: x[0]):
    if (rel, qual) not in P.funcs:
        continue
    base_count = defaultdict(list)
    for k, call in B.direct(node):
        base_count[k].append(call.lineno)
    patched_count = defaultdict(int)
    seen, todo = set(), [(rel, qual)]
    while todo:
        item = todo.pop()
        if item in seen or item not in P.funcs:
            continue
        seen.add(item)
        for k, _ in P.direct(P.funcs[item]):
            patched_count[k] += 1
            todo += [t for t in P.resolve(item[0], item[1], k) if t not in B.funcs]
    for k, lines in sorted(base_count.items()):
        if patched_count[k] < len(lines):
            n_missing += 1
            print(f"  {rel}::{qual}: {k}  v0.30.0 {len(lines)} site(s) (line {','.join(map(str, sorted(set(lines))))}), patched {patched_count[k]}")
print("  items:", n_missing)

print("== calls in the patched tree passing keywords the v0.30.0 definition does not accept")
n_kw = 0
for (rel, qual), node in sorted(P.funcs.items(), key=lambda x: x[0]):
    for k, call in P.direct(node):
        kws = [kw.arg for kw in call.keywords if kw.arg]
        if not kws:
            continue
        for target in B.resolve(rel, qual, k):
            t = B.funcs[target]
            a = t.args
            if a.kwarg is not None:
                continue
            accepted = {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}
            extra = [x for x in kws if x not in accepted]
            if extra:
                n_kw += 1
                print(f"  {rel}::{qual} line {call.lineno} -> {target[1]}: {extra}")
print("  items:", n_kw)

print("== callees with more call sites than in v0.30.0 (same function, plus helpers the patch added)")
# Classification of the added call sites, for reading:
#   diag      pd_diagnostics (module diag, or a method only its classes define)
#   patch     a function or method the patch added
#   stdlib    builtins and standard-library calls
#   vllm      anything else: an existing vLLM (or third-party) API newly called -> read each one
import builtins as _b
DIAG_METHODS = set()
for rel in files:
    if rel.endswith("pd_diagnostics.py"):
        try:
            t = ast.parse(open(f"{patched_root}/{rel}").read())
        except FileNotFoundError:
            continue
        for n in ast.walk(t):
            if isinstance(n, ast.ClassDef):
                DIAG_METHODS |= {m.name for m in n.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
NEW = {q.split(".")[-1] for (r, q) in P.funcs if (r, q) not in B.funcs}
BASE_METHOD_NAMES = {q.split(".")[-1] for (r, q) in B.funcs}
STDLIB_MODS = {"time", "os", "sys", "json", "threading", "asyncio", "itertools", "functools", "contextlib", "math",
               "collections", "weakref", "uuid", "re", "inspect", "traceback", "logging", "dataclasses", "copy", "queue"}
BUILTIN_NAMES = set(dir(_b))
TYPE_METHODS = set()
for t in (str, dict, list, set, frozenset, tuple, int, float, bytes, object):
    TYPE_METHODS |= set(dir(t))


def classify(text, key):
    head = text.split(".")[0].split("(")[0]
    last = key.lstrip(".").split(".")[-1]
    if head == "diag" or text.startswith("_pd") or text.startswith("self._pd") or text.startswith("pd_"):
        return "diag"
    if last in DIAG_METHODS and last not in BASE_METHOD_NAMES and last not in TYPE_METHODS:
        return "diag"
    if last in NEW and last not in BASE_METHOD_NAMES:
        return "patch"
    if head in STDLIB_MODS or (key in BUILTIN_NAMES) or (key.startswith(".") and last in TYPE_METHODS and last not in BASE_METHOD_NAMES):
        return "stdlib"
    return "vllm"


n_new = 0
import os as _os
PRINT_ALL = _os.environ.get("CG_ALL") == "1"
by_class = defaultdict(int)
for (rel, qual), node in sorted(B.funcs.items(), key=lambda x: x[0]):
    if (rel, qual) not in P.funcs:
        continue
    base_count = defaultdict(int)
    for k, _ in B.direct(node):
        base_count[k] += 1
    sites = defaultdict(list)
    seen, todo = set(), [(rel, qual)]
    while todo:
        item = todo.pop()
        if item in seen or item not in P.funcs:
            continue
        seen.add(item)
        for k, call in P.direct(P.funcs[item]):
            sites[k].append((item[1], call.lineno, ast.unparse(call.func)))
            todo += [t for t in P.resolve(item[0], item[1], k) if t not in B.funcs]
    for k, lst in sorted(sites.items()):
        if len(lst) > base_count[k]:
            cls = classify(lst[0][2], k)
            by_class[cls] += 1
            n_new += 1
            if cls == "vllm" or PRINT_ALL:
                where = "; ".join(f"{q}:{ln} {txt}" for q, ln, txt in lst[:3])
                print(f"  [{cls}] {rel}::{qual}: {k}  v0.30.0 {base_count[k]}, patched {len(lst)}  ({where})")
print("  items:", n_new, dict(by_class), "(only [vllm] items are printed)")
