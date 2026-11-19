#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板二 v3.1-extra 更新：一个集群的面板二从 v3 升到 v3.1-extra，已是 v3.1-extra 时重跑一遍；rollback 回到 v3。
只动 --cluster 指定的那一个集群，其他集群留在原来的版本。v2.1 及更早的先用包二 v3 的更新脚本升到 v3。
在监控机上，按顺序：

    python3 vllm_pd_update_pack2_v31extra.py run --cluster 集群名 [--profile 段定义]    预检 → 备份 → 迁移 → 删掉这个集群面板二的数据 → 更新面板二
    python3 vllm_pd_update_pack2_v31extra.py rollback <备份目录> --cluster 集群名       删掉这个集群面板二的数据，恢复到 run 时备份的样子
    python3 vllm_pd_update_pack2_v31extra.py check --cluster 集群名 [--profile 段定义]  可选的核对，只读：预检、迁移预览、升级安全检查、比对；
                                                                                        它的结果不影响 run

--profile：用包里 profiles/<段定义>/segments.json 作这个集群的段定义（写进集群 deploy.env 的 SEGMENTS_PROFILE），
比如 dsv41。不写时沿用 deploy.env 里已有的 SEGMENTS_PROFILE；都没有时用包里的 segments.json，读同事现有日志时和 v3 相同。
proxy 和 Mooncake 两个来源默认不采；打开的方法见操作说明 B（改 deploy.env，bin/puller.sh restart）。

run 和 rollback 不看数据流（链路通不通、过滤器版本、有没有数据进来、mock 在不在跑）：现场的测试数据和日志流是什么样都照常往下做。
它们只查保护机器和别的服务的几项：两个包和 sha256、磁盘空间、集群目录、ClickHouse 容器、Grafana 账号和注册表；不碰面板一、
Prometheus、Grafana 容器和 ClickHouse 插件。这个集群在 ClickHouse 里的表和视图先列出行数再删掉，更新以后从空库开始。

--cluster 不写时是下面的 CLUSTER。一次只做一个集群，几个集群一个一个做。没有面板一的集群（比如只装了面板二的 test）也可以：
面板一那几项（面板一的 check）不做。每次的全部输出同时写进 LOG_DIR 下的日志文件（不含密码）。最后打印的「摘要」就是要发回的内容。
"""
import base64
import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request

# ==================== 现场路径（只改这一段）====================
USER_DIR = "/data/<用户目录>"
CLUSTER = "test"
MODEL_GROUP = "GLM-5.3"
ROOT = "/data/grafana"
P2_NEW = USER_DIR + "/vllm-pd-panel2-update-v3.1-extra"  # 面板二 v3.1-extra 的 tar 和 .sha256 所在目录
P2_OFFLINE = USER_DIR + "/vllm-pd-panel2-update-1112/vllm-pd-grafana-panel2-offline-aarch64.tar.gz"  # 包一用过的离线包，不变
OLD1 = USER_DIR + "/vllm-pd-grafana-panel1"             # 旧面板一的目录（停用与否都可以）
OLD2 = USER_DIR + "/vllm-pd-grafana-panel2/vllm-pd-grafana-panel2"  # 旧面板二的目录（取旧 Prometheus 容器名）
BACKUP_ROOT = os.path.expanduser("~/vllm-pd-update-backup")
LOG_DIR = os.path.expanduser("~/vllm-pd-update-logs")
# ==================== 这次的包（不用改）====================
PACK_VERSION = "v3.1-extra"                           # 面板二
PREV_VERSION = "v3"                                   # 升级的起点，也是 rollback 回到的版本
FROM_VERSIONS = (PREV_VERSION, PACK_VERSION)          # 从 v2.1 升级，或重跑 v3
P2_TAR = "vllm-pd-grafana-panel2.tar.gz"
P2_SHA = "<filled at packing>"                        # 打包时填：面板二 v3.1-extra tar 的 sha256
P2_OFFLINE_SHA = "14c00e3a3a4c73468336c7aa79b7d02fb5f428130495f3fd516a829d5e53b032"
OLD_CONFIG_ID = "pd032-2026-11-12"                    # 包一 segments.json 的 config_id（面板二 v1.0 没有 VERSION 文件，靠它认）
KEEP_HOURS_KEY = "NODE_LOG_KEEP_HOURS"                # v3 起节点日志保留时长在 deploy.env 里（已知问题第 14 条）
SHOW = 10                                             # 每个子检查的 FAIL、WARN、INFO 行，屏幕和摘要里最多这么多行（B2）
MIN_FREE_MB = 500                                     # 解开新包和离线包要的空间（实测约 390 MB）
ROLLBACK_MARK = "rollback-done.txt"                   # rollback 做完后写进所用的备份目录，每次一行时间
PROFILE = ""                                          # --profile：这次写进 deploy.env 的 SEGMENTS_PROFILE
PROFILE_KEY = "SEGMENTS_PROFILE"
EXTRA_MIRRORS = ("_proxy", "_mooncake", "_native")    # v3.1-extra 拉取程序在 PULL_DIR 下多写的三个目录，回到 v3 时删掉

NEW1 = "%s/vllm-pd-grafana-panel1/%s" % (ROOT, CLUSTER)
NEW2 = "%s/vllm-pd-grafana-panel2/%s" % (ROOT, CLUSTER)


def set_cluster(name):
    """--cluster: the cluster this run works on, and its panel 1 and panel 2 folders. The model folder comes from the
    cluster's registry entry when it has one (MODEL_GROUP otherwise)."""
    global CLUSTER, NEW1, NEW2, MODEL_GROUP
    CLUSTER = name
    NEW1 = "%s/vllm-pd-grafana-panel1/%s" % (ROOT, CLUSTER)
    NEW2 = "%s/vllm-pd-grafana-panel2/%s" % (ROOT, CLUSTER)
    try:
        MODEL_GROUP = (json.load(open(REGISTRY))["clusters"].get(CLUSTER) or {}).get("model_group") or MODEL_GROUP
    except (OSError, ValueError, KeyError):
        pass


def has_panel1():
    """Whether the cluster has a panel 1 folder (test, installed with panel 2 only, has none)."""
    return os.path.isfile(os.path.join(NEW1, "bin", "panel1_cluster.sh"))
P2_PACK = P2_NEW + "/pack/vllm-pd-grafana-panel2"
REGISTRY = os.environ.get("VLLM_PD_REGISTRY") or os.path.expanduser("~/.vllm-pd-clusters.json")

# ==================== 输出、日志 ====================
SECRETS = set()
LOG = None
SUMMARY = []


def mask(s):
    for x in sorted(SECRETS, key=len, reverse=True):
        if x and len(x) >= 4:
            s = s.replace(x, "***")
    return s


def say(msg=""):
    msg = mask(str(msg))
    print(msg, flush=True)
    if LOG:
        LOG.write(msg + "\n")
        LOG.flush()


def log_only(msg):
    if LOG:
        LOG.write(mask(str(msg)) + "\n")
        LOG.flush()


class Stop(Exception):
    pass


def sh(cmd, cwd=None, timeout=3600, show_tail=0, log_output=True):
    """Runs a shell command; its whole output goes to the log (unless log_output is false), the last show_tail lines also to the screen."""
    log_only("$ %s%s" % (cmd, "   (in %s)" % cwd if cwd else ""))
    p = subprocess.run(["bash", "-c", cmd], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    out = p.stdout.decode("utf-8", "replace")
    if log_output:
        log_only(out.rstrip())
    if show_tail:
        for line in out.rstrip().splitlines()[-show_tail:]:
            say("    " + line)
    return p.returncode, out


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ==================== 集群的值 ====================
def cluster_env():
    """The cluster's values as its own scripts see them (bin/lib.sh load_env in the panel 2 folder, names.py for panel 1)."""
    keys = ["GRAFANA_URL", "GRAFANA_USER", "GRAFANA_PASSWORD", "GRAFANA_BOARD_UID", "GRAFANA_DS_UID", "CLICKHOUSE_CONTAINER",
            "CENTRAL_CONTAINER", "CH_ADMIN_PASSWORD", "CH_READER_PASSWORD", "PROMETHEUS_CONTAINER", "PROMETHEUS_URL", "PROMETHEUS_YML",
            "PANEL1_P_UID", "PANEL1_D_UID", "TIMING_HOME", "NODE_MODE", "DOCKER_NETWORK", "GRAFANA_CONTAINER", "CH_WRITER_PASSWORD",
            "PULL_DIR", "PROXY_COLLECT", "MOONCAKE_LOG_COLLECT", "SEGMENTS_PROFILE"]
    # the output holds the passwords: it is not written to the log
    code, out = sh('. bin/lib.sh && load_env >/dev/null && for k in %s; do printf "%%s=%%s\\n" "$k" "${!k:-}"; done' % " ".join(keys), cwd=NEW2, log_output=False)
    if code != 0:
        raise Stop("读不了 %s/deploy.env（bin/lib.sh load_env 失败）" % NEW2)
    env = dict(l.split("=", 1) for l in out.splitlines() if "=" in l and l.split("=", 1)[0] in keys)
    code, out = sh("python3 scripts/names.py derive --panel panel1 --env-file deploy.env --format json", cwd=NEW1) if has_panel1() else (1, "")
    if code == 0:
        try:
            p1 = json.loads(out[out.index("{"):])
            env["PROMETHEUS_DS_UID"] = p1.get("PROMETHEUS_DS_UID", "")
            env["PROMETHEUS_VOLUME"] = p1.get("PROMETHEUS_VOLUME", "")
        except ValueError:
            pass
    for k in ("GRAFANA_PASSWORD", "CH_ADMIN_PASSWORD", "CH_READER_PASSWORD", "CH_WRITER_PASSWORD"):
        SECRETS.add(env.get(k, ""))
    return env


def grafana(env, method, path, body=None):
    auth = base64.b64encode(("%s:%s" % (env["GRAFANA_USER"], env["GRAFANA_PASSWORD"])).encode()).decode()
    req = urllib.request.Request(env["GRAFANA_URL"].rstrip("/") + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else {}
        except ValueError:
            return e.code, {"message": raw[:200].decode("utf-8", "replace")}
    except (urllib.error.URLError, OSError) as e:
        return 0, {"message": str(e)}


def ch(env, sql, user="default"):
    pw = env["CH_ADMIN_PASSWORD"] if user == "default" else env["CH_READER_PASSWORD"]
    p = subprocess.run(["docker", "exec", "-i", env["CLICKHOUSE_CONTAINER"], "clickhouse-client", "--user", user, "--password", pw, "--format", "TSV"],
                       input=sql.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
    return p.returncode, p.stdout.decode("utf-8", "replace").strip(), mask(p.stderr.decode("utf-8", "replace").strip())


def cluster_boards(env):
    """The cluster's dashboards: the panel 1 and panel 2 UIDs, and every dashboard in the model folder whose UID ends with the cluster."""
    uids = [env.get("PANEL1_P_UID"), env.get("PANEL1_D_UID"), env.get("GRAFANA_BOARD_UID")]
    code, found = grafana(env, "GET", "/api/search?type=dash-db")
    for d in found if code == 200 else []:
        if d.get("folderTitle") == MODEL_GROUP and d["uid"].endswith("-" + CLUSTER) and d["uid"] not in uids:
            uids.append(d["uid"])
    return [u for u in uids if u]


def board_hash(d):
    d = dict(d)
    for k in ("id", "version", "iteration"):
        d.pop(k, None)
    return hashlib.sha256(json.dumps(d, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def ds_hash(d):
    d = {k: v for k, v in d.items() if k not in ("version", "id", "readOnly")}
    return hashlib.sha256(json.dumps(d, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def profile_elements(env):
    code, out, err = ch(env, "SELECT setting_name, value FROM system.settings_profile_elements WHERE profile_name = 'vllm_reader_profile' AND setting_name != '' ORDER BY setting_name")
    return dict(l.split("\t", 1) for l in out.splitlines() if "\t" in l) if code == 0 else None


def snapshot(env):
    """What must not change: the cluster's containers, volumes and registry entry; old panel 1 as it is now."""
    s = {"containers": {}, "volumes": [], "registry": None, "old1": {}}
    for c in (env.get("CLICKHOUSE_CONTAINER"), env.get("PROMETHEUS_CONTAINER"), env.get("CENTRAL_CONTAINER")):
        code, out = sh("docker inspect %s" % c)
        if code == 0:
            i = json.loads(out)[0]
            s["containers"][c] = {"id": i["Id"][:12], "image": i["Config"]["Image"], "running": i["State"]["Running"],
                                  "mounts": sorted("%s:%s" % (m.get("Name") or m.get("Source"), m["Destination"]) for m in i["Mounts"]),
                                  "ports": json.dumps(i["HostConfig"].get("PortBindings") or {}, sort_keys=True)}
    code, out = sh("docker volume ls -q")
    s["volumes"] = sorted(v for v in out.split() if v.endswith("-" + CLUSTER) or ("-" + CLUSTER + "-") in v)
    try:
        e = json.load(open(REGISTRY))["clusters"][CLUSTER]
        s["registry"] = {p: {k: v for k, v in (e.get(p) or {}).items() if k != "registered"} for p in ("panel1", "panel2")}
        s["registry"].update(model_group=e.get("model_group"), port_base=e.get("port_base"))
    except (OSError, ValueError, KeyError):
        pass
    o = s["old1"]
    if os.path.isdir(OLD1):
        o["files"] = {}
        for dp, dn, fn in os.walk(OLD1):
            for f in fn:
                p = os.path.join(dp, f)
                o["files"][os.path.relpath(p, OLD1)] = sha256(p)
    pc = ""
    if os.path.isfile(OLD2 + "/deploy.env"):
        code, out = sh('set -a; . ./deploy.env >/dev/null 2>&1; printf %s "$PROMETHEUS_CONTAINER"', cwd=OLD2)
        pc = out.strip()
    if pc and pc != env.get("PROMETHEUS_CONTAINER"):
        code, out = sh("docker inspect -f '{{.Id}} {{.State.Running}}' %s" % pc)
        o["prometheus"] = out.strip()[:20] if code == 0 else "没有"
    code, found = grafana(env, "GET", "/api/search?type=dash-db")
    o["boards"] = {}
    for d in found if code == 200 else []:
        if d.get("folderTitle") == "vLLM P/D":
            c2, b = grafana(env, "GET", "/api/dashboards/uid/" + d["uid"])
            o["boards"][d["uid"]] = board_hash(b["dashboard"]) if c2 == 200 else None
    c2, ds = grafana(env, "GET", "/api/datasources/uid/prometheus")
    o["datasource prometheus"] = ds_hash(ds) if c2 == 200 else "没有"
    code, out = sh("docker inspect -f '{{.Id}} {{.State.StartedAt}}' %s" % env.get("GRAFANA_CONTAINER", ""))
    s["grafana"] = out.strip()
    return s


# ==================== 子检查的输出（B2）====================
LEVELS = ("FAIL", "WARN", "INFO", "OK")
FINDING = re.compile(r"^(OK|INFO|WARN|FAIL) ")


def findings(out):
    """A sub-check's finding lines as (level, line), each line as it printed it ("OK   ", "INFO ", "WARN ", "FAIL " and
    the text; an "错误：" line from its fail() counts as FAIL), and its last "RESULT:" line."""
    found, result = [], ""
    for line in out.splitlines():
        m = FINDING.match(line)
        if m:
            found.append((m.group(1), line.rstrip()))
        elif line.startswith("错误："):
            found.append(("FAIL", line.rstrip()))
        elif line.startswith("RESULT:"):
            result = line.strip()
    return found, result


def result_line(found):
    n = {lv: sum(1 for x, _ in found if x == lv) for lv in ("FAIL", "WARN")}
    return "RESULT: " + ("FAIL（%d 项）" % n["FAIL"] if n["FAIL"] else "WARN（%d 项）" % n["WARN"] if n["WARN"] else "OK")


def show(title, result, found, with_ok=False):
    """B2: a sub-check's result and its FAIL, WARN and INFO lines verbatim (OK lines too with with_ok), FAIL first, at most
    SHOW of them, then 「另有 n 行见日志」. Printed to the screen; the lines are returned for the summary."""
    picked = sorted((x for x in found if with_ok or x[0] != "OK"), key=lambda x: LEVELS.index(x[0]))
    out = ["%s：%s" % (title, result)] + [l for _, l in picked[:SHOW]]
    if len(picked) > SHOW:
        out.append("另有 %d 行见日志" % (len(picked) - SHOW))
    for line in out:
        say(line)
    for _, line in picked[SHOW:]:
        log_only(line)
    return out


def safety(cmd, cwd):
    """An upgrade-safety check (bin/panel2_cluster.sh check …, design 2.1); its whole output goes to the log.
    Returns (level, RESULT line, findings); a check that exits non-zero without a FAIL line gets one."""
    code, out = sh("%s 2>&1" % cmd, cwd=cwd)
    found, result = findings(out)
    if code != 0 or result.startswith("RESULT: FAIL"):
        level = "FAIL"
        if not any(lv == "FAIL" for lv, _ in found):
            tail = [l for l in out.splitlines() if l.strip()]
            found.append(("FAIL", "FAIL 升级安全检查没跑完（退出码 %d）：%s" % (code, tail[-1][:200] if tail else "没有输出")))
    elif result.startswith("RESULT: WARN"):
        level = "WARN"
    elif result.startswith("RESULT: OK"):
        level = "OK"
    else:
        level = "WARN"
        found.append(("WARN", "WARN 升级安全检查没有 RESULT 行，看日志"))
    return level, result or "（没有 RESULT 行，退出码 %d）" % code, found


# ==================== 版本 ====================
def installed_version(folder):
    """v2.0 and later write a VERSION file; v1.0 has none, its segments.json config_id tells it apart."""
    p = os.path.join(folder, "VERSION")
    if os.path.isfile(p):
        return open(p).read().strip()
    try:
        cid = json.load(open(os.path.join(folder, "segments.json"))).get("config_id")
    except (OSError, ValueError):
        return "?"
    return "v1.0" if cid == OLD_CONFIG_ID else "?（config_id %s）" % cid


def node_config_versions(env):
    """The segments.json version of every filter process running now (its latest self-check line within 2 minutes; the
    filter writes one a minute, so processes stopped by the update drop out 2 minutes after)."""
    code, out, err = ch(env, "SELECT arrayStringConcat(arraySort(groupUniqArray(v)), ' ') FROM (SELECT argMax(config_version, ts) AS v "
                             "FROM vllm_pd.filter_selfcheck WHERE ts >= now64(3) - INTERVAL 10 MINUTE GROUP BY node, instance, pid "
                             "HAVING max(ts) >= now64(3) - INTERVAL 2 MINUTE)")
    return out.split() if code == 0 else None


# ==================== 新包 ====================
def verify_tars(item):
    """The new pack's tar and the offline pack, by sha256. Problems here stop everything."""
    if not re.fullmatch(r"[0-9a-f]{64}", P2_SHA):
        raise Stop("这份更新脚本还没有填新包的 sha256（P2_SHA），不是交付的那一份")
    p = os.path.join(P2_NEW, P2_TAR)
    if not os.path.isfile(p):
        raise Stop("新包 %s 不存在：把包二 %s 的面板二 tar 和 .sha256 放进 %s" % (p, PACK_VERSION, P2_NEW))
    got = sha256(p)
    if got != P2_SHA:
        raise Stop("新包 %s 的 sha256 不对：%s…，应为 %s…（拷错了或没拷完）" % (p, got[:16], P2_SHA[:16]))
    item("OK", "新包 %s 的 sha256 正确" % p)
    if not os.path.isfile(P2_OFFLINE):
        raise Stop("面板二离线包 %s 不存在" % P2_OFFLINE)
    if sha256(P2_OFFLINE) != P2_OFFLINE_SHA:
        raise Stop("面板二离线包 %s 的 sha256 不对" % P2_OFFLINE)
    item("OK", "面板二离线包 %s 的 sha256 正确" % P2_OFFLINE)


def extract_pack(item):
    """The new pack plus the offline pack into P2_NEW/pack (a scratch folder, not the cluster's), checked by their SHA256SUMS."""
    code, out = sh("rm -rf pack && mkdir pack && tar xzf %s -C pack && tar xzf %s -C pack/vllm-pd-grafana-panel2 && cd pack/vllm-pd-grafana-panel2 "
                   "&& sha256sum -c SHA256SUMS --quiet && cd offline && sha256sum -c SHA256SUMS --quiet" % (P2_TAR, P2_OFFLINE), cwd=P2_NEW)
    if code != 0:
        raise Stop("面板二新包解不开或校验不过，看日志")
    v = installed_version(P2_PACK)
    if v != PACK_VERSION:
        raise Stop("解开的新包是 %s，不是 %s" % (v, PACK_VERSION))
    item("OK", "新包解开到 %s，文件和 SHA256SUMS 相同" % P2_PACK)


def load_criteria(pack):
    """The new pack's judgement functions (scripts/criteria.py), the same ones its own check uses."""
    d = os.path.join(pack, "scripts")
    if not os.path.isfile(os.path.join(d, "criteria.py")):
        raise Stop("新包里没有 scripts/criteria.py（%s）" % d)
    sys.dont_write_bytecode = True
    if d not in sys.path:
        sys.path.insert(0, d)
    import criteria  # noqa: E402
    return criteria


# ==================== 迁移（设计 1.2）====================
PLAIN = re.compile(r"(\$HOME/|\$\{HOME\}/)?[A-Za-z0-9_./:@,+-]*")


def env_file_value(text, key):
    """KEY's value as deploy.env sets it: its last KEY= line, quotes removed, $HOME left as written; "" when not set or
    blank (bin/lib.sh treats a blank value as not set)."""
    val = ""
    for line in text.splitlines():
        if line.startswith(key + "="):
            raw = line[len(key) + 1:]
            try:
                parts = shlex.split(raw, comments=True)
                val = parts[0] if parts else ""
            except ValueError:
                val = raw.strip().strip("'\"")
    return val


def append_env(path, pairs):
    """Appends KEY=value lines for the keys deploy.env does not set yet (bash and bin/lib.sh take the last line of a key);
    no other line is touched, the file keeps its inode and mode. Returns the (key, value) written; a second call with the
    same pairs writes nothing."""
    text = open(path, encoding="utf-8").read()
    todo = [(k, v) for k, v in pairs if not env_file_value(text, k)]
    if todo:
        body = "\n".join("%s=%s" % (k, v if PLAIN.fullmatch(v) else shlex.quote(v)) for k, v in todo)
        with open(path, "a", encoding="utf-8") as f:
            f.write(("" if text.endswith("\n") else "\n") + "\n# ---- 包二 %s 升级时写入（vllm_pd_update_pack2.py）：%s 下的实际值 ----\n" % (
                PACK_VERSION, PREV_VERSION) + body + "\n")
    return todo


def js(v):
    return "（没有）" if v is None else json.dumps(v, ensure_ascii=False)


def migration_preview(installed, pack, version, mode, node_versions, scratch):
    """Worked out before anything changes, read-only (it writes only scratch/segments.json):
    (a) known issue 14: a line.keep_hours of segments.json that differs from the pack's (changed by hand under v2.1)
        moves into deploy.env as NODE_LOG_KEEP_HOURS, since v3 writes the value from there; a deploy.env that already
        sets the key (a re-run of v3) is left as it is;
    (b) other segments.json keys that differ from the pack's are listed, they become the pack's;
    (d) files of the folder that differ from its SHA256SUMS (and that the new pack has) are listed, they get replaced;
    (e) the segments.json version the nodes will get against the one they report now, and whether the filter program or
        its logging config changes (only that needs a vLLM restart).
    installed: the cluster folder; pack: the new pack's folder; version: the installed VERSION; mode: NODE_MODE;
    node_versions: node_config_versions() ([] none in the last 2 minutes, None unknown).
    Returns {"lines": [(level, text)], "migrate": [(key, value, text for the run summary)], "node": text, "restart": text,
    "cid": (config_id now, config_id after)}."""
    C = load_criteria(pack)
    lines, migrate = [], []

    def rd(*p):
        return open(os.path.join(*p), encoding="utf-8").read()

    env_text = rd(installed, "deploy.env")
    prof = PROFILE or env_file_value(env_text, PROFILE_KEY)
    if prof and not os.path.isfile(os.path.join(pack, "profiles", prof, "segments.json")):
        raise ValueError("新包里没有 profiles/%s/segments.json（--profile 或 deploy.env 的 %s）" % (prof, PROFILE_KEY))
    inst_text, pack_text = rd(installed, "segments.json"), rd(pack, *(("profiles", prof, "segments.json") if prof else ("segments.json",)))
    inst, packed = json.loads(inst_text), json.loads(pack_text)
    inst_log = (inst.get("line") or {}).get("log_dir", "")
    log_set = env_file_value(env_text, "NODE_LOG_DIR_IN_CONTAINER")
    # (a)
    inst_keep = (inst.get("line") or {}).get("keep_hours", C.PACK_KEEP_HOURS)
    keep_set = env_file_value(env_text, KEEP_HOURS_KEY)
    if keep_set:
        target_keep = C.keep_hours_value(keep_set)
    elif float(inst_keep) != float(C.PACK_KEEP_HOURS):
        value = C.hours_text(C.keep_hours_value(inst_keep))
        lines.append(("INFO", "将迁移：segments.json 的 keep_hours=%s → deploy.env 的 %s（%s 下改过的节点日志保留时长）" % (value, KEEP_HOURS_KEY, version)))
        migrate.append((KEEP_HOURS_KEY, value, "已迁移：segments.json 的 keep_hours=%s → deploy.env 的 %s" % (value, KEEP_HOURS_KEY)))
        target_keep = float(value)
    else:
        target_keep = float(C.PACK_KEEP_HOURS)
    if float(inst_keep) != target_keep:
        lines.append(("WARN", "升级后会变成 deploy.env 的值：line.keep_hours = %s → %s" % (js(inst_keep), C.hours_text(target_keep))))
    # The log_dir the upgrade writes into segments.json: scripts/generate.py takes NODE_LOG_DIR_IN_CONTAINER after
    # bin/lib.sh's defaults (vllm: NODE_LOG_DIR as written; mock: blank, the pack's value stays).
    pack_log = (packed.get("line") or {}).get("log_dir", "")
    if log_set:
        target, src = log_set, "deploy.env 的值"
    elif mode == "vllm":
        target, src = env_file_value(env_text, "NODE_LOG_DIR") or "$HOME/vllm-pd-diag/logs", "NODE_LOG_DIR 的值"
    else:
        target, src = pack_log, "包里的值"
    if inst_log != target:
        lines.append(("WARN", "升级后会变成%s：line.log_dir = %s → %s" % (src, inst_log, target)))
    # (b) with a profile the whole definition changes: one line instead of every key
    if prof:
        lines.append(("INFO", "段定义换成新包的 profiles/%s/segments.json（config_id %s → %s）" % (prof, inst.get("config_id"), packed.get("config_id"))))
        if env_file_value(env_text, PROFILE_KEY) != prof:
            migrate.append((PROFILE_KEY, prof, "已写入：deploy.env 的 %s=%s" % (PROFILE_KEY, prof)))
    else:
        for key, a, b in C.segments_key_diff(inst, packed, ignore=("line.log_dir", "line.keep_hours")):
            lines.append(("WARN", "升级后会变成包里的值：%s = %s → %s" % (key, js(a), js(b))))
    # (d) deploy.env, inventory.json, generated/ and offline/ are kept by add; segments.json is (a) and (b).
    sums = os.path.join(installed, "SHA256SUMS")
    if not os.path.isfile(sums):
        lines.append(("WARN", "集群目录里没有 SHA256SUMS，比不了哪些文件改过"))
    else:
        changed = []
        for line in open(sums, encoding="utf-8"):
            parts = line.split()
            if len(parts) != 2:
                continue
            name = parts[1].lstrip("*")
            name = name[2:] if name.startswith("./") else name
            if name in ("deploy.env", "inventory.json", "segments.json") or name.startswith(("generated/", "offline/")):
                continue
            if not os.path.isfile(os.path.join(pack, name)):
                continue    # not in the new pack: the upgrade leaves it as it is
            p = os.path.join(installed, name)
            if not os.path.isfile(p):
                changed.append(name + "（缺）")
            elif C.file_sha256(p) != parts[0]:
                changed.append(name)
        if changed:
            lines.append(("WARN", "这些文件改过，升级会换成包里的：%s%s" % (
                " ".join(changed[:12]), "…（共 %d 个）" % len(changed) if len(changed) > 12 else "")))
    # (e) the segments.json v3 generates: the pack's bytes with line.log_dir and line.keep_hours replaced
    os.makedirs(scratch, exist_ok=True)
    new_path = os.path.join(scratch, "segments.json")
    new_text = C.set_keep_hours(C.set_log_dir(pack_text, target), target_keep)
    with open(new_path, "w", encoding="utf-8") as f:
        f.write(new_text)
    new_ver = C.segments_version(new_path)
    cur_ver = C.segments_version(os.path.join(installed, "segments.json"))
    new_cid, cur_cid = json.loads(new_text).get("config_id"), inst.get("config_id")
    cid = "，看板读的 config_id 不变" if new_cid == cur_cid else "；config_id 变了：%s → %s" % (cur_cid, new_cid)
    if node_versions:
        now = " ".join(sorted(set(node_versions)))
        if set(node_versions) == {new_ver}:
            node = "节点文件不变：segments.json 是 %s" % new_ver
        else:
            node = "segments.json 会更新（%s → %s），过滤器自己重读%s" % (now, new_ver, cid)
    else:
        why = "节点上最近 2 分钟没有自检行" if node_versions == [] else "读不出节点上的版本"
        if cur_ver == new_ver:
            node = "节点文件不变：segments.json 是 %s（按集群目录；%s）" % (new_ver, why)
        else:
            node = "segments.json 会更新（%s → %s），过滤器自己重读%s（按集群目录；%s）" % (cur_ver, new_ver, cid, why)
    lines.append(("INFO", node))
    diff = []
    for f in C.FILTER_FILES:
        a, b = os.path.join(installed, "node", f), os.path.join(pack, "node", f)
        if not (os.path.isfile(a) and os.path.isfile(b)) or C.file_sha256(a) != C.file_sha256(b):
            diff.append(f)
    if not diff:
        restart = "过滤器程序和日志配置（%s）和现在的相同，不用重启 vLLM" % "、".join(C.FILTER_FILES)
        lines.append(("INFO", restart))
    elif mode == "vllm":
        restart = "要重启 vLLM：过滤器文件变了（%s），升级后各节点的 vLLM 重启了才用上新的" % "、".join(diff)
        lines.append(("WARN", restart))
    else:
        restart = "过滤器文件变了（%s）；mock 模式下 add 会重启 mock，用上新的" % "、".join(diff)
        lines.append(("INFO", restart))
    return {"lines": lines, "migrate": migrate, "node": node, "restart": restart, "cid": (cur_cid, new_cid)}


def migrate(plan):
    """Run step 3: the keys of (a) appended to the cluster's deploy.env (after the backup, before add)."""
    say("== 迁移（写进 %s/deploy.env，别的行不动）" % NEW2)
    path = os.path.join(NEW2, "deploy.env")
    added = append_env(path, [(k, v) for k, v, _ in plan["migrate"] if k != PROFILE_KEY])
    for k, v, _ in plan["migrate"]:
        if k == PROFILE_KEY and env_file_value(open(path, encoding="utf-8").read(), k) != v:
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n# ---- 面板二 %s 升级时写入（--profile）----\n%s=%s\n" % (PACK_VERSION, k, v))
            added.append((k, v))
    done = []
    for k, v, text in plan["migrate"]:
        if (k, v) in added:
            say("  · " + text)
            done.append(text)
        else:
            say("  · deploy.env 里已经有 %s，不再写" % k)
    if not plan["migrate"]:
        say("  · 没有要写的")
    return done


# ==================== 比对（设计 2.13）====================
def compare(plan):
    """bin/compare.sh all of the new pack: what the upgrade would push against what the site uses now, one line per item.
    It runs in the scratch extraction with a copy of the cluster's deploy.env (plus the keys the migration will add) and
    inventory.json; both copies are removed right after (deploy.env holds passwords). Never FAILs the check."""
    if not os.path.isfile(os.path.join(P2_PACK, "bin", "compare.sh")):
        return "（没有比对）", [("WARN", "WARN 升级前比对：新包里没有 bin/compare.sh，没有比对；run 时 add 每一项照做")]
    env_copy, inv_copy = os.path.join(P2_PACK, "deploy.env"), os.path.join(P2_PACK, "inventory.json")
    try:
        fd = os.open(env_copy, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.chmod(env_copy, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(open(os.path.join(NEW2, "deploy.env"), encoding="utf-8").read())
        append_env(env_copy, [(k, v) for k, v, _ in plan["migrate"]])
        shutil.copyfile(os.path.join(NEW2, "inventory.json"), inv_copy)
        # VLLM_PD_TARGET_DIR: the collector's mount is the installed folder's, not this extraction's
        code, out = sh("VLLM_PD_TARGET_DIR=%s bin/compare.sh all 2>&1" % shlex.quote(NEW2), cwd=P2_PACK)
    finally:
        for f in (env_copy, inv_copy):
            if os.path.lexists(f):
                os.remove(f)
    found, result = findings(out)
    # its own errors count as WARN: the comparison informs, it does not gate the upgrade
    found = [("WARN" if lv == "FAIL" else lv, l) for lv, l in found]
    if code not in (0, 10, 2):
        tail = [l for l in out.splitlines() if l.strip()]
        found.append(("WARN", "WARN 升级前比对没跑完（退出码 %d）：%s" % (code, tail[-1][:200] if tail else "没有输出")))
    return result or "（没有 RESULT 行，退出码 %d）" % code, found


# ==================== 检查 ====================
def free_mb(path):
    """Free space in MiB on the file system of path (its nearest existing parent)."""
    d = path
    while d and not os.path.exists(d) and d != "/":
        d = os.path.dirname(d)
    st = os.statvfs(d or "/")
    return st.f_bavail * st.f_frsize / 1048576


def container_running(name):
    code, out = sh("docker inspect --format '{{.State.Running}}' %s" % shlex.quote(name or "-"))
    return code == 0 and out.strip() == "true"


def check(quiet=False, full=True):
    """Read-only. Returns (ok, env, plan); ok is False when one of the items that protect the machine and the other
    services FAILs (the packs and their sha256, the disk, the cluster folder, the ClickHouse container, the Grafana
    account, the registry); run stops there. What cannot be passed at all (the installed version, the migration
    preview) raises Stop.
    full=False (run): only those items and the migration preview, no data-flow check. full=True (the check command): the
    new pack's upgrade-safety check and the comparison too; their FAILs show in check's own RESULT, never in run."""
    own = []

    def item(level, text):
        own.append((level, text))
        say("%-4s %s" % (level, text))

    say("== %s（不改任何东西）" % ("检查" if full else "预检：只查保护机器和别的服务的几项，不看数据流"))
    item("INFO", "包二 %s：面板二 tar %s…" % (PACK_VERSION, P2_SHA[:16]))
    verify_tars(item)
    mb = free_mb(P2_NEW)
    if mb < MIN_FREE_MB:
        raise Stop("%s 所在盘只剩 %.0f MB，解开新包和离线包要 %d MB：腾出空间再执行" % (P2_NEW, mb, MIN_FREE_MB))
    item("OK", "%s 所在盘空闲 %.0f MB（要 %d MB）" % (P2_NEW, mb, MIN_FREE_MB))
    for f in ("deploy.env", "inventory.json", "segments.json", "bin/panel2_cluster.sh"):
        if not os.path.isfile(os.path.join(NEW2, f)):
            raise Stop("集群目录 %s 里没有 %s" % (NEW2, f))
    ver = installed_version(NEW2)
    if ver in ("v1.0", "v2.0", "v2.1"):
        raise Stop("集群 %s 的面板二是 %s：先用包二 %s 的更新脚本升到 %s，再用这个脚本" % (CLUSTER, ver, PREV_VERSION, PREV_VERSION))
    if ver not in FROM_VERSIONS:
        raise Stop("集群 %s 的面板二是 %s：这个脚本只从 %s 升到 %s，或重跑 %s" % (CLUSTER, ver, PREV_VERSION, PACK_VERSION, PACK_VERSION))
    item("OK", "集群 %s 的面板二现在是 %s%s" % (CLUSTER, ver, "（重跑 %s）" % ver if ver == PACK_VERSION else ""))
    say("  · 解开面板二新包到临时目录 %s，离线包用 %s" % (P2_PACK, P2_OFFLINE))
    extract_pack(item)
    env = cluster_env()
    mode = env.get("NODE_MODE") or "mock"
    item("INFO", "节点模式 %s：%s" % (mode, "更新会重启各节点的 mock" if mode == "mock" else "vLLM 要不要重启，见下面「过滤器程序和日志配置」一行"))
    cc = env.get("CLICKHOUSE_CONTAINER")
    up = container_running(cc)
    item("OK" if up else "FAIL", "ClickHouse 容器 %s %s" % (cc, "在运行" if up else "没在运行 → bin/clickhouse_up.sh"))
    try:
        e = json.load(open(REGISTRY))["clusters"].get(CLUSTER) or {}
        d2 = (e.get("panel2") or {}).get("dir")
        item("OK" if d2 == NEW2 else "FAIL", "注册表里集群 %s 的面板二%s" % (CLUSTER, "已登记，目录正确" if d2 == NEW2 else "不对：%s" % d2))
    except (OSError, ValueError) as ex:
        item("FAIL", "读不了注册表 %s：%s" % (REGISTRY, ex))
    code, user = grafana(env, "GET", "/api/user")
    grafana_ok = code == 200
    if not grafana_ok:
        item("FAIL", "Grafana 账号 %s 登录不了（HTTP %s：%s）" % (env.get("GRAFANA_USER"), code, (user.get("message") or "")[:80]))
    else:
        c2, orgs = grafana(env, "GET", "/api/user/orgs")
        role = next((o.get("role") for o in orgs if o.get("orgId") == user.get("orgId", 1)), "") if c2 == 200 else ""
        item("OK" if role == "Admin" else "FAIL", "Grafana 账号 %s 能登录，组织角色 %s%s" % (env["GRAFANA_USER"], role or "?", "" if role == "Admin" else "（写看板和数据源要 Admin）"))
        c3, b = grafana(env, "GET", "/api/dashboards/uid/" + env["GRAFANA_BOARD_UID"])
        item("OK" if c3 == 200 else "FAIL", "看板 %s %s" % (env["GRAFANA_BOARD_UID"], "在" if c3 == 200 else "不在（HTTP %s）" % c3))
        c3, d = grafana(env, "GET", "/api/datasources/uid/%s" % env.get("GRAFANA_DS_UID"))
        item("OK" if c3 == 200 else "FAIL", "数据源 %s %s" % (env.get("GRAFANA_DS_UID"), "在" if c3 == 200 else "不在（HTTP %s）" % c3))
    prof = profile_elements(env)
    item("OK" if prof is not None else "FAIL", "ClickHouse 能连上，只读用户的 profile 能读出%s" % ("（%d 项）" % len(prof) if prof is not None else ""))
    vers = node_config_versions(env)
    item("INFO", "节点上的过滤器用的 segments.json：%s" % (" ".join(vers) if vers else "最近 2 分钟没有自检行" if vers == [] else "读不出来"))

    blocks = []
    say("== 迁移预览（新包的 scripts/criteria.py 算，只读）")
    try:
        plan = migration_preview(NEW2, P2_PACK, ver, mode, vers, os.path.join(P2_NEW, "preview"))
    except (OSError, ValueError, KeyError) as ex:
        raise Stop("迁移预览算不出来：%s。不升级（升级会把现场改过的值换成包里的）" % ex)
    found = [(lv, "%-4s %s" % (lv, t)) for lv, t in plan["lines"]]
    blocks.append((found, show("迁移预览", result_line(found), found)))
    if not full:
        pass
    elif not grafana_ok:
        # Grafana locks an account for a few minutes after several failed logins in a row: nothing more that logs in
        item("INFO", "Grafana 账号不能用，不再跑新包的升级安全检查和比对（它们也登录 Grafana；连续几次登录失败，Grafana 会把这个账号锁几分钟）")
    else:
        say("== 升级安全检查（新包的 bin/panel2_cluster.sh check --pre-upgrade，对着集群目录 %s，只读）" % NEW2)
        level, result, found = safety("bin/panel2_cluster.sh check %s --dir %s --pre-upgrade" % (CLUSTER, shlex.quote(NEW2)), P2_PACK)
        blocks.append((found, show("升级安全检查（升级前）", result, found)))
        say("== 升级前比对（新包的 bin/compare.sh all，只读）")
        result, found = compare(plan)
        blocks.append((found, show("升级前比对", result, found)))

    fails = ["%-4s %s" % (lv, t) for lv, t in own if lv == "FAIL"] + [l for found, _ in blocks for lv, l in found if lv == "FAIL"]
    n_warn = sum(1 for lv, _ in own if lv == "WARN") + sum(1 for found, _ in blocks for lv, _ in found if lv == "WARN")
    say("RESULT: %s" % ("FAIL（%d 项）" % len(fails) if fails else "WARN（%d 项）" % n_warn if n_warn else "OK"))
    if not quiet:
        own_fails = [lv for lv, _ in own if lv == "FAIL"]
        SUMMARY.append("check：%s（面板二 %s，%s 模式%s）" % (
            "%d 项 FAIL，其中保护机器和别的服务的 %d 项会让 run 停在预检；其余（升级安全检查、比对）不影响 run" % (len(fails), len(own_fails))
            if fails else "通过", ver, mode, "；WARN %d 项" % n_warn if n_warn else ""))
        own_bad = ["%-4s %s" % (lv, t) for lv, t in own if lv in ("FAIL", "WARN")]
        SUMMARY.extend(own_bad[:SHOW])
        for _, out in blocks:
            SUMMARY.extend(out)
    own_fails = ["%-4s %s" % (lv, t) for lv, t in own if lv == "FAIL"]
    plan.update(version=ver, mode=mode, fails=own_fails, warns=n_warn)
    return (not fails) if full else (not own_fails), env, plan


# ==================== 删数据 ====================
# The filters' own records, kept as bin/clear_data.sh keeps them: a filter writes a file header (filter_file) only when
# it opens a file, and an update restarts no vLLM, so dropped headers would not come back and the checks would report
# missing files for good.
KEEP_TABLES = ("filter_file", "filter_other", "filter_selfcheck")


def drop_data(env, why):
    """This cluster's panel 2 data: the tables and views of the database vllm_pd in the cluster's own ClickHouse,
    listed with their rows, then dropped (views first); the filters' own records (KEEP_TABLES) are listed and kept.
    add, or on rollback the earlier version's apply_segments.sh, creates the dropped ones again, empty. The ClickHouse
    container, its users and settings profile, the plugin, panel 1, Prometheus and Grafana are not touched. Returns
    the summary line."""
    code, out, err = ch(env, "SELECT name, engine, total_rows FROM system.tables WHERE database = 'vllm_pd' ORDER BY engine = 'View' DESC, name")
    if code != 0:
        raise Stop("查不了 ClickHouse 里的表：%s" % err[:200])
    rows = [l.split("\t") + ["", ""] for l in out.splitlines() if l.strip()]
    kept = [r for r in rows if r[0] in KEEP_TABLES]
    rows = [r for r in rows if r[0] not in KEEP_TABLES]
    say("== 删掉集群 %s 面板二的数据（%s以后从空库开始；ClickHouse、面板一、Prometheus、Grafana 都不动）" % (CLUSTER, why))
    for name, engine, n in (r[:3] for r in kept):
        say("  · 保留 vllm_pd.%s：%s 行（过滤器自己的记录，和 bin/clear_data.sh 一样不删）" % (name, n if n.strip().isdigit() else 0))
    keep_text = "；保留 %s" % "、".join(r[0] for r in kept) if kept else ""
    if not rows:
        say("  · 库 vllm_pd 里没有要删的表")
        return "删数据：库 vllm_pd 里没有要删的表%s" % keep_text
    total, listed = 0, []
    for name, engine, n in (r[:3] for r in rows):
        n = int(n) if n.strip().isdigit() else 0
        total += n
        kind = "视图" if engine == "View" else "表"
        say("  · %s vllm_pd.%s：%d 行" % (kind, name, n) if kind == "表" else "  · 视图 vllm_pd.%s" % name)
        listed.append("%s %d 行" % (name, n) if kind == "表" else name)
    for name, engine, _ in (r[:3] for r in rows):
        code, out, err = ch(env, "DROP TABLE IF EXISTS vllm_pd.`%s` SYNC" % name.replace("`", ""))
        if code != 0:
            raise Stop("删不掉 vllm_pd.%s：%s" % (name, err[:200]))
    n_tab = sum(1 for r in rows if r[1] != "View")
    say("  · 已删除 %d 张表、%d 个视图，共 %d 行" % (n_tab, len(rows) - n_tab, total))
    return "删数据：已删除 vllm_pd 的 %d 张表、%d 个视图，共 %d 行（%s）%s" % (n_tab, len(rows) - n_tab, total, "，".join(listed), keep_text)


# ==================== 备份 ====================
def backup(env):
    bdir = os.path.join(BACKUP_ROOT, datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(bdir)
    os.chmod(bdir, 0o700)
    say("== 备份到 %s" % bdir)
    man = {"cluster": CLUSTER, "pack": "包二 " + PACK_VERSION, "made": datetime.datetime.now().isoformat(timespec="seconds"),
           "version_before": installed_version(NEW2), "folders": {}, "boards": {}, "datasources": {}}
    name = "panel2-folder.tar"
    with tarfile.open(os.path.join(bdir, name), "w") as t:
        # offline/ is the unchanged offline pack; __pycache__ is rebuilt by Python whenever the scripts run
        t.add(NEW2, arcname=".", filter=lambda ti: None if ti.name.startswith("./offline") or "__pycache__" in ti.name else ti)
    man["folders"]["panel2"] = {"path": NEW2, "tar": name}
    shutil.copy2(os.path.join(NEW2, "deploy.env"), os.path.join(bdir, "panel2-deploy.env"))
    shutil.copy2(REGISTRY, os.path.join(bdir, "registry.json"))
    os.makedirs(os.path.join(bdir, "grafana"))
    u = env["GRAFANA_BOARD_UID"]
    code, b = grafana(env, "GET", "/api/dashboards/uid/" + u)
    if code != 200:
        raise Stop("看板 %s 取不回来（HTTP %s）" % (u, code))
    json.dump(b, open(os.path.join(bdir, "grafana", "board-%s.json" % u), "w"), ensure_ascii=False, indent=1)
    man["boards"][u] = {"title": b["dashboard"].get("title"), "folderUid": b["meta"].get("folderUid"), "hash": board_hash(b["dashboard"])}
    u = env.get("GRAFANA_DS_UID")
    code, d = grafana(env, "GET", "/api/datasources/uid/%s" % u)
    if code != 200:
        raise Stop("数据源 %s 取不回来（HTTP %s）" % (u, code))
    json.dump(d, open(os.path.join(bdir, "grafana", "datasource-%s.json" % u), "w"), ensure_ascii=False, indent=1)
    man["datasources"][u] = ds_hash(d)
    code, out, err = ch(env, "SHOW CREATE SETTINGS PROFILE vllm_reader_profile")
    if code != 0:
        raise Stop("只读用户的 profile 取不回来：%s" % err[:200])
    open(os.path.join(bdir, "reader-profile.sql"), "w").write(out.replace("\\'", "'") + "\n")
    man["profile"] = profile_elements(env)
    man["node_config"] = node_config_versions(env)
    man["snapshot"] = snapshot(env)
    man["panel1_check"], man["panel1_counts"] = panel1_result()
    json.dump(man, open(os.path.join(bdir, "manifest.json"), "w"), ensure_ascii=False, indent=1)
    say("  面板二目录（不含 offline）、deploy.env、注册表、看板、数据源、只读用户的 profile；节点上的 segments.json 版本 %s" % " ".join(man["node_config"] or ["?"]))
    return bdir, man


def prev_backup():
    """The backup that takes the cluster back to v2.1.

    run may be re-run after it stopped midway (design 1.1, S4). Every run makes its own backup, but a re-run's backup can
    hold a half-upgraded folder (deploy.env already migrated, or the scripts already v3). The v2.1 state is the one
    the FIRST run of an attempt backed up, so: among this script's backups of this cluster ("包二 v3.1-extra") whose
    version_before is v2.1, the earliest one made after the last completed rollback to v2.1. A rollback to v2.1 ends an
    attempt (the folder is v2.1 again, the next run starts a new one), so backups older than it belong to an earlier
    attempt. When this run is the first of its attempt, that is this run's own backup. "" when there is none (the cluster
    was never v2.1 under this script)."""
    if not os.path.isdir(BACKUP_ROOT):
        return ""
    cands, last_rollback = [], ""
    for d in sorted(os.listdir(BACKUP_ROOT)):
        p = os.path.join(BACKUP_ROOT, d)
        try:
            m = json.load(open(os.path.join(p, "manifest.json")))
        except (OSError, ValueError):
            continue
        if m.get("cluster") != CLUSTER or m.get("pack") != "包二 " + PACK_VERSION or m.get("version_before") != PREV_VERSION:
            continue
        cands.append((m.get("made", ""), p))
        try:
            done = [l.strip() for l in open(os.path.join(p, ROLLBACK_MARK)) if l.strip()]
        except OSError:
            done = []
        last_rollback = max([last_rollback] + done)
    later = sorted(c for c in cands if c[0] > last_rollback)
    return later[0][1] if later else ""


def rollback_lines(bdir):
    """The rollback commands for the run summary: back to v2.1, and back to just before this run when that differs."""
    me = os.path.abspath(sys.argv[0])
    prev = prev_backup()
    out = []
    if prev:
        out.append("回到 %s：python3 %s rollback %s --cluster %s" % (PREV_VERSION, me, prev, CLUSTER))
    if bdir and bdir != prev:
        before = json.load(open(os.path.join(bdir, "manifest.json"))).get("version_before", "?")
        out.append("回到这次 run 之前（%s）：python3 %s rollback %s --cluster %s" % (before, me, bdir, CLUSTER))
    return out


# ==================== 更新面板二 ====================
def p2_update(env):
    """bin/panel2_cluster.sh add from the new pack against the cluster's folder (deploy.env and inventory.json kept;
    the data was dropped before). One pass: no retry (B5). Items whose content did not change are skipped by add
    (design 2.13); its 「跳过（内容不变）：…」 and 「已更新：…」 lines come back. --no-safety-check: add does not run the
    upgrade-safety check (step 18), the data flow never gates the update."""
    mode = env.get("NODE_MODE") or "mock"
    cmd = 'bin/panel2_cluster.sh add %s --model-group %s --mode %s --inventory "%s/inventory.json" --no-safety-check' % (
        CLUSTER, MODEL_GROUP, mode, NEW2)
    say("  · 用新包执行 bin/panel2_cluster.sh add（不做升级安全检查）：内容不变的项跳过；deploy.env、清单沿用")
    code, out = sh(cmd, cwd=P2_PACK)
    done = []
    for line in out.splitlines():
        t = re.sub(r"^(OK|INFO|WARN) +", "", line.strip())
        if t.startswith(("跳过（内容不变）：", "已更新：")):
            done.append(t)
            say("    " + t)
    if code != 0:
        found, result = findings(out)
        bad = [x for x in found if x[0] == "FAIL"]
        # the last RESULT line of add's output is a step's (often an earlier step's OK), not add's own result
        SUMMARY.extend(show("add", "没有完成（退出码 %d）" % code, bad))
        tail = [l for l in out.splitlines() if l.strip()]
        raise Stop("面板二的 add 没过：%s" % (bad[-1][1] if bad else tail[-1] if tail else "退出码 %d" % code)[:200])
    return done


# ==================== 不变的项 ====================
P1_RESULT = re.compile(r"(\d+) targets down, (\d+) missing, (\d+) instance problems, (\d+) panels")
P1_DOWN = re.compile(r"^\s*DOWN (\S+?): ", re.M)


def own_jobs():
    """This cluster's panel 2 scrape jobs on panel 1's Prometheus: the puller's and the collector's."""
    return re.compile(r"^vllm-timing-(puller|central)-%s$" % re.escape(CLUSTER))


def panel1_counts(out):
    """[targets down, missing, instance problems, panels without data] from panel 1's check output, the targets of
    this cluster's panel 2 jobs (own_jobs) left out of the first: run and rollback restart the puller and the
    collector, and panel 1's Prometheus reports them down until its next scrape. Those are panel 2's own processes,
    not panel 1's. None when the RESULT line is neither OK nor in the form verify_panel1.py prints."""
    result = findings(out)[1].strip()
    if result == "RESULT: OK":
        return [0, 0, 0, 0]
    m = P1_RESULT.search(result)
    if not m:
        return None
    nums = [int(x) for x in m.groups()]
    own = own_jobs()
    nums[0] = max(0, nums[0] - sum(1 for job in P1_DOWN.findall(out) if own.match(job)))
    return nums


def panel1_result():
    """(RESULT line, panel1_counts) of panel 1's own check, recorded with the backup (invariants compares against
    them); (None, None) when the cluster has no panel 1."""
    if not has_panel1():
        return None, None
    code, out = sh("bin/panel1_cluster.sh check %s 2>&1" % CLUSTER, cwd=NEW1)
    return findings(out)[1] or "没有 RESULT 行（退出码 %d）" % code, panel1_counts(out)


def invariants(env, before, what):
    """What must not change, as v2.0 checked it, without the data-accuracy checks (they are bin/panel2_cluster.sh quality
    now and gate nothing): the panel's version, the cluster's containers, volumes and registry entry, old panel 1, the
    Grafana container, and panel 1's own check (its FAIL, WARN and INFO lines verbatim, B2).
    Returns (results, panel 1 lines for the summary)."""
    results = []

    def item(level, text):
        results.append((level, text))
        say("%-4s %s" % (level, text))

    say("== 不变的项（面板二版本、容器、数据卷、注册表、旧面板一、Grafana%s）" % ("、面板一" if has_panel1() else ""))
    want_ver = PACK_VERSION if what == "更新" else before.get("version_before", PREV_VERSION)
    ver = installed_version(NEW2)
    item("OK" if ver == want_ver else "FAIL", "面板二现在是 %s（应为 %s）" % (ver, want_ver))
    now = snapshot(env)
    for c, a in before["snapshot"]["containers"].items():
        z = now["containers"].get(c)
        if not z:
            item("FAIL", "容器 %s 不在了" % c)
        elif c == env.get("CENTRAL_CONTAINER"):
            same = all(a[k] == z[k] for k in ("image", "mounts", "ports")) and z["running"]
            item("OK" if same else "FAIL", "采集器 %s：镜像、数据卷、端口不变，在运行（重建时 ID 会变）" % c)
        else:
            item("OK" if a == z else "FAIL", "容器 %s 不变（ID %s）" % (c, z["id"]) if a == z else "容器 %s 变了：%s → %s" % (c, a, z))
    item("OK" if before["snapshot"]["volumes"] == now["volumes"] else "FAIL", "集群的数据卷不变（%d 个）" % len(now["volumes"]))
    item("OK" if before["snapshot"]["registry"] == now["registry"] else "FAIL", "注册表里集群 %s 的条目不变" % CLUSTER)
    diff = [k for k in set(before["snapshot"]["old1"]) | set(now["old1"]) if before["snapshot"]["old1"].get(k) != now["old1"].get(k)]
    item("OK" if not diff else "FAIL", "旧面板一不变（目录文件、旧 Prometheus 容器、「vLLM P/D」看板、旧数据源）" if not diff else "旧面板一变了：%s" % diff)
    item("OK" if before["snapshot"].get("grafana") == now.get("grafana") else "FAIL", "Grafana 容器没有重启")
    # Panel 1's own check reports panel 1's data (targets down, panels without data), which the site's exporters
    # decide, not pack two (B1): what counts is that the update did not make it worse. Its counts are recorded with
    # the backup and compared here one by one; FAIL only when a count went up. This cluster's own panel 2 jobs
    # (vllm-timing-puller/central-<cluster>), which run and rollback have just restarted, are not counted.
    if not has_panel1():
        item("INFO", "集群 %s 没有面板一（%s 不存在），不比面板一的 check" % (CLUSTER, NEW1))
        bad = [t for lv, t in results if lv == "FAIL"]
        say("RESULT: %s" % ("OK（%d 项全过）" % len(results) if not bad else "FAIL（%d 项没过）" % len(bad)))
        return results, []
    code, out = sh("bin/panel1_cluster.sh check %s 2>&1" % CLUSTER, cwd=NEW1)
    found, result = findings(out)
    result = result or "没有 RESULT 行（退出码 %d）" % code
    was = before.get("panel1_check")
    now_c, was_c = panel1_counts(out), before.get("panel1_counts")
    nums = lambda t: [int(x) for x in re.findall(r"\d+", t or "")]
    if was is None:
        item("INFO", "面板一 check：%s（备份里没有升级前的结果，只记录）" % result)
    elif now_c is not None and was_c is not None:
        better = all(a <= b for a, b in zip(now_c, was_c))
        verdict = "相同" if now_c == was_c else "更好" if better else "比升级前差"
        counts = "，".join("%s %d（升级前 %d）" % (k, a, b) for k, a, b in zip(
            ("targets down", "missing", "instance problems", "panels 没有数据或出错"), now_c, was_c))
        item("OK" if better else "FAIL", "面板一 check：不计本集群面板二的 2 个任务（vllm-timing-puller-%s、vllm-timing-central-%s），%s；%s" % (
            CLUSTER, CLUSTER, counts, verdict))
    elif result == was or (nums(result) and len(nums(result)) == len(nums(was)) and all(a <= b for a, b in zip(nums(result), nums(was)))):
        item("OK", "面板一 check 和升级前相同或更好：%s（升级前 %s）" % (result, was))
    else:
        item("FAIL", "面板一 check 比升级前差：%s（升级前 %s）" % (result, was))
    p1 = show("面板一 check 原文（含本集群面板二的任务）", result or "没有 RESULT 行（退出码 %d）" % code, found) if [x for x in found if x[0] != "OK"] else []
    bad = [t for lv, t in results if lv == "FAIL"]
    say("RESULT: %s" % ("OK（%d 项全过）" % len(results) if not bad else "FAIL（%d 项没过）" % len(bad)))
    return results, p1


# ==================== 恢复 ====================
def restore_folder(tar_path, folder):
    """Writes every file back in place (same inode, so bind-mounted files follow), removes files the update added; offline/ is left alone."""
    with tarfile.open(tar_path) as t:
        members = {m.name[2:] if m.name.startswith("./") else m.name: m for m in t.getmembers()}
        for name, m in members.items():
            if not name or name == ".":
                continue
            dst = os.path.join(folder, name)
            if m.isdir():
                os.makedirs(dst, exist_ok=True)
            elif m.isfile():
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                data = t.extractfile(m).read()
                with open(dst, "wb") as f:
                    f.write(data)
                os.chmod(dst, m.mode)
        kept = set(members)
        for dp, dn, fn in os.walk(folder, topdown=True):
            rel = os.path.relpath(dp, folder)
            if rel == "offline" or rel.startswith("offline" + os.sep):
                dn[:] = []
                continue
            dn[:] = [d for d in dn if not (rel == "." and d == "offline")]
            for f in fn:
                r = os.path.normpath(os.path.join(rel, f))
                if r not in kept and not r.startswith("offline"):
                    os.remove(os.path.join(dp, f))


def restore_registry_entry(saved):
    """Only this cluster's entry and the root come back; clusters added since the backup stay registered."""
    import fcntl
    with open(REGISTRY + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = json.load(open(saved))
        cur = json.load(open(REGISTRY))
        if CLUSTER in old.get("clusters", {}):
            cur.setdefault("clusters", {})[CLUSTER] = old["clusters"][CLUSTER]
        if "root" in old:
            cur["root"] = old["root"]
        tmp = "%s.tmp.%d" % (REGISTRY, os.getpid())
        with open(tmp, "w") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, REGISTRY)


def filter_shas(folder):
    return {f: sha256(os.path.join(folder, "node", f)) if os.path.isfile(os.path.join(folder, "node", f)) else ""
            for f in ("pd_diag_logfilter.py", "pd_diag_logging.json")}


def puller_left():
    """Pids of this cluster's puller (scripts/puller.py in its panel 2 folder) that are still running."""
    pids = set()
    for d in {NEW2, os.path.realpath(NEW2)}:
        try:
            p = subprocess.run(["pgrep", "-f", "--", re.escape(os.path.join(d, "scripts", "puller.py"))],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        pids.update(p.stdout.decode().split())
    return sorted(pids, key=int)


def rollback(env, bdir):
    """Design 1.3, without any data-flow check: the cluster's data is dropped (drop_data) and the earlier version's
    apply_segments.sh creates its tables again, empty. Returns (comparison with the backup, notes), each a list of
    (level, text)."""
    man = json.load(open(os.path.join(bdir, "manifest.json")))
    if man.get("cluster") != CLUSTER or man.get("pack") != "包二 " + PACK_VERSION:
        raise Stop("%s 不是这个脚本（包二 %s）给集群 %s 做的备份（manifest 里是「%s」、集群 %s）" % (
            bdir, PACK_VERSION, CLUSTER, man.get("pack"), man.get("cluster")))
    say("== 恢复 %s（%s 的备份，更新前是 %s）" % (bdir, man["made"], man.get("version_before")))
    notes = []
    mode = env.get("NODE_MODE") or "mock"
    filt_before = filter_shas(NEW2)
    stop_ok = True
    if mode == "mock":
        cur = installed_version(NEW2)
        if cur == PACK_VERSION:
            # This version's nodes.py stops the mock itself over SSH, whatever node_mock.sh the node has (design 2.3).
            # It runs now, before the restore, with the scripts that started the mock.
            say("  · 先停各节点的 mock：用集群目录现在这一版（%s）的 bin/nodes.sh stop，由监控机经 SSH 停进程" % cur)
            code, out = sh("bin/nodes.sh stop 2>&1", cwd=NEW2)
            found, result = findings(out)
            stop_ok = code == 0
            notes.append(("OK" if stop_ok else "FAIL", "mock 停止（bin/nodes.sh stop）：%s" % (result or "退出码 %d" % code)))
            notes += [(lv, l) for lv, l in found if lv != "OK"][:SHOW]
            show("mock 停止", result or "退出码 %d" % code, found, with_ok=True)
        else:
            say("  · 集群目录还是 %s：add 没有换过它的脚本，节点上的 mock 没有被这次升级动过，不另外停" % cur)
    # v3.1-extra's puller may pull the proxy and Mooncake into PULL_DIR/_proxy, _mooncake, _native; v3's collector would
    # read those files and not understand them. The puller is stopped first (its script is still this version's), the
    # three folders and its pairing state removed, and v3's puller started after the restore. A puller that does not
    # stop would keep writing there: the rollback stops before it deletes or restores anything.
    code, out = sh("bin/puller.sh stop 2>&1", cwd=NEW2)
    left = puller_left()
    if code != 0 or left:
        raise Stop("拉取程序停不下来（bin/puller.sh stop 退出码 %d%s），回滚停在这里，还没有删目录、没有写回任何东西。"
                   "先在 %s 里执行 bin/puller.sh stop，或者手工结束这些进程，再重跑这条 rollback" % (
                       code, "，还在运行的进程 %s" % " ".join(left) if left else "", NEW2))
    notes.append(("OK", "拉取程序先停下：已停止"))
    pull = env.get("PULL_DIR") or ""
    gone = []
    for d in EXTRA_MIRRORS:
        p = os.path.join(pull, d)
        if pull and os.path.isdir(p) and os.path.realpath(p).startswith(os.path.realpath(pull) + os.sep):
            shutil.rmtree(p)
            gone.append(d)
    st = os.path.join(env.get("TIMING_HOME") or "", "puller", "state", "native.json")
    if env.get("TIMING_HOME") and os.path.isfile(st):
        os.remove(st)
    notes.append(("INFO", "镜像目录里 v3.1-extra 多出来的 %s" % ("目录已删除：%s" % "、".join(gone) if gone else "目录没有（proxy、Mooncake 都没有采过）")))
    for key, f in man["folders"].items():
        restore_folder(os.path.join(bdir, f["tar"]), f["path"])
        say("  · 目录 %s 写回（含 deploy.env、segments.json）" % f["path"])
    restore_registry_entry(os.path.join(bdir, "registry.json"))
    say("  · 注册表里集群 %s 的条目写回" % CLUSTER)
    notes.append(("INFO", drop_data(env, "回滚")))
    # The folder is back at the earlier version: its own apply_segments.sh rebuilds that version's views, re-imports its
    # board, restarts the collector with its rules and pushes its three node files (bin/nodes.sh push), so the nodes get
    # that version's segments.json (with the keep_hours it had). No --drop-removed: columns the update added stay (empty
    # for that version, it does not read them).
    say("  · 用更新前那一版的 bin/apply_segments.sh 写回视图、采集规则、看板，并下发它的三个节点文件（bin/nodes.sh push；不删列）")
    code, out = sh("bin/apply_segments.sh", cwd=NEW2, show_tail=1)
    if code != 0:
        raise Stop("更新前那一版的 apply_segments.sh 没过（视图、采集器或节点文件下发），看日志")
    seg_now = os.path.join(NEW2, "segments.json")
    cid_now = json.load(open(seg_now, encoding="utf-8")).get("config_id")
    notes.append(("OK", "节点文件：更新前的三个文件已下发（apply_segments.sh 里的 bin/nodes.sh push），segments.json 是 %s" % (
        load_criteria(NEW2).segments_version(seg_now))))
    notes.append(("INFO", "看板读回 %s 的 config_id %s，从空库开始：回滚以后的请求才显示" % (man.get("version_before"), cid_now)))
    code, out = sh("bin/puller.sh start 2>&1", cwd=NEW2)
    notes.append(("OK" if code == 0 else "FAIL", "拉取程序用回 %s 启动：%s" % (man.get("version_before"), "已启动" if code == 0 else "没有启动，执行 bin/puller.sh start")))
    if mode == "mock":
        if stop_ok:
            code, out = sh("bin/nodes.sh start 2>&1", cwd=NEW2)
            found, result = findings(out)
            notes.append(("OK" if code == 0 else "FAIL", "mock 用回 %s 启动：%s" % (man.get("version_before"), result or "退出码 %d" % code)))
            notes += [(lv, l) for lv, l in found if lv != "OK"][:SHOW]
            show("mock 启动", result or "退出码 %d" % code, found)
        else:
            notes.append(("FAIL", "mock 没有启动：停的时候还有进程停不掉（见上面那一行的 pid 和命令）；照做后再执行同一条 rollback"))
            say("FAIL mock 没有启动：先处理停不掉的进程")
    u, info = next(iter(man["boards"].items()))
    b = json.load(open(os.path.join(bdir, "grafana", "board-%s.json" % u)))
    d = dict(b["dashboard"])
    d.pop("id", None)
    code, r = grafana(env, "POST", "/api/dashboards/db", {"dashboard": d, "folderUid": info["folderUid"], "overwrite": True, "message": "vllm_pd_update_pack2 rollback"})
    say("  · 看板 %s 写回：HTTP %s" % (u, code))
    if code != 200:
        raise Stop("看板 %s 写不回去：%s" % (u, r.get("message")))
    for u, h in man["datasources"].items():
        code, cur = grafana(env, "GET", "/api/datasources/uid/" + u)
        if code == 200 and ds_hash(cur) == h:
            say("  · 数据源 %s 没变，不动" % u)
            continue
        dsrc = json.load(open(os.path.join(bdir, "grafana", "datasource-%s.json" % u)))
        code, r = grafana(env, "PUT", "/api/datasources/uid/" + u, dsrc)
        say("  · 数据源 %s 写回：HTTP %s" % (u, code))
    sql = open(os.path.join(bdir, "reader-profile.sql")).read().strip()
    sql = re.sub(r"^CREATE SETTINGS PROFILE", "ALTER SETTINGS PROFILE", sql)
    code, out, err = ch(env, sql)
    if code == 0 and profile_elements(env) != man["profile"]:
        names = ", ".join("%s = %s" % (k, v if re.match(r"^\d+$", v) else "'%s'" % v) for k, v in sorted(man["profile"].items()))
        code, out, err = ch(env, "ALTER SETTINGS PROFILE vllm_reader_profile DROP ALL SETTINGS, ADD SETTINGS %s" % names)
    say("  · 只读用户的 profile 写回：%s" % ("OK" if code == 0 and profile_elements(env) == man["profile"] else "失败 %s" % err[:200]))
    filt_after = filter_shas(NEW2)
    if filt_before == filt_after:
        notes.append(("INFO", "vLLM 不用重启：过滤器程序和日志配置（pd_diag_logfilter.py、pd_diag_logging.json）回滚前后相同，segments.json 过滤器自己重读"))
    else:
        changed = [f for f in filt_before if filt_before[f] != filt_after[f]]
        notes.append(("WARN" if mode == "vllm" else "INFO", "过滤器文件回滚前后不同（%s）：%s" % (
            "、".join(changed), "各节点的 vLLM 要重启才用回旧的" if mode == "vllm" else "mock 已用回旧的启动")))
    say("== 和更新前比较")
    res = []
    for u, info in man["boards"].items():
        code, b = grafana(env, "GET", "/api/dashboards/uid/" + u)
        ok = code == 200 and board_hash(b["dashboard"]) == info["hash"]
        res.append(("OK" if ok else "FAIL", "看板 %s 和更新前相同" % u if ok else "看板 %s 和更新前不同" % u))
    for u, h in man["datasources"].items():
        code, d = grafana(env, "GET", "/api/datasources/uid/" + u)
        ok = code == 200 and ds_hash(d) == h
        res.append(("OK" if ok else "FAIL", "数据源 %s 和更新前%s" % (u, "相同" if ok else "不同")))
    res.append(("OK" if profile_elements(env) == man["profile"] else "FAIL", "只读用户的 profile 和更新前%s" % ("相同" if profile_elements(env) == man["profile"] else "不同")))
    for key, f in man["folders"].items():
        with tarfile.open(os.path.join(bdir, f["tar"])) as t:
            diff = [m.name for m in t.getmembers() if m.isfile() and "__pycache__" not in m.name
                    and hashlib.sha256(t.extractfile(m).read()).hexdigest() != sha256(os.path.join(f["path"], m.name))]
        res.append(("OK" if not diff else "FAIL", "目录 %s 的文件和更新前相同" % f["path"] if not diff else "目录 %s 有 %d 个文件不同：%s" % (f["path"], len(diff), " ".join(diff[:5]))))
    for lv, text in res + [x for x in notes if not FINDING.match(x[1])]:
        say("%-4s %s" % (lv, text))
    # a completed rollback ends an upgrade attempt (prev_backup)
    with open(os.path.join(bdir, ROLLBACK_MARK), "a") as f:
        f.write(datetime.datetime.now().isoformat(timespec="seconds") + "\n")
    return res, notes


# ==================== 入口 ====================
re_cluster = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")

def main():
    global LOG, PROFILE
    args = sys.argv[1:]
    if "--cluster" in args:
        i = args.index("--cluster")
        if i + 1 >= len(args) or not re_cluster.fullmatch(args[i + 1]):
            print(__doc__)
            return 2
        set_cluster(args[i + 1])
        args = args[:i] + args[i + 2:]
    if "--profile" in args:
        i = args.index("--profile")
        if i + 1 >= len(args) or not re.fullmatch(r"[a-z0-9_-]+", args[i + 1]):
            print(__doc__)
            return 2
        PROFILE = args[i + 1]
        args = args[:i] + args[i + 2:]
    if args not in (["check"], ["run"]) and not (len(args) == 2 and args[0] == "rollback" and not args[1].startswith("-")):
        print(__doc__)
        return 2
    cmd = args[0]
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, "pack2-v31extra-%s-%s-%s.log" % (CLUSTER, cmd, datetime.datetime.now().strftime("%Y%m%d-%H%M%S")))
    LOG = open(path, "w")
    os.chmod(path, 0o600)
    say("vllm_pd_update_pack2_v31extra.py %s，面板二 %s，集群 %s%s，%s" % (" ".join(args), PACK_VERSION, CLUSTER,
        "，--profile %s" % PROFILE if PROFILE else "", datetime.datetime.now().isoformat(timespec="seconds")))
    step, rc, bdir = "检查", 0, ""
    try:
        if cmd == "check":
            ok, env, plan = check()
            rc = 0 if ok else 1
        elif cmd == "run":
            # Only what protects the machine and the other services gates the update; the data flow never does.
            ok, env, plan = check(quiet=True, full=False)
            if not ok:
                raise Stop("预检有 FAIL，什么都没有改：%s。处理后重跑 run" % "；".join(plan["fails"])[:400])
            SUMMARY.append("预检：通过（更新前面板二是 %s，%s 模式%s）" % (
                plan["version"], plan["mode"], "；迁移预览 WARN %d 项见上面和日志" % plan["warns"] if plan["warns"] else ""))
            SUMMARY.extend(["%-4s %s" % (lv, t) for lv, t in plan["lines"] if lv == "WARN"][:SHOW])
            step = "备份"
            bdir, man = backup(env)
            SUMMARY.append("备份：%s" % bdir)
            step = "迁移"
            done = migrate(plan)
            SUMMARY.extend(done or ["迁移：deploy.env 里没有要写的"])
            step = "删数据"
            SUMMARY.append(drop_data(env, "升级"))
            step = "更新面板二"
            say("== 更新面板二")
            SUMMARY.extend(p2_update(env) or ["add：完成（没有打印跳过或更新的项，看日志）"])
            SUMMARY.append("%s%s；%s" % ("" if plan["node"].startswith("节点文件") else "节点文件：", plan["node"], plan["restart"]))
            if plan["cid"][0] != plan["cid"][1]:
                SUMMARY.append("看板：config_id %s → %s，从空库开始，只显示升级以后的请求" % plan["cid"])
            env2 = cluster_env()
            SUMMARY.append("段定义：%s；proxy 采集 %s，Mooncake 计时采集 %s（打开见操作说明 B 第 4 步）" % (
                "profiles/%s/segments.json" % env2.get("SEGMENTS_PROFILE") if env2.get("SEGMENTS_PROFILE") else "包里的 segments.json",
                "开" if env2.get("PROXY_COLLECT") == "1" else "关", "开" if env2.get("MOONCAKE_LOG_COLLECT") == "1" else "关"))
            step = "不变的项"
            res, p1 = invariants(env, man, "更新")
            bad = [t for lv, t in res if lv == "FAIL"]
            SUMMARY.append("不变的项：%s" % ("全部通过（%d 项）" % len(res) if not bad else "%d 项没过：%s" % (len(bad), "；".join(bad)[:300])))
            SUMMARY.extend(p1)
            rc = 0 if not bad else 1
            SUMMARY.append("几分钟后看看板；可选的核对（不影响升级）：cd %s && bin/panel2_cluster.sh check %s" % (NEW2, CLUSTER))
            SUMMARY.extend(rollback_lines(bdir))
        else:
            bdir = os.path.abspath(args[1])
            if not os.path.isfile(os.path.join(bdir, "manifest.json")):
                raise Stop("%s 里没有 manifest.json，不是备份目录" % bdir)
            env = cluster_env()
            step = "恢复"
            res, notes = rollback(env, bdir)
            man = json.load(open(os.path.join(bdir, "manifest.json")))
            step = "恢复后校验"
            inv, p1 = invariants(env, man, "恢复")
            bad = [t for lv, t in res + notes + inv if lv == "FAIL"]
            other = [t for lv, t in res + inv if lv == "FAIL"]      # the notes' FAIL lines follow below
            SUMMARY.append("恢复 %s：%s" % (bdir, "和更新前一致，校验全部通过" if not bad else
                                          "%d 项没过%s" % (len(bad), "：" + "；".join(other)[:300] if other else "，见下面的 FAIL 行")))
            SUMMARY.extend(("%-4s %s" % (lv, t) if not FINDING.match(t) else t) for lv, t in notes)
            SUMMARY.extend(p1)
            rc = 0 if not bad else 1
    except Stop as e:
        say("停在「%s」：%s" % (step, e))
        SUMMARY.append("停在「%s」：%s" % (step, e))
        if cmd == "run" and bdir and step not in ("检查", "备份"):
            SUMMARY.extend(rollback_lines(bdir))
        rc = 1
    except Exception as e:  # noqa: BLE001
        say("停在「%s」：意外错误 %s: %s" % (step, type(e).__name__, e))
        SUMMARY.append("停在「%s」：意外错误 %s: %s" % (step, type(e).__name__, str(e)[:200]))
        if cmd == "run" and bdir and step not in ("检查", "备份"):
            SUMMARY.extend(rollback_lines(bdir))
        rc = 1
    say("")
    say("==== 摘要（只发这一段）====")
    lines = ["vllm_pd_update_pack2_v31extra.py %s，面板二 %s，集群 %s，%s" % (" ".join(args), PACK_VERSION, CLUSTER, "成功" if rc == 0 else "没有完成")] + SUMMARY + ["日志：%s" % path]
    for line in lines:
        say(line[:500])
    return rc


if __name__ == "__main__":
    sys.exit(main())
