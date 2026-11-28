#!/usr/bin/env python3
"""The three files of a cluster folder, written once by bin/panel2_cluster.sh add (and by the migration command from an
earlier deploy.env) with every name, port and UID written out; nothing reads the deployment name at run time.

  clickhouse.env  the ClickHouse side (scheme 9.4.10)       ingest.json  the ingest process (9.4.9)
  grafana.env     the panel side (9.4.11)

  cluster_files.py write --dir D --cluster C --model-group G --port-base B [--root R] [--env-from X] [--fetcher-url U]
                         [--set KEY=VALUE ...]
      Folder without clickhouse.env: the three files from scripts/names.py (the names) and the values every cluster on
      this machine shares (Grafana, the container network, MONITOR_HOST). Folder with them: only --set and --fetcher-url
      are applied. Either way generated/registry.env is rewritten: the record scripts/names.py checks and registers.
  cluster_files.py from-deploy-env --dir D [--fetcher-url U]
      The migration: the three files from the folder's deploy.env (the names it uses now, derived where it left them
      blank); deploy.env is kept as deploy.env.before-t62.
  cluster_files.py missing --dir D
      The required values still to be filled in, one line each; exit 1 when there are any.
"""
import argparse
import json
import os
import re
import secrets
import shlex
import shutil
import string
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from segdef import read_env  # noqa: E402

NAMES = os.path.join(HERE, "names.py")
CH_KEYS = ["CLICKHOUSE_CONTAINER", "CLICKHOUSE_IMAGE", "CLICKHOUSE_ADDR", "CLICKHOUSE_HTTP_PORT", "CLICKHOUSE_DATA_DIR",
           "CLICKHOUSE_MIN_DISK_GB", "CLICKHOUSE_MAX_SERVER_MEMORY_MB", "CLICKHOUSE_DOCKER_ARGS", "CLICKHOUSE_COMPAT_BINARY",
           "DOCKER_NETWORK", "CH_ADMIN_PASSWORD", "CH_WRITER_PASSWORD", "CH_READER_PASSWORD", "SUMMARY_TTL_DAYS",
           "INGEST_STATUS_PORT", "MONITOR_HOST", "PROMETHEUS_URL", "PROMETHEUS_CONTAINER", "PROMETHEUS_YML", "PROMETHEUS_MARK",
           "INGEST_JOB", "RULES_FILE", "RULES_GROUP", "ALERT_STALE_SECONDS"]
GF_KEYS = ["DB_INTERFACE", "CLICKHOUSE_ADDR", "CLICKHOUSE_HTTP_PORT", "CH_READER_PASSWORD", "GRAFANA_URL", "GRAFANA_USER",
           "GRAFANA_PASSWORD", "GRAFANA_CONTAINER", "MODEL_GROUP", "GRAFANA_DS_UID", "GRAFANA_DS_NAME", "GRAFANA_BOARD_UID",
           "GRAFANA_BOARD_TITLE", "BOARD_HAS_VALUE", "PANEL1_P_UID", "PANEL1_D_UID", "STALE_RED_SECONDS"]
CH_IMAGE = "clickhouse/clickhouse-server:26.8.36.3"
DB_INTERFACE = "pd_diag_db/v1"
INGEST_INTERFACE = "pd_diag/v1"
FETCHER_PLACEHOLDER = "http://<proxy 机器>:9300"
HEAD_CH = ("# ClickHouse 侧的配置（面板二）：ClickHouse 启动脚本、建表命令、入库进程、告警任务脚本读取。\n"
           "# 由 bin/panel2_cluster.sh add 写入；三个账号的密码由 add 随机生成，可以改为自定的值（字母和数字）；MONITOR_HOST 由监控端人员填写。\n")
HEAD_GF = ("# 面板端的配置（面板二）：看板生成器和 grafana_api.py 读取。\n"
           "# 由 bin/panel2_cluster.sh add 写入；GRAFANA_PASSWORD、MODEL_GROUP 由监控端人员填写；修改后重新生成看板。\n")
PLAIN = re.compile(r"^[A-Za-z0-9_./:@,+-]*$")
IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def password():
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24))


def env_text(head, keys, vals):
    lines = [head.rstrip("\n")]
    for k in keys:
        v = str(vals.get(k, "") or "")
        lines.append("%s=%s" % (k, v if PLAIN.match(v) else shlex.quote(v)))
    return "\n".join(lines) + "\n"


def write_private(path, text):
    tmp = path + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def names_json(*args):
    r = subprocess.run([sys.executable, "-I", NAMES] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if r.returncode:
        raise SystemExit(r.stderr.strip() or r.stdout.strip())
    return json.loads(r.stdout)


def grafana_port(ch):
    """grafana.env's CLICKHOUSE_HTTP_PORT: the published port for an IPv4 address, the container's 8123 for a name."""
    return ch["CLICKHOUSE_HTTP_PORT"] if IPV4.match(ch.get("CLICKHOUSE_ADDR", "")) else "8123"


def values_from_names(n, shared):
    """clickhouse.env and grafana.env values from scripts/names.py's names for the cluster and the shared values."""
    c = n["CLUSTER"]
    ch = {"CLICKHOUSE_CONTAINER": n["CLICKHOUSE_CONTAINER"], "CLICKHOUSE_IMAGE": CH_IMAGE, "CLICKHOUSE_ADDR": n["CLICKHOUSE_ADDR"],
          "CLICKHOUSE_HTTP_PORT": n["CLICKHOUSE_HTTP_PORT"], "CLICKHOUSE_DATA_DIR": n.get("CLICKHOUSE_DATA_DIR", ""),
          "CLICKHOUSE_MIN_DISK_GB": "1000", "DOCKER_NETWORK": shared.get("DOCKER_NETWORK") or "vllm-pd-monitor",
          "CH_ADMIN_PASSWORD": password(), "CH_WRITER_PASSWORD": password(), "CH_READER_PASSWORD": password(),
          "SUMMARY_TTL_DAYS": "30", "INGEST_STATUS_PORT": n["PULLER_METRICS_PORT"], "MONITOR_HOST": shared.get("MONITOR_HOST", ""),
          "PROMETHEUS_URL": n["PROMETHEUS_URL"], "PROMETHEUS_CONTAINER": n["PROMETHEUS_CONTAINER"],
          "PROMETHEUS_YML": n["PROMETHEUS_YML"], "PROMETHEUS_MARK": n["PROMETHEUS_MARK"],
          "INGEST_JOB": "vllm-pd-ingest-%s" % c, "RULES_FILE": "/prometheus/vllm-pd-ingest-rules-%s.yml" % c,
          "RULES_GROUP": "vllm-pd-ingest-%s" % c, "ALERT_STALE_SECONDS": "120"}
    gf = {"DB_INTERFACE": DB_INTERFACE, "CLICKHOUSE_ADDR": ch["CLICKHOUSE_ADDR"], "CLICKHOUSE_HTTP_PORT": grafana_port(ch),
          "CH_READER_PASSWORD": ch["CH_READER_PASSWORD"], "GRAFANA_URL": shared.get("GRAFANA_URL") or "http://localhost:3000",
          "GRAFANA_USER": shared.get("GRAFANA_USER") or "admin", "GRAFANA_PASSWORD": shared.get("GRAFANA_PASSWORD", ""),
          "GRAFANA_CONTAINER": shared.get("GRAFANA_CONTAINER") or "vllm-pd-grafana", "MODEL_GROUP": n.get("MODEL_GROUP", ""),
          "GRAFANA_DS_UID": n["GRAFANA_DS_UID"], "GRAFANA_DS_NAME": n["GRAFANA_DS_NAME"], "GRAFANA_BOARD_UID": n["GRAFANA_BOARD_UID"],
          "GRAFANA_BOARD_TITLE": n["GRAFANA_BOARD_TITLE"], "BOARD_HAS_VALUE": "", "PANEL1_P_UID": n["PANEL1_P_UID"],
          "PANEL1_D_UID": n["PANEL1_D_UID"], "STALE_RED_SECONDS": "60"}
    return ch, gf


def apply_sets(ch, gf, pairs):
    for pair in pairs:
        k, _, v = pair.partition("=")
        if not re.match(r"^[A-Z_][A-Z0-9_]*$", k) or not _:
            raise SystemExit("不是 KEY=VALUE：%s" % pair)
        hit = False
        if k in CH_KEYS:
            ch[k], hit = v, True
        if k in GF_KEYS and k != "CLICKHOUSE_HTTP_PORT":
            gf[k], hit = v, True
        if not hit:
            raise SystemExit("%s 不是 clickhouse.env 或 grafana.env 的项" % k)
    gf["CLICKHOUSE_HTTP_PORT"] = grafana_port(ch) if gf.get("CLICKHOUSE_ADDR") == ch.get("CLICKHOUSE_ADDR") else gf["CLICKHOUSE_HTTP_PORT"]


def write_ingest(d, url):
    path = os.path.join(d, "ingest.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            ing = json.load(f)
    else:
        ing = {"interface": INGEST_INTERFACE, "fetcher_url": FETCHER_PLACEHOLDER, "poll_seconds": 5, "max_bytes": 4194304}
    if url:
        if not re.match(r"^http://[^/<>]+:\d+$", url):
            raise SystemExit("--fetcher-url 应为 http://<proxy 机器>:<listen_port>：%s" % url)
        ing["fetcher_url"] = url
    with open(path + ".new", "w", encoding="utf-8") as f:
        json.dump(ing, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(path + ".new", path)


def registry_env(d, ch, gf, cluster, port_base, root):
    """generated/registry.env: the cluster's values under the names scripts/names.py checks and registers."""
    os.makedirs(os.path.join(d, "generated"), exist_ok=True)
    vals = {"CLUSTER": cluster, "MODEL_GROUP": gf.get("MODEL_GROUP", ""), "PORT_BASE": port_base or "", "VLLM_PD_ROOT": root or "",
            "PANEL2_DIR": d, "CLICKHOUSE_CONTAINER": ch["CLICKHOUSE_CONTAINER"], "CLICKHOUSE_HTTP_PORT": ch["CLICKHOUSE_HTTP_PORT"],
            "CLICKHOUSE_DATA_DIR": ch.get("CLICKHOUSE_DATA_DIR", ""), "PULLER_METRICS_PORT": ch["INGEST_STATUS_PORT"],
            "PROMETHEUS_CONTAINER": ch["PROMETHEUS_CONTAINER"], "PROMETHEUS_URL": ch["PROMETHEUS_URL"],
            "PROMETHEUS_YML": ch["PROMETHEUS_YML"], "PROMETHEUS_MARK": ch["PROMETHEUS_MARK"], "DOCKER_NETWORK": ch["DOCKER_NETWORK"],
            "MONITOR_HOST": ch.get("MONITOR_HOST", "")}
    for k in ("GRAFANA_URL", "GRAFANA_USER", "GRAFANA_PASSWORD", "GRAFANA_CONTAINER", "GRAFANA_DS_UID", "GRAFANA_DS_NAME",
              "GRAFANA_BOARD_UID", "GRAFANA_BOARD_TITLE", "PANEL1_P_UID", "PANEL1_D_UID"):
        vals[k] = gf.get(k, "")
    write_private(os.path.join(d, "generated", "registry.env"),
                  env_text("# 集群登记用（scripts/names.py check、register）；由 scripts/cluster_files.py 生成，运行时不读取。\n", list(vals), vals))


def load_files(d):
    return read_env(os.path.join(d, "clickhouse.env")), read_env(os.path.join(d, "grafana.env"))


def save_files(d, ch, gf):
    write_private(os.path.join(d, "clickhouse.env"), env_text(HEAD_CH, CH_KEYS, ch))
    write_private(os.path.join(d, "grafana.env"), env_text(HEAD_GF, GF_KEYS, gf))


def cmd_write(a):
    d = os.path.abspath(a.dir)
    if os.path.exists(os.path.join(d, "clickhouse.env")):
        ch, gf = load_files(d)
        apply_sets(ch, gf, a.set)
        save_files(d, ch, gf)
        print("沿用 %s 的 clickhouse.env、grafana.env%s" % (d, "，已按 --set 修改 %d 项" % len(a.set) if a.set else ""))
    else:
        args = ["derive", "--panel", "panel2", "--cluster", a.cluster, "--port-base", str(a.port_base), "--format", "json"]
        if a.model_group:
            args += ["--model-group", a.model_group]
        if a.root:
            args += ["--root", a.root]
        n = names_json(*args)
        shared = names_json("shared", "--format", "json", *(["--env-from", a.env_from] if a.env_from else []))
        if not shared.get("MONITOR_HOST"):
            shared["MONITOR_HOST"] = sibling_monitor_host(d)
        ch, gf = values_from_names(n, shared)
        apply_sets(ch, gf, a.set)
        save_files(d, ch, gf)
        print("已写 %s/clickhouse.env、grafana.env：集群 %s，ClickHouse 端口 %s，入库进程状态端口 %s" % (
            d, a.cluster, ch["CLICKHOUSE_HTTP_PORT"], ch["INGEST_STATUS_PORT"]))
    write_ingest(d, a.fetcher_url)
    registry_env(d, ch, gf, a.cluster, str(a.port_base) if a.port_base else "", a.root)


def sibling_monitor_host(d):
    """MONITOR_HOST of another cluster folder of this machine's registry (one monitoring machine, one address)."""
    try:
        reg = names_json("list", "--format", "json")
    except SystemExit:
        return ""
    for e in reg.get("clusters", {}).values():
        sd = (e.get("panel2") or {}).get("dir")
        if sd and os.path.abspath(sd) != d and os.path.exists(os.path.join(sd, "clickhouse.env")):
            v = read_env(os.path.join(sd, "clickhouse.env")).get("MONITOR_HOST")
            if v:
                return v
    return ""


def cmd_from_deploy_env(a):
    d = os.path.abspath(a.dir)
    old = os.path.join(d, "deploy.env")
    if not os.path.exists(old):
        raise SystemExit("%s 里没有 deploy.env" % d)
    if os.path.exists(os.path.join(d, "clickhouse.env")):
        raise SystemExit("%s 里已经有 clickhouse.env：已经迁移过" % d)
    raw = read_env(old)
    if not raw.get("CLUSTER"):
        raise SystemExit("deploy.env 没有 CLUSTER：先用旧包的 bin/panel2_cluster.sh adopt 登记这个集群")
    n = names_json("derive", "--panel", "panel2", "--env-file", old, "--format", "json")
    shared = {k: raw.get(k, "") for k in ("GRAFANA_URL", "GRAFANA_USER", "GRAFANA_PASSWORD", "GRAFANA_CONTAINER", "DOCKER_NETWORK", "MONITOR_HOST")}
    ch, gf = values_from_names(n, shared)
    for k in ("CLICKHOUSE_IMAGE", "CLICKHOUSE_MIN_DISK_GB", "CLICKHOUSE_MAX_SERVER_MEMORY_MB", "CLICKHOUSE_DOCKER_ARGS",
              "CLICKHOUSE_COMPAT_BINARY", "CH_ADMIN_PASSWORD", "CH_WRITER_PASSWORD", "CH_READER_PASSWORD", "SUMMARY_TTL_DAYS"):
        if raw.get(k):
            ch[k] = raw[k]
    gf["CH_READER_PASSWORD"] = ch["CH_READER_PASSWORD"]
    if raw.get("BOARD_HAS_VALUE"):
        gf["BOARD_HAS_VALUE"] = raw["BOARD_HAS_VALUE"]
    gf["CLICKHOUSE_HTTP_PORT"] = grafana_port(ch)
    save_files(d, ch, gf)
    write_ingest(d, a.fetcher_url)
    registry_env(d, ch, gf, raw["CLUSTER"], raw.get("PORT_BASE", ""), raw.get("VLLM_PD_ROOT", ""))
    shutil.copy2(old, old + ".before-t62")
    os.remove(old)
    print("已从 %s 写出 clickhouse.env、grafana.env、ingest.json；原文件保留为 deploy.env.before-t62" % old)
    print("容器名、端口、UID 与原来相同；入库进程状态端口 %s 沿用原拉取程序的端口" % ch["INGEST_STATUS_PORT"])


def missing(d):
    ch, gf = load_files(d)
    out = []
    for k in ("CH_ADMIN_PASSWORD", "CH_WRITER_PASSWORD", "CH_READER_PASSWORD", "MONITOR_HOST"):
        if not ch.get(k) or ch[k].startswith("change-me"):
            out.append("clickhouse.env 的 %s" % k)
    for k in ("GRAFANA_PASSWORD", "MODEL_GROUP"):
        if not gf.get(k) or gf[k].startswith("change-me"):
            out.append("grafana.env 的 %s" % k)
    if ch.get("CH_READER_PASSWORD") != gf.get("CH_READER_PASSWORD"):
        out.append("clickhouse.env 和 grafana.env 的 CH_READER_PASSWORD 要相同")
    return out


def main():
    ap = argparse.ArgumentParser(description="面板二集群目录的三份配置文件")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("write")
    p.add_argument("--dir", required=True)
    p.add_argument("--cluster", required=True)
    p.add_argument("--model-group", default="")
    p.add_argument("--port-base", type=int)
    p.add_argument("--root")
    p.add_argument("--env-from")
    p.add_argument("--fetcher-url")
    p.add_argument("--set", action="append", default=[])
    p = sub.add_parser("from-deploy-env")
    p.add_argument("--dir", required=True)
    p.add_argument("--fetcher-url")
    p = sub.add_parser("missing")
    p.add_argument("--dir", required=True)
    a = ap.parse_args()
    if a.cmd == "write":
        cmd_write(a)
    elif a.cmd == "from-deploy-env":
        cmd_from_deploy_env(a)
    else:
        m = missing(os.path.abspath(a.dir))
        for x in m:
            print("待填写：" + x)
        sys.exit(1 if m else 0)


if __name__ == "__main__":
    main()
