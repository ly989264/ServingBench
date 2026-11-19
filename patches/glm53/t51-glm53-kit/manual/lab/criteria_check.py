"""Criteria of guide A (patch kit 207d7a4) and step 11 (Mooncake package 6d70978c), each with the exact phrases the
manual must keep, checked against the built manual. Prints one line per criterion and exits 1 if one is missing.
    python3 -I criteria_check.py <manual.md> <guide A 207d7a4.md> <step 11 6d70978c.md>
Every phrase is first checked in its source too, so a phrase that is not the source's wording is caught."""
import sys

manual, ga, g11 = (open(p, encoding="utf-8").read() for p in sys.argv[1:4])
C = [  # (id, where in the source, source, phrases)
    ("C01", "A 7.6", ga, ["有实例的 `/health` 不是 `200`，或者起不来"]),
    ("C02", "A 7.6", ga, ["没有设 `VLLM_LOGGING_CONFIG_PATH`"]),
    ("C03", "A 7.2", ga, ["`pd_diagnostics`、`pd_device_timing`、`worker_base`、`ascend_store`、`mooncake_connector`、`recompute_scheduler`"]),
    ("C04", "A 7.6", ga, ["D 的 forward、sampling、start_load_kv/wait_for_transfer 仍然全为空（第 3 批看 P 的 forward、sampling、schedule）"]),
    ("C05", "A 7.6", ga, ["`cand_maps` 是 0，或者 `no_execution_id` 大于 0"]),
    ("C06", "A 8", ga, ["`BASELINE_DIFF 端口:项:旧值->新值 …`", "kind、conn、spec、eager、async、load_async、tp、dp、调度器有变化"]),
    ("C07", "A 8", ga, ["三项有一项不是 `ok` 或 `ok(…)`", "`unpatched`、`absent`、`-`（这一项没有跑）、`BAD(…)`", "`mooncake=BAD(other)` 不算"]),
    ("C08", "A 8", ga, ["`run` 行是 `logcfg=unset`"]),
    ("C09", "A 8", ga, ["`site_patches` 和第 2 步不同"]),
    ("C10", "A 8", ga, ["`BalanceScheduler` 带 `balance=True`", "scheduler=vllm_ascend.patch.platform.patch_balance_schedule.BalanceScheduler balance=False recompute=False dyntra_lb=False batch_job=False profiling_chunk=False", "scheduler=vllm_ascend.core.recompute_scheduler.AsyncRecomputeScheduler balance=False recompute=True dyntra_lb=False batch_job=False profiling_chunk=False", "worker model_runner=vllm_ascend.worker.model_runner_v1.NPUModelRunner tp_rank=<n>"]),
    ("C11", "A 8", ga, ["`tp_rank=0` 是 `off`", "`tp_rank=0` 是 `?`", "`tp_rank=0` 是 `off` 或 `?`"]),
    ("C12", "A 7.3", ga, ["`this_batch` 是 1 的行"]),
    ("C13", "A 7.3", ga, ["`window_min` 明显大于 35"]),
    ("C14", "A 7.3", ga, ["`pid_list` 不同"]),
    ("C15", "A 7.3", ga, ["`ended_win` 大于 0", "`forward_win` 或 `sampling_win` 大于 0", "`ended_win` 是 0"]),
    ("C16", "A 7.3", ga, ["| 超过 1% | 没有增加 |", "| 不超过 1% | 增加了 |", "| 不超过 1% | 没有增加 |", "D：这一批失败", "P：带回"]),
    ("C17", "A 7.3", ga, ["`forward_empty_pct` = 1 − `forward_win` ÷ `alloc_win`", "比例可能是零点几的负数，按 0 看", "留空次数只看增没增加"]),
    ("C18", "A 7.6", ga, ["节点目录每天增长 × `NODE_LOG_KEEP_HOURS` ÷ 24，再加这台机器的实例数 × 1 GiB（D 机器 16 GiB，P 机器 1 GiB）"]),
    ("C19", "A 7.6", ga, ["vLLM 日志里有 `[PD_DIAG]` 行（一出现就处理，不等满 30 分钟）"]),
    ("C20", "A 7.6", ga, ["把 `[BatchPutTiming]` 每天的增长 × 现场 vLLM 日志保留的天数也加进上面那一条的预算，和同一个 `Avail` 的一半比"]),
    ("C21", "A 7.6", ga, ["`[BatchPutTiming]` 每台机器每天超过 43.1 GB（D 不往缓存池保存时应当是 0 行）"]),
    ("C22", "A 7.2", ga, ["`命令行和重启前相同`"]),
    ("C23", "A 7.5", ga, ["大 10% 以上"]),
    ("C24", "A 7.1", ga, ["`True <用时> s <峰值内存> MiB`"]),
    ("C25", "A 7.4", ga, ["`起点已记下：<n> 行`", "D 机器 18，P 机器 3"]),
    ("C26", "A 2", ga, ["`site_patches patch/__init__.py=<16 位> patch/platform/__init__.py=<16 位> patch/platform/patch_kv_load_failure_reason.py=d7c764bb425993c0 patch/platform/patch_prefill_length_validation.py=c36392a6338bc2f1`", "baseline_token="]),
    ("C27", "A 3", ga, ["`2c4acb3eeb1ced7d`", "`d7c764bb425993c0`", "`c36392a6338bc2f1`"]),
    ("C28", "A 4.3", ga, ["p2d_extra X1=1 X2=1/1 X3=2 X4=4/1 X5=1 X6=ok", "same_as_replacement=False", "replay_p2d: check ok: 28 of 28 changes located, result checks passed, nothing written"]),
    ("C29", "A 8", ga, ["`state=v0.30.0+port`", "`port=25/25`", "`port=11/11`", "p2d=ok(changes=28/28,missing=none,struct=ok,extra=same,replacement_file=0)", "`baseline=same`", "`logcfg=set`"]),
    ("C30", "A 7.6", ga, ["明显大于 0 时带回来"]),
    ("C31", "A 7.6", ga, ["`check` 不是 `RESULT: OK`，或者这一批的实例不是 `…-19c` 那一版"]),   # kept for the last batch; per batch see appendix B
    ("M01", "11.2", g11, ["`mc_pre result=ok_replace`", "replacement_vs_site=-0/+3(expected -0/+3)", "`consumers_ok n_elf=<n> removed_names=0`", "`mc_pre result=STOP_missing`"]),
    ("M02", "11.3", g11, ["本容器已经做过第 11.4 节的，这一条不适用，不要重跑第 11.3 节"]),
    ("M03", "11.4", g11, ["`3131784385846064`", "`15402248`", "(deleted)"]),
    ("M04", "11.5", g11, ["`<Mooncake 目录>/store.cpython-311-aarch64-linux-gnu.so <用时> s`", "`undefined symbol`", "`libascend_hal.so: cannot open shared object file`"]),
    ("M05", "11.5", g11, ["port=<端口> new", "port=<端口> old"]),
    ("M06", "11.6", g11, ["changed_since_install=1 timing=2 store=replacement", "changed_since_install=0 timing=0 store=site_original", "现场第 1、2 次检测"]),
    ("M07", "11.7", g11, ["`complete=false` 不是 0"]),
    ("M08", "11.9", g11, ["`3bf82f8b0c5ab715`", "`17610953`"]),
]
# the phrases the manual rewords on purpose (step numbers): checked in the source with the source's numbering
RENUMBERED = {"`site_patches` 和第 2 步不同": "`site_patches` 和第 3 步不同",
              "本容器已经做过第 11.4 节的，这一条不适用，不要重跑第 11.3 节": "本容器已经做过第 30 步的，这一条不适用，不要重跑第 29 步"}
bad = 0
for cid, where, src, phrases in C:
    miss_src = [p for p in phrases if p not in src]
    miss = [p for p in phrases if RENUMBERED.get(p, p) not in manual]
    ok = not miss and not miss_src
    bad += not ok
    print("%s %-6s %-5s %s%s%s" % (cid, where, "ok" if ok else "MISS", "; ".join(p[:40] for p in phrases)[:110],
          "" if not miss else "  missing in manual: " + " | ".join(m[:60] for m in miss),
          "" if not miss_src else "  not in source: " + " | ".join(m[:60] for m in miss_src)))
print("criteria %d, missing %d" % (len(C), bad))
sys.exit(1 if bad else 0)
