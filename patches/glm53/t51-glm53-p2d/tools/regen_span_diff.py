"""Fill the generated block of field_check/span_diff.py from the d4ad49796 original and the replacement file.

Usage: python3 -I regen_span_diff.py <original.py> <replacement.py> <span_diff.py>
REF_LINES: sha16 of the original function's non-blank stripped lines; REF_BOUNDS: the stage boundaries on them
(primary anchors C17, C19, C20, C21); PATCH_LINES: lines of the replacement's function that the original's does
not have (the inserted lines, the stage calls included); the rewritten READ line is read back, not listed.
"""

import importlib.util
import sys

original_path, replacement_path, tool_path = sys.argv[1:4]
spec = importlib.util.spec_from_file_location("span_diff", tool_path)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)
original = open(original_path).read()
replacement = open(replacement_path).read()
ref, _ = tool.normalise(tool.function_lines(original), patched=False)
ref_bounds = tool.bounds_by_anchor(ref)
assert ref_bounds is not None
ref_set = {tool.line_hash(t) for t in ref}
new_raw = tool.function_lines(replacement)
inserted = sorted({tool.line_hash(r.strip()) for r in new_raw if r.strip() and tool.line_hash(r.strip()) not in ref_set
                   and not tool.READ_BACK.search(r.strip())})
src = open(tool_path).read()
begin, end = "# --- BEGIN GENERATED", "# --- END GENERATED ---"
head, rest = src.split(begin, 1)
_, tail = rest.split(end, 1)
block = (begin + " (tools/regen_span_diff.py, from the d4ad49796 original and the T56 replacement) ---\n"
         + "REF_LINES = " + repr([tool.line_hash(t) for t in ref]) + "\n"
         + "REF_BOUNDS = " + repr(ref_bounds) + "\n"
         + "PATCH_LINES = {" + ", ".join(repr(h) for h in inserted) + "}\n" + end)
open(tool_path, "w").write(head + block + tail)
print(f"ref_lines={len(ref)} bounds={ref_bounds} patch_lines={len(inserted)}")
