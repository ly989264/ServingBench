# Mooncake（替换库待现场核对后交付）

这个目录现在只有本说明。Mooncake 的替换是操作说明 A 第 11 步，可以以后单独做；第 1–10 步不依赖它。

- **不做第 11 步时：** P 侧「后台 wait_for_save → put」下面的 3 个子段（submit、wait_for_transfer、finalize）为空，父段 put 仍有值；P 合计、TTFT 拆解和 D 侧都不受影响。47 段里 42 段有值，做了第 11 步以后 45 段。
- **替换库何时交付：** 替换件（打了补丁 04 的 `libmooncake_store.so`）、替换前的只读核对脚本和校验表，不放进这个补丁包，以后单独出一个 Mooncake 增量包：替换件、替换前的只读核对（precheck）、SHA256SUMS、第 11 步的完整操作和回滚方法。替换件和 precheck 定稿以后才出。
