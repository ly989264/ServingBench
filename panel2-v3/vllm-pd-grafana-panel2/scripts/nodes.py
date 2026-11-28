#!/usr/bin/env python3
"""Runs on the monitoring machine. Works on every P and D machine of inventory.json over SSH, or on the ones named.

  nodes.py check      preflight: SSH, Python, rsync, disk of NODE_LOG_DIR, clock
  nodes.py paths      no SSH: per machine, the folders, the instance names and (NODE_MODE=vllm) what goes into each launch script
  nodes.py push       sends the three node files to NODE_CONF_DIR; only a file whose sha256 differs from the node's is
                      replaced (written to a temporary name, then renamed), the others keep their inode and ctime
  nodes.py selfcheck  runs scripts/node_selfcheck.py on each machine with the packaged sample, against the pushed files;
                      NODE_MODE=vllm: also checks the running vLLM containers (found by itself when NODE_VLLM_CONTAINERS is blank)
  nodes.py start      NODE_MODE=mock only: checks MOCK_RATE per D, pushes the files, stops what runs, starts the mock writer
  nodes.py stop       stops the mock from here over SSH, whatever node_mock.sh the node has: SIGTERM, up to 10 s, SIGKILL
  nodes.py clean      mock: stops the mock, deletes its logs slowly (0.2 s per file, nice 19), the node files and its folders;
                      vllm: only the mock folders, never the node files or the logs vLLM writes
  nodes.py status     mock: FAIL when the run-instance processes of a machine are not as many as its instances
  nodes.py mock-clean mock only, used by bin/clear_data.sh: stops the mock and deletes the logs it wrote (node_mock.sh clean)

One line per machine, OK or FAIL, and a summary at the end, short enough to copy by hand.
"""
import argparse
import atexit
import hashlib
import io
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from inventory_tool import load as load_inventory, machines  # noqa: E402
from names import Names, NamesError  # noqa: E402
import criteria  # noqa: E402

ENV = os.environ
PACK = pathlib.Path(__file__).resolve().parent.parent
LIMIT = threading.Semaphore(int(ENV.get("PULL_CONCURRENCY") or 16))
# Named by cluster, so that removing one cluster closes only its own shared connections (scripts/uninstall_old.py).
try:
    SOCKETS = tempfile.mkdtemp(prefix=Names(ENV).get("SSH_SOCKET_PREFIX"))
except NamesError as e:
    raise SystemExit("错误：%s" % e)
atexit.register(shutil.rmtree, SOCKETS, True)
NODE_DIR = "vllm-timing-node"
NODE_FILES = ["node/pd_diag_logging.json", "node/pd_diag_logfilter.py", "segments.json"]
MOCK_FILES = ["bin/lib.sh", "bin/node_mock.sh", "mock/pd032_mock.py", "inventory.json"]
SHIP_ENV = ["MOCK_RATE", "MOCK_SEED", "MOCK_P_RANKS", "MOCK_STEP_EVENTS", "MOCK_CLOCK_OFFSET_S", "MOCK_TRUTH_DIR", "NODE_CONF_DIR", "NODE_LOG_DIR"]
MODE = (ENV.get("NODE_MODE") or "mock").strip()
PRINT = threading.Lock()


def home_path(p, fallback):
    p = (p or fallback).strip()
    return p


def conf_dir():
    return home_path(ENV.get("NODE_CONF_DIR_RAW") or ENV.get("NODE_CONF_DIR"), "$HOME/vllm-pd-diag/conf")


def log_dir():
    return home_path(ENV.get("NODE_LOG_DIR_RAW") or ENV.get("NODE_LOG_DIR"), "$HOME/vllm-pd-diag/logs")


def sh_path(p):
    """A path for the remote shell: $HOME stays a variable, everything else is quoted."""
    if p.startswith("$HOME/"):
        return '"$HOME"/' + shlex.quote(p[6:])
    if p.startswith("~/"):
        return '"$HOME"/' + shlex.quote(p[2:])
    return shlex.quote(p)


def ssh_command(addr):
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ControlMaster=auto", "-o", "ControlPersist=30", "-o", "ControlPath=%s/%%C" % SOCKETS,
           "-o", "StrictHostKeyChecking=accept-new", "-o", "LogLevel=ERROR"]
    cmd += shlex.split(ENV.get("SSH_OPTS") or "")
    if ENV.get("SSH_USER"):
        cmd += ["-l", ENV["SSH_USER"]]
    return cmd + [addr]


def remote(addr, script, stdin=None, timeout=300, progress=None):
    """stderr goes to a file: the connection ssh leaves behind for reuse keeps it open, and a pipe would never end.
    With progress, the lines the script prints that start with "进度 " or "删除 " are shown at once, prefixed by addr."""
    with tempfile.TemporaryFile() as err:
        if progress is None:
            p = subprocess.run(ssh_command(addr) + ["sh -c " + shlex.quote(script)], input=stdin, stdout=subprocess.PIPE, stderr=err, timeout=timeout)
            out = p.stdout
        else:
            p = subprocess.Popen(ssh_command(addr) + ["sh -c " + shlex.quote(script)], stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=err)
            if stdin:
                p.stdin.write(stdin)
                p.stdin.close()
            timer = threading.Timer(timeout, p.kill)
            timer.start()
            lines = []
            for raw in p.stdout:
                lines.append(raw)
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line.startswith(("进度 ", "删除 ")):
                    with PRINT:
                        print("     %s %s" % (addr, line), flush=True)
            p.wait()
            timer.cancel()
            out = b"".join(lines)
        err.seek(0)
        return p.returncode, out.decode("utf-8", "replace"), err.read().decode("utf-8", "replace").strip()


def node_home():
    """TIMING_HOME on the nodes as deploy.env writes it ($HOME stays a variable); blank means $HOME/vllm-timing (bin/lib.sh)."""
    v = ""
    for line in (PACK / "deploy.env").read_text(encoding="utf-8").splitlines():
        if line.startswith("TIMING_HOME="):
            v = line.split("=", 1)[1].strip().strip("'\"")
    return v or "$HOME/vllm-timing"


def short_names(names):
    """D0 D1 ... D7 as D0–D7 when they run on without a gap; otherwise as they are."""
    if len(names) > 2 and len({n[0] for n in names}) == 1:
        nums = [int(n[1:]) for n in names]
        if nums == list(range(nums[0], nums[0] + len(nums))):
            return "%s%d–%s%d" % (names[0][0], nums[0], names[0][0], nums[-1])
    return " ".join(names)


def instances(inv):
    """{machine: [(name, port)]} in inventory order: P0.. from prefill.api_servers, D0.. from decode.api_servers."""
    out = {}
    for side, letter in (("prefill", "P"), ("decode", "D")):
        for i, a in enumerate(inv[side]["api_servers"]):
            host, port = a.rsplit(":", 1)
            out.setdefault(host, []).append(("%s%d" % (letter, i), port))
    return out


PROBE = r'''
for c in "$PY" python3 python3.13 python3.12 python3.11 python3.10 python3.9 python; do
  [ -n "$c" ] && v=$("$c" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3]) if sys.version_info >= (3, 9) else sys.exit(1)' 2>/dev/null) && py="$c" && break
  v=""
done
echo "python ${v:-none}"
echo "pybin ${py:-}"
echo "arch $(uname -m)"
mkdir -p LOGDIR 2>/dev/null
echo "disk $(df -Pk LOGDIR 2>/dev/null | awk 'NR == 2 { printf "%d", $4 / 1048576 }')"
echo "logw $( (touch LOGDIR/.w 2>/dev/null && rm -f LOGDIR/.w && echo yes) || echo no)"
echo "user $(id -un) $(id -u)"
echo "host $(hostname)"
echo "clock $(date +%s)"
echo "docker $(command -v docker >/dev/null 2>&1 && echo yes || echo no)"
echo "rsync $($RS --version 2>/dev/null | head -n 1)"
'''


def probe(addr):
    py = shlex.quote(ENV.get("NODE_PYTHON") or "")
    script = "PY=%s\nRS=%s\n%s" % (py, shlex.quote(ENV.get("RSYNC_PATH") or "rsync"), PROBE.replace("LOGDIR", sh_path(log_dir())))
    t0 = time.time()
    code, out, err = remote(addr, script, timeout=30)
    t1 = time.time()
    if code != 0:
        return None, err or "ssh 退出码 %d" % code
    info = dict(line.split(" ", 1) for line in out.splitlines() if " " in line)
    info["skew"] = int(info.get("clock", "0")) + 0.5 - (t0 + t1) / 2   # date +%s cuts the fraction off
    return info, ""


def check(addr, names):
    info, err = probe(addr)
    if info is None:
        who = ("%s@" % ENV["SSH_USER"] if ENV.get("SSH_USER") else "") + addr
        return False, "SSH 连不上（%s）→ 在监控机上执行 ssh-copy-id %s，或检查网络、SSH_USER、SSH_OPTS" % (err[:60], who)
    arch = info.get("arch", "?")
    need = int(ENV.get("NODE_MIN_DISK_GB") or 50)
    disk = int(info.get("disk") or 0)
    python = info.get("python", "none")
    head = "%s %s Python %s 日志盘剩余 %d GB 时钟 %+.1f 秒 实例 %s" % (info.get("host", "?"), arch, python, disk, info["skew"], " ".join(n for n, _ in names))
    if arch not in ("aarch64", "arm64", "x86_64", "amd64"):
        return False, "%s → 架构不支持" % head
    if python == "none":
        head += "（没有 Python 3.9 及以上，自检和 mock 用包里带的）"
    if info.get("logw") != "yes":
        return False, "%s → 日志目录 %s 建不了或写不了（deploy.env 的 NODE_LOG_DIR）" % (head, log_dir())
    if disk < need:
        return False, "%s → 日志目录所在盘要 %d GB（deploy.env 的 NODE_MIN_DISK_GB）" % (head, need)
    rsync_line = info.get("rsync", "")
    if "version" not in rsync_line:
        return False, "%s → 没有 rsync：yum install rsync 或 apt install rsync；装在别处时把路径写进 deploy.env 的 RSYNC_PATH" % head
    if " 3." not in rsync_line:
        return False, "%s → rsync 不是 3.x（%s）" % (head, rsync_line[:40])
    if abs(info["skew"]) >= 300:
        return False, "%s → 时钟差超过 5 分钟，先校时" % head
    if abs(info["skew"]) > 1:
        # P and D are placed by their own clocks on the P / D timeline; a skew shows up as a wrong gap between them.
        return "WARN", "%s → 时钟差超过 1 秒，P / D 时间线上 P 末到 D 首的间隔会差这么多。建议校时（chrony/ntp）" % head
    return True, head


def tar_of(files, extra=None):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in files:
            tar.add(str(PACK / rel), arcname=os.path.basename(rel) if rel.startswith("node/") or rel == "segments.json" else rel)
        for name, data in (extra or {}).items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def node_sums():
    return criteria.expected_node_sums(str(PACK))


SHA = '(sha256sum "$1" 2>/dev/null || shasum -a 256 "$1" 2>/dev/null) | cut -c1-64'


def push(addr):
    """Three files into NODE_CONF_DIR (design 2.10, fix 1): the node's sha256 of each is compared with the one to push and
    only a file that differs is replaced, by a rename from a temporary folder next to it. An unchanged file keeps its inode
    and ctime, so that a later change of ctime still means a new file."""
    d = sh_path(conf_dir())
    data = tar_of(NODE_FILES)
    want = node_sums()
    names = list(criteria.NODE_FILES)
    script = (
        "set -e; sha() { %s; }; D=%s; mkdir -p \"$D\"; T=$(mktemp -d \"$D/.push.XXXXXX\"); tar xzf - -C \"$T\"; "
        "for f in %s; do "
        "  if [ -f \"$D/$f\" ] && [ \"$(sha \"$D/$f\")\" = \"$(sha \"$T/$f\")\" ]; then rm -f \"$T/$f\"; echo \"same $f\"; "
        "  else chmod 0644 \"$T/$f\"; mv -f \"$T/$f\" \"$D/$f\"; echo \"new $f\"; fi; "
        "done; rmdir \"$T\"; [ \"$(stat -c %%a \"$D\" 2>/dev/null)\" = 755 ] || chmod 0755 \"$D\"; "
        "for f in %s; do echo \"sum $f $(sha \"$D/$f\")\"; done" % (SHA, d, " ".join(names), " ".join(names)))
    code, out, err = remote(addr, script, stdin=data)
    if code != 0:
        return False, "下发失败：%s" % (err or out)[:160]
    got = {x.split()[1]: x.split()[2] if len(x.split()) > 2 else "" for x in out.splitlines() if x.startswith("sum ")}
    verdict = criteria.judge_node_sums(want, got)
    if verdict.level != "OK":
        return False, "下发后校验和不对：%s" % verdict.text
    new = [x.split()[1] for x in out.splitlines() if x.startswith("new ")]
    same = [x.split()[1] for x in out.splitlines() if x.startswith("same ")]
    how = ("换了 %d 个（%s）" % (len(new), " ".join(new)) if new else "三个文件内容都没变，一个都没换") + (
        "，%d 个内容相同没有动" % len(same) if new and same else "")
    return True, "三个文件已在 %s（segments.json %s）：%s" % (conf_dir(), want["segments.json"][:8], how)


def conf_in_container():
    """NODE_CONF_DIR_IN_CONTAINER; blank means the same path as on the host (design 2.8)."""
    return (ENV.get("NODE_CONF_DIR_IN_CONTAINER") or "").strip() or conf_dir()


def log_in_container():
    """Where the filter writes inside the container: line.log_dir of the segments.json this machine pushes."""
    return json.loads((PACK / "segments.json").read_text(encoding="utf-8"))["line"]["log_dir"]


def per_addr(name):
    """deploy.env value `address=a[;b],address=c` as {address: [a, b]} (NODE_VLLM_CONTAINERS, NODE_VLLM_STDOUT_FILES)."""
    out = {}
    for part in (ENV.get(name) or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out.setdefault(k.strip(), []).extend(x.strip() for x in v.split(";") if x.strip())
    return out


# Runs on the node: which docker works, the vLLM containers (named in deploy.env, or found here), and in each of them
# the three files, the log folder, the environment of the vLLM processes and the standard output. Reads only.
# Containers are found by their command, then by the cgroup of a vllm serve process, then by `docker top`: a process
# started the way vLLM is (vllm serve, python -m vllm.entrypoints..., a <name with vllm>.py serve). The patterns are
# written with brackets (vllm[ ]serve) and the script has no comments, so that they never match the script's own shell.
VLLM_CHECK = r"""
D=docker
if ! docker version >/dev/null 2>&1; then
  if sudo -n docker version >/dev/null 2>&1; then D="sudo -n docker"; else D=""; fi
fi
printf 'docker\t%s\n' "${D:-no}"
printf 'hostconf\t%s\n' "$HC"
for f in $OUTFILES; do if [ -r "$f" ]; then printf 'outfile\t%s\tyes\n' "$f"; else printf 'outfile\t%s\tno\n' "$f"; fi; done
[ -n "$D" ] || exit 0
if [ -z "$WANT" ]; then
  how=cmd
  found=$($D ps --no-trunc --format '{{.Names}}	{{.Command}}' 2>/dev/null | awk -F'\t' 'tolower($2) ~ /vllm/ {print $1}')
  if [ -z "$found" ]; then
    how=cgroup
    for id in $(for p in $(pgrep -f 'vllm[ ]serve' 2>/dev/null); do grep -oE '[0-9a-f]{64}' "/proc/$p/cgroup" 2>/dev/null | head -n 1; done | sort -u); do
      found="$found $($D ps --no-trunc --format '{{.ID}} {{.Names}}' 2>/dev/null | awk -v id="$id" '$1 == id {print $2}')"
    done
  fi
  if [ -z "$(echo $found)" ]; then
    how=top
    for c in $($D ps --format '{{.Names}}' 2>/dev/null); do
      $D top "$c" -eo pid,args 2>/dev/null | grep -qE 'vllm[ ]serve|vllm[.]entrypoints|vllm[A-Za-z0-9_.-]*[.]py[ ]serve' && found="$found $c"
    done
  fi
  WANT=$(echo $found)
  printf 'found\t%s\t%s\n' "$how" "$WANT"
fi
for c in $WANT; do
  printf 'state\t%s\t%s\n' "$c" "$($D inspect --format '{{.State.Running}}|{{.HostConfig.LogConfig.Type}}' "$c" 2>&1 | head -n 1)"
  printf 'mounts\t%s\t%s\n' "$c" "$($D inspect --format '{{json .Mounts}}' "$c" 2>/dev/null | head -c 20000)"
  $D exec -e CIN="$CIN" -e LIN="$LIN" "$c" sh -c '
    for f in pd_diag_logging.json pd_diag_logfilter.py segments.json; do
      s=-
      [ -f "$CIN/$f" ] && s=$( (sha256sum "$CIN/$f" 2>/dev/null || python3 -c "import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], \"rb\").read()).hexdigest())" "$CIN/$f" 2>/dev/null || echo "?") | cut -c1-64)
      printf "in\tfile\t%s\t%s\n" "$f" "$s"
    done
    if [ -d "$LIN" ] && [ -w "$LIN" ]; then printf "in\tlogw\tyes\n"; else printf "in\tlogw\tno\n"; fi
    r=none
    for d in /proc/[0-9]*; do
      cmd=$(tr "\0" " " < "$d/cmdline" 2>/dev/null)
      case "$cmd" in *"sh -c"*) continue ;; *vllm*) ;; *) continue ;; esac
      if [ -r "$d/environ" ] && tr "\0" "\n" < "$d/environ" >/dev/null 2>&1; then r=yes; break; fi
      r=no
    done
    printf "in\tenviron\t%s\n" "$r"
  ' 2>&1 | sed "s/^/$c	/" | sed -n "s/^\([^	]*\)	in	/exec	\1	/p"
  out=$($D logs --tail 1 "$c" 2>&1); rc=$?
  if [ "$rc" = 0 ] && [ -n "$out" ]; then printf 'logs\t%s\tyes\n' "$c"; else printf 'logs\t%s\tno\n' "$c"; fi
done
"""


def selfcheck(addr):
    info, err = probe(addr)
    if info is None:
        return [("FAIL", "SSH 连不上（%s）" % err[:80])]
    py = node_python(addr, info)
    data = tar_of(["scripts/node_selfcheck.py", "samples/pd032_full_P0_D0.log.gz", "samples/pd032_truth_P0_D0.jsonl"])
    d = sh_path(conf_dir())
    extra = []
    if ENV.get("NODE_CONTAINER_UID"):
        extra += ["--uid", ENV["NODE_CONTAINER_UID"]]
    if MODE == "vllm" and ENV.get("NODE_SELFCHECK_IMAGE"):
        extra += ["--docker-image", ENV["NODE_SELFCHECK_IMAGE"], "--conf-in-container", conf_in_container()]
    script = (
        "T=$(mktemp -d); trap 'rm -rf \"$T\"' EXIT; tar xzf - -C \"$T\"; D=%s; "
        "VLLM_LOGGING_CONFIG_PATH=\"$D/pd_diag_logging.json\" PYTHONPATH=\"$D\" PD_DIAG_INSTANCE=selfcheck %s -I \"$T/scripts/node_selfcheck.py\" "
        "--sample \"$T/samples/pd032_full_P0_D0.log.gz\" --truth \"$T/samples/pd032_truth_P0_D0.jsonl\" --log-dir %s %s" % (
            d, shlex.quote(py), sh_path(log_dir()), " ".join(shlex.quote(x) for x in extra)))
    # -I keeps the current folder and PYTHON* variables out; the module comes in through sys.path from PYTHONPATH below.
    script = script.replace(" -I ", " ")
    code, out, err = remote(addr, script, stdin=data, timeout=600)
    lines = [x for x in out.splitlines() if x.startswith(("OK", "FAIL"))]
    fails = [x[5:] for x in lines if x.startswith("FAIL")]
    if code == 0 and not fails:
        res = [("OK", "%s 自检 %d 项全过" % (info.get("host", ""), len(lines)))]
    else:
        res = [("FAIL", "%s 自检没过：%s" % (info.get("host", ""), ("；".join(fails) or err or out)[:300]))]
    if MODE == "vllm":
        res += vllm_containers(addr)
    return res


def host_path_in_container(mounts, host_path):
    """Where a host path is visible inside the container, through the longest bind mount that holds it; None if none."""
    best = None
    for mnt in mounts or []:
        src, dst = (mnt.get("Source") or "").rstrip("/"), (mnt.get("Destination") or "").rstrip("/")
        if src and (host_path == src or host_path.startswith(src + "/")):
            if best is None or len(src) > len(best[0]):
                best = (src, dst)
    return None if best is None else best[1] + host_path[len(best[0]):]


def vllm_containers(addr):
    """F1 2 and design 2.2: the running vLLM containers of this machine, judged from inside. WARN, never OK, when they
    could not be checked (no docker for the SSH user, no container found)."""
    named = per_addr("NODE_VLLM_CONTAINERS").get(addr, [])
    outfiles = per_addr("NODE_VLLM_STDOUT_FILES").get(addr, [])
    cin, lin = conf_in_container(), log_in_container()
    env = "WANT=%s CIN=%s LIN=%s HC=%s OUTFILES=%s\n" % (
        shlex.quote(" ".join(named)), shlex.quote(cin), shlex.quote(lin), sh_path(conf_dir()), shlex.quote(" ".join(outfiles)))
    code, out, err = remote(addr, "export WANT CIN LIN\n" + env + VLLM_CHECK, timeout=120)
    rows = [x.split("\t") for x in out.splitlines()]
    get = lambda k: [r[1:] for r in rows if r[0] == k]  # noqa: E731
    res = []
    for f, ok_ in get("outfile"):
        res.append(("OK", "标准输出文件 %s 能读（NODE_VLLM_STDOUT_FILES）" % f) if ok_ == "yes" else
                   ("FAIL", "标准输出文件 %s 读不了（NODE_VLLM_STDOUT_FILES）" % f))
    dock = (get("docker") or [["?"]])[0][0]
    if dock in ("no", "?"):
        res.append(("WARN", "SSH 用户不能执行 docker（不在 docker 组，也不能免密 sudo docker），没有核对真实容器" if dock == "no"
                    else "没有核对真实容器：%s" % (err or out or "退出码 %d" % code)[:100]))
        return res
    found = get("found")
    if found:
        how, names = found[0][0], found[0][1].split() if len(found[0]) > 1 else []
        if not names:
            res.append(("WARN", "没有找到 vLLM 容器，没有核对真实容器 → 填 deploy.env 的 NODE_VLLM_CONTAINERS"))
            return res
        basis = {"cmd": "命令含 vllm", "cgroup": "vllm serve 进程在这个容器里", "top": "容器里有 vllm 进程"}[how]
        res.append(("INFO", "找到 vLLM 容器 %s（%s）；不对时填 deploy.env 的 NODE_VLLM_CONTAINERS" % (" ".join(names), basis)))
    else:
        names = named
    want = node_sums()
    host_conf = (get("hostconf") or [[conf_dir()]])[0][0]
    for c in names:
        state = dict((r[0], r[2:]) for r in rows if len(r) > 1 and r[1] == c and r[0] in ("state", "mounts", "logs"))
        st = (state.get("state") or [""])[0]
        if not st.startswith("true|"):
            res.append(("FAIL", "容器 %s 不在运行（%s）" % (c, st[:100] or "docker inspect 没有结果")))
            continue
        driver = st.split("|", 1)[1]
        inside = [r[2:] for r in rows if r[0] == "exec" and len(r) > 3 and r[1] == c]
        files = {r[1]: r[2] for r in inside if r[0] == "file" and len(r) > 2}
        logw = next((r[1] for r in inside if r[0] == "logw"), "?")
        environ = next((r[1] for r in inside if r[0] == "environ"), "?")
        try:
            mounts = json.loads((state.get("mounts") or ["[]"])[0] or "[]")
        except ValueError:
            mounts = []
        seen = host_path_in_container(mounts, host_conf)
        hint = ("；下发的宿主机目录 %s 在容器里是 %s，NODE_CONF_DIR_IN_CONTAINER 写的是 %s" % (host_conf, seen, cin)
                if seen and seen != cin else "")
        missing = [f for f in criteria.NODE_FILES if files.get(f, "-") == "-"]
        differ = [f for f in criteria.NODE_FILES if f not in missing and files.get(f) != want[f]]
        if not files:
            res.append(("FAIL", "容器 %s 里执行不了检查（docker exec）：%s" % (c, (err or "没有输出")[:100])))
        elif missing:
            res.append(("FAIL", "容器 %s 里 %s 没有 %s：容器内路径和下发的目录不一样%s" % (c, cin, " ".join(missing), hint)))
        elif differ:
            res.append(("FAIL", "容器 %s 里 %s 的 %s 和下发的不同：这个目录不是 push 写入的那一个%s" % (c, cin, " ".join(differ), hint)))
        elif logw != "yes":
            res.append(("FAIL", "容器 %s 里日志目录 %s 写不了（docker exec 的默认用户）" % (c, lin)))
        else:
            res.append(("OK", "容器 %s：%s 里是下发的三个文件（sha256 相同），日志目录 %s 能写" % (c, cin, lin)))
        if environ == "no":
            res.append(("WARN", "容器 %s：读不了 vLLM 进程的环境变量，结论只靠文件和标准输出判断" % c))
        elif environ == "none":
            res.append(("WARN", "容器 %s 里没有 vllm 进程，环境变量没有核对" % c))
        if not outfiles:
            if (state.get("logs") or ["no"])[0] == "yes":
                res.append(("OK", "容器 %s 的标准输出在 docker logs 里（日志驱动 %s）" % (c, driver)))
            else:
                res.append(("WARN", "容器 %s 的标准输出不在 docker logs 里（日志驱动 %s，没有读到行）→ 填 deploy.env 的 NODE_VLLM_STDOUT_FILES" % (c, driver)))
    return res


def mock_tar(info):
    """The mock writer and what it reads; the pack's own Python for a machine that has none."""
    lines = ["# 从监控机的 deploy.env 生成。只有节点需要的参数，没有密码。"]
    raw = (PACK / "deploy.env").read_text().splitlines()
    lines += [x for x in raw if x.startswith("TIMING_HOME=") or x.startswith("PYTHON=")]
    raw = {"NODE_CONF_DIR": conf_dir(), "NODE_LOG_DIR": log_dir()}
    lines += ["%s=%s" % (k, raw.get(k, ENV.get(k, ""))) for k in SHIP_ENV if k in raw or ENV.get(k) is not None]
    files = list(MOCK_FILES)
    if info.get("python", "none") == "none":
        arch = {"arm64": "aarch64", "amd64": "x86_64"}.get(info.get("arch"), info.get("arch"))
        found = sorted((PACK / "offline").glob("python-*-%s-*.tar.gz" % arch))
        if not found:
            raise RuntimeError("这台机器没有 Python 3.9 及以上，offline 目录里也没有 %s 的 Python" % arch)
        files.append(str(found[0].relative_to(PACK)))
    return tar_of(files, {"deploy.env": ("\n".join(lines) + "\n").encode()})


def node_python(addr, info):
    """The Python 3.9+ to use on the machine: its own, or the pack's, unpacked into TIMING_HOME by bin/lib.sh."""
    if info.get("pybin"):
        return info["pybin"]
    code, out, err = remote(addr, 'mkdir -p ~/%s && tar xzf - -C ~/%s' % (NODE_DIR, NODE_DIR), stdin=mock_tar(info))
    if code != 0:
        raise RuntimeError("拷不过去：%s" % err[:100])
    code, out, err = remote(addr, "bash -c 'cd ~/%s && . bin/lib.sh && load_env && need_python && echo \"PY=$PYTHON\"'" % NODE_DIR)
    py = [x[3:] for x in out.splitlines() if x.startswith("PY=")]
    if code != 0 or not py:
        raise RuntimeError("用不了包里带的 Python：%s" % (err or out)[:120])
    return py[0]


# ---- stopping the mock from the monitoring machine (design 2.3) ----
# Works whatever node_mock.sh the node has (v2.0's does not wait). The patterns are written as pd032_mock[.]py so that
# they never match the shell that runs this script, whose command line holds the script text.
STOP = r"""
A='pd032_mock[.]py run'
I='pd032_mock[.]py run-instance'
cnt() { pgrep -f "$1" 2>/dev/null | wc -l | tr -d ' '; }
n=$(cnt "$I")
pids=$(pgrep -f "$A" 2>/dev/null)
forced=0
if [ -n "$pids" ]; then
  kill -TERM $pids 2>/dev/null
  i=0
  while [ "$i" -lt 10 ] && [ -n "$(pgrep -f "$A" 2>/dev/null)" ]; do sleep 1; i=$((i + 1)); done
  left=$(pgrep -f "$A" 2>/dev/null)
  if [ -n "$left" ]; then
    forced=$(cnt "$I")
    kill -KILL $left 2>/dev/null
    sleep 2
  fi
fi
for p in $(pgrep -f "$A" 2>/dev/null); do
  printf 'left\t%s\t%s\t%s\n' "$p" "$(ps -o lstart= -p "$p" 2>/dev/null)" "$(ps -o user= -p "$p" 2>/dev/null)"
done
[ "$(cnt "$A")" = 0 ] && rm -f PIDFILE
printf 'stop\t%s\t%s\t%s\n' "$n" "$forced" "$(cnt "$A")"
"""
COUNT = "pgrep -f 'pd032_mock[.]py run-instance' 2>/dev/null | wc -l | tr -d ' '"


def stop_mock(addr):
    """SIGTERM to the mock's parent and every run-instance, a count each second for up to 10 s, SIGKILL to what is left,
    2 s, and a last count. Returns (ok, text, processes found, processes killed)."""
    code, out, err = remote(addr, STOP.replace("PIDFILE", sh_path(node_home() + "/run/mock.pid")), timeout=60)
    last = [x.split("\t") for x in out.splitlines() if x.startswith("stop\t")]
    if not last:
        return False, "SSH 连不上或停止脚本没有跑完（%s）" % (err or out or "退出码 %d" % code)[:100], 0, 0
    n, forced, left = (int(x) for x in last[-1][1:4])
    if left:
        rows = [x.split("\t") for x in out.splitlines() if x.startswith("left\t")]
        pids = " ".join(r[1] for r in rows)
        listed = "；".join("pid %s 启动 %s 用户 %s" % (r[1], r[2].strip(), r[3].strip()) for r in rows)
        return False, "还有 %d 个 mock 进程停不掉（%s）→ 在 %s 上执行 kill -9 %s；是别的用户的进程时执行 sudo kill -9 %s" % (
            left, listed, addr, pids, pids), n, forced
    if n == 0 and forced == 0:
        return True, "没有 mock 进程在运行", 0, 0
    if forced:
        return True, "已停止：%d 个进程，其中 %d 个强制结束（最后几秒的数据会少）" % (n, forced), n, forced
    return True, "已停止：%d 个进程，都正常退出" % n, n, forced


def stop(addr):
    ok_, text, _, _ = stop_mock(addr)
    return ok_, text


def mock_count(addr):
    """run-instance processes on the machine (the whole machine, design 2.3 known limit); None when SSH fails."""
    code, out, err = remote(addr, COUNT, timeout=30)
    try:
        return int(out.strip().splitlines()[-1]), ""
    except (ValueError, IndexError):
        return None, "SSH 连不上（%s）" % (err or out or "退出码 %d" % code)[:80]


def mock_rate_verdict(inv):
    n_d = len(inv["decode"]["api_servers"])
    return criteria.judge_mock_rate(ENV.get("MOCK_RATE"), n_d), n_d


def ship_mock(addr, info):
    code, out, err = remote(addr, 'mkdir -p ~/%s && tar xzf - -C ~/%s' % (NODE_DIR, NODE_DIR), stdin=mock_tar(info))
    return code == 0, "拷不过去：%s" % err[:100]


def start(addr, names):
    if MODE != "mock":
        return False, "NODE_MODE=%s：节点上跑的是真实 vLLM，mock 不启动" % MODE
    ok_, msg = push(addr)
    if not ok_:
        return False, msg
    ok_, text, _, _ = stop_mock(addr)
    if not ok_:
        return False, "停完还有进程，没有启动：%s" % text
    info, err = probe(addr)
    if info is None:
        return False, "SSH 连不上（%s）" % err[:80]
    ok_, msg = ship_mock(addr, info)
    if not ok_:
        return False, msg
    code, out, err = remote(addr, "cd ~/%s && bin/node_mock.sh start %s" % (NODE_DIR, shlex.quote(addr)))
    if code != 0:
        return False, (err or out).strip().splitlines()[-1][:160] if (err or out).strip() else "退出码 %d" % code
    found, err = mock_count(addr)
    verdict = criteria.judge_mock_processes(found if found is not None else -1, names)
    if verdict.level != "OK":
        return False, "启动以后%s" % verdict.text
    return True, (out.strip().splitlines()[-1][:160] if out.strip() else "mock 已启动") + "；" + verdict.text


def clean(addr):
    """mock: everything this pack put on the machine, logs included, the logs slowly (node_mock.sh purge).
    vllm: only the mock folders; the conf and log folders belong to vLLM, which keeps its log files open."""
    ok_, text, _, _ = stop_mock(addr)
    if not ok_:
        return False, "没有删：%s" % text
    home = sh_path(node_home())
    drop_home = '[ -f {h}/.vllm-timing-home ] && rm -rf {h}; rm -rf ~/{d}'.format(h=home, d=NODE_DIR)
    if MODE != "mock":
        code, out, err = remote(addr, drop_home + "; echo done")
        if code != 0:
            return False, (err or out)[:100]
        return True, ("%s；已删除 mock 的目录。节点文件和日志目录没有动：vllm 模式下日志由 vLLM 开着在写，删了以后新数据到不了看板，"
                      "要等它换文件或重启 vLLM 才恢复" % text)
    ok_, msg = ship_mock(addr, {"python": "not needed"})
    if not ok_:
        return False, msg
    script = "cd ~/{d} && bin/node_mock.sh purge && cd && {drop}; rm -rf {c}; rmdir {l} 2>/dev/null; echo done".format(
        d=NODE_DIR, drop=drop_home, c=sh_path(conf_dir()), l=sh_path(log_dir()))
    code, out, err = remote(addr, script, timeout=24 * 3600, progress=True)
    if code != 0 or not out.strip().endswith("done"):
        return False, "删除没有做完：%s" % ((err or out).strip().splitlines() or ["退出码 %d" % code])[-1][:160]
    took = [x for x in out.splitlines() if x.startswith("mock 的日志已删除")]
    return True, "%s；%s；节点文件和 mock 的目录已删除" % (text, took[-1].split("；")[0] if took else "mock 的日志已删除")


def mock_clean(addr):
    """Design 2.5, mock step 2: stop the mock and delete the logs it wrote (node_mock.sh clean); the node files stay."""
    if MODE != "mock":
        return False, "NODE_MODE=%s：节点上的日志属于 vLLM，不删" % MODE
    ok_, text, _, _ = stop_mock(addr)
    if not ok_:
        return False, "没有删：%s" % text
    # After bin/nodes.sh clean nothing is left: the mock folder is not shipped again just to find that out.
    code, out, err = remote(addr, "find %s -type f 2>/dev/null | head -n 1 | wc -l" % sh_path(log_dir()), timeout=30)
    if code == 0 and out.strip() == "0":
        return True, "%s；%s 里没有文件，不用删" % (text, log_dir())
    ok_, msg = ship_mock(addr, {"python": "not needed"})
    if not ok_:
        return False, msg
    code, out, err = remote(addr, "cd ~/%s && bin/node_mock.sh clean" % NODE_DIR, timeout=24 * 3600, progress=True)
    if code != 0:
        return False, "删除没有做完：%s" % ((err or out).strip().splitlines() or ["退出码 %d" % code])[-1][:160]
    return True, "%s；%s" % (text, (out.strip().splitlines() or [""])[-1][:160])


def status(addr, names):
    files = ('D=%s; L=%s; echo "节点文件 $(ls "$D" 2>/dev/null | tr "\\n" " ")；日志 $(find "$L" -name "*.log" 2>/dev/null | wc -l | tr -d " ") 个文件 '
             '$(du -sh "$L" 2>/dev/null | cut -f1)"') % (sh_path(conf_dir()), sh_path(log_dir()))
    code, out, err = remote(addr, "echo \"mock $(%s)\"; %s" % (COUNT, files), timeout=30)
    if code != 0:
        return False, "SSH 连不上（%s）" % err[:80]
    lines = out.strip().splitlines()
    rest = "；".join(lines[1:])
    if MODE != "mock":
        return True, rest
    try:
        found = int(lines[0].split()[1])
    except (IndexError, ValueError):
        return False, "数不出 mock 进程（%s）" % out[:80]
    verdict = criteria.judge_mock_processes(found, names)
    if verdict.level == "OK":
        text = verdict.text.replace("（%s）" % " ".join(names), "（%s）" % short_names(names))
    else:
        text = verdict.text.replace(" →", "（%s）→" % short_names(names), 1)
    return verdict.level == "OK", "%s；%s" % (text, rest)


def paths(inv):
    """The per-machine list. No SSH; also written to generated/node_paths.txt."""
    cin, lin = conf_in_container(), log_in_container()
    out = ["# 每台机器的目录和实例（从 inventory.json 算出，P0.. 按 prefill.api_servers 的顺序，D0.. 按 decode.api_servers 的顺序）",
           "# 运行环境 NODE_MODE=%s；节点文件 NODE_CONF_DIR=%s；日志 NODE_LOG_DIR=%s" % (MODE, conf_dir(), log_dir()), ""]
    # Each instance folder is named by its vllm serve --port: the filter takes it from the command line of each DP
    # rank, so one set of variables per machine serves every rank a launch script starts there.
    for host, names in instances(inv).items():
        out.append("%s" % host)
        out.append("  %s/  pd_diag_logging.json  pd_diag_logfilter.py  segments.json" % conf_dir())
        for name, port in names:
            roles = "api engine worker"
            files = "  ".join("%s.<启动时间>.<pid>.<序号>.log" % r for r in roles.split())
            out.append("  %s/%s/  %s    （%s，vllm serve --port %s）" % (log_dir(), port, files, name, port))
        if MODE == "vllm":
            # Appended to PYTHONPATH: the container may set it already (for example in ~/.bashrc).
            out.append("  这台机器一组：启动脚本里、vllm serve 之前加这两行，所有 rank 共用")
            out.append("    export VLLM_LOGGING_CONFIG_PATH=%s/pd_diag_logging.json" % cin)
            out.append("    export PYTHONPATH=%s${PYTHONPATH:+:$PYTHONPATH}" % cin)
            out.append("  容器加挂载：-v %s:%s:ro -v %s:%s（容器里已经能在同一路径看到这两个宿主机目录时，不用加）" % (conf_dir(), cin, log_dir(), lin))
        out.append("")
    if MODE == "vllm":
        # F2 and its addition: where the two exports go, and what NODE_CONF_DIR_IN_CONTAINER must be.
        if cin != conf_dir() or lin != log_dir():
            out.append("容器内路径与宿主机不同：三个文件 %s → %s，日志 %s → %s；export 写容器内的路径" % (conf_dir(), cin, log_dir(), lin))
        out.append("两行 export 放在容器里执行 vllm serve 的那个脚本里、vllm serve 之前（或用 docker run/exec -e 传进去）；写在宿主机的 shell 或别的脚本里不生效")
        out.append("VLLM_LOGGING_CONFIG_PATH 和 PYTHONPATH 都指向 NODE_CONF_DIR_IN_CONTAINER（%s）：它必须是 push 写入的目录在容器里看到的路径，三个文件放在一起" % cin)
        out.append("改完要重启 vLLM；重启以后用 bin/diag.sh vllm 确认")
        out.append("")
    text = "\n".join(out)
    (PACK / "generated").mkdir(exist_ok=True)
    (PACK / "generated" / "node_paths.txt").write_text(text + "\n", encoding="utf-8")
    print(text)
    n_p, n_d = len(inv["prefill"]["api_servers"]), len(inv["decode"]["api_servers"])
    print("共 %d 台机器、%d 个实例（P %d 个，D %d 个）；写进了 generated/node_paths.txt" % (len(instances(inv)), n_p + n_d, n_p, n_d))
    return 0


LEVEL = {True: "OK", False: "FAIL"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["check", "paths", "push", "selfcheck", "start", "stop", "clean", "status", "mock-clean"])
    ap.add_argument("addresses", nargs="*")
    args = ap.parse_args()
    inv = load_inventory(str(PACK / "inventory.json"))
    if args.command == "paths":
        return paths(inv)
    per_host = instances(inv)
    known = list(per_host)
    targets = args.addresses or known
    unknown = [a for a in targets if a not in known]
    if unknown:
        raise SystemExit("清单里没有 %s。清单里的 P、D 机器：%s" % (" ".join(unknown), " ".join(known)))
    if args.command == "start" and MODE == "mock":
        # Design 2.2: MOCK_RATE is the whole model's rate; above 1.5 a second per D nothing is started.
        verdict, n_d = mock_rate_verdict(inv)
        if verdict.level == "FAIL":
            print("FAIL %s，什么都没有改" % verdict.text)
            print("RESULT: FAIL（mock 速率超过保护线）")
            return 1
        ENV["MOCK_RATE"] = "%g" % criteria.effective_mock_rate(ENV.get("MOCK_RATE"), n_d)
        print("OK   %s" % verdict.text)
    results = {}

    def work(addr):
        names = [n for n, _ in per_host[addr]]
        with LIMIT:
            try:
                if args.command == "check":
                    results[addr] = check(addr, per_host[addr])
                elif args.command == "push":
                    results[addr] = push(addr)
                elif args.command == "selfcheck":
                    results[addr] = selfcheck(addr)
                elif args.command == "start":
                    results[addr] = start(addr, names)
                elif args.command == "stop":
                    results[addr] = stop(addr)
                elif args.command == "clean":
                    results[addr] = clean(addr)
                elif args.command == "mock-clean":
                    results[addr] = mock_clean(addr)
                else:
                    results[addr] = status(addr, names)
            except Exception as e:  # noqa: BLE001
                results[addr] = (False, str(e)[:160])

    threads = [threading.Thread(target=work, args=(a,)) for a in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    bad = warn = 0
    for addr in targets:
        got = results[addr]
        lines = got if isinstance(got, list) else [(LEVEL.get(got[0], got[0]), got[1])]
        levels = [lv for lv, _ in lines]
        bad += 1 if "FAIL" in levels else 0
        warn += 1 if "WARN" in levels and "FAIL" not in levels else 0
        for lv, text in lines:
            print("%-4s %s %s" % (lv, addr, text))
    if bad:
        print("RESULT: FAIL（%d 台里 %d 台没过）" % (len(targets), bad))
        return 1
    print("RESULT: OK（%d 台全过%s）" % (len(targets), "，其中 %d 台有 WARN" % warn if warn else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
