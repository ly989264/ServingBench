"""msgspec.Struct classes (not covered by check_signatures.py): base fields must stay a prefix, in order; fields
appended must have defaults (array_like structs are encoded by position). AST only."""
import ast, sys
base, patched, files = sys.argv[1], sys.argv[2], sys.argv[3:]
def structs(path):
    out = {}
    for n in ast.walk(ast.parse(open(path).read())):
        if isinstance(n, ast.ClassDef):
            bases = [ast.unparse(b) for b in n.bases] + [ast.unparse(k.value) for k in n.keywords if k.arg == "metaclass"]
            if not any("Struct" in b for b in bases):
                continue
            kw = {k.arg: ast.unparse(k.value) for k in n.keywords}
            fields = [(s.target.id, s.value is not None) for s in n.body
                      if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
                      and not ast.unparse(s.annotation).startswith("ClassVar")]
            out[n.name] = (bases, kw, fields)
    return out
bad = 0
for f in files:
    b, p = structs(f"{base}/{f}"), structs(f"{patched}/{f}")
    for name, (bases, kw, fields) in b.items():
        if name not in p:
            print(f"BREAK {f}: {name} removed"); bad += 1; continue
        pb, pkw, pf = p[name]
        names, pnames = [x[0] for x in fields], [x[0] for x in pf]
        added = pf[len(fields):]
        status = "ok"
        if pnames[:len(names)] != names:
            status = "BREAK order"; bad += 1
        elif any(not d for _, d in added):
            status = "BREAK added without default"; bad += 1
        elif (bases, kw) != (pb, pkw):
            status = "REVIEW bases/options changed"
        print(f"{status} {f}: {name} {kw} base {len(names)} fields, appended {[n for n, _ in added]}")
print("BREAK items:", bad)
