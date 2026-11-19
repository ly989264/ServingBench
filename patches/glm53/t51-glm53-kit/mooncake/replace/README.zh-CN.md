# glm5.3 Mooncake 替换库（补丁 04）

## 1. 替换哪个文件

只替换一个文件：`<site-packages>/mooncake/libmooncake_store.so`。其他文件都不动：store 模块、`libtransfer_engine.so`、`libmooncake_common.so`、`libasio.so`、`ascend_transport.so`、自带的系统库。

| 文件 | 大小（字节） | sha256 |
|---|---|---|
| 现场原有的 `mooncake/libmooncake_store.so`（小核对 f-1） | 17610953 | `3bf82f8b0c5ab715…`（f-1 只带回前 16 位） |
| 替换库：`lib/libmooncake_store.so.xz` 解压后 | 15402248 | `3131784385846064327e858d6dcdb11a6c7674c2162964d52f9ed0e866b52bb8` |
| `lib/libmooncake_store.so.xz`（xz -9） | 2630444 | `b89bb174212f26d72f332072dc08e06635f6cccad3f3c7b7109b931400092dde` |

替换前先跑只读核对 `../precheck/dist/t51_mc_precheck.py`，最后一行是 `mc_pre result=ok_replace` 才替换（见 `../precheck/使用说明.zh-CN.md`）。

## 2. 源码和补丁

- **源码。** Mooncake `78566d459c140956fd15606e102132b1acc83eec`（v0.3.14-rc1，版本串 `0.3.13 (git: 78566d4)`）。
- **补丁。** `../patch/04-mooncake-0.3.13-native-timing.patch`，sha256 `0625b51e7f71a62f4cb6e279182fa709d1537800c43bcf7d977459e43c01921f`（`git diff --full-index`，可直接用于 78566d4）。dsv4.1 的补丁 04（`bd807d9b…`）不动。
  - 补丁只编进 `libmooncake_store`，不改数据成员和虚函数。
  - **只增不减。** 两个私有成员函数 `CollectResults(ops)`、`BatchWriteWhenPreferSameNode(ops, is_upsert)` 保留原签名，作为薄包装，转调新增的带计时参数的重载；另新增计时行的写出函数 `LogBatchWriteTiming`。`DurationUsBetween` 是 `static`，不导出。导出名因此只多 3 个、不少任何一个。
  - **原签名包装的计时行。** 原签名的 `BatchWriteWhenPreferSameNode` 拿不到调用方在调用前取的两个时刻，传 `time_point::min()` 作为「未知」。这时计时行只写 phase2–4，不写 `phase1_start_batch_put_us` 和 `total_us`，并写 `complete=false`。面板读不到这两项，对应的段为空，不会是 0。

**调用点**（`mooncake-store/src/client_service.cpp`；行号：原 78566d4 → 打补丁后）。源码里只有这一个文件用到这三个名字；实验室布局里的其他 ELF 文件都不引用它们（`lab/sym_check.txt`）。

| 被调用的函数 | 调用它的函数 | 行号 | 打补丁后调的是 |
|---|---|---|---|
| `BatchWriteWhenPreferSameNode` | `BatchUpsert` | 2223 → 2246 | 新重载，带 `batch_begin` 等两个时刻 |
| `BatchWriteWhenPreferSameNode` | `BatchPut` | 3429 → 3557 | 同上 |
| `CollectResults` | `BatchUpsert` | 2240 → 2276 | 新重载，带 `failed_keys` |
| `CollectResults` | `BatchWriteWhenPreferSameNode` | 3402 → 3512 | 同上 |
| `CollectResults` | `BatchPut` | 3446 → 3590 | 同上 |
| 原签名包装 `CollectResults(ops)` | 无调用方 | 3325 | 转调新重载 |
| 原签名包装 `BatchWriteWhenPreferSameNode(ops, is_upsert)` | 无调用方 | 3392 | 转调新重载，两个时刻传「未知」 |

这些调用都是同一线程里的同步调用。

## 3. 构建环境和选项

| 项 | 取值 | 依据 |
|---|---|---|
| 镜像 | `quay.io/ascend/cann:9.2.0-a3-openeuler24.03-py3.11` | 现场 wheel 标签 `cp311-cp311-manylinux_2_38_aarch64` |
| 编译器 | gcc 12.3.1（openEuler 12.3.1-110.oe2403sp4），cmake 3.31.12 | openEuler 24.03 LTS SP4 软件源 |
| 六个自带库 | glog 0.6.0-1、zstd 1.5.5-4、xxhash 0.8.2-1、yaml-cpp 0.7.0-2、gflags 2.2.2-4、jsoncpp 1.9.5-5（均为 oe2403sp4） | 每个文件 sha256 的前 8 位，都等于现场 NEEDED 里 auditwheel 加的哈希，是同一份二进制 |
| msgpack | **msgpack-c cpp-9.0.0** 头文件（压缩包 sha256 `a72ff747…`） | 现场 store 库导出 `create_object_visitor::visit_ext(const char*, unsigned long)`，第二个参数是 `size_t`。这个参数在 msgpack-c 提交 `a78b895d64`（2026-08-25）从 `uint32_t` 改成 `size_t`，第一个包含它的发行版是 cpp-9.0.0；2.1.5 到 8.0.0 都是 `uint32_t` |
| 其余依赖 | boost 1.83.0、rdma-core 50.0、numactl 2.0.16、libcurl 8.4.0；yalantinglibs `7801bc9a` | Mooncake `FindYLT.cmake` 的 URL_HASH |
| 选项 | `CMAKE_BUILD_TYPE=RelWithDebInfo`（`-O2 -g -DNDEBUG`）、`BUILD_UNIT_TESTS=ON`、`USE_ASCEND_DIRECT=ON`、`USE_ETCD=OFF`、liburing 强制不用、`BUILD_EXAMPLES=OFF`、`BUILD_BENCHMARK=OFF`、`WITH_STORE_RUST=OFF` | 现场 `assert=0`、`nvme_stub=1`；NEEDED 里没有 etcd、liburing |

编出的库再做两步（`build/relink_like_site.py`）：
1. `strip --strip-all`；
2. 一次 `patchelf`：NEEDED 换成现场的文件名，RUNPATH 设为 `$ORIGIN`，SONAME 设为 `libmooncake_store.so`。

## 4. 和现场逐项对照

**比较对象。** 核对件，即同一环境、同一组选项编出的未打补丁 78566d45（msgpack 9.0.0），和现场原库比。现场一侧的数据来自小核对 f-1、Mooncake 现场核对（glm53mc-2，P、D 相同）。

| 项 | 核对件 | 现场 | 结果 |
|---|---|---|---|
| 导出名 | 10974 个，`8d83f4262568bdd3` | 10974 个，`8d83f4262568bdd3` | 相同 |
| 导入名 | 713 | 713 | 相同 |
| 14 个名字族 | 逐族相同 | — | 相同（msgpack 对齐前只差 1 个名字，就是上面那个 `visit_ext`） |
| assert / nvme_stub / timing | 0 / 1 / 0 | 0 / 1 / 0 | 相同 |
| NEEDED、SONAME | 17 项，`libmooncake_store.so` | 同左 | 相同 |
| 线程局部存储 | 3000 | 3000 | 相同 |
| 四个兄弟库的导出名单 | ascend_transport 261、libasio 848、libmooncake_common 499、libtransfer_engine 1821 | 同左 | 逐个名字相同（按集合比较） |
| 编译产物各节大小（PROGBITS） | 指纹 `bffe6014d7458021` | 现场 precheck 在替换前读取 | 待替换前确认：`.rodata` 以外要精确相等，`.rodata` 按构建路径预测 |

**跨库影响。** msgpack 不跨库：
- store 模块从 store 库导入的 46 个名字、store 库的全部导入名、四个兄弟库的导出和导入里，都没有 msgpack 类型；
- 兄弟库的目标文件不包含 msgpack 头文件；
- 46 个名字里出现的 15 个 mooncake 类型都没有 msgpack 数据成员。

## 5. 实验室结果

**静态对比**（`lab/sym_check.txt`）：替换库和核对件比。
- 导出名：去掉 0 个、新增 3 个，正好是补丁 04 新增的：`Client::CollectResults` 和 `Client::BatchWriteWhenPreferSameNode` 各一个带计时参数的重载，以及 `Client::LogBatchWriteTiming`。原签名的两个名字都在。
- `store` 模块从这个库取的 46 个名字全部在。
- 导入名 713 个，完全相同。
- NEEDED、SONAME、RUNPATH 相同。

**原签名反例测试**（`lab/oldsig_test.txt`；`build/t51_oldsig_test.cpp`、`build/oldsig_test.sh`、`build/oldsig_puller_check.py`）：在构建容器里，直接调用打了补丁的 store 库里的原签名函数。glog 用的是系统的 0.6.0，和现场自带的是同一份二进制。

| 调用 | 计时行 | 面板二拉取脚本写出的记录 |
|---|---|---|
| 原签名 `BatchWriteWhenPreferSameNode(ops, false)`，空批 | 有 phase2–4，没有 phase1、`total_us`，`complete=false` | submit、wait_for_transfer、finalize 三段；没有 start_batch_put（缺项，不是 0） |
| `LogBatchWriteTiming`，两个起始时刻为「未知」 | 同上 | 同上 |
| `LogBatchWriteTiming`，全部时刻已知 | 四段和 `total_us` 都有，`complete=true` | 四段都有 |
| 原签名 `CollectResults(ops)` 和新重载 | 结果相同（空批都为 0 项） | — |

- glm5.3 的 `segments.json` 只读 submit、wait_for_transfer、finalize 三段，不读 start_batch_put。
- **glog 0.6.0 的前缀。** glog 0.6.0 默认在前缀里写年份（`I20261119 08:36:03…`），现场的 glog 就是 0.6.0，`glog_env=unset`。
  - 面板二 v3.1-extra 原来的 `scripts/native_put.py`（`bc4b2280…`）只认 `I1119 08:36:03…`，原样喂进去三行都解析不出；去掉年份（`--drop-year`）以后结果同上表。
  - 面板二 `7ef24eb` 改了正则，两种前缀都认。用改后的版本（`39171ded…`）原样喂进去，结果同上表。

**加载和 put**（`lab/lab_test.txt`，`build/lab_test.sh`，`docker run --rm`）：现场样式的布局，原库换成上面的核对件；store 模块用 msgpack 9.0.0 编的未打补丁版本（`build/pathx.sh` 顺带编出，不是按打了补丁的头文件重编的）。

| 项 | 核对件 | 换成替换库后 |
|---|---|---|
| `ldd` 解析不到的库 | 无 | 无 |
| `import mooncake.store` | 成功 | 成功 |
| 进程实际映射的库 | `mooncake/libmooncake_store.so` | 同左 |
| `setup` / `put_batch` 返回 | 0 / 0 | 0 / 0 |
| `get` 内容一致 | 是 | 是 |
| `[BatchPutTiming]` 行数 | 0 | 1（标准错误，带 glog 前缀；`complete=true`，四段和 `total_us` 都有） |

## 6. 复现

1. 装好系统依赖：`build/setup.sh`。
2. 换 msgpack 头文件：`build/exp_msgpack.sh cpp-9.0.0`。脚本换完头文件会 `touch`，然后增量重编核对件并报出指纹。
3. 打补丁：`git apply ../patch/04-mooncake-0.3.13-native-timing.patch`。
4. 增量重编：`ninja -j1 mooncake_store`。单个编译进程最多约 5.7 GB 内存。
5. 去调试信息、重链接：`build/relink_like_site.py`。
6. 原签名反例测试：`build/oldsig_test.sh`，再用 `build/oldsig_puller_check.py` 把计时行喂给面板二的拉取脚本。
7. 在另一个路径下编未打补丁的核对件和 store 模块：`TARGETS="mooncake_store store" build/pathx.sh <目录>`。它同时用来量 `.rodata` 随构建路径长度的变化（替换前核对的规则 1b）。
8. 搭现场样式的布局：`STORE_MODULE=<第 7 步的 store 模块> build/make_layout.sh`。
9. 跑加载和 put 测试：`build/lab_test.sh`。
