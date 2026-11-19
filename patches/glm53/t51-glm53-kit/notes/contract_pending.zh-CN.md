# glm5.3 约定：下一次修订时一起改的事项

（dsv4.1 那份约定不动。）

1. **C15 之后 host 侧时长保留**（11-19，统筹转来）。第 1 节 C14 那一段之后加一句：「C15 之后，`stages.forward`、`stages.sampling` 的 host 侧 `duration_ns` 保留不删，只是不再作为段的取值来源。过滤器要靠这两个对象在不在，来统计留空次数（acc_fallback 的 left_empty 分支），删了以后，判据会在不报错的情况下失效。」第 6 节差异表和修订记录同步。
2. **`[BatchPutTiming]` 的第三种形式**（11-19，补丁 04 方案 (i)）。Mooncake 计时行一节加一句：「原签名的 `BatchWriteWhenPreferSameNode(ops, is_upsert)` 保留为包装，它不知道调用前的两个时刻，写出的计时行只有 `phase2_submit_transfers_us`、`phase3_wait_transfers_us`、`phase4_finalize_batch_put_us`，没有 `phase1_start_batch_put_us` 和 `total_us`，并带 `complete=false`。拉取脚本对缺的字段不写对应的 stage，面板上该段为空，不是 0。`complete` 只用来标出这种行，不作为指标展示。现有代码里没有调用这个包装的地方。」修订记录同步。
3. **glog 前缀带年份**（11-19；面板二拉取脚本已在 7ef24eb 改为两种都认）。现场 Mooncake 自带的 glog 0.6.0 在前缀里写年份（`I20261119 08:36:03.863473  1664 file:line]`）。约定里计时行的前缀格式要写成两种都认：`I` 后面可选 4 位年份，再接 `MMDD`。
