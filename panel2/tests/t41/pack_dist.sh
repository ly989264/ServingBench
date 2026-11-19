#!/bin/bash
# T41-D: assemble the delivery folder of pack two v3 (the Caching repository's 包二-v3/) from a commit of this repository.
#   [TAR_FROM=<dir of the walked tar>] tests/t41/pack_dist.sh <out dir> [commit, default HEAD]
# 1. the panel-2 tar and its .sha256: with TAR_FROM, the tar the lab walk used (checked file by file against the
#    commit's pack); otherwise a new build (tests/t41/build_pack.sh);
# 2. both update scripts with P2_SHA set to the tar's sha256;
# 3. the 【待打包填】 places of docs/v3/内网升级总说明.zh-CN.md (working tree; sizes and the sha256 of the filled update
#    script, which the filled-path guide names as <更新脚本>), then docs/v3/fill_guide.py for the filled-path copy;
#    the tar's sha256 in docs/v3/面板系统说明书.zh-CN.md (its versions table);
# 4. 包二-v3-MANIFEST.txt; 5. both guides and docs/v3/面板系统说明书.zh-CN.md;
# 6. leak check: only the two filled-path files may contain the user directory.
# Step 3 changes the working tree: commit the guides afterwards. Running it again gives the same files.
set -euo pipefail
OUT=${1:?out dir}
REV=${2:-HEAD}
REPO=$(cd "$(dirname "$0")/../.." && pwd)
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
TAR=vllm-pd-grafana-panel2.tar.gz
GUIDE="$REPO/docs/v3/内网升级总说明.zh-CN.md"
FILLED_GUIDE="$REPO/docs/v3/已填路径/内网升级总说明_已填路径.zh-CN.md"
MAN=包二-v3-MANIFEST.txt
size() { wc -c < "$1" | tr -d ' '; }
sha() { shasum -a 256 "$1" | cut -d' ' -f1; }

if [ -n "${TAR_FROM:-}" ]; then
  # the tar the lab walk used: its files must be the commit's pack (build_pack.sh stamps a build time, so a new build
  # never has the same sha256)
  W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
  mkdir "$W/a" "$W/b"
  (cd "$TAR_FROM" && shasum -a 256 -c "$TAR.sha256" >/dev/null)
  tar -xzf "$TAR_FROM/$TAR" -C "$W/a"
  git -C "$REPO" archive --format=tar "$REV" vllm-pd-grafana-panel2 | tar -xf - -C "$W/b"
  rm -rf "$W/b/vllm-pd-grafana-panel2/generated" "$W/b/vllm-pd-grafana-panel2/.gitignore"
  find "$W/b" -name __pycache__ -prune -exec rm -rf {} +
  diff -r -x MANIFEST.txt -x SHA256SUMS "$W/a/vllm-pd-grafana-panel2" "$W/b/vllm-pd-grafana-panel2" >&2 \
    || { echo "$TAR_FROM/$TAR is not the pack of $REV" >&2; exit 1; }
  cp "$TAR_FROM/$TAR" "$TAR_FROM/$TAR.sha256" "$OUT/"
else
  "$REPO/tests/t41/build_pack.sh" "$OUT" "$REV" >/dev/null
fi
TSHA=$(sha "$OUT/$TAR")
for f in vllm_pd_update_pack2.py vllm_pd_update_pack2_filled.py; do
  git -C "$REPO" show "$REV:update/$f" | sed "s|^P2_SHA = \"<filled at packing>\"|P2_SHA = \"$TSHA\"|" > "$OUT/$f"
  grep -q "^P2_SHA = \"$TSHA\"" "$OUT/$f" || { echo "P2_SHA not set in $f" >&2; exit 1; }
done

manifest() {   # $1: sha256 of each listed file, or "dummy" for 64 zeros (the size does not depend on the values)
  local z=0000000000000000000000000000000000000000000000000000000000000000 f
  echo "包二 v3：面板二读补丁 01、02 的新日志（新填 8 个段），修 6 条已知问题；更新脚本按集群直接升级，先删掉这个集群面板二的数据"
  echo
  echo "版本号 v3 写在部署包里（VERSION 文件、MANIFEST.txt 第二行）和脚本里（PACK_VERSION），check、run、rollback 的第一行和摘要第一行都会打印。"
  echo
  echo "sha256                                                            文件（Caching 仓库 ServingBench 分支 包二-v3/ 里）"
  for f in $TAR vllm_pd_update_pack2.py vllm_pd_update_pack2_filled.py 内网升级总说明.zh-CN.md 内网升级总说明_已填路径.zh-CN.md 面板系统说明书.zh-CN.md; do
    if [ "$1" = dummy ]; then echo "$z  $f"; else echo "$(sha "$OUT/$f")  $f"; fi
  done
  echo "14c00e3a3a4c73468336c7aa79b7d02fb5f428130495f3fd516a829d5e53b032  ../vllm032-pd-grafana-panel2/vllm-pd-grafana-panel2-offline-aarch64.tar.gz（9 个分片合并后；和包一、包二 v2.0、v2.1 相同，不用重拷）"
}
manifest dummy > "$OUT/$MAN"
MSIZE=$(size "$OUT/$MAN")

python3 -I - "$GUIDE" "$(size "$OUT/$TAR")" "$(size "$OUT/vllm_pd_update_pack2_filled.py")" "$MSIZE" "$(sha "$OUT/vllm_pd_update_pack2_filled.py")" <<'EOF'
import re, sys
p, tsize, ssize, msize, ssha = sys.argv[1:]
s = open(p, encoding="utf-8").read()
for pat, new in ((r"(<组> )(?:【待打包填】|\d+)( <日期> vllm-pd-grafana-panel2\.tar\.gz\n)", tsize),
                 (r"(<组> )(?:【待打包填】|\d+)( <日期> <更新脚本>\n)", ssize),
                 (r"(<组> )(?:【待打包填】|\d+)( <日期> 包二-v3-MANIFEST\.txt\n)", msize),
                 (r'(echo ")(?:【待打包填：更新脚本的 sha256】|[0-9a-f]{64})(  <更新脚本>")', ssha)):
    s, n = re.subn(pat, lambda m: m.group(1) + new + m.group(2), s)
    if n != 1:
        sys.exit("guide: %d matches for %s" % (n, pat))
if "【待" in s:
    sys.exit("guide: placeholders left: %s" % re.findall(r"【待[^】]*】", s))
open(p, "w", encoding="utf-8").write(s)
EOF
python3 -I - "$REPO/docs/v3/面板系统说明书.zh-CN.md" "$TSHA" <<'EOF'
import re, sys
p, tsha = sys.argv[1:]
s = open(p, encoding="utf-8").read()
s, n = re.subn(r"(v3，部署包 sha256)(?:【待打包填】| `[0-9a-f]{8}…`)", lambda m: m.group(1) + " `%s…`" % tsha[:8], s)
if n != 1:
    sys.exit("manual: %d matches for the pack's sha256" % n)
open(p, "w", encoding="utf-8").write(s)
EOF
python3 -I "$REPO/docs/v3/fill_guide.py" >/dev/null
cp "$GUIDE" "$FILLED_GUIDE" "$REPO/docs/v3/面板系统说明书.zh-CN.md" "$OUT/"
manifest real > "$OUT/$MAN"
[ "$(size "$OUT/$MAN")" = "$MSIZE" ] || { echo "MANIFEST size changed" >&2; exit 1; }

UD=$(sed -n 's/^USER_DIR = "\(.*\)"$/\1/p' "$OUT/vllm_pd_update_pack2_filled.py")
case "$UD" in */\<*|"") echo "USER_DIR of the filled script is not filled" >&2; exit 1;; esac
leaks=$(cd "$OUT" && grep -rlF -- "$UD" . | grep -vE '^\./(vllm_pd_update_pack2_filled\.py|内网升级总说明_已填路径\.zh-CN\.md)$' || true)
tleaks=$(tar -xzOf "$OUT/$TAR" | grep -cF -- "$UD" || true)
[ -z "$leaks" ] && [ "$tleaks" = 0 ] || { echo "user directory found in: $leaks (tar: $tleaks)" >&2; exit 1; }
left=$(cd "$OUT" && grep -rlF "【待" . | grep -v '\.tar\.gz$' || true)
tleft=$(tar -xzOf "$OUT/$TAR" | grep -cF "【待" || true)
[ -z "$left" ] && [ "$tleft" = 0 ] || { echo "【待 left in: $left (tar: $tleft)" >&2; exit 1; }
(cd "$OUT" && ls -l $TAR $TAR.sha256 vllm_pd_update_pack2.py vllm_pd_update_pack2_filled.py $MAN *.zh-CN.md | awk '{print $5, $NF}')
echo "leak check: user directory only in the two filled-path files; no 【待 left in the folder or the tar"
