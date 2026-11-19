# 操作说明 A「替换 Mooncake 的 store 库」（glm5.3）

## 0. 换什么，为什么要分批

- **只换一个文件**：每个 P、D 容器里的 `<PKG>/libmooncake_store.so`（`<PKG>` 是 vLLM 所用 Python 的 `site-packages/mooncake`）。store 模块、`libtransfer_engine.so`、`libmooncake_common.so`、`libasio.so`、`ascend_transport.so` 和自带的系统库都不动。
- **替换库**：kit `mooncake/replace/lib/libmooncake_store.so.xz`，解压后 15,402,248 字节，sha256 `3131784385846064327e858d6dcdb11a6c7674c2162964d52f9ed0e866b52bb8`。它是 Mooncake 78566d4 加补丁 04 编出来的，和现场原库同一套构建环境、选项、依赖（见 `mooncake/replace/README.zh-CN.md`）。
- **`<PKG>` 怎么取**：用运行 vLLM 的那个 `python3`：

  ```bash
  docker exec <容器名> python3 -I -S -c 'import sysconfig; print(sysconfig.get_paths()["platlib"] + "/mooncake")'
  ```

  再确认 `docker exec <容器名> ls -l <PKG>/libmooncake_store.so` 的大小是 17610953。
- **生效时机**：进程启动时加载，换完要重启该容器里的 vLLM 才生效。
- **分批的原因**：替换前核对只能证明二进制层面一致，不能证明运行时没有问题。所以先在一台 D 机器上换，跑一段时间确认没问题，再换其余 D 机器，最后换 P。

## 1. 替换前核对（每个要换的容器都跑，是最后一道关）

脚本：kit `mooncake/precheck/dist/t51_mc_precheck.py`（glm53mc-5），sha256 `4475c662c9ae669b50fa60bd097b386f2b4a3f6b5987eb78b0a0686cafd24cd4`（以 Mooncake 增量包为准）。

```bash
docker cp t51_mc_precheck.py <容器名>:/tmp/
```

```bash
docker exec <容器名> python3 -I -S -B /tmp/t51_mc_precheck.py
```

```bash
docker exec <容器名> rm -f /tmp/t51_mc_precheck.py
```

- 在 vLLM 运行时跑。只读、几秒钟跑完，不导入 Mooncake、不写文件、不起子进程。
- **判据**：最后一行是 `mc_pre result=ok_replace` 才换这个容器。其他任何结果都不换，把整段输出带回。各行怎么读、各个 STOP 的含义，见 `mooncake/precheck/使用说明.zh-CN.md`。
- 正常时还应看到：`mc_orig … f1=same`、`mc_build sections_other=same … result=same_build`、`mc_exports … replacement_vs_site=-0/+3(expected -0/+3)`、`consumers_ok … removed_names=0`。

## 2. 替换（一个容器）

**2.1 备份原库到主机。** 备份不能放在 `<PKG>` 或任何 `mooncake*.libs` 目录里，否则核对脚本和检测脚本会把它当成第二份 store 库。

```bash
mkdir -p <主机备份目录> && docker cp <容器名>:<PKG>/libmooncake_store.so <主机备份目录>/<容器名>.libmooncake_store.so
```

```bash
sha256sum <主机备份目录>/<容器名>.libmooncake_store.so
```

前 16 位应为 `3bf82f8b0c5ab715`（小核对 f-1 的值）。

**2.2 放入替换库。** 先复制到同一目录下的临时名，再用 `mv` 改名。改名不会动正在运行的 vLLM 已经映射的旧文件；直接覆盖原文件可能让正在运行的进程崩溃。

```bash
xz -dk libmooncake_store.so.xz && sha256sum libmooncake_store.so
```

```bash
docker cp libmooncake_store.so <容器名>:<PKG>/.t51_new_store.so
```

```bash
docker exec <容器名> sh -c 'cd <PKG> && chmod 0755 .t51_new_store.so && mv -f .t51_new_store.so libmooncake_store.so && sha256sum libmooncake_store.so'
```

sha256 必须是 `3131784385846064327e858d6dcdb11a6c7674c2162964d52f9ed0e866b52bb8`。

**2.3 重启该容器里的 vLLM**，按本说明的重启步骤。不要设 `MC_LOG_DIR`。

## 3. 换后核对（每个换过的容器）

**3.1 检测脚本 glm53-6**（kit `detect/dist/t51_glm53_check.py`，sha256 `f9e6073f692dd4ea8b08b0c7aa26400ac0603495e89b5641e8c4bd20f67ef4ed`，用法同检测那一步）：

| 行 | 应为 | 说明 |
|---|---|---|
| `mooncake=` 行的 `store=` | `replacement` | 文件的 sha16 等于替换库 |
| `mooncake=` 行的 `timing=` | `2`（换前为 `0`） | 库里有 `[BatchPutTiming]`、`[BatchUpsertTiming]` 两种计时行 |
| `mooncake=` 行的 `changed_since_install=` | 不为 0，属预期 | 安装记录里还是原库的哈希，不是漂移 |
| `mooncake=` 行的 `loaded=` | 运行中的 vLLM 加载了 `<PKG>/libmooncake_store.so` | 确认重启后用的是新文件 |
| `verdict` 行的 `mooncake=` | `ok` | `not_replaced` 是没换上；`BAD(other)` 是换成了别的文件 |

**3.2 小核对 glm53f-3b 的 `mc_syms` 行**（用法同小核对那一步）：

| 字段 | 应为 | 换前 |
|---|---|---|
| `exports=` | `10977` | 10974（补丁 04 新增 3 个名字，不去掉任何名字） |
| `imports=` | `713` | 713 |
| `assert=` | `0` | 0 |
| `nvme_stub=` | `1` | 1 |
| `timing=` | `1` | 0 |

**3.3 运行。** vLLM 起得来，日志里没有 `undefined symbol`；有请求经过缓存池保存后，vLLM 日志里出现 `[BatchPutTiming]` 行（D 只在往缓存池保存时才有）。

## 4. 分批顺序

| 批次 | 范围 | 换完以后 |
|---|---|---|
| 第 1 批 | 一台 D 机器（16 个 D 实例） | 第 3 节全部通过，服务正常运行一段时间（时长由现场定），再进行第 2 批 |
| 第 2 批 | 其余 D 机器 | 每台都过第 3 节 |
| 第 3 批 | P 机器 | 每台都过第 3 节 |

任何一批出现第 3 节不符，或者服务异常（vLLM 起不来、崩溃、KV 传输报错），就停止后续批次，按第 5 节回滚出问题的容器，把检测脚本、小核对的输出和 vLLM 日志的相关部分带回。

## 5. 回滚（一个容器）

```bash
docker cp <主机备份目录>/<容器名>.libmooncake_store.so <容器名>:<PKG>/.t51_orig_store.so
```

```bash
docker exec <容器名> sh -c 'cd <PKG> && mv -f .t51_orig_store.so libmooncake_store.so && sha256sum libmooncake_store.so'
```

前 16 位回到 `3bf82f8b0c5ab715` 后，重启该容器里的 vLLM。检测脚本应显示 `store=site_original`、`timing=0`，`verdict` 行 `mooncake=not_replaced`。

回滚后面板二的 Mooncake 三段（submit、wait_for_transfer、finalize）为空，其他段不受影响。
