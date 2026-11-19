#!/bin/bash
# 面板二诊断工具的入口。在监控机上执行：bin 不需要在集群目录里，默认读 /data/grafana/vllm-pd-grafana-panel2/glm53-1。
# 用法见同目录 README.zh-CN.md，或 diag.sh -h。除 pair 以外都只读。
DIR=$(cd "$(dirname "$0")" && pwd)
for py in python3 python3.12 python3.11 python3.10 python3.9 python3.8 python3.7 python3.6; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 6) else 1)' 2>/dev/null; then
    PYTHONDONTWRITEBYTECODE=1 exec "$py" -B "$DIR/diag.py" "$@"
  fi
done
echo "错误：这台机器上没有 Python 3.6 及以上" >&2
exit 2
