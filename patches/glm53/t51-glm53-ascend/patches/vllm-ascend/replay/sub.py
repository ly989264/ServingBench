"""Exact, count-checked text substitutions: sub.py FILE SPEC.py ; SPEC defines EDITS=[(old,new,count)]."""
import sys, runpy
path, spec = sys.argv[1], sys.argv[2]
edits = runpy.run_path(spec)["EDITS"]
text = open(path).read()
for i, (old, new, count) in enumerate(edits):
    n = text.count(old)
    if n != count:
        sys.exit(f"edit {i}: expected {count} occurrence(s), found {n}: {old[:80]!r}")
    text = text.replace(old, new)
open(path, "w").write(text)
print(f"{path}: {len(edits)} edits applied")
