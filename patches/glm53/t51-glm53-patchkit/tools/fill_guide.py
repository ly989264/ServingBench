# -*- coding: utf-8 -*-
"""Filled-path copy of guide A, with the rules of the other filled guides: only USER_DIR and ROOT of the v3 filled update
script; everything else stays a placeholder.
    python3 fill_guide.py <v3 filled update script> <guide> <out>"""
import re
import sys

filled_v3, src, out = sys.argv[1:4]
NOTE = ("> **路径已填好的一版，只放在私有的 Caching 仓库。** `<用户目录>`、`<总目录>` 取自 v3 已填路径的更新脚本 "
        "`vllm_pd_update_pack2_filled.py` 的 `USER_DIR`、`ROOT`。其余尖括号里的值仍是占位符：已有的已填文件里没有这些值，"
        "按现场的情况填。")
text = open(filled_v3, encoding="utf-8").read()
user = re.search(r'^USER_DIR = "([^"]+)"', text, re.M).group(1)
root = re.search(r'^ROOT = "([^"]+)"', text, re.M).group(1)
if "<" in user or "<" in root:
    raise SystemExit("v3 的已填脚本里 USER_DIR、ROOT 还是占位符")
s = open(src, encoding="utf-8").read()
s = re.sub(r"<!-- 占位符版开始 -->\n.*?<!-- 占位符版结束 -->\n", NOTE + "\n", s, flags=re.S)
s = re.sub(r"<!-- 占位符表开始 -->\n(.*?)<!-- 占位符表结束 -->\n", lambda m: "\n".join(
    l for l in m.group(1).splitlines() if not l.startswith(("| `<用户目录>`", "| `<总目录>`"))) + "\n", s, flags=re.S)
first = s.split("\n", 1)
s = first[0] + "（路径已填好）\n" + first[1]
s = s.replace("<用户目录>", user).replace("<总目录>", root)
open(out, "w", encoding="utf-8").write(s)
print("写好 " + out)
