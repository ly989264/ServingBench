# NPU 设备事件计时探测：`npu_event_probe.py`

用途：在有昇腾卡的机器上确认 torch_npu（目标 2.10）下，`torch.npu.Event(enable_timing=True)` 能给一次 aclgraph replay 计时，并且量出的设备时长和「host 计时 + 同步」量出的时长接近。这是改用设备事件计时之前的前提。用户还没定要不要改，这个脚本先备着。

## 会占用一张 NPU

- 只在**空闲的卡**上跑。先用 `npu-smi info` 确认这张卡上没有进程、HBM 占用接近 0。
- **不要在正在服务的容器里跑，也不要选正在服务的卡**：它会和服务抢算力和显存，影响线上请求。
- 用 `ASCEND_RT_VISIBLE_DEVICES=<卡号>` 指定那张空闲的卡；脚本里固定用可见设备 0。
- 不加载模型、不碰 vLLM。张量是 2048×2048 的 fp16（每个 8 MB，共两个），每次 replay 做 32 次矩阵乘，默认 20 轮，预计几秒内结束（没在卡上跑过，时长是估计）。

## 运行

```bash
ASCEND_RT_VISIBLE_DEVICES=<空闲卡号> python3 npu_event_probe.py
```

可选参数：`--iters`（轮数，默认 20）、`--size`（矩阵边长，默认 2048）、`--ops`（每次 replay 的矩阵乘次数，默认 32）、`--no-graph`（不用 aclgraph，直接计时同一段算子序列）。

## 做了什么

1. 在一条流（当前流）上，先在旁路流上预热 3 次，再用 `torch.npu.NPUGraph` + `torch.npu.graph` 捕获一段固定的矩阵乘序列。捕获失败时打印原因，改为计时同一段算子序列（eager）。
2. 每一轮：`start.record(stream)` → replay → `end.record(stream)`；先 `end.query()`，没完成就短暂等待后再查，完成后才调 `start.elapsed_time(end)`，不在没完成的事件上调用。两个事件每轮复用，不新建。
3. 同一轮另做两次 host 计时：`perf_counter` 包住 replay（只算发射）；`perf_counter` 包住 replay 再加 `torch.npu.synchronize()`（发射 + 设备执行 + 同步）。

## 输出

```
torch <版本>, torch_npu <版本>, <设备名>, aclgraph replay
iterations 20, size 2048, matmuls 32; end event not yet done right after launch: <n>/20
device (event elapsed_time)        median <ms>
host perf_counter + synchronize    median <ms>
host launch only (no synchronize)  median <ms>
device vs synchronized: CLOSE|NOT CLOSE (|diff| <ms>, allowed <ms>)
```

- **三个数**：设备事件时长、host 计时加同步、只算 host 发射，都是 20 轮的中位数，单位 ms。
- **判断**：`|同步值 − 设备值| ≤ max(0.3 ms, 同步值 × 25%)` 记为 CLOSE。同步值多出的部分是发射和同步本身的开销，所以设备值应当略小于同步值。
- `end event not yet done right after launch`：host 发射完立刻查询时，结束事件还没完成的轮数。它大于 0，说明 host 返回时设备还在算，host 侧量到的确实只是发射；这正是要改用设备计时的原因。
- **退出码**：0 为 CLOSE，2 为 NOT CLOSE，1 为出错（例如 torch_npu 不支持带计时的事件）。

请把整段输出原样带回。

## 本地验证到哪一步

本机没有昇腾卡。只在 CPU 上用假的 `torch.npu`（事件用 `perf_counter` 冒充）把三条代码路径各跑了一遍：aclgraph 路径、捕获失败回退到算子序列、`--no-graph`。这些运行只说明脚本能跑通，打印的数字没有意义。真实结果要在卡上跑。
