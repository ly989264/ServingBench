#!/bin/bash
# Moving an installed cluster folder of the earlier panel two (deploy.env, puller, Vector) to this version.
#   bin/migrate_deploy_env.sh split <集群目录> [--fetcher-url http://<proxy 机器>:<端口>]
#       migration step 0: writes clickhouse.env, grafana.env and ingest.json from the folder's deploy.env, with the names,
#       ports and UIDs the cluster uses now; deploy.env is kept as deploy.env.before-t62. The scripts of the folder are
#       replaced by this pack's. Nothing is started or stopped: the earlier puller and Vector keep running.
#   bin/migrate_deploy_env.sh retire-old <集群目录>
#       migration step 5: stops the earlier puller (and its rsync copies) and removes the Vector container and the earlier
#       alert rules file. ClickHouse, its old tables, the board and the data source stay: bin/panel2_cluster.sh add then
#       replaces the board, the data source and the Prometheus block (same UIDs and markers).
#   bin/migrate_deploy_env.sh drop-old-tables <集群目录> --yes
#       migration step 8: drops the 9 tables and views of the earlier version with their data and removes the earlier
#       Vector container's data volume (cannot be undone).
. "$(dirname "$0")/lib.sh"
need_python
cmd=${1:-}; dir=${2:-}
[ -n "$cmd" ] && [ -n "$dir" ] || { sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
shift 2
D=$(cd "$dir" 2>/dev/null && pwd) || fail "没有这个目录：$dir"
case $cmd in
  split)
    url=""
    while [ $# -gt 0 ]; do
      case $1 in --fetcher-url) url=${2:-}; shift 2 ;; *) fail "split 只认 --fetcher-url" ;; esac
    done
    [ -f "$D/deploy.env" ] || fail "$D 里没有 deploy.env"
    if [ "$D" != "$PACK_DIR" ]; then
      # Backed up: only what the lines below remove or overwrite (the removed scripts and files, and every top-level
      # entry of this pack that the folder has too); the cluster's data, logs and other files stay out of the tar.
      rm -f "$D/pack-before-t62.tar.gz.new"     # left by an earlier run that stopped here
      items=()
      for x in bin scripts mock prometheus grafana clickhouse ingest inventory.json deploy.env.example inventory.example.json \
               $(cd "$PACK_DIR" && ls -A); do
        case $x in clickhouse.env|grafana.env|ingest.json|generated|offline|run|logs|deploy.env*|pack-before-t62.tar.gz*) continue ;; esac
        { [ -e "$D/$x" ] || [ -L "$D/$x" ]; } || continue
        case " ${items[*]-} " in *" $x "*) continue ;; esac
        items+=("$x")
      done
      [ ${#items[@]} -gt 0 ] || fail "$D 里没有要备份的脚本，什么都没有改"
      if ! terr=$(tar -czf "$D/pack-before-t62.tar.gz.new" -C "$D" -- "${items[@]}" 2>&1); then
        printf '%s\n' "$terr" | tail -n 3 | sed 's/^/tar 输出：/' >&2
        rm -f "$D/pack-before-t62.tar.gz.new"
        fail "备份不了 $D 原来的脚本，什么都没有改"
      fi
      mv "$D/pack-before-t62.tar.gz.new" "$D/pack-before-t62.tar.gz" || { rm -f "$D/pack-before-t62.tar.gz.new"; fail "备份不了 $D 原来的脚本（改名失败），什么都没有改"; }
    fi
    "$PYTHON" -I "$PACK_DIR/scripts/cluster_files.py" from-deploy-env --dir "$D" ${url:+--fetcher-url "$url"} || exit 1
    if [ "$D" != "$PACK_DIR" ]; then
      # The earlier scripts go (kept in pack-before-t62.tar.gz); this pack's take their place.
      for x in bin scripts mock prometheus grafana clickhouse ingest; do [ -e "$D/$x" ] && rm -rf "${D:?}/$x"; done
      rm -f "$D/inventory.json" "$D/deploy.env.example" "$D/inventory.example.json"
      ( cd "$PACK_DIR" && tar -cf - --exclude=./clickhouse.env --exclude=./grafana.env --exclude=./ingest.json --exclude=./generated \
          --exclude=./offline --exclude=./run --exclude=./logs --exclude='./deploy.env*' --exclude='*/__pycache__' . ) | ( cd "$D" && tar -xf - ) \
        || fail "拷不了部署包到 $D"
    fi
    echo "原来的脚本保留在 $D/pack-before-t62.tar.gz，原 deploy.env 保留为 deploy.env.before-t62"
    echo "下一步（集群仍在运行）：cd $D && bin/clickhouse_up.sh（在同一个 ClickHouse 里建新表，旧表和旧面板二照常运行）"
    echo "RESULT: OK"
    ;;
  retire-old)
    old=$D/deploy.env.before-t62
    [ -f "$old" ] || fail "$D 里没有 deploy.env.before-t62：先执行 split"
    vals=$("$PYTHON" -I "$PACK_DIR/scripts/names.py" derive --panel panel2 --env-file "$old" --format json) || exit 1
    get() { "$PYTHON" -I -c 'import json,sys; print(json.loads(sys.argv[1]).get(sys.argv[2]) or "")' "$vals" "$1"; }
    th=$(get TIMING_HOME); central=$(get CENTRAL_CONTAINER); pull=$(get PULL_DIR); rules=$(get RULES_FILE)
    [ -f "$D/clickhouse.env" ] || fail "$D 里没有 clickhouse.env：先执行 split"
    CH_ENV_FILE=$D/clickhouse.env load_ch_env
    pids=()
    for f in "$th/puller/loop.pid" "$th/puller/puller.pid"; do p=$(running_pid "$f") && pids+=("$p"); done
    if [ ${#pids[@]} -gt 0 ]; then
      kill "${pids[@]}" 2>/dev/null; sleep 3
      kill -9 "${pids[@]}" 2>/dev/null
      echo "已停止原拉取程序（进程 ${pids[*]}）"
    else
      echo "原拉取程序没有在运行"
    fi
    [ -n "$pull" ] && pkill -9 -f "rsync .*$pull/" 2>/dev/null && echo "已停止原拉取程序留下的 rsync"
    rm -f "$th/puller/loop.pid" "$th/puller/puller.pid"
    if docker ps -a --format '{{.Names}}' | grep -qx "$central"; then
      docker stop -t 60 "$central" >/dev/null 2>&1; docker rm -fv "$central" >/dev/null && echo "已删除原采集器容器 $central"
    else
      echo "原采集器容器 $central 不存在"
    fi
    [ -n "$rules" ] && docker exec -u root "$PROMETHEUS_CONTAINER" rm -f "/prometheus/$rules" 2>/dev/null && echo "已删除原告警规则文件 /prometheus/$rules"
    echo "ClickHouse、旧表、看板和数据源保留。下一步：bin/panel2_cluster.sh add <部署名> --fetcher-url http://<proxy 机器>:9300"
    echo "原镜像目录 $pull 和原拉取程序的状态目录 $th/puller 没有删除，迁移完成后可手工删除"
    echo "原采集器的数据卷 $central-data 保留（第 8 步前回退时还要用），第 8 步 drop-old-tables 时删除"
    echo "RESULT: OK"
    ;;
  drop-old-tables)
    [ "${1:-}" = --yes ] || fail "会删除旧版的 9 张表和视图及其中的数据，不能恢复。确认后加 --yes"
    "$PYTHON" -I "$PACK_DIR/ingest/pd_ingest.py" drop-old --dir "$D" || exit 1
    old=$D/deploy.env.before-t62
    if [ -f "$old" ]; then
      central=$("$PYTHON" -I "$PACK_DIR/scripts/names.py" derive --panel panel2 --env-file "$old" --format json \
        | "$PYTHON" -I -c 'import json,sys; print(json.load(sys.stdin).get("CENTRAL_CONTAINER") or "")')
      if [ -n "$central" ] && docker volume inspect "$central-data" >/dev/null 2>&1; then
        docker volume rm "$central-data" >/dev/null && echo "已删除原采集器的数据卷 $central-data"
      fi
    fi
    ;;
  *) fail "用法：bin/migrate_deploy_env.sh split|retire-old|drop-old-tables <集群目录>" ;;
esac
