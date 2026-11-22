#!/bin/bash
# T64 现场只读核对：删 glm53-1 的 mock 面板二、把 test-glm53 的面板二挪进顶层 GLM-5.3-test 之前，看一眼现场。
# 讨论稿附录 E 的查询 A、B、C 合在这一个脚本里，输出四十行上下。
# 在 glm53-test 的监控机上，用装面板的账号执行：bash 01e_现场只读核对_删mock与挪文件夹.sh
# 只读：监控机上只读文件，只执行 list、others、state 这几个只看不改的命令；glm53-1 的每台机器用面板二自己的
# SSH 账号和选项（glm53-1 的 deploy.env 里的 SSH_USER、SSH_OPTS）登录，只列目录。不改、不删、不重启任何东西。
set -u
. ~/vllm-pd-test-glm53/vars.sh || { echo "读不了 ~/vllm-pd-test-glm53/vars.sh，停下"; exit 1; }
G=$ROOT/vllm-pd-grafana-panel2/glm53-1
[ -f "$G/deploy.env" ] || { echo "没有 $G/deploy.env，停下"; exit 1; }
del=(); mv=()
# One value of glm53-1's deploy.env as written (not expanded here: $HOME in it is the node's home).
raw() { grep -E "^$1=" "$G/deploy.env" | tail -n 1 | cut -d= -f2- | sed -e "s/^[\"']//" -e "s/[\"']\$//" -e 's/\${HOME}/$HOME/g'; }

echo "== A 监控机：glm53-1 的面板二"
ver=$(cat "$G/VERSION" 2>/dev/null)
mode=$(raw NODE_MODE)
CONF=$(raw NODE_CONF_DIR); LOGS=$(raw NODE_LOG_DIR); TH=$(raw TIMING_HOME)
echo "版本 $ver；NODE_MODE=$mode NODE_CONF_DIR=$CONF NODE_LOG_DIR=$LOGS TIMING_HOME=$TH"
[ "$ver" = v3 ] || del+=("版本不是 v3")
[ "$mode" = mock ] || del+=("模式不是 mock")
list=$(cd "$D" && bin/panel2_cluster.sh list 2>&1)
echo "$list"
echo "$list" | grep -q '^glm53-1 ' || del+=("注册表里没有 glm53-1")
echo "$list" | grep -q '^test-glm53 ' || del+=("注册表里没有 test-glm53")
others=$(cd "$D" && python3 scripts/names.py others --cluster glm53-1 --panel panel2 2>&1 | xargs)
echo "别的面板二集群：$others"
case " $others " in *" test-glm53 "*) ;; *) del+=("别的面板二集群里没有 test-glm53（remove 会删插件、重启 Grafana）") ;; esac
same=""
for k in GRAFANA_URL GRAFANA_USER GRAFANA_PASSWORD GRAFANA_CONTAINER; do
  a=$(grep "^$k=" "$ENV2" | tail -n 1 | cut -d= -f2-); b=$(grep "^$k=" "$D/deploy.env" | tail -n 1 | cut -d= -f2-)
  if [ -n "$a" ] && [ "$a" = "$b" ]; then same="$same $k 相同"; else same="$same $k 不同"; del+=("ENV2 的 $k 和 test-glm53 不同"); fi
done
echo "ENV2 和 test-glm53 的 Grafana 4 项：${same# }"

echo "== B 监控机：看板分层（对照表里 GLM-5.3 和 test-glm53 的行，各集群的看板在哪）"
awk '!/^#/ && NF && ($1 == "GLM-5.3" || /test-glm53/)' "$T45/boards_layout.txt"
grep -qE '^GLM-5\.3[[:space:]]+test-glm53[[:space:]]+test-glm53[[:space:]]*$' "$T45/boards_layout.txt" || mv+=("对照表里没有 GLM-5.3 test-glm53 test-glm53 这一行")
state=$(python3 "$T45/arrange_boards.py" state --env-from "$D/deploy.env" 2>&1 | grep -e '^集群' -e '^看板')
echo "$state"
echo "$state" | grep -q '^看板 vllm-pd-request-test-glm53 .*在 GLM-5\.3/test-glm53 ' || mv+=("test-glm53 的面板二看板不在 GLM-5.3/test-glm53")

echo "== C glm53-1 的机器（SSH，只列目录）"
eval "$(env -i HOME="$HOME" PATH="$PATH" bash -c 'set -a; . "$1" >/dev/null 2>&1; set +a; printf "SSH_USER=%q\nSSH_OPTS=%q\n" "${SSH_USER-}" "${SSH_OPTS-}"' _ "$G/deploy.env")"
eval "sshopts=($SSH_OPTS)"
[ -n "$CONF" ] || CONF='$HOME/vllm-pd-diag/conf'
[ -n "$LOGS" ] || LOGS='$HOME/vllm-pd-diag/logs'
[ -n "$TH" ] || TH='$HOME/vllm-timing'
hosts=$(python3 -I -c 'import json, sys
inv = json.load(open(sys.argv[1]))
hs = [inv.get("proxy", {}).get("host")] + [a.rsplit(":", 1)[0] for a in inv["prefill"]["api_servers"] + inv["decode"]["api_servers"]]
print("\n".join(dict.fromkeys(h for h in hs if h)))' "$G/inventory.json" 2>/dev/null)
[ -n "$hosts" ] || del+=("读不出 glm53-1 的机器清单（$G/inventory.json）")
remote="C=$CONF
L=$LOGS
H=$TH
"'P=$(dirname "$C")
echo "conf|$(ls -A "$C" 2>/dev/null | sort | tr "\n" " ")"
echo "mock|$(find "$L" -mindepth 2 -maxdepth 2 -type f \( -name "api.*.log" -o -name "engine.*.log" -o -name "worker.*.log" -o -name "pool.*.log" \) 2>/dev/null | wc -l | tr -d " ")"
P=${P%/}; P=${P:-/}; d=${P#/}
if [ "$P" = / ] || [ "$d" = "${d#*/}" ] || [ "$P" = "${HOME%/}" ] || [ "$P" = "$(dirname "$HOME")" ]; then
  echo "other|跳过：$P"
else
  o=$(find "$P" -mindepth 1 -maxdepth 3 2>/dev/null | grep -vE "^$C(/|\$)|^$L(/[^/]+)?\$|^$L/[^/]+/(api|engine|worker|pool)\.[^/]*\.log\$" | sort)
  echo "other|$(printf "%s\n" "$o" | grep -c .)|$(printf "%s\n" "$o" | head -n 3 | tr "\n" " ")"
fi
if [ -d "$H" ]; then
  echo "home|$(du -sh "$H" 2>/dev/null | cut -f1)"
  find "$H" -mindepth 1 -maxdepth 1 | sort | while read -r p; do
    echo "item|${p#$H/}|$(du -sh "$p" 2>/dev/null | cut -f1)|$(find "$p" -type f 2>/dev/null | wc -l | tr -d " ")|$(find "$p" -type f -printf "%Tm-%Td %TH:%TM\n" 2>/dev/null | sort | tail -n 1)"
  done
else
  echo "home|"
fi'
hname=$TH; [ "$TH" = '$HOME/vllm-timing' ] && hname='~/vllm-timing'
confs=""; lines=(); n=0
for h in $hosts; do
  n=$((n + 1)); who=$h; [ -n "$SSH_USER" ] && who=$SSH_USER@$h
  out=$(printf '%s\n' "$remote" | timeout 60 ssh -o BatchMode=yes -o ConnectTimeout=5 -o LogLevel=ERROR ${sshopts[@]+"${sshopts[@]}"} "$who" 'bash -s' 2>&1); rc=$?
  if [ $rc -ne 0 ] || ! printf '%s\n' "$out" | grep -q '^conf|'; then
    lines+=("$h  SSH 不通：$(printf '%s' "$out" | head -n 1 | cut -c1-80)"); del+=("$h SSH 不通"); continue
  fi
  c=$(printf '%s\n' "$out" | sed -n 's/^conf|//p' | sed 's/ *$//'); c=${c:-（没有）}
  confs="$confs$c
"
  extra=""; for x in $c; do case $x in __pycache__|pd_diag_logfilter.py|pd_diag_logging.json|segments.json|（没有）) ;; *) extra="$extra $x" ;; esac; done
  [ -n "$extra" ] && del+=("$h 的 conf 里还有$extra（remove 会整个删掉 conf）")
  m=$(printf '%s\n' "$out" | sed -n 's/^mock|//p')
  IFS='|' read -r _ on of <<<"$(printf '%s\n' "$out" | grep '^other|')"; of=$(printf '%s' "$of" | sed 's/ *$//')
  hs=$(printf '%s\n' "$out" | sed -n 's/^home|//p')
  if [ -z "$hs" ]; then home="$hname 没有"; else
    home="$hname $hs："; sep=""
    while IFS='|' read -r _ name size files last; do
      [ -n "$name" ] || continue
      case $name in .vllm-timing-home|run|python|logs) tag="" ;; *) tag="（表外）" ;; esac
      home="$home$sep$name$tag $size $files 个 $last"; sep="；"
    done <<<"$(printf '%s\n' "$out" | grep '^item|')"
  fi
  case $on in
    跳过*) oth="${on#跳过：} 是浅目录，没有遍历"; del+=("$h 的 ${on#跳过：} 是浅目录，没有遍历，conf 和日志以外有什么不清楚") ;;
    *) oth="$(dirname "$CONF") 下其他 $on 个${of:+：$of}" ;;
  esac
  lines+=("$h  mock 和过滤器写的文件 $m 个；$oth；$home")
done
u=$(printf '%s' "$confs" | sort -u | grep -c .)
if [ "$u" -eq 0 ]; then :; elif [ "$u" -eq 1 ]; then echo "$CONF：$(printf '%s' "$confs" | sort -u)（$(printf '%s' "$confs" | grep -c .) 台相同）"; else echo "$CONF 各台不同："; printf '%s' "$confs" | sort | uniq -c; fi
printf '%s\n' "${lines[@]}"

echo "== 结论"
if [ ${#del[@]} -eq 0 ]; then echo "删 glm53-1 的 mock 面板二：和讨论稿附录 E 的预期一致，可以执行"; else echo "删 glm53-1 的 mock 面板二：先停下，带回这份输出：$(printf '%s；' "${del[@]}")"; fi
if [ ${#mv[@]} -eq 0 ]; then echo "挪文件夹：和预期一致"; else echo "挪文件夹：和预期不同，带回这份输出：$(printf '%s；' "${mv[@]}")"; fi
