"""Per scenario: are the per-step SchedulerOutput digests and the transported outputs identical? (ids masked)"""
import json, re, sys, collections
RID = re.compile(r"-[0-9a-f]{8}(?=$|[^0-9a-f])"); HEX = re.compile(r"[0-9a-f]{32}")
def norm(v):
    if isinstance(v, dict): return {norm(k): norm(x) for k, x in v.items()}
    if isinstance(v, list): return [norm(x) for x in v]
    if isinstance(v, str): return RID.sub("-<r>", HEX.sub("<id>", re.sub(r"-\d+-\d+$", "-<pid>-<n>", v)))
    return v
def load(p):
    d = collections.defaultdict(lambda: {"decision": [], "transport": []})
    for l in open(p):
        o = json.loads(l)
        for k in ("decision", "transport"):
            if k in o:
                d[o["scenario"]][k].append(json.dumps(norm(o[k]), sort_keys=True))
    return d
a, b = load(sys.argv[1]), load(sys.argv[2])
bad = 0
for s in sorted(set(a) | set(b)):
    same_d = a[s]["decision"] == b[s]["decision"]; same_t = a[s]["transport"] == b[s]["transport"]
    bad += not (same_d and same_t)
    print(f"  {s}: steps {len(a[s]['decision'])}/{len(b[s]['decision'])} decisions {'same' if same_d else 'DIFFERENT'}, outputs {'same' if same_t else 'DIFFERENT'}")
print("  scenarios with differences:", bad)
