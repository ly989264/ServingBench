"""Build the glm5.3 patching manual from template.zh-CN.md: every {{B:<text>}} becomes the one fenced command block of
the sources that contains <text> (the block copied byte for byte, with its language tag), so no command is retyped.
Other {{NAME}} slots are filled from parts/NAME.md. Stops when a reference matches no block or more than one distinct
block, or a slot has no file.
    python3 -I build_manual.py <template> <parts dir> <out> <source.md> [<source.md> ...]
"""
import os
import re
import sys

tpl, parts, out = sys.argv[1:4]
sources = sys.argv[4:]
blocks = []   # (source, lang, body)
for src in sources:
    text = open(src, encoding="utf-8").read()
    # blocks inside a block quote ("> ```") count too, with the "> " taken off
    unquoted = "\n".join(l[2:] if l.startswith("> ") else ("" if l == ">" else l) for l in text.split("\n"))
    for t in (text, unquoted):
        for m in re.finditer(r"^```([a-z]*)\n(.*?)^```\n", t, re.S | re.M):
            blocks.append((os.path.basename(src), m.group(1), m.group(2)))
s = open(tpl, encoding="utf-8").read()
used = []
renamed = []
# placeholders are text, not commands: the ones that name a step of guide A are renumbered like the prose
RENAMES = {"<第 2 步的 baseline_token>": "<第 3 步的 baseline_token>"}


def block(m):
    key = m.group(1)
    hits = {(lang, body) for _, lang, body in blocks if key in body}
    exact = {(lang, body) for lang, body in hits if body.strip() == key.strip()}
    if len(hits) > 1 and len(exact) == 1:   # the key is the whole block: that one
        hits = exact
    if len(hits) != 1:
        where = sorted({src for src, lang, body in blocks if key in body})
        raise SystemExit("reference %r matches %d distinct blocks %s" % (key[:60], len(hits), where))
    lang, body = hits.pop()
    used.append(key)
    for a, b in RENAMES.items():   # placeholder text that names a step of the source document, renumbered
        if a in body:
            renamed.append(a)
            body = body.replace(a, b)
    return "```%s\n%s```" % (lang, body)




def slot(m):
    p = os.path.join(parts, m.group(1) + ".md")
    if not os.path.exists(p):
        raise SystemExit("slot %s has no file %s" % (m.group(1), p))
    return open(p, encoding="utf-8").read().rstrip("\n")


s = re.sub(r"\{\{([A-Z0-9_]+)\}\}", slot, s)
s = re.sub(r"\{\{B:(.*?)\}\}", block, s, flags=re.S)
assert "{{" not in s, "unfilled slot"
open(out, "w", encoding="utf-8").write(s)
left = sorted(set(re.findall(r"<[^<>\n]*第 ?[0-9]+(?:\.[0-9]+)? ?[步节][^<>\n]*>", "\n".join(re.findall(r"^```[a-z]*\n(.*?)^```", s, re.S | re.M)))))
left = [x for x in left if x not in RENAMES.values()]
if left:
    raise SystemExit("placeholders in command blocks still name a step: %s" % left)
print("%s: %d command blocks copied from %d sources; placeholders renumbered in %d blocks" % (out, len(used), len(sources), len(renamed)))
