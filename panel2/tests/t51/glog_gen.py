#!/usr/bin/env python3
"""Lab only (T51): a vLLM stand-in's standard error with glog lines. Every 0.2 s one line goes to stderr (docker logs)
and to /var/log/vllm/stderr.log inside the container: [BatchPutTiming] lines (sequence number in
phase1_start_batch_put_us, thread 4242), every fifth an [BatchUpsertTiming], and ordinary vLLM lines between them."""
import datetime, os, sys, time
os.makedirs("/var/log/vllm", exist_ok=True)
f = open("/var/log/vllm/stderr.log", "a", buffering=1)
seq = 0
while True:
    seq += 1
    now = datetime.datetime.now()
    pre = "I%s %6d client_service.cpp:3327] " % (now.strftime("%m%d %H:%M:%S.%f"), 4242)
    kind = "BatchUpsertTiming" if seq % 5 == 0 else "BatchPutTiming"
    line = (pre + "[%s] keys=4 bytes=4096 phase1_start_batch_put_us=%d phase2_submit_transfers_us=20 phase3_wait_transfers_us=30 "
            "phase4_finalize_batch_put_us=40 total_us=%d timing_scope=normal_full status=ok complete=true failed_keys=0") % (kind, seq, seq + 90)
    noise = "(EngineCore_DP0 pid=7) INFO %s [core.py:123] seq %d: an ordinary vLLM line, not a timing line" % (now.strftime("%m-%d %H:%M:%S"), seq)
    for l in (line, noise):
        sys.stderr.write(l + "\n"); sys.stderr.flush()
        f.write(l + "\n")
    time.sleep(0.2)
