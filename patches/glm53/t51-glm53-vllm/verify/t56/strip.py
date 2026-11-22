"""Drop stages.kv_ready_wait from every record. usage: python3 strip.py IN.jsonl OUT.jsonl"""
import json
import sys

n = 0
with open(sys.argv[2], "w") as f:
    for line in open(sys.argv[1]):
        o = json.loads(line)
        rec = o.get("rec")
        if rec and isinstance(rec.get("stages"), dict) and "kv_ready_wait" in rec["stages"]:
            del rec["stages"]["kv_ready_wait"]
            n += 1
        f.write(json.dumps(o, sort_keys=True) + "\n")
print("stripped", n)
