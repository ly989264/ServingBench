#!/bin/bash
# T64 第 12 条：删 glm53-1 各台机器上 remove 留下的 ~/vllm-timing（里面只剩面板二的标记文件 .vllm-timing-home）
# 和空的 ~/vllm-pd-diag（节点上面板二的 conf、logs 的上一级目录，remove 删了 conf、logs，留下它）。
# 在 glm53-test 的监控机上，用装面板的账号执行，分两步：
#   bash 01f_删节点上的vllm-timing.sh save     remove 之前：记下 glm53-1 的机器清单和 SSH 设置（remove 会删掉 deploy.env 和 inventory.json）
#   bash 01f_删节点上的vllm-timing.sh delete   remove 之后：先逐台只读核对，每台的 ~/vllm-timing 都只剩标记文件（或已经没有）、
#                                             ~/vllm-pd-diag 都是空目录（或已经没有）才删；有一台不是这样，或者连不上，一台也不删，列出原因后停下
# 只用面板二自己的 SSH 账号和选项登录；只删 ~/vllm-timing/.vllm-timing-home、随后变空的 ~/vllm-timing 和空的 ~/vllm-pd-diag，别的什么都不动。
set -u
. ~/vllm-pd-test-glm53/vars.sh || { echo "读不了 ~/vllm-pd-test-glm53/vars.sh，停下"; exit 1; }
SAVED=~/vllm-pd-test-glm53/glm53-1-nodes.sh
case ${1:-} in
save)
  G=$ROOT/vllm-pd-grafana-panel2/glm53-1
  [ -f "$G/deploy.env" ] && [ -f "$G/inventory.json" ] || { echo "没有 $G/deploy.env 或 inventory.json（remove 已经做过了？），停下"; exit 1; }
  val() { grep -E "^$1=" "$G/deploy.env" | tail -n 1 | cut -d= -f2- | sed -e "s/^[\"']//" -e "s/[\"']\$//" -e 's/\${HOME}/$HOME/g'; }
  th=$(val TIMING_HOME); nc=$(val NODE_CONF_DIR); nl=$(val NODE_LOG_DIR)
  # The folder above the node's conf and logs, when both sit in the same one directly under the home (glm53-1: $HOME/vllm-pd-diag).
  pd=""
  case $nc in \$HOME/?*/conf) [ "${nc%/*}" = "${nl%/*}" ] && pd=${nc%/*} ;; esac
  case $pd in \$HOME/*/*) pd="" ;; esac
  eval "$(env -i HOME="$HOME" PATH="$PATH" bash -c 'set -a; . "$1" >/dev/null 2>&1; set +a; printf "SSH_USER=%q\nSSH_OPTS=%q\n" "${SSH_USER-}" "${SSH_OPTS-}"' _ "$G/deploy.env")"
  hosts=$(python3 -I -c 'import json, sys
inv = json.load(open(sys.argv[1]))
hs = [inv.get("proxy", {}).get("host")] + [a.rsplit(":", 1)[0] for a in inv["prefill"]["api_servers"] + inv["decode"]["api_servers"]]
print(" ".join(dict.fromkeys(h for h in hs if h)))' "$G/inventory.json") && [ -n "$hosts" ] || { echo "读不出 $G/inventory.json 的机器清单，停下"; exit 1; }
  printf 'SSH_USER=%q\nSSH_OPTS=%q\nTH=%q\nPD=%q\nHOSTS=%q\n' "$SSH_USER" "$SSH_OPTS" "${th:-\$HOME/vllm-timing}" "$pd" "$hosts" > "$SAVED"
  echo "已记下 $(set -- $hosts; echo $#) 台机器和 SSH 设置：$SAVED"
  [ -n "$pd" ] || echo "deploy.env 里 NODE_CONF_DIR、NODE_LOG_DIR 不在家目录下同一个目录里（$nc、$nl），delete 不删它们的上一级目录"
  ;;
delete)
  [ -f "$SAVED" ] || { echo "没有 $SAVED：remove 之前要先执行 save，停下"; exit 1; }
  PD=""; . "$SAVED"
  eval "sshopts=($SSH_OPTS)"; SHOW=${TH/\$HOME/\~}; SHOWD=${PD/\$HOME/\~}
  BOTH=$SHOW; [ -n "$PD" ] && BOTH="$SHOW 和 $SHOWD"
  # On the node, two lines. ~/vllm-timing: "gone", "only" (the folder holds just the marker, a plain file) or "stop <what is there>";
  # ~/vllm-pd-diag: "none" (not asked to), "gone", "empty" (an empty folder, not a link) or "stop <what is there>".
  look='
list() { ls -AdF "$1" "$1"/* "$1"/.[!.]* 2>/dev/null | head -n 5 | tr "\n" " "; }
only() { [ -d "$H" ] && [ ! -L "$H" ] && [ "$(ls -A "$H")" = .vllm-timing-home ] && [ -f "$H/.vllm-timing-home" ] && [ ! -L "$H/.vllm-timing-home" ]; }
empty() { [ -d "$D" ] && [ ! -L "$D" ] && [ -z "$(ls -A "$D")" ]; }
gone() { [ ! -e "$1" ] && [ ! -L "$1" ]; }'
  check="H=$TH; D=$PD$look"'
if gone "$H"; then echo gone; elif only; then echo only; else echo "stop $(list "$H")"; fi
if [ -z "$D" ]; then echo none; elif gone "$D"; then echo gone; elif empty; then echo empty; else echo "stop $(list "$D")"; fi'
  run() { local who=$1; [ -n "$SSH_USER" ] && who=$SSH_USER@$1
    printf '%s\n' "$2" | timeout 60 ssh -o BatchMode=yes -o ConnectTimeout=5 -o LogLevel=ERROR ${sshopts[@]+"${sshopts[@]}"} "$who" 'bash -s' 2>&1; }
  bad=(); todo=()
  for h in $HOSTS; do
    out=$(run "$h" "$check"); a=$(printf '%s\n' "$out" | tail -n 2 | head -n 1); b=$(printf '%s\n' "$out" | tail -n 1)
    case "$a|$b" in
      gone\|none|gone\|gone) echo "$h  $BOTH 已经没有了" ;;
      gone\|empty|only\|none|only\|gone|only\|empty) todo+=("$h") ;;
      *)
        case $a in gone|only|stop*) ;; *) bad+=("$h  连不上或没有回答：$(printf '%s' "$out" | tail -n 1 | cut -c1-80)"); continue ;; esac
        case $b in none|gone|empty|stop*) ;; *) bad+=("$h  连不上或没有回答：$(printf '%s' "$out" | tail -n 1 | cut -c1-80)"); continue ;; esac
        case $a in stop*) bad+=("$h  $SHOW 里不只有标记文件：${a#stop }") ;; esac
        case $b in stop*) bad+=("$h  $SHOWD 不是空目录：${b#stop }") ;; esac ;;
    esac
  done
  if [ ${#bad[@]} -gt 0 ]; then
    printf '%s\n' "${bad[@]}"
    echo "结论：上面几台的 $BOTH 不是只剩标记文件、空目录（或连不上），一台也没有删。把这几行带回来"
    exit 1
  fi
  fail=0
  for h in ${todo[@]+"${todo[@]}"}; do
    r=$(run "$h" "H=$TH; D=$PD$look"'
if only; then rm -f "$H/.vllm-timing-home" && rmdir "$H" || { echo changed; exit; }; elif ! gone "$H"; then echo changed; exit; fi
if [ -n "$D" ] && empty; then rmdir "$D" || { echo changed; exit; }; elif [ -n "$D" ] && ! gone "$D"; then echo changed; exit; fi
echo deleted' | tail -n 1)
    [ "$r" = deleted ] && echo "$h  $BOTH 已删除" || { echo "$h  没有删完：$r"; fail=1; }
  done
  [ $fail = 0 ] && echo "结论：$(set -- $HOSTS; echo $#) 台机器上都没有 $BOTH 了" || { echo "结论：有机器没有删完，见上面"; exit 1; }
  ;;
*) echo "用法：bash $0 save（remove 之前） | delete（remove 之后）"; exit 2 ;;
esac
