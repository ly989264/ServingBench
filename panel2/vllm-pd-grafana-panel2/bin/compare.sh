#!/bin/bash
# Compares what this pack would deploy with what the live services use (design 2.13). Reads only; writes only under
# generated/compare/. One line per item: OK / INFO / WARN; exit 0 unchanged, 10 changed or not comparable, 2 usage or
# environment error. See scripts/compare.py.
#   bin/compare.sh all|board|ddl|prometheus|central|datasource [--sql-out generated/compare/<file>]
case ${1:-} in
  all|board|ddl|prometheus|central|datasource) ;;
  *) echo "用法：bin/compare.sh all|board|ddl|prometheus|central|datasource [--sql-out FILE]" >&2; exit 2 ;;
esac
. "$(dirname "$0")/lib.sh"
# A deploy.env or Python problem is an environment error here (2), not a difference.
fail() { echo "错误：$*" >&2; exit 2; }
load_env
need_python
exec "$PYTHON" "$PACK_DIR/scripts/compare.py" "$@"
