#!/usr/bin/env python3
"""Put each cluster's Grafana dashboards into the two-level folders of boards_layout.txt, and keep each cluster's
model folder (MODEL_GROUP) equal to its subfolder.

Usage:
    python3 arrange_boards.py folder <cluster>
    python3 arrange_boards.py plan  --env-from <deploy.env holding the Grafana values>
    python3 arrange_boards.py apply --env-from <deploy.env holding the Grafana values>
    python3 arrange_boards.py undo <backup folder> --env-from <deploy.env holding the Grafana values>
    python3 arrange_boards.py state --env-from <deploy.env holding the Grafana values> [--except <cluster>]

folder  prints the subfolder of a cluster in the layout: the --model-group to give a deploy pack's add. Read-only.
plan    read-only: lists what apply would change, one line each, and the folders it would leave alone.
apply   for every cluster of the layout that the registry has:
          1. its model folder: the registry's model_group and MODEL_GROUP in the deploy.env of each of its panels are
             set to its subfolder (the deploy packs then find that title, and their checks stay as they are);
          2. the folders: the top-level folder and the subfolder are created when missing;
          3. its dashboards (by UID, from the registry) are moved into the subfolder; with a fourth layout column the
             cluster name at the end of its two panel-1 titles is replaced by that name;
          4. top-level folders named like a subfolder of the layout, and subfolders of a cluster that is no longer
             registered, are deleted when they are empty (no dashboard, subfolder, alert rule or library panel).
        Before changing anything it copies the registry and the deploy.env files into
        ~/vllm-pd-arrange-backup/<time>/ and records every change there (manifest.json). Running it again when
        everything is in place changes nothing and makes no backup.
undo    puts back what one apply changed, from its backup folder: dashboards (folder and title), model folders,
        and the folders it created when they are empty again. Items changed since are reported and left alone.
state   read-only: one line per registered cluster's model folder, per file of its panels (sha256, first 8
        characters), per dashboard (version, folder, title) and per running container (start time). Two outputs
        compared with diff show what changed in between.

Why: the deploy packs find their Grafana folder by title among the top-level folders only, so every add, update
or rollback of a pack puts the dashboards back into a top-level folder named MODEL_GROUP. apply moves them back.
Grafana address and account: GRAFANA_URL, GRAFANA_USER, GRAFANA_PASSWORD (or GRAFANA_TOKEN) from the environment,
else from --env-from. Registry: VLLM_PD_REGISTRY, else ~/.vllm-pd-clusters.json, written under the deploy packs'
lock. Standard library only, Python 3.6 or later.
"""
import base64
import contextlib
import datetime
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LAYOUT = os.path.join(HERE, "boards_layout.txt")
CLUSTER_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")  # the rule of the deploy packs' names.py
NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?$")  # folder titles: written into deploy.env as they are
GRAFANA_KEYS = ("GRAFANA_URL", "GRAFANA_USER", "GRAFANA_PASSWORD", "GRAFANA_TOKEN")
FILES = {"panel1": ("deploy.env", "prometheus/prometheus.yml", "prometheus/inventory.json"), "panel2": ("deploy.env", "inventory.json")}


class Bad(Exception):
    pass


def utf8_stdout():
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
            setattr(sys, name, io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace", line_buffering=True))


def home():
    return os.environ.get("HOME") or os.path.expanduser("~")


# ---------------- layout ----------------

def load_layout(path=LAYOUT):
    rows, seen, parent_of = [], {}, {}
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError as e:
        raise Bad("读不了对照表 %s：%s" % (path, e))
    for no, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        w = line.split()
        if len(w) not in (3, 4):
            raise Bad("对照表第 %d 行：写成 上一层文件夹 集群名 子文件夹 [标题里的名字]" % no)
        parent, cluster, child = w[:3]
        title = w[3] if len(w) == 4 else None
        if not CLUSTER_RE.match(cluster):
            raise Bad("对照表第 %d 行：集群名 %s 不合规" % (no, cluster))
        for x in (parent, child, title):
            if x is not None and not NAME_RE.match(x):
                raise Bad("对照表第 %d 行：%s 只能用字母、数字和 . _ -" % (no, x))
        if cluster in seen:
            raise Bad("对照表第 %d 行：集群 %s 在第 %d 行已经有了" % (no, cluster, seen[cluster]))
        if parent_of.setdefault(child, parent) != parent:
            raise Bad("对照表第 %d 行：子文件夹 %s 已经放在 %s 下" % (no, child, parent_of[child]))
        seen[cluster] = no
        rows.append({"parent": parent, "cluster": cluster, "child": child, "title": title})
    if not rows:
        raise Bad("对照表 %s 是空的" % path)
    if set(parent_of) & {r["parent"] for r in rows}:
        raise Bad("对照表里有的名字既是上一层文件夹又是子文件夹")
    return rows


# ---------------- registry and deploy.env ----------------

def registry_path():
    return os.environ.get("VLLM_PD_REGISTRY") or os.path.join(home(), ".vllm-pd-clusters.json")


def load_registry():
    path = registry_path()
    if not os.path.exists(path):
        return {"clusters": {}}
    with open(path, encoding="utf-8") as f:
        reg = json.load(f)
    if reg.get("format") != "vllm-pd-clusters":
        raise Bad("%s 不是集群注册表" % path)
    reg.setdefault("clusters", {})
    return reg


@contextlib.contextmanager
def locked_registry():
    """The registry under the deploy packs' lock, written back the way their names.py writes it."""
    path = registry_path()
    with open(path + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        reg = load_registry()
        yield reg
        tmp = "%s.tmp.%d" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(reg, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)


def env_file(panel_section):
    return os.path.join(panel_section["dir"], "deploy.env")


def read_group(path):
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError as e:
        raise Bad("读不了 %s：%s" % (path, e))
    found = [l for l in lines if l.startswith("MODEL_GROUP=")]
    if len(found) != 1:
        raise Bad("%s 里有 %d 行 MODEL_GROUP=，应为 1 行" % (path, len(found)))
    v = found[0][len("MODEL_GROUP="):].strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        v = v[1:-1]
    return v


def write_group(path, value):
    lines = open(path, encoding="utf-8").read().split("\n")
    lines = ["MODEL_GROUP=%s" % value if l.startswith("MODEL_GROUP=") else l for l in lines]
    mode = os.stat(path).st_mode & 0o7777
    tmp = path + ".arrange.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def derived_folder(panel, sec):
    """GRAFANA_FOLDER as the panel's own names.py derives it from its deploy.env."""
    names = os.path.join(sec["dir"], "scripts", "names.py")
    p = subprocess.run([sys.executable, names, "derive", "--panel", panel, "--env-file", env_file(sec), "--format", "json"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        return "（推不出来：%s）" % p.stderr.decode("utf-8", "replace").strip()[:200]
    return json.loads(p.stdout.decode("utf-8"))["GRAFANA_FOLDER"]


def env_values(path):
    """The Grafana values of a deploy.env as bash sees it; the environment wins."""
    vals = {}
    if path:
        script = 'set -a; . "$1" >/dev/null 2>&1; shift; for k in "$@"; do printf "%s=%s\\0" "$k" "${!k-}"; done'
        p = subprocess.run(["bash", "-c", script, "_", path] + list(GRAFANA_KEYS), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env={"HOME": home(), "PATH": os.environ.get("PATH", "/usr/bin:/bin")})
        if p.returncode != 0 or not os.path.isfile(path):
            raise Bad("读不了 %s" % path)
        for item in p.stdout.decode("utf-8").split("\0"):
            k, eq, v = item.partition("=")
            if eq:
                vals[k] = v
    for k in GRAFANA_KEYS:
        if os.environ.get(k):
            vals[k] = os.environ[k]
    return vals


# ---------------- Grafana ----------------

class Grafana:
    def __init__(self, env_from):
        v = env_values(env_from)
        self.url = (v.get("GRAFANA_URL") or "").rstrip("/")
        if not self.url:
            raise Bad("没有 GRAFANA_URL：用 --env-from 给出含 Grafana 地址和账号的 deploy.env")
        if v.get("GRAFANA_TOKEN"):
            self.auth = "Bearer " + v["GRAFANA_TOKEN"]
        else:
            self.auth = "Basic " + base64.b64encode(("%s:%s" % (v.get("GRAFANA_USER", "admin"), v.get("GRAFANA_PASSWORD", ""))).encode()).decode()
        self._paths = {}

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.url + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Accept": "application/json", "Authorization": self.auth})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode("utf-8")
                return r.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw[:200]
        except urllib.error.URLError as e:
            raise Bad("连不上 Grafana %s：%s" % (self.url, e.reason))

    def must(self, method, path, body=None):
        code, res = self.call(method, path, body)
        if code != 200:
            raise Bad("Grafana %s %s：HTTP %s %s" % (method, path, code, str(res)[:200]))
        return res

    def top(self):
        return self.must("GET", "/api/folders?limit=1000")

    def children(self, uid):
        return self.must("GET", "/api/folders?limit=1000&parentUid=" + uid) if uid else []

    def folder(self, uid):
        code, res = self.call("GET", "/api/folders/" + uid)
        if code == 404:
            return None
        if code != 200:
            raise Bad("Grafana GET /api/folders/%s：HTTP %s %s" % (uid, code, str(res)[:200]))
        return res

    def path_of(self, uid):
        if not uid:
            return "General"
        if uid not in self._paths:
            f = self.folder(uid)
            self._paths[uid] = "/".join([p["title"] for p in f.get("parents") or []] + [f["title"]]) if f else "（已删的文件夹 %s）" % uid
        return self._paths[uid]

    def counts(self, uid):
        return self.must("GET", "/api/folders/%s/counts" % uid)

    def board(self, uid):
        code, res = self.call("GET", "/api/dashboards/uid/" + uid)
        if code == 404:
            return None
        if code != 200:
            raise Bad("Grafana GET /api/dashboards/uid/%s：HTTP %s %s" % (uid, code, str(res)[:200]))
        return res

    def save(self, dashboard, folder_uid):
        return self.must("POST", "/api/dashboards/db", {"dashboard": dashboard, "folderUid": folder_uid, "overwrite": False,
                                                         "message": "arrange_boards.py"})


def board_uids(entry):
    """(uid, is a panel-1 dashboard) of a registered cluster: panel 1's two from its panel-1 entry, panel 2's one."""
    p1 = (entry.get("panel1") or {}).get("values") or {}
    p2 = (entry.get("panel2") or {}).get("values") or {}
    out = [(p1.get(k), True) for k in ("PANEL1_P_UID", "PANEL1_D_UID")] + [(p2.get("GRAFANA_BOARD_UID"), False)]
    return [(u, one) for u, one in out if u]


# ---------------- plan ----------------

def compute(g, rows, reg):
    """The changes apply would make, in order, as dicts with a "line" to print; plus notes."""
    acts, notes = [], []
    clusters = reg["clusters"]
    present = [r for r in rows if r["cluster"] in clusters]
    for r in present:
        e = clusters[r["cluster"]]
        files = []
        for panel in ("panel1", "panel2"):
            sec = e.get(panel)
            if sec:
                v = read_group(env_file(sec))
                if v != r["child"]:
                    files.append({"panel": panel, "path": env_file(sec), "old": v})
        old = e.get("model_group") or ""
        if old != r["child"] or files:
            where = (["注册表"] if old != r["child"] else []) + [f["path"] for f in files]
            acts.append({"kind": "group", "cluster": r["cluster"], "old": old, "new": r["child"], "registry": old != r["child"], "files": files,
                         "line": "改 模型文件夹 %s：%s → %s（%s）" % (r["cluster"], old or "（空）", r["child"], "；".join(where))})
    top = g.top()
    parents = {}
    for r in rows:
        if r["parent"] in parents:
            continue
        same = [f for f in top if f["title"] == r["parent"]]
        if len(same) > 1:
            raise Bad("顶层有 %d 个名为 %s 的文件夹（uid %s）：上一层文件夹只能有一个，先在 Grafana 里把它们合成一个；什么都没有改"
                      % (len(same), r["parent"], "、".join(f["uid"] for f in same)))
        parents[r["parent"]] = same[0]["uid"] if same else None
    needed = []
    for r in present:
        if r["parent"] not in needed:
            needed.append(r["parent"])
    for p in needed:
        if parents[p] is None:
            acts.append({"kind": "mkdir", "title": p, "parent": None, "line": "建 文件夹 %s（顶层）" % p})
    kids = {p: g.children(u) for p, u in parents.items() if u}
    children = {}
    for r in present:
        key = (r["parent"], r["child"])
        if key in children:
            continue
        same = [f for f in kids.get(r["parent"], []) if f["title"] == r["child"]]
        if len(same) > 1:
            raise Bad("%s 下有 %d 个名为 %s 的子文件夹（uid %s）：先在 Grafana 里把它们合成一个；什么都没有改"
                      % (r["parent"], len(same), r["child"], "、".join(f["uid"] for f in same)))
        children[key] = same[0]["uid"] if same else None
        if not same:
            acts.append({"kind": "mkdir", "title": r["child"], "parent": r["parent"], "line": "建 文件夹 %s/%s" % key})
    leaving = {}
    for r in present:
        target = children[(r["parent"], r["child"])]
        for uid, panel1 in board_uids(clusters[r["cluster"]]):
            b = g.board(uid)
            if b is None:
                notes.append("看板 %s 不在 Grafana 里（没装或已删），跳过" % uid)
                continue
            meta = b.get("meta") or {}
            if meta.get("provisioned") or meta.get("provisionedExternalId"):
                raise Bad("看板 %s 是文件预置的，不能用 API 挪；什么都没有改" % uid)
            title = b["dashboard"].get("title", "")
            new_title, tail = title, "（%s）" % r["cluster"]
            if r["title"] and panel1 and title.endswith(tail):
                new_title = title[:-len(tail)] + "（%s）" % r["title"]
            here = meta.get("folderUid") or ""
            if target is None or here != target or new_title != title:
                if target is None or here != target:
                    leaving[here] = leaving.get(here, 0) + 1
                line = "挪 看板 %s「%s」：%s → %s/%s" % (uid, title, g.path_of(here), r["parent"], r["child"])
                if target is not None and here == target:
                    line = "改 看板 %s 的标题「%s」" % (uid, title)
                if new_title != title:
                    line += "；标题改为「%s」" % new_title
                acts.append({"kind": "move", "uid": uid, "from": here, "from_path": g.path_of(here), "old_title": title, "new_title": new_title,
                             "parent": r["parent"], "child": r["child"], "line": line})
    child_titles = {r["child"] for r in rows}
    in_use = {u for u in list(parents.values()) + list(children.values()) if u}
    cands = [(f, f["title"]) for f in top if f["title"] in child_titles and f["uid"] not in in_use]
    for p, u in parents.items():
        registered = {r["child"] for r in present if r["parent"] == p}
        for f in kids.get(p, []):
            if f["title"] in child_titles and f["title"] not in registered:
                cands.append((f, "%s/%s" % (p, f["title"])))
    for f, shown in cands:
        c = g.counts(f["uid"])
        rest = dict(c, dashboard=c.get("dashboard", 0) - leaving.get(f["uid"], 0))
        if not any(rest.get(k, 0) for k in ("dashboard", "folder", "alertrule", "librarypanel")):
            acts.append({"kind": "rmdir", "uid": f["uid"], "title": f["title"], "parentUid": f.get("parentUid") or "", "path": shown,
                         "line": "删 空文件夹 %s（uid %s）" % (shown, f["uid"])})
        else:
            notes.append("文件夹 %s（uid %s）里还有别的：看板 %d、子文件夹 %d、告警规则 %d、库面板 %d，不删"
                         % (shown, f["uid"], rest.get("dashboard", 0), rest.get("folder", 0), rest.get("alertrule", 0), rest.get("librarypanel", 0)))
    return acts, notes, present


def header(rows, present):
    names = [r["cluster"] for r in present]
    later = [r["cluster"] for r in rows if r not in present]
    print("== 对照表 %s：%d 个集群；已登记的 %d 个：%s%s" % (os.path.basename(LAYOUT), len(rows), len(names), "、".join(names) or "（无）",
                                                    "；还没登记的：%s" % "、".join(later) if later else ""))


def setup(args):
    env_from = None
    rest = list(args)
    if "--env-from" in rest:
        i = rest.index("--env-from")
        if i + 1 >= len(rest):
            raise Bad("--env-from 后面要写 deploy.env 的路径")
        env_from = rest[i + 1]
        del rest[i:i + 2]
    return Grafana(env_from), rest


def cmd_folder(args):
    if len(args) != 1:
        raise Bad("用法：arrange_boards.py folder <集群名>")
    for r in load_layout():
        if r["cluster"] == args[0]:
            print(r["child"])
            return 0
    raise Bad("对照表 %s 里没有集群 %s" % (LAYOUT, args[0]))


def cmd_plan(args):
    g, rest = setup(args)
    if rest:
        raise Bad("用法：arrange_boards.py plan --env-from <deploy.env>")
    rows, reg = load_layout(), load_registry()
    acts, notes, present = compute(g, rows, reg)
    header(rows, present)
    for a in acts:
        print(a["line"])
    for n in notes:
        print("INFO " + n)
    print("RESULT: OK（没有要改的：看板都在对照表的位置）" if not acts else "RESULT: 要改 %d 项（plan 只看不改；执行 apply 才改）" % len(acts))
    return 0


def save_manifest(bdir, man):
    tmp = os.path.join(bdir, "manifest.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(bdir, "manifest.json"))


def cmd_apply(args):
    g, rest = setup(args)
    if rest:
        raise Bad("用法：arrange_boards.py apply --env-from <deploy.env>")
    rows, reg = load_layout(), load_registry()
    acts, notes, present = compute(g, rows, reg)
    header(rows, present)
    if not acts:
        for n in notes:
            print("INFO " + n)
        print("RESULT: OK（没有要改的：看板都在对照表的位置）")
        return 0
    bdir = os.path.join(home(), "vllm-pd-arrange-backup", datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(bdir)
    os.chmod(bdir, 0o700)
    shutil.copy2(registry_path(), os.path.join(bdir, "registry.json"))
    man = {"made": datetime.datetime.now().isoformat(timespec="seconds"), "groups": [], "created": [], "moves": [], "deleted": []}
    for a in acts:
        for f in a.get("files", []):
            name = "%s-%s-deploy.env" % (a["cluster"], f["panel"])
            shutil.copy2(f["path"], os.path.join(bdir, name))
    save_manifest(bdir, man)
    print("== 备份到 %s（注册表、要改的 deploy.env；manifest.json 记下每一项改动）" % bdir)
    created = {}
    for a in acts:
        if a["kind"] == "group":
            if a["registry"]:
                with locked_registry() as r:
                    r["clusters"][a["cluster"]]["model_group"] = a["new"]
            for f in a["files"]:
                write_group(f["path"], a["new"])
            man["groups"].append({k: a[k] for k in ("cluster", "old", "new", "registry", "files")})
        elif a["kind"] == "mkdir":
            body = {"title": a["title"]}
            if a["parent"]:
                body["parentUid"] = created.get((None, a["parent"])) or next(f["uid"] for f in g.top() if f["title"] == a["parent"])
            uid = g.must("POST", "/api/folders", body)["uid"]
            created[(a["parent"], a["title"])] = uid
            man["created"].append({"uid": uid, "title": a["title"], "parent": a["parent"]})
        elif a["kind"] == "move":
            target = created.get((a["parent"], a["child"]))
            if target is None:
                puid = created.get((None, a["parent"])) or next(f["uid"] for f in g.top() if f["title"] == a["parent"])
                target = next(f["uid"] for f in g.children(puid) if f["title"] == a["child"])
            b = g.board(a["uid"])
            d = b["dashboard"]
            d["title"] = a["new_title"]
            g.save(d, target)
            man["moves"].append({"uid": a["uid"], "from": a["from"], "from_path": a["from_path"], "to": target,
                                 "old_title": a["old_title"], "new_title": a["new_title"]})
        elif a["kind"] == "rmdir":
            c = g.counts(a["uid"])
            if any(c.get(k, 0) for k in ("dashboard", "folder", "alertrule", "librarypanel")):
                print("没有删 文件夹 %s（uid %s）：里面还有东西 %s" % (a["path"], a["uid"], c))
                continue
            g.must("DELETE", "/api/folders/" + a["uid"])
            man["deleted"].append({"uid": a["uid"], "title": a["title"], "parentUid": a["parentUid"], "path": a["path"]})
        save_manifest(bdir, man)
        print("已" + a["line"])
    for n in notes:
        print("INFO " + n)
    print("== 核对")
    reg = load_registry()
    bad = 0
    for a in [x for x in acts if x["kind"] == "group"]:
        e = reg["clusters"][a["cluster"]]
        got = [("注册表", e.get("model_group"))]
        for panel in ("panel1", "panel2"):
            if e.get(panel):
                got.append((env_file(e[panel]) + " 推出的文件夹", derived_folder(panel, e[panel])))
        for where, v in got:
            ok = v == a["new"]
            bad += not ok
            print("%s 集群 %s：%s 是 %s" % ("OK  " if ok else "FAIL", a["cluster"], where, v))
    again, _, _ = compute(g, rows, reg)
    bad += len(again)
    print("%s 再看一遍：%s" % ("OK  " if not again else "FAIL", "没有要改的" if not again else "还有 %d 项：%s" % (len(again), "；".join(x["line"] for x in again))))
    undo = "python3 %s undo %s --env-from <同一个 deploy.env>" % (os.path.basename(__file__), bdir)
    if bad:
        print("RESULT: FAIL（改了 %d 项，核对有 %d 项不对，见上面的 FAIL 行；回退：%s）" % (len(acts), bad, undo))
        return 1
    print("RESULT: OK（改了 %d 项；回退：%s）" % (len(acts), undo))
    return 0


def cmd_undo(args):
    g, rest = setup(args)
    if len(rest) != 1:
        raise Bad("用法：arrange_boards.py undo <备份目录> --env-from <deploy.env>")
    bdir = rest[0]
    try:
        man = json.load(open(os.path.join(bdir, "manifest.json"), encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise Bad("读不了 %s/manifest.json：%s" % (bdir, e))
    print("== 按 %s 回退" % bdir)
    done, left = 0, 0
    remade = {}
    for m in reversed(man["moves"]):
        b = g.board(m["uid"])
        if b is None:
            print("跳过 看板 %s：不在 Grafana 里了" % m["uid"])
            left += 1
            continue
        here = (b.get("meta") or {}).get("folderUid") or ""
        if here != m["to"]:
            print("跳过 看板 %s：已经不在 apply 放的位置（现在在 %s）" % (m["uid"], g.path_of(here)))
            left += 1
            continue
        dest = m["from"]
        if dest and g.folder(dest) is None:
            gone = next((x for x in man["deleted"] if x["uid"] == dest), None)
            if dest not in remade:
                body = {"title": gone["title"] if gone else m["from_path"].split("/")[-1]}
                if gone and gone.get("parentUid"):
                    body["parentUid"] = gone["parentUid"]
                remade[dest] = g.must("POST", "/api/folders", body)["uid"]
                print("已重建 文件夹 %s（apply 删掉的那个，新 uid %s）" % (m["from_path"], remade[dest]))
            dest = remade[dest]
        d = b["dashboard"]
        d["title"] = m["old_title"]
        g.save(d, dest)
        done += 1
        print("已挪回 看板 %s「%s」：%s" % (m["uid"], m["old_title"], m["from_path"]))
    for a in reversed(man["groups"]):
        if a["registry"]:
            with locked_registry() as r:
                e = r["clusters"].get(a["cluster"])
                if e and e.get("model_group") == a["new"]:
                    e["model_group"] = a["old"]
                    done += 1
                    print("已改回 注册表里集群 %s 的模型文件夹：%s" % (a["cluster"], a["old"]))
                else:
                    left += 1
                    print("跳过 注册表里集群 %s 的模型文件夹：现在不是 %s" % (a["cluster"], a["new"]))
        for f in a["files"]:
            if os.path.isfile(f["path"]) and read_group(f["path"]) == a["new"]:
                write_group(f["path"], f["old"])
                done += 1
                print("已改回 %s：MODEL_GROUP=%s" % (f["path"], f["old"]))
            else:
                left += 1
                print("跳过 %s：现在不是 MODEL_GROUP=%s" % (f["path"], a["new"]))
    for c in reversed(man["created"]):
        if g.folder(c["uid"]) is None:
            continue
        n = g.counts(c["uid"])
        if any(n.get(k, 0) for k in ("dashboard", "folder", "alertrule", "librarypanel")):
            left += 1
            print("跳过 文件夹 %s（uid %s）：里面还有东西，不删" % (c["title"], c["uid"]))
            continue
        g.must("DELETE", "/api/folders/" + c["uid"])
        done += 1
        print("已删 apply 建的文件夹 %s%s（uid %s）" % (c["parent"] + "/" if c["parent"] else "", c["title"], c["uid"]))
    print("RESULT: %s（回退了 %d 项%s）" % ("OK" if not left else "OK，有跳过的项", done, "；跳过 %d 项，见上面的「跳过」行" % left if left else ""))
    return 0


def sha8(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()[:8]


def cmd_state(args):
    g, rest = setup(args)
    skip = None
    if len(rest) == 2 and rest[0] == "--except":
        skip = rest[1]
    elif rest:
        raise Bad("用法：arrange_boards.py state --env-from <deploy.env> [--except <集群名>]")
    reg = load_registry()
    for c in sorted(reg["clusters"]):
        if c == skip:
            continue
        e = reg["clusters"][c]
        print("集群 %s 模型文件夹 %s" % (c, e.get("model_group") or "（空）"))
        for panel in ("panel1", "panel2"):
            sec = e.get(panel)
            for rel in FILES[panel] if sec else ():
                p = os.path.join(sec["dir"], rel)
                if os.path.isfile(p):
                    print("文件 %s %s" % (p, sha8(p)))
        for uid, _ in board_uids(e):
            b = g.board(uid)
            if b is None:
                print("看板 %s 不在 Grafana 里" % uid)
            else:
                print("看板 %s 版本 %s 在 %s 标题「%s」" % (uid, b["dashboard"].get("version"), g.path_of((b.get("meta") or {}).get("folderUid")), b["dashboard"].get("title")))
    p = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise Bad("docker ps 失败：%s" % p.stderr.decode("utf-8", "replace").strip()[:200])
    for n in sorted(p.stdout.decode().split()):
        q = subprocess.run(["docker", "inspect", "-f", "{{.State.StartedAt}}", n], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print("容器 %s 启动于 %s" % (n, q.stdout.decode().strip()))
    return 0


def main():
    utf8_stdout()
    cmds = {"folder": cmd_folder, "plan": cmd_plan, "apply": cmd_apply, "undo": cmd_undo, "state": cmd_state}
    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    try:
        return cmds[sys.argv[1]](sys.argv[2:]) or 0
    except Bad as e:
        print("错误：%s" % e, file=sys.stderr)
        print("RESULT: FAIL", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
