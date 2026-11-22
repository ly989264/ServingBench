#!/usr/bin/env python3
"""T58: profiles/glm53/segments.json of v3.3-extra from v3.2-extra's: the config_id, and each node's from_level after
W1's T58 design, section 2 of part 乙 (the only source; W1 tells t41 when it changes):
  P background wait_for_save's 5 sub-segments: 2; put's 3 (submit, wait_for_transfer, finalize): 3;
  D start_load_kv's 4 sub-segments: 2; everything else: 1 (no attribute).
Idempotent. Usage: make_profile_v33.py <segments.json in> <segments.json out>"""
import json
import sys

CONFIG_ID = "pd-diag-v3.3-extra-glm53-2026-11-20a"
src, dst = sys.argv[1:3]
raw = json.load(open(src))
raw["config_id"] = CONFIG_ID
seen = []


def mark(node, level, path):
    node["from_level"] = level
    seen.append((path, level))


def find(nodes, nid):
    return next(n for n in nodes if n["id"] == nid)


wfs = find(raw["sides"]["prefill"]["background"], "wait_for_save")
for c in wfs["children"]:
    mark(c, 2, "p.wait_for_save." + c["id"])
    if c["id"] == "put":
        for g in c["children"]:
            mark(g, 3, "p.wait_for_save.put." + g["id"])
slk = find(raw["sides"]["decode"]["ttft"], "start_load_kv")
for c in slk["children"]:
    mark(c, 2, "d.start_load_kv." + c["id"])
assert len(seen) == 12, seen
json.dump(raw, open(dst, "w"), ensure_ascii=False, indent=2)
open(dst, "a").write("\n")
print("%s: %s; from_level on %d nodes: %s" % (dst, CONFIG_ID, len(seen), ", ".join("%s=%d" % x for x in seen)))
