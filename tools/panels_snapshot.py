#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板一、面板二的快照：只读导出一个集群的配置，写出 MANIFEST 的导出部分和 10 行以内的摘要。

    python3 panels_snapshot.py <集群名> [--out <输出目录>] [--registry <注册表>] [--update-script <vllm_pd_update.py>]

只读：Grafana 只发 GET；ClickHouse 只跑 SELECT 和 SHOW CREATE TABLE / SHOW CREATE SETTINGS PROFILE；文件只读；
docker 只用 exec cat 和 exec clickhouse-client。不导出 ClickHouse 数据、Prometheus TSDB、密码、口令哈希、token：
不执行 SHOW CREATE USER，不读 users.d，deploy.env 不进快照，Vector 配置只导出 render_vector.py --print 的占位符版。

和 vllm_pd_update.py 用同一套东西：路径从注册表（VLLM_PD_REGISTRY，缺省 ~/.vllm-pd-clusters.json）和两个 deploy.env 读
（面板二用 bin/lib.sh load_env，面板一用 scripts/names.py derive）；看板、数据源、只读 profile 的 sha 用它的 board_hash、
ds_hash、profile_elements；屏幕输出过它的 mask()。它从本文件所在目录导入，或用 --update-script 指定；导入时不写 __pycache__。

摘要分三类（逐行比较）：
  原样          面板一看板、面板二看板、ClickHouse 建表、只读 profile、文件（segments.json、节点侧过滤脚本、规则文件、
                names.py、两个面板目录里的 SHA256SUMS）
  抹掉站点值    prometheus.yml（targets 换成占位，api_server、node 两个 label 换成占位）、Vector 配置（占位符版）、
                数据源（地址抹掉）
  只记录        清单（两个 inventory.json；站点不同，不比）

输出目录（缺省 ./panels-snapshot-<集群>，必须不存在或为空）：
    SUMMARY.txt、EXPORT.zh-CN.md（MANIFEST 的导出部分）
    grafana/boards/<uid>.json        看板，去掉 id、version、iteration（board_hash 算的就是它）
    grafana/datasources.json         数据源，去掉 id、version、readOnly 和密码字段
    prometheus/prometheus.yml        抹掉站点值以后的配置
    clickhouse/tables/<库>.<表>.sql  SHOW CREATE TABLE；clickhouse/vllm_reader_profile.sql
    panel2/vector-placeholders.yaml  render_vector.py --print 的输出
    inventory/panel1-inventory.json、inventory/panel2-inventory.json（只记录）
    sha256.txt                       文件类：segments.json、过滤脚本、规则文件、names.py、SHA256SUMS 的 sha256
Python 3.6 起可用，只用标准库。
"""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys

FORMAT = 2
P2_KEYS = ("GRAFANA_URL", "GRAFANA_USER", "GRAFANA_PASSWORD", "GRAFANA_BOARD_UID", "GRAFANA_DS_UID", "CLICKHOUSE_CONTAINER",
           "CENTRAL_CONTAINER", "CH_ADMIN_PASSWORD", "CH_WRITER_PASSWORD", "CH_READER_PASSWORD")
P1_KEYS = ("PANEL1_P_UID", "PANEL1_D_UID", "PROMETHEUS_DS_UID", "PROMETHEUS_CONTAINER", "PROMETHEUS_URL", "PROMETHEUS_YML")
SECRET_DS = ("password", "basicAuthPassword", "secureJsonData")
SITE_LABELS = ("api_server", "node")
U = None   # vllm_pd_update, imported in main()


class Stop(Exception):
    pass


def say(msg):
    print(U.mask(str(msg)) if U else str(msg), flush=True)


def run(cmd, cwd=None, extra_env=None):
    env = dict(os.environ)
    env.update(extra_env or {})
    p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=300)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


def sha_lines(rows):
    """Category sha: rows (name, value) sorted, one 'name<TAB>value' line each, sha256 of the lot, first 16 hex."""
    return sha("".join("%s\t%s\n" % r for r in sorted(rows)))[:16]


def shown(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


def import_update(path):
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("vllm_pd_update", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ---------------- 集群的值（和 vllm_pd_update.cluster_env 同样的读法） ----------------
def load_cluster(cluster, registry):
    try:
        with open(registry, encoding="utf-8") as f:
            entry = json.load(f)["clusters"][cluster]
    except (OSError, ValueError, KeyError):
        raise Stop("注册表 %s 里没有集群 %s" % (shown(registry), cluster))
    new1 = (entry.get("panel1") or {}).get("dir")
    new2 = (entry.get("panel2") or {}).get("dir")
    if not new1 or not os.path.isfile(os.path.join(new1, "deploy.env")):
        raise Stop("集群 %s 的面板一目录不在（注册表里是 %s）" % (cluster, new1))
    env = {"MODEL_GROUP": entry.get("model_group", "")}
    if new2 and os.path.isfile(os.path.join(new2, "bin", "lib.sh")):
        script = '. bin/lib.sh && load_env >/dev/null && for k in %s; do printf "%%s=%%s\\n" "$k" "${!k}"; done' % " ".join(P2_KEYS)
        code, out, err = run(["bash", "-c", script], cwd=new2)
        if code != 0:
            raise Stop("读不了 %s/deploy.env（bin/lib.sh load_env 失败）" % new2)
        env.update(l.split("=", 1) for l in out.splitlines() if "=" in l and l.split("=", 1)[0] in P2_KEYS)
    else:
        new2 = None   # panel 1 only: its deploy.env carries the shared Grafana values
        script = 'set -a; . ./deploy.env >/dev/null 2>&1; for k in GRAFANA_URL GRAFANA_USER GRAFANA_PASSWORD; do printf "%s=%s\\n" "$k" "${!k}"; done'
        code, out, err = run(["bash", "-c", script], cwd=new1)
        env.update(l.split("=", 1) for l in out.splitlines() if "=" in l)
    for k in ("GRAFANA_PASSWORD", "CH_ADMIN_PASSWORD", "CH_WRITER_PASSWORD", "CH_READER_PASSWORD"):
        U.SECRETS.add(env.get(k, ""))
    code, out, err = run([sys.executable, "scripts/names.py", "derive", "--panel", "panel1", "--env-file", "deploy.env", "--format", "json"], cwd=new1)
    if code != 0:
        raise Stop("面板一的名字推不出来（%s 里 names.py derive 失败）" % new1)
    p1 = json.loads(out[out.index("{"):])
    env.update((k, str(p1.get(k) or "")) for k in P1_KEYS)
    return new1, new2, env


# ---------------- Grafana（只读） ----------------
def export_grafana(env, cluster, put, rows):
    code, user = U.grafana(env, "GET", "/api/user")
    if code != 200:
        raise Stop("Grafana %s 登录不了（HTTP %s）；不再访问 Grafana（连续登录失败会锁账号）" % (env["GRAFANA_URL"], code))
    p1 = [env["PANEL1_P_UID"], env["PANEL1_D_UID"]]
    p2 = [env["GRAFANA_BOARD_UID"]] if env.get("GRAFANA_BOARD_UID") else []
    code, found = U.grafana(env, "GET", "/api/search?type=dash-db")
    for d in found if code == 200 else []:
        if d.get("folderTitle") == env["MODEL_GROUP"] and d["uid"].endswith("-" + cluster) and d["uid"] not in p1 + p2:
            p2.append(d["uid"])
    for cat, uids in (("面板一看板", p1), ("面板二看板", sorted(p2))):
        for uid in uids:
            code, b = U.grafana(env, "GET", "/api/dashboards/uid/" + uid)
            if code != 200:
                raise Stop("看板 %s 取不到（HTTP %s）" % (uid, code))
            d = {k: v for k, v in b["dashboard"].items() if k not in ("id", "version", "iteration")}
            put("grafana/boards/%s.json" % uid, json.dumps(d, ensure_ascii=False, sort_keys=True, indent=1) + "\n")
            rows.setdefault(cat, []).append((uid, U.board_hash(b["dashboard"])))
    dss = []
    for uid in (env.get("PROMETHEUS_DS_UID"), env.get("GRAFANA_DS_UID")):
        if not uid:
            continue
        code, d = U.grafana(env, "GET", "/api/datasources/uid/" + uid)
        if code != 200:
            raise Stop("数据源 %s 取不到（HTTP %s）" % (uid, code))
        d = {k: v for k, v in d.items() if k not in SECRET_DS}
        dss.append({k: v for k, v in d.items() if k not in ("id", "version", "readOnly")})
        scrubbed = dict(d, url="<地址>")
        if isinstance(d.get("jsonData"), dict):
            scrubbed["jsonData"] = dict((k, "<地址>" if k in ("host", "server", "url") else v) for k, v in d["jsonData"].items())
        rows.setdefault("数据源", []).append((uid, U.ds_hash(scrubbed)))
    put("grafana/datasources.json", json.dumps(sorted(dss, key=lambda x: x.get("uid", "")), ensure_ascii=False, sort_keys=True, indent=1) + "\n")
    return len(p1), len(p2), len(dss)


# ---------------- Prometheus（抹掉站点值）和规则文件 ----------------
def scrub_prometheus(yml):
    yml = re.sub(r"(targets:\s*)\[[^\]]*\]", r'\1["<target>"]', yml)
    for k in SITE_LABELS:
        yml = re.sub(r'(\b%s:\s*)"[^"]*"' % k, r'\1"<site>"', yml)
    return re.sub(r"^(\s*-?\s*(?:password|bearer_token|credentials|client_secret)\s*:\s*)\S.*$", r"\1<masked>", yml, flags=re.M | re.I)


def export_prometheus(env, new1, put, rows, files):
    path = env.get("PROMETHEUS_YML") or os.path.join(new1, "prometheus", "prometheus.yml")
    with open(path, encoding="utf-8") as f:
        yml = f.read()
    clean = scrub_prometheus(yml)
    put("prometheus/prometheus.yml", clean)
    rows["prometheus.yml"] = [("prometheus.yml", sha(clean)[:16])]
    m = re.search(r"^rule_files:[ \t]*\n((?:[ \t]+-.*\n)+)", yml, re.M)
    rules = re.findall(r"-\s*['\"]?([^'\"\s]+)", m.group(1)) if m else []
    for p in sorted(rules):
        code, out, err = run(["docker", "exec", env["PROMETHEUS_CONTAINER"], "cat", p])
        if code != 0:
            raise Stop("规则文件 %s 在容器 %s 里读不到" % (p, env["PROMETHEUS_CONTAINER"]))
        files.append(("规则文件 " + os.path.basename(p), sha(out)))
    return len(rules)


# ---------------- ClickHouse（只读） ----------------
def ch_raw(env, sql):
    """SHOW CREATE output, raw. The password reaches clickhouse-client through docker exec's environment."""
    code, out, err = run(["docker", "exec", "-i", "-e", "CLICKHOUSE_PASSWORD", env["CLICKHOUSE_CONTAINER"], "clickhouse-client",
                          "--user", "default", "--format", "TSVRaw", "--query", sql], extra_env={"CLICKHOUSE_PASSWORD": env.get("CH_ADMIN_PASSWORD", "")})
    if code != 0:
        raise Stop("ClickHouse 查询失败：%s" % U.mask((err.strip().splitlines() or ["?"])[-1][:200]))
    return out


def export_clickhouse(env, put, rows):
    tables = [l.split("\t") for l in ch_raw(env, "SELECT database, name FROM system.tables WHERE database NOT IN "
                                                 "('system', 'INFORMATION_SCHEMA', 'information_schema') AND NOT is_temporary "
                                                 "ORDER BY database, name").splitlines() if "\t" in l]
    for db, name in tables:
        ddl = ch_raw(env, "SHOW CREATE TABLE `%s`.`%s`" % (db, name)).rstrip("\n") + "\n"
        if re.search(r"PASSWORD|IDENTIFIED", ddl, re.I):
            ddl = re.sub(r"(PASSWORD\s*=?\s*|IDENTIFIED\s+(?:WITH\s+\w+\s+)?BY\s+)'[^']*'", r"\1'<masked>'", ddl, flags=re.I)
        put("clickhouse/tables/%s.%s.sql" % (db, name), ddl)
        rows.setdefault("ClickHouse 建表", []).append(("%s.%s" % (db, name), sha(ddl)[:16]))
    put("clickhouse/vllm_reader_profile.sql", ch_raw(env, "SHOW CREATE SETTINGS PROFILE vllm_reader_profile").rstrip("\n") + "\n")
    prof = U.profile_elements(env)
    if prof is None:
        raise Stop("只读用户的 profile 读不出来")
    rows["只读 profile"] = [("vllm_reader_profile", sha(json.dumps(prof, sort_keys=True))[:16])]
    return len(tables)


# ---------------- 面板二：Vector 占位符版、文件 ----------------
def export_panel2(new2, put, rows, files):
    code, out, err = run(["bash", "-c", ". bin/lib.sh && load_env >/dev/null && python3 scripts/render_vector.py --print"], cwd=new2)
    if code != 0 or not out.strip():
        raise Stop("render_vector.py --print 失败：%s" % U.mask((err.strip().splitlines() or ["?"])[-1][:200]))
    if U.mask(out) != out:
        raise Stop("Vector 占位符版里出现了密码，不导出")
    put("panel2/vector-placeholders.yaml", out)
    rows["Vector 配置"] = [("vector-placeholders.yaml", sha(out)[:16])]
    for label, rel in (("segments.json", "segments.json"), ("节点侧过滤脚本", "node/pd_diag_logfilter.py"),
                       ("面板二 names.py", "scripts/names.py"), ("面板二 SHA256SUMS", "SHA256SUMS")):
        with open(os.path.join(new2, rel), "rb") as f:
            files.append((label, sha(f.read())))


def main():
    global U
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cluster")
    ap.add_argument("--out")
    ap.add_argument("--registry", default=os.environ.get("VLLM_PD_REGISTRY") or os.path.expanduser("~/.vllm-pd-clusters.json"))
    ap.add_argument("--update-script", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "vllm_pd_update.py"))
    a = ap.parse_args()
    if not os.path.isfile(a.update_script):
        print("没有 %s：快照的 sha 要用它的 board_hash、ds_hash、profile_elements，用 --update-script 指定" % a.update_script)
        return 1
    U = import_update(a.update_script)
    out = os.path.abspath(a.out or "panels-snapshot-%s" % a.cluster)
    try:
        if os.path.exists(out) and os.listdir(out):
            raise Stop("输出目录 %s 不是空的" % out)
        new1, new2, env = load_cluster(a.cluster, a.registry)
        say("== 集群 %s：面板一 %s，面板二 %s" % (a.cluster, new1, new2 or "没有登记"))
        written, rows, files = {}, {}, []

        def put(name, data):
            written[name] = data

        say("== Grafana 看板和数据源（只读）")
        n1, n2, nds = export_grafana(env, a.cluster, put, rows)
        say("   面板一 %d 张，面板二 %d 张，数据源 %d 个" % (n1, n2, nds))
        say("== Prometheus 配置（抹掉站点值）、规则文件")
        nrules = export_prometheus(env, new1, put, rows, files)
        with open(os.path.join(new1, "scripts", "names.py"), "rb") as f:
            files.append(("面板一 names.py", sha(f.read())))
        with open(os.path.join(new1, "SHA256SUMS"), "rb") as f:
            files.append(("面板一 SHA256SUMS", sha(f.read())))
        ntables = 0
        if new2:
            say("== ClickHouse 建表、只读 profile（只读查询）")
            ntables = export_clickhouse(env, put, rows)
            say("   %d 张表（含视图）" % ntables)
            say("== Vector 配置（占位符版）、面板二的文件")
            export_panel2(new2, put, rows, files)
        for name, rel in (("panel1-inventory.json", os.path.join(new1, "prometheus", "inventory.json")),
                          ("panel2-inventory.json", os.path.join(new2, "inventory.json") if new2 else "")):
            if rel and os.path.isfile(rel):
                with open(rel, encoding="utf-8") as f:
                    data = f.read()
                put("inventory/" + name, data)
                rows.setdefault("清单", []).append((name, sha(data)[:16]))
        put("sha256.txt", "".join("%s  %s\n" % (h, label) for label, h in sorted(files)))
        rows["文件"] = [(label, h[:16]) for label, h in files]
        for name, data in written.items():
            if U.mask(data) != data:
                raise Stop("%s 里出现了密码，不写出" % name)
        for name, data in sorted(written.items()):
            p = os.path.join(out, name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(data)

        def line(kind, cat, note):
            r = rows.get(cat)
            return "%s %s %s（%s）" % (kind, cat, sha_lines(r) if r else "-", note if r else "无")

        summary = ["面板快照 格式 %d 集群 %s" % (FORMAT, a.cluster),
                   line("原样", "面板一看板", "%d 张" % n1),
                   line("原样", "面板二看板", "%d 张" % n2),
                   line("原样", "ClickHouse 建表", "%d 张表" % ntables),
                   line("原样", "只读 profile", "vllm_reader_profile"),
                   line("原样", "文件", "%d 个，见明细" % len(files)),
                   line("抹掉站点值", "prometheus.yml", "targets、api_server、node 换成占位；规则文件 %d 个" % nrules),
                   line("抹掉站点值", "Vector 配置", "render_vector.py --print"),
                   line("抹掉站点值", "数据源", "%d 个，地址抹掉" % nds),
                   line("只记录", "清单", "不比")]
        with open(os.path.join(out, "SUMMARY.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(summary) + "\n")
        made = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        md = ["## 导出部分（panels_snapshot.py 格式 %d，%s）" % (FORMAT, made), "",
              "集群 %s；面板一目录 `%s`；面板二目录 `%s`；注册表 `%s`。只读导出，不含 ClickHouse 数据、Prometheus TSDB、密码、口令哈希。" % (
                  a.cluster, new1, new2 or "没有登记", shown(a.registry)), "",
              "### 摘要（逐行比较；「只记录」一行不比）", "", "```"] + summary + ["```", "", "### 明细（每类的 sha 由这些行算出）", "",
              "| 类别 | 项 | sha（前 16 位） |", "|---|---|---|"]
        for cat in ("面板一看板", "面板二看板", "ClickHouse 建表", "只读 profile", "文件", "prometheus.yml", "Vector 配置", "数据源", "清单"):
            for name, h in sorted(rows.get(cat, [])):
                md.append("| %s | %s | %s |" % (cat, name, h))
        md += ["", "算法：看板用 vllm_pd_update.board_hash（去掉 id、version、iteration）；数据源用 ds_hash，先把 url 和 jsonData 里的 "
               "host、server、url 换成「<地址>」；只读 profile 用 profile_elements 的结果（键排序的 JSON）再算 sha256；其余是文件内容的 sha256。"
               "每类：各项按名字排序，每行「名字<TAB>sha」，整体算 sha256，取前 16 位。", ""]
        with open(os.path.join(out, "EXPORT.zh-CN.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(md))
        say("== 输出 %s" % out)
        say("==== 摘要 ====")
        for s in summary:
            say(s)
        return 0
    except Stop as e:
        say("停下：%s（什么都没有改）" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
