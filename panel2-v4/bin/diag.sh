#!/bin/bash
# 面板二诊断工具（监控机一端）的入口。在监控机上、集群目录中执行：bin/diag.sh <子命令>，bin/diag.sh -h 查看用法。
# 检查入库进程、ClickHouse 和 status_doc；采集进程和写出队列在集群端用 fetcher_check.py 检查。
# 除 pair 以外只读：不写集群目录，不重启、不停止、不修改任何服务；报告写在 ~/vllm-pd-diag-reports/<集群>/。
# 说明见 docs/诊断工具说明.zh-CN.md。
set -u
. "$(dirname "$0")/lib.sh"
load_ch_env
load_gf_env
need_python
export PYTHONDONTWRITEBYTECODE=1
exec "$PYTHON" -I -B "$PACK_DIR/scripts/diag.py" "$@"
