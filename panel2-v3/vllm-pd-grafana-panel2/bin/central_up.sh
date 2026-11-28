#!/bin/bash
# Starts the collector on the monitoring machine, or restarts it with a config generated from segments.json. Its buffers and position are kept.
# Run it again after changing segments.json (bin/apply_segments.sh does that).
# When config, image and run arguments are what the running collector already has (bin/compare.sh central), it is left alone,
# or only started if it was stopped. VLLM_PD_FORCE=1 rebuilds it anyway.
. "$(dirname "$0")/lib.sh"
load_env
need_python
need_inventory
names_check
docker image inspect "$CENTRAL_IMAGE" >/dev/null 2>&1 || fail "没有镜像 $CENTRAL_IMAGE。先执行 docker load -i offline/vector-image.tar。"
updated=""
if [ "${VLLM_PD_FORCE:-}" = 1 ]; then
  echo "VLLM_PD_FORCE=1：不比对，照做：采集器"
else
  line=$("$PACK_DIR/bin/compare.sh" central); rc=$?
  [ "$rc" -eq 0 ] || [ "$rc" -eq 10 ] || fail "比对采集器失败，看上面的提示"
  if [ "$rc" -eq 0 ]; then
    if [ "$(docker inspect --format '{{.State.Running}}' "$CENTRAL_CONTAINER" 2>/dev/null)" = true ]; then
      echo "跳过（内容不变）：采集器"
    else
      [ -z "${CLICKHOUSE_DATA_DIR:-}" ] || mkdir -p "$CLICKHOUSE_DATA_DIR/vector" || fail "建不了 $CLICKHOUSE_DATA_DIR/vector"
      make_pull_dir
      docker start "$CENTRAL_CONTAINER" >/dev/null || fail "采集器容器启动失败，看 docker logs $CENTRAL_CONTAINER"
      sleep 3
      [ "$(docker inspect --format '{{.State.Running}}' "$CENTRAL_CONTAINER" 2>/dev/null)" = true ] || { docker logs --tail 15 "$CENTRAL_CONTAINER" >&2; fail "采集器没有起来，看上面的日志。"; }
      echo "跳过（内容不变）：采集器；容器原来没在运行，已启动（没有重建）"
    fi
    echo "采集器在运行：容器 $CENTRAL_CONTAINER，读 $PULL_DIR"
    echo "RESULT: OK"
    exit 0
  fi
  [ "${line%% *}" = WARN ] && echo "$line"
  what=${line#*：}; updated="已更新：采集器：${what#有变化：}"
fi
CONF="$PACK_DIR/generated/central"
mkdir -p "$CONF" || fail "建不了目录 $CONF"
"$PYTHON" "$PACK_DIR/scripts/inventory_tool.py" "$INVENTORY" csv > "$CONF/inventory.csv" || exit 1
MODEL_NAME=$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("model", "unknown"))' "$INVENTORY") \
  "$PYTHON" "$PACK_DIR/scripts/render_vector.py" "$CONF/central.yaml" /var/lib/vector /etc/vector || exit 1
chmod 644 "$CONF/inventory.csv"
chmod 600 "$CONF/central.yaml"     # it holds the ClickHouse writer password
out=$(docker run --rm -v "$CONF:/etc/vector:ro" "$CENTRAL_IMAGE" validate --no-environment /etc/vector/central.yaml 2>&1) || { echo "$out" >&2; fail "采集器配置有错，看上面的输出。容器没有动。"; }
if [ -n "${CLICKHOUSE_DATA_DIR:-}" ]; then
  mkdir -p "$CLICKHOUSE_DATA_DIR/vector" || fail "建不了 $CLICKHOUSE_DATA_DIR/vector"
  data="$CLICKHOUSE_DATA_DIR/vector"
else
  data="$CENTRAL_CONTAINER-data"
fi
# The command (scripts/render_vector.py RUN_SCRIPT) also removes dead-letter files older than 7 days, once at start and then hourly.
# The label keeps a sha of CENTRAL_DOCKER_ARGS, which docker inspect cannot show, for bin/compare.sh central.
run_script=$("$PYTHON" "$PACK_DIR/scripts/render_vector.py" --run-script) || fail "拿不到采集器的启动命令"
args_sha=$("$PYTHON" -c 'import hashlib, sys; print(hashlib.sha256(sys.argv[1].encode("utf-8")).hexdigest()[:16])' "${CENTRAL_DOCKER_ARGS:-}")
make_pull_dir
docker network inspect "$DOCKER_NETWORK" >/dev/null 2>&1 || docker network create "$DOCKER_NETWORK" >/dev/null || fail "建不了网络 $DOCKER_NETWORK"
# Stop it first and give it time to finish: killed outright, the collector sends rows from its disk buffer again after
# the restart, and the event table gets them twice.
docker stop -t 60 "$CENTRAL_CONTAINER" >/dev/null 2>&1
docker rm -f "$CENTRAL_CONTAINER" >/dev/null 2>&1
# shellcheck disable=SC2086
docker run -d --name "$CENTRAL_CONTAINER" --network "$DOCKER_NETWORK" --restart unless-stopped --ulimit nofile=65536:65536 ${CENTRAL_DOCKER_ARGS:-} \
  --label "vllm-pd.docker-args=$args_sha" \
  -v "$CONF:/etc/vector:ro" -v "$data:/var/lib/vector" -v "$PULL_DIR:/pull:ro" --entrypoint sh "$CENTRAL_IMAGE" \
  -c "$run_script" >/dev/null \
  || fail "采集器容器启动失败。"
sleep 3
[ "$(docker inspect --format '{{.State.Running}}' "$CENTRAL_CONTAINER" 2>/dev/null)" = true ] || { docker logs --tail 15 "$CENTRAL_CONTAINER" >&2; fail "采集器没有起来，看上面的日志。"; }
echo "采集器已启动：容器 $CENTRAL_CONTAINER，读 $PULL_DIR"
[ -z "$updated" ] || echo "$updated"
echo "RESULT: OK"
