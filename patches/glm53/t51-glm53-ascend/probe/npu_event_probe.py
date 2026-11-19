"""Can torch.npu events time an aclgraph replay?  One NPU, small tensors, a few seconds.

OCCUPIES ONE NPU. Run it only on an idle card (check `npu-smi info` first), never inside a serving
container and never on a card a serving process is using. Pick the card with ASCEND_RT_VISIBLE_DEVICES.
No model, no vLLM.

    ASCEND_RT_VISIBLE_DEVICES=<idle card> python3 npu_event_probe.py [--iters 20] [--size 2048] [--ops 32] [--no-graph]

Per iteration, on one stream: record start -> replay -> record end; poll end.query() until done, then
start.elapsed_time(end). Compared with (a) perf_counter around replay + torch.npu.synchronize() and
(b) perf_counter around replay alone (host launch time). Prints the three medians in ms and a verdict.
Exit code 0: device value close to the synchronized value; 2: not close; 1: error.
"""

import argparse
import statistics
import sys
import time

import torch
import torch_npu  # noqa: F401  (registers torch.npu)

parser = argparse.ArgumentParser()
parser.add_argument("--iters", type=int, default=20)
parser.add_argument("--size", type=int, default=2048)
parser.add_argument("--ops", type=int, default=32, help="matmuls per replay")
parser.add_argument("--no-graph", action="store_true", help="time the eager op sequence instead of aclgraph")
args = parser.parse_args()

torch.npu.set_device(0)
x = torch.randn(args.size, args.size, dtype=torch.float16, device="npu")
w = torch.randn(args.size, args.size, dtype=torch.float16, device="npu") * 0.01


def ops():
    y = x
    for _ in range(args.ops):
        y = torch.matmul(y, w)
    return y


stream = torch.npu.current_stream()
run, mode = ops, "eager op sequence"
if not args.no_graph:
    try:
        side = torch.npu.Stream()
        side.wait_stream(stream)
        with torch.npu.stream(side):  # warm up outside the capture, as CUDA-graph capture requires
            for _ in range(3):
                ops()
        stream.wait_stream(side)
        graph = torch.npu.NPUGraph()
        with torch.npu.graph(graph):
            static_out = ops()
        run, mode = graph.replay, "aclgraph replay"
    except Exception as exc:  # fall back to the fixed op sequence
        print(f"aclgraph capture failed ({type(exc).__name__}: {exc}); timing the eager op sequence")

for _ in range(3):
    run()
torch.npu.synchronize()
start, end = torch.npu.Event(enable_timing=True), torch.npu.Event(enable_timing=True)  # reused every iteration
device_ms, synced_ms, launch_ms, not_ready = [], [], [], 0
for _ in range(args.iters):
    torch.npu.synchronize()
    start.record(stream)
    t0 = time.perf_counter()
    run()
    launch_ms.append((time.perf_counter() - t0) * 1e3)
    end.record(stream)
    not_ready += 0 if end.query() else 1  # still running on the device when the host returned
    while not end.query():  # deferred read: never block inside elapsed_time
        time.sleep(0.0001)
    device_ms.append(start.elapsed_time(end))
    torch.npu.synchronize()
    t0 = time.perf_counter()
    run()
    torch.npu.synchronize()
    synced_ms.append((time.perf_counter() - t0) * 1e3)

device, synced, launch = (statistics.median(v) for v in (device_ms, synced_ms, launch_ms))
close = abs(synced - device) <= max(0.3, 0.25 * synced)
print(f"torch {torch.__version__}, torch_npu {torch_npu.__version__}, {torch.npu.get_device_name(0)}, {mode}")
print(f"iterations {args.iters}, size {args.size}, matmuls {args.ops}; end event not yet done right after launch: "
      f"{not_ready}/{args.iters}")
print(f"device (event elapsed_time)        median {device:.3f} ms")
print(f"host perf_counter + synchronize    median {synced:.3f} ms")
print(f"host launch only (no synchronize)  median {launch:.3f} ms")
print(f"device vs synchronized: {'CLOSE' if close else 'NOT CLOSE'} "
      f"(|diff| {abs(synced - device):.3f} ms, allowed {max(0.3, 0.25 * synced):.3f} ms)")
sys.exit(0 if close else 2)
