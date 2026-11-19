#!/bin/bash
# Stops the running proxy: its main python process (SIGTERM, which also ends the uvicorn workers; up to 60 s), then
# checks that no proxy process is left. A shell that started it ends by itself. Only bash, kill, sleep, readlink, tr, grep.
proxies() {   # python processes whose command line names the proxy file
  local d
  for d in /proc/[0-9]*; do
    tr '\0' ' ' < "$d/cmdline" 2>/dev/null | grep -q 'load_balance_proxy_server_example' || continue
    case "$(readlink "$d/exe" 2>/dev/null)" in *python*) echo "${d#/proc/}" ;; esac
  done
}
py=" $(proxies | tr '\n' ' ') "
main=""
for p in $py; do
  pp=""
  while read -r k v _; do [ "$k" = "PPid:" ] && pp=$v; done < "/proc/$p/status"
  case "$py" in *" $pp "*) ;; *) main="$main $p" ;; esac
done
set -- $main
[ $# -eq 0 ] && { echo "没有在跑的 proxy"; exit 0; }
[ $# -gt 1 ] && { echo "有 $# 个 proxy（$main）：停下"; exit 1; }
P=$1
kill "$P"; for i in $(seq 60); do kill -0 "$P" 2>/dev/null || break; sleep 1; done; sleep 3
[ -n "$(proxies)" ] && { echo "停了 $P 以后还有 proxy 进程：停下"; exit 1; }
echo "已停 proxy（进程 $P）"
