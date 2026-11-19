# T41 包二 v3：rc1 接入的测试

测的是 v3 的 `segments.json` 和 mock：方案（`docs/tasks/T40_包二v3方案.zh-CN.md` 第一节）里填上的 8 个段、留空的 9 个段，以及 mock 写出的新记录。功能测试，不用 perflock。

## 怎么跑

单元测试（不起容器，约 2 秒）：

```bash
python3 -I tests/t41/test_segments_mock.py
```

端到端（一次性容器 Vector 0.61.0、ClickHouse 26.8.36.3，`--rm`，结束时删掉；每次约 2 分钟）。`<pack>` 是面板二目录的一份副本（测试会在里面生成 `generated/`），`<work>` 是一个空目录：

```bash
python3 -I tests/t41/e2e_v3.py <pack> <work> mock
```

```bash
python3 -I tests/t41/e2e_v3.py <pack> <work> replay <P.log> <D.log>
```

- `mock`：mock 在模拟时间里写 P0、D0，经包里的过滤器、采集器写进 ClickHouse，单请求 JSON 的每个值和 mock 的真值比。
- `replay`：一份 P 侧日志当 P0、它对应的 D 侧日志当 D0（`tools/check_pd_diag.py` 拆分，两侧用同一个时间平移），每个值和直接从日志算出的值比。真实日志只在本机，不进仓库；结果里只记段名、数值和请求键的末尾几位。
- 最后一行 `0 FAIL` 为通过。`各段有值的请求数` 一行列出每个段在多少个请求里有值，留空的段应为 0。

## 文件

- `test_segments_mock.py`：段树和用户名单一致；8 段的来源、9 段为空；缓存池记录只留 TP rank 0；mock 写出补丁的 9 种记录和形状（排队到首次候选、lookup 两次调用、hash 分次、worker 的 submit 和 wait_for_save、首次候选那一步不带请求号、共用的保存任务不带 request_id）；过滤器只写段要读的那几种。
- `e2e_v3.py`：从 demo 仓库 `tests/t29/e2e_local.py` 改来，加了 v3 的段和两份日志的回放。
- `tools/check_pd_diag.py`、`tools/check_pd_diag_format.py`：demo 仓库 `tools/` 的副本，用于回放时拆分日志。这份格式检查只认 v2.1 的 7 种记录，回放 rc1 时会报 pool_task、pd_transfer_summary 为 E2，不影响拆分。

## 2026-11-16 的结果

- 单元测试 13 项通过。
- `mock`：164 个请求、4920 个值和真值比，差 0；看板 130 次查询，出错 0。
- `replay`（rc1 的 P.log、D.log 副本）：3 个请求、114 个值和日志直接算的值比，差 0；8 个新段的值和评估第二节的实测值一致，9 个留空的段为空。
