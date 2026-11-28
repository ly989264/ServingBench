#!/usr/bin/env python3
"""Files and Grafana objects of one panel-1 cluster: its Prometheus configuration, its data source, its two dashboards.

Usage (bin/panel1_cluster.sh runs these; standard library only, Python >= 3.8):
    gen_cluster.py generate       --env-file DIR/deploy.env --inventory INV
    gen_cluster.py prometheus-cmd --env-file DIR/deploy.env
    gen_cluster.py grafana-add    --env-file DIR/deploy.env
    gen_cluster.py grafana-remove --env-file DIR/deploy.env

Names, ports and UIDs come from scripts/names.py (blank values in deploy.env are derived from CLUSTER and PORT_BASE).
generate writes, inside the cluster's folder:
    prometheus/prometheus.yml    gen_prometheus_yml.py output for the inventory (rewritten in place: it is bind-mounted);
                                 panel 2's marked blocks (# BEGIN/END vllm-timing ...) and a rule_files list already
                                 in the file are kept as they are
    prometheus/inventory.json    a copy of the inventory
    cluster/<P uid>.json, cluster/<D uid>.json
                                 the pack's two dashboards with three per-cluster values: uid, title suffix, and the
                                 datasource variable (regex matching only this cluster's data source, current = it)
    cluster/datasource.json      the data source: literal url http://<Prometheus container>:9090, timeInterval = the
                                 inventory's scrape_interval
grafana-add creates or updates the data source, finds or creates the folder MODEL_GROUP, and imports both dashboards
through the Grafana API (no file, no provider, no restart). grafana-remove deletes the dashboards and the data source;
it refuses dashboards that Grafana loads from files (an installation from the README, not from this script).
Grafana address and account: GRAFANA_URL, GRAFANA_USER, GRAFANA_PASSWORD (or GRAFANA_TOKEN), from the environment or
deploy.env. Creating a data source needs the Admin role in the organisation.
"""
import argparse
import base64
import json
import os
import re
import shlex
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(PACK, "prometheus"))
import names as names_mod  # noqa: E402

PROM_IMAGE = "prom/prometheus:v3.11.2"
BOARDS = (("P", "vllm-pd-prefill.json", "PANEL1_P_UID"), ("D", "vllm-pd-decode.json", "PANEL1_D_UID"))
SHARED = ("GRAFANA_URL", "GRAFANA_USER", "GRAFANA_PASSWORD", "GRAFANA_TOKEN", "DOCKER_NETWORK")


class Fail(Exception):
    pass


def load(env_file):
    env = names_mod.env_from_file(env_file)
    try:
        n = names_mod.Names(env).output("panel1")
    except names_mod.NamesError as e:
        raise Fail(str(e))
    for k in SHARED:  # the environment wins over deploy.env
        n[k] = os.environ.get(k) or env.get(k, "")
    n["DIR"] = os.path.dirname(os.path.abspath(env_file))
    return n


def regex_of(name):
    return "/^%s$/" % re.sub(r"([\\^$.|?*+()\[\]{}/])", r"\\\1", name)


def cluster_board(path, n, uid_key):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    d["uid"] = n[uid_key]
    d["title"] = d["title"] + n["PANEL1_TITLE_SUFFIX"]
    found = 0
    for v in d.get("templating", {}).get("list", []):
        if v.get("type") == "datasource":
            v["regex"] = regex_of(n["PROMETHEUS_DS_NAME"])
            v["current"] = {"text": n["PROMETHEUS_DS_NAME"], "value": n["PROMETHEUS_DS_UID"]}
            found += 1
    if found != 1:
        raise Fail("%s 里应有 1 个 datasource 变量，实际 %d 个" % (path, found))
    d.pop("id", None)
    d.pop("version", None)
    return d


def datasource(n, inv):
    return {"name": n["PROMETHEUS_DS_NAME"], "uid": n["PROMETHEUS_DS_UID"], "type": "prometheus", "access": "proxy",
            "url": "http://%s:9090" % n["PROMETHEUS_CONTAINER"], "isDefault": False,
            "jsonData": {"timeInterval": inv.get("scrape_interval", "15s"), "httpMethod": "POST", "prometheusType": "Prometheus"}}


def write_in_place(path, text):
    """Same inode when the file exists: Prometheus has it bind-mounted."""
    if os.path.exists(path):
        with open(path, "r+", encoding="utf-8") as f:
            if f.read() == text:
                return False
            f.seek(0)
            f.write(text)
            f.truncate()
        return True
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return True


# Panel 2 writes its scrape jobs and its rules between these markers ("面板二" or "面板二 <cluster>").
PANEL2_BLOCK = re.compile(r"^# BEGIN vllm-timing (?:rules )?\(面板二[^)\n]*\)\n.*?^# END vllm-timing (?:rules )?\(面板二[^)\n]*\)\n",
                          re.S | re.M)
TOP_RULE_FILES = re.compile(r"^rule_files:[ \t]*\n(?:[ \t]+.*\n|[ \t]*\n)*", re.M)


def keep_existing(old, new):
    """The generated prometheus.yml with what the current file has on top of the generated part: every 面板二 block,
    back where it was (a block right after the scrape_configs: header goes there again, any other one right before
    scrape_configs:), and a top-level rule_files: list outside the blocks. Returns (text, number of blocks kept)."""
    blocks = list(PANEL2_BLOCK.finditer(old))
    rest = PANEL2_BLOCK.sub("", old)
    extra = TOP_RULE_FILES.search(rest)
    if not blocks and not extra:
        return new, 0
    if not re.search(r"^scrape_configs:[ \t]*$", new, re.M):
        raise Fail("生成的 prometheus.yml 里没有 scrape_configs:，放不回面板二的标记段")
    before, after = [], []
    for m in blocks:
        head = PANEL2_BLOCK.sub("", old[:m.start()])
        (after if re.search(r"^scrape_configs:[ \t]*\n\Z", head, re.M) else before).append(m.group(0))
    if extra and not TOP_RULE_FILES.search(new):
        before.insert(0, extra.group(0))
    text = re.sub(r"^scrape_configs:[ \t]*\n", lambda m: "".join(before) + m.group(0) + "".join(after), new, count=1, flags=re.M)
    return text, len(blocks)


def generate(n, inv_path):
    import gen_prometheus_yml as gen
    with open(inv_path, encoding="utf-8") as f:
        inv = json.load(f)
    yml = gen.render(inv, gen.build(inv))
    pdir, cdir = os.path.join(n["DIR"], "prometheus"), os.path.join(n["DIR"], "cluster")
    os.makedirs(cdir, exist_ok=True)
    inv_copy = os.path.join(pdir, "inventory.json")
    if os.path.abspath(inv_path) != os.path.abspath(inv_copy):
        with open(inv_copy, "w", encoding="utf-8") as f:
            json.dump(inv, f, ensure_ascii=False, indent=2)
            f.write("\n")
    yml_path = os.path.join(pdir, "prometheus.yml")
    kept = 0
    if os.path.exists(yml_path):
        with open(yml_path, encoding="utf-8") as f:
            text, kept = keep_existing(f.read(), yml)
    else:
        text = yml
    changed = write_in_place(yml_path, text)
    if kept:
        print("prometheus.yml 里面板二的 %d 段标记段原样保留" % kept)
    for side, name, key in BOARDS:
        d = cluster_board(os.path.join(PACK, "grafana", "dashboards", name), n, key)
        with open(os.path.join(cdir, "%s.json" % n[key]), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
            f.write("\n")
    with open(os.path.join(cdir, "datasource.json"), "w", encoding="utf-8") as f:
        json.dump(datasource(n, inv), f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("集群 %s：prometheus.yml %s（%d 个抓取 job）；看板 %s、%s；数据源 %s（%s）" % (
        n["CLUSTER"], "已写" if changed else "没有变化", yml.count("- job_name:"), n["PANEL1_P_UID"], n["PANEL1_D_UID"],
        n["PROMETHEUS_DS_NAME"], n["PROMETHEUS_DS_UID"]))
    print("Prometheus 启动命令：")
    print(prometheus_cmd(n))


def prometheus_cmd(n):
    if not n.get("DOCKER_NETWORK"):
        raise Fail("没有 DOCKER_NETWORK：共用的 Grafana 所在的容器网络（取自面板二 deploy.env，或 Grafana 容器的网络）")
    yml = os.path.join(n["DIR"], "prometheus", "prometheus.yml")
    return " ".join(shlex.quote(x) for x in [
        "docker", "run", "-d", "--name", n["PROMETHEUS_CONTAINER"], "--network", n["DOCKER_NETWORK"],
        "--restart", "unless-stopped", "-p", "%s:9090" % n["PROMETHEUS_PORT"],
        "-v", "%s:/etc/prometheus/prometheus.yml:ro" % yml, "-v", "%s:/prometheus" % n["PROMETHEUS_VOLUME"],
        PROM_IMAGE, "--config.file=/etc/prometheus/prometheus.yml", "--storage.tsdb.path=/prometheus",
        "--web.enable-lifecycle"])


# ---------------------------------------------------------------- Grafana API
class Grafana:
    def __init__(self, n):
        if not n.get("GRAFANA_URL"):
            raise Fail("没有 GRAFANA_URL")
        self.url = n["GRAFANA_URL"].rstrip("/")
        if n.get("GRAFANA_TOKEN"):
            self.auth = "Bearer " + n["GRAFANA_TOKEN"]
        elif n.get("GRAFANA_USER"):
            raw = "%s:%s" % (n["GRAFANA_USER"], n.get("GRAFANA_PASSWORD", ""))
            self.auth = "Basic " + base64.b64encode(raw.encode("utf-8")).decode("ascii")
        else:
            raise Fail("没有 Grafana 账号：GRAFANA_USER + GRAFANA_PASSWORD，或 GRAFANA_TOKEN")

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body).encode("utf-8") if body is not None else None,
                                     headers={"Content-Type": "application/json", "Accept": "application/json",
                                              "Authorization": self.auth})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode("utf-8")
                return r.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw[:300]
        except urllib.error.URLError as e:
            raise Fail("连不上 Grafana %s：%s" % (self.url, e.reason))

    def must(self, method, path, body=None, ok=(200,)):
        code, res = self.call(method, path, body)
        if code not in ok:
            raise Fail("Grafana %s %s：HTTP %s %s" % (method, path, code, str(res)[:200]))
        return res


def lookup(g, path):
    """GET that tells "not there" (404 -> None) from everything else: any answer but 200 or 404 (wrong password,
    not an admin, ...) stops before anything is created or deleted."""
    code, res = g.call("GET", path)
    if code == 200:
        return res
    if code == 404:
        return None
    raise Fail("Grafana GET %s：HTTP %s %s（账号或密码不对，或者没有权限）；什么都没有改" % (path, code, str(res)[:120]))


def file_loaded(res):
    """A dashboard Grafana loads from a file; with allowUiUpdates true it reports provisioned=false but keeps the file id."""
    meta = res.get("meta") or {}
    return bool(meta.get("provisioned") or meta.get("provisionedExternalId"))


def folder_uid(g, title):
    for f in g.must("GET", "/api/folders?limit=1000") or []:
        if f.get("title") == title:
            return f["uid"], False
    return g.must("POST", "/api/folders", {"title": title})["uid"], True


def grafana_add(n):
    g = Grafana(n)
    cdir = os.path.join(n["DIR"], "cluster")
    with open(os.path.join(cdir, "datasource.json"), encoding="utf-8") as f:
        ds = json.load(f)
    old = lookup(g, "/api/datasources/uid/" + ds["uid"])
    for _, _, key in BOARDS:
        b = lookup(g, "/api/dashboards/uid/" + n[key])
        if b is not None and file_loaded(b):
            raise Fail("看板 %s 是文件预置的，不由这个脚本管" % n[key])
    if old is not None:
        if old.get("readOnly"):
            raise Fail("数据源 %s 是文件预置的，不由这个脚本管" % ds["uid"])
        g.must("PUT", "/api/datasources/uid/" + ds["uid"], dict(ds, version=old.get("version", 1)))
        print("数据源 %s 已更新" % ds["name"])
    else:
        g.must("POST", "/api/datasources", ds)
        print("数据源 %s 已建" % ds["name"])
    code, h = g.call("GET", "/api/datasources/uid/%s/health" % ds["uid"])
    print("数据源健康检查：%s" % ("OK" if code == 200 else "HTTP %s %s" % (code, str(h)[:120])))
    fuid, created = folder_uid(g, n["GRAFANA_FOLDER"])
    print("文件夹 %s %s" % (n["GRAFANA_FOLDER"], "已建" if created else "已有"))
    for side, _, key in BOARDS:
        uid = n[key]
        with open(os.path.join(cdir, "%s.json" % uid), encoding="utf-8") as f:
            board = json.load(f)
        g.must("POST", "/api/dashboards/db", {"dashboard": board, "folderUid": fuid, "overwrite": True,
                                               "message": "panel1_cluster.sh %s" % n["CLUSTER"]})
        print("看板 %s「%s」已导入文件夹 %s" % (uid, board["title"], n["GRAFANA_FOLDER"]))


def grafana_remove(n):
    g = Grafana(n)
    plan = []
    for _, _, key in BOARDS:
        old = lookup(g, "/api/dashboards/uid/" + n[key])
        if old is not None and file_loaded(old):
            raise Fail("看板 %s 是文件预置的（按 README 装的那一套），不能用这个命令删；什么都没有改" % n[key])
        plan.append((n[key], old is not None))
    ds = lookup(g, "/api/datasources/uid/" + n["PROMETHEUS_DS_UID"])
    if ds is not None and ds.get("readOnly"):
        raise Fail("数据源 %s 是文件预置的，不能用这个命令删；什么都没有改" % n["PROMETHEUS_DS_UID"])
    for uid, exists in plan:
        if exists:
            g.must("DELETE", "/api/dashboards/uid/" + uid)
        print("看板 %s %s" % (uid, "已删" if exists else "本来就没有"))
    if ds is not None:
        g.must("DELETE", "/api/datasources/uid/" + n["PROMETHEUS_DS_UID"])
    print("数据源 %s %s" % (n["PROMETHEUS_DS_UID"], "已删" if ds is not None else "本来就没有"))
    print("文件夹 %s 不删（可能还有别的看板）" % n["GRAFANA_FOLDER"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["generate", "prometheus-cmd", "grafana-add", "grafana-remove"])
    ap.add_argument("--env-file", required=True)
    ap.add_argument("--inventory")
    args = ap.parse_args()
    try:
        n = load(args.env_file)
        if args.cmd == "generate":
            if not args.inventory:
                ap.error("generate 要 --inventory")
            generate(n, args.inventory)
        elif args.cmd == "prometheus-cmd":
            print(prometheus_cmd(n))
        elif args.cmd == "grafana-add":
            grafana_add(n)
        else:
            grafana_remove(n)
        return 0
    except (Fail, OSError, ValueError) as e:
        print("错误：%s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
