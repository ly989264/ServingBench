#!/usr/bin/env python3
"""Test-only: arrange_boards.py against a throwaway Grafana 12.4.2, with dashboards imported by the deploy packs' own code.

    python3 test_arrange_boards.py <arrange_boards.py> <panel-1 pack dir> <panel-2 pack dir> <work dir> <grafana url> <password file>
The start is the site's: glm53-1 panel 1 (gen_cluster.py generate + grafana-add), glm53-1 and test panel 2
(grafana_api.py board-only), all in the top-level folder GLM-5.3, registered with model_group GLM-5.3.
The checks marked "- " are for the layout's top-level line `-  test-glm53  GLM-5.3-test`: test-glm53 starts in
GLM-5.3/test-glm53 with model folder test-glm53, as on the site.
Prints one line per check and a RESULT line.
"""
import base64, json, os, shutil, subprocess, sys

AR, P1, P2, WORK, URL, PWF = sys.argv[1:7]
PW = open(PWF).read().strip()
HOME = os.path.join(WORK, "home")
ROOT = os.path.join(WORK, "root")
ENV = dict(os.environ, HOME=HOME, VLLM_PD_REGISTRY="", GRAFANA_URL="", GRAFANA_USER="", GRAFANA_PASSWORD="")
for k in ("VLLM_PD_REGISTRY", "GRAFANA_URL", "GRAFANA_USER", "GRAFANA_PASSWORD"):
    ENV.pop(k)
fails = []


def check(name, ok, detail=""):
    print(("OK   " if ok else "FAIL ") + name + ("" if ok else "  " + str(detail)[:900]))
    if not ok:
        fails.append(name)


def sh(args, env=None, cwd=None):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env or ENV, cwd=cwd)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def api(method, path, body=None):
    import urllib.request, urllib.error
    req = urllib.request.Request(URL + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Basic " + base64.b64encode(("admin:" + PW).encode()).decode()})
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


def where(uid):
    c, b = api("GET", "/api/dashboards/uid/" + uid)
    if c != 200:
        return None
    fu = b["meta"].get("folderUid") or ""
    if not fu:
        return ("General", b["dashboard"]["title"])
    c, f = api("GET", "/api/folders/" + fu)
    return ("/".join([p["title"] for p in f.get("parents") or []] + [f["title"]]), b["dashboard"]["title"])


def top_titles():
    return sorted(f["title"] for f in api("GET", "/api/folders?limit=1000")[1])


GRAFANA = ["GRAFANA_URL=%s" % URL, "GRAFANA_USER=admin", "GRAFANA_PASSWORD=%s" % PW, "GRAFANA_CONTAINER=vllm-pd-grafana", "DOCKER_NETWORK=vllm-pd-monitor"]


def panel1(cluster, group, base):
    d = os.path.join(ROOT, "vllm-pd-grafana-panel1", cluster)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(os.path.join(d, "prometheus"))
    for x in ("scripts", "grafana"):
        shutil.copytree(os.path.join(P1, x), os.path.join(d, x))
    shutil.copy(os.path.join(P1, "prometheus", "gen_prometheus_yml.py"), os.path.join(d, "prometheus"))
    open(os.path.join(d, "deploy.env"), "w").write("\n".join(["CLUSTER=%s" % cluster, "MODEL_GROUP=%s" % group, "PORT_BASE=%d" % base,
                                                              "VLLM_PD_ROOT=%s" % ROOT, "PANEL1_DIR=%s" % d] + GRAFANA) + "\n")
    os.chmod(os.path.join(d, "deploy.env"), 0o600)
    net = {"glm53-1": 31, "dsv41-flash-1": 41}[cluster]
    inv = {"vllm_ascend_patched": False, "prefill": {"procs_per_instance": 4, "api_servers": ["10.%d.1.1:7100" % net]},
           "decode": {"procs_per_instance": 2, "api_servers": ["10.%d.2.1:7100" % net]}, "npu_exporter_port": 8082, "node_exporter_port": 9100}
    json.dump(inv, open(os.path.join(d, "inv.json"), "w"))
    return d


def add_panel1(cluster, group, base):
    d = panel1(cluster, group, base)
    rc, out = sh([sys.executable, os.path.join(d, "scripts", "gen_cluster.py"), "generate", "--env-file", d + "/deploy.env", "--inventory", d + "/inv.json"])
    shutil.copy(os.path.join(d, "inv.json"), os.path.join(d, "prometheus", "inventory.json"))
    rc2, out2 = sh([sys.executable, os.path.join(d, "scripts", "gen_cluster.py"), "grafana-add", "--env-file", d + "/deploy.env"])
    rc3, out3 = sh([sys.executable, os.path.join(d, "scripts", "names.py"), "register", "--panel", "panel1", "--env-file", d + "/deploy.env", "--inventory", d + "/prometheus/inventory.json"])
    return rc == 0 and rc2 == 0 and rc3 == 0, out + out2 + out3


def readd_panel1(cluster):
    d = os.path.join(ROOT, "vllm-pd-grafana-panel1", cluster)
    return sh([sys.executable, os.path.join(d, "scripts", "gen_cluster.py"), "grafana-add", "--env-file", d + "/deploy.env"])


def add_panel2(cluster, group, base):
    d = os.path.join(ROOT, "vllm-pd-grafana-panel2", cluster)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(os.path.join(d, "generated"))
    shutil.copytree(os.path.join(P2, "scripts"), os.path.join(d, "scripts"))
    open(os.path.join(d, "deploy.env"), "w").write("\n".join(["CLUSTER=%s" % cluster, "MODEL_GROUP=%s" % group, "PORT_BASE=%d" % base,
                                                              "VLLM_PD_ROOT=%s" % ROOT, "NODE_MODE=mock"] + GRAFANA) + "\n")
    json.dump({"uid": "vllm-pd-request", "title": "x", "panels": [], "templating": {"list": []}, "links": [], "schemaVersion": 41},
              open(os.path.join(d, "generated", "vllm-pd-request.json"), "w"))
    json.dump({"nodes": []}, open(os.path.join(d, "inventory.json"), "w"))
    rc, out = sh(["bash", "-c", 'set -a; . ./deploy.env; set +a; exec "$0" scripts/grafana_api.py board-only', sys.executable], cwd=d)
    rc2, out2 = sh([sys.executable, os.path.join(d, "scripts", "names.py"), "register", "--panel", "panel2", "--env-file", d + "/deploy.env", "--dir", d])
    return rc == 0 and rc2 == 0, out + out2


def ar(*args):
    return sh([sys.executable, AR] + list(args) + (["--env-from", ENVF] if args[0] != "folder" else []))


def reg():
    return json.load(open(os.path.join(HOME, ".vllm-pd-clusters.json")))


def group_of(path):
    return [l for l in open(path).read().splitlines() if l.startswith("MODEL_GROUP=")]


shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(HOME)
for f in api("GET", "/api/folders?limit=1000")[1]:
    api("DELETE", "/api/folders/" + f["uid"])
c, top = api("POST", "/api/folders", {"title": "GLM-5.3"})
ok1, o1 = add_panel1("glm53-1", "GLM-5.3", 18100)
ok2, o2 = add_panel2("glm53-1", "GLM-5.3", 18100)
ok3, o3 = add_panel2("test", "GLM-5.3", 18200)
check("start: packs' import code put the four boards into GLM-5.3", ok1 and ok2 and ok3 and all(
    (where(u) or ("",))[0] == "GLM-5.3" for u in ("vllm-pd-prefill-glm53-1", "vllm-pd-decode-glm53-1", "vllm-pd-request-glm53-1", "vllm-pd-request-test")), o1 + o2 + o3)
ENVF = os.path.join(ROOT, "vllm-pd-grafana-panel2", "glm53-1", "deploy.env")
FOUR = ("vllm-pd-prefill-glm53-1", "vllm-pd-decode-glm53-1", "vllm-pd-request-glm53-1", "vllm-pd-request-test")
DENV = [os.path.join(ROOT, "vllm-pd-grafana-panel1", "glm53-1", "deploy.env"), ENVF, os.path.join(ROOT, "vllm-pd-grafana-panel2", "test", "deploy.env")]
orig_env = {p: open(p).read() for p in DENV}
api("POST", "/api/user/stars/dashboard/uid/vllm-pd-prefill-glm53-1")

# folder
rc, out = ar("folder", "glm53-1")
rc2, out2 = ar("folder", "dsv41-flash-3")
rc3, out3 = ar("folder", "nope")
check("folder: glm53-1 -> glm53-1, dsv41-flash-3 -> dsv4.1-flash-3, unknown -> FAIL",
      (rc, out.strip(), rc2, out2.strip(), rc3) == (0, "glm53-1", 0, "dsv4.1-flash-3", 1) and "没有集群 nope" in out3, out + out2 + out3)

# plan is read-only
rc, s0 = ar("state")
rc, plan = ar("plan")
rc2, s1 = ar("state")
check("plan: 2 model folders, 1 folder, 4 boards = 7 items", rc == 0 and "RESULT: 要改 7 项" in plan and plan.count("挪 看板") == 4
      and "改 模型文件夹 glm53-1：GLM-5.3 → glm53-1（注册表；" in plan and "改 模型文件夹 test：GLM-5.3 → glm53-1" in plan and "建 文件夹 GLM-5.3/glm53-1" in plan, plan)
check("plan changes nothing (state before = after)", s0 == s1 and "容器" not in "" and s0.count("看板 ") == 4, s0 + s1)
check("plan names the clusters not yet registered", "还没登记的：glm53-0、glm53-2、dsv41-flash-1" in plan, plan)

# apply
rc, out = ar("apply")
bdirs = os.listdir(os.path.join(HOME, "vllm-pd-arrange-backup"))
b1 = os.path.join(HOME, "vllm-pd-arrange-backup", bdirs[0]) if bdirs else ""
check("apply: RESULT OK, 7 items, checks OK", rc == 0 and "RESULT: OK（改了 7 项；回退：" in out and "FAIL" not in out and out.count("OK  ") == 6, out)
check("apply: four boards in GLM-5.3/glm53-1, titles unchanged", all((where(u) or ("",))[0] == "GLM-5.3/glm53-1" for u in FOUR)
      and where("vllm-pd-request-test")[1].endswith("（test）"), [where(u) for u in FOUR])
r = reg()
check("apply: registry model_group glm53-1 for glm53-1 and test", r["clusters"]["glm53-1"]["model_group"] == "glm53-1" and r["clusters"]["test"]["model_group"] == "glm53-1", r["clusters"])
check("apply: three deploy.env have MODEL_GROUP=glm53-1, mode kept", all(group_of(p) == ["MODEL_GROUP=glm53-1"] for p in DENV) and oct(os.stat(DENV[0]).st_mode & 0o777) == "0o600", [group_of(p) for p in DENV])
check("apply: backup holds registry, three deploy.env, manifest", len(bdirs) == 1 and sorted(os.listdir(b1)) == sorted(
    ["registry.json", "manifest.json", "glm53-1-panel1-deploy.env", "glm53-1-panel2-deploy.env", "test-panel2-deploy.env"]), os.listdir(b1) if b1 else bdirs)
c, b = api("GET", "/api/dashboards/uid/vllm-pd-prefill-glm53-1")
check("apply: uid, url and star kept", b["meta"]["url"].startswith("/d/vllm-pd-prefill-glm53-1/") and b["meta"]["isStarred"] is True, b["meta"])
rc, s2 = ar("state")
diff = sorted(set(s0.splitlines()) ^ set(s2.splitlines()))
check("state diff: only the 2 model folders, 3 deploy.env and 4 boards changed", len(diff) == 2 * (2 + 3 + 4)
      and all(l.startswith(("集群 glm53-1", "集群 test", "文件 ", "看板 ")) for l in diff) and not [l for l in diff if "prometheus" in l], "\n".join(diff))

# idempotent
rc, out = ar("apply")
check("apply again: nothing to change, no new backup", rc == 0 and "RESULT: OK（没有要改的" in out and len(os.listdir(os.path.join(HOME, "vllm-pd-arrange-backup"))) == 1, out)

# a pack re-add puts the panel-1 boards into a new top-level glm53-1
rc, out = readd_panel1("glm53-1")
check("re-add (MODEL_GROUP glm53-1): pack creates a top-level glm53-1 and moves the two boards there",
      "文件夹 glm53-1 已建" in out and where("vllm-pd-prefill-glm53-1")[0] == "glm53-1" and top_titles() == ["GLM-5.3", "glm53-1"], out + str(top_titles()))
rc, chk = sh([sys.executable, os.path.join(P1, "scripts", "names.py"), "derive", "--panel", "panel1", "--env-file", DENV[0], "--format", "json"])
check("re-add: deploy.env still derives folder glm53-1 (the checks compare with it)", json.loads(chk)["GRAFANA_FOLDER"] == "glm53-1", chk)
rc, plan = ar("plan")
check("plan after re-add: 2 moves + delete the empty top-level glm53-1", "RESULT: 要改 3 项" in plan and plan.count("挪 看板") == 2 and "删 空文件夹 glm53-1（uid" in plan, plan)
rc, out = ar("apply")
check("apply after re-add: boards back, top-level glm53-1 gone", rc == 0 and all(where(u)[0] == "GLM-5.3/glm53-1" for u in FOUR) and top_titles() == ["GLM-5.3"], out + str(top_titles()))
rc, out = sh(["bash", "-c", 'set -a; . ./deploy.env; set +a; exec "$0" scripts/grafana_api.py board-only', sys.executable], cwd=os.path.dirname(DENV[2]))
rc, out = ar("apply")
check("panel-2 re-import of test, then apply: back in GLM-5.3/glm53-1", where("vllm-pd-request-test")[0] == "GLM-5.3/glm53-1" and "已挪 看板 vllm-pd-request-test" in out, out)

# a top-level folder named like a subfolder but holding something else is left alone
c, f2 = api("POST", "/api/folders", {"title": "glm53-2"})
api("POST", "/api/dashboards/db", {"dashboard": {"uid": "other", "title": "other", "panels": []}, "folderUid": f2["uid"]})
rc, plan = ar("plan")
check("non-empty top-level glm53-2: reported, not deleted", "RESULT: OK（没有要改的" in plan and "INFO 文件夹 glm53-2" in plan and "看板 1" in plan, plan)
api("DELETE", "/api/dashboards/uid/other")
api("DELETE", "/api/folders/" + f2["uid"])

# dsv41-flash-1: added with --model-group dsv4.1-flash-1
rc, s_glm = ar("state", "--except", "dsv41-flash-1")
ok, o = add_panel1("dsv41-flash-1", "dsv4.1-flash-1", 18110)
check("dsv add: pack puts its boards into a top-level dsv4.1-flash-1", ok and where("vllm-pd-prefill-dsv41-flash-1")[0] == "dsv4.1-flash-1", o)
rc, plan = ar("plan")
check("dsv plan: create dsv4.1-flash and its subfolder, 2 moves with titles, delete the empty top-level folder",
      "建 文件夹 dsv4.1-flash（顶层）" in plan and "建 文件夹 dsv4.1-flash/dsv4.1-flash-1" in plan and plan.count("标题改为「") == 2
      and "删 空文件夹 dsv4.1-flash-1（uid" in plan and "RESULT: 要改 5 项" in plan, plan)
rc, out = ar("apply")
w = where("vllm-pd-decode-dsv41-flash-1")
check("dsv apply: dsv4.1-flash/dsv4.1-flash-1, title 「…Decode实例（dsv4.1-flash-1）」", rc == 0 and w == ("dsv4.1-flash/dsv4.1-flash-1", "vLLM 服务侧监控面板 - Decode实例（dsv4.1-flash-1）")
      and top_titles() == sorted(["dsv4.1-flash", "GLM-5.3"]), out + str(w) + str(top_titles()))
rc, s_glm2 = ar("state", "--except", "dsv41-flash-1")
check("dsv apply did not touch glm53-1 or test (state --except, containers aside)", [l for l in s_glm.splitlines() if not l.startswith("容器")] ==
      [l for l in s_glm2.splitlines() if not l.startswith("容器")], "\n".join(sorted(set(s_glm.splitlines()) ^ set(s_glm2.splitlines()))))
rc, out = readd_panel1("dsv41-flash-1")
rc, out2 = ar("apply")
check("dsv re-add then apply: titles and folder restored", where("vllm-pd-prefill-dsv41-flash-1") == ("dsv4.1-flash/dsv4.1-flash-1", "vLLM 服务侧监控面板 - Prefill实例（dsv4.1-flash-1）") and top_titles() == sorted(["dsv4.1-flash", "GLM-5.3"]), out2)

# ambiguous parent
c, dup = api("POST", "/api/folders", {"title": "GLM-5.3"})
rc, s_a = ar("state")
rc, out = ar("apply")
rc2, s_b = ar("state")
check("two top-level GLM-5.3: FAIL, nothing changed", rc == 1 and "顶层有 2 个名为 GLM-5.3 的文件夹" in out and s_a == s_b, out)
api("DELETE", "/api/folders/" + dup["uid"])

# undo of the first apply
rc, out = ar("undo", b1)
r = reg()
check("undo: four boards back in GLM-5.3, subfolder glm53-1 deleted, model folders GLM-5.3", rc == 0 and all(where(u)[0] == "GLM-5.3" for u in FOUR)
      and [f["title"] for f in api("GET", "/api/folders?limit=1000&parentUid=" + top["uid"])[1]] == []
      and r["clusters"]["glm53-1"]["model_group"] == "GLM-5.3" and r["clusters"]["test"]["model_group"] == "GLM-5.3", out)
check("undo: three deploy.env byte-identical to the start", all(open(p).read() == orig_env[p] for p in DENV), [group_of(p) for p in DENV])
check("undo: dsv41-flash-1 left as it is", where("vllm-pd-prefill-dsv41-flash-1")[0] == "dsv4.1-flash/dsv4.1-flash-1", where("vllm-pd-prefill-dsv41-flash-1"))
rc, out = ar("undo", b1)
check("undo twice: every item skipped, nothing changed", rc == 0 and "有跳过的项" in out and "已挪回" not in out, out)
rc, out = ar("apply")
check("redo apply after undo: back in GLM-5.3/glm53-1", rc == 0 and all(where(u)[0] == "GLM-5.3/glm53-1" for u in FOUR) and "RESULT: OK（改了 7 项" in out, out)

# a removed cluster leaves an empty subfolder: apply deletes it, keeps the parent
d = os.path.join(ROOT, "vllm-pd-grafana-panel1", "dsv41-flash-1")
sh([sys.executable, os.path.join(d, "scripts", "gen_cluster.py"), "grafana-remove", "--env-file", d + "/deploy.env"])
sh([sys.executable, os.path.join(d, "scripts", "names.py"), "unregister", "--cluster", "dsv41-flash-1", "--panel", "panel1"])
rc, plan = ar("plan")
rc, out = ar("apply")
check("removed dsv41-flash-1: its empty subfolder deleted, dsv4.1-flash kept", "删 空文件夹 dsv4.1-flash/dsv4.1-flash-1" in plan and "RESULT: 要改 1 项" in plan
      and top_titles() == sorted(["dsv4.1-flash", "GLM-5.3"]) and api("GET", "/api/folders?limit=1000&parentUid=" + next(f["uid"] for f in api("GET", "/api/folders?limit=1000")[1] if f["title"] == "dsv4.1-flash"))[1] == [], plan + out)

# a - line (top level): test-glm53 from GLM-5.3/test-glm53 into the top-level GLM-5.3-test
B13 = "vllm-pd-request-test-glm53"
ENV13 = os.path.join(ROOT, "vllm-pd-grafana-panel2", "test-glm53", "deploy.env")
BK = os.path.join(HOME, "vllm-pd-arrange-backup")


def newest_backup():
    return os.path.join(BK, sorted(os.listdir(BK))[-1])


def glm_children():
    return sorted(f["title"] for f in api("GET", "/api/folders?limit=1000&parentUid=" + top["uid"])[1])


def others():  # state of every cluster but test-glm53, containers aside
    return [l for l in ar("state", "--except", "test-glm53")[1].splitlines() if not l.startswith("容器")]


rc, out = ar("folder", "test-glm53")
check("folder: test-glm53 (a - line) -> GLM-5.3-test", (rc, out.strip()) == (0, "GLM-5.3-test"), out)
s_oth0 = others()
ok, o = add_panel2("test-glm53", "test-glm53", 18300)
c, sub13 = api("POST", "/api/folders", {"title": "test-glm53", "parentUid": top["uid"]})
c, b = api("GET", "/api/dashboards/uid/" + B13)
old_top = b["meta"]["folderUid"]
c2, _ = api("POST", "/api/dashboards/db", {"dashboard": b["dashboard"], "folderUid": sub13["uid"], "overwrite": False})
api("DELETE", "/api/folders/" + old_top)
check("- start: test-glm53 added with model folder test-glm53, its board moved to GLM-5.3/test-glm53 (the site's state)",
      ok and c2 == 200 and where(B13)[0] == "GLM-5.3/test-glm53" and reg()["clusters"]["test-glm53"]["model_group"] == "test-glm53"
      and group_of(ENV13) == ["MODEL_GROUP=test-glm53"] and "test-glm53" not in top_titles(), o + str(where(B13)))
orig13 = open(ENV13).read()
title13 = where(B13)[1]

# old folder not empty: the board moves, the folder stays
api("POST", "/api/dashboards/db", {"dashboard": {"uid": "other13", "title": "other13", "panels": []}, "folderUid": sub13["uid"]})
rc, s_a = ar("state")
rc, plan = ar("plan")
rc2, s_b = ar("state")
check("- plan, old folder holds another board: model folder, create GLM-5.3-test, 1 move = 3 items; old folder reported, kept",
      rc == 0 and "RESULT: 要改 3 项" in plan and "改 模型文件夹 test-glm53：test-glm53 → GLM-5.3-test（注册表；%s）" % ENV13 in plan
      and "建 文件夹 GLM-5.3-test（顶层）" in plan and "挪 看板 %s「%s」：GLM-5.3/test-glm53 → GLM-5.3-test（顶层）" % (B13, title13) in plan
      and "INFO 文件夹 GLM-5.3/test-glm53（uid %s）里还有别的：看板 1、" % sub13["uid"] in plan and "删 空文件夹" not in plan, plan)
check("- plan changes nothing (state before = after)", s_a == s_b, "\n".join(sorted(set(s_a.splitlines()) ^ set(s_b.splitlines()))))
rc, out = ar("apply")
bA = newest_backup()
check("- apply, old folder not empty: board in GLM-5.3-test, GLM-5.3/test-glm53 kept with the other board",
      rc == 0 and "RESULT: OK（改了 3 项" in out and "FAIL" not in out and where(B13) == ("GLM-5.3-test", title13)
      and where("other13")[0] == "GLM-5.3/test-glm53" and glm_children() == ["glm53-1", "test-glm53"], out)
rc, out = ar("undo", bA)
c, b = api("GET", "/api/dashboards/uid/" + B13)
check("- undo of it: board back in the same GLM-5.3/test-glm53 (same uid), GLM-5.3-test deleted, model folder test-glm53",
      rc == 0 and "有跳过" not in out and "已重建" not in out and b["meta"]["folderUid"] == sub13["uid"] and "GLM-5.3-test" not in top_titles()
      and reg()["clusters"]["test-glm53"]["model_group"] == "test-glm53" and open(ENV13).read() == orig13, out)
api("DELETE", "/api/dashboards/uid/other13")

# old folder empty after the move: deleted
rc, plan = ar("plan")
check("- plan: model folder, create GLM-5.3-test, 1 move, delete the emptied GLM-5.3/test-glm53 = 4 items",
      rc == 0 and "RESULT: 要改 4 项" in plan and "删 空文件夹 GLM-5.3/test-glm53（uid %s）" % sub13["uid"] in plan and "INFO 文件夹 GLM-5.3/test-glm53" not in plan, plan)
nb = len(os.listdir(BK))
rc, out = ar("apply")
bE = newest_backup()
r = reg()
check("- apply: RESULT OK, 4 items, checks OK (registry, folder derived from deploy.env, nothing left)", rc == 0 and "RESULT: OK（改了 4 项" in out
      and "FAIL" not in out and "集群 test-glm53：注册表 是 GLM-5.3-test" in out and "deploy.env 推出的文件夹 是 GLM-5.3-test" in out and out.count("OK  ") == 3, out)
check("- apply: top-level GLM-5.3-test created, board there with its title, uid and url kept",
      "GLM-5.3-test" in top_titles() and where(B13) == ("GLM-5.3-test", title13)
      and api("GET", "/api/dashboards/uid/" + B13)[1]["meta"]["url"].startswith("/d/%s/" % B13), str(top_titles()) + str(where(B13)))
check("- apply: emptied GLM-5.3/test-glm53 deleted, GLM-5.3 keeps glm53-1", glm_children() == ["glm53-1"] and api("GET", "/api/folders/" + sub13["uid"])[0] == 404, glm_children())
check("- apply: model folder GLM-5.3-test in the registry and deploy.env, rest of deploy.env unchanged", r["clusters"]["test-glm53"]["model_group"] == "GLM-5.3-test"
      and open(ENV13).read() == orig13.replace("MODEL_GROUP=test-glm53\n", "MODEL_GROUP=GLM-5.3-test\n"), [r["clusters"]["test-glm53"]["model_group"], group_of(ENV13)])
man = json.load(open(os.path.join(bE, "manifest.json")))
check("- apply: backup holds registry (model folder test-glm53), the deploy.env as it was, manifest with the deleted folder", len(os.listdir(BK)) == nb + 1
      and sorted(os.listdir(bE)) == ["manifest.json", "registry.json", "test-glm53-panel2-deploy.env"]
      and json.load(open(os.path.join(bE, "registry.json")))["clusters"]["test-glm53"]["model_group"] == "test-glm53"
      and open(os.path.join(bE, "test-glm53-panel2-deploy.env")).read() == orig13
      and man["deleted"] == [{"uid": sub13["uid"], "title": "test-glm53", "parentUid": top["uid"], "path": "GLM-5.3/test-glm53"}], [os.listdir(bE), man])
rc, st = ar("state")
check("- state: model folder GLM-5.3-test, board in GLM-5.3-test", "集群 test-glm53 模型文件夹 GLM-5.3-test" in st and "看板 %s 版本 " % B13 in st
      and ("在 GLM-5.3-test 标题「%s」" % title13) in st, st)
check("- the - line did not touch the other clusters (state --except test-glm53)", others() == s_oth0, "\n".join(sorted(set(others()) ^ set(s_oth0))))
rc, out = ar("apply")
check("- apply again: nothing to change, no new backup", rc == 0 and "RESULT: OK（没有要改的" in out and len(os.listdir(BK)) == nb + 1, out)
rc, out = sh(["bash", "-c", 'set -a; . ./deploy.env; set +a; exec "$0" scripts/grafana_api.py board-only', sys.executable], cwd=os.path.dirname(ENV13))
rc2, plan = ar("plan")
check("- panel-2 re-import (MODEL_GROUP GLM-5.3-test): board stays in GLM-5.3-test, plan has nothing to change",
      rc == 0 and where(B13)[0] == "GLM-5.3-test" and "RESULT: OK（没有要改的" in plan, out + plan)

# undo: the deleted folder comes back, the model folder too
rc, out = ar("undo", bE)
kids13 = [f for f in api("GET", "/api/folders?limit=1000&parentUid=" + top["uid"])[1] if f["title"] == "test-glm53"]
c, b = api("GET", "/api/dashboards/uid/" + B13)
check("- undo: GLM-5.3/test-glm53 created again under GLM-5.3 (new uid), board back in it, GLM-5.3-test deleted",
      rc == 0 and "已重建 文件夹 GLM-5.3/test-glm53（apply 删掉的那个，新 uid" in out and "有跳过" not in out and len(kids13) == 1
      and b["meta"]["folderUid"] == kids13[0]["uid"] and where(B13) == ("GLM-5.3/test-glm53", title13) and "GLM-5.3-test" not in top_titles(), out)
check("- undo: registry model folder test-glm53, deploy.env byte-identical to the start", reg()["clusters"]["test-glm53"]["model_group"] == "test-glm53"
      and open(ENV13).read() == orig13, group_of(ENV13))
check("- undo did not touch the other clusters", others() == s_oth0, "\n".join(sorted(set(others()) ^ set(s_oth0))))
rc, out = ar("apply")
check("- redo apply after undo: board in GLM-5.3-test, the re-created GLM-5.3/test-glm53 deleted again", rc == 0 and "RESULT: OK（改了 4 项" in out
      and where(B13)[0] == "GLM-5.3-test" and glm_children() == ["glm53-1"], out)
rc, out = ar("apply")
check("- apply once more: nothing to change", rc == 0 and "RESULT: OK（没有要改的" in out, out)

# two top-level GLM-5.3-test: refused, nothing changed
c, dup13 = api("POST", "/api/folders", {"title": "GLM-5.3-test"})
rc, s_a = ar("state")
rc, out = ar("apply")
rc2, s_b = ar("state")
check("- two top-level GLM-5.3-test: FAIL, nothing changed", rc == 1 and "顶层有 2 个名为 GLM-5.3-test 的文件夹" in out and s_a == s_b, out)
api("DELETE", "/api/folders/" + dup13["uid"])

# lock file of the deploy packs used
check("registry written under the packs' lock file", os.path.exists(os.path.join(HOME, ".vllm-pd-clusters.json.lock")))
print("RESULT: %s（%d 项不通过）" % ("OK" if not fails else "FAIL", len(fails)))
sys.exit(1 if fails else 0)
