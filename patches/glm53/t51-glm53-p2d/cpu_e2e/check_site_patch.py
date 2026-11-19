"""Scenario 5: the simulated site wrappers were called as expected. python3 check_site_patch.py <case dir>

From the "p2d_site_patch" lines of the server log(s):
  - AsyncLLM._add_request wrapper: an n=1 request is one call with parent=None; an n=2 request is two calls with
    the same parent and index 0 and 1 (vLLM v0.30.0 sends each child through _add_request; the vLLM patch db27fd1
    keeps that). client.py sends 4 + 1 + 1 + 4 (concurrent) n=1 requests and 4 n=2 requests: 10 parentless calls,
    4 parents with children {0, 1}. With the site patches it also sends one n=1 and one n=2 request that fail the
    length check, then one normal request: two more parentless calls, and a fifth parent that stops at child 0;
    2 "rejected" lines.
  - the keyword arguments each _add_request call received (the patched vLLM adds owner= / accounted=) are counted.
  - completion_stream_generator wrapper: one "stream chunks=N" line per streamed /v1/completions request
    (stream n=1, stream n=2, the abandoned stream): 3 lines.
"""
import collections
import glob
import gzip
import json
import os
import re
import sys

REJECT = re.compile(r"p2d_site_patch rejected request_id=(\S+) prompt_tokens=(\d+)")
ADD = re.compile(r"p2d_site_patch add_request request_id=(\S+) parent=(\S+) index=(\S+) prompt_tokens=(\d+)(?: kwargs=(\S+))?")
STREAM = re.compile(r"p2d_site_patch stream chunks=(\d+)")
case = sys.argv[1]
adds, streams, rejected = [], [], []
for path in glob.glob(os.path.join(case, "*.log")) + glob.glob(os.path.join(case, "*.log.gz")):
    for line in (gzip.open(path, "rt", errors="replace") if path.endswith(".gz") else open(path, errors="replace")):
        m = ADD.search(line)
        if m:
            adds.append(m.groups())
        m = REJECT.search(line)
        if m:
            rejected.append(m.group(1))
        m = STREAM.search(line)
        if m:
            streams.append(int(m.group(1)))
parents = collections.defaultdict(list)
plain = 0
for rid, parent, index, _, _ in adds:
    if parent == "None":
        plain += 1
    else:
        parents[parent].append(int(index))
# the n = 2 request that fails the length check stops at its first child (index 0)
full = [p for p, v in parents.items() if sorted(v) == [0, 1]]
stopped = [p for p, v in parents.items() if sorted(v) == [0]]
result = {
    "add_request_calls": len(adds), "parentless_calls": plain, "n2_parents": len(parents),
    "children_per_parent": {p: sorted(v) for p, v in parents.items()},
    "kwargs_seen": dict(collections.Counter(a[4] or "-" for a in adds)),
    "stream_wrapper_calls": len(streams), "stream_chunks": streams,
    "rejected": len(rejected), "n2_parents_with_children_0_1": len(full), "n2_parents_stopped_at_child_0": len(stopped),
    "ok": plain == 12 and len(full) == 4 and len(stopped) == 1 and len(parents) == 5 and len(rejected) == 2
          and len(streams) == 3,
}
print(json.dumps(result, indent=1))
sys.exit(0 if result["ok"] else 1)
