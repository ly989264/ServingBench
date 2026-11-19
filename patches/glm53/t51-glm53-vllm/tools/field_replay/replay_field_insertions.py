"""Reverse replay: move a field patch's inserted statements into the vLLM port's function.

A field patch replaces e.g. ``AsyncLLM.add_request`` with a copy of vLLM v0.30.0's original
function plus a few added statements. Patch 01 rewrote that function (the whole body moved
into ``with _api_submission(...)``), so 01 cannot be replayed into the copy line by line.
This tool goes the other way: it finds the copy's additions relative to the original, inserts
them into the port's version of the function next to the same anchor statements, and writes
the result over the copy in the field file.

Pure standard library (Python 3.10+). Nothing in the three files is imported or executed.

usage:
  python3 replay_field_insertions.py --field FIELD.py --field-func add_request \
      --original ORIGINAL/async_llm.py --port PORT/async_llm.py --qualname AsyncLLM.add_request \
      [--port-module vllm.v1.engine.async_llm] [--write OUT.py] [--report REPORT.json]

Exit codes: 0 all checks passed (merged file written if --write); 2 refused, nothing written;
3 input error. The report has counts and hashes only, never source text.

Checks (all must pass):
  C1 every anchor matches exactly one statement in the original and exactly one in the port;
  C2 the field function is the original plus the insertions: same signature, no original
     statement removed or changed;
  C3 the merged field file compiles and every name the merged function uses resolves (in the
     field module, or imported from the port module by one added local import);
  C4 the merged function is the port's function plus exactly the field's insertions, each
     next to its anchor (plus that one import, if it was needed).
"""

import argparse
import ast
import builtins
import hashlib
import io
import json
import sys
import tokenize

BLOCK_FIELDS = ("body", "orelse", "finalbody")


class Refuse(Exception):
    pass


def fp(node):
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def h16(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def sub_bodies(node):
    """(label, statement list) for every nested block of a compound statement, in order."""
    out = []
    for name in BLOCK_FIELDS:
        value = getattr(node, name, None)
        if isinstance(value, list) and (not value or isinstance(value[0], ast.stmt)):
            out.append((name, value))
    for i, handler in enumerate(getattr(node, "handlers", None) or []):
        out.append((f"handlers[{i}]:{fp(handler.type) if handler.type else ''}:{handler.name}", handler.body))
    for i, case in enumerate(getattr(node, "cases", None) or []):
        out.append((f"cases[{i}]:{fp(case.pattern)}:{fp(case.guard) if case.guard else ''}", case.body))
    return out


def header_fp(node):
    """Fingerprint of a compound statement without its nested blocks."""
    parts = [type(node).__name__]
    for name, value in ast.iter_fields(node):
        if name in BLOCK_FIELDS or name in ("handlers", "cases"):
            continue
        if isinstance(value, ast.AST):
            parts.append(f"{name}={fp(value)}")
        elif isinstance(value, list):
            parts.append(f"{name}=[{','.join(fp(x) if isinstance(x, ast.AST) else repr(x) for x in value)}]")
        else:
            parts.append(f"{name}={value!r}")
    return "|".join(parts)


def path_str(path):
    return "/".join(f"{i}.{label}" for i, label in path) or "<function body>"


def align(orig, new, path):
    """Insert-only alignment of two statement lists.

    Returns [(path, after_index, [new statements])]; after_index -1 means before the first
    statement of that block. Raises Refuse when an original statement is missing or changed."""
    insertions, pending = [], []
    i = j = 0
    while i < len(new):
        stmt = new[i]
        matched = False
        if j < len(orig):
            if fp(stmt) == fp(orig[j]):
                matched, nested = True, []
            elif type(stmt) is type(orig[j]) and sub_bodies(stmt) and header_fp(stmt) == header_fp(orig[j]):
                nb, ob = sub_bodies(stmt), sub_bodies(orig[j])
                if [lab for lab, _ in nb] == [lab for lab, _ in ob]:
                    try:
                        nested = []
                        for (label, nbody), (_, obody) in zip(nb, ob):
                            nested += align(obody, nbody, path + [(j, label)])
                        matched = True
                    except Refuse:
                        matched = False
        if matched:
            if pending:
                insertions.append((path, j - 1, pending))
                pending = []
            insertions += nested
            i += 1
            j += 1
        else:
            pending.append(stmt)
            i += 1
    if j != len(orig):
        raise Refuse(f"C2: original statement {j} of {path_str(path)} is missing or changed in the copy")
    if pending:
        insertions.append((path, j - 1, pending))
    return insertions


def body_at(func, path):
    body = func.body
    for index, label in path:
        body = dict(sub_bodies(body[index]))[label]
    return body


def all_statements(func):
    out = []

    def walk(body):
        for stmt in body:
            out.append(stmt)
            for _, sub in sub_bodies(stmt):
                walk(sub)

    walk(func.body)
    return out


def find_qualname(tree, qualname):
    nodes, found = tree.body, None
    for name in qualname.split("."):
        found = next((n for n in nodes if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                      and n.name == name), None)
        if found is None:
            return None
        nodes = found.body
    return found if isinstance(found, (ast.FunctionDef, ast.AsyncFunctionDef)) else None


def defs_named(tree, name):
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]


def multiline_string_lines(source):
    """1-based numbers of the continuation lines of multi-line string tokens."""
    lines = set()
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.STRING and tok.end[0] > tok.start[0]:
            lines.update(range(tok.start[0] + 1, tok.end[0] + 1))
    return lines


def shift(line, delta):
    if not line.strip():
        return line
    lead = len(line) - len(line.lstrip(" "))
    if lead + delta < 0:
        raise Refuse("C3: indentation would become negative while re-indenting")
    return " " * (lead + delta) + line.lstrip(" ")


def top_level_names(tree):
    names = set()
    for n in tree.body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in n.names)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for t in n.targets if isinstance(n, ast.Assign) else [n.target]:
                names.update(x.id for x in ast.walk(t) if isinstance(x, ast.Name))
        elif isinstance(n, (ast.If, ast.Try)):  # e.g. guarded imports
            names.update(x.id for x in ast.walk(n) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store))
            for x in ast.walk(n):
                if isinstance(x, (ast.Import, ast.ImportFrom)):
                    names.update((a.asname or a.name).split(".")[0] for a in x.names)
    return names


def enclosing_names(tree, func):
    """Names bound in the functions that enclose func (closures)."""
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not func and any(c is func for c in ast.walk(n)):
            names.update(a.arg for a in n.args.args + n.args.kwonlyargs + n.args.posonlyargs)
            for c in ast.walk(n):
                if isinstance(c, ast.Name) and isinstance(c.ctx, ast.Store):
                    names.add(c.id)
                elif isinstance(c, (ast.Import, ast.ImportFrom)):
                    names.update((a.asname or a.name).split(".")[0] for a in c.names)
                elif isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(c.name)
    return names


def free_names(func):
    a = func.args
    local = {x.arg for x in a.args + a.kwonlyargs + a.posonlyargs}
    local.update(x.arg for x in (a.vararg, a.kwarg) if x)
    for n in ast.walk(func):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            local.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            local.update((x.asname or x.name).split(".")[0] for x in n.names)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            local.add(n.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n is not func:
            local.add(n.name)
    used = {n.id for n in ast.walk(func) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return sorted(used - local - set(dir(builtins)))


def merge(args, report):
    field_src, orig_src, port_src = (open(p).read() for p in (args.field, args.original, args.port))
    for label, text in (("field", field_src), ("original", orig_src), ("port", port_src)):
        report[f"{label}_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    field_tree, orig_tree, port_tree = ast.parse(field_src), ast.parse(orig_src), ast.parse(port_src)
    O, P = find_qualname(orig_tree, args.qualname), find_qualname(port_tree, args.qualname)
    hits = defs_named(field_tree, args.field_func)
    if O is None or P is None or len(hits) != 1:
        raise ValueError(f"definitions found: original={O is not None} port={P is not None} field={len(hits)}")
    F = hits[0]

    # C2 -- the copy is the original plus insertions.
    if type(F) is not type(O) or fp(F.args) != fp(O.args):
        raise Refuse("C2: the field function's kind or signature differs from the original")
    insertions = align(O.body, F.body, [])
    if not insertions:
        raise Refuse("C2: the field function has no insertions (it equals the original)")
    report["checks"]["C2_insert_only"] = "pass"

    # C1 -- each anchor is unique in the original and in the port.
    o_all, p_all = all_statements(O), all_statements(P)
    plan = []
    for path, after, stmts in insertions:
        body = body_at(O, path)
        mode, anchor = ("after", body[after]) if after >= 0 else ("before", body[0])
        in_o = sum(fp(s) == fp(anchor) for s in o_all)
        p_hits = [s for s in p_all if fp(s) == fp(anchor)]
        entry = {
            "block": path_str(path),
            "position": mode,
            "after_statement_index": after,
            "anchor_sha16": h16(fp(anchor)),
            "inserted_statements": len(stmts),
            "inserted_sha16": h16("".join(fp(s) for s in stmts)),
            "anchor_matches_in_original": in_o,
            "anchor_matches_in_port": len(p_hits),
        }
        report["insertions"].append(entry)
        if in_o != 1 or len(p_hits) != 1:
            raise Refuse(f"C1: anchor {entry['anchor_sha16']} matches {in_o} time(s) in the original and "
                         f"{len(p_hits)} time(s) in the port")
        plan.append((mode, p_hits[0], stmts))
    report["checks"]["C1_anchors_unique"] = "pass"

    # Merge as text: the port's function, with the field's statements spliced in.
    port_lines = port_src.split("\n")
    field_lines = field_src.split("\n")
    port_protect = multiline_string_lines(port_src)
    field_protect = multiline_string_lines(field_src)
    p_start = min([P.lineno] + [d.lineno for d in P.decorator_list])
    lines = [(port_lines[n - 1], n in port_protect) for n in range(p_start, P.end_lineno + 1)]
    splices = []
    for mode, anchor, stmts in plan:
        first, last = stmts[0].lineno, stmts[-1].end_lineno
        if any(n in field_protect for n in range(first, last + 1)):
            raise Refuse("C3: an inserted statement contains a multi-line string")
        delta = anchor.col_offset - stmts[0].col_offset
        block = [(shift(field_lines[n - 1], delta), False) for n in range(first, last + 1)]
        at = (anchor.end_lineno if mode == "after" else anchor.lineno - 1) - p_start + 1
        splices.append((at, block))
    for at, block in sorted(splices, key=lambda s: s[0], reverse=True):
        lines[at:at] = block
    header_len = P.body[0].lineno - p_start
    body = lines[header_len:]

    # Re-indent the body from the port method's column to the field function's column.
    delta = F.body[0].col_offset - P.body[0].col_offset
    body = [line if protected else shift(line, delta) for line, protected in body]

    f_start = min([F.lineno] + [d.lineno for d in F.decorator_list])
    header = field_lines[f_start - 1:F.body[0].lineno - 1]

    def assemble(body_lines):
        return "\n".join(field_lines[:f_start - 1] + header + body_lines + field_lines[F.end_lineno:])

    merged_src = assemble(body)
    try:
        merged_tree = ast.parse(merged_src)
    except SyntaxError as exc:
        raise Refuse(f"C3: merged file does not parse: {exc.msg} (line {exc.lineno})")
    M = defs_named(merged_tree, args.field_func)[0]

    # C3 -- names: anything the field module lacks comes from the port module, via one local import.
    available = top_level_names(field_tree) | enclosing_names(field_tree, F)
    missing = [n for n in free_names(M) if n not in available]
    unresolved = [n for n in missing if n not in top_level_names(port_tree)]
    if unresolved:
        raise Refuse(f"C3: {len(unresolved)} name(s) resolve neither in the field module nor in the port module")
    report["names_imported_from_port_module"] = missing
    if missing:
        doc = P.body[0]
        after_doc = (doc.end_lineno - doc.lineno + 1) if (
            isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant) and isinstance(doc.value.value, str)) else 0
        indent = " " * F.body[0].col_offset
        body = body[:after_doc] + [f"{indent}from {args.port_module} import {', '.join(missing)}"] + body[after_doc:]
        merged_src = assemble(body)
    try:
        compile(merged_src, args.field, "exec")
    except SyntaxError as exc:
        raise Refuse(f"C3: merged file does not compile: {exc.msg} (line {exc.lineno})")
    report["checks"]["C3_compiles_and_names_resolve"] = "pass"

    # C4 -- merged function == port function + the insertions, each next to its anchor.
    M = defs_named(ast.parse(merged_src), args.field_func)[0]
    if fp(M.args) != fp(P.args):
        raise Refuse("C4: the merged function's signature differs from the port's")

    def is_added_import(s):
        return isinstance(s, ast.ImportFrom) and s.module == args.port_module and missing and \
            sorted(a.name for a in s.names) == sorted(missing)

    got = []
    for path, after, stmts in align(P.body, M.body, []):
        kept = [s for s in stmts if not is_added_import(s)]
        if kept:
            got.append((path, after, kept))
    if sorted(fp(s) for _, _, ss in got for s in ss) != sorted(fp(s) for _, _, ss in insertions for s in ss):
        raise Refuse("C4: the merged function is not the port's function plus exactly the field's insertions")
    for mode, anchor, stmts in plan:
        want = [fp(s) for s in stmts]
        ok = False
        for path, after, ss in got:
            if [fp(s) for s in ss] != want:
                continue
            block = body_at(P, path)
            if mode == "after" and after >= 0 and fp(block[after]) == fp(anchor):
                ok = True
            if mode == "before" and after == -1 and block and fp(block[0]) == fp(anchor):
                ok = True
        if not ok:
            raise Refuse("C4: an insertion did not land next to its anchor")
    report["checks"]["C4_port_plus_insertions"] = "pass"
    return merged_src


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--field", required=True)
    ap.add_argument("--field-func", required=True)
    ap.add_argument("--original", required=True)
    ap.add_argument("--port", required=True)
    ap.add_argument("--qualname", required=True)
    ap.add_argument("--port-module", default="vllm.v1.engine.async_llm")
    ap.add_argument("--write")
    ap.add_argument("--report")
    args = ap.parse_args()
    report = {"tool": "replay_field_insertions", "version": 1, "checks": {}, "insertions": []}
    code, merged = 0, None
    try:
        merged = merge(args, report)
        report["result"] = "merged"
    except Refuse as exc:
        code, report["result"], report["reason"] = 2, "refused", str(exc)
    except (ValueError, OSError, SyntaxError) as exc:
        code, report["result"], report["reason"] = 3, "error", f"{type(exc).__name__}: {exc}"
    if code == 0 and args.write:
        with open(args.write, "w") as f:
            f.write(merged)
        report["written_sha256"] = hashlib.sha256(merged.encode()).hexdigest()
    text = json.dumps(report, indent=1, sort_keys=True)
    if args.report:
        with open(args.report, "w") as f:
            f.write(text + "\n")
    print(text)
    sys.exit(code)


if __name__ == "__main__":
    main()
