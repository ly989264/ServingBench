"""Per-kind record sizes from the volume harness (n = requests in the step: 1, 64, 256), re-normalized with norm.py.
Fits bytes = a + b*n on n=64 and n=256 and checks the fit at n=1."""
import collections, json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from norm import msg_bytes
d = json.load(open(sys.argv[1]))
P_MAX = 200000          # request ids below this number are P's (harness numbering)
def side_of(x, part):
    rid = x["normalized"].get("request_id")
    ids = x["normalized"].get("request_ids") or []
    probe = rid or (ids[0] if ids else None) or ((x["normalized"].get("members") or [{}])[0].get("request_id"))
    if probe and probe.startswith("chatcmpl-"):
        return "P" if int(probe.rsplit("-", 1)[1], 16) < P_MAX else "D"
    return "P" if part == "P" else "D"
tab = collections.defaultdict(lambda: collections.defaultdict(list))
for r in d["runs"]:
    if not r["d_save"]:
        continue
    for part in ("P", "D_first", "D_decode"):
        for x in r[part]:
            k = x["kind"]
            if k == "pool_task[scope=task][operation=save_submit]" and part != "P":
                k += "(decode, no saves)" if (x["n_members"] == 0) else ""
            tab[(side_of(x, part), k)][r["n"]].append(msg_bytes(x["normalized"]))
out = {}
for key in sorted(tab):
    v = tab[key]
    m = {n: sum(s) / len(s) for n, s in v.items()}
    if 64 in m and 256 in m:
        b = (m[256] - m[64]) / 192
        a = m[64] - 64 * b
        pred1 = a + b
        out["%s %s" % key] = {"a": round(a, 1), "b": round(b, 2), "n1_measured": m.get(1), "n1_fit": round(pred1, 1),
                              "counts": {n: len(s) for n, s in v.items()}}
    else:
        out["%s %s" % key] = {"means": m, "counts": {n: len(s) for n, s in v.items()}}
for k, v in out.items():
    print("%-75s %s" % (k, v))
json.dump(out, open(sys.argv[2], "w"), indent=1)
