#!/bin/bash
# Removes the earlier 面板二 (tar 59604f30), or one cluster of this version (bin/panel2_cluster.sh remove). Runs on the monitoring machine.
# Usage: bin/uninstall_old.sh plan|remove|purge-data|check <旧版目录> [--remove-plugin] [--backup <目录>]
# What each step does and keeps: scripts/uninstall_old.py, and the tutorial's section 「卸载旧版（59604f30）」.
. "$(dirname "$0")/lib.sh"
[ $# -ge 2 ] || fail "用法：bin/uninstall_old.sh plan|remove|purge-data|check <旧版目录>"
# The old folder is the first argument after the action that is neither an option nor an option's value.
old=""
shift_next=""
for a in "${@:2}"; do
  if [ -n "$shift_next" ]; then shift_next=""; continue; fi
  case "$a" in
    --backup) shift_next=1 ;;
    --*) ;;
    *) old=$a; break ;;
  esac
done
[ -n "$old" ] && [ -f "$old/deploy.env" ] || fail "${old:-（没有写）} 里没有 deploy.env：写旧版的目录，比如 ~/vllm-pd-grafana-panel2-59604f30"
# The old folder's deploy.env has every setting this needs; scripts/uninstall_old.py reads it and derives the names it
# leaves blank (scripts/names.py). This pack's own deploy.env is not read: it may not exist yet, or be another cluster's.
unset PYTHONHOME PYTHONPATH LD_PRELOAD LD_LIBRARY_PATH TIMING_HOME
need_python
exec "$PYTHON" "$PACK_DIR/scripts/uninstall_old.py" "$@"
