#!/usr/bin/env python3
"""T56: the lab stand-in's sample with the T56 records (docs/tasks/T56/20_ section 6.2): the pack's
samples/pd032_full_P0_D0.log.gz with what tests/t56/t56_records.py adds: 16 reading tasks for each request that read
from P, the three maxima and the envelope on its request_generation and a rank-1 copy, kv_ready_wait, the four runner
stages, step_device_span_ns, cudagraph_runtime_mode. One request has no envelope; no equation is broken on purpose (that
is e2e_t56's part).

  python3 tests/t56/make_sample.py <pack> <out folder>

Writes <out folder>/pd032_full_P0_D0.log.gz, the name the stand-in reads (gzip without a time stamp: the same input gives
the same bytes); the lab takes it with SAMPLES=<out folder> (tests/t33/lab33.sh). fake_vllm.py itself is not changed. It
replays the sample by process kind on P and D alike, so in the lab P's stand-in writes the new fields too; the segment
definitions read them on D's side only.
"""
import gzip
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import t56_records  # noqa: E402


def main():
    pack, out = sys.argv[1], sys.argv[2]
    src = os.path.join(pack, "samples", "pd032_full_P0_D0.log.gz")
    with gzip.open(src, "rt", errors="replace") as f:
        lines = f.readlines()
    rg = []
    for line in lines:
        _, o = t56_records.parse(line)
        if o and o.get("event") == "pd_transfer_summary" and o.get("scope") == "request_generation" and o.get("tp_rank", 0) == 0:
            rg.append(o["request_id"])
    out_lines, info = t56_records.augment(lines, {"env_null": rg[0] if rg else None})
    os.makedirs(out, exist_ok=True)
    dst = os.path.join(out, "pd032_full_P0_D0.log.gz")
    with open(dst, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as f:
        f.write("".join(out_lines).encode("utf-8"))
    n_task = sum(len(v) for v in info["tasks"].values())
    print("样本：%s（%d 行）-> %s（%d 行）" % (src, len(lines), dst, len(out_lines)))
    print("从 P 读 KV 的请求 %d 个，读取任务 %d 条，rank 1 的 request_generation %d 条，等调度 %d 个请求，Worker 新 stage %d 处，"
          "step_device_span_ns %d 条；没有包络的请求：%s" % (len(info["rg"]), n_task, info["rank1"], len(info["kv_ready_wait"]),
                                                       info["worker_stages"], info["spans"], info["env_null"]))
    bad = []
    for rid, ts in info["tasks"].items():
        g = info["rg"][rid]
        env = g["kv_load_envelope_ns"]
        if env is None:
            continue
        av = lambda t, k: ((t.get("associated_stages") or {}).get(k) or {}).get("duration_ns") or 0
        if env < g["transfer_envelope_ns"] or env < max(g[f] for _, f in t56_records.F3) \
                or any(env < (t.get("queue_wait_ns") or 0) + av(t, "meta_fetch") + av(t, "prepare")
                       + (((t.get("stages") or {}).get("wait_for_transfer") or {}).get("duration_ns") or 0) for t in ts) \
                or env > max(t["end_ns"] for t in ts) - min(t["queued_ns"] for t in ts):
            bad.append(rid)
    print("10_ 第 3 节的三条不等式和「包络 ≥ 3 个最大值」：%d 个请求里不满足的 %d 个%s" % (len(info["rg"]), len(bad), "：" + "、".join(bad[:5]) if bad else ""))
    print("sha256 %s" % hashlib.sha256(open(dst, "rb").read()).hexdigest())
    print(json.dumps({"violators": info["violators"]}))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
