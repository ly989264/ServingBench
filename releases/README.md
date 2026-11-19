# 在用部署包（GitHub Release）

部署包和离线包不进 git，作为 GitHub Release 的资产发布；每个包对应一个 git tag，本目录 `<tag>/SHA256SUMS` 记录资产的 sha256。选 Release 而不用 Git LFS 的原因：GitHub 免费账户的 LFS 额度为存储 1 GiB、流量每月 1 GiB，离线包一个就有 398 MB；Release 资产单个上限 2 GiB，不计 LFS 额度。

资产都是交付给内网并经过走查的原件（tar 因 gzip 时间戳不能逐字节复现，重新出包得到的 sha256 不同，以原件为准）。2026-11-19 用本仓库源码重新出包核对过：面板一包内 `SHA256SUMS` 与原件相同；面板二两个包的包内文件与原件逐文件相同，只有 `MANIFEST.txt` 的构建时间一行不同。

| tag | 资产 | 用在 | 构建来源 |
|---|---|---|---|
| `panel1-60ce7f30` | `vllm-pd-grafana-panel1.tar.gz`（92 KB） | 8 套面板一（glm53-0、glm53-1、glm53-2、dsv41-flash-1…5） | `panel1/pack/`，built 2026-11-12T15:06:34Z |
| `panel2-v3-3e98c2b4` | `vllm-pd-grafana-panel2.tar.gz`（1.2 MB）、`vllm_pd_update_pack2.py`（交付的占位符版更新脚本，内含 tar 的 sha256）、`包二-v3-MANIFEST.txt` | 面板二 glm53-1 | 开发仓库 765a0fc（`MANIFEST.txt` 中的 built 行），config_id `pd-diag-v3-2026-11-16` |
| `panel2-v3.1-extra-e3885b93` | `vllm-pd-grafana-panel2.tar.gz`（1.2 MB）、`vllm_pd_update_pack2_v31extra.py`（交付的占位符版更新脚本） | 面板二 test-glm53 | 开发仓库分支 v3.1-extra 7ef24eb，config_id 后缀 -19c |
| `glm53-patch-kit-d21cc319` | `t51-glm53-patch-kit.tar`（5.7 MB）；交付说明在本目录 | glm5.3 测试集群的 P、D 容器（打补丁手册，《操作说明 A》第 1–10 步） | `patches/glm53/t51-glm53-patchkit/`（207d7a4），`tools/make_tar.sh` |
| `glm53-mooncake-kit-6d70978c` | `t51-glm53-mooncake-kit.tar`（15 MB，含替换用的 `libmooncake_store.so`）；交付说明在本目录 | 同上，《操作说明 A》第 11 步（可延后） | `patches/glm53/t51-glm53-kit/mooncake/`（913b4f0） |
| `panel2-offline-aarch64-14c00e3a` | `vllm-pd-grafana-panel2-offline-aarch64.tar.gz`（398 MB） | 面板二在 aarch64 监控机上的离线安装 | 第三方原件：`clickhouse/clickhouse-server:26.8.36.3`、`timberio/vector:0.61.0-debian` 的 `docker save`，`grafana-clickhouse-datasource` 4.23.4 linux_arm64，Python 3.12.16 standalone aarch64；清单见包内 `offline/VERSIONS.txt` |

更新脚本的已填路径版（`*_filled.py`，只把 `USER_DIR` 一行改成现场的用户目录）不发布。

## 发布命令

在仓库根目录执行；资产的本地副本在 `local/release-assets/<tag>/`（不进 git）。先核对：

```bash
for d in local/release-assets/*/; do (cd "$d" && shasum -a 256 -c *.sha256); done
```

然后打 tag、推送、建 Release：

```bash
for t in panel1-60ce7f30 panel2-v3-3e98c2b4 panel2-v3.1-extra-e3885b93 panel2-offline-aarch64-14c00e3a glm53-patch-kit-d21cc319 glm53-mooncake-kit-6d70978c; do git tag -a "$t" -m "$t"; done
```

```bash
git push origin main --follow-tags
```

```bash
for t in panel1-60ce7f30 panel2-v3-3e98c2b4 panel2-v3.1-extra-e3885b93 panel2-offline-aarch64-14c00e3a glm53-patch-kit-d21cc319 glm53-mooncake-kit-6d70978c; do gh release create "$t" --verify-tag --title "$t" --notes "资产的 sha256 见 releases/$t/SHA256SUMS；用途见 releases/README.md。" local/release-assets/"$t"/*; done
```

下载后的核对（在下载目录中执行，`<仓库>` 为本仓库的路径）：

```bash
shasum -a 256 -c <仓库>/releases/<tag>/SHA256SUMS
```
