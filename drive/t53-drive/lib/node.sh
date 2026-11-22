#!/bin/bash
# t53drive, machine side. The monitor runs it through  ssh root@<machine> bash /tmp/t53/lib/node.sh <cmd> KEY=VALUE ...
# Prints sections ("== name") with the raw output of each check, and "K key=value" lines the monitor keeps; the monitor
# judges. Never stops or starts vLLM, never removes a container.
#   find                 the vLLM container (C=- : the one container on this machine with vllm serve in it)
#   pre                  stage ① (read only, plus the kits unpacked in /tmp/t53 inside the container)
#   mcexc                stage ① for one P and one D: the .comment exception's two read-only commands
#   prep                 stage ② on the machine: backup, p2d replay to /tmp/t53
#   prep2                stage ② after the panel's add: node files seen in the container, log folder writable
#   stopped              stage ③: no vLLM process left, same container, files still there
#   swap                 stage ④
#   started              stage ⑤
#   late                 stage ⑥ (startup lines again)
#   rb_check | rb_restore | rb_started    rollback
#   pxpre | pxprep | pxswap | pxstarted | pxrestore    the proxy's machine (PROXY=on)
set -u
CMD=$1; shift
for kv in "$@"; do eval "${kv%%=*}=\$(printf '%s' \"\${kv#*=}\")"; done
T=/tmp/t53
C=${C:--}; ROLE=${ROLE:-}; PORTS=${PORTS:-}
sec() { echo "== $*"; }
K() { echo "K $1=$2"; }
probe() { docker exec -i "$C" python3 -I -S -B - "$@" < $T/lib/c_probe.py 2>&1; }
act() { docker exec -i "$C" python3 -I -S -B - "$@" < $T/lib/c_act.py 2>&1; }
dx() { docker exec "$C" "$@"; }

find_container() {
  if [ "$C" != - ]; then
    docker inspect -f '{{.State.Running}}' "$C" 2>/dev/null | grep -qx true || { echo "find=not_running $C"; return 1; }
    echo "find=given"; return 0
  fi
  local hits=() n
  for n in $(docker ps --format '{{.Names}}'); do
    docker top "$n" -eo pid,args 2>/dev/null | grep -qE 'vllm serve|vllm\.entrypoints\.openai\.api_server' && hits+=("$n")
  done
  [ ${#hits[@]} = 1 ] || { echo "find=found_${#hits[@]}"; return 1; }
  C=${hits[0]}; echo "find=found"
}

kit_in() {   # the patch kit (and the Mooncake kit) unpacked inside the container, checked against SHA256SUMS
  dx mkdir -p $T || return 1
  docker cp $T/kits/t58-glm53-patch-kit.tar "$C:$T/" >/dev/null && dx sh -c "mkdir -p $T/kit && tar -xf $T/t58-glm53-patch-kit.tar -C $T/kit && cd $T/kit && LC_ALL=C sha256sum -c SHA256SUMS | grep -v ': OK\$'; echo kit_sums_done" || return 1
  if [ "${MOONCAKE:-skip}" = replace ]; then
    docker cp $T/kits/t58-glm53-mooncake-kit.tar "$C:$T/" >/dev/null && dx sh -c "mkdir -p $T/mc && tar -xf $T/t58-glm53-mooncake-kit.tar -C $T/mc && cd $T/mc && LC_ALL=C sha256sum -c SHA256SUMS | grep -v ': OK\$'; echo mc_sums_done" || return 1
  fi
}

detect() {   # the T58 kit's detector, with the start files found by the probe; detail file removed afterwards
  local a=(--out-dir $T/det)
  [ -n "${TMPL:-}" ] && [ "$TMPL" != - ] && a+=(--run-dp-template "$TMPL")
  [ -n "${LAUNCH:-}" ] && [ "$LAUNCH" != - ] && a+=(--launch-online-dp "$LAUNCH")
  a+=(--start-script "$START")
  [ -n "${MCJSON:-}" ] && [ "$MCJSON" != - ] && a+=(--mooncake-json "$MCJSON")
  [ -n "${STARTMC:-}" ] && [ "$STARTMC" != - ] && a+=(--start-mooncake "$STARTMC")
  [ -n "${LOG1:-}" ] && [ "$LOG1" != - ] && a+=(--startup-log "$LOG1")
  [ -n "${BASELINE:-}" ] && a+=(--baseline "$BASELINE")
  dx mkdir -p $T/det
  dx python3 -I -S -B $T/kit/detect/t51_glm53_check.py "${a[@]}" 2>&1; echo "detect_exit=$?"
  dx sh -c "rm -f $T/det/*"
}

start_files() {   # from the probe's lines: the start script (list wins), launcher, template, mooncake files, first log
  local pr=$1 d
  START=${START_GIVEN:-}
  [ -n "$START" ] && [ "$START" != - ] || START=$(sed -n 's/^start_scripts=//p' "$pr")
  LAUNCH=$(sed -n 's/^launchers=//p' "$pr"); TMPL=$(sed -n 's/^templates=//p' "$pr")
  LOGS=$(sed -n 's/^logs=//p' "$pr"); LOG1=${LOGS%%;*}
  MCJSON=$(sed -n 's/^env_MOONCAKE_CONFIG_PATH=//p' "$pr"); case $MCJSON in *"<unset>"*|*"|"*|"") MCJSON=- ;; esac
  d=$(dirname "$START"); STARTMC=-
  [ "$MCJSON" = - ] && dx test -f "$d/mooncake.json" && MCJSON=$d/mooncake.json
  dx test -f "$d/start_mooncake.sh" && STARTMC=$d/start_mooncake.sh
  K start "$START"; K launch "${LAUNCH:--}"; K tmpl "${TMPL:--}"; K logs "${LOGS:--}"; K mcjson "$MCJSON"; K startmc "$STARTMC"
}

bkdir() {   # the backup folder in the container: /tmp/t53/backup, or /root/t53-backup when /tmp is a tmpfs
  local fs; fs=$(probe mounttype /tmp | sed -n 's/^fstype=//p')
  [ "$fs" = tmpfs ] && echo /root/t53-backup || echo $T/backup
}

case $CMD in
find)
  find_container; K container "$C"
  ;;
pre)
  sec t64
  # T64 done on site: no mock of the old panel two runs on this machine (any account). Folders do not tell: panel two
  # v3.4-extra itself puts ~/vllm-timing (its Python) and ~/vllm-timing-node on every node.
  for f in /proc/[0-9]*/cmdline; do tr '\0' ' ' < "$f" 2>/dev/null; echo; done | grep -E 'mock/(mockgen|pd032_mock)\.py' | grep -v grep | head -n 1 | cut -c1-80 | sed 's/^/t64_left=/'; echo t64_done
  sec container
  find_container || exit 0
  K container "$C"; K cid "$(docker inspect -f '{{.Id}}' "$C" | cut -c1-16)"
  sec devino
  p=/data/pd-diag; while [ ! -e "$p" ] && [ "$p" != / ]; do p=$(dirname "$p"); done
  echo "host $(stat -c %d:%i "$p") $p"; echo "cont $(probe devino "$p")"
  sec kit
  kit_in; echo "kit_exit=$?"
  sec procs
  probe procs > $T/procs.$C.txt; cat $T/procs.$C.txt
  start_files $T/procs.$C.txt
  sec detect
  BASELINE= detect   # stage ① makes the token; a rerun of ① must not compare with an earlier one
  sec files
  probe files $T/kit/patches
  sec p2d
  F=${PURELIB:-$(sed -n 's/^purelib=//p' $T/procs.$C.txt)}/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py
  dx python3 -I -S -B $T/kit/patches/p2d/replay_p2d.py "$F" --check 2>&1 | tail -n 40; echo "p2d_check_exit=${PIPESTATUS[0]}"
  dx python3 -I -S -B -c "import sys; sys.path.insert(0, sys.argv[2]); import extra_checks; print(extra_checks.extra_line(open(sys.argv[1]).read()))" "$F" $T/kit/patches/p2d/field_check 2>&1
  sec exports
  probe exports "$START"
  sec space
  probe mounttype /tmp | sed 's/^/tmp_/'
  probe dfree "$p"
  sec logsizes
  [ -n "$LOGS" ] && [ "$LOGS" != - ] && probe sizes $(echo "$LOGS" | tr ';' ' ')
  if [ "${MOONCAKE:-skip}" = replace ]; then
    sec mcpre
    dx python3 -I -S -B $T/mc/precheck/dist/t51_mc_precheck.py 2>&1 | tail -n 20; echo "mcpre_exit=$?"
  fi
  sec end
  ;;
mcexc)   # one P and one D, only after every container reported the .comment-only STOP: the two read-only commands
  sec mcexc
  docker cp $T/kits/t53-mc-exception "$C:$T/" >/dev/null
  dx sh -c "cd $T/t53-mc-exception && LC_ALL=C sha256sum -c SHA256SUMS | grep -v ': OK\$'; echo exc_sums_done"
  pid=$(probe mcpid | sed -n 's/^mc_pid=//p'); echo "mc_pid=${pid:-none}"
  if [ -n "$pid" ] && [ "$pid" != none ]; then
    dx env PID="$pid" bash $T/t53-mc-exception/mc_comment.cmd.sh 2>&1 | tail -n 8
    probe mem
    if probe mem | grep -qx mem_ok=1; then dx env PID="$pid" bash $T/t53-mc-exception/mc_bytes.cmd.sh 2>&1 | tail -n 4; else echo "mc_bytes skipped=memory"; fi
  fi
  ;;
prep)
  BK=$(bkdir); K bk "$BK"
  sec backup
  MCLIB=-
  if [ "${MOONCAKE:-skip}" = replace ]; then
    MCLIB=$(dx python3 -I -B -c "import importlib.util as u; print(list(u.find_spec('mooncake').submodule_search_locations)[0])")/libmooncake_store.so
    K mclib "$MCLIB"
  fi
  if [ "$MCLIB" = - ]; then act backup $T/kit/patches "$BK" "$START"; else act backup $T/kit/patches "$BK" "$START" "$MCLIB"; fi
  if [ "${MOONCAKE:-skip}" = replace ]; then   # vLLM's LD_LIBRARY_PATH for the import check (old step 71)
    dx sh -c "for d in /proc/[0-9]*; do a=\$(tr '\0' ' ' < \"\$d/cmdline\" 2>/dev/null); case \"\$a\" in *'vllm serve'*|*vllm.entrypoints.openai.api_server*) tr '\0' '\n' < \"\$d/environ\" | grep '^LD_LIBRARY_PATH=' > $T/vllm.ld_library_path; break;; esac; done; wc -c < $T/vllm.ld_library_path" | sed 's/^/ldlp_bytes=/'
  fi
  rm -rf "$T/out/$NAME"; mkdir -p "$T/out"
  docker cp "$C:$BK" "$T/out/$NAME" >/dev/null && echo "copied_out=1"
  (cd "$T/out/$NAME" && LC_ALL=C sha256sum -c SHA256SUMS | grep -v ': OK$'; echo host_sums_done)
  sec replay
  F=${PURELIB:-$(sed -n 's/^purelib=//p' $T/procs.$C.txt)}/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py
  dx python3 -I -S -B $T/kit/patches/p2d/replay_p2d.py "$F" --out $T/mooncake_connector.p2d58.py 2>&1 | tail -n 3; echo "replay_exit=${PIPESTATUS[0]}"
  dx sha256sum $T/mooncake_connector.p2d58.py | sed 's/^/s58 /'
  ;;
prep2)
  sec nodefiles
  ls /data/pd-diag/conf 2>&1 | tr '\n' ' '; echo
  dx ls /data/pd-diag/conf 2>&1 | tr '\n' ' '; echo
  echo "host $(stat -c %d:%i /data/pd-diag/conf 2>/dev/null)"; echo "cont $(probe devino /data/pd-diag/conf)"
  sec writable
  probe writable /data/pd-diag/logs
  ;;
stopped)
  sec stopped
  docker inspect -f '{{.State.Running}} {{.Id}}' "$C" 2>&1 | awk '{print "running=" $1; print "cid=" substr($2, 1, 16)}'
  probe noproc
  for f in $T/kit/SHA256SUMS $T/mooncake_connector.p2d58.py "$BK/SHA256SUMS"; do dx test -f "$f" && echo "have $f" || echo "lost $f"; done
  dx sha256sum $T/mooncake_connector.p2d58.py 2>/dev/null | sed 's/^/s58 /'
  act verify "$BK"
  ;;
swap)
  sec swap
  probe noproc
  # ②'s kit and p2d result in the container gone (container /tmp emptied): redo them; the connector is still the
  # site's original here (files are swapped only below), so the replay gives the same result, checked by apply
  if ! dx sh -c "cd $T/kit && LC_ALL=C sha256sum -c --quiet SHA256SUMS" >/dev/null 2>&1 \
     || [ "$(dx sha256sum $T/mooncake_connector.p2d58.py 2>/dev/null | cut -c1-64)" != "$S58" ]; then
    echo "prepared=redo"
    kit_in > /dev/null || echo "kit_in=fail"
    F=${PURELIB:-$(sed -n 's/^purelib=//p' $T/procs.$C.txt)}/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py
    dx python3 -I -S -B $T/kit/patches/p2d/replay_p2d.py "$F" --out $T/mooncake_connector.p2d58.py 2>&1 | tail -n 1
  fi
  act apply $T/kit/patches $T/mooncake_connector.p2d58.py "$S58"
  act exports "$START" "$INSERT" "$ANCHOR"
  probe exports "$START"
  if [ "${MOONCAKE:-skip}" = replace ]; then
    act mcswap $T/mc/lib/libmooncake_store.so "$MCLIB" "$MC_NEW_SHA"
    probe mcmaps
  fi
  if [ "${FIRST:-0}" = 1 ]; then
    sec import
    dx python3 -B -c "import time, resource; t = time.time(); import vllm_ascend.distributed.kv_transfer.kv_p2p.mooncake_connector as m; print('import_conn', hasattr(m.KVCacheRecvingThread, '_transfer_batch'), '%.1f s' % (time.time() - t), '%d MiB' % (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024))" 2>&1 | tail -n 3
    if [ "${MOONCAKE:-skip}" = replace ]; then
      dx sh -c "L=\$(cat $T/vllm.ld_library_path 2>/dev/null); [ -n \"\$L\" ] && export \"\$L\"; python3 -B -c \"import time; t = time.time(); import mooncake.store as s; print('import_mc', s.__file__, '%.1f s' % (time.time() - t))\"" 2>&1 | tail -n 3
    fi
  fi
  ;;
started)
  sec health
  dx python3 -I -S -B -c '
import sys, urllib.request
o = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for p in sys.argv[1].split(","):
    try:
        c = o.open("http://127.0.0.1:%s/health" % p, timeout=5).status
    except Exception as e:
        c = getattr(e, "code", 0)
    print("health %s %s" % (p, c))' "$PORTS" 2>&1
  sec procs
  probe procs
  sec scan
  [ -n "${OFFSETS:-}" ] && probe scan $(echo "$OFFSETS" | tr ';' ' ')
  sec detect
  detect
  if [ "${MOONCAKE:-skip}" = replace ]; then sec mcmaps; probe mcmaps; fi
  ;;
late)
  sec scan
  [ -n "${OFFSETS:-}" ] && probe scan $(echo "$OFFSETS" | tr ';' ' ')
  sec disk
  echo "now $(date +%s) logs_bytes $(du -sb /data/pd-diag/logs 2>/dev/null | cut -f1)"
  ;;
rb_check)
  sec rbcheck
  docker inspect -f '{{.Id}}' "$C" 2>&1 | cut -c1-16 | sed 's/^/cid=/'
  if dx test -f "$BK/SHA256SUMS"; then act verify "$BK"; else echo "bk_lost=1"; fi
  ;;
rb_restore)
  sec restore
  probe noproc
  docker inspect -f '{{.Id}}' "$C" 2>&1 | cut -c1-16 | sed 's/^/cid=/'
  if ! dx test -f "$BK/SHA256SUMS"; then   # the container's copy is gone: bring the monitor's copy back in
    dx mkdir -p "$BK" && docker cp "$T/back/$NAME/." "$C:$BK" >/dev/null && echo "backup_from_monitor=1"
  fi
  dx test -f $T/kit/SHA256SUMS || { kit_in >/dev/null; echo "kit_again=1"; }
  if [ "${MCLIB:--}" = - ]; then act restore $T/kit/patches "$BK" "$START"; else act restore $T/kit/patches "$BK" "$START" "$MCLIB"; fi
  probe exports "$START"
  if [ "$ROLE" = P ] && [ -d /data/pd-diag/logs/mooncake ]; then   # moved aside, never deleted
    mv /data/pd-diag/logs/mooncake /data/pd-diag/mooncake-rolled-back-$(date +%Y%m%d-%H%M%S) && echo "mc_logs_moved=1"
  fi
  ;;
rb_started)
  sec health
  dx python3 -I -S -B -c '
import sys, urllib.request
o = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for p in sys.argv[1].split(","):
    try:
        c = o.open("http://127.0.0.1:%s/health" % p, timeout=5).status
    except Exception as e:
        c = getattr(e, "code", 0)
    print("health %s %s" % (p, c))' "$PORTS" 2>&1
  sec procs
  probe procs
  sec detect
  detect
  ;;
pxpre)
  sec proxy
  docker inspect -f '{{.State.Running}}' "$C" 2>&1 | sed 's/^/running=/'
  probe proxy "$PXFILE"
  ;;
pxprep)
  sec proxy
  docker exec -i "$C" python3 -I -S -B - --log-dir /data/pd-diag/logs --expect-sha256 "$PX_ORIG" < $T/kits/t51-glm53-proxy-kit/t51_proxy_check.py 2>&1 | grep -E '^(proxies|logdir|RESULT)'
  mkdir -p $T/out/proxy && docker cp "$C:$PXFILE" $T/out/proxy/orig.py >/dev/null && sha256sum $T/out/proxy/orig.py | sed 's/^/pxbk /'
  dx mkdir -p $T && docker cp $T/kits/proxy_new.py "$C:$T/proxy_new.py" >/dev/null && dx sha256sum $T/proxy_new.py | sed 's/^/pxnew /'
  echo "host $(stat -c %d:%i /data/pd-diag/logs)"; echo "cont $(probe devino /data/pd-diag/logs)"
  ;;
pxswap)
  sec proxy
  act pxswap "$PXFILE" $T/proxy_new.py "$PX_ORIG" "$PX_NEW"
  echo "swap_uptime=$(cut -d' ' -f1 /proc/uptime) boot_id=$(cat /proc/sys/kernel/random/boot_id)"
  ;;
pxstarted)
  sec proxy
  probe proxy "$PXFILE"
  echo "boot_id=$(cat /proc/sys/kernel/random/boot_id) hz=$(dx getconf CLK_TCK 2>/dev/null || echo 100)"
  n=$(cat /data/pd-diag/logs/proxy/*/proxy.*.log 2>/dev/null | grep -c '"event":"pd_proxy_request"'); echo "proxy_lines=$n"
  ;;
pxrestore)
  sec proxy
  docker cp $T/out/proxy/orig.py "$C:$T/proxy_orig.py" >/dev/null
  act pxrestore "$PXFILE" $T/proxy_orig.py "$PX_ORIG"
  ;;
*) echo "unknown command $CMD"; exit 2 ;;
esac
