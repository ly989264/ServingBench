"""Remove worker_summary stages.submit from the records of the D scenarios (D1-D4) of a d_equiv dump, and
count what is there.  usage: python3 strip_submit.py IN.jsonl OUT.jsonl [--count-only]
Prints, per scenario, how many worker_summary records carry stages.submit (before removal)."""
import collections
import json
import sys

count_only = "--count-only" in sys.argv
have = collections.Counter()
out = None if count_only else open(sys.argv[2], "w")
for line in open(sys.argv[1]):
    o = json.loads(line)
    rec = o.get("rec")
    if isinstance(rec, dict) and rec.get("event") == "worker_summary" and "submit" in (rec.get("stages") or {}):
        have[o.get("scenario")] += 1
        if not count_only and str(o.get("scenario", "")).startswith("D"):
            del rec["stages"]["submit"]
    if out:
        out.write(json.dumps(o, sort_keys=True) + "\n")
print("worker_summary with stages.submit:", dict(sorted(have.items())))
