"""Replays the T56 lab sample's EngineCore and Worker lines through the pack's filter: (a) 20b from the start, (b) 19c,
(c) 19c then reread as 20b halfway. Counts the requests whose partial sums have lookup / cand_lookup / alloc."""
import collections, gzip, importlib.util, json, logging, os, re, shutil, sys
PACK, SEG19, SEG20, OUT = sys.argv[1:5]
spec = importlib.util.spec_from_file_location("flt", os.path.join(PACK, "node", "pd_diag_logfilter.py"))
flt = importlib.util.module_from_spec(spec); spec.loader.exec_module(flt)
PRE = re.compile(r"\((?P<proc>[A-Za-z_0-9]+) pid=\d+\).*?\[PD_DIAG\] (?P<j>\{.*\})")
recs = []
for line in gzip.open(sys.argv[5], "rt"):
    m = PRE.search(line)
    if m and not m.group("proc").startswith("APIServer"):
        recs.append((m.group("proc"), m.group("j")))
os.environ["PD_DIAG_INSTANCE"] = "D0"
def run(name, first, second=None, at=None):
    d = os.path.join(OUT, name); shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
    live = os.path.join(d, "seg.json"); shutil.copyfile(first, live)
    h = flt.PdDiagHandler(mapping=live, log_dir=os.path.join(d, "out"))
    for i, (proc, payload) in enumerate(recs):
        if second and i == at:
            shutil.copyfile(second, live)
            with h._mu:
                h._load_config()
        r = logging.LogRecord("vllm.pd_diagnostics", logging.INFO, "x.py", 0, "[PD_DIAG] %s", (payload,), None)
        r.processName = proc
        h.handle(r)
    h.close()
    c = collections.Counter(); cv = collections.Counter()
    for root, _, fs in os.walk(os.path.join(d, "out")):
        for f in fs:
            for l in open(os.path.join(root, f)):
                if "pd_seg_partial" not in l: continue
                o = json.loads(l[l.index("[PD_DIAG] ") + 10:])
                after = (second is None) or o.get("config_version", "").startswith("pd-diag-v3.1-extra-glm53-2026-11-20b")
                for k in ("lookup", "cand_lookup", "alloc", "cand_alloc"):
                    s = (o.get("segs") or {}).get(k)
                    if isinstance(s, dict) and s.get("ns"):
                        c[(o.get("config_version", "")[-14:-9], k)] += 1
    print(name, dict(sorted(c.items())))
run("a-20b", SEG20)
run("b-19c", SEG19)
run("c-19c-then-20b", SEG19, SEG20, len(recs) // 2)

def run2(name, first, second):
    """Pass 1 under `first`, reread as `second`, pass 2: the same records with every request id renamed (new requests)."""
    d = os.path.join(OUT, name); shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
    live = os.path.join(d, "seg.json"); shutil.copyfile(first, live)
    h = flt.PdDiagHandler(mapping=live, log_dir=os.path.join(d, "out"))
    for p in (1, 2):
        if p == 2:
            shutil.copyfile(second, live)
            with h._mu:
                h._load_config()
        for proc, payload in recs:
            if p == 2:
                payload = payload.replace('chatcmpl-', 'chatcmpl-p2-').replace('execution-', 'execution-p2-')
            r = logging.LogRecord("vllm.pd_diagnostics", logging.INFO, "x.py", 0, "[PD_DIAG] %s", (payload,), None)
            r.processName = proc
            h.handle(r)
    h.close()
    c = collections.Counter()
    for root, _, fs in os.walk(os.path.join(d, "out")):
        for f in fs:
            for l in open(os.path.join(root, f)):
                if "pd_seg_partial" not in l: continue
                o = json.loads(l[l.index("[PD_DIAG] ") + 10:])
                for k in ("lookup", "cand_lookup", "alloc", "cand_alloc"):
                    s = (o.get("segs") or {}).get(k)
                    if isinstance(s, dict) and s.get("ns"):
                        c[(o.get("config_version", "")[-14:-9], k)] += 1
    print(name, dict(sorted(c.items())))
run2("d-19c-then-20b-new-requests", SEG19, SEG20)
run2("e-19c-then-19c-new-requests", SEG19, SEG19)
