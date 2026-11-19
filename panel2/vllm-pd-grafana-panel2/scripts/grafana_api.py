#!/usr/bin/env python3
"""Adds or removes 面板二 on an existing Grafana through its HTTP API. Other dashboards are not touched.

Settings come from the environment (bin/lib.sh loads deploy.env). The names of the data source, the dashboard and its
folder are the cluster's (scripts/names.py); the generated dashboard is rewritten with them before it is imported.
"""
import argparse
import base64
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from names import Names, NamesError  # noqa: E402

ENV = os.environ
PACK = pathlib.Path(__file__).resolve().parent.parent
URL = ENV.get("GRAFANA_URL", "http://localhost:3000").rstrip("/")
AUTH = base64.b64encode(("%s:%s" % (ENV.get("GRAFANA_USER", "admin"), ENV.get("GRAFANA_PASSWORD", ""))).encode()).decode()
try:
    N = Names(ENV)
    DS_UID, DS_NAME = N.get("GRAFANA_DS_UID"), N.get("GRAFANA_DS_NAME")
    BOARD_UID, BOARD_TITLE, FOLDER = N.get("GRAFANA_BOARD_UID"), N.get("GRAFANA_BOARD_TITLE"), N.get("GRAFANA_FOLDER")
except NamesError as e:
    raise SystemExit("错误：%s" % e)
PLUGIN = "grafana-clickhouse-datasource"
# The two links at the top of the generated dashboard, by title, and the deploy.env key of the panel 1 dashboard each opens.
PANEL1 = {"面板一·P": "PANEL1_P_UID", "面板一·D": "PANEL1_D_UID"}


def panel1_uid(title):
    return N.get(PANEL1[title])


def js_regex_escape(s):
    return re.sub(r"[.*+?^${}()|\[\]\\/]", lambda m: "\\" + m.group(0), s)


def rewrite_board(board, uid, title, ds_uid, ds_name, panel1_links):
    """The generated dashboard with this cluster's names: its UID, title, the data source variable (regex and current
    value), any panel naming the data source by UID, the links that open the same dashboard for one request, and the
    two links at the top (panel1_links: link title -> panel 1 UID, or None to leave the link out). Returns a new dict."""
    old_uid = board["uid"]
    text = json.dumps(board, ensure_ascii=False)
    # The links to the same dashboard: /d/<uid>/<slug>?..., written with the generated UID as both.
    text = text.replace("/d/%s/%s?" % (old_uid, old_uid), "/d/%s/%s?" % (uid, uid))
    text = re.sub(r"/d/%s(?=[/?\"])" % re.escape(old_uid), lambda m: "/d/" + uid, text)
    board = json.loads(text)
    board.pop("id", None)
    board["uid"], board["title"] = uid, title
    old_ds = set()
    for v in (board.get("templating") or {}).get("list") or []:
        if v.get("type") == "datasource" and v.get("query") == PLUGIN:
            old_ds.add((v.get("current") or {}).get("value"))
            # Only this cluster's data source matches. If it is missing the dashboard says so instead of using another one.
            v["regex"] = "/^%s$/" % js_regex_escape(ds_name)
            v["current"] = {"text": ds_name, "value": ds_uid}

    def walk(x):
        if isinstance(x, dict):
            if x.get("type") == PLUGIN and x.get("uid") in old_ds:
                x["uid"] = ds_uid
            for y in x.values():
                walk(y)
        elif isinstance(x, list):
            for y in x:
                walk(y)
    walk(board.get("panels"))
    links = []
    for link in board.get("links", []):
        if link.get("title") in panel1_links:
            target = panel1_links[link["title"]]
            if not target:
                continue
            link["url"] = "/d/" + target
        links.append(link)
    board["links"] = links
    return board


def call(method, path, body=None):
    req = urllib.request.Request(URL + path, method=method, data=None if body is None else json.dumps(body).encode())
    req.add_header("Authorization", "Basic " + AUTH)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except ValueError:
            return e.code, {"message": raw[:200].decode("utf-8", "replace")}
    except urllib.error.URLError as e:
        raise SystemExit("连不上 Grafana %s：%s。检查 deploy.env 里的 GRAFANA_URL。" % (URL, e.reason))


def require_login():
    code, body = call("GET", "/api/org")
    if code in (401, 403):
        raise SystemExit("Grafana 登录失败（HTTP %d）。检查 deploy.env 里的 GRAFANA_USER 和 GRAFANA_PASSWORD。" % code)
    if code != 200:
        raise SystemExit("Grafana 返回 HTTP %d：%s" % (code, body))


def plugin_check():
    require_login()
    code, body = call("GET", "/api/plugins/%s/settings" % PLUGIN)
    if code != 200:
        print("Grafana 里没有 ClickHouse 插件（HTTP %d）。看 docker logs 里 plugin 相关的行。" % code)
        return 1
    print("ClickHouse 插件版本: %s" % body.get("info", {}).get("version"))
    print("RESULT: OK")
    return 0


def folder_uid():
    code, folders = call("GET", "/api/folders?limit=1000")
    for f in folders if code == 200 else []:
        if f.get("title") == FOLDER:
            return f["uid"]
    code, body = call("POST", "/api/folders", {"title": FOLDER})
    if code not in (200, 201):
        raise SystemExit("建不了目录 %s：HTTP %d %s" % (FOLDER, code, body))
    return body["uid"]


def datasource_body():
    """The data source as install() writes it (scripts/compare.py compares it with the live one)."""
    return {
        "name": DS_NAME,
        "uid": DS_UID,
        "type": PLUGIN,
        "access": "proxy",
        "isDefault": False,
        "jsonData": {
            "host": N.get("CLICKHOUSE_ADDR"),
            # Inside the docker network the container listens on 8123 whatever port is published.
            "port": 8123 if N.get("CLICKHOUSE_ADDR") == N.get("CLICKHOUSE_CONTAINER") else int(N.get("CLICKHOUSE_HTTP_PORT")),
            "protocol": "http",
            "username": "vllm_reader",
            "defaultDatabase": "vllm_pd",
        },
        "secureJsonData": {"password": ENV.get("CH_READER_PASSWORD", "")},
    }


def install(skip_datasource=False, skip_board=False, datasource_change="", board_change=""):
    """skip_*: bin/grafana_board.sh found that item unchanged (scripts/compare.py); *_change: where it differs, printed
    once the item is written."""
    require_login()
    if skip_datasource:
        print("跳过（内容不变）：数据源")
    else:
        ds = datasource_body()
        code, existing = call("GET", "/api/datasources/uid/" + DS_UID)
        if code == 200:
            code, body = call("PUT", "/api/datasources/uid/" + DS_UID, dict(ds, version=existing.get("version", 1)))
        else:
            code, body = call("POST", "/api/datasources", ds)
        if code != 200:
            raise SystemExit("加数据源失败：HTTP %d %s。插件还没装时会这样，先做插件那一步。" % (code, body))
        print("数据源已就绪: " + DS_NAME)
        if datasource_change:
            print("已更新：数据源：" + datasource_change)
    code, body = call("GET", "/api/datasources/uid/%s/health" % DS_UID)
    if code != 200 or body.get("status") != "OK":
        raise SystemExit("Grafana 连不上 ClickHouse：%s。检查 CLICKHOUSE_ADDR 和 CH_READER_PASSWORD。" % body.get("message", body))
    print("Grafana 能连上 ClickHouse")
    if skip_board:
        print("跳过（内容不变）：看板")
    else:
        import_board()
        if board_change:
            print("已更新：看板：" + board_change)
    print("RESULT: OK")
    return 0


def prepared_board(board, quiet=False):
    """The generated dashboard as import_board() sends it: the cluster's names, and the two links at the top going to the
    cluster's panel 1 dashboards (left out when Grafana has no such dashboard). quiet: no note about a missing one."""
    targets = {}
    for title in PANEL1:
        uid = panel1_uid(title)
        if call("GET", "/api/dashboards/uid/" + uid)[0] != 200:
            if not quiet:
                print("没有 UID 为 %s 的看板，页面顶部不放「%s」的跳转。面板一装好以后重新执行 bin/grafana_board.sh；现场 UID 不同就写进 deploy.env 的 %s" % (uid, title, PANEL1[title]))
            uid = None
        targets[title] = uid
    return rewrite_board(board, BOARD_UID, BOARD_TITLE, DS_UID, DS_NAME, targets)


def import_board():
    with open(PACK / "generated" / "vllm-pd-request.json", encoding="utf-8") as f:
        board = json.load(f)
    # The two links at the top of the page go to the cluster's panel 1 dashboards.
    board = prepared_board(board)
    code, body = call("POST", "/api/dashboards/db", {"dashboard": board, "folderUid": folder_uid(), "overwrite": True, "message": "面板二"})
    if code != 200:
        raise SystemExit("导入看板失败：HTTP %d %s" % (code, body))
    print("看板已导入: %s%s" % (URL, body.get("url", "")))


def preflight():
    """One line per item, OK, INFO or FAIL. Reads only."""
    try:
        code, body = call("GET", "/api/org")
    except SystemExit:
        print("FAIL Grafana %s 连不上 → 检查 deploy.env 的 GRAFANA_URL。" % URL)
        return 0
    if code != 200:
        print("FAIL Grafana 登录失败（HTTP %d）→ 检查 deploy.env 的 GRAFANA_USER 和 GRAFANA_PASSWORD。" % code)
        return 0
    code, health = call("GET", "/api/health")
    version = health.get("version", "?")
    try:
        major, minor = [int(x) for x in version.split(".")[:2]]
    except ValueError:
        major, minor = 0, 0
    if (major, minor) >= (11, 6):
        print("OK Grafana %s 能登录" % version)
    else:
        print("FAIL Grafana %s 太旧 → 插件 4.23.4 要求 11.6 及以上。" % version)
    code, boards = call("GET", "/api/search?type=dash-db&limit=1000")
    known = ", ".join(b.get("uid", "") for b in boards) if code == 200 else ""
    for title in PANEL1:
        uid = panel1_uid(title)
        if call("GET", "/api/dashboards/uid/" + uid)[0] == 200:
            print("OK %s看板 %s" % (title, uid))
        else:
            # Only the jump at the top of the dashboard needs it (prepared_board leaves the link out), so a cluster without
            # panel 1 installs (known issue 11 (1)).
            print("INFO %s看板 %s 没找到：看板顶部不放这个跳转，集群没有面板一时是这样。有面板一、UID 不同时，deploy.env 的 %s 写现场的 UID，现有：%s" % (
                title, uid, PANEL1[title], known[:200]))
    return 0


def uninstall():
    require_login()
    call("DELETE", "/api/dashboards/uid/" + BOARD_UID)
    call("DELETE", "/api/datasources/uid/" + DS_UID)
    print("已从 Grafana 删除面板二的看板和数据源")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "plugin-check":
        sys.exit(plugin_check())
    if cmd == "install":
        ap = argparse.ArgumentParser(prog="grafana_api.py install")
        ap.add_argument("--skip-datasource", action="store_true", help="数据源不变：不写")
        ap.add_argument("--skip-board", action="store_true", help="看板不变：不导入")
        ap.add_argument("--datasource-change", default="", help="数据源哪一处不同，写完以后打印")
        ap.add_argument("--board-change", default="", help="看板哪一处不同，导入以后打印")
        a = ap.parse_args(sys.argv[2:])
        sys.exit(install(a.skip_datasource, a.skip_board, a.datasource_change, a.board_change))
    if cmd == "board-only":
        require_login()
        import_board()
        sys.exit(0)
    if cmd == "preflight":
        sys.exit(preflight())
    if cmd == "uninstall":
        sys.exit(uninstall())
    raise SystemExit("用法：grafana_api.py plugin-check|install|board-only|preflight|uninstall")
