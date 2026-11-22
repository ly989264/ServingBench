#!/usr/bin/env python3
"""Where the site's own changes to _transfer_kv_cache_all_groups lie relative to the two timed stages.

    python3 -I -S -B span_diff.py <site mooncake_connector.py> [--show]

Read-only, standard library only (Python 3.9+), no imports of vLLM or vllm-ascend. Works on the site original and
on a file already replayed with the p2d patch (T51 or T56): the lines the patch inserts are recognised and left out,
and the READ line it rewrites is read back as the original call.

The function is compared, non-blank line by non-blank line (white space at both ends removed), with the vllm-ascend
d4ad49796 original (REF_LINES: sha256 of each line, first 16 hex digits). Every line the site added or removed is
put in one of five regions of the function:

    before      from the start of the function to the start of meta_fetch
    meta_fetch  "取对端信息": the meta_fetch stage (associated_stages.meta_fetch)
    between     from the end of meta_fetch to the start of prepare
    prepare     "组读表": the prepare stage (associated_stages.prepare)
    after       from the end of prepare to the end of the function (the READ and what follows)

Stage boundaries: on a patched file, the _pd_stage_begin / _pd_stage_end lines themselves (spans=instrumented);
on an unpatched file, the anchors of replay changes C17, C19, C20, C21, primary first and then the replay's
alternatives (spans=anchors). Removed lines are placed by the original's boundaries.

Output, one line: p2d_span_diff spans=... func_lines=N ref_lines=M added=A removed=R before=+a/-r
meta_fetch=+a/-r between=+a/-r prepare=+a/-r after=+a/-r added_at=<region>:<sha16 of the line>,...
(func_lines: lines of the function in the input, blank lines included, 284 in d4ad49796; ref_lines: non-blank lines
of the d4ad49796 function.)
With --show, the added and removed lines are printed after it, one per line, prefixed by "+ <region>" / "- <region>".
Exit codes: 0 printed, 1 usage or I/O, 2 the function or a stage boundary was not found.
"""

import ast
import difflib
import hashlib
import re
import sys

FUNCTION = "KVCacheRecvingThread._transfer_kv_cache_all_groups"
MARKER = "from vllm.v1.metrics import pd_diagnostics as diag"
REGIONS = ("before", "meta_fetch", "between", "prepare", "after")
INSTRUMENTED = (
    '_pd_stage_begin(req_meta, "meta_fetch")',
    '_pd_stage_end(req_meta, "meta_fetch", failed=False)',
    '_pd_stage_begin(req_meta, "prepare")',
    '_pd_stage_end(req_meta, "prepare", failed=False)',
)
# Per boundary, the anchors of the replay in the same order (primary first): (match, anchor, where, walk back to).
# where: the boundary is the anchor line itself ("at") or the line after it ("after"); walk: for C17's alternative,
# the boundary is the nearest line above that equals this text.
ANCHORS = (
    (("equal", "# Check if we have the remote metadata cached.", "after", None),  # C17: meta_fetch begins
     ("prefix", "has_remote_metadata = (", "at", "with self.remote_metadata_lock:")),
    (("equal", "remote_layer_name_to_idx = build_layer_name_to_metadata_idx(remote_kv_group2layeridx)", "at", None),  # C19
     ("prefix", "session_id = ", "at", None),
     ("prefix", "req_start_time = time.perf_counter()", "at", None)),
    (("prefix", "req_start_time = time.perf_counter()", "after", None),),  # C20: prepare begins
    (("equal", "if not src_list:", "at", None),  # C21: prepare ends
     ("regex", r"self\.engine\.batch_transfer_sync_read\(", "at", None)),
)
READ_BACK = re.compile(r"self\._transfer_batch\(req_meta, (.*), req_start_time\)\s*$")

# --- BEGIN GENERATED (tools/regen_span_diff.py, from the d4ad49796 original and the T56 replacement) ---
REF_LINES = ['4b07111cfff22d70', 'c3f1da03356b430c', '8914dfd4440a3aeb', '233736f076bc4084', '85f87a78a939667f', '143ae72f1224c890', '96451c24539b5133', '1833bdf42b99b6cb', 'ec3d2c66b2aa4f3f', '683a4d605a3efb06', '13d5466ab409283c', '6e59ff1b4d462aa8', 'f1d68dee1d3c0201', '4ed2d849a3e067a0', '7f19072525a5b64e', 'f4730cbc3c399e88', '7187f0675eb38279', '75ffb6041356657d', 'fb64af6073d68dfa', '1d2ced63a26f1863', 'a40bceef5bec5444', '8f65d17c51f85245', 'ba5ec51d07a4ac0e', '7161da95a4961769', 'b6e51a709cd72464', 'fb64af6073d68dfa', '0e51d32982fc59ac', '65d268c434446d74', 'd60f90117725f878', '24d98eef1b82557e', 'deef5dcf29130c95', 'c31e0d7a24cead18', 'eada6790468c6f14', 'ba5ec51d07a4ac0e', '1e04ef740789acf2', 'e81eeafee30fcdea', 'a8cdb51ca0110816', 'a7fdae52a32488e8', '4a61d8baf19d313c', '69775948b3ddc142', '647ff701a3ce3e1a', 'bfe199c8a9a43346', 'ae8cda6a52c0de81', '9af6926de73e20d1', '8b1974b8df50acdb', 'b0feb8f3754f621c', '3c1dcd4f27919699', 'ba5ec51d07a4ac0e', '9f3c589c20ddb77f', 'cf1f7e356c0d8440', '41367e6faf0e0fe9', '104b9bb8345b9fbf', 'c5fe741a17edb515', '86f9444198a40371', '5fcf172866c61fcc', '4921d24626811d5b', '932a961c14f838d5', 'd50ea00c1cbc2d84', 'ba5ec51d07a4ac0e', '33ac649e112ef580', 'c2d67fd5ec06b3d4', '20bc4776303eee11', 'eada6790468c6f14', '927c288371918ae9', 'ba5ec51d07a4ac0e', '48f981f2ac98c86e', '9c6de976b59e0147', 'a7905bcd8f2ea318', '4d401a943ef2e79a', 'a07746ce19413c00', 'b15836f3735b7a32', 'fedcd60ae5328800', 'ccb52d6b74cc0dd6', '07bd7bc1994d64a0', 'c0a2afb56f96c308', '3c7ce9b607bd465b', '65d50f9057320eae', 'f42679106964754b', '5af28674a9c0352d', 'ba5ec51d07a4ac0e', 'b400d98a0553cc7b', 'e258f25b1d88cfce', 'cfaea2bfe61592b6', '2283b1b24140c4f9', 'c822d66d531bf3f1', '413225145be14810', 'aa5e6ac0f3e3655b', 'e256ee8e7aff6957', '582f360e25ac8cbe', '056db1cb4e2707fa', 'faabeac8c9da4029', 'cbf680075f122803', '64cbe405c56364c4', 'e4a13716455a3394', '37c18c8028414f29', '570037fcb929ef79', 'e256ee8e7aff6957', '4665005285838cb0', '12de9c6c0f1cecb6', '74a5e66e84aa16cf', 'b89ac9a64da2040b', 'fe7d227e3f42b039', '7a132eceed3218ff', '4c0f45a1ad79c31f', 'c340920c7ceb5608', '270dffe1557e38ce', '318841d01690bd1f', '5c90b4e92e95ff5e', 'a74da936096e8f24', 'ba5ec51d07a4ac0e', '675c27cf4847cf5f', 'c930fa83d6a1b20d', '03e11314ad68e872', '229672c0716c4a0a', '32ebb1abcc1c601c', '23d7e2e4c702b242', '2ed31ed24f765ded', 'ba5ec51d07a4ac0e', 'ba5ec51d07a4ac0e', '675c27cf4847cf5f', '00111c282fd0d324', 'aad7976302b1ccf0', 'ebec5c9c2c58f64f', 'ae516da4b0c8779c', 'd7557aae12da7324', '7a4a97942b6147bc', '7c3edb6659906054', 'd85351268d3df744', '093f62c9c93e1bb9', 'ba5ec51d07a4ac0e', '7e02373728977762', '675c27cf4847cf5f', '27fe626000a1fbd6', '8d4b40343808ae33', 'd7557aae12da7324', 'b9f39b86fbcbf223', '7c3edb6659906054', 'd85351268d3df744', '8f1a3fb042ee6b89', 'ba5ec51d07a4ac0e', '4b72f4e640f5425a', '45124db9c5d3730f', '9e9cdc34e95f2463', 'd37c9ac481d5a83e', '1eb1cddc758c7e6c', 'f36443d66297074a', '0f2172d752f20685', '65d50f9057320eae', 'd25472c6598e3048', 'ba5ec51d07a4ac0e', '6ca268edcda6c852', '1a670f758f922866', 'c7b01bc773412f7c', '381a1c9c6e78eeba', '7cf7ebef346a276e', 'b1cf8887c12d66c2', 'ca640f28d7f4f150', '5e730182cf164af6', '31dd2852a8a73ca9', '84fe01eaa97532e2', '3ff72b6311510977', '9b461a02a7c4d667', 'de31864f8c1a853c', '102af2e728171ef6', '8bce2a44a9fce9f4', 'ba5ec51d07a4ac0e', '5bd86e4c7c7ad2dc', '3d8f69f41dc546d4', 'c7462c1a54d263d1', 'f4bbd8dc0a3fd7f2', 'e8712c4b8f32091d', '6a30c7ccf36605ba', '297683a49d26efe3', '4da4523cf56b633f', '3a342a318f5e394d', '65bbbaf3dcb96355', '0f2172d752f20685', 'f1194b2f18a979d7', 'b5c33c84b7568b08', '2400b70a1e14a648', '2edcf9b56fcf45dd', '086a7fd78103b75e', 'ba5ec51d07a4ac0e', 'e256ee8e7aff6957', '1eb1cddc758c7e6c', 'f36443d66297074a', '0f2172d752f20685', '65d50f9057320eae', 'd25472c6598e3048', 'ba5ec51d07a4ac0e', '0d2a7dc66aae1b58', 'cb5fd2558634b9ce', '599ddbd1bf906a74', '77cd33b2c64d293f', 'eb98bd81df5386b9', '790dc6277ab4aac5', '3da9745d3114c4da', '77e9c89ced31dc27', '9494d794c0d2dca9', '3b0c3174e3323988', '6be2bf5abf2e052e', '675c27cf4847cf5f', '4304aeff060f582f', 'e256ee8e7aff6957', '2a4ca41b88af5dbe', '8a129f1bfa9be205', 'dd51cc446f8b6b4d', 'c73ca24ca8c2ff41', '6aa14c0ad47823c8', 'f70aba83f94f8000', 'ba5ec51d07a4ac0e', '74fb128fb0e86d9c', '761ea79d3dc3e038', 'bda5b8fcacb5ad7b', 'dc53ed1b25655a4b', '107278f71a2faf71', '54b4d0fd4170c1a3', 'd099a4604f31eded', 'e8712c4b8f32091d', 'c111e0b61e1aeb38', '8a9cdd18cb5521da', '3a342a318f5e394d', '65bbbaf3dcb96355', '0f2172d752f20685', 'dd51cc446f8b6b4d', '8a129f1bfa9be205', '2400b70a1e14a648', '2edcf9b56fcf45dd', '086a7fd78103b75e', 'ba5ec51d07a4ac0e', '609dbd6758706c97', '7187f0675eb38279', 'e8712c4b8f32091d', '0e133f97f590a91e', '3a342a318f5e394d', '086a7fd78103b75e', 'c7b01bc773412f7c', '381a1c9c6e78eeba', '7cf7ebef346a276e', 'ba5ec51d07a4ac0e', '04b964d3b726319d', 'abc467d591078204', 'd4695d8531b8fc77', 'b690f9f995065c6d', '7d9f2bc11aee0df1', '073ff676d5264d6c', 'ba5ec51d07a4ac0e', 'd7c87e62587d8ae1', '3601e7bbbf09fd78', '7adb819068e13a16', 'f4ffd0aff9896e60', '674d3bdb77799f83', '3a342a318f5e394d', '0bf2449feb4f7e03', '2bea478f30e4422e', '34096f77069fb61b', '086a7fd78103b75e', 'ba5ec51d07a4ac0e', '5df27c44fe342460', '12aeef156afbcb12', 'd85e318ca1058e9c', 'ee9bb41d5abed64d', 'b6c82fc7a3c934e7', '0ed96521a1a71f03', '7a610b6660261306', 'fc15ae255f0ac47e', '41657065fa2cacee', '840b16d7838bb662', 'ba5ec51d07a4ac0e']
REF_BOUNDS = [18, 34, 37, 230]
PATCH_LINES = {'2afd89cc53c7ff1c', '3daa83007b151ff6', '7ac825e670f92a13', '84f5829da8a7da73', '9c782b268b858b4b', 'ca631bc4e4205e6d', 'd47b6859020fa75e', 'ded765cd8961fc20'}
# --- END GENERATED ---


def line_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def function_lines(source):
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == FUNCTION.split(".")[0]:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == FUNCTION.split(".")[1]:
                    return source.split("\n")[item.lineno - 1:item.end_lineno]
    return None


def normalise(raw_lines, patched):
    """Non-blank stripped lines; on a patched file the inserted lines are dropped, the READ is read back, and the
    positions of the four stage calls (in the kept lines) are returned as the stage boundaries."""
    out, marks = [], []
    for raw in raw_lines:
        text = raw.strip()
        if not text:
            continue
        if patched:
            if text in INSTRUMENTED:
                marks.append((text, len(out)))
                continue
            back = READ_BACK.search(text)
            if back:
                text = text[:back.start()] + f"self.engine.batch_transfer_sync_read({back.group(1)})"
            elif line_hash(text) in PATCH_LINES:
                continue
        out.append(text)
    bounds = None
    if patched and [m[0] for m in marks] == list(INSTRUMENTED):
        bounds = [m[1] for m in marks]
    return out, bounds


def _hits(lines, match, anchor):
    if match == "equal":
        return [i for i, text in enumerate(lines) if text == anchor]
    if match == "prefix":
        return [i for i, text in enumerate(lines) if text.startswith(anchor)]
    return [i for i, text in enumerate(lines) if re.search(anchor, text)]


def bounds_by_anchor(lines):
    """Indices into lines: start of meta_fetch, end of meta_fetch, start of prepare, end of prepare. Each boundary
    takes the first of its anchors that matches exactly one line, as the replay does."""
    found = []
    for alternatives in ANCHORS:
        for match, anchor, where, walk in alternatives:
            hits = _hits(lines, match, anchor)
            if len(hits) != 1:
                continue
            index = hits[0]
            if walk is not None:
                above = [i for i in range(index - 1, -1, -1) if lines[i] == walk]
                if not above:
                    continue
                index = above[0]
            found.append(index + (1 if where == "after" else 0))
            break
        else:
            return None
    return found


def region(index, bounds):
    for name, bound in zip(REGIONS, bounds):
        if index < bound:
            return name
    return REGIONS[-1]


def span_diff(source, show=False):
    """The one-line summary (and with show=True the changed lines); raises LookupError when not applicable."""
    raw = function_lines(source)
    if raw is None:
        raise LookupError(f"{FUNCTION} not found")
    patched = MARKER in source
    lines, bounds = normalise(raw, patched)
    if not patched:
        bounds = bounds_by_anchor(lines)
    if bounds is None:
        raise LookupError("a stage boundary was not found" + (" (stage calls)" if patched else " (anchors C17/C19/C20/C21)"))
    hashed = [line_hash(text) for text in lines]
    counts = {name: [0, 0] for name in REGIONS}
    added_at, shown = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, REF_LINES, hashed, autojunk=False).get_opcodes():
        if tag in ("insert", "replace"):
            for j in range(j1, j2):
                name = region(j, bounds)
                counts[name][0] += 1
                added_at.append(f"{name}:{hashed[j]}")
                shown.append(f"+ {name}: {lines[j]}")
        if tag in ("delete", "replace"):
            for i in range(i1, i2):
                name = region(i, REF_BOUNDS)
                counts[name][1] += 1
                shown.append(f"- {name}: (line {i + 1} of the original's non-blank lines)")
    added = sum(c[0] for c in counts.values())
    removed = sum(c[1] for c in counts.values())
    summary = (
        f"p2d_span_diff spans={'instrumented' if patched else 'anchors'} func_lines={len(raw)} "
        f"ref_lines={len(REF_LINES)} added={added} removed={removed} "
        + " ".join(f"{name}=+{a}/-{r}" for name, (a, r) in counts.items())
        + f" added_at={','.join(added_at) or 'none'}"
    )
    return summary + ("\n" + "\n".join(shown) if show and shown else "")


def main(argv):
    args = [a for a in argv if a != "--show"]
    if len(args) != 1:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 1
    try:
        with open(args[0], encoding="utf-8") as handle:
            source = handle.read()
    except OSError as exc:
        print(f"span_diff: cannot read input: {exc}", file=sys.stderr)
        return 1
    try:
        print(span_diff(source, show="--show" in argv))
    except (LookupError, SyntaxError) as exc:
        print(f"p2d_span_diff missing: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
