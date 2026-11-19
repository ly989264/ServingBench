#!/usr/bin/env python3
"""Makes the filled-path copy of 内网升级总说明.zh-CN.md (the placeholder copy is the source).

    python3 docs/v3/fill_guide.py [--check <v2.1 filled update script>]

Every value comes from a file of v2.1 that already carried it: the v2.1 filled update script
(vllm_pd_update_pack2_filled.py: USER_DIR, ROOT, CLUSTER, MODEL_GROUP, P2_OFFLINE) and the v2.1 system manual
(面板系统说明书 section 7: the vllm-mode cluster test). No node address is filled in: the guide's commands read them
from the cluster's inventory.json. --check compares the values with a v2.1 filled script. The output goes to
docs/v3/已填路径/ and belongs to the private Caching repository only, never into a pack.
"""
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE / "内网升级总说明.zh-CN.md"
OUT = HERE / "已填路径" / "内网升级总说明_已填路径.zh-CN.md"
USER_DIR = __import__("os").environ.get("USER_DIR") or sys.exit("set USER_DIR=<用户目录> first: the site user directory is not kept in this repository")
VALUES = {                                   # placeholder -> value, and the v2.1 constant it comes from
    "<离线包>": (USER_DIR + "/vllm-pd-panel2-update-1112/vllm-pd-grafana-panel2-offline-aarch64.tar.gz", "P2_OFFLINE"),
    "<用户目录>": (USER_DIR, "USER_DIR"),
    "<总目录>": ("/data/grafana", "ROOT"),
    "<更新脚本>": ("vllm_pd_update_pack2_filled.py", None),
    "<mock 集群>": ("glm53-1", "CLUSTER"),
    "<vllm 集群>": ("test", None),           # v2.1 面板系统说明书 7.2: 集群目录 /data/grafana/vllm-pd-grafana-panel2/test
    "<模型文件夹>": ("GLM-5.3", "MODEL_GROUP"),
}
NOTE = ("> **路径已填好的一版，只放在私有的 Caching 仓库。** 取值来自 v2.1 已填路径的更新脚本 `vllm_pd_update_pack2_filled.py` "
        "（`USER_DIR`、`ROOT`、`CLUSTER`、`MODEL_GROUP`、`P2_OFFLINE`）和 v2.1《面板系统说明书》第 7 节（vllm 模式的集群 test）。"
        "节点地址不写在文中，命令从集群目录的 `inventory.json` 读出来。")


def check(path):
    text = open(path, encoding="utf-8").read()
    got = {}
    for name, prefix, value in re.findall(r'^([A-Z_0-9]+) = (USER_DIR \+ )?"([^"]*)"', text, re.M):
        got[name] = (got.get("USER_DIR", "") if prefix else "") + value
    for ph, (value, const) in VALUES.items():
        if const and got.get(const) != value:
            raise SystemExit("v2.1 的 %s 是 %r，不是 %r" % (const, got.get(const), value))
    print("和 %s 的值一致" % path)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--check":
        check(sys.argv[2])
    s = SRC.read_text(encoding="utf-8")
    s = re.sub(r"<!-- 占位符版开始 -->\n.*?<!-- 占位符版结束 -->\n", NOTE + "\n", s, flags=re.S)
    s = re.sub(r"<!-- 占位符表开始 -->\n.*?<!-- 占位符表结束 -->\n", "", s, flags=re.S)
    s = s.replace("# 包二 v3 升级说明书：面板二（v2.1 → v3）", "# 包二 v3 升级说明书：面板二（v2.1 → v3，路径已填好）", 1)
    for ph, (value, _) in VALUES.items():
        s = s.replace(ph, value)
    left = sorted(set(re.findall(r"<(?:用户目录|总目录|离线包|更新脚本|mock 集群|vllm 集群|模型文件夹)>", s)))
    if left or "<!--" in s:
        raise SystemExit("还有没换掉的占位符：%s" % left)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(s, encoding="utf-8")
    print("写好 %s" % OUT)


if __name__ == "__main__":
    main()
