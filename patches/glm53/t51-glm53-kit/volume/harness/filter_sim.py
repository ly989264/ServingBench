"""Feed measured records through the real panel-2 filter (pack two v3.1-extra pd_diag_logfilter.py with the glm53
segments.json) and measure the bytes of each kind of line it writes to its log directory.
usage: filter_sim.py FILTER.py SEGMENTS.json VOLUME_LONG.json EQUIV.jsonl OUT_DIR"""
import copy, glob, importlib.util, json, logging, os, sys, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from norm import normalize, HOST20, CLOCK0
FILTER, SEG, LONG, EQUIV, OUTD = sys.argv[1:6]
os.getpid = lambda: 1234567          # site-like pid width (filter lines carry the pid)
os.environ["PD_DIAG_INSTANCE"] = "8001"
spec = importlib.util.spec_from_file_location("pdf", FILTER)
pdf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pdf)

long = json.load(open(LONG))["runs"][0]          # n=1, d_save=True, 69 decode steps
eq = [json.loads(l) for l in open(EQUIV)]
api = [r["rec"] for r in eq if "rec" in r and r["scenario"] == "F5_chat" and r["rec"].get("event") == "api_summary"]
for a in api:                                    # detokenize ranges at their cap of 64 (entry repeated)
    if a.get("scope") == "request":
        rg = a["stages"]["detokenize"]["ranges"]
        a["stages"]["detokenize"]["ranges"] = [copy.deepcopy(rg[-1]) for _ in range(64)]

def run(side, records, worker_proc):
    d = os.path.join(OUTD, side)
    os.makedirs(d, exist_ok=True)
    for f in glob.glob(os.path.join(d, "*", "*.log")):
        os.remove(f)
    h = pdf.PdDiagHandler(mapping=SEG, log_dir=d)
    def emit(rec, proc):
        payload = json.dumps(normalize(rec), separators=(",", ":"))
        lr = logging.LogRecord("vllm.v1.metrics.pd_diagnostics", logging.INFO, "/v/vllm/v1/metrics/pd_diagnostics.py",
                               214, "[PD_DIAG] %s", (payload,), None)
        lr.vllm_process_name = proc
        lr.process = 1234567
        h.emit(lr)
        h._host = HOST20
    for rec in records:
        ev = rec.get("event")
        proc = "APIServer" if ev == "api_summary" else (worker_proc if ev in ("worker_summary", "pool_task") or (ev == "pd_transfer_summary" and rec.get("scope") == "task") else "EngineCore_DP0")
        emit(rec, proc)
    with h._mu:
        for st in h._reqs.values():              # site-like magnitudes of the sums before they are written
            st.first_mono = st.last_mono = CLOCK0
            if side == "D":     # D requests loaded from P: the first-candidate step's values are added (candidate rules)
                for cid in (("cand_lookup", "cand_alloc", "cand_other") if st.role == "engine" else ("cand_submit",)):
                    st.acc.setdefault(cid, pdf._Acc())
            for a in st.acc.values():
                a.total, a.steps = 12345678901, 1234
                if a.first is not None:
                    a.first = 12345678
            st.dirty = True
        h._flush_partials(force_all=True, reason="periodic")
        h._selfcheck()
    h.flush()
    sizes = collections.defaultdict(list)
    for f in sorted(glob.glob(os.path.join(d, "*", "*.log"))):
        for line in open(f, "rb"):
            payload = json.loads(line.split(b"[PD_DIAG] ", 1)[1])
            k = payload.get("event") + ("[%s]" % payload["scope"] if payload.get("scope") else "") + ("[role=%s]" % payload["role"] if payload.get("event") in ("pd_seg_partial",) else "")
            sizes[(os.path.basename(f).split(".")[0], k)].append(len(line))
    print("==", side, "counts", {k: v for k, v in h._counts.items() if v})
    for (fileprefix, k), v in sorted(sizes.items()):
        print("  %-7s %-40s n=%d bytes=%s" % (fileprefix, k, len(v), sorted(set(v))))
    return sizes

d_recs = [x["normalized"] for x in long["D_first"] + long["D_decode"]] + api
run("D", d_recs, "EngineCore_DP0")
p_recs = [x["normalized"] for x in long["P"]] + api
run("P", p_recs, "Worker_TP0")
