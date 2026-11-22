"""Lab cases for field_check/span_diff.py, standard library only:
`python3 -I -B test_span_diff.py <span_diff.py> <replay_p2d.py> <original.py> <t51 replacement.py> <t56 replacement.py> <outdir>
[<t58 replacement.py>]`.

1. The original (spans=anchors) and every replacement file (spans=instrumented) report no difference, and the
   stage boundaries found from the stage calls are the ones the anchors give.
2. Lines a site might add inside meta_fetch, inside prepare, before, between and after the stages, and a removed
   line, are each reported in their region, on the unpatched file and, identically, on its T56 replay; with the
   C17 anchor comment removed, the boundary comes from the replay's alternative anchor.
3. A function that is missing gives "missing" and exit code 2.
"""

import importlib.util
import os
import subprocess
import sys

tool_path, replay, original_path, t51_path, t56_path, outdir = sys.argv[1:7]
t58_path = sys.argv[7] if len(sys.argv) > 7 else None
spec = importlib.util.spec_from_file_location("span_diff", tool_path)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)
original = open(original_path).read()
os.makedirs(outdir, exist_ok=True)
failures = []


def expect(cond, label, detail=""):
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        failures.append(label)
        if detail:
            print("     " + detail)


def fields(summary):
    head = summary.split("\n", 1)[0].split()
    return dict(item.split("=", 1) for item in head[1:])


def counts(summary):
    f = fields(summary)
    return {k: f[k] for k in tool.REGIONS} | {"added": f["added"], "removed": f["removed"], "added_at": f["added_at"]}


def replayed(source, name):
    path, out = os.path.join(outdir, f"in_{name}.py"), os.path.join(outdir, f"out_{name}.py")
    with open(path, "w") as handle:
        handle.write(source)
    proc = subprocess.run([sys.executable, "-I", replay, path, "--out", out], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return open(out).read()


def edit(source, anchor, block, after=True):
    lines = source.split("\n")
    hits = [i for i, line in enumerate(lines) if line.strip() == anchor]
    assert len(hits) == 1, (anchor, len(hits))
    i = hits[0] + (1 if after else 0)
    indent = " " * (len(lines[hits[0]]) - len(lines[hits[0]].lstrip()))
    lines[i:i] = [indent + text for text in block]
    return "\n".join(lines)


ZERO = {k: "+0/-0" for k in tool.REGIONS} | {"added": "0", "removed": "0", "added_at": "none"}

# 1
base = tool.span_diff(original)
expect(fields(base)["spans"] == "anchors" and counts(base) == ZERO and fields(base)["func_lines"] == "284",
       "original: spans=anchors, no difference, 284 lines", base)
anchor_bounds = tool.bounds_by_anchor(tool.normalise(tool.function_lines(original), False)[0])
for label, path in (("T51 replacement", t51_path), ("T56 replacement", t56_path)) + ((("T58 replacement", t58_path),) if t58_path else ()):
    text = open(path).read()
    summary = tool.span_diff(text)
    _, bounds = tool.normalise(tool.function_lines(text), True)
    expect(fields(summary)["spans"] == "instrumented" and counts(summary) == ZERO and bounds == anchor_bounds,
           f"{label}: spans=instrumented, no difference, boundaries equal the anchors'", summary)

# 2
CASES = [
    ("meta_fetch", "remote_transfer_port = self.remote_te_port[remote_engine_id][remote_handshake_port]",
     ["site_meta_note = remote_transfer_port"], True, {"meta_fetch": "+1/-0"}),
    ("prepare", "dst_list: list[int] = []", ["site_list_a: list[int] = []", "site_list_b: list[int] = []",
     "site_list_c: list[int] = []", "site_list_d: list[int] = []", "site_list_e: list[int] = []",
     "site_list_f: list[int] = []", "site_list_g: list[int] = []", "site_list_h: list[int] = []"], True,
     {"prepare": "+8/-0"}),
    ("before", "# Check if we have the remote metadata cached.", ["site_before = None"], False, {"before": "+1/-0"}),
    ("between", "remote_layer_name_to_idx = build_layer_name_to_metadata_idx(remote_kv_group2layeridx)",
     ["site_between = None"], True, {"between": "+1/-0"}),
    ("after", "req_end_time = time.perf_counter()", ["site_after = req_end_time"], True, {"after": "+1/-0"}),
]
for name, anchor, block, after, want in CASES:
    edited = edit(original, anchor, block, after)
    plain = tool.span_diff(edited)
    patched = tool.span_diff(replayed(edited, name))
    expected = dict(ZERO, **want)
    got = counts(plain)
    expected["added"] = str(len(block))
    expected["added_at"] = got["added_at"]
    expect(got == expected and counts(patched) == got and got["added_at"].count(name + ":") == len(block),
           f"{len(block)} line(s) added in {name}: reported there, same on the unpatched file and on its replay",
           plain + " | " + patched)
removed = original.replace("        attention_group_reformat_block_ids: list[tuple[tuple[int, list[list[int]], int, list[int]], bool]] = []\n",
                           "", 1)
assert removed != original
plain, patched = tool.span_diff(removed), tool.span_diff(replayed(removed, "removed"))
expect(counts(plain) == dict(ZERO, prepare="+0/-1", removed="1") and counts(patched) == counts(plain),
       "a line removed in prepare: prepare=+0/-1, same after the replay", plain + " | " + patched)
# the C17 anchor comment gone, as in the replay's scenario C: the boundary comes from the replay's alternative
no_comment = original.replace("        # Check if we have the remote metadata cached.\n", "", 1)
assert no_comment != original
no_comment = edit(no_comment, "remote_transfer_port = self.remote_te_port[remote_engine_id][remote_handshake_port]",
                  ["site_meta_note = remote_transfer_port"])
plain, patched = tool.span_diff(no_comment), tool.span_diff(replayed(no_comment, "no_comment"))
expect(fields(plain)["spans"] == "anchors" and counts(plain)["meta_fetch"] == "+1/-0" and counts(plain)["before"] == "+0/-1"
       and counts(patched) == counts(plain),
       "C17's comment removed: boundary from the alternative anchor, same as on the replay", plain + " | " + patched)
shown = tool.span_diff(edit(original, "dst_list: list[int] = []", ["site_list_a: list[int] = []"]), show=True)
expect(shown.split("\n")[1:] == ["+ prepare: site_list_a: list[int] = []"], "--show prints the added line with its region", shown)

# 3
with open(os.path.join(outdir, "missing.py"), "w") as handle:
    handle.write("class KVCacheRecvingThread:\n    pass\n")
proc = subprocess.run([sys.executable, "-I", "-S", "-B", tool_path, os.path.join(outdir, "missing.py")],
                      capture_output=True, text=True)
expect(proc.returncode == 2 and proc.stdout.startswith("p2d_span_diff missing:"), "function missing: exit 2", proc.stdout)

print(f"{len(failures)} failed")
sys.exit(1 if failures else 0)
