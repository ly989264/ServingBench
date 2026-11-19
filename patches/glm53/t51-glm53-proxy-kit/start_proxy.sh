#!/bin/bash
# Starts the proxy again exactly as it ran when this folder was made (arguments, environment, working directory,
# stdout and stderr), with the two PD_PROXY_DIAG settings added when the first argument is "on".
BK=$(cd "$(dirname "$0")" && pwd)
mapfile -d '' -t A < "$BK/cmdline"
mapfile -d '' -t E < "$BK/environ"
if [ "${1:-}" = on ]; then E+=(PD_PROXY_DIAG=1 PD_PROXY_DIAG_DIR=/data/pd-diag/logs/proxy); fi
cd "$(cat "$BK/cwd")" || exit 1
exec env -i "${E[@]}" sh -c 'o=$1; e=$2; shift 2; exec "$@" >> "$o" 2>> "$e" < /dev/null' sh "$(cat "$BK/stdout")" "$(cat "$BK/stderr")" "${A[@]}"
