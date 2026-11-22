#!/usr/bin/env python3
"""T58: scripts/native_put.py of v3.3-extra on a synthetic mirror: Mooncake's own files (W1's T58 design, part 甲: "[PD_DIAG] "
plus JSON, mc_batch_put / mc_batch_upsert with submitted_keys, submitted_bytes, native_tid, wall_ns, pid) paired with
pool_task[save] records of pool.<start>.<pid>.<seq>.log. Prints one line per check and "N FAIL" at the end.
Usage: test_native_put_v33.py <panel pack dir>"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(sys.argv[1], "scripts"))
import native_put  # noqa: E402

FAILS = []


def say(ok, what):
    print("%s  %s" % ("OK  " if ok else "FAIL", what))
    if not ok:
        FAILS.append(what)


def node_line(e, proc="worker", pid=4242):
    return "2026-11-20T20:00:00.000+08:00 (%s pid=%d) INFO [x.py:0] [PD_DIAG] %s\n" % (proc, pid, json.dumps(e, separators=(",", ":")))


def mc_line(e):
    return "[PD_DIAG] %s\n" % json.dumps(e, separators=(",", ":"))


def call(tid, start, end, keys, nbytes, op="batch_put"):
    return {"operation": op, "native_tid": tid, "start_wall_ns": start, "end_wall_ns": end, "submitted_keys": keys,
            "submitted_bytes": nbytes}


def task(rid, calls, tid=7, pid=4242, rec_id="r"):
    return {"event": "pool_task", "version": 2, "scope": "task", "operation": "save", "request_id": rid, "record_id": rec_id,
            "task_id": "t-" + rid, "native_calls": calls}


def timing(pid, tid, keys, nbytes, wall, p=(1, 2, 3, 4), ev="mc_batch_put"):
    return {"event": ev, "version": 1, "pid": pid, "native_tid": tid, "submitted_keys": keys, "submitted_bytes": nbytes,
            "wall_ns": wall, "timing_scope": "normal_full", "status": "ok", "complete": True,
            "phase1_start_batch_put_us": p[0], "phase2_submit_transfers_us": p[1], "phase3_wait_transfers_us": p[2],
            "phase4_finalize_batch_put_us": p[3]}


def append(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(text)


def outputs(pull):
    recs = []
    for root, _, files in os.walk(os.path.join(pull, "_native")):
        for f in sorted(files):
            for line in open(os.path.join(root, f)):
                e = json.loads(line[line.index("[PD_DIAG] ") + 10:])
                if e.get("event") == "pd_native_put":
                    recs.append((os.path.relpath(root, os.path.join(pull, "_native")), e))
    return recs


def main():
    base = tempfile.mkdtemp(prefix="t58np-")
    try:
        pull, state = os.path.join(base, "pull"), os.path.join(base, "state")
        os.makedirs(state)
        pool = os.path.join(pull, "n-P0", "P0", "pool.20261120T200000.4242.0000.log")
        mc = os.path.join(pull, "n-P0", "mooncake", "mc.20261120T200000.4242.0000.log")
        # what is in the mirror before the puller starts: skipped (the first round starts at the end)
        append(pool, node_line(task("old", [call(7, 0, 10, 1, 1)])))
        append(mc, mc_line({"event": "pd_file_open", "version": 1, "source": "mooncake", "pid": 4242}) + mc_line(timing(4242, 7, 1, 1, 5)))
        nat = native_put.Puller({"NODE_LOG_KEEP_HOURS": "12"}, pull, state)
        nat.run_once()
        c = nat.assoc.counts
        say(c["save_records"] == 0 and c["native_lines"] == 0 and c["mc_files"] == 1,
            "第一轮从文件末尾读：之前的任务和计时行都不读（save_records %d，native_lines %d，mc_files %d）" % (c["save_records"], c["native_lines"], c["mc_files"]))

        # 1. a task of two calls, both lines there; mc_timing_stats and pd_file_open lines are not timing lines
        append(mc, mc_line({"event": "mc_timing_stats", "written": 3, "dropped": 0}) + mc_line(timing(4242, 7, 4, 400, 1500, (1, 10, 20, 30)))
               + mc_line(timing(4242, 7, 2, 200, 2500, (1, 11, 21, 31))))
        append(pool, node_line(task("req-a", [call(7, 1000, 2000, 4, 400), call(7, 2000, 3000, 2, 200)], rec_id="ra")))
        nat.run_once()
        out = outputs(pull)
        ok = len(out) == 1 and out[0][0] == os.path.join("n-P0", "P0")
        e = out[0][1] if out else {}
        st = e.get("stages", {})
        say(ok and st.get("submit", {}).get("duration_ns") == 21000 and st.get("wait_for_transfer", {}).get("duration_ns") == 41000
            and st.get("finalize", {}).get("duration_ns") == 61000 and e.get("native_calls") == 2 and e.get("request_id") == "req-a",
            "两次调用的任务：配上，写出一条 pd_native_put，submit / wait_for_transfer / finalize 是两行之和（21 / 41 / 61 µs）")
        say(nat.assoc.counts["lines_unparsed"] == 0, "mc_timing_stats、pd_file_open 不算读不出的行")

        # 2. two lines fit (same keys, both in the window): bytes tells them apart
        append(mc, mc_line(timing(4242, 7, 3, 300, 5500, (1, 5, 5, 5))) + mc_line(timing(4242, 7, 3, 333, 5600, (1, 9, 9, 9))))
        append(pool, node_line(task("req-b", [call(7, 5000, 6000, 3, 333)], rec_id="rb")))
        nat.run_once()
        out = [x for x in outputs(pull) if x[1]["request_id"] == "req-b"]
        say(len(out) == 1 and out[0][1]["stages"]["submit"]["duration_ns"] == 9000, "两行都合适时按字节数挑：挑中 333 字节那一行")

        # 3. one line fits by keys and time but bytes differ: matched, counted
        before = nat.assoc.counts["bytes_mismatch"]
        append(mc, mc_line(timing(4242, 7, 5, 999, 8500)))
        append(pool, node_line(task("req-c", [call(7, 8000, 9000, 5, 500)], rec_id="rc")))
        nat.run_once()
        out = [x for x in outputs(pull) if x[1]["request_id"] == "req-c"]
        say(len(out) == 1 and nat.assoc.counts["bytes_mismatch"] == before + 1, "只有一行合适、字节数不同：照样配上，bytes_mismatch 加 1")

        # 4. another process's line (pid) never matches; both expire and are counted
        append(mc, mc_line(timing(5555, 7, 6, 600, 10500)))
        append(pool, node_line(task("req-d", [call(7, 10000, 11000, 6, 600)], rec_id="rd")))
        nat.run_once()
        say(not [x for x in outputs(pull) if x[1]["request_id"] == "req-d"], "别的进程（pid 不同）的行不配")
        nat.assoc.expire(now=10 ** 9)
        say(nat.assoc.counts["tasks_incomplete"] >= 1 and nat.assoc.counts["lines_unmatched"] >= 1,
            "过了窗口：任务记 tasks_incomplete，行记 lines_unmatched（%d、%d）" % (nat.assoc.counts["tasks_incomplete"], nat.assoc.counts["lines_unmatched"]))

        # 5. the line comes a round after the task
        append(pool, node_line(task("req-e", [call(8, 20000, 21000, 1, 100)], rec_id="re")))
        nat.run_once()
        say(not [x for x in outputs(pull) if x[1]["request_id"] == "req-e"], "行还没来：先不写")
        append(mc, mc_line(timing(4242, 8, 1, 100, 20500)))
        nat.run_once()
        say(len([x for x in outputs(pull) if x[1]["request_id"] == "req-e"]) == 1, "下一轮行来了：配上")

        # 6. a broken timing line is counted; wall_ns outside the call does not match
        before = nat.assoc.counts["lines_unparsed"]
        append(mc, '[PD_DIAG] {"event":"mc_batch_put","version":1,"pid":4242}\n' + mc_line(timing(4242, 9, 1, 100, 99999)))
        append(pool, node_line(task("req-f", [call(9, 30000, 31000, 1, 100)], rec_id="rf")))
        nat.run_once()
        say(nat.assoc.counts["lines_unparsed"] == before + 1, "缺字段的计时行：记 lines_unparsed")
        say(not [x for x in outputs(pull) if x[1]["request_id"] == "req-f"], "wall_ns 不在调用的起止之间：不配")

        # 7. a machine with several log folders (<machine>/d0/...), and upsert
        pool2 = os.path.join(pull, "n-P1", "d0", "P1", "pool.20261120T200000.777.0000.log")
        mc2 = os.path.join(pull, "n-P1", "d0", "mooncake", "mc.20261120T200000.777.0000.log")
        append(mc2, mc_line(timing(777, 3, 2, 20, 40500, ev="mc_batch_upsert")))
        append(pool2, node_line(task("req-g", [call(3, 40000, 41000, 2, 20, op="batch_upsert")], rec_id="rg"), pid=777))
        nat.run_once()
        out = [x for x in outputs(pull) if x[1]["request_id"] == "req-g"]
        say(len(out) == 1 and out[0][0] == os.path.join("n-P1", "P1"), "<机器>/d0/mooncake/ 的文件和 <机器>/d0/<实例>/ 的任务：配上，写到 _native/n-P1/P1")
        say(nat.status()["counts"]["mc_files"] == 2, "status 里 mc_files 是镜像里 Mooncake 文件的个数（2）")
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print("%d FAIL" % len(FAILS))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
