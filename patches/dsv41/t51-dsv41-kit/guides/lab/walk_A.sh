#!/bin/bash
# Walk guide A steps 1, 3, 4 and 9 in a throwaway container, using the guide's own command blocks.
# Usage: walk_A.sh SCRATCH WORK KIT_TAR
#   WORK must hold vllm-0320 and asc-f9c (public trees); they are copied, never modified.
set -u
SCRATCH=$1; WORK=$2; TAR=$3; OBS_WHEEL_DIR=${4:-}
HERE=$(cd "$(dirname "$0")" && pwd)
KIT=$(cd "$HERE/../.." && pwd)
GUIDE="$KIT/guides/操作说明A_容器内打补丁.zh-CN.md"
W="$SCRATCH/walkA"; rm -rf "$W"; mkdir -p "$W/ws"
cp -R "$WORK/vllm-0320" "$W/ws/vllm"; cp -R "$WORK/asc-f9c" "$W/ws/vllm-ascend"
python3 "$KIT/detect/lab/make_site.py" "$W/site" >/dev/null

# code blocks of the guide, in order
python3 - "$GUIDE" "$W/blocks" <<'PY'
import re, sys, os
text = open(sys.argv[1], encoding="utf-8").read()
blocks = re.findall(r"```bash\n(.*?)```", text, re.S)
os.makedirs(sys.argv[2], exist_ok=True)
for i, b in enumerate(blocks):
    open(os.path.join(sys.argv[2], "%02d.sh" % i), "w").write(b)
print(len(blocks), "blocks")
PY
ls "$W/blocks"
# pick blocks by content, not by position: host copy, unpack, dirs, verify, backup, vLLM, ascend, stderr query, rollback
pick() { grep -l -- "$1" "$W"/blocks/*.sh | head -1; }
HOST=$(pick 'docker cp'); UNPACK=$(pick 'tar -xf /tmp/t51-dsv41-kit.tar'); DIRS=$(pick 'VLLM_PKG=$(python3'); VERIFY=$(pick 'vllm/SHA256SUMS.before | grep'); BACKUP=$(pick 'BK=/tmp/t51-backup'); V=$(pick 'vllm/files/vllm/.'); A=$(pick 'vllm-ascend/files/vllm_ascend/.'); ERR=$(pick "fd/2"); RB=$(pick 'BK=<')
MCCHECK=$(pick 'M=/tmp/t51-kit/patches/mooncake/lib'); MCDF=$(pick 'df -h /tmp'); MCBACK=$(pick 'mooncake.tar" $(awk'); MCREP=$(pick 't51new'); MCRB=$(pick 'mktemp -d -p')
{
  echo 'set -u'
  for b in "$UNPACK" "$DIRS" "$VERIFY" "$BACKUP" "$V" "$A"; do echo "echo '### $(basename "$b")'"; cat "$b"; done
  if [ -n "$OBS_WHEEL_DIR" ]; then
    echo "echo '### detect before Mooncake'"
    echo 'python3 /tmp/t51-kit/detect/t51_dsv41_check.py --out-dir /tmp | grep -E "^mooncake="'
    for b in "$MCCHECK" "$MCDF" "$MCBACK" "$MCREP"; do echo "echo '### $(basename "$b") (5.1)'"; cat "$b"; done
    echo "echo '### backup size and checksum (5.1)'"
    echo 'ls -l "$BK/mooncake.tar" | awk "{print \$5}"; sha256sum "$BK/mooncake.tar" | cut -c1-16'
    echo 'ls -l "$BK"/vllm.tar "$BK"/vllm-ascend.tar | awk "{print \$5, \$NF}"'
  fi
  echo "echo '### detect after replace'"
  echo 'python3 /tmp/t51-kit/detect/t51_dsv41_check.py --out-dir /tmp | grep -E "^(vllm|ascend|mooncake)="'
  echo "echo '### $(basename "$RB") (rollback)'"
  sed 's#^BK=<第 3 步记下的备份目录>; ##' "$RB"
  if [ -n "$OBS_WHEEL_DIR" ]; then echo "echo '### $(basename "$MCRB") (Mooncake rollback)'"; cat "$MCRB"; fi
  echo "echo '### detect after rollback'"
  echo 'python3 /tmp/t51-kit/detect/t51_dsv41_check.py --out-dir /tmp | grep -E "^(vllm|ascend|mooncake)="'
} > "$W/walk.sh"
cp "$ERR" "$W/stderr_query.sh"
# this walk is the path without proxy (section 0): none of the executed blocks may touch the proxy switch
echo "### path: without proxy; executed blocks that set PD_PROXY_DIAG: $(grep -c 'PD_PROXY_DIAG' "$W/walk.sh")"
C=t51lab-walka
docker rm -fv $C >/dev/null 2>&1
if [ -n "$OBS_WHEEL_DIR" ]; then
  # writable site-packages: the fake vLLM metadata plus the real OBS Mooncake wheel
  docker run --rm -d --name $C -v "$W/ws:/vllm-workspace" -v "$W/site:/siteseed:ro" -v "$OBS_WHEEL_DIR:/w:ro" python:3.12-slim sleep 900 >/dev/null
  docker exec $C sh -c 'cp -r /siteseed/. /usr/local/lib/python3.12/site-packages/ && pip install -q --no-deps --no-index /w/*.whl 2>&1 | grep -v "Running pip as" | tail -1'
else
  docker run --rm -d --name $C -v "$W/ws:/vllm-workspace" -v "$W/site:/usr/local/lib/python3.12/site-packages:ro" python:3.12-slim sleep 600 >/dev/null
fi
sed "s#<容器名>#$C#; s#t51-dsv41-kit.tar#$TAR#" "$HOST" > "$W/host.sh"
bash "$W/host.sh" && docker exec $C bash -c "mv /tmp/$(basename "$TAR") /tmp/t51-dsv41-kit.tar 2>/dev/null; true"
docker cp "$W/walk.sh" $C:/tmp/walk.sh >/dev/null
docker exec $C bash /tmp/walk.sh 2>&1
docker rm -fv $C >/dev/null 2>&1
# the stderr query of step 6, against fake vLLM workers whose stderr goes to a log file
C2=t51lab-walka-err
docker rm -fv $C2 >/dev/null 2>&1
docker run --rm -d --name $C2 --tmpfs /logs -v "$KIT/detect/lab/bin:/opt/t51bin:ro" -v "$KIT/detect/lab/launch_p:/launch:ro" python:3.12-slim bash /launch/start_p.sh >/dev/null
sleep 4
docker cp "$W/stderr_query.sh" $C2:/tmp/q.sh >/dev/null
echo "### stderr query (step 6)"
docker exec $C2 bash /tmp/q.sh 2>&1
docker rm -fv $C2 >/dev/null 2>&1
# 5.1's host round trip: copy mooncake.tar to the host, check it, delete the container copy, copy it back
# before the Mooncake rollback (step 9).
if [ -n "$OBS_WHEEL_DIR" ]; then
  C3=t51lab-walka-host
  docker rm -fv $C3 >/dev/null 2>&1
  docker run --rm -d --name $C3 -v "$W/site:/siteseed:ro" -v "$OBS_WHEEL_DIR:/w:ro" python:3.12-slim sleep 900 >/dev/null
  docker exec $C3 sh -c 'cp -r /siteseed/. /usr/local/lib/python3.12/site-packages/ && pip install -q --no-deps --no-index /w/*.whl >/dev/null 2>&1'
  docker cp "$TAR" $C3:/tmp/t51-dsv41-kit.tar >/dev/null
  BKC=/tmp/t51-backup-hosttest
  { echo 'set -u'; cat "$UNPACK"; echo "BK=$BKC; mkdir -p \$BK"; cat "$MCCHECK" "$MCDF" "$MCBACK" "$MCREP"; echo 'sha256sum "$BK/mooncake.tar" | cut -c1-64'; } > "$W/p1.sh"
  { echo 'set -u'; echo "BK=$BKC"; head -2 "$MCCHECK"; cat "$MCRB"; } > "$W/p2.sh"
  docker cp "$W/p1.sh" $C3:/tmp/p1.sh >/dev/null; docker cp "$W/p2.sh" $C3:/tmp/p2.sh >/dev/null
  echo "### host round trip: container side (5.1)"
  CSUM=$(docker exec $C3 bash /tmp/p1.sh 2>&1 | tail -1)
  echo "container sha256 ${CSUM:0:16}"
  rm -rf "$W/hostcopy"; mkdir -p "$W/hostcopy"
  docker cp $C3:$BKC/mooncake.tar "$W/hostcopy/" >/dev/null
  HSUM=$(shasum -a 256 "$W/hostcopy/mooncake.tar" | cut -c1-64)
  echo "host sha256      ${HSUM:0:16} $( [ "$HSUM" = "$CSUM" ] && echo same || echo DIFFERENT)"
  docker exec $C3 rm "$BKC/mooncake.tar"
  echo "container copy deleted: $(docker exec $C3 sh -c "ls $BKC/mooncake.tar 2>&1 | grep -c 'No such'")"
  echo "### host round trip: copy back, then step 9 Mooncake rollback"
  docker cp "$W/hostcopy/mooncake.tar" $C3:$BKC/ >/dev/null
  docker exec $C3 bash /tmp/p2.sh 2>&1
  echo "after rollback: $(docker exec $C3 python3 /tmp/t51-kit/detect/t51_dsv41_check.py --out-dir /tmp | grep '^mooncake=' | cut -d' ' -f2-3)"
  docker rm -fv $C3 >/dev/null 2>&1
  rm -rf "$W/hostcopy"
fi
