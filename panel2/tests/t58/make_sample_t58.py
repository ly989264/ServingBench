#!/usr/bin/env python3
"""T58 lab sample: tests/t56/samples/pd032_full_P0_D0.log.gz (T56's records, level 3) as D writes it at PD_DIAG_LEVEL 1
(W1's T58 design, part 乙 section 2): diag_level 1 on the request summaries (api_summary / core_summary scope request);
request_generation without the 4 maxima fields, Worker execute records without stages.submit, Worker sample records
without stages.bookkeeping. The lab's D stand-in replays it (FAKE_SAMPLE); P keeps the level-3 sample.
Usage: make_sample_t58.py <in .log.gz> <out .log.gz>"""
import collections
import gzip
import json
import re
import sys

PREFIX = re.compile(r"\[PD_DIAG\] ")
src, dst = sys.argv[1:3]
n = collections.Counter()
with gzip.open(src, "rt") as f, gzip.open(dst, "wt") as g:
    for line in f:
        m = PREFIX.search(line)
        if not m:
            g.write(line)
            continue
        try:
            o = json.loads(line[m.end():])
        except ValueError:
            g.write(line)
            continue
        ev, sc = o.get("event"), o.get("scope")
        if (ev, sc) in (("api_summary", "request"), ("core_summary", "request")):
            o["diag_level"] = 1
            n["diag_level"] += 1
        if ev == "pd_transfer_summary" and sc == "request_generation":
            for f_ in ("task_max_queue_wait_ns", "task_max_meta_fetch_ns", "task_max_prepare_ns", "transfer_envelope_ns"):
                n["rg_field"] += o.pop(f_, None) is not None
        if ev == "worker_summary" and o.get("phase") == "execute":
            n["submit"] += (o.get("stages") or {}).pop("submit", None) is not None
        if ev == "worker_summary" and o.get("phase") == "sample":
            n["bookkeeping"] += (o.get("stages") or {}).pop("bookkeeping", None) is not None
        g.write(line[:m.end()] + json.dumps(o, separators=(",", ":")) + "\n")
print("%s: %s" % (dst, dict(n)))
