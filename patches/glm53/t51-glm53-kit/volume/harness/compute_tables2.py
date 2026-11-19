"""Guide-A log-volume tables, glm5.3 shape (P: TP16, 1 instance per container; D: TP1, 16 instances per container,
MTP k=5, async). Parameters are the coordinator's grid; every one is to be confirmed on site.
All byte counts are measured (results/filter_lines.txt, fit.json, per_request_api_engine.txt; t51-glm53-p2d
12_record_bytes.json; mooncake/replace/lab/lab_test.txt)."""
import math
H12 = 12 * 3600
DAY = 86400
GB, MB = 1e9, 1e6
# ---------------------------------------------------------------- filter files (NODE_LOG_DIR), lines incl. prefix
F_PREFIX_API, F_PREFIX_ENG, F_PREFIX_WRK = 84, 89, 85
API_RANGE, API_REQ_1 = (22570 - 2779) / 63, 2779          # api_summary[request]: + per detokenize range (cap 64)
CORE_RANGE, CORE_REQ_4 = (10128 - 2544) / 60, 2544        # D core_summary[request]: + per hash range (cap 64)
SELFCHECK = 4 * 921 / 60                                  # 4 files, one line a minute, per instance
def d_steps(r, B, t):            # steps a request stays on D (Little: B = r * T), t = step interval in s
    return B / (r * t)
def d_req_disk(r, B, t, hit=0, save=0):
    s = d_steps(r, B, t)
    api_req = API_REQ_1 + API_RANGE * (min(64, max(1, s)) - 1) + F_PREFIX_API
    core_req = CORE_REQ_4 + CORE_RANGE * (min(64, s + 1) - 4) + F_PREFIX_ENG
    fixed = 2214 + 2208 + (1088 + F_PREFIX_ENG)          # api http, engine_submission, p2d request_generation
    return fixed + api_req + core_req + hit * 3211 + save * 3647
D_PARTIAL = 1075 + 944                                   # engine + worker pd_seg_partial per 5-s window
def d_disk_rate(r, B, t, hit=0, save=0):                 # bytes/s per D instance
    return r * d_req_disk(r, B, t, hit, save) + D_PARTIAL * (B / 5 + r) + SELFCHECK
P_REQ_DISK = (2122 + 84) + (2085 + 84) + (2741 + 84) + 2379 + 3644   # api x3 (1 detok range), core, save
P_PARTIAL = 879 + 874
def p_disk_rate(r, b, hit=1):
    return r * (P_REQ_DISK + P_PARTIAL + hit * (3138 + 57 * b + F_PREFIX_WRK)) + SELFCHECK
# ---------------------------------------------------------------- vLLM log: [BatchPutTiming] (stderr, always)
BPT = 312
def p_bpt_rate(r, g=1):
    return r * 16 * g * BPT
def d_bpt_rate(r, g=1, save=1):
    return r * g * BPT * save
# ---------------------------------------------------------------- raw [PD_DIAG] (into vLLM's log if the panel-2
# logging config is not installed), message + 45 B vLLM prefix
PRE = 45
def d_step_raw(B):
    return (1718 + 504 * B) + (1613 + 57 * B) + (838 + 57 * B) + (1625 + 57 * B) + 4 * PRE + 60
def d_req_raw(r, B, t, hit=0, save=0):
    s = d_steps(r, B, t)
    api = 2130 + 2124 + API_REQ_1 + API_RANGE * (min(64, max(1, s)) - 1) + 3 * PRE
    core = CORE_REQ_4 + CORE_RANGE * (min(64, s + 1) - 4) + PRE
    p2d = 8491 + 3 * PRE                                  # one read task, 64 requests in the step (p2d table)
    return api + core + p2d + hit * (3125 + 1909 + 649 + 3 * PRE) + save * (3564 + PRE)
def p_step_raw(b, tp=16):
    save_submit = 1838 + 57 * b + 148 * min(b, 128)
    return (1747 + 505 * b) + tp * ((1621 + 57 * b) + (838 + 57 * b) + save_submit) + (1 + 3 * tp) * PRE + 60
def p_req_raw(b, tp=16):
    return (2130 + 2085 + 2741 + 3 * PRE) + (2291 + PRE) + tp * ((3138 + 57 * b + PRE) + (3565 + PRE))
def fmt(x):
    return ("%.2f" % x) if x < 100 else ("%.0f" % x)
R = (1, 10, 50); BS = (16, 64, 256); TS = (0.020, 0.050, 0.100); PS = (2, 5, 10); PB = (1, 4, 16)
out = []
w = out.append
w("## 1. 过滤器目录，每实例 12 小时（GB）\n\n### D（不命中缓存池、不保存）\n")
w("| 每实例每秒请求数 | 步间隔 | " + " | ".join("B=%d" % B for B in BS) + " |\n|---|---|" + "---|" * len(BS))
for r in R:
    for t in TS:
        w("| %d | %d ms | " % (r, t * 1000) + " | ".join(fmt(d_disk_rate(r, B, t) * H12 / GB) for B in BS) + " |")
w("\n### P（命中缓存池，按每请求 1 条 load 算；不命中时每请求少约 3.3 KB）\n")
w("| 每实例每秒请求数 | " + " | ".join("每步 %d 个请求" % b for b in PB) + " |\n|---|" + "---|" * len(PB))
for r in R:
    w("| %d | " % r + " | ".join(fmt(p_disk_rate(r, b) * H12 / GB) for b in PB) + " |")
w("\n## 2. vLLM 日志每天增长：[BatchPutTiming]（GB/天，g = 每请求保存的 KV cache 组数，按 1）\n")
w("| 每实例每秒请求数 | P 实例（TP16） | D 实例（保存时） | D 容器（16 实例，保存时） |\n|---|---|---|---|")
for r in R:
    w("| %d | %s | %s | %s |" % (r, fmt(p_bpt_rate(r) * DAY / GB), fmt(d_bpt_rate(r) * DAY / GB), fmt(16 * d_bpt_rate(r) * DAY / GB)))
w("\n## 3. 原始 [PD_DIAG]（GB/天；没装面板二日志配置时全部进 vLLM 日志）\n\n### D 每实例（每步部分 + 每请求部分）\n")
w("| 每实例每秒请求数 | 步间隔 | " + " | ".join("B=%d" % B for B in BS) + " |\n|---|---|" + "---|" * len(BS))
for r in R:
    for t in TS:
        w("| %d | %d ms | " % (r, t * 1000) + " | ".join(fmt((d_step_raw(B) / t + r * d_req_raw(r, B, t)) * DAY / GB) for B in BS) + " |")
w("\n### P 每实例（TP16）\n")
w("| 每秒步数 | 每步请求数 | 每秒请求数（= 步数 × 每步请求数） | GB/天 |\n|---|---|---|---|")
for s in PS:
    for b in PB:
        r = s * b
        w("| %d | %d | %d | %s |" % (s, b, r, fmt((s * p_step_raw(b) + r * p_req_raw(b)) * DAY / GB)))
w("\n## 4. 两个示例点（假设全部待现场确认）\n")
for name, (r, B, t), (ps, pb) in (("示例 A", (1, 64, 0.050), (10, 1)), ("示例 B", (10, 256, 0.020), (10, 4))):
    pr = ps * pb
    dd = d_disk_rate(r, B, t) * H12 / GB
    pdk = p_disk_rate(pr, pb) * H12 / GB
    w("- **%s**：D 每实例每秒 %d 个请求、B=%d、步间隔 %d ms、MTP k=5（D 上每请求 %.0f 步、停留 %.0f 秒）；P 每秒 %d 步、每步 %d 个请求（每秒 %d 个请求）、命中缓存池；g=1，D 不保存。"
      % (name, r, B, t * 1000, d_steps(r, B, t), B / r, ps, pb, pr))
    w("  - 过滤器目录 12 小时：D 容器（16 实例）%.1f GB + 上限 16 GiB；P 容器 %.1f GB + 上限 1 GiB" % (16 * dd, pdk))
    w("  - vLLM 日志每天（[BatchPutTiming]）：P 容器 %.1f GB；D 不保存时 0" % (p_bpt_rate(pr) * DAY / GB))
    w("  - 没装日志配置时 vLLM 日志每天再加：D 容器 %.0f GB，P 容器 %.0f GB" % (16 * (d_step_raw(B) / t + r * d_req_raw(r, B, t)) * DAY / GB, (ps * p_step_raw(pb) + pr * p_req_raw(pb)) * DAY / GB))
print("\n".join(out))
