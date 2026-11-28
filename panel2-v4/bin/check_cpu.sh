#!/bin/bash
# Preflight on the monitoring machine. Prints one line per item, OK or FAIL with what to do, and a summary. Starts and changes nothing.
# Whether the official ClickHouse build can run is decided from this machine's own CPU features, never from a model name.
# Reads clickhouse.env (the ClickHouse side) and grafana.env (Grafana and the board's data source).
. "$(dirname "$0")/lib.sh"
load_ch_env
load_gf_env
need_python
arch=$(machine_arch)
pass "架构 $arch"

# What the official builds are compiled for (cmake/cpu_features.cmake in ClickHouse 26.8):
#   aarch64: -march=armv8.2-a+simd+crypto+dotprod+ssbs+rcpc+bf16     x86_64: x86-64-v3 and pclmul
# ssbs and bf16 are not checked: compilers do not emit them on their own, ClickHouse's own install script ignores them, and a kernel
# in a VM may not list ssbs on a CPU where the official build runs fine. The trial run below is the final word.
case $arch in
  aarch64) want="asimd aes sha2 atomics lrcpc asimddp"; line_re='^features'; compat_file=clickhouse-aarch64v80compat ;;
  x86_64)  want="ssse3 sse4_2 popcnt pclmulqdq avx2 bmi1 bmi2 fma"; line_re='^flags'; compat_file=clickhouse-amd64compat ;;
esac
if [ -r /proc/cpuinfo ]; then
  features=$(grep -m1 -iE "$line_re" /proc/cpuinfo)
else
  features=$(docker run --rm --entrypoint sh "$CLICKHOUSE_IMAGE" -c "grep -m1 -iE '$line_re' /proc/cpuinfo" 2>/dev/null)
fi
missing=""
for f in $want; do
  case " $features " in *" $f "*) ;; *) missing="$missing $f" ;; esac
done
compat=${CLICKHOUSE_COMPAT_BINARY:-}
if [ -z "$missing" ]; then
  pass "CPU 特性：官方版要求的 $(echo $want | wc -w | tr -d ' ') 项都有"
elif [ -n "$compat" ]; then
  pass "CPU 特性：缺${missing}，已配置兼容版"
else
  miss "CPU 特性：缺$missing → clickhouse.env 写 CLICKHOUSE_COMPAT_BINARY=$PACK_DIR/offline/${compat_file}，重跑这条命令。"
fi
check_page_size
if docker_version=$(docker version --format '{{.Server.Version}}' 2>/dev/null); then
  pass "docker $docker_version"
else
  miss "docker 连不上 → 确认 docker 在运行，当前用户有权限（root 或 docker 组）。"
  check_summary
  exit 1
fi
check_python
# ---- clickhouse.env ----
bad_pw=""
for k in CH_ADMIN_PASSWORD CH_WRITER_PASSWORD CH_READER_PASSWORD; do
  case ${!k} in change-me-*|*[!A-Za-z0-9]*|"") bad_pw="$bad_pw $k" ;; esac
done
if [ -n "$bad_pw" ]; then miss "密码:$bad_pw 还没改或含特殊字符 → 只用字母和数字。"; else pass "clickhouse.env 密码已设置"; fi
case $MONITOR_HOST in
  localhost|127.*|0.0.0.0|""|10.0.3.2) miss "MONITOR_HOST=$MONITOR_HOST → clickhouse.env 写监控机的 IP，Prometheus 用它访问入库进程的状态端口。" ;;
  *) pass "MONITOR_HOST $MONITOR_HOST" ;;
esac

# ---- ClickHouse image and trial run ----
if ! image_arch=$(docker image inspect --format '{{.Architecture}}' "$CLICKHOUSE_IMAGE" 2>/dev/null); then
  miss "镜像 $CLICKHOUSE_IMAGE 不在 → docker load -i offline/clickhouse-image.tar"
else
  case $arch:$image_arch in
    aarch64:arm64|x86_64:amd64) pass "镜像 ${CLICKHOUSE_IMAGE}（${image_arch}）" ;;
    *) miss "镜像是 ${image_arch}，机器是 $arch → 离线包拿错了，要 $arch 那一份。" ;;
  esac
  mount=()
  kind=官方版
  if [ -n "$compat" ]; then
    kind=兼容版
    if [ ! -f "$compat" ]; then
      miss "CLICKHOUSE_COMPAT_BINARY 指向的文件不存在：$compat"
      compat=""
    else
      chmod a+rx "$compat" 2>/dev/null
      # The copy from bin/fetch_offline.sh unpacks itself on first run and needs a writable folder. The one in the offline bundle is already unpacked.
      if [ "$(wc -c < "$compat")" -lt 500000000 ]; then
        dir=$(cd "$(dirname "$compat")" && pwd); name=$(basename "$compat")
        docker run --rm -v "$dir:/compat" --entrypoint "/compat/$name" "$CLICKHOUSE_IMAGE" local -q 'SELECT 1' >/dev/null 2>&1
      fi
      mount=(-v "$compat:/usr/bin/clickhouse:ro")
    fi
  fi
  # Through sh and with a second command, so that clickhouse is not process 1 in the container: a crash must end the run and give its exit code (132 = illegal instruction).
  version=$(docker run --rm "${mount[@]+"${mount[@]}"}" --entrypoint sh "$CLICKHOUSE_IMAGE" -c "clickhouse local -q 'SELECT version()'; exit \$?" 2>&1)
  code=$?
  if [ $code -eq 0 ]; then
    if [ -n "$missing" ] && [ -z "$compat" ]; then
      miss "ClickHouse ${kind}试运行通过 ${version}，但 CPU 缺特性，真正查询时可能崩 → 用兼容版。"
    else
      pass "ClickHouse ${kind}试运行 $version"
    fi
  elif [ -z "$compat" ]; then
    miss "ClickHouse ${kind}试运行失败（退出码 ${code}）→ clickhouse.env 写 CLICKHOUSE_COMPAT_BINARY=$PACK_DIR/offline/${compat_file}。"
  else
    miss "ClickHouse ${kind}试运行失败（退出码 ${code}）：$(echo "$version" | tail -n 1 | cut -c1-80) → 把 ClickHouse 放到别的机器（README 第 10 节）。"
  fi
fi

# ---- disk, memory, port ----
min_gb=${CLICKHOUSE_MIN_DISK_GB:-250}
if [ -n "${CLICKHOUSE_DATA_DIR:-}" ]; then
  where=$CLICKHOUSE_DATA_DIR
  if docker image inspect "$CLICKHOUSE_IMAGE" >/dev/null 2>&1 && reason=$(data_dir_check); then :; else miss "数据盘：${reason:-先导入镜像才能检查}"; where=""; fi
else
  where=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)
fi
if [ -n "$where" ]; then
  free=$(free_gb "$where")
  if [ "${free:-0}" -ge "$min_gb" ]; then pass "数据盘 $where 剩余 $free GB（建议 $min_gb GB）"
  else miss "数据盘 $where 剩余 ${free:-?} GB，建议 $min_gb GB → clickhouse.env 的 CLICKHOUSE_DATA_DIR 写大盘上的目录。"; fi
fi
if [ -r /proc/meminfo ]; then
  mem=$(awk '/^MemTotal/ { printf "%d", $2 / 1048576 }' /proc/meminfo)
  if [ "$mem" -ge 6 ]; then pass "内存 $mem GB"; else miss "内存 $mem GB，太少（测试环境 7 GB 能跑）→ 把 ClickHouse 放到别的机器（README 第 10 节）。"; fi
fi
port_free() { "$PYTHON" -c 'import socket,sys; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("0.0.0.0", int(sys.argv[1])))' "$1" 2>/dev/null; }
if docker ps -a --format '{{.Names}}' | grep -qx "$CLICKHOUSE_CONTAINER"; then
  pass "端口 ${CLICKHOUSE_HTTP_PORT}（本包的 ClickHouse 容器已存在）"
elif port_free "$CLICKHOUSE_HTTP_PORT"; then
  pass "端口 $CLICKHOUSE_HTTP_PORT 空闲"
else
  miss "端口 $CLICKHOUSE_HTTP_PORT 被占用 → 改 clickhouse.env 的 CLICKHOUSE_HTTP_PORT。"
fi
if running_pid "$PACK_DIR/run/ingest-loop.pid" >/dev/null; then
  pass "端口 ${INGEST_STATUS_PORT}（本集群的入库进程在用）"
elif port_free "$INGEST_STATUS_PORT"; then
  pass "端口 $INGEST_STATUS_PORT 空闲"
else
  miss "端口 $INGEST_STATUS_PORT 被占用 → 改 clickhouse.env 的 INGEST_STATUS_PORT。"
fi

# ---- panel 1 ----
running=$(docker ps --format '{{.Names}} {{.Image}}')
if echo "$running" | grep -q "^$GRAFANA_CONTAINER "; then pass "Grafana 容器 $GRAFANA_CONTAINER"
else miss "Grafana 容器 $GRAFANA_CONTAINER 没在运行 → grafana.env 的 GRAFANA_CONTAINER 写：$(echo "$running" | awk '/grafana/ { print $1 }' | tr '\n' ' ')"; fi
while read -r status rest; do
  if [ "$status" = OK ]; then pass "$rest"; elif [ "$status" = INFO ]; then echo "INFO $rest"; else miss "$rest"; fi
done < <("$PYTHON" -I "$PACK_DIR/scripts/grafana_api.py" preflight 2>&1)
if echo "$running" | grep -q "^$PROMETHEUS_CONTAINER "; then
  pass "Prometheus 容器 $PROMETHEUS_CONTAINER"
  if ! docker network inspect --format '{{range .Containers}}{{.Name}} {{end}}' "$DOCKER_NETWORK" 2>/dev/null | grep -qw "$GRAFANA_CONTAINER"; then
    miss "docker 网络 $DOCKER_NETWORK 里没有 Grafana → clickhouse.env 的 DOCKER_NETWORK 写：$(docker inspect --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$GRAFANA_CONTAINER" 2>/dev/null)"
  else
    pass "docker 网络 $DOCKER_NETWORK"
  fi
  source=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/etc/prometheus/prometheus.yml"}}{{.Source}}{{end}}{{end}}' "$PROMETHEUS_CONTAINER" 2>/dev/null)
  if [ ! -f "$PROMETHEUS_YML" ]; then
    miss "prometheus.yml 不在 $PROMETHEUS_YML → clickhouse.env 的 PROMETHEUS_YML 写：${source:-面板一解包目录下的 prometheus/prometheus.yml}"
  elif [ -n "$source" ] && [ "$source" != "$PROMETHEUS_YML" ] && [ "$(cd "$(dirname "$PROMETHEUS_YML")" && pwd -P)/$(basename "$PROMETHEUS_YML")" != "$source" ]; then
    miss "prometheus.yml 不是容器在用的那份 → clickhouse.env 的 PROMETHEUS_YML 写：$source"
  else
    pass "prometheus.yml $PROMETHEUS_YML"
  fi
  while read -r status rest; do
    if [ "$status" = OK ]; then pass "$rest"; else miss "$rest"; fi
  done < <("$PYTHON" - "$PROMETHEUS_URL" "$PROMETHEUS_CONTAINER" <<'PY'
import json, sys, urllib.request as u
url, name = sys.argv[1:3]
try:
    flags = json.load(u.urlopen(url + "/api/v1/status/flags", timeout=5))["data"]
except Exception as e:
    print("FAIL Prometheus %s 访问不到 → 检查 clickhouse.env 的 PROMETHEUS_URL。" % url)
    sys.exit(0)
if flags.get("web.enable-lifecycle") == "true":
    print("OK Prometheus %s 支持热加载" % url)
else:
    print("FAIL Prometheus 没带 --web.enable-lifecycle → 加规则后要 docker restart %s，采集中断几秒。" % name)
PY
)
else
  miss "Prometheus 容器 $PROMETHEUS_CONTAINER 没在运行 → clickhouse.env 的 PROMETHEUS_CONTAINER 写：$(echo "$running" | awk '/prometheus/ { print $1 }' | tr '\n' ' ')"
fi
check_summary
