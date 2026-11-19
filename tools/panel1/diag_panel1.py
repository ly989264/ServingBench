#!/usr/bin/env python3
"""Read-only diagnosis for "the panel-1 dashboards changed, then went back": who else writes them, and how.

  python3 diag_panel1.py                                  # address and login from GRAFANA_URL, GRAFANA_USER,
                                                          # GRAFANA_PASSWORD or GRAFANA_TOKEN
  python3 diag_panel1.py --provisioning-dir grafana/provisioning   # also read the dashboard provider files

For each of the two dashboards it prints: how Grafana says the dashboard is managed (meta and, on Grafana 12,
the k8s annotations), the last 10 saved versions with who saved them and the save message, and whether the
dashboard as it is now has the four changes of update_panel1.py. Nothing is written. Python 3.6+, standard library.
"""
import argparse
import base64
import getpass
import glob
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

UIDS = ("vllm-pd-prefill", "vllm-pd-decode")
SAVE_MESSAGE = "panel 1: overview backup and instant stats"  # start of the message update_panel1.py saves with
API_VERSIONS = ("v1beta1", "v0alpha1", "v2beta1")
OVERVIEW_STATS = (801, 802, 803, 804, 805, 806, 808, 809, 810, 811, 813, 814, 815, 816, 817, 818)


def utf8_stdio():
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if hasattr(stream, "buffer") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            setattr(sys, name, io.TextIOWrapper(stream.buffer, encoding="utf-8", line_buffering=True))


def auth_header(args):
    token = args.token or os.environ.get("GRAFANA_TOKEN")
    if token:
        return "Bearer " + token
    user = args.user or os.environ.get("GRAFANA_USER")
    if not user:
        return None
    password = os.environ.get("GRAFANA_PASSWORD")
    if password is None:
        password = getpass.getpass("Grafana password for %s: " % user)
    return "Basic " + base64.b64encode(("%s:%s" % (user, password)).encode("utf-8")).decode("ascii")


def get(url, path, auth):
    """(status, parsed JSON or None); never raises for HTTP errors."""
    req = urllib.request.Request(url.rstrip("/") + path)
    req.add_header("Accept", "application/json")
    if auth:
        req.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, OSError) as e:
        sys.exit("连不上 Grafana %s：%s" % (url, getattr(e, "reason", e)))
    except ValueError:
        return 0, None


def short_time(t):
    return (t or "?").replace("T", " ")[:19]


def all_panels(d):
    for p in d.get("panels", []):
        yield p
        for q in p.get("panels") or []:
            yield q


def state_of(d):
    """Whether the dashboard has the four changes of update_panel1.py."""
    ps = {p.get("id"): p for p in all_panels(d)}
    rows = [i for i in (900, 950) if ps.get(i, {}).get("type") == "row"]
    merged = [i for i in range(101, 107) if i in ps and ps[i].get("title", "").endswith("p50 / p90 / p99")]
    sparks = sum(1 for i in OVERVIEW_STATS if i in ps and ps[i].get("type") == "stat"
                 and ps[i].get("options", {}).get("graphMode", "area") != "none")
    hm = next((p for p in ps.values() if p.get("type") == "heatmap" and p.get("title", "").startswith("AICore")), {})
    scheme = hm.get("options", {}).get("color", {}).get("scheme")
    tps = 111 in ps
    done = len(rows) == 2 and merged and sparks == 0 and scheme == "RdYlGn" and tps
    none = not rows and not merged and sparks > 0 and scheme != "RdYlGn" and not tps
    verdict = "改动后" if done else ("改动前" if none else "部分改动")
    return "%s（行900/950:%d/2 合并图:%d 总览带趋势线:%d 热力图:%s TPS:%s）" % (
        verdict, len(rows), len(merged), sparks, scheme, "有" if tps else "无")


def k8s_annotations(url, uid, auth, namespace):
    for v in API_VERSIONS:
        code, data = get(url, "/apis/dashboard.grafana.app/%s/namespaces/%s/dashboards/%s" % (v, namespace, uid), auth)
        if code == 200 and data:
            ann = (data.get("metadata") or {}).get("annotations") or {}
            keep = {k.replace("grafana.app/", ""): v2 for k, v2 in ann.items()
                    if any(s in k for s in ("managedBy", "managerId", "manager", "source", "updatedBy", "repo"))}
            return "%s %s" % (v, json.dumps(keep, ensure_ascii=False, separators=(",", ":")) if keep else "无管理来源注解")
    return "取不到（%s 都不通）" % "、".join(API_VERSIONS)


def versions(url, uid, auth):
    code, data = get(url, "/api/dashboards/uid/%s/versions?limit=10" % uid, auth)
    if code != 200 or data is None:
        return ["  版本历史：取不到（HTTP %s）" % code]
    items = data.get("versions", []) if isinstance(data, dict) else data
    seen, uniq = set(), []
    for v in sorted(items, key=lambda v: v.get("version", 0), reverse=True):
        if v.get("version") not in seen:  # Grafana 12 can list a version twice
            seen.add(v.get("version"))
            uniq.append(v)
    items = uniq[:10]
    out, ours = [], None
    for v in items:
        msg = (v.get("message") or "").strip()
        mark = ""
        if msg.startswith(SAVE_MESSAGE):
            mark, ours = " ←update_panel1.py", v.get("version")
        who = v.get("createdBy", "?")
        out.append("  v%s %s %s 「%s」%s" % (v.get("version"), short_time(v.get("created")),
                                          "Anonymous(预置加载)" if who == "Anonymous" else who, msg[:28], mark))
    if ours is not None:
        later = [v for v in items if v.get("version", 0) > ours]
        out.append("  update_panel1.py 保存的是 v%s，之后又有 %d 次保存" % (ours, len(later)))
    else:
        out.append("  最近 10 个版本里没有 update_panel1.py 的保存")
    return out


def poll(url, uids, auth, n=10, gap=1.0):
    """Read both dashboards n rounds, gap seconds apart, twice back to back per round (so a strict round-robin
    in front of two replicas cannot send all reads of one dashboard to the same replica): [(version, id, updated, uid)]."""
    seen = {u: [] for u in uids}
    for i in range(n):
        for u in [x for x in uids for _ in (0, 1)]:
            code, res = get(url, "/api/dashboards/uid/%s" % u, auth)
            if code == 200 and res:
                d, m = res["dashboard"], res.get("meta", {})
                seen[u].append((d.get("version"), d.get("id"), short_time(m.get("updated")), d.get("uid")))
            else:
                seen[u].append(("HTTP%s" % code, None, None, u))
        if i < n - 1:
            time.sleep(gap)
    return seen


def poll_lines(url, uid, reads, auth):
    """One line about the 10 reads; a second one naming the writers when the version went up."""
    vs = [r[0] for r in reads]
    keys = []
    for r in reads:
        if (r[0], r[1]) not in keys:
            keys.append((r[0], r[1]))
    ints = [v for v in vs if isinstance(v, int)]
    if len(keys) == 1:
        return ["  连读 %d 次（10 秒内）：version=%s id=%s，没有变化" % (len(reads), keys[0][0], keys[0][1])]
    monotonic = ints == sorted(ints) and len(set(r[1] for r in reads)) == 1
    if not monotonic:
        counts = ", ".join("v%s·id%s ×%d" % (v, i, sum(1 for r in reads if (r[0], r[1]) == (v, i))) for v, i in keys)
        return ["  连读 %d 次：%s，来回跳，可能有多个 Grafana 副本各用各的数据库" % (len(reads), counts)]
    first = ints[0]
    at = next(k for k, v in enumerate(vs) if v != first) + 1
    out = ["  连读 %d 次：version 由 %s 涨到 %s（第 %d 次读到），读的过程中有人在保存" % (len(reads), first, ints[-1], at)]
    code, data = get(url, "/api/dashboards/uid/%s/versions?limit=10" % uid, auth)
    items = (data.get("versions", []) if isinstance(data, dict) else data) if code == 200 and data else []
    new, latest = {}, None
    for v in items:
        if v.get("version", 0) > first and not (v.get("message") or "").startswith(SAVE_MESSAGE):
            new.setdefault(v.get("createdBy", "?"), set()).add(v.get("version"))
            if latest is None or v.get("version", 0) > latest.get("version", 0):
                latest = v
    if new:
        out.append("  别的写入者：%s；最新一次 v%s 由 %s 保存，说明「%s」" % (
            "；".join("%s（v%s）" % (who, ",".join(str(x) for x in sorted(vv))) for who, vv in new.items()),
            latest.get("version"), latest.get("createdBy", "?"), (latest.get("message") or "")[:28]))
    return out


def parse_providers(text):
    """Dashboard providers from a provisioning YAML, read line by line (no YAML library in the standard library)."""
    provs, cur = [], None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        m = re.match(r"^\s*-\s*name:\s*(.*)$", line)
        if m:
            cur = {"name": m.group(1).strip().strip("'\"")}
            provs.append(cur)
            continue
        m = re.match(r"^\s*(type|folder|path|allowUiUpdates|updateIntervalSeconds|disableDeletion|foldersFromFilesStructure):\s*(.*)$", line)
        if m and cur is not None:
            cur[m.group(1)] = m.group(2).strip().strip("'\"")
    return provs


def provisioning(dirname, uids):
    out = []
    files = sorted(set(glob.glob(os.path.join(dirname, "dashboards", "*.y*ml")) + glob.glob(os.path.join(dirname, "*.y*ml"))))
    if not files:
        return ["预置配置：%s 下没有 dashboards/*.yaml" % dirname]
    for f in files:
        with open(f, encoding="utf-8") as fh:
            provs = parse_providers(fh.read())
        for p in provs:
            path = p.get("path", "?")
            out.append("预置 %s：provider %s path=%s allowUiUpdates=%s updateIntervalSeconds=%s" % (
                os.path.basename(f), p.get("name"), path, p.get("allowUiUpdates", "(默认 false)"),
                p.get("updateIntervalSeconds", "(默认 10)")))
            cands = [path, os.path.join(os.path.dirname(os.path.abspath(dirname)), "dashboards")]
            where = next((c for c in cands if os.path.isdir(c)), None)
            if where is None:
                out.append("  主机上找不到 %s（可能是容器内路径），也没有 %s" % (path, cands[1]))
                continue
            hits = []
            for jf in glob.glob(os.path.join(where, "**", "*.json"), recursive=True):
                try:
                    with open(jf, encoding="utf-8") as fh:
                        d = json.load(fh)
                except (OSError, ValueError):
                    continue
                d = d.get("dashboard", d) if isinstance(d, dict) else {}
                if d.get("uid") in uids:
                    hits.append("%s=%s（%s）" % (d.get("uid"), os.path.relpath(jf, where), state_of(d).split("（")[0]))
            out.append("  在 %s 里：%s" % (where, "；".join(hits) if hits else "没有这两个 uid"))
    return out


def main():
    utf8_stdio()
    ap = argparse.ArgumentParser(description="面板一·P、面板一·D 被改回去时的只读诊断。")
    ap.add_argument("--url", default=os.environ.get("GRAFANA_URL"), help="Grafana 地址（或环境变量 GRAFANA_URL）")
    ap.add_argument("--token", help="服务账号 token（或 GRAFANA_TOKEN）")
    ap.add_argument("--user", help="用户名（或 GRAFANA_USER）；密码取 GRAFANA_PASSWORD，没有就提示输入")
    ap.add_argument("--uids", nargs=2, default=list(UIDS), metavar=("P_UID", "D_UID"))
    ap.add_argument("--provisioning-dir", help="Grafana 的 provisioning 目录（v3 部署包是 grafana/provisioning）")
    args = ap.parse_args()
    if not args.url:
        ap.error("需要 --url（或环境变量 GRAFANA_URL）")
    auth = auth_header(args)
    code, health = get(args.url, "/api/health", None)
    code, fs = get(args.url, "/api/frontend/settings", auth)
    namespace = (fs or {}).get("namespace") or "default"
    health = health or {}
    print("Grafana %s，commit %s，database %s，命名空间 %s" % (health.get("version", "?"), health.get("commit", "?"),
                                                       health.get("database", "?"), namespace))
    reads = poll(args.url, args.uids, auth)
    for side, uid in zip(("P", "D"), args.uids):
        code, res = get(args.url, "/api/dashboards/uid/%s" % uid, auth)
        if code != 200 or not res:
            print("%s %s：读不到（HTTP %s）" % (side, uid, code))
            continue
        m = res.get("meta", {})
        print("%s %s：provisioned=%s externalId=%s version=%s updated=%s updatedBy=%s folderUid=%s" % (
            side, uid, m.get("provisioned"), m.get("provisionedExternalId") or "-", res["dashboard"].get("version"),
            short_time(m.get("updated")), m.get("updatedBy"), m.get("folderUid") or "-"))
        print("  管理来源：%s" % k8s_annotations(args.url, uid, auth, namespace))
        print("  现在：%s" % state_of(res["dashboard"]))
        for line in poll_lines(args.url, uid, reads[uid], auth) + versions(args.url, uid, auth):
            print(line)
    if args.provisioning_dir:
        for line in provisioning(args.provisioning_dir, set(args.uids)):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
