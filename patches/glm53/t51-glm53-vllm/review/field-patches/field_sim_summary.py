"""Per mode and HTTP scenario: status, and whether each API-side segment source is present."""
import collections, json, sys
def num(v): return isinstance(v, int) and not isinstance(v, bool)
for path in sys.argv[1:]:
    mode = path.rsplit("/", 1)[-1].removesuffix(".jsonl")
    by = collections.defaultdict(list); http = {}
    for l in open(path):
        o = json.loads(l)
        if "http" in o: http[o["scenario"]] = o["http"]["status"]
        if "rec" in o: by[o["scenario"]].append(o["rec"])
        if "driver_error" in o: print(mode, "DRIVER ERROR", o["driver_error"][-300:])
    print(f"== {mode}")
    print("   scenario                 http  req_resolve render tokenize add_request detok queue hash first_generated http.status")
    for s in sorted(http):
        rs = by[s]
        h = [r for r in rs if r["event"] == "api_summary" and r.get("scope") == "http"]
        sub = [r for r in rs if r["event"] == "api_summary" and r.get("scope") == "engine_submission"]
        req = [r for r in rs if r["event"] == "api_summary" and r.get("scope") == "request"]
        core = [r for r in rs if r["event"] == "core_summary" and r.get("scope") == "request"]
        st = lambda rec, name: num(rec.get("stages", {}).get(name, {}).get("duration_ns"))
        f = lambda ok: "yes" if ok else "-"
        row = [
            f(h and st(h[0], "req_resolve")), f(h and st(h[0], "render")), f(h and st(h[0], "tokenize")),
            f(sub and st(sub[0], "add_request")), f(req and num(req[0].get("terminal", {}).get("detokenize_duration_ns"))),
            f(core and num(core[0]["stages"]["queue"].get("initial_wait_ns"))), f(core and st(core[0], "hash")),
            f(h and "first_generated" in h[0]), (h[0]["status"] if h else "-"),
        ]
        print(f"   {s:24s} {http[s]:4d}  " + "  ".join(f"{x:>6s}" for x in row))
