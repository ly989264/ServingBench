# glm5.3 测试集群 proxy 打补丁操作手册

## 第一页

**这份手册做什么。** 给 glm5.3 测试集群的 proxy（vllm-ascend 的 `load_balance_proxy_server_example.py`）换上打了补丁的文件，只重启 proxy。打开以后 proxy 每个请求写一行记录，写进 proxy 所在机器的节点日志目录 `/data/pd-diag/logs/proxy`，由 test-glm53 的拉取程序收进 ClickHouse，面板二的两个 `P/D LB proxy` 段（`P lb_proxy`、`D lb_proxy`）就有值了。做完以后：

- proxy 的参数、环境、工作目录、输出文件和原来相同，只多了两个环境变量 `PD_PROXY_DIAG=1`、`PD_PROXY_DIAG_DIR=/data/pd-diag/logs/proxy`；
- `/data/pd-diag/logs/proxy/proxy-<端口>/` 里有 proxy 的记录文件；
- test-glm53 的 `vllm_pd.proxy_line` 有行，`bin/coverage.sh` 里 `P lb_proxy`、`D lb_proxy` 两段有值。

**总顺序：**

1. 面板手册（《面板二：删除 test、安装 test-glm53 操作手册》）第 1–15 步；
2. 打补丁手册：给 glm5.3 的 vLLM 打补丁、重启；
3. 面板手册第 16 步（看板归位）；
4. **本手册第 1–11 步**；
5. 面板手册第 17 步起。第 17 步只剩 Mooncake 的 3 行，proxy 的几行本手册第 4 步已经加过。

**怎么用。** 每一步写明在哪台机器上做、命令、应当看到什么、不对时怎么办。看到的和「应当看到」不一样时停下，不做下一步，把这一步标「带回：」的输出带回来（文末有汇总清单）。监控机上的命令用装面板的那个账号，以 `. ~/vllm-pd-test-glm53/vars.sh` 开头（面板手册第 1 步写的变量文件）；glm5.3 机器上的命令用 root。

**补丁包：** `t51-glm53-proxy-kit.tar.gz`，解开是一个目录 `t51-glm53-proxy-kit/`：

| 文件 | 说明 |
|---|---|
| `t51_proxy_check.py` | 只读核对脚本（第 3、6、8 步用） |
| `main/load_balance_proxy_server_example.py` | 打好补丁的文件，给 vllm-ascend main 那一版（原文件 sha16 `ffd9a54456dc6ad1`，1440 行） |
| `v0.30/load_balance_proxy_server_example.py` | 打好补丁的文件，给 v0.30 那一版（原文件 sha16 `85b8982948530758`，1396 行；vllm-ascend v0.30.0 和 d4ad49796 里就是这一版） |
| `main/base/`、`v0.30/base/` | 两版原文件，回滚用 |
| `main/proxy-pd-diag.patch`、`v0.30/proxy-pd-diag.patch` | 两版的补丁（`git apply` 可用） |
| `main/tests/`、`v0.30/tests/` | 本地测试（假的 P、D 服务），现场不用 |
| `stop_proxy.sh`、`start_proxy.sh` | 停 proxy；按备份下来的参数和环境起 proxy（第 5 步拷进备份目录） |
| `SHA256SUMS` | 以上各文件的 sha256 |

**要先准备好的现场值：**

| 值 | 怎么得到 |
|---|---|
| `<补丁包目录>` | proxy 所在机器上放补丁包的目录，自己定，例如 root 家目录下的 `t51-proxy` |
| `<容器>` | 第 1 步的输出 |
| `<proxy 机器地址>` | 第 1 步找到 proxy 的那台机器，写法和 `bin/puller.sh status` 里这台机器的写法相同（面板手册第 13 步写进清单的地址） |
| `<进程号>`、`<proxy 文件>`、`<端口>`、`<版本>` | 第 3 步的输出：`proc pid=`、`file path=`、`proc port=`、`file … version=` |
| `<模型名>` | 和面板手册相同：glm5.3 的 `vllm serve --served-model-name`；不知道时填 `glm-5.3` |

**对其他服务的影响：**

| 对象 | 影响 |
|---|---|
| proxy | 第 7 步停、起一次，前后约 15 秒。这段时间里发给 proxy 的请求失败，正在处理的请求断开 |
| proxy 的行为 | 打开以后请求号由 proxy 生成，见附录 A「请求号」；其余转发逻辑不变 |
| vLLM、Mooncake | 不动，不重启 |
| 节点日志目录 | 多一个子目录 `proxy/`，每个请求约 1 KB，按 12 小时保留，单个文件 256 MB 换新 |
| test-glm53 | `deploy.env` 末尾加 5 行，拉取程序重启一次（第 4 步）；其他集群不动 |

## 步骤

### 1. 找 proxy 在哪台机器、哪个容器（只读）

**在哪台机器：** glm5.3 的两台机器，各执行一次。

```bash
for c in $(docker ps -q); do docker top "$c" -eo pid,args 2>/dev/null | grep -q "[l]oad_balance_proxy_server_example" && docker inspect --format "proxy 在容器 {{.Name}}" "$c"; done; echo "本机查完：$(docker ps -q | wc -l) 个容器"
```

**应当看到：** 一台机器多一行 `proxy 在容器 /<容器>`，另一台只有最后一行：

```
proxy 在容器 /<容器>
本机查完：<n> 个容器
```

记下这台机器（`<proxy 机器地址>`）和容器名（`<容器>`，不带开头的 `/`）。

**不对时：**

- 两台都没有这一行：proxy 可能不在容器里。在两台上各执行 `pgrep -af load_balance_proxy_server_example | cut -c1-80`；有输出的那台就是 proxy 机器，以后每条命令把 `docker exec -i <容器> ` 去掉，`docker exec -d <容器> bash X` 换成 `setsid bash X > /dev/null 2>&1 < /dev/null &`。两台都没有输出：停下，带回。
- 有两行以上，或者两台都有：停下，带回两台的输出。

**带回：** 两台机器的输出。

### 2. 放补丁包，核对

**在哪台机器：** 第 1 步找到的 proxy 机器。把 `t51-glm53-proxy-kit.tar.gz` 拷到 `<补丁包目录>`：

```bash
cd <补丁包目录> && tar xzf t51-glm53-proxy-kit.tar.gz && cd t51-glm53-proxy-kit && sha256sum -c SHA256SUMS
```

**应当看到：** 13 行，每行以 `: OK` 结尾。

**不对时：** 有一行不是 `OK`：停下，重新拷一次补丁包。

### 3. 核对现场的 proxy（只读）

**在哪台机器：** proxy 机器。脚本从标准输入读进容器，不往容器里拷文件。

```bash
cd <补丁包目录>/t51-glm53-proxy-kit && docker exec -i <容器> python3 -I -S -B - < t51_proxy_check.py && stat -c "宿主机 dev_ino=%d:%i" /data/pd-diag/logs
```

**应当看到：** 最后两行是 `RESULT: OK` 和宿主机的 `dev_ino`，和 `logdir` 一行的 `dev_ino` 相同。实验室的输出：

```
t51_proxy_check=px-1 python=3.12.3 uid=0
proxies running=1
proc pid=21 uid=0 container=none container_init=no same_mount_ns=yes exe=python3.12 cwd=/opt/proxy
proc port=8000 workers_opt=2 worker_processes=2 children=3 prefiller_hosts=2 decoder_hosts=8
proc argv=python3 load_balance_proxy_server_example.py --host 0.0.0.0 --port 8000 --workers 2 --prefiller-hosts <host> <host> --prefiller-ports 7100 7101 --decoder-hosts <host> <host> <host> <host> <host> <host> <host> <host> --decoder-ports 7100 7101 7102 7103 7104 7105 7106 7107
proc parent_pid=15 parent=sh -c cd /opt/proxy && python3 load_balance_proxy_server_example.py --host <ip> --port 8000 --workers 2 --prefiller-hosts <ip> <ip> --prefiller-ports 7100 7101 --decoder-hosts <ip> <ip> <ip> <ip> <ip> <ip> <ip> <ip> --decoder-ports 7100 7101 7102 7103 7104 7105 7106 7107 >> /var/log/proxy.log 2>&1
proc stdout=file:/var/log/proxy.log stderr=file:/var/log/proxy.log
env PD_PROXY_DIAG*=unset
deps fastapi=0.136.3 httpx=0.28.1 uvicorn=0.54.0 starlette=1.10.0
file path=/opt/proxy/load_balance_proxy_server_example.py
file sha16=85b8982948530758 lines=1396 version=v0.30
logdir path=/data/pd-diag/logs proxy_sees=yes same_as_here=yes dev_ino=65025:768913 mode=1777 owner_uid=1000 proxy_can_write=yes proxy_subdir=missing
RESULT: OK
宿主机 dev_ino=65025:768913
```

上面是实验室走查时 px-1 的输出。补丁包里的核对脚本是 px-2，在 `proc parent=` 一行后面多打一行 `proc ancestor_root=`。同样起法（`docker exec -d … sh -c '…'`）下补测的这一行是：

```
proc ancestor_root=15 pid1=no root=sh -c cd /opt/px && python3 load_balance_proxy_server_example.py --host <ip> --port 8000 --workers 2 --prefiller-hosts <ip> --prefiller-ports 8100 --decoder-hosts <ip> --decoder-ports 8200 >> /var/log/px.log 2>&1
```

逐行的意思：

- `proc pid=`：proxy 主进程在容器里的进程号，记为 `<进程号>`；`proc port=` 记为 `<端口>`；
- `proc argv=`、`parent=`：proxy 的启动参数和起它的那个进程，地址都换成了 `<host>`、`<ip>`；
- `proc ancestor_root=`：沿父进程往上，在容器里走到的最上面那个进程。`pid1=no` 说明 proxy 不是由容器自己的启动命令带起的，停 proxy 不会停容器；
- `proc stdout=`、`stderr=`：proxy 的输出写在哪里；
- `env`：现在没有设 `PD_PROXY_DIAG`；
- `deps`：proxy 那个 Python 的 fastapi、httpx、uvicorn 版本；
- `file`：proxy 文件的路径（记为 `<proxy 文件>`）、sha16、行数、版本。`version=main` 或 `version=v0.30`，记为 `<版本>`；
- `logdir`：proxy 看得到 `/data/pd-diag/logs`、能写；`dev_ino` 和宿主机相同，说明容器里和宿主机上是同一个目录。

**不对时：** 最后一行是 `RESULT: STOP <原因>`，或者两个 `dev_ino` 不同：停下，不做第 4 步以后的任何一步。带回：这条命令的全部输出（里面没有地址和口令）。几种原因：

| 原因 | 说明 |
|---|---|
| `file_not_a_known_version` | 文件不是补丁包认识的版本（多半是现场改过）。再执行一次，在 `t51_proxy_check.py` 后面加 ` --show-diff`，看一遍差异里有没有地址、口令，一起带回；我们按现场版本移植补丁 |
| `file_is_an_older_upstream_version` | 是更早的上游版本，同样要移植。带回全部输出 |
| `proxy_is_container_init` | proxy 是容器的第一个进程，停 proxy 就是停容器，不能只重启 proxy |
| `proxy_started_by_container_init` | proxy 由容器自己的启动命令带起（`proc ancestor_root=` 一行是 `pid1=yes`），停它容器就退出，不能只重启 proxy。proxy 直接跑在宿主机上（第 1 步「不对时」第一条）时，往上走到的是宿主机的 1 号进程，也报这一条，同样停下带回 |
| `stdout_not_a_file` | proxy 的输出接在管道或终端上，第 7 步没法照原样接回去 |
| `proxy_does_not_see_log_dir`、`log_dir_not_the_same_folder`、`proxy_cannot_write_log_dir` | proxy 所在容器没有在同一路径挂 `/data/pd-diag/logs`，或者没有写权限。要加挂载，就得重建容器，不在本手册里 |
| `more_than_one_proxy`、`no_proxy_running` | 容器里有不止一个 proxy，或者没有 |

**带回：** 全部输出。

### 4. 打开 test-glm53 的 proxy 来源

**在哪台机器：** 监控机。在 test-glm53 的 `deploy.env` 末尾加 5 行，再重启拉取程序。要在第 7 步重启 proxy 之前做：第 5 行让节点日志的拉取跳过 `proxy/` 子目录，proxy 的文件只经 proxy 来源收一遍（附录 A）。

```bash
. ~/vllm-pd-test-glm53/vars.sh && printf '\n# proxy 打补丁手册第 4 步\nPROXY_COLLECT=1\nPROXY_MACHINE=%s\nPROXY_LOG_DIR=/data/pd-diag/logs/proxy\nPULL_RSYNC_FLAGS="--append --exclude=/proxy/"\n' '<proxy 机器地址>' >> "$D/deploy.env" && tail -n 5 "$D/deploy.env"
```

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$D" && bin/puller.sh restart && bin/puller.sh status
```

**应当看到：** 第一条打出加的 5 行。第二条：每台机器一行 `OK`，proxy 机器多一行（同一个地址出现两次，第二行是 proxy 来源），最后 `RESULT: OK`。这时 proxy 还没写文件，proxy 那一行也是 `OK`。实验室的输出：

```
RESULT: OK（集群 test-glm53：注册表里没有重复，端口空闲或属于本集群）
拉取程序已启动，每 10 秒拉一轮，镜像到 <总目录>/vllm-timing/test-glm53/pull
RESULT: OK
OK   <P 机器地址> <P 机器地址>      上次成功 1.9 秒前  
OK   <D 机器地址>   <D 机器地址>      上次成功 1.9 秒前  
OK   <proxy 机器地址> <proxy 机器地址>      上次成功 1.8 秒前  
镜像占用 67M
RESULT: OK
```

**不对时：** 某一行不是 `OK`：带回 `bin/puller.sh status` 的输出。

**带回：** 第二条命令的输出。

### 5. 备份 proxy 的启动方式和原文件

**在哪台机器：** proxy 机器。在容器里建 `/root/t51-proxy-bk`（只有 root 能读），存下 proxy 进程的参数、环境、工作目录、输出文件和原文件，再放进起 proxy 用的 `start_proxy.sh`。`<进程号>`、`<proxy 文件>` 是第 3 步记下的值。

```bash
cd <补丁包目录>/t51-glm53-proxy-kit && docker exec -i <容器> bash -s <进程号> <proxy 文件> <<'EOF'
P=$1; F=$2; BK=/root/t51-proxy-bk
[ -e "$BK" ] && { echo "已有 $BK：停下"; exit 1; }
mkdir -m 700 "$BK" && cp /proc/$P/cmdline /proc/$P/environ "$BK"/ && readlink /proc/$P/cwd > "$BK/cwd" && readlink /proc/$P/fd/1 > "$BK/stdout" && readlink /proc/$P/fd/2 > "$BK/stderr" && echo "$F" > "$BK/file" && cp -p "$F" "$BK/orig.py" && echo "备份好：原文件 $(sha256sum "$BK/orig.py" | cut -c1-16)，$(ls "$BK" | tr '\n' ' ')"
EOF
docker exec -i <容器> sh -c 'cat > /root/t51-proxy-bk/start_proxy.sh && chmod 700 /root/t51-proxy-bk/start_proxy.sh && echo 起 proxy 的脚本已放好' < start_proxy.sh
```

**应当看到：** 两行。原文件的 sha16 和第 3 步 `file sha16=` 相同：

```
备份好：原文件 <第 3 步的 sha16>，cmdline cwd environ file orig.py stderr stdout
起 proxy 的脚本已放好
```

再核对容器里的 bash 能跑 `start_proxy.sh`（它要 bash 4.4 以上的 `mapfile -d`；第 7 步和回滚 1 都用它）：

```bash
docker exec <容器> bash -c 'mapfile -d "" -t a < /dev/null && echo "bash ${BASH_VERSION}：start_proxy.sh 可用"'
```

**应当看到：** 一行，以 `可用` 结尾。实验室容器里是：

```
bash 5.2.21(1)-release：start_proxy.sh 可用
```

**不对时：** `已有 /root/t51-proxy-bk：停下`：以前做过一次，先带回 `docker exec <容器> ls -l /root/t51-proxy-bk` 的输出，不要删。bash 那一条没有以 `可用` 结尾的行（报错，或者什么都没打印）：停下，不做第 6、7 步，带回这条命令的输出。其他报错：带回输出。

### 6. 换 proxy 文件

**在哪台机器：** proxy 机器。原地覆盖（属主、权限不变）。正在跑的 proxy 用的是启动时读进内存的代码，换文件不影响它，第 7 步重启以后才用新文件。`<版本>` 是第 3 步的 `main` 或 `v0.30`。

```bash
cd <补丁包目录>/t51-glm53-proxy-kit && docker exec -i <容器> sh -c 'cat > "$1"' sh <proxy 文件> < <版本>/load_balance_proxy_server_example.py && docker exec -i <容器> python3 -I -S -B - --proxy-file <proxy 文件> < t51_proxy_check.py | grep '^file sha16'
```

**应当看到：** 一行。`<版本>` 是 `v0.30` 时：

```
file sha16=68e07c0abd1fbf4c lines=1787 version=patched-v0.30
```

`<版本>` 是 `main` 时是 `file sha16=205aa8d49ad7f4b1 lines=1831 version=patched-main`。

**不对时：** 不是这一行：马上把原文件放回去（`docker exec <容器> sh -c 'cat /root/t51-proxy-bk/orig.py > "$1"' sh <proxy 文件>`），停下，带回输出。

### 7. 重启 proxy（打开记录）

**在哪台机器：** proxy 机器。先停（SIGTERM 发给主进程，uvicorn 的 worker 跟着退出，最多等 60 秒），确认没有 proxy 进程以后，用备份下来的参数、环境、工作目录、输出文件再起，只多两个环境变量。

```bash
cd <补丁包目录>/t51-glm53-proxy-kit && docker exec -i <容器> bash -s < stop_proxy.sh && docker exec -d <容器> bash /root/t51-proxy-bk/start_proxy.sh on && sleep 15 && docker exec -i <容器> python3 -I -S -B - < t51_proxy_check.py | grep -E '^(proxies|env)'
```

**应当看到：**

```
已停 proxy（进程 <进程号>）
proxies running=1
env PD_PROXY_DIAG=1 PD_PROXY_DIAG_DIR=/data/pd-diag/logs/proxy
```

**不对时：**

- `停了 … 以后还有 proxy 进程：停下`：有别的东西（守护脚本、循环）把 proxy 又拉起来了。不要再起第二个；带回 `docker exec <容器> ps -eo pid,ppid,args | cut -c1-120` 的输出（里面有地址，带回前换成 `<ip>`）。
- `proxies running=0`：proxy 没起来，看 proxy 的输出文件（第 3 步 `proc stdout=`）最后 30 行；马上按回滚 1 恢复原文件和原来的启动方式，带回这 30 行。
- `docker exec` 报容器没在跑：容器已经退出。执行 `docker start <容器>`，让它按原来的方式起（文件已经是补丁版，但没设环境变量，行为和原来相同），带回 `docker ps -a` 里这个容器的那一行。

### 8. 重启以后核对（只读）

**在哪台机器：** proxy 机器。

```bash
cd <补丁包目录>/t51-glm53-proxy-kit && docker exec -i <容器> python3 -I -S -B - < t51_proxy_check.py
```

**应当看到：** 和第 3 步比，只有这几处不同，最后 `RESULT: OK`：

- `proc pid=` 是新的进程号；`proc parent_pid=0 parent=?`：由 `docker exec -d` 起，父进程在容器外；
- `env PD_PROXY_DIAG=1 PD_PROXY_DIAG_DIR=/data/pd-diag/logs/proxy`；
- `file sha16=` 是第 6 步那个，`version=patched-<版本>`；
- `logdir` 一行的 `proxy_subdir=` 可能已经是 `exists(…)`（proxy 写了第一个文件）。

`proc argv=`、`proc port=`、`workers_opt=`、`worker_processes=`、`stdout=`、`stderr=` 和第 3 步相同。实验室的输出：

```
t51_proxy_check=px-1 python=3.12.3 uid=0
proxies running=1
proc pid=151 uid=0 container=none container_init=no same_mount_ns=yes exe=python3.12 cwd=/opt/proxy
proc port=8000 workers_opt=2 worker_processes=2 children=3 prefiller_hosts=2 decoder_hosts=8
proc argv=python3 load_balance_proxy_server_example.py --host 0.0.0.0 --port 8000 --workers 2 --prefiller-hosts <host> <host> --prefiller-ports 7100 7101 --decoder-hosts <host> <host> <host> <host> <host> <host> <host> <host> --decoder-ports 7100 7101 7102 7103 7104 7105 7106 7107
proc parent_pid=0 parent=?
proc stdout=file:/var/log/proxy.log stderr=file:/var/log/proxy.log
env PD_PROXY_DIAG=1 PD_PROXY_DIAG_DIR=/data/pd-diag/logs/proxy
deps fastapi=0.136.3 httpx=0.28.1 uvicorn=0.54.0 starlette=1.10.0
file path=/opt/proxy/load_balance_proxy_server_example.py
file sha16=68e07c0abd1fbf4c lines=1787 version=patched-v0.30
logdir path=/data/pd-diag/logs proxy_sees=yes same_as_here=yes dev_ino=65025:768913 mode=1777 owner_uid=1000 proxy_can_write=yes proxy_subdir=missing
RESULT: OK
```

上面也是 px-1 的输出。px-2 在 `proc parent_pid=0 parent=?` 后面多一行 `proc ancestor_root=<新进程号> pid1=no root=python3 load_balance_proxy_server_example.py …`：由 `docker exec -d` 起时父进程在容器外，往上走到的就是 proxy 自己（补测过）。

**不对时：** 有别的不同：带回第 3 步和这一步的全部输出。

**带回：** 全部输出。

### 9. proxy 的记录写进了日志目录

**在哪台机器：** proxy 机器。经 proxy 发 5 个小请求（每个 8 个 token），再看日志目录。

```bash
docker exec <容器> python3 -c 'import json, sys, urllib.request as u
o = u.build_opener(u.ProxyHandler({}))
for i in range(5):
    r = u.Request("http://127.0.0.1:%s/v1/completions" % sys.argv[1], json.dumps({"model": sys.argv[2], "prompt": "hello", "max_tokens": 8}).encode(), {"Content-Type": "application/json"})
    print("请求", i + 1, o.open(r, timeout=120).status)' <端口> <模型名>
```

等 2 秒以后：

```bash
sleep 2 && ls -ln /data/pd-diag/logs/proxy/*/ && cat /data/pd-diag/logs/proxy/*/proxy.*.log | grep -c '"event":"pd_proxy_request"'
```

**应当看到：** 5 行 `请求 <n> 200`；`proxy-<端口>/` 里每个 uvicorn worker 一个文件（`proxy.<启动时刻>.<进程号>.0000.log`），最后一行的数不小于 5（测试集群上同时有别的请求时会更多）：

```
请求 1 200
请求 2 200
请求 3 200
请求 4 200
请求 5 200
total 8
-rw-r--r-- 1 0 0 2895 Nov 19 15:52 proxy.20261119T155248.166.0000.log
-rw-r--r-- 1 0 0 2030 Nov 19 15:52 proxy.20261119T155248.167.0000.log
5
```

**不对时：**

- 请求不是 200：proxy 转发不通，和打补丁无关的原因居多（P、D 没起来、模型名不对）。先按第 3 步 `proc stdout=` 看 proxy 输出文件的最后 30 行，带回。
- 目录不存在、文件里没有 `pd_proxy_request`：带回第 8 步的输出和 `ls -lnR /data/pd-diag/logs/proxy` 的输出。

**带回：** 两条命令的输出。

### 10. 拉取到监控机、进了 ClickHouse，只收了一遍

**在哪台机器：** 监控机。第 9 步以后等 1 分钟再做。

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$D" && bash -c '. bin/lib.sh && load_env >/dev/null && echo "节点日志镜像里的 proxy 子目录：$(ls -d "$PULL_DIR"/*/proxy 2>/dev/null | wc -l)" && echo "proxy 来源镜像里的文件：$(find "$PULL_DIR/_proxy" -name "proxy.*.log" | wc -l)" && echo "采集器解析失败的抽样：$(docker exec "$CENTRAL_CONTAINER" sh -c "cat /var/lib/vector/dead-letter-*.jsonl 2>/dev/null | wc -l")" && curl -s -u "vllm_reader:$CH_READER_PASSWORD" "http://localhost:$CLICKHOUSE_HTTP_PORT/" --data-binary @-' <<'SQL'
SELECT count() AS rows, countIf(status = 'ok') AS ok, countIf(proxy_to_p_ms IS NOT NULL) AS with_p, countIf(proxy_to_d_ms IS NOT NULL) AS with_d, uniqExact(request_id) AS requests, max(ts) AS last FROM vllm_pd.proxy_line WHERE ts > now() - INTERVAL 30 MINUTE FORMAT PrettyCompactMonoBlock
SQL
```

**应当看到：**

- 节点日志镜像里的 proxy 子目录是 `0`：节点日志的拉取跳过了它；
- proxy 来源镜像里的文件数等于第 9 步 `proxy-<端口>/` 里的文件数；
- 解析失败的抽样是 `0`；
- 表里 `rows` 不小于 5，`rows`、`ok`、`with_p`、`with_d`、`requests` 五个数相同（每个请求一行），`last` 是刚才的时间（UTC）。

```
节点日志镜像里的 proxy 子目录：0
proxy 来源镜像里的文件：2
采集器解析失败的抽样：0
   ┌─rows─┬─ok─┬─with_p─┬─with_d─┬─requests─┬────────────────────last─┐
1. │    5 │  5 │      5 │      5 │        5 │ 2026-11-19 15:52:48.657 │
   └──────┴────┴────────┴────────┴──────────┴─────────────────────────┘
```

**不对时：**

- proxy 子目录不是 `0`：第 4 步的 `PULL_RSYNC_FLAGS` 没生效，带回 `grep -n PULL_RSYNC_FLAGS "$D/deploy.env"` 和这一步的输出；
- 文件数是 `0` 或表里没有行：带回 `bin/puller.sh status` 和这一步的输出；
- `rows` 比 `requests` 多：同一个请求收了两遍，带回这一步的输出。

**带回：** 全部输出。

### 11. 面板二的两个 LB proxy 段有值

**在哪台机器：** 监控机。

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$D" && bin/coverage.sh 200 30 | grep -E 'lb_proxy|^段定义'
```

**应当看到：** `P lb_proxy`、`D lb_proxy` 两行的第一个数大于 0，不大于第 10 步的 `requests`。没经过 proxy 的请求（直接发给 vLLM 的）这两段为空。

```
段定义 pd-diag-v3.1-extra-glm53-2026-11-19c：最近 30 分钟里 100 个请求（最多取 200 个）
  P lb_proxy                                          5 / 100
  D lb_proxy                                          5 / 100
```

在 Grafana 的 `GLM-5.3/test-glm53` 里打开面板二的看板，时间选最近 15 分钟，两侧的 `P/D LB proxy` 段有值。

**不对时：** 两个数都是 0：带回这一步和第 10 步的输出。

**带回：** 这条命令的输出。

做完以后回到面板手册第 17 步。

## 回滚（不需要时不做）

### 回滚 1. proxy 恢复原文件和原来的启动方式

**在哪台机器：** proxy 机器。

```bash
cd <补丁包目录>/t51-glm53-proxy-kit && docker exec -i <容器> bash -s < stop_proxy.sh && docker exec <容器> sh -c 'cat /root/t51-proxy-bk/orig.py > "$(cat /root/t51-proxy-bk/file)"' && docker exec -d <容器> bash /root/t51-proxy-bk/start_proxy.sh && sleep 15 && docker exec -i <容器> python3 -I -S -B - < t51_proxy_check.py | grep -E '^(env|file sha16)|^RESULT'
```

**应当看到：** `已停 proxy（进程 <n>）`，然后 `env PD_PROXY_DIAG*=unset`、`file sha16=<第 3 步的 sha16> … version=<版本>`、`RESULT: OK`。

**不对时：** 带回输出。

### 回滚 2. 关掉 test-glm53 的 proxy 来源

**在哪台机器：** 监控机。删掉第 4 步加的 5 行（从 `# proxy 打补丁手册第 4 步` 到它后面的 `PULL_RSYNC_FLAGS=` 一行），重启拉取程序：

```bash
. ~/vllm-pd-test-glm53/vars.sh && cp -p "$D/deploy.env" "$SV/deploy.env.before-proxy-rollback" && sed -i '/^# proxy 打补丁手册第 4 步$/,/^PULL_RSYNC_FLAGS=/d' "$D/deploy.env" && diff "$SV/deploy.env.before-proxy-rollback" "$D/deploy.env"; cd "$D" && bin/puller.sh restart && bin/puller.sh status
```

**应当看到：** `diff` 打出删掉的 5 行（以 `<` 开头）；`status` 里没有 proxy 那一行，最后 `RESULT: OK`。

### 回滚 3. 删掉节点日志目录里 proxy 的文件

**在哪台机器：** proxy 机器，root。

```bash
rm -rf /data/pd-diag/logs/proxy && ls /data/pd-diag/logs
```

**应当看到：** 列出的目录里没有 `proxy`。ClickHouse 里已经收进来的 proxy 行留着，按表的保留天数过期。

## 带回清单

| 步骤 | 带回 |
|---|---|
| 1 | 两台机器的输出 |
| 3 | 全部输出（`STOP` 时另带 `--show-diff` 的输出，带回前看一遍里面有没有地址、口令） |
| 4 | `bin/puller.sh status` 的输出 |
| 8 | 全部输出 |
| 9 | 两条命令的输出 |
| 10 | 全部输出 |
| 11 | 这条命令的输出 |
| 出错时 | 这一步「不对时」写的那些 |

## 附录 A：原因和背景

- **为什么写进节点日志目录、怎么只收一遍。** proxy 所在的机器就是 glm5.3 的一台节点机器，拉取程序已经能用 SSH 连上它、读 `/data/pd-diag/logs`。proxy 来源设成 `PROXY_MACHINE=<proxy 机器地址>`、`PROXY_LOG_DIR=/data/pd-diag/logs/proxy`，拉取程序把这个目录镜像到监控机的 `<镜像目录>/_proxy/<机器>/`，采集器从那里读成 `vllm_pd.proxy_line`。节点日志的拉取是整个目录递归的（`rsync -r`，只取 `*.log`），不加第 4 步的 `PULL_RSYNC_FLAGS="--append --exclude=/proxy/"` 时，它会把同一批文件再镜像一份到 `<镜像目录>/<机器>/proxy/`。这一份不会进 `proxy_line`（那张表只读 `_proxy`），但采集器读节点文件时认不出这种文件名，每一行都算解析失败，会触发「解析失败」告警、写 dead-letter。`--exclude=/proxy/` 只对节点日志目录顶层名叫 `proxy` 的子目录生效；proxy 来源拉的是 `/data/pd-diag/logs/proxy` 本身，不受影响。`--append` 是原来的值，保留。
- **为什么第 4 步在重启 proxy 之前。** 节点日志的拉取一直在跑。先让它跳过 `proxy/`，proxy 写第一个文件时就不会被节点日志的拉取拿走。
- **和节点上别的程序互不影响。** 节点上的过滤器只清理它自己实例目录里、自己命名规则的文件；面板二删除集群时的清理只删实例目录下 `api`、`engine`、`worker`、`pool` 开头的文件和空目录。proxy 只清理 `proxy-<端口>/` 里自己命名规则的文件。
- **请求号。** 打开以后，proxy 给每个请求生成一个 UUID，作为 `X-Request-Id` 头和请求体的 `request_id` 发给 P、D，重算重试时不变；客户端带来的号记在 proxy 行的 `client_request_id`。所以 vLLM 日志和面板二里看到的是 proxy 生成的号，客户端带了 `X-Request-Id` 时，客户端收到的响应 `id` 也从 `chatcmpl-<客户端的号>` 变成 `chatcmpl-<proxy 生成的号>`。按客户端请求号查 vLLM 日志、靠响应 `id` 对账的人要事先知道。关掉（回滚 1）以后恢复原样。
- **两段的含义。** `P lb_proxy` = proxy 收到请求到发给 P 之前；`D lb_proxy` = 收到 P 的响应到发给 D 之前。两个时刻都取 proxy 自己的单调时钟。
- **两个版本。** vllm-ascend 的这个文件 2026-10-30 以后的 main（含 v0.32.0rc1）是 `main` 那一版；v0.30.0 和 d4ad49796 是 `v0.30` 那一版，比 main 少 prefill 会话亲和（`--enable-prefill-affinity`）和非流式请求去掉 `stream_options` 两处。补丁在两版上加的行完全相同；两版不同的 3 个函数（`assign_instances`、`handle_completions_impl`、`stream_service_response`）里，补丁的几处插入点和 main 版相同，差的只是 main 独有的那几行。
- **为什么用 `start_proxy.sh` 起。** 现场怎么起的 proxy 事先不知道。第 5 步从 `/proc` 存下进程的参数、全部环境变量、工作目录和输出文件，`start_proxy.sh` 用 `env -i` 原样还原，只多两个环境变量，输出照原来的文件追加。备份目录里的 `environ` 是 proxy 的全部环境变量，只有 root 能读，确认不再回滚以后可以删掉整个 `/root/t51-proxy-bk`。
- **为什么脚本从标准输入读。** `docker exec -i <容器> python3 -I -S -B - < t51_proxy_check.py` 不往容器里拷文件；`-I -S -B` 不读环境变量、不执行 `.pth`、不写 `.pyc`，少一个脚本就拒绝运行。脚本只读 `/proc` 和 proxy 文件，不写文件、不联网、不起子进程，只用 Python 标准库。

## 附录 B：已知问题

1. 第 7 步停、起 proxy 之间约 15 秒，这段时间发给 proxy 的请求失败。
2. proxy 已经返回流式响应、但还没开始读流时客户端就断开，这个请求不写行（读代码推断，没有复现）。
3. 没有量过打开记录以后对 proxy 吞吐和延迟的影响。每个请求在事件循环上只多建一个对象、放进队列，写文件在后台线程。
4. **核对脚本的零影响：** 第一轮的 strace 记录只覆盖 STOP 分支（2 行的桩文件，`version=other`）。px-2 的 OK 分支也在 strace 下跑过（`RESULT: OK`）：写打开、网络、子进程、删改名都是 0，`write` 只写标准输出。第一轮记录里列在「new .pyc」下的 3 个 `encodings` 的 `.pyc` 是镜像里原有的，不是脚本写的（写打开计数是 0）。
5. **`stop_proxy.sh` 认不出 uvicorn 的 worker。** 它按命令行里有没有 proxy 文件名找进程。uvicorn 的 worker 是 `multiprocessing.spawn` 子进程，命令行里没有文件名，所以「停了以后还有 proxy 进程」查不到残留的 worker。uvicorn 正常会带着 worker 一起退出（实验室如此）。万一主进程先退、worker 留下占着端口，新起的 proxy 绑不上端口，第 7 步走到 `proxies running=0`，回滚 1 也一样绑不上。
6. **优雅退出超过 60 秒时，提示文字会误导。** SIGTERM 以后 uvicorn 会等在途请求结束，长生成请求多时，60 秒后主进程还在，脚本打印的是「有别的东西把 proxy 又拉起来了」那一条。进程号没变、只是还在退出时，再等 1–2 分钟重跑 `stop_proxy.sh`。第一页影响表写的「约 15 秒」，前提是 proxy 空闲。
7. **输出文件被轮转删除以后的 `(deleted)`。** proxy 的标准输出指向一个已删除的文件时，核对脚本报 `stdout=file:<路径> (deleted)`，不报 STOP；`start_proxy.sh` 会往一个字面名字带 ` (deleted)` 的新文件里追加。看到第 3 步的 `stdout=` 以 ` (deleted)` 结尾时，按 STOP 处理，带回。
8. **`not_root` 不在第 3 步的原因表里。** 容器默认用户不是 root 时，脚本报 `not_root`：改用 `docker exec -i -u 0 <容器> …` 再执行。
9. **第 4 步会覆盖现场原有的 `PULL_RSYNC_FLAGS`。** 面板包的模板本来就有 `PULL_RSYNC_FLAGS=--append`；第 4 步在末尾再加一行，shell 取最后一个。现场改过这个值时（限速、`--append-verify` 等），会被替掉；回滚 2 只删这 5 行，恢复原值。做第 4 步以前可以先看 `grep -n '^PULL_RSYNC_FLAGS=' "$D/deploy.env"`：只有一行且是 `--append` 时照做；不是时带回。
10. **打补丁手册 857bbec4 第一页的总顺序里没有 proxy 手册。** 打补丁手册冻结版 98e65095 已经改成 6 段，含 proxy 手册。
11. **第 10 步「不对时」会误判一种情况。** 先做了第 7 步、后做第 4 步时，节点日志的拉取已经把 `<机器>/proxy/` 镜像过一次；加了 `--exclude=/proxy/` 以后，rsync 的 `--delete` 不删接收端被排除的路径，这个目录会一直留着，第 10 步显示子目录 1。这时 `PULL_RSYNC_FLAGS` 其实已经生效：确认 `deploy.env` 里有那一行以后，在监控机上执行一次 `rm -rf "$PULL_DIR"/*/proxy`（`PULL_DIR` 照第 10 步的写法由 `load_env` 给出），再看第 10 步。
12. **请求号的行为变化只写在本手册附录 A**，面板手册里没有。
13. **`main` 那一版没有在实验室里走过。** 它和 `v0.30` 的差异行逐行核过，风险只在 main 独有的 prefill 亲和两处，补丁没有碰它们。

## 附录 C：没有测过的部分

- **内网的真实环境：** proxy 实际的启动方式（守护脚本、systemd 等）、容器的挂载、真实的 P/D 集群。实验室里 proxy 在一个单独的容器里，由 `docker exec -d … sh -c '… >> 日志文件'` 起，P、D 是 vLLM 替身。
- **`main` 那一版没有在实验室里走：** 实验室走的是 `v0.30` 那一版；`main` 那一版只跑过本地测试（`main/tests/`，Python 3.12、fastapi 0.123.10）。
- **proxy 不在容器里的那一支**（第 1 步「不对时」的第一条）。
- **第 3 步 `STOP` 以后的移植**：要等现场的文件或差异带回来。
- **第 5 步 bash 预检不通过的那一支：** 实验室容器是 bash 5.2，bash 低于 4.4 时这条命令的报错没有实测。
- **px-2 的两种起法只在单独的容器里补测过**（容器自己的 `sh -c` 带起：`STOP proxy_started_by_container_init`；`docker exec -d` 带起：`RESULT: OK`），没有在实验室里重走手册。
- **看板本身没有在实验室里打开看：** 第 11 步的 `coverage.sh` 读的是 `vllm_pd.request_json`，和看板上单个请求的面板是同一份文档；两段在看板上的显示以现场第 11 步打开看板为准。
- **不加 `--exclude=/proxy/` 的情况只在实验室里试过一次（附录 A 第一条）：** 节点日志镜像里多出 `proxy/` 子目录，采集器的 dead-letter 有这些行，`proxy_line` 仍是每个请求一行。
