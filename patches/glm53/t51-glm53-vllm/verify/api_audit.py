"""Every vLLM-internal name that the patch's added code uses: does it exist in v0.30.0, and is its definition the
same as in v0.32.0 (which patch 01 was written against)? AST only for the trees; runs in an image that has the
third-party packages, only to list the attribute names of builtin and third-party types.

usage: api_audit.py V030_TREE PATCHED_TREE V032_TREE RUNTIME_DIFF NEW_FILE...
  RUNTIME_DIFF: `git diff -U0 v0.30.0 <port>` of vllm/ (added lines = the patch's code)

Reported, for reading:
  1. imports in added code that do not resolve in the patched tree, or that resolve only to names the patch adds
     although the module existed in v0.30.0 without them (listed, not an error);
  2. attribute names read in added code (including getattr/hasattr/setattr string literals) that are defined
     nowhere: not in the v0.30.0 vllm package, not by the patch, not on builtin, standard-library or third-party
     types used here -> would raise AttributeError, or silently take getattr's default;
  3. attribute names defined in v0.30.0, where a class of the same name defines the attribute in v0.32.0 but not in
     v0.30.0 -> 01 may read it from an object that lacks it on v0.30.0;
  4. vLLM functions and methods the added code calls, whose definition differs between v0.30.0 and v0.32.0
     (docstrings ignored) -> semantics to compare by reading.
"""
import ast
import os
import re
import sys
from collections import defaultdict

V030, PATCHED, V032, DIFF, NEW_FILES = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5:]


def py_files(root):
    for d, _, fs in os.walk(os.path.join(root, "vllm")):
        for f in fs:
            if f.endswith(".py"):
                p = os.path.join(d, f)
                yield os.path.relpath(p, root)


def parse(root, rel):
    try:
        return ast.parse(open(os.path.join(root, rel)).read())
    except (SyntaxError, FileNotFoundError, UnicodeDecodeError):
        return None


class Defs:
    """Where attribute names are defined in a tree."""

    def __init__(self, root):
        self.attr_classes = defaultdict(set)   # name -> {class names defining it}
        self.attr_any = set()                  # names assigned as obj.name = ... anywhere, or class members
        self.module_names = {}                 # module dotted -> set(top-level names)
        self.funcs = defaultdict(list)         # name -> [(rel, qualname, node)]
        self.stars = []
        for rel in py_files(root):
            tree = parse(root, rel)
            if tree is None:
                continue
            mod = rel[:-3].replace("/", ".")
            if mod.endswith(".__init__"):
                mod = mod[: -len(".__init__")]
            top = set()
            for n in tree.body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    top.add(n.name)
                elif isinstance(n, (ast.Assign, ast.AnnAssign)):
                    for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                        for x in ast.walk(t):
                            if isinstance(x, ast.Name):
                                top.add(x.id)
                elif isinstance(n, (ast.Import, ast.ImportFrom)):
                    for a in n.names:
                        top.add((a.asname or a.name).split(".")[0])
                elif isinstance(n, (ast.If, ast.Try)):
                    for x in ast.walk(n):
                        if isinstance(x, (ast.FunctionDef, ast.ClassDef)):
                            top.add(x.name)
                        elif isinstance(x, (ast.Import, ast.ImportFrom)):
                            for a in x.names:
                                top.add((a.asname or a.name).split(".")[0])
                        elif isinstance(x, ast.Assign):
                            for t in x.targets:
                                if isinstance(t, ast.Name):
                                    top.add(t.id)
            if rel.endswith("vllm/envs.py") or rel == "vllm/envs.py":
                for x in ast.walk(tree):
                    if isinstance(x, ast.Dict):
                        top |= {k.value for k in x.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
                    if isinstance(x, ast.AnnAssign) and isinstance(x.target, ast.Name):
                        top.add(x.target.id)
            self.module_names[mod] = top
            for n in tree.body:
                if isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names):
                    target = n.module or ""
                    if n.level:
                        pkg = mod.split(".") if rel.endswith("__init__.py") else mod.split(".")[:-1]
                        pkg = pkg[: len(pkg) - n.level + 1]
                        target = ".".join(pkg + ([n.module] if n.module else []))
                    self.stars.append((mod, target))
            for n in ast.walk(tree):
                if isinstance(n, ast.ClassDef):
                    for item in n.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            self.attr_classes[item.name].add(n.name)
                            self.funcs[item.name].append((rel, f"{n.name}.{item.name}", item))
                        elif isinstance(item, (ast.Assign, ast.AnnAssign)):
                            for t in (item.targets if isinstance(item, ast.Assign) else [item.target]):
                                for x in ast.walk(t):
                                    if isinstance(x, ast.Name):
                                        self.attr_classes[x.id].add(n.name)
                    for item in ast.walk(n):
                        if isinstance(item, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                            for t in (item.targets if isinstance(item, ast.Assign) else [item.target]):
                                for x in ast.walk(t):
                                    if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "self":
                                        self.attr_classes[x.attr].add(n.name)
                if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                        for x in ast.walk(t):
                            if isinstance(x, ast.Attribute):
                                self.attr_any.add(x.attr)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.attr_any.add(n.name)
                    self.funcs[n.name].append((rel, n.name, n))
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "setattr" and len(n.args) >= 2 \
                        and isinstance(n.args[1], ast.Constant) and isinstance(n.args[1].value, str):
                    self.attr_any.add(n.args[1].value)
                if isinstance(n, ast.keyword) and n.arg:
                    pass
            for names in self.module_names.values():
                pass

    def resolve_stars(self):
        for _ in range(3):
            for mod, target in self.stars:
                self.module_names.setdefault(mod, set()).update(self.module_names.get(target, set()))

    def defined(self, name):
        return name in self.attr_classes or name in self.attr_any or any(name in v for v in self.module_names.values())


def added_lines(diff_path):
    """Added line numbers per file, and the subset that is new code: an added line whose text (whitespace ignored)
    is also among the file's deleted lines was only moved or re-indented (01 wraps whole bodies in `with` blocks)."""
    out, new, cur, nline = defaultdict(set), defaultdict(set), None, None
    deleted, added = defaultdict(list), defaultdict(list)
    for line in open(diff_path):
        if line.startswith("diff --git"):
            cur = None
        elif line.startswith("+++ "):
            cur = line[6:].strip() if line.startswith("+++ b/") else None
        elif line.startswith("--- "):
            continue
        elif line.startswith("@@") and cur:
            m = re.match(r"@@ -\S+ \+(\d+)(?:,(\d+))? @@", line)
            nline = int(m.group(1))
        elif cur and line.startswith("+"):
            out[cur].add(nline)
            added[cur].append((nline, re.sub(r"\s+", "", line[1:])))
            nline += 1
        elif cur and line.startswith("-"):
            deleted[cur].append(re.sub(r"\s+", "", line[1:]))
    from collections import Counter
    for f, rows in added.items():
        pool = Counter(deleted[f])
        for ln, text in rows:
            if pool[text] > 0:
                pool[text] -= 1
            else:
                new[f].add(ln)
    return out, new


def strip_doc(node):
    node = ast.parse(ast.unparse(node)).body[0]
    for n in ast.walk(node):
        body = getattr(n, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                and isinstance(body[0].value.value, str):
            n.body = body[1:] or [ast.Pass()]
    return ast.dump(node)


# ---- names of builtin, standard-library and third-party types (by importing them here)
EXTERNAL = set()
import builtins, time, json, threading, asyncio, collections, contextlib, functools, itertools, logging, weakref
import uuid, math, enum, dataclasses, inspect, traceback, queue, concurrent.futures, typing, types, os as _os
import socket, atexit, signal, hashlib, random, struct, pickle, copy, heapq, bisect, operator, string, warnings, gc
import platform, secrets, contextvars
for t in (str, bytes, bytearray, dict, list, set, frozenset, tuple, int, float, bool, object, type, BaseException,
          Exception, types.FunctionType, types.MethodType, types.SimpleNamespace, memoryview, range, slice, property):
    EXTERNAL |= set(dir(t))
for m in (builtins, time, json, threading, asyncio, collections, contextlib, functools, itertools, logging, weakref, uuid,
          math, enum, dataclasses, inspect, traceback, queue, concurrent.futures, typing, _os, sys, socket, atexit,
          signal, hashlib, random, struct, pickle, copy, heapq, bisect, operator, string, warnings, gc, platform,
          secrets, contextvars, contextvars.ContextVar("x"), contextvars.copy_context()):
    EXTERNAL |= set(dir(m))
for obj in (threading.Thread, threading.Lock(), threading.Event(), threading.local(), collections.deque(),
            collections.OrderedDict(), collections.defaultdict(list), collections.Counter(), concurrent.futures.Future(),
            logging.getLogger("x"), logging.LogRecord("x", 1, "", 1, "", None, None), enum.Enum, queue.Queue(),
            contextlib.ExitStack(), asyncio.Future, asyncio.Task, asyncio.Queue, asyncio.AbstractEventLoop, asyncio.Event,
            types.CodeType, types.FrameType, BaseException().__traceback__ or BaseException):
    EXTERNAL |= set(dir(obj))
for modname, attrs in (("torch", ("Tensor", "device", "dtype", "cuda", "Event", "Stream", "distributed")),
                       ("msgspec", ("Struct",)), ("zmq", ("Socket", "Frame")), ("zmq.asyncio", ("Socket",)),
                       ("fastapi", ("Request", "FastAPI")), ("starlette.requests", ("Request",)),
                       ("starlette.responses", ("Response", "StreamingResponse")), ("pydantic", ("BaseModel",)),
                       ("numpy", ("ndarray",))):
    try:
        m = __import__(modname, fromlist=["x"])
        EXTERNAL |= set(dir(m))
        for a in attrs:
            EXTERNAL |= set(dir(getattr(m, a)))
    except Exception as e:  # noqa: BLE001
        print(f"note: could not import {modname} for type names: {type(e).__name__}")

D030, DPAT, D032 = Defs(V030), Defs(PATCHED), Defs(V032)
for d in (D030, DPAT, D032):
    d.resolve_stars()
ADDED, NEWCODE = added_lines(DIFF)
for f in NEW_FILES:
    ADDED[f] = NEWCODE[f] = set(range(1, 100000))
print("added lines:", sum(len(v) for v in ADDED.values()), " of them new code (not moved or re-indented):",
      sum(len(v) for k, v in NEWCODE.items() if k not in NEW_FILES), "+ the new files")

imports, attr_uses, getattr_uses, calls = [], defaultdict(list), defaultdict(list), defaultdict(list)
for rel, lines in sorted(ADDED.items()):
    tree = parse(PATCHED, rel)
    if tree is None:
        continue
    mod = rel[:-3].replace("/", ".")
    for n in ast.walk(tree):
        ln = getattr(n, "lineno", None)
        if ln not in lines:
            continue
        is_new = ln in NEWCODE.get(rel, ())
        if isinstance(n, ast.ImportFrom) and n.module:
            base = n.module
            if n.level:
                pkg = mod.split(".")[: -n.level] if not rel.endswith("__init__.py") else mod.split(".")[: -n.level + 1]
                base = ".".join(pkg + ([n.module] if n.module else []))
            for a in n.names:
                imports.append((rel, ln, base, a.name))
        elif isinstance(n, ast.Import):
            for a in n.names:
                imports.append((rel, ln, a.name, None))
        elif not is_new:
            continue
        elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
            if isinstance(n.value, ast.Name) and n.value.id == "diag":
                continue
            attr_uses[n.attr].append((rel, ln, ast.unparse(n)[:80]))
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("getattr", "hasattr", "setattr") \
                and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant) and isinstance(n.args[1].value, str):
            getattr_uses[n.args[1].value].append((rel, ln, ast.unparse(n)[:90]))
        if isinstance(n, ast.Call) and is_new:
            f = n.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if name and not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "diag"):
                calls[name].append((rel, ln, ast.unparse(f)[:60]))

print("== 1. imports in added code")
bad = 0
for rel, ln, module, name in imports:
    if not module.startswith("vllm"):
        continue
    names = DPAT.module_names.get(module)
    if names is None:
        print(f"  MISSING module {module} ({rel}:{ln})"); bad += 1; continue
    if name is None:
        continue
    sub = f"{module}.{name}" in DPAT.module_names
    if name not in names and not sub:
        print(f"  MISSING {module}.{name} ({rel}:{ln})"); bad += 1
    elif module in D030.module_names and name not in D030.module_names[module] and f"{module}.{name}" not in D030.module_names:
        print(f"  patch-defined {module}.{name} ({rel}:{ln})")
print("  unresolved:", bad, " imports from vllm in added code:", sum(1 for i in imports if i[2].startswith("vllm")))

PATCH_ONLY = {n for n in set(DPAT.attr_classes) | DPAT.attr_any if not D030.defined(n)}


def status(name):
    if D030.defined(name):
        return "v030"
    if name in PATCH_ONLY or DPAT.defined(name):
        return "patch"
    if name in EXTERNAL:
        return "external"
    return "MISSING"


print("== 2. attribute names in added code that are defined nowhere")
counts = defaultdict(int)
for name, uses in sorted(attr_uses.items()):
    s = status(name)
    counts[s] += 1
    if s == "MISSING":
        print(f"  MISSING .{name}: " + "; ".join(f"{r}:{l} {t}" for r, l, t in uses[:3]))
print("  attribute names:", dict(counts))
counts = defaultdict(int)
for name, uses in sorted(getattr_uses.items()):
    s = status(name)
    counts[s] += 1
    if s in ("MISSING", "external"):
        print(f"  getattr-literal {s} '{name}': " + "; ".join(f"{r}:{l} {t}" for r, l, t in uses[:3]))
print("  getattr/hasattr/setattr literals:", dict(counts))

print("== 3. names that a class defines in v0.32.0 but the same-named class lacks in v0.30.0")
n3 = 0
for name in sorted(set(attr_uses) | set(getattr_uses)):
    if status(name) != "v030":
        continue
    c32, c30 = D032.attr_classes.get(name, set()), D030.attr_classes.get(name, set())
    all30 = set().union(*[set(v) for v in D030.attr_classes.values()]) if False else None
    lacking = sorted(c for c in c32 - c30 if any(c in v for v in D030.attr_classes.values()))
    if lacking:
        n3 += 1
        uses = (attr_uses.get(name) or []) + (getattr_uses.get(name) or [])
        print(f"  .{name}: in v0.32.0 also on {lacking}; v0.30.0 on {sorted(c30)[:6]} | used: " +
              "; ".join(f"{r}:{l} {t}" for r, l, t in uses[:2]))
print("  items:", n3)

print("== 4. vLLM functions/methods called by added code whose definition differs between v0.30.0 and v0.32.0")
n4 = 0
for name, uses in sorted(calls.items()):
    if name in EXTERNAL and name not in D030.funcs:
        continue
    defs30 = {(r, q): node for r, q, node in D030.funcs.get(name, [])}
    defs32 = {(r, q): node for r, q, node in D032.funcs.get(name, [])}
    if not defs30:
        continue
    diff = [k for k in defs30 if k in defs32 and strip_doc(defs30[k]) != strip_doc(defs32[k])]
    gone = [k for k in defs30 if k not in defs32]
    if diff:
        n4 += 1
        print(f"  {name}(): differs at " + ", ".join(f"{r}::{q}" for r, q in diff[:4]) +
              (f" (+{len(diff) - 4})" if len(diff) > 4 else "") +
              f" | called: " + "; ".join(f"{r}:{l} {t}" for r, l, t in uses[:2]))
print("  items:", n4)

print("== external names (builtin, standard-library, third-party) used in added code")
print("  " + ", ".join(sorted(n for n in attr_uses if status(n) == "external")))


def fields(root):
    """(class name, field) -> annotation, for annotated class-level fields (dataclasses, msgspec structs, ...)."""
    out = {}
    for rel in py_files(root):
        tree = parse(root, rel)
        if tree is None:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.ClassDef):
                for item in n.body:
                    if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                        out[(n.name, item.target.id)] = ast.unparse(item.annotation)
    return out


print("== 5. fields read in new code whose annotation differs between v0.30.0 and v0.32.0 (same class name)")
F030, F032 = fields(V030), fields(V032)
used = set(attr_uses) | set(getattr_uses)
n5 = 0
for (cls, name), ann in sorted(F030.items()):
    if name in used and (cls, name) in F032 and F032[(cls, name)] != ann:
        n5 += 1
        print(f"  {cls}.{name}: v0.30.0 {ann} | v0.32.0 {F032[(cls, name)]}")
print("  items:", n5)
