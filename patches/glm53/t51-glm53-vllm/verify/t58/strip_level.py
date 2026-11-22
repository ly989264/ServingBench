"""Check and remove diag_level in a T58 dump. It must be on every api_summary[scope=request] and
core_summary[scope=request] record, with the given level, when LEVEL < 3, and on no other record
(and on none at all when LEVEL is 3).  usage: python3 strip_level.py IN.jsonl OUT.jsonl LEVEL"""
import collections
import json
import sys

level = int(sys.argv[3])
TARGET = {("api_summary", "request"), ("core_summary", "request")}
seen, missing, wrong = collections.Counter(), collections.Counter(), []
with open(sys.argv[2], "w") as f:
    for line in open(sys.argv[1]):
        o = json.loads(line)
        rec = o.get("rec")
        if isinstance(rec, dict):
            kind = (rec.get("event"), rec.get("scope"))
            if "diag_level" in rec:
                value = rec.pop("diag_level")
                seen[kind] += 1
                if kind not in TARGET or level == 3 or value != level or type(value) is not int:
                    wrong.append((o.get("scenario"), kind, value))
            elif kind in TARGET and level < 3:
                missing[kind] += 1
        if "diag_level" in json.dumps(o):
            wrong.append((o.get("scenario"), "diag_level elsewhere", None))
        f.write(json.dumps(o, sort_keys=True) + "\n")
print(f"level {level}: with diag_level {dict(seen)}; target records without it {dict(missing)}; wrong {len(wrong)}"
      + ("  OK" if not wrong and not missing else ""))
for w in wrong[:10]:
    print("  wrong:", w)
