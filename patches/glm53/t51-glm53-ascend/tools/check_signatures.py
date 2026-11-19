#!/usr/bin/env python3
"""Backward compatibility of the package's changed files: every function, method and dataclass / NamedTuple of the
public d4ad49796 file against the patched file, by AST (no imports).

usage: check_signatures.py BASE_TREE PATCHED_TREE FILE...      (FILE relative to the trees)

Per function present in the base file:
  - missing in the patched file                                   -> BREAK
  - a parameter added without a default, removed, renamed, moved,
    or a default / *args / **kwargs removed                        -> BREAK
  - a parameter added with a default (at the end of its kind)     -> ok (listed)
  - decorators added or removed                                    -> REVIEW (the wrapper must keep the signature and
                                                                      the return value)
  - the shapes of its return statements differ                    -> REVIEW (None, value, tuple arity, yield)
Per dataclass / NamedTuple: fields compared in order (positional construction) -> BREAK if a field without a default
is added, or fields are reordered or removed; a defaulted field appended -> ok.
dataclass(init=False): construction is the class's own __init__ (compared as a function); field list -> REVIEW.
Module-level names (functions, classes, assignments, imports) of the base file missing in the patched one -> BREAK.
Functions only in the patched file are listed as new (old callers cannot reach them).
"""

import ast
import sys
from pathlib import Path


def params(fn):
    a = fn.args
    pos = a.posonlyargs + a.args
    n_def = len(a.defaults)
    out = []
    for i, arg in enumerate(pos):
        out.append(("pos", arg.arg, i >= len(pos) - n_def))
    if a.vararg:
        out.append(("*", a.vararg.arg, True))
    for arg, default in zip(a.kwonlyargs, a.kw_defaults):
        out.append(("kw", arg.arg, default is not None))
    if a.kwarg:
        out.append(("**", a.kwarg.arg, True))
    return out


def return_shapes(fn):
    shapes = set()
    for node in ast.walk(fn):
        if node is not fn and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Return):
            v = node.value
            if v is None or (isinstance(v, ast.Constant) and v.value is None):
                shapes.add("None")
            elif isinstance(v, ast.Tuple):
                shapes.add(f"tuple{len(v.elts)}")
            else:
                shapes.add("value")
        elif isinstance(node, (ast.Yield, ast.YieldFrom)):
            shapes.add("yield")
    # nested functions are skipped by walk only at their own level; good enough for a review flag
    return shapes


def collect(tree):
    funcs, records = {}, {}

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = prefix + child.name
                funcs[q] = child
                visit(child, q + ".<locals>.")
            elif isinstance(child, ast.ClassDef):
                q = prefix + child.name
                decos = [ast.unparse(d) for d in child.decorator_list]
                bases = [ast.unparse(b) for b in child.bases]
                if any("dataclass" in d for d in decos) or "NamedTuple" in bases:
                    fields = [("<init=False>", True)] if any("init=False" in d for d in decos) else []
                    for stmt in child.body:
                        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                            ann = ast.unparse(stmt.annotation)
                            if ann.startswith("ClassVar"):
                                continue
                            fields.append((stmt.target.id, stmt.value is not None))
                    records[q] = fields
                visit(child, q + ".")

    visit(tree, "")
    return funcs, records


def module_names(src):
    names = set()
    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def compare(base_src, new_src):
    out = {"BREAK": [], "REVIEW": [], "ok": [], "new": []}
    bf, br = collect(ast.parse(base_src))
    nf, nr = collect(ast.parse(new_src))
    for q, fn in bf.items():
        if q not in nf:
            out["BREAK"].append(f"{q}: missing in the patched file")
            continue
        new = nf[q]
        old_p, new_p = params(fn), params(new)
        old_names = [(k, n) for k, n, _ in old_p]
        new_names = [(k, n) for k, n, _ in new_p]
        problems = []
        # every old parameter must still be there, same kind, same position among its kind, not losing a default
        for kind in ("pos", "*", "kw", "**"):
            o = [(n, d) for k, n, d in old_p if k == kind]
            m = [(n, d) for k, n, d in new_p if k == kind]
            if kind == "pos":
                if [n for n, _ in m[:len(o)]] != [n for n, _ in o]:
                    problems.append(f"positional parameters changed: {[n for n, _ in o]} -> {[n for n, _ in m]}")
                for (n, d), (n2, d2) in zip(o, m):
                    if d and not d2:
                        problems.append(f"default removed: {n}")
                for n, d in m[len(o):]:
                    if not d and not any(k == "*" for k, _, _ in old_p):
                        problems.append(f"new required positional parameter: {n}")
            elif kind == "kw":
                old_kw = dict(o)
                for n, d in m:
                    if n not in old_kw and not d and not any(k == "**" for k, _, _ in old_p):
                        problems.append(f"new required keyword-only parameter: {n}")
                    if n in old_kw and old_kw[n] and not d:
                        problems.append(f"default removed: {n}")
                for n in old_kw:
                    if n not in dict(m):
                        problems.append(f"keyword-only parameter removed: {n}")
            elif o and not m:
                problems.append(f"{kind}{o[0][0]} removed")
        added = [n for k, n, d in new_p if (k, n) not in old_names]
        if problems:
            out["BREAK"].append(f"{q}: " + "; ".join(problems))
        elif added:
            out["ok"].append(f"{q}: added with defaults: {added}")
        od = [ast.unparse(d) for d in fn.decorator_list]
        nd = [ast.unparse(d) for d in new.decorator_list]
        if od != nd:
            out["REVIEW"].append(f"{q}: decorators {od} -> {nd}")
        rs_o, rs_n = return_shapes(fn), return_shapes(new)
        if rs_o != rs_n:
            out["REVIEW"].append(f"{q}: return shapes {sorted(rs_o)} -> {sorted(rs_n)}")
    for q in nf:
        if q not in bf:
            out["new"].append(q)
    gone = sorted(module_names(base_src) - module_names(new_src))
    if gone:
        out["BREAK"].append(f"module-level names removed: {gone}")
    for q, fields in br.items():
        if q not in nr:
            out["BREAK"].append(f"{q}: record class missing")
            continue
        new_fields = nr[q]
        names_o, names_n = [n for n, _ in fields], [n for n, _ in new_fields]
        if names_o[:1] == ["<init=False>"]:
            # construction goes through the class's own __init__ (compared above); field order only affects repr
            if names_o != names_n:
                out["REVIEW"].append(f"{q}: dataclass(init=False) field list changed (construction is via __init__): "
                                     f"+{[n for n in names_n if n not in names_o]} "
                                     f"-{[n for n in names_o if n not in names_n]}")
            continue
        if names_n[:len(names_o)] != names_o:
            out["BREAK"].append(f"{q}: fields reordered or removed: {names_o} -> {names_n}")
        else:
            extra = new_fields[len(fields):]
            if any(not d for _, d in extra):
                out["BREAK"].append(f"{q}: new field without default: {[n for n, d in extra if not d]}")
            elif extra:
                out["ok"].append(f"{q}: fields appended with defaults: {[n for n, _ in extra]}")
    return out, sorted(set(bf) & set(nf)), bf, nf


def main():
    base, new, files = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:]
    total_break = 0
    for f in files:
        new_src = (new / f).read_text()
        if not (base / f).exists():
            nf, _ = collect(ast.parse(new_src))
            print(f"== {f}: new file ({len(nf)} functions), no old callers")
            continue
        base_src = (base / f).read_text()
        out, common, bf, nf = compare(base_src, new_src)
        changed = [q for q in common if ast.dump(bf[q]) != ast.dump(nf[q])]
        print(f"== {f}: {len(changed)} changed of {len(common)} functions; {len(out['new'])} new")
        for q in changed:
            print(f"   changed: {q}{'' if params(bf[q]) == params(nf[q]) else '  (signature differs)'}")
        for kind in ("BREAK", "REVIEW", "ok"):
            for line in out[kind]:
                print(f"   {kind}: {line}")
        for q in out["new"]:
            print(f"   new: {q}")
        total_break += len(out["BREAK"])
    print(f"\nBREAK items: {total_break}")
    return 1 if total_break else 0


if __name__ == "__main__":
    sys.exit(main())
