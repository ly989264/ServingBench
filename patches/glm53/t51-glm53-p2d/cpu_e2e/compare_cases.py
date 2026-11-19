"""Compare the client results of a case before and after the patch: python3 compare_cases.py before.json after.json"""
import json
import sys

a, b = (json.load(open(p)) for p in sys.argv[1:3])
diff = []
for name in sorted(set(a) | set(b)):
    x, y = a.get(name, {}), b.get(name, {})
    for key in ("status", "texts", "finish", "done"):
        if name.endswith("aborted") and key in ("texts", "finish"):
            continue  # how far an abandoned stream got depends on timing
        if x.get(key) != y.get(key):
            diff.append(f"{name}.{key}: {x.get(key)!r} != {y.get(key)!r}")
print(json.dumps({"scenarios": len(set(a) | set(b)), "differences": diff}, ensure_ascii=False, indent=1))
sys.exit(1 if diff else 0)
