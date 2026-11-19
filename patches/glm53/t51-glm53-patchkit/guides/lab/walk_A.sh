#!/bin/bash
# Walk guide A (glm5.3) in a throwaway ${IMG:-python:3.11.10-slim} container (--rm), with the guide's own command blocks:
# steps 1, 3 (incl. the backup round trip host -> monitor), 4, the parts of 7 that run in a container, and 9 (restore
# from the monitor's copy).
# Usage: walk_A.sh SCRATCH SRC_DIR KIT_TAR DETECTOR [clean|site]
#   SRC_DIR holds vllm-0300 (git archive v0.30.0) and asc-d4a (git archive d4ad49796); copied, never modified.
#   clean: mooncake_connector.py is the public d4ad49796 file. site: it is replaced first by p2d's
#   tools/simulate_site_edits.py mode C (edits on the lines the site's file is known to change), a site-like file.
# The monitor is $W/mon and the machine's host is $W/host on this Mac: scp/ssh become cp/mkdir, docker cp is real.
set -u
SCRATCH=$1; SRC=$2; TAR=$3; DET=$4; MODE=${5:-clean}
IMG=${IMG:-python:3.11.10-slim}
HERE=$(cd "$(dirname "$0")" && pwd)
KIT=$(cd "$HERE/../.." && pwd)
GUIDE="$KIT/guides/操作说明A_glm53_容器内打补丁.zh-CN.md"
P2D_SIM=${P2D_SIM:?set P2D_SIM to patches/glm53/t51-glm53-p2d/tools/simulate_site_edits.py}
W="$SCRATCH/walkA-$MODE"; rm -rf "$W"; mkdir -p "$W/site" "$W/mon/vllm-pd-patch-glm53" "$W/host"
cp -R "$SRC/vllm-0300/vllm" "$W/site/vllm"; cp -R "$SRC/asc-d4a/vllm_ascend" "$W/site/vllm_ascend"
# what a pip install leaves next to the code: version files and dist-info, so the detector sees installed packages
printf '__version__ = version = "0.30.0"\n__version_tuple__ = version_tuple = (0, 30, 0)\n' > "$W/site/vllm/_version.py"
printf '__version__ = version = "0.30.0rc2.dev157+gd4ad49796.d20261018"\n' > "$W/site/vllm_ascend/_version.py"
for nv in "vllm 0.30.0" "vllm_ascend 0.30.0rc2.dev157+gd4ad49796.d20261018"; do
  set -- $nv; d="$W/site/$1-$2.dist-info"; mkdir -p "$d"
  printf "Metadata-Version: 2.1\nName: %s\nVersion: %s\n" "$(echo $1 | tr _ -)" "$2" > "$d/METADATA"; echo "$1" > "$d/top_level.txt"
done
CONN=vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py
if [ "$MODE" = site ]; then
  python3 "$P2D_SIM" "$W/site/$CONN" "$W/mc_site.py" C && cp "$W/mc_site.py" "$W/site/$CONN"
fi
echo "### mode $MODE: mooncake_connector.py sha16 $(shasum -a 256 "$W/site/$CONN" | cut -c1-16)"
cp "$TAR" "$W/mon/vllm-pd-patch-glm53/"; (cd "$W/mon/vllm-pd-patch-glm53" && shasum -a 256 "$(basename "$TAR")" > "$(basename "$TAR").sha256")

# the guide's code blocks, in order; picked by content, not position
python3 - "$GUIDE" "$W/blocks" <<'PY'
import re, sys, os
text = open(sys.argv[1], encoding="utf-8").read()
blocks = re.findall(r"```bash\n(.*?)```", text, re.S)
os.makedirs(sys.argv[2], exist_ok=True)
for i, b in enumerate(blocks):
    open(os.path.join(sys.argv[2], "%02d.sh" % i), "w").write(b)
print(len(blocks), "bash blocks in the guide")
PY
pick() { grep -lF -- "$1" "$W"/blocks/*.sh | head -1; }
MONSHA=$(pick 'sha256sum -c t51-glm53-patch-kit.tar.sha256'); DCP=$(pick 'docker cp /tmp/t51-glm53-patch-kit.tar')
UNPACK=$(pick 'tar -xf /tmp/t51-glm53-patch-kit.tar'); TMPFS=$(pick 'df -hT /tmp'); DIRS=$(pick 'VLLM_PKG=$(python3')
VERIFY=$(pick 'NEW_FILES.txt); do [ -e'); LOCAL=$(pick 'grep -nE "original|_orig|kwargs"'); BACKUP=$(pick 'BKBASE=/tmp')
HOSTCP=$(pick 'docker cp <容器名>:<备份目录>'); MONCP=$(pick 'scp -r <SSH 账号>@<机器地址>:/tmp/t51-glm53-backup-')
MONVER=$(pick 'backup/<机器>-<时间戳> && LC_ALL=C sha256sum -c SHA256SUMS'); GUARD=$(pick '"${VLLM_PKG:?}"')
V=$(pick 'vllm/files/vllm/.'); A=$(pick 'vllm-ascend/files/vllm_ascend/.'); PCHK=$(pick '"$F" --check')
EXTRA=$(pick 'extra_checks.extra_line'); POUT=$(pick '--out /tmp/mooncake_connector.p2d.py'); PREP=$(pick 'cp /tmp/mooncake_connector.p2d.py "$F"')
IMP=$(pick 'resource.getrusage'); CMDB=$(pick 'cmdline.before"; cat'); CMDA=$(pick 'cmdline.after"; diff'); ENVC=$(pick 'VLLM_LOGGING_CONFIG_PATH=.*/pd_diag_logging')
DFH=$(pick 'df -hT <节点目录>'); DISK0=$(pick 't51-glm53-disk.t0 && echo'); DISK1=$(pick 'tail -c +$((s0 + 1))')
RSCP=$(pick 'ssh <SSH 账号>@<机器地址> mkdir -p /tmp/<机器>-<时间戳>'); RDCP=$(pick 'docker cp /tmp/<机器>-<时间戳>/.')
RVER=$(pick 'cd /tmp/<机器>-<时间戳> && LC_ALL=C sha256sum -c SHA256SUMS'); RB=$(pick 'BK=/tmp/<机器>-<时间戳>; K='); PYC=$(pick '新模块的 .pyc 已删')
for b in MONSHA DCP UNPACK TMPFS DIRS VERIFY LOCAL BACKUP HOSTCP MONCP MONVER GUARD V A PCHK EXTRA POUT PREP IMP CMDB CMDA ENVC DFH DISK0 DISK1 RSCP RDCP RVER RB PYC; do
  [ -n "${!b}" ] || { echo "block $b not found"; exit 1; }
done
# the detector of the delivered tar (step 2 / 8 run it from /tmp/t51-kit/detect); DETECTOR is only mounted for reference
DET_IN="python3 -I -S -B /tmp/t51-kit/detect/t51_glm53_check.py --out-dir /tmp --max-seconds 120 2>&1 | grep -E '^(vllm|ascend)=|^(site_patches|strict|verdict) |REFUSED|BLOCKED' | cut -c1-260"
lbl() { echo "echo '### $(basename "$1") ${2:-}'"; }
MACH=lab-d0
NEWPYC='for f in $(sed "s#^#$VLLM_ROOT/#" $K/vllm/NEW_FILES.txt) $(sed "s#^#$ASCEND_ROOT/#" $K/vllm-ascend/NEW_FILES.txt); do ls "$(dirname "$f")/__pycache__/$(basename "$f" .py)".*.pyc 2>/dev/null; done | wc -l'

# phase 1 (container): steps 1 and 3 up to the backup; the variables are kept for the next phases, as the open shell would
{
  echo 'set -u'
  lbl "$UNPACK"; cat "$UNPACK"; lbl "$TMPFS"; cat "$TMPFS"
  for b in "$DIRS" "$VERIFY"; do lbl "$b"; cat "$b"; done
  lbl "$LOCAL" '(the two local patch files of the site, not in the public tree)'; cat "$LOCAL"
  lbl "$BACKUP"; cat "$BACKUP"
  echo "echo '### detector before'"; echo "$DET_IN"
  echo 'declare -p VLLM_PKG ASCEND_PKG VLLM_ROOT ASCEND_ROOT F K BK > /tmp/walkenv'
} > "$W/p1.sh"
# phase 2 (container): step 4
{
  echo 'set -u'
  echo "echo '### $(basename "$GUARD") in a new shell without the variables (must refuse)'"
  echo "bash -c '$(sed "s/'/'\\\\''/g" "$GUARD")' 2>&1; echo \"rc=\$?\""
  echo '. /tmp/walkenv'
  lbl "$GUARD" '(in the shell of step 3)'; cat "$GUARD"
  for b in "$V" "$A" "$PCHK" "$EXTRA" "$POUT" "$PREP"; do lbl "$b"; cat "$b"; done
  echo "echo '### every replaced and replayed file compiles with this Python'"
  echo 'python3 -V; (cd "$VLLM_ROOT" && python3 -m py_compile $(awk "{print \$2}" $K/vllm/SHA256SUMS.after) && echo "vllm: $(wc -l < $K/vllm/SHA256SUMS.after) files compile"); (cd "$ASCEND_ROOT" && python3 -m py_compile $(awk "{print \$2}" $K/vllm-ascend/SHA256SUMS.after) "$F" && echo "vllm-ascend: $(wc -l < $K/vllm-ascend/SHA256SUMS.after) files + mooncake_connector.py compile")'
  echo "echo \"### .pyc of the 4 new modules after py_compile (step 9 removes them): \$($NEWPYC)\""
  echo "echo '### detector after'"; echo "$DET_IN"
} > "$W/p2.sh"
# phase 3 (container): step 7.1 / 7.2 command lines and logging config, with stand-in "vllm serve" processes
{
  echo 'set -u; . /tmp/walkenv'
  echo 'mkdir -p /tmp/fakebin && printf "#!/usr/bin/env python3\nimport time\ntime.sleep(3600)\n" > /tmp/fakebin/vllm && chmod +x /tmp/fakebin/vllm'
  echo 'up() { for p in "$@"; do VLLM_LOGGING_CONFIG_PATH=/lab/node/conf/pd_diag_logging.json /tmp/fakebin/vllm serve /models/glm --port $p --tensor-parallel-size 1 >/dev/null 2>&1 < /dev/null & done; sleep 1; }'
  echo 'down() { for d in /proc/[0-9]*; do case "$(tr "\0" " " < $d/cmdline 2>/dev/null)" in *"vllm serve"*) kill ${d#/proc/};; esac; done; sleep 1; }'
  echo "echo '### stand-ins: 3 instances (ports 9100-9102)'; up 9100 9101 9102"
  lbl "$CMDB" '(before stopping)'; sed "s#^BK=<备份目录>#BK=\$BK#" "$CMDB"
  echo "echo '### stop the instances (step 7.1)'; down"
  lbl "$IMP" '(batch 1 only, instances stopped; no torch in this image: an AST check instead; the import itself ran in t51g-vllmtest:v1, import_measure.txt)'
  echo 'python3 -c "import ast, sys; t = ast.parse(open(sys.argv[1]).read()); print(any(isinstance(n, ast.ClassDef) and n.name == \"KVCacheRecvingThread\" and any(isinstance(m, ast.FunctionDef) and m.name == \"_transfer_batch\" for m in n.body) for n in ast.walk(t)))" "$F"'
  echo "echo '### start them again with the same arguments'; up 9100 9101 9102"
  lbl "$CMDA" '(after the restart)'; sed "s#^BK=<备份目录>#BK=\$BK#" "$CMDA"
  lbl "$ENVC" '(all stand-ins carry the logging config)'; cat "$ENVC"
  echo "echo '### restart with one port changed (9102 -> 9103): the comparison must show it'; down; up 9100 9101 9103"
  lbl "$CMDA"; sed "s#^BK=<备份目录>#BK=\$BK#" "$CMDA"; echo 'echo "rc=$?"'
  echo "echo '### one more stand-in without the logging config: the check must name it'"
  echo '/tmp/fakebin/vllm serve /models/glm --port 9104 >/dev/null 2>&1 < /dev/null & sleep 1'
  lbl "$ENVC"; cat "$ENVC"
  echo 'down'
} > "$W/p3.sh"
# phase 4 (container standing in for the D host): step 7.1 df and 7.4 disk growth, with a made-up node dir and two logs
{
  echo 'set -u'
  echo 'mkdir -p /lab/node/logs/9100 /lab/log && head -c 1000 /dev/zero > /lab/node/logs/9100/api.log && printf "INFO start\n" > /lab/log/v9100.log && printf "INFO start\n" > /lab/log/v9101.log'
  lbl "$DFH"; sed 's#<节点目录>#/lab/node#; s#<vLLM 日志文件>#/lab/log/v9100.log /lab/log/v9101.log#' "$DFH"
  lbl "$DISK0" '(start)'; sed 's#<节点目录>#/lab/node#; s#<vLLM 日志文件>#/lab/log/v9100.log /lab/log/v9101.log#' "$DISK0"
  echo "echo '### written during the window: 3000000 bytes into the node dir; 100 [BatchPutTiming] lines of 312 bytes (31200 bytes) and 50 other lines into the vLLM logs; 0 [PD_DIAG] lines'"
  echo 'head -c 3000000 /dev/zero > /lab/node/logs/9100/engine.log'
  echo 'L=$(printf "%0300d" 0); for i in $(seq 50); do printf "I1120 10:00:00.000000 1234567 x.cpp:1] [BatchPutTiming] %s\n" "${L:0:255}" >> /lab/log/v9100.log; printf "I1120 10:00:00.000000 1234567 x.cpp:1] [BatchPutTiming] %s\n" "${L:0:255}" >> /lab/log/v9101.log; echo "INFO other line" >> /lab/log/v9101.log; done'
  echo 'echo "BatchPutTiming bytes written: $(grep -h "\[BatchPutTiming\]" /lab/log/*.log | wc -c)"'
  echo 'sleep 5'
  lbl "$DISK1" '(after the window; here 5 s instead of 30 min)'; sed 's#<节点目录>#/lab/node#' "$DISK1"
  echo 'echo "expected, from the same start and the current time: $(awk -v t=$(( $(date +%s) - $(sed -n 1p ~/t51-glm53-disk.t0) )) "BEGIN { printf \"node %.1f GB/day, BatchPutTiming %.2f GB/day\", 3000000 / t * 86400 / 1e9, 31200 / t * 86400 / 1e9 }")"'
  echo "echo '### one [PD_DIAG] line in a vLLM log (logging config not in effect): the count must show it'"
  echo 'echo "INFO [PD_DIAG] {\"event\":\"api_summary\"}" >> /lab/log/v9100.log'
  sed 's#<节点目录>#/lab/node#' "$DISK1"
} > "$W/p4.sh"
# phase 5 (container): step 9 with the backup sent back from the monitor
{
  echo 'set -u'
  lbl "$RVER" '(the copy sent back from the monitor)'; sed "s#<机器>-<时间戳>#$MACH-TS_HERE#g" "$RVER"
  lbl "$DIRS" '(a new shell: the directories again)'; cat "$DIRS"
  lbl "$RB" '(rollback)'; sed "s#<机器>-<时间戳>#$MACH-TS_HERE#g" "$RB"
  lbl "$PYC"; cat "$PYC"
  echo "echo \"### .pyc of the 4 new modules left: \$($NEWPYC)\""
  echo "echo '### detector after rollback'"; echo "$DET_IN"
} > "$W/p5.sh"

C=t51g53-walka-$MODE
docker rm -fv $C >/dev/null 2>&1
docker run --rm -d --name $C -v "$W/site:/siteseed:ro" -v "$(dirname "$DET"):/det:ro" $IMG sleep 900 >/dev/null
docker exec $C sh -c 'cp -r /siteseed/. /usr/local/lib/python3.11/site-packages/'
echo "### $(basename "$MONSHA") (on the monitor; here a --rm container with the delivered tar and its .sha256)"
docker run --rm -v "$W/mon:/mon" $IMG bash -c "$(sed 's#<用户目录>#/mon#' "$MONSHA")"
echo "### $(basename "$DCP") (on the machine; here the Mac)"
sed "s#/tmp/t51-glm53-patch-kit.tar#$TAR#; s#<容器名>#$C#" "$DCP" > "$W/dcp.sh"; bash "$W/dcp.sh"; echo "docker cp rc=$?"
echo "### phase 1 in the container"
docker exec -i $C bash < "$W/p1.sh"
BK=$(docker exec $C bash -c '. /tmp/walkenv && echo "$BK"'); TS=${BK##*/t51-glm53-backup-}
echo "### $(basename "$HOSTCP") (on the machine; here the Mac, host dir \$W/host)"
sed "s#<容器名>#$C#; s#<备份目录>#$BK#; s# /tmp/\$# $W/host/#" "$HOSTCP" > "$W/hostcp.sh"; cat "$W/hostcp.sh"; bash "$W/hostcp.sh"; echo "docker cp rc=$?"
echo "### $(basename "$MONCP") (on the monitor; here scp -r becomes cp -R from \$W/host)"
sed "s#scp -r <SSH 账号>@<机器地址>:/tmp/#cp -R $W/host/#; s#<用户目录>#$W/mon#g; s#<机器>#$MACH#g; s#<时间戳>#$TS#g" "$MONCP" > "$W/moncp.sh"; bash "$W/moncp.sh"; echo "rc=$?"
echo "### $(basename "$MONVER") (on the monitor; here a --rm container)"
docker run --rm -v "$W/mon:/mon" $IMG bash -c "$(sed "s#<用户目录>#/mon#; s#<机器>#$MACH#; s#<时间戳>#$TS#" "$MONVER")"
echo "### phase 2 in the container"
docker exec -i $C bash < "$W/p2.sh"
echo "### phase 3 in the container"
docker exec -i $C bash < "$W/p3.sh"
echo "### phase 4 in the container"
docker exec -i $C bash < "$W/p4.sh"
echo "### $(basename "$RSCP") (on the monitor; here ssh mkdir and scp become mkdir and cp into \$W/host)"
sed "s#ssh <SSH 账号>@<机器地址> mkdir -p /tmp/#mkdir -p $W/host/#; s#^\(.*\)&& scp #\1\&\& cp #; s#<SSH 账号>@<机器地址>:/tmp/#$W/host/#; s#<用户目录>#$W/mon#; s#<机器>#$MACH#g; s#<时间戳>#$TS#g" "$RSCP" > "$W/rscp.sh"; bash "$W/rscp.sh"; echo "rc=$?"; ls "$W/host/$MACH-$TS"
echo "### $(basename "$RDCP") (on the machine; here the Mac)"
sed "s#/tmp/<机器>-<时间戳>/\.#$W/host/$MACH-$TS/.#; s#<容器名>#$C#; s#<机器>#$MACH#g; s#<时间戳>#$TS#g" "$RDCP" > "$W/rdcp.sh"; bash "$W/rdcp.sh"; echo "docker cp rc=$?"
echo "### phase 5 in the container"
sed "s#TS_HERE#$TS#g" "$W/p5.sh" | docker exec -i $C bash
echo "### after the walk: the site-packages trees against the public sources, regular files only, __pycache__ not counted"
docker exec $C sh -c 'cd /usr/local/lib/python3.11/site-packages && find vllm vllm_ascend -name __pycache__ -prune -o -type f -print | LC_ALL=C sort | xargs sha256sum' > "$W/after.sums"
(cd "$W/site" && find vllm vllm_ascend -name __pycache__ -prune -o -type f -print | LC_ALL=C sort | xargs shasum -a 256) > "$W/before.sums"
if diff -q "$W/before.sums" "$W/after.sums" >/dev/null; then echo "identical: $(wc -l < "$W/before.sums") files"; else diff "$W/before.sums" "$W/after.sums" | head -20; fi
echo "### the /tmp is tmpfs case of step 3 (a separate --rm container started with --tmpfs /tmp)"
docker run --rm --tmpfs /tmp $IMG bash -c "$(cat "$TMPFS"); $(head -1 "$BACKUP"); echo \"BKBASE=\$BKBASE\""
docker rm -fv $C >/dev/null 2>&1; echo "### container removed: $(docker ps -a --format '{{.Names}}' | grep -c "^$C$")"
