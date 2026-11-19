"""Guide-A log-volume tables from the measured line sizes (bytes). Prints markdown."""
KEEP_H = 12
S12 = KEEP_H * 3600
GB = 1e9
# ---- (a) filter files in NODE_LOG_DIR, measured lines incl. the filter's prefix and newline (filter_sim.py)
P_REQ = {"api_summary[http]": 2122 + 84, "api_summary[engine_submission]": 2085 + 84, "api_summary[request]": 2741 + 84,
         "core_summary[request]": 2379, "pool_task[save]": 3644}
P_PARTIAL = 879 + 874            # engine + worker pd_seg_partial, per 5-s window
P_LOAD = lambda b: 3138 + 57 * b + 85   # pool_task[load] (pool hit), b = requests in the P step
D_REQ = {"api_summary[http]": 2214, "api_summary[engine_submission]": 2208, "api_summary[request]": 22654,
         "core_summary[request]": 10217, "pd_transfer_summary[request_generation]": 1088 + 89}
D_PARTIAL = 1075 + 944           # with the first-candidate step's values
D_LOAD, D_SAVE = 3211, 3647
SELFCHECK = 4 * 921 / 60         # 4 files x one line a minute
def p_rate(r, hit, b=1):
    return r * (sum(P_REQ.values()) + P_PARTIAL + hit * P_LOAD(b)) + SELFCHECK
def d_rate(r, B, hit=0, save=0):
    return r * (sum(D_REQ.values()) + hit * D_LOAD + save * D_SAVE) + D_PARTIAL * (B / 5 + r) + SELFCHECK
print("P per request:", sum(P_REQ.values()) + P_PARTIAL, "+ pool hit", P_LOAD(1))
print("D per request:", sum(D_REQ.values()) + D_PARTIAL, "; per running request per second", D_PARTIAL / 5)
print("\n### P (per P instance), GB per %d h" % KEEP_H)
print("| req/s | no pool hit | pool hit, 1 req/step | pool hit, 8 req/step |\n|---|---|---|---|")
for r in (0.5, 1, 2, 4, 8):
    print("| %g | %.2f | %.2f | %.2f |" % (r, p_rate(r, 0) * S12 / GB, p_rate(r, 1) * S12 / GB, p_rate(r, 1, 8) * S12 / GB))
print("\n### D (per D instance), GB per %d h (no pool hit, D does not save)" % KEEP_H)
Bs = (16, 64, 128, 256)
print("| req/s | " + " | ".join("B=%d" % B for B in Bs) + " |\n|---|" + "---|" * len(Bs))
for r in (0.25, 0.5, 1, 2, 4):
    print("| %g | " % r + " | ".join("%.2f" % (d_rate(r, B) * S12 / GB) for B in Bs) + " |")
print("D extra per request: pool hit +%d B, D saves +%d B" % (D_LOAD, D_SAVE))
# ---- (b) [BatchPutTiming] in the vLLM log file (stderr): 290 B measured (keys=4 ...), 312 B at site widths
BPT = 312
print("\n### [BatchPutTiming], MB per day in the vLLM log (g = KV cache groups saved per request)")
print("| req/s | P (TP16, g=1) | P (TP16, g=2) | D saves (g=1) |\n|---|---|---|---|")
for r in (0.5, 1, 2, 4, 8):
    print("| %g | %.0f | %.0f | %.0f |" % (r, r * 16 * BPT * 86400 / 1e6, r * 32 * BPT * 86400 / 1e6, r * BPT * 86400 / 1e6))
# ---- (c) raw [PD_DIAG] emitted into vLLM's logging (before the filter), message + 45 B vLLM prefix
PRE = 45
def d_step(B):
    return (1718 + 504 * B) + (1613 + 57 * B) + (838 + 57 * B) + (1625 + 57 * B) + 4 * PRE + 60
def p_step(b, tp=16):
    save_submit = 1838 + 57 * b + 148 * min(b, 128)
    return (1747 + 505 * b) + tp * ((1621 + 57 * b) + (838 + 57 * b) + save_submit) + (1 + 3 * tp) * PRE + 60
print("\n### raw per step (KB): D TP1 decode step with B running; P TP16 step with b requests")
print("| B or b | D step | P step |\n|---|---|---|")
for n in (1, 8, 16, 64, 128, 256):
    print("| %d | %.1f | %.1f |" % (n, d_step(n) / 1e3, p_step(n) / 1e3))
print("D raw MB/s at 20 steps/s, B=64: %.2f; B=256: %.2f" % (20 * d_step(64) / 1e6, 20 * d_step(256) / 1e6))
