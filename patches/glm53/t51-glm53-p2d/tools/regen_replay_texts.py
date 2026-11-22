"""Fill the generated part of replay_p2d.py from the replacement file, so the replay can only insert what the
patch inserts. Each non-equal block of difflib(original, replacement) is one change, in file order (C01..C14, C29, C15..C28);
its text is the block with the indentation the replay will add removed (from the primary anchor on the
original). Then BASE_FUNCTION_LINES and PATCHED_SHA256 are filled in.

Usage: python3 regen_replay_texts.py <original.py> <replacement.py> <replay_p2d.py>
"""

import difflib
import hashlib
import importlib.util
import re
import sys

original_path, replacement_path, replay_path = sys.argv[1:4]
original = open(original_path).read()
replacement = open(replacement_path).read()
a, b = original.split("\n"), replacement.split("\n")
blocks = [(tag, i1, i2, j1, j2) for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
          if tag != "equal"]

NAMES = ["C01", "TEXT_HELPERS", None, "TEXT_RECEIVER_STATE", "TEXT_ADD_REQUEST_PARAMS", "TEXT_ADD_REQUEST_DIAG",
         "TEXT_SUBMIT", "TEXT_HANDLE_WRAPPER", "TEXT_SKIPPED", "TEXT_TRANSFER_OUTCOME", "TEXT_TRANSFER_EXCEPTION",
         "TEXT_TRACKER", "TEXT_REFORMAT_STAGE", "TEXT_REFORMAT_FAILURE", "TEXT_REQUEST_DONE", "TEXT_RECEIVER_METHODS",
         "TEXT_PREFIX_HIT",
         "TEXT_META_BEGIN", "TEXT_META_REQUEST", "TEXT_META_END", "TEXT_PREPARE_BEGIN", "TEXT_PREPARE_END",
         "TEXT_NO_WORK", None, "TEXT_CONSUMER_LINK", "TEXT_PRODUCER_OFFER", "C26", "TEXT_KWARGS", "C28"]
assert len(blocks) == len(NAMES) == 29, len(blocks)

src = open(replay_path).read()
begin, end = "# --- BEGIN GENERATED TEXTS", "# --- END GENERATED TEXTS ---"
head, rest = src.split(begin, 1)
_, tail = rest.split(end, 1)
# load the engine with empty texts to reuse its locating code
stub = "\n".join(f"{n} = ''" for n in NAMES if n and n.startswith("TEXT_"))
spec_src = head + begin + " ---\n" + stub + "\n" + end + tail
module = type(sys)("replay_engine")
module.__file__ = replay_path
exec(compile(spec_src, replay_path, "exec"), module.__dict__)

out = []
work = list(a)  # the original with C01..Ck-1 applied, as the replay sees it when it reaches Ck
for name, (tag, i1, i2, j1, j2), change in zip(NAMES, blocks, module.CHANGES):
    new = b[j1:j2]
    if name is not None and name.startswith("TEXT_"):
        alt = change["alts"][0]
        lo, hi = module.scope_range(work, change["function"])
        index = module.locate(work, lo, hi, alt)
        base = max(0, module.indent_of(work[index]) + alt.get("indent", 0))
        if alt["op"] == "before":
            # difflib may pair the blank line before the insertion point with the block's last blank line;
            # the inserted text is the same either way once the leading blanks are moved to the end
            while new and not new[0].strip():
                new = new[1:] + [""]
        for line in new:
            assert not line.strip() or line.startswith(" " * base), (name, line)
        text = "\n".join(line[base:] if line.strip() else "" for line in new)
        if name in ("TEXT_TRANSFER_EXCEPTION", "TEXT_REFORMAT_FAILURE"):
            assert text.count("type(e).__name__") == 1, name
            text = text.replace("type(e).__name__", "type({exc}).__name__")
        if name == "TEXT_REFORMAT_STAGE":
            assert tag == "replace" and a[i1].strip() == "self._reformat_pending_kv_caches(request_id)"
            text = text.replace("(req_meta, request_id):", "(req_meta, {arg}):").replace(
                "    self._reformat_pending_kv_caches(request_id)", "    {line}")
        if name == "TEXT_META_REQUEST":
            assert tag == "replace"
            text = text.replace("    self._get_remote_metadata(remote_host, remote_handshake_port)", "    {line}")
        assert "\'\'\'" not in text and "\\" not in text, name
        out.append(f"{name} = \'\'\'{text}\'\'\'\n")
        change["text"] = text
    module.apply_change(work, change)
if "\n".join(work) != replacement:
    diff = difflib.unified_diff(replacement.split("\n"), work, "replacement", "sequential", lineterm="", n=2)
    print("\n".join(list(diff)[:60]))
    raise SystemExit("sequential apply does not reproduce the replacement file")
generated = begin + " (tools/regen_replay_texts.py, from the replacement file) ---\n" + "\n".join(out) + end
src = head + generated + tail
exec(compile(src, replay_path, "exec"), module.__dict__)
lines_map = module.compute_facts(original)["functions.lines"]
src = re.sub(r'BASE_FUNCTION_LINES = .*\n', "BASE_FUNCTION_LINES = " + repr(lines_map) + "\n", src, count=1)
src = re.sub(r'PATCHED_SHA256 = ".*"\n', 'PATCHED_SHA256 = "' + hashlib.sha256(replacement.encode()).hexdigest() + '"\n', src, count=1)
open(replay_path, "w").write(src)
print(f"regenerated {len(out)} texts; base functions {len(lines_map)}")
