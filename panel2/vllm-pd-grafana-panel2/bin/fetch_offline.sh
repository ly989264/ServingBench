#!/bin/bash
# Backup only: the two offline bundles already carry all of this, pinned and checked. Use this when a bundle is not at hand.
# Run on a machine with internet. Downloads everything the intranet needs into offline/.
# Usage: bin/fetch_offline.sh <aarch64|x86_64> [more architectures]
# The first architecture is the one the ClickHouse image is saved for (the monitoring machine's).
set -u
PACK_DIR=$(cd "$(dirname "$0")/.." && pwd)
OFF="$PACK_DIR/offline"
[ $# -ge 1 ] || { echo "用法：bin/fetch_offline.sh <aarch64|x86_64> [其他架构]" >&2; exit 1; }
mkdir -p "$OFF"
VECTOR=0.61.0
PLUGIN=4.23.4
PY=3.12.16
PY_DATE=20261026
IMAGE=clickhouse/clickhouse-server:26.8.36.3
first=$1
for arch in "$@"; do
  case $arch in
    aarch64) go=arm64 ;;
    x86_64) go=amd64 ;;
    *) echo "不认识的架构：$arch" >&2; exit 1 ;;
  esac
  f="python-$PY-$arch-unknown-linux-gnu.tar.gz"
  echo "下载 $f"
  curl -fSL --retry 3 -o "$OFF/$f" "https://github.com/astral-sh/python-build-standalone/releases/download/$PY_DATE/cpython-$PY%2B$PY_DATE-$arch-unknown-linux-gnu-install_only_stripped.tar.gz" || { echo "下载失败：$f" >&2; exit 1; }
  f="grafana-clickhouse-datasource-$PLUGIN.linux_$go.zip"
  echo "下载 $f"
  curl -fSL --retry 3 -o "$OFF/$f" "https://grafana.com/api/plugins/grafana-clickhouse-datasource/versions/$PLUGIN/download?os=linux&arch=$go" || { echo "下载失败：$f" >&2; exit 1; }
done
case $first in aarch64) plat=linux/arm64 ;; x86_64) plat=linux/amd64 ;; esac
echo "拉取镜像 $IMAGE（$plat）"
docker pull --platform "$plat" "$IMAGE" || { echo "拉取镜像失败" >&2; exit 1; }
docker save "$IMAGE" -o "$OFF/clickhouse-image.tar" || docker save --platform "$plat" "$IMAGE" -o "$OFF/clickhouse-image.tar" || { echo "保存镜像失败" >&2; exit 1; }
echo "拉取镜像 timberio/vector:$VECTOR-debian（$plat）"
docker pull --platform "$plat" "timberio/vector:$VECTOR-debian" || { echo "拉取镜像失败" >&2; exit 1; }
docker save "timberio/vector:$VECTOR-debian" -o "$OFF/vector-image.tar" || docker save --platform "$plat" "timberio/vector:$VECTOR-debian" -o "$OFF/vector-image.tar" || { echo "保存镜像失败" >&2; exit 1; }
# The compatibility builds exist only for master, so what this downloads changes from day to day, unlike the bundles.
case $first in aarch64) dir=aarch64v80compat ;; x86_64) dir=amd64compat ;; esac
echo "下载 ClickHouse 兼容版（CPU 缺官方版要求的特性时用，约 200 MB，版本不固定）"
curl -fSL --retry 3 -o "$OFF/clickhouse-$dir" "https://builds.clickhouse.com/master/$dir/clickhouse" || { echo "下载失败：兼容版" >&2; exit 1; }
chmod a+rx "$OFF/clickhouse-$dir"
ls -lh "$OFF"
echo "RESULT: OK"
