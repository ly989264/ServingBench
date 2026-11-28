#!/bin/bash
# 面板二诊断工具的入口。在监控机上、集群目录里执行：bin/diag.sh <子命令>，bin/diag.sh -h 看用法。
# 除 pair 以外只读：不写集群目录，不重启、不停止、不修改任何服务；报告写在 ~/vllm-pd-diag-reports/<集群>/。
# 说明见 docs/诊断工具说明.zh-CN.md。
set -u
DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONDONTWRITEBYTECODE=1
# The Python the pack's other scripts use (bin/lib.sh: PYTHON in deploy.env, then PATH, then the pack's own).
PY=$(cd "$DIR" && . bin/lib.sh && load_env >/dev/null && echo "$PYTHON") || exit 2
[ -n "$PY" ] || { echo "错误：找不到 Python 3.9 及以上" >&2; exit 2; }
exec "$PY" -B "$DIR/scripts/diag.py" "$@"
