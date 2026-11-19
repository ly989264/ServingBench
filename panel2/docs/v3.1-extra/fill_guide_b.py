#!/usr/bin/env python3
"""Makes the filled-path copy of 操作说明B_只升级test面板.zh-CN.md (the placeholder copy is the source).

    python3 docs/v3.1-extra/fill_guide_b.py

Only values that an earlier filled file already carried: USER_DIR and ROOT of the v3 filled update script
(update/vllm_pd_update_pack2_filled.py). The proxy machine, its log directory, where vLLM's standard error goes, the
nodes' addresses and folder stay placeholders: no filled file has them; they come from the environment check and
guide A. The output goes to docs/v3.1-extra/已填路径/ and belongs to the private Caching repository only.
"""
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE / "操作说明B_只升级test面板.zh-CN.md"
OUT = HERE / "已填路径" / "操作说明B_只升级test面板_已填路径.zh-CN.md"
FILLED_V3 = HERE.parent.parent / "update" / "vllm_pd_update_pack2_filled.py"
NOTE = ("> **路径已填好的一版，只放在私有的 Caching 仓库。** `<用户目录>`、`<总目录>` 取自 v3 已填路径的更新脚本 "
        "`vllm_pd_update_pack2_filled.py` 的 `USER_DIR`、`ROOT`。proxy 机器、它的日志目录、vLLM 标准错误的去处、节点地址和目录"
        "仍是占位符：已有的已填文件里没有这些值，按环境检测结果和操作说明 A 填。")


def main():
    text = FILLED_V3.read_text(encoding="utf-8")
    user = re.search(r'^USER_DIR = "([^"]+)"', text, re.M).group(1)
    root = re.search(r'^ROOT = "([^"]+)"', text, re.M).group(1)
    if "<" in user or "<" in root:
        raise SystemExit("v3 的已填脚本里 USER_DIR、ROOT 还是占位符")
    values = {"<用户目录>": user, "<总目录>": root, "<更新脚本>": "vllm_pd_update_pack2_v31extra_filled.py"}
    s = SRC.read_text(encoding="utf-8")
    s = re.sub(r"<!-- 占位符版开始 -->\n.*?<!-- 占位符版结束 -->\n", NOTE + "\n", s, flags=re.S)
    s = re.sub(r"<!-- 占位符表开始 -->\n(.*?)<!-- 占位符表结束 -->\n", lambda m: "\n".join(
        l for l in m.group(1).splitlines() if not l.startswith(("| `<用户目录>`", "| `<总目录>`", "| `<更新脚本>`"))) + "\n", s, flags=re.S)
    s = s.replace("（v3 → v3.1-extra）", "（v3 → v3.1-extra，路径已填好）", 1)
    s = s.replace("# 内网操作说明 B：只把 test 的面板二升到 v3.1-extra，并测试",
                  "# 内网操作说明 B：只把 test 的面板二升到 v3.1-extra，并测试（路径已填好）", 1)
    for ph, v in values.items():
        s = s.replace(ph, v)
    left = sorted(set(re.findall(r"<(?:用户目录|总目录|更新脚本)>", s)))
    if left or "<!--" in s:
        raise SystemExit("还有没换掉的占位符：%s" % left)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(s, encoding="utf-8")
    print("写好 %s" % OUT)


if __name__ == "__main__":
    main()
