# Mooncake（T56 不改）

T56 不碰 Mooncake：这个包里没有 Mooncake 的文件，升级和回滚都不动 `libmooncake_store.so`。

- glm5.3 测试集群在 T56 之前可能已经按例外换过库（11-20，只限这个测试集群这一次），也可能还没换。两种都是正常状态：
  - 换过库：检测脚本报 `mooncake=ok`，`mooncake=` 那一行是 `store=replacement changed_since_install=1`；
  - 没换：报 `mooncake=not_replaced`。
- 升级前后两次检测的 Mooncake 状态应当相同。
- Mooncake 的替换库、换库前核对和回滚，仍按 T51 的 Mooncake 增量包 `t51-glm53-mooncake-kit.tar`（sha256 `6d70978c…`）和打补丁手册第六部分。
