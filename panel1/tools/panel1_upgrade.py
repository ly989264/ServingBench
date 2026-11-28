#!/usr/bin/env python3
"""Upgrade the installed panel-1 dashboards from deploy pack v1.0 or v1.1 to v1.2:
- the stat panel「服务实例在线」(panel 807) shows P, D and Mooncake as「在线数 / 应有数」at all times, green when all
  are online, orange when some are, red at 0 (the change of v1.1);
- every data panel of the visible rows carries the description of deploy pack v1.2 (the「?」next to its title).
Queries and layout of the other panels are not changed.

Usage:
    python3 panel1_upgrade.py check --env-from <deploy.env holding the Grafana values>
    python3 panel1_upgrade.py apply --env-from <deploy.env holding the Grafana values>
    python3 panel1_upgrade.py undo <backup folder> --env-from <deploy.env holding the Grafana values>
    python3 panel1_upgrade.py envfile

check  read-only. One line per cluster of the registry, and one per panel-1 dashboard found in Grafana that the
       registry does not name: the version of each dashboard (v1.0 / v1.1 / v1.2 / 被改过), time-series panels
       without a tooltip height limit, the online and expected counts of P, D and Mooncake from the cluster's
       Prometheus (through Grafana), and whether the expected counts equal the cluster's inventory.json.
       A dashboard is v1.0 when panel 807 has the v1.0 queries and no panel of the pack has a description, v1.1
       when panel 807 has the v1.2 queries and no description is set, v1.2 when panel 807 has the v1.2 queries and
       every panel of the pack has the pack's description; anything else is 被改过. Panels are matched by id and
       title.
apply  for every v1.0 or v1.1 dashboard: panel 807 in its v1.0 form is replaced by v1.2's (same id and position),
       the matched panels get the pack's descriptions, time-series panels without a tooltip height limit get 360
       pixels, and the dashboard is saved with the same uid, folder and title. A dashboard that is 被改过 or has
       no panel 807 is left alone and reported.
       The dashboards are copied first into ~/vllm-pd-panel1-v1.2-backup/<time>/ (manifest.json lists them).
       Running it again when everything is v1.2 changes nothing and makes no backup.
undo   writes the copies of one apply back. A dashboard saved again since that apply is reported and left alone.
envfile  read-only. Prints the path of the first deploy.env of the registry (panel one of each cluster, then panel
       two) whose Grafana address and account log in to Grafana; nothing on stdout and exit code 1 when none does.

Panel 807 and the descriptions of v1.2 are read from vllm-pd-grafana-panel1.tar.gz next to this script.
Grafana address and account: GRAFANA_URL, GRAFANA_USER, GRAFANA_PASSWORD (or GRAFANA_TOKEN) from the environment,
else from --env-from. Registry: VLLM_PD_REGISTRY, else ~/.vllm-pd-clusters.json (read only).
Standard library only, Python 3.6 or later.
"""
import base64
import copy
import datetime
import io
import json
import os
import subprocess
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PACK_TAR = os.path.join(HERE, "vllm-pd-grafana-panel1.tar.gz")
GRAFANA_KEYS = ("GRAFANA_URL", "GRAFANA_USER", "GRAFANA_PASSWORD", "GRAFANA_TOKEN")
PANEL_ID, PANEL_TITLE = 807, "服务实例在线"
OLD_EXPRS = [
    'count(vllm:num_requests_running{role="prefill", model_name="$model_name"})',
    'count(vllm:num_requests_running{role="decode", model_name="$model_name"})',
    'sum(up{job="mooncake-master"})',
]
JOBS = (("P", "vllm-prefill", "prefill"), ("D", "vllm-decode", "decode"), ("Mooncake", "mooncake-master", None))
TOOLTIP_TYPES = ("timeseries",)
MAX_HEIGHT = 360
SEARCH_TITLE = "vLLM 服务侧监控面板"


class Bad(Exception):
    pass


def utf8_stdout():
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
            setattr(sys, name, io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace", line_buffering=True))


def home():
    return os.environ.get("HOME") or os.path.expanduser("~")


def registry_path():
    return os.environ.get("VLLM_PD_REGISTRY") or os.path.join(home(), ".vllm-pd-clusters.json")


def load_registry():
    path = registry_path()
    if not os.path.exists(path):
        raise Bad("没有集群注册表 %s" % path)
    with open(path, encoding="utf-8") as f:
        reg = json.load(f)
    if reg.get("format") != "vllm-pd-clusters":
        raise Bad("%s 不是集群注册表" % path)
    return reg.get("clusters") or {}


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

    def board(self, uid):
        code, res = self.call("GET", "/api/dashboards/uid/" + urllib.parse.quote(uid))
        if code == 404:
            return None
        if code != 200:
            raise Bad("Grafana GET /api/dashboards/uid/%s：HTTP %s %s" % (uid, code, str(res)[:200]))
        return res

    def path_of(self, uid):
        if not uid:
            return "General"
        if uid not in self._paths:
            code, f = self.call("GET", "/api/folders/" + urllib.parse.quote(uid))
            self._paths[uid] = "/".join([p["title"] for p in f.get("parents") or []] + [f["title"]]) if code == 200 else uid
        return self._paths[uid]

    def query(self, ds_uid, expr):
        """{job: value} of an instant PromQL query grouped by job, through the data source proxy; None when it fails."""
        path = "/api/datasources/proxy/uid/%s/api/v1/query?%s" % (urllib.parse.quote(ds_uid), urllib.parse.urlencode({"query": expr}))
        code, res = self.call("GET", path)
        if code != 200 or not isinstance(res, dict) or res.get("status") != "success":
            return None
        return {r["metric"].get("job", ""): int(float(r["value"][1])) for r in res["data"]["result"]}


# ---------------- dashboards ----------------

class Pack:
    """Deploy pack v1.2: panel 807 (the same on both dashboards) and {id: (title, description)} per side."""

    def __init__(self):
        if not os.path.isfile(PACK_TAR):
            raise Bad("没有 %s（部署包 v1.2 应和本脚本放在同一个目录）" % PACK_TAR)
        found, self.texts = [], {}
        with tarfile.open(PACK_TAR) as t:
            for side, name in (("P", "prefill"), ("D", "decode")):
                d = json.load(t.extractfile("vllm-pd-grafana-panel1/grafana/dashboards/vllm-pd-%s.json" % name))
                found += [p for p in d["panels"] if p.get("id") == PANEL_ID and p.get("title") == PANEL_TITLE]
                self.texts[side] = {p["id"]: (p.get("title", ""), p["description"]) for p in all_panels(d) if p.get("description")}
        if len(found) != 2 or found[0] != found[1] or exprs(found[0]) == OLD_EXPRS or not found[0].get("description") \
                or min(len(v) for v in self.texts.values()) < 50:
            raise Bad("%s 不是部署包 v1.2" % PACK_TAR)
        self.panel = found[0]

    def side_of(self, d, side):
        """P or D: the side given by the registry, else the one whose panels the dashboard matches most."""
        if side in self.texts:
            return side
        return max(self.texts, key=lambda s: len(matched(d, self.texts[s])))


def matched(d, texts):
    """The panels of dashboard d whose id and title are those of a described panel of the pack."""
    return [p for p in all_panels(d) if p.get("id") in texts and p.get("title") == texts[p["id"]][0]]


def exprs(panel):
    return [t.get("expr") for t in panel.get("targets") or []]


def top_panel(d):
    hits = [p for p in d.get("panels") or [] if p.get("id") == PANEL_ID and p.get("title") == PANEL_TITLE]
    return hits[0] if len(hits) == 1 else None


def all_panels(d):
    for p in d.get("panels") or []:
        yield p
        for q in p.get("panels") or []:
            yield q


def no_height(d):
    """Time-series panels whose tooltip has no height limit."""
    out = []
    for p in all_panels(d):
        tip = (p.get("options") or {}).get("tooltip")
        if p.get("type") in TOOLTIP_TYPES and isinstance(tip, dict) and tip.get("mode") != "hidden" and not tip.get("maxHeight"):
            out.append(p)
    return out


def state(d, pack, side):
    """(version, reason): v1.0 / v1.1 / v1.2 / 被改过 / 没有 panel 807, and for 被改过 what differs."""
    p = top_panel(d)
    if p is None:
        return "没有 panel 807", ""
    texts = pack.texts[pack.side_of(d, side)]
    ps = matched(d, texts)
    none = sum(1 for q in ps if not q.get("description"))
    full = sum(1 for q in ps if q.get("description") == texts[q["id"]][1])
    if exprs(p) == OLD_EXPRS and none == len(ps):
        return "v1.0", ""
    if exprs(p) == exprs(pack.panel):
        if none == len(ps):
            return "v1.1", ""
        if full == len(ps):
            return "v1.2", ""
    if exprs(p) not in (OLD_EXPRS, exprs(pack.panel)):
        return "被改过", "panel 807 的查询与 v1.0、v1.2 都不同"
    return "被改过", "panel 807 是 %s 的查询；说明与 v1.2 相同 %d 个，没有说明 %d 个，与 v1.2 不同 %d 个" % (
        "v1.0" if exprs(p) == OLD_EXPRS else "v1.2", full, none, len(ps) - full - none)


def upgraded(d, pack, side):
    """A copy of dashboard d (v1.0 or v1.1) with panel 807 of v1.2 (same id and position), the pack's descriptions and
    tooltip height limits filled in."""
    d = copy.deepcopy(d)
    texts = pack.texts[pack.side_of(d, side)]
    for i, p in enumerate(d["panels"]):
        if p.get("id") == PANEL_ID and p.get("title") == PANEL_TITLE and exprs(p) == OLD_EXPRS:
            q = copy.deepcopy(pack.panel)
            q["gridPos"] = p["gridPos"]
            d["panels"][i] = q
    for p in matched(d, texts):
        p["description"] = texts[p["id"]][1]
    for p in no_height(d):
        p["options"]["tooltip"]["maxHeight"] = MAX_HEIGHT
    return d


# ---------------- inventory ----------------

def expected_from_inventory(panel1_dir):
    """{job: count} from the cluster's inventory.json: API servers per side, 1 Mooncake master; None if unreadable."""
    try:
        with open(os.path.join(panel1_dir, "prometheus", "inventory.json"), encoding="utf-8") as f:
            inv = json.load(f)
        out = {}
        for _, job, side in JOBS:
            if side is None:
                out[job] = 1 if inv.get("mooncake_master") else 0
            elif side in inv:
                out[job] = len(inv[side]["api_servers"])
            else:
                out[job] = sum(1 for s in inv.get("api_servers") or [] if s.get("role") == side)
        return out
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


# ---------------- discovery ----------------

def discover(g):
    """[(row name, cluster entry or None, [(side, uid, board or None)])] for the registry, then unregistered boards."""
    clusters = load_registry()
    rows, known = [], set()
    for c in sorted(clusters):
        p1 = (clusters[c].get("panel1") or {})
        vals = p1.get("values") or {}
        uids = [(s, vals.get(k)) for s, k in (("P", "PANEL1_P_UID"), ("D", "PANEL1_D_UID")) if vals.get(k)]
        if not uids:
            rows.append((c, None, []))
            continue
        known.update(u for _, u in uids)
        rows.append((c, p1, [(s, u, g.board(u)) for s, u in uids]))
    hits = g.must("GET", "/api/search?" + urllib.parse.urlencode({"type": "dash-db", "query": SEARCH_TITLE, "limit": 5000}))
    for h in sorted(hits, key=lambda h: h.get("uid", "")):
        if h.get("uid") in known:
            continue
        b = g.board(h["uid"])
        if b and top_panel(b["dashboard"]) is not None:
            rows.append(("（未登记）", None, [("看板", h["uid"], b)]))
    return rows


def grafana_version(g):
    code, res = g.call("GET", "/api/health")
    return res.get("version", "?") if code == 200 and isinstance(res, dict) else "?"


# ---------------- commands ----------------

def cmd_check(g):
    pack = Pack()
    rows = discover(g)
    print("Grafana %s；注册表 %s：%d 个集群" % (grafana_version(g), registry_path(), len(load_registry())))
    n = {"v1.0": 0, "v1.1": 0, "v1.2": 0, "被改过": 0, "没有 panel 807": 0, "看板不在": 0}
    clusters, mismatch, unreachable = 0, 0, 0
    for name, p1, boards in rows:
        if p1 is None and not boards:
            print("%s：没有面板一，跳过" % name)
            continue
        parts, folders = [], set()
        for side, uid, b in boards:
            if b is None:
                parts.append("%s 不在（%s）" % (side, uid))
                n["看板不在"] += 1
                continue
            st, why = state(b["dashboard"], pack, side)
            n[st] += 1
            gaps = len(no_height(b["dashboard"]))
            got = len(matched(b["dashboard"], pack.texts[pack.side_of(b["dashboard"], side)]))
            want = len(pack.texts[pack.side_of(b["dashboard"], side)])
            parts.append("%s %s%s%s%s" % (side, st, "（%s）" % why if why else "", "，对上 %d/%d 个 panel" % (got, want) if got < want else "",
                                          "，缺高度上限 %d" % gaps if gaps else ""))
            folders.add(g.path_of(b["meta"].get("folderUid", "")))
        line = "%s：%s；文件夹 %s" % (name, "，".join(parts), "、".join(sorted(folders)) or "-")
        if p1 is not None:
            clusters += 1
            ds = (p1.get("values") or {}).get("PROMETHEUS_DS_UID", "")
            on = g.query(ds, 'sum by (job) (up{job=~"vllm-prefill|vllm-decode|mooncake-master"})')
            ex = g.query(ds, 'count by (job) (up{job=~"vllm-prefill|vllm-decode|mooncake-master"})')
            if on is None or ex is None:
                line += "；Prometheus 查不到（数据源 %s）" % ds
                unreachable += 1
            else:
                line += "；在线/应有 " + " ".join("%s %s/%s" % (s, on.get(j, "-"), ex.get(j, "-")) for s, j, _ in JOBS)
                inv = expected_from_inventory(p1.get("dir", ""))
                if inv is None:
                    line += "；清单读不了"
                    mismatch += 1
                elif all(inv[j] == ex.get(j, 0) for _, j, _ in JOBS):
                    line += "；应有数与清单一致"
                else:
                    line += "；应有数与清单不同（清单 %s）" % " ".join("%s %d" % (s, inv[j]) for s, j, _ in JOBS)
                    mismatch += 1
        print(line)
    total = sum(n.values())
    print("RESULT: %s：%d 个集群 %d 张看板，v1.0 %d，v1.1 %d，v1.2 %d，被改过 %d，没有 panel 807 %d，看板不在 %d；应有数与清单不同 %d，Prometheus 查不到 %d"
          % ("OK" if n["被改过"] + n["没有 panel 807"] + n["看板不在"] + mismatch + unreachable == 0 else "有差异",
             clusters, total, n["v1.0"], n["v1.1"], n["v1.2"], n["被改过"], n["没有 panel 807"], n["看板不在"], mismatch, unreachable))


def cmd_apply(g):
    pack = Pack()
    todo, skipped = [], []
    for name, p1, boards in discover(g):
        for side, uid, b in boards:
            if b is None:
                continue
            st, why = state(b["dashboard"], pack, side)
            if st in ("被改过", "没有 panel 807"):
                skipped.append("%s %s（%s）：%s%s，没有动" % (name, side, uid, st, "（%s）" % why if why else ""))
            elif st in ("v1.0", "v1.1") or no_height(b["dashboard"]):
                todo.append((name, side, uid, b))
    for s in skipped:
        print("跳过 " + s)
    if not todo:
        print("RESULT: OK（没有要改的看板%s）" % ("；跳过 %d 张" % len(skipped) if skipped else ""))
        return 0 if not skipped else 1
    bdir = os.path.join(home(), "vllm-pd-panel1-v1.2-backup", datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(bdir)
    manifest = []
    for name, side, uid, b in todo:
        with open(os.path.join(bdir, uid + ".json"), "w", encoding="utf-8") as f:
            json.dump(b, f, ensure_ascii=False, indent=2)
        manifest.append({"cluster": name, "side": side, "uid": uid, "folderUid": b["meta"].get("folderUid", ""),
                         "version_before": b["dashboard"].get("version"), "state_before": state(b["dashboard"], pack, side)[0]})
    print("== 备份到 %s（%d 张看板）" % (bdir, len(todo)))
    failed = 0
    for m, (name, side, uid, b) in zip(manifest, todo):
        d = upgraded(b["dashboard"], pack, side)
        code, res = g.call("POST", "/api/dashboards/db", {"dashboard": d, "folderUid": m["folderUid"], "overwrite": False,
                                                          "message": "面板一 v1.2：服务实例在线与 panel 说明"})
        after = g.board(uid)
        ok = code == 200 and after is not None and state(after["dashboard"], pack, side)[0] == "v1.2" and not no_height(after["dashboard"])
        m["version_after"] = after["dashboard"].get("version") if after else None
        if ok:
            print("已改 %s %s（%s）：%s → v1.2，版本 %s → %s，文件夹 %s" % (name, side, uid, m["state_before"], m["version_before"], m["version_after"],
                                                                          g.path_of(m["folderUid"])))
        else:
            failed += 1
            print("没改成 %s %s（%s）：HTTP %s %s" % (name, side, uid, code, str(res)[:160]))
        with open(os.path.join(bdir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
    print("RESULT: %s（改了 %d 张%s%s；回退：python3 panel1_upgrade.py undo %s --env-from <同一个 deploy.env>）"
          % ("OK" if not failed and not skipped else "有问题", len(todo) - failed, "，没改成 %d 张" % failed if failed else "",
             "，跳过 %d 张" % len(skipped) if skipped else "", bdir))
    return 1 if failed or skipped else 0


def cmd_undo(g, bdir):
    try:
        with open(os.path.join(bdir, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        raise Bad("%s 里没有可用的 manifest.json" % bdir)
    done, left = 0, 0
    for m in manifest:
        uid = m["uid"]
        cur = g.board(uid)
        if cur is None or m.get("version_after") is None or cur["dashboard"].get("version") != m["version_after"]:
            left += 1
            print("没动 %s %s（%s）：apply 之后又保存过，或不在了" % (m["cluster"], m["side"], uid))
            continue
        with open(os.path.join(bdir, uid + ".json"), encoding="utf-8") as f:
            d = json.load(f)["dashboard"]
        d["version"] = cur["dashboard"]["version"]
        code, res = g.call("POST", "/api/dashboards/db", {"dashboard": d, "folderUid": m["folderUid"], "overwrite": False,
                                                          "message": "面板一 v1.2 回退"})
        if code == 200:
            done += 1
            print("已恢复 %s %s（%s）" % (m["cluster"], m["side"], uid))
        else:
            left += 1
            print("没恢复 %s %s（%s）：HTTP %s %s" % (m["cluster"], m["side"], uid, code, str(res)[:160]))
    print("RESULT: %s（恢复 %d 张，没动 %d 张）" % ("OK" if not left else "有问题", done, left))
    return 1 if left else 0


def cmd_envfile():
    reg = load_registry()
    tried = []
    for panel in ("panel1", "panel2"):
        for c in sorted(reg):
            d = (reg[c].get(panel) or {}).get("dir")
            if not d:
                continue
            path = os.path.join(d, "deploy.env")
            if not os.path.isfile(path):
                tried.append("%s 不在" % path)
                continue
            try:
                g = Grafana(path)
                code, _ = g.call("GET", "/api/search?limit=1")
            except Bad as e:
                tried.append("%s：%s" % (path, e))
                continue
            if code == 200:
                print(path)
                return 0
            tried.append("%s：HTTP %s" % (path, code))
    for t in tried:
        print("没用上 " + t, file=sys.stderr)
    print("FAIL 注册表里没有能登录 Grafana 的 deploy.env", file=sys.stderr)
    return 1


def main():
    utf8_stdout()
    args = sys.argv[1:]
    env_from = None
    if "--env-from" in args:
        i = args.index("--env-from")
        if i + 1 >= len(args):
            raise SystemExit(__doc__)
        env_from = args[i + 1]
        del args[i:i + 2]
    try:
        if args == ["check"]:
            cmd_check(Grafana(env_from))
            return 0
        if args == ["envfile"]:
            return cmd_envfile()
        if args == ["apply"]:
            return cmd_apply(Grafana(env_from))
        if len(args) == 2 and args[0] == "undo":
            return cmd_undo(Grafana(env_from), args[1])
    except Bad as e:
        print("FAIL %s" % e)
        return 2
    raise SystemExit(__doc__)


if __name__ == "__main__":
    sys.exit(main())
