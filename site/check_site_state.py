#!/usr/bin/env python3
"""Read-only comparison of the intranet state with the assumptions written below (v2).

  监控机，一次跑完所有机器：  python3 -I -S -B check_site_state.py --all
  只查监控机：               python3 -I -S -B check_site_state.py
  单台 P、D、proxy 机器：     python3 -I -S -B check_site_state.py --node p|d|proxy [--cluster test-glm53|glm53-1] [--container 名称]

--all reads the P, D and proxy machines of test-glm53 and glm53-1 from the panel-two cluster folders (inventory.json;
proxy = the host of ingest.json fetcher_url for test-glm53, PROXY_MACHINE of deploy.env for glm53-1) and runs this
script on each over ssh root@<machine> 'python3 -I -S -B - --node <role> --cluster <cluster>' with the script on stdin.
The vLLM container is found by its processes (vllm serve or api_server); --container names it by hand.

Each assumption has an id, the object checked, the expected value and where the expected value comes from
(现状文档 = the 11-28 site-state document before the site check, line numbers; 现场 v1 输出 11-28; or a delivery
package). The personal directory on the monitoring machine is given with --personal-dir (default: not checked).
An expected value of None means it is not known: the actual value is printed.

Output: one segment per machine (machines with the same result share one), each a summary line
(一致 / 不一致 / 未知 / 无法检查) and the items that are not 一致; 150 lines in all. Every item and the details go to
report-<time>.txt in the folder of this script on the monitoring machine (or --report-dir): the only file this script
writes. Nodes write nothing.

Read-only: docker inspect / docker ps / docker network inspect; files read from the host, and files of running
containers read through /proc/<pid>/root; environment variables through /proc/<pid>/environ; ClickHouse through
docker exec <ClickHouse container> clickhouse-client --readonly=1 with SELECT only; Grafana and the ingest status
port through HTTP GET; crontab -l. No docker exec into vLLM containers, no signals, no writes other than the report.
"""
import argparse
import base64
import datetime
import glob
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.request

# ============================================================================ assumptions (data)

S02 = "现状文档（11-28 核实前）第 %s 行"
SITE = "现场 v1 输出 11-28"
PANEL1 = [  # cluster, port base, top folder, sub folder, source of the port base, verified
    ("glm53-0", 18170, "GLM-5.3", "glm53-0", SITE, True),
    ("glm53-1", 18100, "GLM-5.3", "glm53-1", SITE, True),
    ("glm53-2", 18180, "GLM-5.3", "glm53-2", SITE, True),
    ("dsv41-flash-1", 18120, "dsv4.1-flash", "dsv4.1-flash-1", SITE, True),
    ("dsv41-flash-2", 18130, "dsv4.1-flash", "dsv4.1-flash-2", SITE, True),
    ("dsv41-flash-3", 18140, "dsv4.1-flash", "dsv4.1-flash-3", SITE, True),
    ("dsv41-flash-4", 18150, "dsv4.1-flash", "dsv4.1-flash-4", SITE, True),
    ("dsv41-flash-5", 18160, "dsv4.1-flash", "dsv4.1-flash-5", SITE, True),
]
NET = "vllm-pd-monitor"   # docker network of every container (现场 v1 输出 11-28)
PANEL2 = ("glm53-1", "test-glm53")
ALL_CLUSTERS = [c[0] for c in PANEL1] + ["test-glm53"]
ROOT = "/data/grafana"
OLD = "/data/<个人目录>"   # set by --personal-dir
TOOLS = ROOT + "/tools"
SIX_DATA_DIRS = ("clickhouse-glm53-1", "clickhouse-test-glm53", "vllm-clickhouse", "vllm-clickhouse-glm53-test",
                 "vllm-clickhouse-test", "vllm-timing")
CH_IMAGE = "clickhouse/clickhouse-server:26.8.36.3"
VECTOR_IMAGE = "timberio/vector:0.61.0-debian"
PROM_TAG = "v3.11.2"
GRAFANA_VERSION = "12.4.2"
CH_PLUGIN = "4.23.4"
PANEL1_DESC = {"prefill": 91, "decode": 80}  # panels with a description in deploy pack v1.2 (Caching 8629d97)
V34_TABLE = "phase_line_cur"                 # the table the v3.4-extra board reads (pack v3.4-extra)
T62_TABLES = ("request_doc", "status_doc", "segment_def", "db_interface")  # T62 schema.sql (package 980e5a2c)
NODE = {  # per panel-two cluster, for --node
    "test-glm53": {"container": None, "container_src": S02 % 92,
                   "mooncake": "39854f25629a3cac", "mooncake_src": S02 % 95 + "（替换库 v3）",
                   "level": None, "level_src": S02 % 99, "state": "T62",
                   "image": "vllm-ascend-glm53-v30:backup", "image_src": SITE, "t53tar": "文件，1.1M"},
    "glm53-1": {"container": None, "container_src": S02 % 127,
                "mooncake": "37a3aaf8acb53c6a", "mooncake_src": S02 % 78 + "（替换库 v2，按包推得）",
                "level": "未设置", "level_src": S02 % 80, "state": "T73",
                "connector": "2c4acb3eeb1ced7d", "connector_src": "T53 两步包 lib/expect.sh EXP_CONN"},
}
SITE_CONNECTOR = "f10dee5cd4dfdccb"   # mooncake_connector.py replayed on the site original (02 第 94 行)
SITE_ORIGINAL = "2c4acb3eeb1ced7d"    # the site's original mooncake_connector.py (02 第 93 行)
CONNECTOR = "vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py"

# T62 状态（升级包 980e5a2c 的 patch-kit/patches/*/SHA256SUMS.after），sha256 前 16 位：47 个
T62_FILES = {
    'vllm/entrypoints/openai/chat_completion/api_router.py': 'e3cb395d2f925480',
    'vllm/entrypoints/openai/chat_completion/serving.py': 'accbe961f497b114',
    'vllm/entrypoints/openai/completion/api_router.py': 'b284cf3740795756',
    'vllm/entrypoints/openai/completion/serving.py': '0173143f82777d3a',
    'vllm/entrypoints/serve/utils/pd_diagnostics.py': '317bb063b7253f34',
    'vllm/inputs/engine.py': 'be94b4c9048b57b8',
    'vllm/inputs/llm.py': '374373c3cc9aa9b8',
    'vllm/renderers/base.py': 'dd6633fb8fff209d',
    'vllm/renderers/online_renderer.py': '837987a673c3569c',
    'vllm/v1/core/kv_cache_manager.py': '63038887c5da057c',
    'vllm/v1/core/sched/output.py': 'a291745978d0573f',
    'vllm/v1/core/sched/scheduler.py': '6e3bb3e618c701f3',
    'vllm/v1/engine/__init__.py': '9c73aa0972950294',
    'vllm/v1/engine/async_llm.py': '4f01dd247018f5d2',
    'vllm/v1/engine/core.py': '68c846a6052f4d4a',
    'vllm/v1/engine/core_client.py': '296ecaee34825ce9',
    'vllm/v1/engine/input_processor.py': '92366cddd360d0d0',
    'vllm/v1/engine/output_processor.py': 'b9a0bbc99c4c7c6c',
    'vllm/v1/metrics/pd_diagnostics.py': '54aadd92fc5a8029',
    'vllm/v1/metrics/pd_hooks/__init__.py': '19d1f2c283bcbe19',
    'vllm/v1/metrics/pd_hooks/engine.py': '625041e028389eec',
    'vllm/v1/metrics/pd_hooks/frontend.py': '5a5e6a1dbbcb98f1',
    'vllm/v1/metrics/pd_hooks/output.py': '4e2cf28cf7adcaad',
    'vllm/v1/metrics/pd_hooks/render.py': '374298565ea8aac4',
    'vllm/v1/metrics/pd_hooks/request.py': 'ae7973a52d68382a',
    'vllm/v1/metrics/pd_hooks/scheduler.py': 'e24d4007cfd0b6bc',
    'vllm/v1/metrics/pd_hooks/worker.py': 'd3388eae94a765d3',
    'vllm/v1/metrics/stats.py': 'bf8238e46eae80ad',
    'vllm/v1/request.py': 'b2fe473d08ce7c09',
    'vllm/v1/worker/gpu/kv_connector.py': '749a99b289d4a9b3',
    'vllm/v1/worker/gpu/model_runner.py': 'affe2249501a6770',
    'vllm/v1/worker/kv_connector_model_runner_mixin.py': '647ef8caf6a6ad21',
    'vllm/v1/worker/worker_base.py': '6a50a18da7d036f6',
    'vllm_ascend/core/recompute_diagnostics.py': '47c27da0fdb7aff7',
    'vllm_ascend/core/recompute_scheduler.py': '7a33cb1079b46ebc',
    'vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py': '128d4cd1117ee8d2',
    'vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector_pd_diag.py': 'ff15e8b4e6acc56d',
    'vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/backend/mooncake_backend.py': '722c0669f43294f5',
    'vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/backend/mooncake_layerwise.py': 'e06b0c7c2e964f79',
    'vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/diagnostics.py': '4140400b3cbf0003',
    'vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/kv_transfer.py': 'a414e51bcccca393',
    'vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/metadata.py': 'd95b58fb9925a6a1',
    'vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/pool_scheduler.py': '287cc78617fe0100',
    'vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/pool_worker.py': '099c4a13d408fd72',
    'vllm_ascend/worker/model_runner_v1.py': 'ba1b84ecd2ee4c60',
    'vllm_ascend/worker/pd_device_timing.py': 'e1e29b6b9432a0c1',
    'vllm_ascend/worker/v2/model_runner.py': 'f198987d020b4d88',
}
# T73 状态（升级包 980e5a2c 的 patch-kit/states/SHA256SUMS.t73）：37 个
T73_FILES = {
    'vllm/entrypoints/openai/chat_completion/api_router.py': 'e3cb395d2f925480',
    'vllm/entrypoints/openai/chat_completion/serving.py': 'd4763272daf478b4',
    'vllm/entrypoints/openai/completion/api_router.py': 'b284cf3740795756',
    'vllm/entrypoints/openai/completion/serving.py': '8854679c75e645a7',
    'vllm/entrypoints/serve/utils/pd_diagnostics.py': 'c056e7bb787819dd',
    'vllm/inputs/engine.py': 'be94b4c9048b57b8',
    'vllm/inputs/llm.py': '374373c3cc9aa9b8',
    'vllm/renderers/base.py': 'efdb5a6e406e8934',
    'vllm/renderers/online_renderer.py': 'd7ff1838f146e116',
    'vllm/v1/core/kv_cache_manager.py': '2dda5a2c85b192a8',
    'vllm/v1/core/sched/output.py': 'a291745978d0573f',
    'vllm/v1/core/sched/scheduler.py': 'cfc61ab32445df88',
    'vllm/v1/engine/__init__.py': '9c73aa0972950294',
    'vllm/v1/engine/async_llm.py': '9af12e9b42641a60',
    'vllm/v1/engine/core.py': 'c8b2f333579f4de7',
    'vllm/v1/engine/core_client.py': '563ff6679aa241e3',
    'vllm/v1/engine/input_processor.py': 'e9fc7a2e5f38e99e',
    'vllm/v1/engine/output_processor.py': 'a2295ff4a9c63765',
    'vllm/v1/metrics/pd_diagnostics.py': '54aadd92fc5a8029',
    'vllm/v1/metrics/stats.py': 'de1fba29e7857c10',
    'vllm/v1/request.py': '3571cd9128303bc8',
    'vllm/v1/worker/gpu/kv_connector.py': 'ff0d4a29dbfcc2af',
    'vllm/v1/worker/gpu/model_runner.py': '1a3647ab8ccaa357',
    'vllm/v1/worker/kv_connector_model_runner_mixin.py': 'a7b0368ff7919a52',
    'vllm/v1/worker/worker_base.py': '5c9ebc138927f572',
    'vllm_ascend/core/recompute_scheduler.py': 'ff719569361e1f41',
    'vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py': '01ce3b2a7dd5cdb1',
    'vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/backend/mooncake_backend.py': 'f5f59540786c2691',
    'vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/backend/mooncake_layerwise.py': 'e06b0c7c2e964f79',
    'vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/diagnostics.py': 'cc6e75e1f4012527',
    'vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/kv_transfer.py': 'd0402076808a8246',
    'vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/metadata.py': 'f365ce6f46a484c2',
    'vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/pool_scheduler.py': '64e8403e58de105d',
    'vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/pool_worker.py': '8997e1b2949067ae',
    'vllm_ascend/worker/model_runner_v1.py': '17ade1c36ced679c',
    'vllm_ascend/worker/pd_device_timing.py': 'e1e29b6b9432a0c1',
    'vllm_ascend/worker/v2/model_runner.py': 'f198987d020b4d88',
}
# T73 状态下不存在、T62 新增的文件（patch-kit/states/NEW_FILES.txt）
T62_NEW_FILES = ['vllm/v1/metrics/pd_hooks/__init__.py', 'vllm/v1/metrics/pd_hooks/engine.py', 'vllm/v1/metrics/pd_hooks/frontend.py', 'vllm/v1/metrics/pd_hooks/output.py', 'vllm/v1/metrics/pd_hooks/render.py', 'vllm/v1/metrics/pd_hooks/request.py', 'vllm/v1/metrics/pd_hooks/scheduler.py', 'vllm/v1/metrics/pd_hooks/worker.py', 'vllm_ascend/core/recompute_diagnostics.py', 'vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector_pd_diag.py']

# ============================================================================ helpers

SECRET = re.compile(r"PASS|TOKEN|KEY|SECRET", re.I)


class Unreadable(Exception):
    """The actual value cannot be read: the item is 无法检查."""


class Skip(Exception):
    """The item depends on something another item already reports as missing: not recorded."""


def run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                           timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", "没有命令 %s" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "", "超过 %d 秒没有返回" % timeout


def last(err, o=""):
    t = (err or o or "").strip().splitlines()
    return t[-1][:150] if t else "无输出"


def sha16(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()[:16]


def tstamp(t):
    return datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")


def human(n):
    n = float(n or 0)
    for u in ("B", "K", "M", "G", "T"):
        if n < 1024 or u == "T":
            return ("%d%s" % (n, u)) if u == "B" else ("%.1f%s" % (n, u))
        n /= 1024


def env_values(path, keys=None):
    """name -> value of an env file as bash sees it, in a clean environment. None when the file is missing."""
    if not os.path.isfile(path):
        return None
    if keys is None:
        keys = []
        with open(path, errors="replace") as f:
            for line in f:
                m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=", line)
                if m and m.group(1) not in keys:
                    keys.append(m.group(1))
    script = 'set -a; . "$1" >/dev/null 2>&1; set +a; shift; for k; do printf "%s=%s\\0" "$k" "${!k-}"; done'
    rc, o, e = run(["env", "-i", "PATH=/usr/bin:/bin", "HOME=" + os.environ.get("HOME", "/root"),
                    "bash", "--noprofile", "--norc", "-c", script, "_", path] + keys, 15)
    vals = {}
    for item in o.split("\0"):
        if "=" in item:
            k, v = item.split("=", 1)
            vals[k] = v
    raw = {}
    with open(path, errors="replace") as f:
        for line in f:
            m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line.rstrip("\n"))
            if m:
                raw[m.group(1)] = m.group(2).strip().strip("'\"")
    for k in keys:
        if not vals.get(k) and raw.get(k):
            vals[k] = raw[k]   # a value bash cannot take as written (e.g. unquoted spaces): the text after "="
    return vals


def masked(k, v):
    return ("***" if v else "（空）") if SECRET.search(k) else v


def url_port_name(u):
    """'name:port' of a URL; for an address made of digits only the port is kept ('<IP>:port')."""
    m = re.match(r"^\w+://([^/:]+)(?::(\d+))?", u or "")
    if not m:
        return u or "-"
    host, port = m.group(1), m.group(2) or ""
    if re.match(r"^[0-9.]+$", host) or ":" in host:
        host = "<IP>"
    return "%s:%s" % (host, port)


TG_BOARD_UID = "vllm-pd-request-test-glm53"
TG_BOARD_TITLE = "面板二：请求时延拆解（P / D）（test-glm53）"
TG_DS_NAME = "vLLM ClickHouse（test-glm53）"
TG_DS_UID = "vllm-pd-clickhouse-test-glm53"
DERIVED = "（deploy.env 未写，按部署脚本默认规则）"
# panel-two pack scripts/names.py RULES: how a blank deploy.env key is derived (c = CLUSTER, B = PORT_BASE)
RULES = {"CLICKHOUSE_CONTAINER": ("name", "vllm-pd-clickhouse"), "CLICKHOUSE_HTTP_PORT": ("port", 0),
         "CENTRAL_CONTAINER": ("name", "vllm-pd-vector"), "PULLER_METRICS_PORT": ("port", 1),
         "GRAFANA_DS_UID": ("name", "vllm-pd-clickhouse"), "GRAFANA_DS_NAME": ("title", "vLLM ClickHouse"),
         "GRAFANA_BOARD_UID": ("name", "vllm-pd-request"), "GRAFANA_BOARD_TITLE": ("title", "面板二：请求时延拆解（P / D）"),
         "PROMETHEUS_CONTAINER": ("name", "vllm-pd-prometheus"), "PROMETHEUS_MARK": ("mark", "面板二")}
FIXED = {"DOCKER_NETWORK": NET, "NODE_MODE": "mock", "NODE_CONF_DIR": "$HOME/vllm-pd-diag/conf",
         "NODE_LOG_DIR": "$HOME/vllm-pd-diag/logs", "CLICKHOUSE_IMAGE": "clickhouse/clickhouse-server:26.8.36.3",
         "CLICKHOUSE_DATA_DIR": "（空：数据放在 Docker 卷）"}


def derive(k, c, base, vals):
    if k in ("CLUSTER",):
        return c
    if k == "PORT_BASE":
        return str(base)
    if k in RULES:
        kind, b = RULES[k]
        if kind == "name":
            return "%s-%s" % (b, c)
        if kind == "title":
            return "%s（%s）" % (b, c)
        if kind == "mark":
            return "%s %s" % (b, c)
        pb = vals.get("PORT_BASE") or base
        return str(int(pb) + b)
    return FIXED.get(k)


def same_derived(e, a):
    return a.replace(DERIVED, "") == str(e)


def ttl30(e, a):
    return bool(re.search(r"(?i)(toIntervalDay\(30\)|INTERVAL\s+30\s+DAY)", a))


def find_ds(dss, uid, names):
    for d in dss:
        if d.get("uid") == uid:
            return d
    for d in dss:
        if d.get("name") in names:
            return d
    return None


def ds_line(d):
    return "%s｜UID %s｜%s %s" % (d.get("name"), d.get("uid"), d.get("type"),
                                 url_port_name(d.get("url") or (d.get("jsonData") or {}).get("host")))


def empty_dir(dk, p):
    if not os.path.lexists(p):
        return "不存在"
    if not os.path.isdir(p) or os.path.islink(p):
        return exists_label(p)
    n = len(listdir(p))
    m = mounted_by(dk, p)
    return "%s，%s" % ("空目录" if n == 0 else "顶层 %d 项" % n, m)


# ============================================================================ result table

class Table(object):
    def __init__(self, overrides):
        self.items = []
        self.info = []
        self.overrides = overrides

    def add(self, iid, group, obj, expected, source, actual, same=None):
        """actual: a value, a callable returning it, or an Unreadable. same(expected, actual) -> bool."""
        if iid in self.overrides:
            expected = self.overrides[iid]
            source = "%s（--assume 改为 %s）" % (source, expected)
        try:
            a = actual() if callable(actual) else actual
            if isinstance(a, Unreadable):
                raise a
        except Skip:
            return
        except Unreadable as e:
            self.items.append((iid, group, obj, "未知" if expected is None else expected, source, str(e), "无法检查"))
            return
        except Exception as e:  # noqa: BLE001 - every item ends in one line
            self.items.append((iid, group, obj, "未知" if expected is None else expected, source,
                               "%s: %s" % (type(e).__name__, e), "无法检查"))
            return
        a = "-" if a is None else str(a)
        if expected is None:
            st = "未知"
        else:
            ok = same(expected, a) if same else str(expected) == a
            st = "一致" if ok else "不一致"
        self.items.append((iid, group, obj, "未知" if expected is None else expected, source, a, st))

    def note(self, s):
        self.info.append(s)

    def counts(self):
        c = {"一致": 0, "不一致": 0, "未知": 0, "无法检查": 0}
        for it in self.items:
            c[it[6]] += 1
        return c


def contains(exp, act):
    return str(exp) in act


def exists_label(p):
    if os.path.islink(p):
        return "链接 → %s" % os.readlink(p)
    if os.path.isdir(p):
        return "存在"
    if os.path.exists(p):
        return "存在（文件）"
    return "不存在"


def present(exp, act):
    return act == "存在" or act.startswith("存在") or (exp == "存在" and act.startswith("链接"))


# ============================================================================ docker

class Docker(object):
    def __init__(self):
        self.err = None
        self.cs = {}
        rc, o, e = run(["docker", "ps", "-aq", "--no-trunc"])
        if rc != 0:
            self.err = "docker 不可用：%s" % last(e, o)
            return
        ids = o.split()
        if ids:
            rc, o, e = run(["docker", "inspect"] + ids, 60)
            if rc != 0:
                self.err = "docker inspect 失败：%s" % last(e, o)
                return
            for c in json.loads(o):
                self.cs[c["Name"].lstrip("/")] = c

    def need(self):
        if self.err:
            raise Unreadable(self.err)

    def get(self, name):
        self.need()
        return self.cs.get(name)

    def state(self, name):
        c = self.get(name)
        if not c:
            return "不存在"
        st = c.get("State", {})
        return "运行中" if st.get("Running") else "已停止（%s）" % st.get("Status")

    def image(self, name):
        c = self.get(name)
        return c.get("Config", {}).get("Image") if c else "不存在"

    def host_ports(self, name):
        c = self.get(name)
        if not c:
            return "不存在"
        res = []
        for k, bl in sorted((c.get("HostConfig", {}).get("PortBindings") or {}).items()):
            for b in bl or []:
                res.append("%s->%s" % (b.get("HostPort"), k.replace("/tcp", "")))
        if not res and c.get("HostConfig", {}).get("NetworkMode") == "host":
            return "host 网络"
        return ",".join(sorted(set(res))) or "无"

    def has(self, name):
        """for items that depend on the container: Skip when it does not exist (its state item says so)"""
        if not self.get(name):
            raise Skip()
        return True

    def mounts(self, name):
        c = self.get(name)
        if not c:
            return []
        return [(m.get("Source", "") if m.get("Type") == "bind" else "卷 " + str(m.get("Name")), m.get("Destination"))
                for m in c.get("Mounts", [])]

    def networks(self, name):
        c = self.get(name)
        if not c:
            return "不存在"
        return ",".join(sorted((c.get("NetworkSettings", {}).get("Networks") or {}).keys())) or "无"

    def pid(self, name):
        c = self.get(name)
        if not c or not c.get("State", {}).get("Running"):
            raise Unreadable("容器 %s %s" % (name, "不存在" if not c else "没有在运行"))
        return int(c["State"]["Pid"])


def mounted_by(dk, path):
    if dk.err:
        return "（docker 不可用）"
    path = os.path.normpath(path)
    users = sorted({n for n in dk.cs for src, _d in dk.mounts(n)
                    if src.startswith("/") and src not in ("/", "/data")
                    and (os.path.normpath(src) == path or os.path.normpath(src).startswith(path + "/")
                         or path.startswith(os.path.normpath(src) + "/"))})
    return ("挂载于 " + "、".join(users)) if users else "未挂载"


def folder_facts(dk, path):
    if not os.path.lexists(path):
        return "不存在"
    if os.path.islink(path):
        return "链接 → %s" % os.readlink(path)
    try:
        names = os.listdir(path)
        newest = os.lstat(path).st_mtime
        for n in names:
            try:
                newest = max(newest, os.lstat(os.path.join(path, n)).st_mtime)
            except OSError:
                pass
        sv = os.statvfs(path)
        return "%s，顶层 %d 项，最新修改 %s，剩余 %s" % (mounted_by(dk, path), len(names), tstamp(newest),
                                                 human(sv.f_bavail * sv.f_frsize))
    except OSError as e:
        raise Unreadable(e.strerror)


def tree_facts(path, cap=20000):
    """exists / top-level entries / size by a walk that stops after cap files (no du)."""
    if not os.path.lexists(path):
        return "不存在"
    if os.path.isfile(path):
        return "文件，%s" % human(os.path.getsize(path))
    n = size = 0
    for dp, _dns, fns in os.walk(path):
        for f in fns:
            n += 1
            try:
                size += os.lstat(os.path.join(dp, f)).st_size
            except OSError:
                pass
            if n >= cap:
                return "顶层 %d 项，前 %d 个文件已有 %s（未数完）" % (len(os.listdir(path)), n, human(size))
    return "顶层 %d 项，%d 个文件，%s" % (len(os.listdir(path)), n, human(size))


def listdir(p):
    try:
        return sorted(os.listdir(p))
    except OSError as e:
        raise Unreadable("%s：%s" % (p, e.strerror))


# ============================================================================ Grafana and ClickHouse

class Grafana(object):
    def __init__(self, env_files):
        self.err, self.url, self.auth, self.src = None, None, None, None
        self.cache = {}
        why = "没有含 GRAFANA_URL 的 env 文件"
        for p in env_files:
            v = env_values(p, ["GRAFANA_URL", "GRAFANA_USER", "GRAFANA_PASSWORD", "GRAFANA_TOKEN"]) or {}
            u = (v.get("GRAFANA_URL") or "").rstrip("/")
            if not u:
                continue
            a = ("Bearer " + v["GRAFANA_TOKEN"]) if v.get("GRAFANA_TOKEN") else "Basic " + base64.b64encode(
                ("%s:%s" % (v.get("GRAFANA_USER") or "admin", v.get("GRAFANA_PASSWORD") or "")).encode()).decode()
            try:
                self.url, self.auth = u, a
                self.get("/api/datasources")
                self.src = p
                break
            except Unreadable as e:
                self.url = None
                why = "%s 的地址或账号不能用：%s" % (p, e)
        if not self.url:
            self.err = "查不了 Grafana：" + why

    def get(self, path, auth=True):
        if not self.url:
            raise Unreadable(self.err)
        if path in self.cache:
            return self.cache[path]
        req = urllib.request.Request(self.url + path, headers={"Authorization": self.auth} if auth else {})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                v = json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            raise Unreadable("GET %s：%s" % (path, e))
        self.cache[path] = v
        return v

    def folders(self):
        """uid -> (title, parent uid or "")"""
        res = {}
        for f in self.get("/api/search?type=dash-folder&limit=5000"):
            res[f.get("uid")] = (f.get("title"), f.get("folderUid") or "")
        return res

    def boards(self):
        return {b.get("uid"): b for b in self.get("/api/search?type=dash-db&limit=5000")}

    def folder_path(self, uid):
        fs = self.folders()
        parts, seen = [], set()
        while uid and uid in fs and uid not in seen:
            seen.add(uid)
            parts.insert(0, fs[uid][0])
            uid = fs[uid][1]
        return "/".join(parts) or "General"


def ch_query(container, password, sql):
    cmd = ["docker", "exec", container, "clickhouse-client", "--readonly=1", "--format", "TSV", "-q", sql]
    if password:
        cmd[4:4] = ["--password", password]
    rc, o, e = run(cmd, 30)
    if rc != 0:
        raise Unreadable(last(e, o))
    return [l.split("\t") for l in o.splitlines() if l]


class ClickHouse(object):
    def __init__(self, dk, container, password):
        self.name, self.err, self.tables, self.ver = container, None, {}, None
        try:
            dk.pid(container)
            self.ver = ch_query(container, password, "SELECT version()")[0][0]
            rows = ch_query(container, password,
                            "SELECT database, name, engine, extract(engine_full, 'TTL (.+?)(?: SETTINGS |$)') "
                            "FROM system.tables WHERE database NOT IN ('system','INFORMATION_SCHEMA','information_schema')")
            parts = {(r[0], r[1]): (r[2], r[3]) for r in ch_query(
                container, password, "SELECT database, table, sum(rows), sum(data_compressed_bytes) FROM system.parts "
                                     "WHERE active GROUP BY database, table")}
            for db, t, eng, ttl in rows:
                r, b = parts.get((db, t), ("0", "0"))
                self.tables["%s.%s" % (db, t)] = (eng, int(r), int(b), ttl)
        except Unreadable as e:
            self.err = str(e)

    def need(self):
        if self.err:
            raise Unreadable(self.err)


# ============================================================================ monitoring machine

def panel2_dir(c):
    return os.path.join(ROOT, "vllm-pd-grafana-panel2", c)


def proc_list():
    res = []
    for pid in os.listdir("/proc") if os.path.isdir("/proc") else []:
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        if cmd:
            res.append((int(pid), cmd))
    return res


def read_pidfile(p):
    try:
        with open(p) as f:
            v = f.read().strip()
    except OSError:
        return "没有 pid 文件"
    return "在运行" if v.isdigit() and os.path.exists("/proc/" + v) else "pid %s 不在" % v


def last_log_time(p):
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 8192))
            ls = [l for l in f.read().decode("utf-8", "replace").splitlines() if l.strip()]
    except OSError:
        return "没有日志"
    return ls[-1].split(" ")[0] if ls else "日志为空"


def check_monitor(T):
    dk = Docker()
    add = T.add
    if dk.err:
        T.note(dk.err)

    # ---------------- containers
    G = "容器"
    for c, base, _top, _sub, src, ver in PANEL1:
        n = "vllm-pd-prometheus-" + c
        s = src + ("" if ver else "（未核实）")
        add("C-%s-1" % c, G, n + " 状态", "运行中", S02 % 57, lambda n=n: dk.state(n))
        add("C-%s-2" % c, G, n + " 镜像", "prom/prometheus:" + PROM_TAG, S02 % 36,
            lambda n=n: dk.has(n) and dk.image(n), lambda e, a: a.endswith(":" + PROM_TAG))
        add("C-%s-3" % c, G, n + " 主机端口", "%d->9090" % (base + 2), s, lambda n=n: dk.has(n) and dk.host_ports(n), contains)
        add("C-%s-4" % c, G, n + " 数据卷", "vllm-pd-prometheus-data-" + c, S02 % 57,
            lambda n=n: dk.has(n) and ",".join(s for s, _d in dk.mounts(n) if s.startswith("卷 ")) or "无", contains)
        add("C-%s-5" % c, G, n + " 配置挂载", "%s/vllm-pd-grafana-panel1/%s/prometheus" % (ROOT, c), S02 % 56,
            lambda n=n: dk.has(n) and ",".join(s for s, _d in dk.mounts(n) if s.startswith("/")) or "无", contains)
        add("C-%s-6" % c, G, n + " 网络", NET, SITE, lambda n=n: dk.has(n) and dk.networks(n), contains)
    for n, exp, src in (("vllm-pd-clickhouse-glm53-1", "运行中", S02 % 71), ("vllm-pd-vector-glm53-1", "运行中", S02 % 71),
                        ("vllm-pd-clickhouse-test-glm53", "运行中", S02 % 90),
                        ("vllm-pd-prometheus-test-glm53", "运行中", S02 % 91)):
        add("C-%s-1" % n, G, n + " 状态", exp, src, lambda n=n: dk.state(n))
        add("C-%s-6" % n, G, n + " 网络", NET, SITE, lambda n=n: dk.has(n) and dk.networks(n), contains)
    add("C-ch-glm53-1-2", G, "vllm-pd-clickhouse-glm53-1 镜像", CH_IMAGE, S02 % 37 + "（T53 之后未核实）",
        lambda: dk.has("vllm-pd-clickhouse-glm53-1") and dk.image("vllm-pd-clickhouse-glm53-1"))
    add("C-ch-glm53-1-3", G, "vllm-pd-clickhouse-glm53-1 主机端口", "18100->8123", SITE,
        lambda: dk.has("vllm-pd-clickhouse-glm53-1") and dk.host_ports("vllm-pd-clickhouse-glm53-1"))
    for n in ("vllm-pd-clickhouse-glm53-1", "vllm-pd-vector-glm53-1"):
        add("C-%s-5" % n, G, n + " 数据目录挂载", "/data/clickhouse-glm53-1", SITE,
            lambda n=n: dk.has(n) and (",".join(s for s, _d in dk.mounts(n) if s.startswith("/")) or "无"), contains)
    add("C-vec-glm53-1-2", G, "vllm-pd-vector-glm53-1 镜像", VECTOR_IMAGE, S02 % 37, lambda: dk.has("vllm-pd-vector-glm53-1") and dk.image("vllm-pd-vector-glm53-1"))
    add("C-ch-test-glm53-2", G, "vllm-pd-clickhouse-test-glm53 镜像", CH_IMAGE, S02 % 37 + "（T62 之后未核实）",
        lambda: dk.has("vllm-pd-clickhouse-test-glm53") and dk.image("vllm-pd-clickhouse-test-glm53"))
    add("C-ch-test-glm53-3", G, "vllm-pd-clickhouse-test-glm53 主机端口", "18190", SITE,
        lambda: dk.has("vllm-pd-clickhouse-test-glm53") and dk.host_ports("vllm-pd-clickhouse-test-glm53"), lambda e, a: a.split("->")[0] == e or (e + "->") in a)
    add("C-ch-test-glm53-5", G, "vllm-pd-clickhouse-test-glm53 数据目录", "/data/clickhouse-test-glm53", SITE,
        lambda: dk.has("vllm-pd-clickhouse-test-glm53") and (",".join(s for s, _d in dk.mounts("vllm-pd-clickhouse-test-glm53") if s.startswith("/")) or "无"), contains)
    add("C-vec-test-glm53-1", G, "vllm-pd-vector-test-glm53 状态", "不在运行", S02 % 116,
        lambda: dk.state("vllm-pd-vector-test-glm53"), lambda e, a: a != "运行中")
    add("C-ap-1", G, "vllm-pd-prometheus-test-glm53 配置挂载", ROOT + "/vllm-pd-prometheus-test-glm53",
        S02 % 91, lambda: dk.has("vllm-pd-prometheus-test-glm53") and (",".join(s for s, _d in dk.mounts("vllm-pd-prometheus-test-glm53") if s.startswith("/")) or "无"),
        contains)
    add("C-ap-2", G, "vllm-pd-prometheus-test-glm53 镜像与网络", "与 vllm-pd-prometheus-glm53-1 相同", S02 % 108,
        lambda: dk.has("vllm-pd-prometheus-test-glm53") and dk.has("vllm-pd-prometheus-glm53-1") and "相同" if (dk.image("vllm-pd-prometheus-test-glm53"), dk.networks("vllm-pd-prometheus-test-glm53")) ==
        (dk.image("vllm-pd-prometheus-glm53-1"), dk.networks("vllm-pd-prometheus-glm53-1")) else "%s %s" % (
            dk.image("vllm-pd-prometheus-test-glm53"), dk.networks("vllm-pd-prometheus-test-glm53")),
        lambda e, a: a == "相同")
    add("C-old-test", G, "vllm-pd-prometheus-test（整理脚本删除对象）", None, "监控机整理脚本",
        lambda: dk.state("vllm-pd-prometheus-test"))
    gname = lambda: ([n for n in sorted(dk.cs) if "grafana" in (n + dk.image(n)).lower()] or [None])[0]
    add("C-gf-1", G, "Grafana 容器名", None, S02 % 129, lambda: gname() or "没有 Grafana 容器")
    add("C-gf-2", G, "Grafana 镜像", "grafana/grafana:" + GRAFANA_VERSION, S02 % 34,
        lambda: dk.image(gname()) if gname() else "没有 Grafana 容器", lambda e, a: a.endswith(":" + GRAFANA_VERSION))
    add("C-gf-3", G, "Grafana 主机端口", "3000", S02 % 34 + "（未核实）",
        lambda: dk.host_ports(gname()) if gname() else "没有 Grafana 容器", lambda e, a: ("3000->" in a) or a == "host 网络")
    add("C-net", G, "docker 网络 " + NET, "存在", SITE,
        lambda: "存在" if run(["docker", "network", "inspect", NET])[0] == 0 else "不存在")
    ours = set(["vllm-pd-prometheus-" + c[0] for c in PANEL1] + ["vllm-pd-clickhouse-glm53-1", "vllm-pd-vector-glm53-1",
               "vllm-pd-clickhouse-test-glm53", "vllm-pd-prometheus-test-glm53", "vllm-pd-vector-test-glm53",
               "vllm-pd-prometheus-test"])

    def others():
        dk.need()
        g = gname()
        rest = ["%s(%s,%s)" % (n, dk.image(n), "运行" if dk.state(n) == "运行中" else "停") for n in sorted(dk.cs)
                if n not in ours and n != g]
        return "；".join(rest) or "无"
    add("C-other", G, "其他容器（不在以上清单）", None, S02 % 130, others)

    # ---------------- folders
    G = "目录"
    for n in SIX_DATA_DIRS:
        p = "/data/" + n
        if n.startswith("vllm-"):
            add("D-data-" + n, G, p + "（残留，可删）", "空目录，未挂载", SITE, lambda p=p: empty_dir(dk, p))
        else:
            add("D-data-" + n, G, p, None, "用户 11-28 报告的残留", lambda p=p: folder_facts(dk, p))
    known_data = set(SIX_DATA_DIRS) | {"grafana", os.path.basename(OLD), "pd-diag"}
    add("D-data-other", G, "/data 下其他条目", None, S02 % 31, lambda: "、".join(
        n for n in listdir("/data") if n not in known_data) or "无")
    for n in ("vllm-pd-grafana-panel1", "vllm-pd-grafana-panel2", "vllm-pd-prometheus-test-glm53", "vllm-timing"):
        p = os.path.join(ROOT, n)
        add("D-gr-" + n, G, p, "存在", S02 % 31 if n != "vllm-timing" else "用户 11-28 报告", lambda p=p: exists_label(p))
    add("D-gr-timing", G, ROOT + "/vllm-timing 挂载", None, "用户 11-28 报告",
        lambda: folder_facts(dk, ROOT + "/vllm-timing"))
    add("D-gr-other", G, ROOT + " 下其他条目", None, S02 % 31, lambda: "、".join(
        n for n in listdir(ROOT) if n not in ("vllm-pd-grafana-panel1", "vllm-pd-grafana-panel2",
                                             "vllm-pd-prometheus-test-glm53", "vllm-timing")) or "无")
    add("D-p1-list", G, ROOT + "/vllm-pd-grafana-panel1 下的集群", " ".join(sorted(c[0] for c in PANEL1)), S02 % 56,
        lambda: " ".join(n for n in listdir(ROOT + "/vllm-pd-grafana-panel1")
                         if os.path.isdir(os.path.join(ROOT, "vllm-pd-grafana-panel1", n))))
    add("D-p2-list", G, ROOT + "/vllm-pd-grafana-panel2 下的集群", "glm53-1 test-glm53", S02 % 69 + "、89",
        lambda: " ".join(n for n in listdir(ROOT + "/vllm-pd-grafana-panel2")
                         if os.path.isdir(os.path.join(ROOT, "vllm-pd-grafana-panel2", n))))
    for c, *_r in PANEL1:
        for rel in ("prometheus/inventory.json", "prometheus/prometheus.yml"):
            p = "%s/vllm-pd-grafana-panel1/%s/%s" % (ROOT, c, rel)
            add("D-p1-%s-%s" % (c, rel.split("/")[1].split(".")[0]), G, p, "存在（文件）", S02 % 56,
                lambda p=p: exists_label(p))

    def either(n):
        a, b = os.path.join(OLD, n), os.path.join(TOOLS, n)
        if os.path.isdir(b) and not os.path.islink(b):
            return "在 %s" % TOOLS
        if os.path.isdir(a) and not os.path.islink(a):
            return "在 %s" % OLD
        return "两处都没有"
    for n, src in (("vllm-pd-panel1-v1.2", S02 % 59), ("vllm-pd-boards-clusters", S02 % 61),
                   ("vllm-pd-panel1-update-2", S02 % 59)):
        add("D-tool-" + n, G, n + " 位置", "在 %s 或 %s" % (OLD, TOOLS), src + "；监控机整理脚本", lambda n=n: either(n),
            lambda e, a: a.startswith("在 "))
    add("D-tool-1112", G, "vllm-pd-panel1-update-1112 位置", None, S02 % 130, lambda: either("vllm-pd-panel1-update-1112"))
    add("D-old-patch", G, OLD + "/vllm-pd-patch-glm53", None, S02 % 119, lambda: exists_label(OLD + "/vllm-pd-patch-glm53"))
    add("D-old-other", G, OLD + " 下其他条目", None, S02 % 32, lambda: "、".join(
        n for n in listdir(OLD) if n not in ("vllm-pd-panel1-v1.2", "vllm-pd-boards-clusters", "vllm-pd-panel1-update-2",
                                             "vllm-pd-panel1-update-1112", "vllm-pd-patch-glm53")) or "无")
    add("D-tools", G, TOOLS + " 内容", None, "监控机整理脚本", lambda: "、".join(listdir(TOOLS)) if os.path.isdir(TOOLS) else "不存在")
    home = os.environ.get("HOME") or "/root"
    for rel, src in (("vllm-pd-boards/vars.sh", S02 % 60), ("vllm-pd-panel1-v1.2-backup", S02 % 60), ("t53", S02 % 70),
                     ("t62/t62-site/t62_site.py", S02 % 88), ("t62/glm53-test.conf", S02 % 88),
                     (".vllm-pd-clusters.json", S02 % 33)):
        p = os.path.join(home, rel)
        add("D-home-" + rel.split("/")[-1], G, p, "存在", src, lambda p=p: exists_label(p), present)

    t53b = sorted(glob.glob(home + "/t53/state2/*/back"))
    add("D-home-t53", G, home + "/t53", None, S02 % 70, lambda: tree_facts(home + "/t53"))
    add("D-home-t53-back", G, home + "/t53/state2/*/back", None, "用户 11-28 询问",
        lambda: "；".join("%s %s" % (p.split("/")[-2], tree_facts(p)) for p in t53b) or "没有")
    add("D-home-t62", G, home + "/t62", None, S02 % 88, lambda: tree_facts(home + "/t62"))
    add("D-home-t62-backup", G, home + "/t62/backup/<机器地址>", None, S02 % 88, lambda: "；".join(
        "%s %s" % ("<IP>" if re.match(r"^[0-9.]+$", n) else n, tree_facts(os.path.join(home, "t62/backup", n)))
        for n in listdir(home + "/t62/backup")) if os.path.isdir(home + "/t62/backup") else "没有")

    def home_other():
        known = {"vllm-pd-boards", "vllm-pd-panel1-v1.2-backup", "t53", "t62"}
        return "、".join(n for n in listdir(home) if not n.startswith(".") and n not in known) or "无"
    add("D-home-other", G, home + " 下其他条目", None, S02 % 60, home_other)

    def vars_paths():
        vs = home + "/vllm-pd-boards/vars.sh"
        if not os.path.isfile(vs):
            raise Unreadable("没有 " + vs)
        script = '. "$1" >/dev/null 2>&1; for k in T U12 UP1 P1 ENV2; do printf "%s=%s\\n" "$k" "${!k-}"; done'
        rc, o, e = run(["bash", "--noprofile", "--norc", "-c", script, "_", vs], 20)
        bad = [l for l in o.splitlines() if "=" in l and not os.path.exists(l.split("=", 1)[1] or "/nonexistent-")]
        return "都存在" if not bad else "不存在：" + "；".join(bad)
    add("D-vars", G, "vars.sh 的 T、U12、UP1、P1、ENV2 指向", "都存在", S02 % 60, vars_paths)

    # ---------------- panel-two configuration files
    G = "面板二配置"
    d1, d2 = panel2_dir("glm53-1"), panel2_dir("test-glm53")
    e1 = env_values(d1 + "/deploy.env")
    ch_env = env_values(d2 + "/clickhouse.env")
    gf_env = env_values(d2 + "/grafana.env")

    def key(vals, path, k, cluster="glm53-1", base=18100):
        if vals is None:
            raise Unreadable("没有 " + path)
        if vals.get(k):
            return masked(k, vals[k])
        d = derive(k, cluster, base, vals)
        return "%s%s" % (d, DERIVED) if d is not None else "（没有这个键，部署脚本没有默认规则）"
    for k, exp, src in (("CLUSTER", "glm53-1", S02 % 69), ("PORT_BASE", "18100", S02 % 45),
                        ("CLICKHOUSE_CONTAINER", "vllm-pd-clickhouse-glm53-1", S02 % 71),
                        ("CENTRAL_CONTAINER", "vllm-pd-vector-glm53-1", S02 % 71),
                        ("PROMETHEUS_CONTAINER", "vllm-pd-prometheus-glm53-1", S02 % 73),
                        ("GRAFANA_BOARD_UID", "vllm-pd-request-glm53-1", S02 % 74),
                        ("GRAFANA_BOARD_TITLE", "vLLM 请求侧拆解面板", S02 % 74),
                        ("GRAFANA_DS_NAME", "vllm-pd-clickhouse-glm53-1", S02 % 74),
                        ("NODE_CONF_DIR", "/data/pd-diag/conf", S02 % 75), ("NODE_LOG_DIR", "/data/pd-diag/logs", S02 % 75),
                        ("DOCKER_NETWORK", NET, SITE), ("CLICKHOUSE_IMAGE", CH_IMAGE, S02 % 37),
                        ("CLICKHOUSE_HTTP_PORT", "18100", SITE), ("PULLER_METRICS_PORT", "18101", "部署脚本规则 PORT_BASE+1"),
                        ("CLICKHOUSE_DATA_DIR", "/data/clickhouse-glm53-1", SITE), ("NODE_MODE", "vllm", S02 % 45)):
        add("F-g1-" + k, G, "glm53-1 deploy.env " + k, exp, src, lambda k=k: key(e1, d1 + "/deploy.env", k), same_derived)
    add("F-g1-VERSION", G, "glm53-1 面板二版本文件 VERSION", "v3.4-extra-t53", SITE + "；T53 两步包 lib/common.sh EXP_PANEL_VERSION", lambda: open(d1 + "/VERSION").read().strip()
        if os.path.isfile(d1 + "/VERSION") else Unreadable("没有 %s/VERSION" % d1))
    for f in ("clickhouse.env", "grafana.env", "ingest.json", "deploy.env.before-t62", "pack-before-t62.tar.gz",
              "inventory.json"):
        add("F-tg-file-" + f, G, d2 + "/" + f, "存在（文件）", S02 % 89, lambda f=f: exists_label(d2 + "/" + f))
    add("F-tg-file-deploy.env", G, d2 + "/deploy.env", "不存在", S02 % 89 + "（T62 拆分）", lambda: exists_label(d2 + "/deploy.env"))
    add("F-tg-VERSION", G, "test-glm53 面板二版本文件 VERSION", "v4.0-extra", S02 % 87, lambda: open(d2 + "/VERSION").read().strip()
        if os.path.isfile(d2 + "/VERSION") else Unreadable("没有 %s/VERSION" % d2))
    for k, exp, src in (("CLICKHOUSE_CONTAINER", "vllm-pd-clickhouse-test-glm53", S02 % 90),
                        ("CLICKHOUSE_HTTP_PORT", "18190", SITE),
                        ("PROMETHEUS_CONTAINER", "vllm-pd-prometheus-test-glm53", S02 % 91),
                        ("PROMETHEUS_YML", ROOT + "/vllm-pd-prometheus-test-glm53/prometheus.yml", S02 % 91),
                        ("CLICKHOUSE_DATA_DIR", "/data/clickhouse-test-glm53", SITE), ("INGEST_STATUS_PORT", "18191", SITE)):
        add("F-tg-ch-" + k, G, "test-glm53 clickhouse.env " + k, exp, src, lambda k=k: key(ch_env, d2 + "/clickhouse.env", k))
    for k, exp in (("GRAFANA_BOARD_UID", TG_BOARD_UID), ("GRAFANA_BOARD_TITLE", TG_BOARD_TITLE),
                   ("GRAFANA_DS_NAME", TG_DS_NAME), ("GRAFANA_DS_UID", TG_DS_UID)):
        add("F-tg-gf-" + k, G, "test-glm53 grafana.env " + k, exp, SITE,
            lambda k=k: key(gf_env, d2 + "/grafana.env", k, "test-glm53", 18190))

    def fetcher_port():
        with open(d2 + "/ingest.json") as f:
            return url_port_name(json.load(f).get("fetcher_url", "")).split(":")[-1]
    add("F-tg-fetcher", G, "test-glm53 ingest.json fetcher_url 端口", "9300", S02 % 86,
        lambda: fetcher_port() if os.path.isfile(d2 + "/ingest.json") else Unreadable("没有 ingest.json"))

    def mark_in_yml():
        if e1 is None:
            raise Unreadable("没有 glm53-1 的 deploy.env")
        mark = e1.get("PROMETHEUS_MARK") or "vllm-timing"
        y = e1.get("PROMETHEUS_YML") or "%s/vllm-pd-grafana-panel1/glm53-1/prometheus/prometheus.yml" % ROOT
        with open(y, errors="replace") as f:
            return "有" if mark in f.read() else "没有（%s 中找不到 %s）" % (y, mark)
    add("F-g1-alert", G, "glm53-1 面板一 prometheus.yml 中面板二的标记段", "有", S02 % 73, mark_in_yml)

    def alert_rule():
        hits = []
        for p in glob.glob(ROOT + "/vllm-pd-prometheus-test-glm53/**", recursive=True):
            if os.path.isfile(p) and os.path.getsize(p) < 2 << 20:
                with open(p, errors="replace") as f:
                    if "PdIngestDocsStale" in f.read():
                        hits.append(os.path.basename(p))
        return "有（%s）" % "、".join(hits) if hits else "没有"
    add("F-tg-alert", G, "test-glm53 只告警 Prometheus 的规则 PdIngestDocsStale", "有", S02 % 91, alert_rule,
        lambda e, a: a.startswith("有"))

    # ---------------- ingest
    G = "入库进程"
    ps = proc_list()
    ing = [(p, c) for p, c in ps if "pd_ingest.py" in c and "bash -c" not in c]
    add("I-tg-run", G, "test-glm53 入库进程 pd_ingest.py", "在运行", S02 % 21,
        lambda: "在运行（pid %s）" % ",".join(str(p) for p, c in ing if "test-glm53" in c) if any(
            "test-glm53" in c for p, c in ing) else "没有在运行", lambda e, a: a.startswith("在运行"))
    add("I-tg-loop", G, "test-glm53 run/ingest-loop.pid", "在运行", "T62 包 bin/ingest.sh",
        lambda: read_pidfile(d2 + "/run/ingest-loop.pid"))
    add("I-tg-log", G, "test-glm53 logs/ingest.log 最后一条时间", None, S02 % 100, lambda: last_log_time(d2 + "/logs/ingest.log"))
    add("I-g1-none", G, "glm53-1 入库进程（旧方案不用）", "没有", S02 % 45,
        lambda: "有" if any("glm53-1" in c and "test-glm53" not in c for p, c in ing) else "没有")

    # ---------------- registry
    G = "注册表"
    rp = os.path.join(home, ".vllm-pd-clusters.json")

    def reg():
        try:
            with open(rp) as f:
                r = json.load(f)
        except (OSError, ValueError) as e:
            raise Unreadable("%s：%s" % (rp, getattr(e, "strerror", None) or e))
        if r.get("format") != "vllm-pd-clusters":
            raise Unreadable("%s 不是集群注册表" % rp)
        return r.get("clusters") or {}
    add("R-list", G, "已登记的集群", " ".join(sorted(ALL_CLUSTERS)), S02 % 25, lambda: " ".join(sorted(reg())))
    for c, base, top, _sub, src, ver in PANEL1 + [("test-glm53", 18190, None, "", SITE, True)]:
        panels = "panel1,panel2" if c == "glm53-1" else ("panel2" if c == "test-glm53" else "panel1")
        add("R-%s-panels" % c, G, c + " 登记的面板", panels, S02 % 25,
            lambda c=c: ",".join(k for k in ("panel1", "panel2") if k in (reg().get(c) or {})) or "未登记")
        add("R-%s-port" % c, G, c + " port_base", str(base), src + ("" if ver else "（未核实）"),
            lambda c=c: str((reg().get(c) or {}).get("port_base", "未登记")))
        add("R-%s-group" % c, G, c + " model_group", (c.replace("dsv41", "dsv4.1") if top else None), SITE,
            lambda c=c: str((reg().get(c) or {}).get("model_group", "未登记")))

    # ---------------- Grafana
    G = "Grafana"
    gf = Grafana([d1 + "/deploy.env", d2 + "/grafana.env"])
    if gf.err:
        T.note(gf.err)
    add("G-ver", G, "Grafana 版本", GRAFANA_VERSION, S02 % 34, lambda: gf.get("/api/health", auth=False).get("version"))
    add("G-plugin", G, "ClickHouse 插件版本", CH_PLUGIN, S02 % 34,
        lambda: ((gf.get("/api/plugins/grafana-clickhouse-datasource/settings").get("info") or {}).get("version")))
    add("G-nfolder", G, "文件夹数", "11", S02 % 25 + "（未核实）", lambda: str(len(gf.folders())))
    add("G-nboard", G, "看板数", "18", S02 % 25 + "（未核实）", lambda: str(len(gf.boards())))
    dss = lambda: gf.get("/api/datasources")
    add("G-nds", G, "数据源数", "10", S02 % 25 + "（未核实）", lambda: str(len(dss())))
    add("G-nds-prom", G, "Prometheus 数据源数", "8", S02 % 25, lambda: str(sum(1 for d in dss() if d.get("type") == "prometheus")))
    add("G-nds-ch", G, "ClickHouse 数据源数", "2", S02 % 25,
        lambda: str(sum(1 for d in dss() if "clickhouse" in (d.get("type") or ""))))
    fpaths = lambda: sorted(gf.folder_path(u) for u in gf.folders())
    for fp, src in (("GLM-5.3", S02 % 10), ("GLM-5.3-test", S02 % 14), ("dsv4.1-flash", S02 % 15)):
        add("G-folder-" + fp, G, "文件夹 " + fp, "存在", src, lambda fp=fp: "存在" if fp in fpaths() else "不存在")
    for c, base, top, sub, src, ver in PANEL1:
        fp = top + "/" + sub
        for side in ("prefill", "decode"):
            uid = "vllm-pd-%s-%s" % (side, c)

            def where(uid=uid):
                b = gf.boards().get(uid)
                return gf.folder_path(b.get("folderUid")) if b else "看板不在"

            def v12(uid=uid, side=side):
                if uid not in gf.boards():
                    raise Skip()
                d = gf.get("/api/dashboards/uid/" + uid).get("dashboard") or {}
                ps, stack = [], list(d.get("panels") or [])
                while stack:
                    p = stack.pop()
                    ps.append(p)
                    stack += p.get("panels") or []
                n = sum(1 for p in ps if (p.get("description") or "").strip())
                has807 = any(p.get("id") == 807 for p in ps)
                return "panel 807 %s，带说明 %d 个" % ("有" if has807 else "没有", n)
            add("G-%s-%s" % (c, side), G, "看板 %s 所在文件夹" % uid, fp, S02 % (12 if c == "glm53-1" else 11), where)
            add("G-%s-%s-v12" % (c, side), G, "看板 %s 的 v1.2 标记" % uid, "panel 807 有，带说明 %d 个" % PANEL1_DESC[side],
                S02 % 55 + "；Caching 8629d97", v12)
        ds = "prometheus-" + c

        def ds_target(ds=ds, c=c):
            m = find_ds(dss(), ds, (ds, "Prometheus（%s）" % c))
            return ds_line(m) if m else "没有 UID 或名称为 %s 的数据源" % ds
        add("G-ds-" + c, G, "数据源 %s（按 UID 和名称）" % ds, "prometheus vllm-pd-prometheus-%s:9090 或 <IP>:%d" % (c, base + 2),
            S02 % 58, ds_target, lambda e, a, c=c, base=base: ("prometheus vllm-pd-prometheus-%s:9090" % c) in a or (
                "prometheus <IP>:%d" % (base + 2)) in a)
    add("G-req-g1", G, "看板 vllm-pd-request-glm53-1 标题与文件夹", "vLLM 请求侧拆解面板｜GLM-5.3/glm53-1", S02 % 74,
        lambda: ("%s｜%s" % (gf.boards()["vllm-pd-request-glm53-1"].get("title"),
                            gf.folder_path(gf.boards()["vllm-pd-request-glm53-1"].get("folderUid"))))
        if "vllm-pd-request-glm53-1" in gf.boards() else "看板不在")
    add("G-ds-ch-g1", G, "数据源 vllm-pd-clickhouse-glm53-1（按 UID 和名称）", "存在", S02 % 74,
        lambda: ("存在：" + ds_line(find_ds(dss(), "vllm-pd-clickhouse-glm53-1", ("vllm-pd-clickhouse-glm53-1", "vLLM ClickHouse（glm53-1）"))))
        if find_ds(dss(), "vllm-pd-clickhouse-glm53-1", ("vllm-pd-clickhouse-glm53-1", "vLLM ClickHouse（glm53-1）")) else "不存在",
        lambda e, a: a.startswith("存在"))
    add("G-ds-ch-tg", G, "数据源 %s（按 UID 和名称）" % TG_DS_UID, "%s｜UID %s" % (TG_DS_NAME, TG_DS_UID), SITE,
        lambda: ds_line(find_ds(dss(), TG_DS_UID, (TG_DS_NAME,))) if find_ds(dss(), TG_DS_UID, (TG_DS_NAME,)) else "不存在",
        lambda e, a: a.startswith("%s｜UID %s" % (TG_DS_NAME, TG_DS_UID)))
    add("G-tg-board-uid", G, "看板 " + TG_BOARD_UID, TG_BOARD_TITLE, SITE,
        lambda: gf.boards()[TG_BOARD_UID].get("title") if TG_BOARD_UID in gf.boards() else "看板不在")
    tg_boards = lambda: [b for b in gf.boards().values() if gf.folder_path(b.get("folderUid")) == "GLM-5.3-test"]
    add("G-tg-n", G, "文件夹 GLM-5.3-test 中的看板数", "1", S02 % 14, lambda: str(len(tg_boards())))
    add("G-old-ds", G, "旧面板一的数据源 Prometheus", "不存在", SITE,
        lambda: "存在" if any(d.get("name") == "Prometheus" for d in dss()) else "不存在")
    add("G-old-folder", G, "旧面板一的文件夹 vLLM P/D", "不存在", SITE,
        lambda: "存在" if "vLLM P/D" in fpaths() else "不存在")
    known_boards = {"vllm-pd-%s-%s" % (s, c[0]) for c in PANEL1 for s in ("prefill", "decode")} | {"vllm-pd-request-glm53-1"}
    add("G-other-boards", G, "其他看板", None, S02 % 131, lambda: "；".join(
        "%s〔%s〕%s" % (b.get("title"), u, gf.folder_path(b.get("folderUid"))) for u, b in sorted(gf.boards().items())
        if u not in known_boards and b not in tg_boards()) or "无")
    if gf.url:
        try:
            for d in sorted(dss(), key=lambda x: x.get("name", "")):
                T.note("数据源 %s | %s | uid %s | %s" % (d.get("name"), d.get("type"), d.get("uid"), url_port_name(
                    d.get("url") or (d.get("jsonData") or {}).get("host"))))
            for u, b in sorted(gf.boards().items(), key=lambda x: gf.folder_path(x[1].get("folderUid"))):
                T.note("看板 %s | %s | %s" % (gf.folder_path(b.get("folderUid")), b.get("title"), u))
        except Unreadable as e:
            T.note("Grafana 明细读不了：%s" % e)

    # ---------------- ClickHouse
    G = "ClickHouse"
    pw1 = (e1 or {}).get("CH_ADMIN_PASSWORD", "")
    pw2 = (ch_env or {}).get("CH_ADMIN_PASSWORD", "")
    ch1 = ClickHouse(dk, "vllm-pd-clickhouse-glm53-1", pw1) if not dk.err else None
    ch2 = ClickHouse(dk, "vllm-pd-clickhouse-test-glm53", pw2) if not dk.err else None

    def chv(ch, f):
        if ch is None:
            raise Unreadable(dk.err)
        ch.need()
        return f(ch)
    add("H-g1-ver", G, "glm53-1 ClickHouse version()", "26.8.36.3", S02 % 37 + "（T53 之后未核实）",
        lambda: chv(ch1, lambda c: c.ver), lambda e, a: a.startswith(e))
    add("H-g1-table", G, "glm53-1 表 vllm_pd.%s" % V34_TABLE, "存在", "包二 v3.4-extra",
        lambda: chv(ch1, lambda c: "存在" if "vllm_pd." + V34_TABLE in c.tables else "不存在"))
    add("H-tg-ver", G, "test-glm53 ClickHouse version()", "26.8.36.3", S02 % 37 + "（T62 之后未核实）",
        lambda: chv(ch2, lambda c: c.ver), lambda e, a: a.startswith(e))
    for t in T62_TABLES:
        add("H-tg-" + t, G, "test-glm53 表 vllm_pd." + t, "存在", S02 % 90 + "；T62 schema.sql",
            lambda t=t: chv(ch2, lambda c: "存在" if "vllm_pd." + t in c.tables else "不存在"))
    add("H-tg-old", G, "test-glm53 旧表 vllm_pd.%s（第 7 步前保留）" % V34_TABLE, "存在", S02 % 90,
        lambda: chv(ch2, lambda c: "存在" if "vllm_pd." + V34_TABLE in c.tables else "不存在"))
    add("H-tg-rows", G, "test-glm53 vllm_pd.request_doc TTL", "30 天", SITE,
        lambda: chv(ch2, lambda c: "%s 行，TTL %s" % (c.tables["vllm_pd.request_doc"][1], c.tables["vllm_pd.request_doc"][3] or "无")
                    if "vllm_pd.request_doc" in c.tables else "没有这张表"), ttl30)
    for ch in (ch1, ch2):
        if ch and not ch.err:
            for t, (eng, rows, b, ttl) in sorted(ch.tables.items()):
                T.note("%s %s | %s | %d 行 %s | TTL %s" % (ch.name, t, eng, rows, human(b), ttl or "无"))
        elif ch:
            T.note("%s：%s" % (ch.name, ch.err))
    check_details(T, dk, ps, ch2, home, d1, d2, e1, ch_env, gf_env)
    if not dk.err:
        for n in sorted(dk.cs):
            T.note("容器 %s | %s | %s | 端口 %s | 网络 %s | 挂载 %s" % (n, dk.image(n), dk.state(n), dk.host_ports(n),
                                                             dk.networks(n), "；".join("%s→%s" % m for m in dk.mounts(n))))
    return "监控机"


# ============================================================================ details printed as they are

def read_text(p, cap=4 << 20):
    try:
        if os.path.isfile(p) and os.path.getsize(p) <= cap:
            with open(p, errors="replace") as f:
                return f.read()
    except OSError:
        pass
    return ""


def rule_files(dk, yml_host, container):
    """[(rule file as written, host path or None)] from rule_files: of prometheus.yml"""
    txt, out, on = read_text(yml_host), [], False
    for line in txt.splitlines():
        if re.match(r"^rule_files\s*:", line):
            on = True
            continue
        if on:
            m = re.match(r"^\s+-\s*['\"]?([^'\"#]+?)['\"]?\s*$", line)
            if m:
                out.append(m.group(1))
            elif line.strip() and not line.startswith((" ", "\t", "#")):
                break
    res = []
    mounts = dk.mounts(container) if not dk.err and dk.get(container) else []
    for f in out:
        host = None
        for src, dst in mounts:
            if src.startswith("/") and (f == dst or f.startswith(dst.rstrip("/") + "/")):
                host = src + f[len(dst.rstrip("/")):] if f != dst else src
        if host is None and os.path.isabs(f) and os.path.exists(f):
            host = f
        res.append((f, host))
    return res


def check_details(T, dk, ps, ch2, home, d1, d2, e1, ch_env, gf_env):
    add = T.add
    G = "明细"
    # alert-only Prometheus
    ap = "vllm-pd-prometheus-test-glm53"
    yml = ((ch_env or {}).get("PROMETHEUS_YML") or ROOT + "/vllm-pd-prometheus-test-glm53/prometheus.yml")

    def rules():
        if not os.path.isfile(yml):
            raise Unreadable("没有 " + yml)
        rf = rule_files(dk, yml, ap)
        if not rf:
            return "prometheus.yml 没有 rule_files"
        parts, where = [], []
        for f, host in rf:
            hits = sorted(glob.glob(host)) if host and any(ch in host for ch in "*?[") else ([host] if host else [])
            names = []
            for h in hits:
                names += re.findall(r"^\s*-?\s*(?:alert|record)\s*:\s*['\"]?([A-Za-z0-9_:]+)", read_text(h), re.M)
                if "PdIngestDocsStale" in read_text(h):
                    where.append(h)
            parts.append("%s → %s：%s" % (f, host or "宿主机上找不到", "、".join(names) or "无规则"))
        return "；".join(parts) + "｜PdIngestDocsStale：" + ("在 " + "、".join(where) if where else "不在已加载的规则文件里")
    add("X-alert-rules", G, "只告警 Prometheus 加载的规则文件与规则名", None, "直接打印", rules)
    # ingest
    ing = [(p, c) for p, c in ps if "pd_ingest.py" in c and "bash -c" not in c]
    add("X-ingest-run", G, "入库进程 pd_ingest.py", None, "直接打印",
        "在运行（%d 个，pid %s）" % (len(ing), ",".join(str(p) for p, _c in ing[:5])) if ing else "没有在运行")
    port = (ch_env or {}).get("INGEST_STATUS_PORT") or "18191"

    def status():
        try:
            with urllib.request.urlopen("http://127.0.0.1:%s/" % port, timeout=5) as r:
                body = r.read(4096).decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            raise Unreadable("GET 127.0.0.1:%s：%s" % (port, e))
        try:
            j = json.loads(body)
            if isinstance(j, dict):
                body = json.dumps({k: ("***" if SECRET.search(k) else v) for k, v in j.items()}, ensure_ascii=False)
        except ValueError:
            pass
        return re.sub(r"\s+", " ", body)[:300]
    add("X-ingest-status", G, "入库状态端口 %s 的返回（GET，前 300 字）" % port, None, "直接打印", status)

    def maxts():
        if ch2 is None:
            raise Unreadable(dk.err or "docker 不可用")
        ch2.need()
        pw = (ch_env or {}).get("CH_ADMIN_PASSWORD", "")
        cols = ch_query(ch2.name, pw, "SELECT name FROM system.columns WHERE database='vllm_pd' AND table='request_doc' "
                                      "AND type LIKE '%DateTime%' ORDER BY position")
        if not cols:
            raise Unreadable("request_doc 没有时间列")
        names = [c[0] for c in cols]
        col = "ts" if "ts" in names else names[0]
        return "max(%s) = %s" % (col, ch_query(ch2.name, pw, "SELECT max(`%s`) FROM vllm_pd.request_doc" % col)[0][0])
    add("X-ingest-maxts", G, "vllm_pd.request_doc 最新时间", None, "直接打印", maxts)
    # reference sources
    refs = reference_sources(dk, ps, home)
    for p in (ROOT + "/vllm-pd-grafana-panel2/glm53-1.leftover-20261127-172638", ROOT + "/vllm-timing"):
        add("X-dir-" + os.path.basename(p), G, p, None, "直接打印",
            lambda p=p: "%s｜%s" % (tree_facts(p), referenced(p, refs)) if os.path.lexists(p) else "不存在")
    # cleanup candidates
    G = "清理候选"
    pats = ("vllm-pd-panel2-update-*", "vllm-pd-update-v*", "vllm032-pd-grafana-panel2", "vllm-pd-grafana-panel2",
            "vllm-pd-grafana-panel1", "panel1-export-*", "dashboards.bak-*", "vllm-pd-patch-glm53", "t53-*.tar*",
            "vllm-pd-update-backup*", "vllm-pd-arrange-backup", "panel2-1114", "check-1112", "vllm-pd-test-glm53",
            "vllm-pd-grafana-panel2*.tar.gz")
    seen = set()
    for base in (OLD, home):
        for pat in pats:
            for p in sorted(glob.glob(os.path.join(base, pat))):
                if p in seen:
                    continue
                seen.add(p)
                add("K-" + p, G, p, None, "清理候选",
                    lambda p=p: "%s｜%s" % (size_of(p), referenced(p, refs)))
    if not seen:
        add("K-none", G, "%s 和 %s 下的旧包和旧备份" % (OLD, home), None, "清理候选", "没有")


def size_of(p):
    if os.path.islink(p):
        return "链接 → %s" % os.readlink(p)
    if os.path.isfile(p):
        return human(os.path.getsize(p))
    n = size = 0
    for dp, _dns, fns in os.walk(p):
        for f in fns:
            n += 1
            try:
                size += os.lstat(os.path.join(dp, f)).st_size
            except OSError:
                pass
            if n >= 20000:
                return "至少 %s" % human(size)
    return human(size)


def reference_sources(dk, ps, home):
    """[(who, text)]: registry, env/json files of both panels, crontab, systemd units, running commands, mounts."""
    out = []
    for p in [os.path.join(home, ".vllm-pd-clusters.json")] + sorted(
            glob.glob(ROOT + "/vllm-pd-grafana-panel2/*/*.env") + glob.glob(ROOT + "/vllm-pd-grafana-panel2/*/ingest.json")
            + glob.glob(ROOT + "/vllm-pd-grafana-panel1/*/*.env") + glob.glob(home + "/vllm-pd-boards/vars.sh")):
        if ".leftover-" in p or p.endswith((".before-t62",)):
            continue
        out.append(("文件 " + p, read_text(p)))
    rc, o, _e = run(["crontab", "-l"], 10)
    if rc == 0:
        out.append(("crontab", o))
    for p in glob.glob("/var/spool/cron/*") + glob.glob("/var/spool/cron/crontabs/*") + glob.glob("/etc/cron.d/*") + [
            "/etc/crontab"]:
        out.append(("cron " + p, read_text(p)))
    for p in glob.glob("/etc/systemd/system/*.service") + glob.glob("/etc/systemd/system/*.timer") + glob.glob(
            "/etc/systemd/system/*/*.service") + glob.glob("/usr/lib/systemd/system/vllm*"):
        out.append(("systemd " + os.path.basename(p), read_text(p)))
    for pid, c in ps:
        out.append(("进程 %d（%s）" % (pid, " ".join(c.split()[:3])[:60]), c))
        try:
            out.append(("进程 %d 工作目录" % pid, os.readlink("/proc/%d/cwd" % pid) + "/"))
        except OSError:
            pass
    if not dk.err:
        for n in dk.cs:
            out.append(("容器 %s 挂载" % n, "\n".join(src + "/" for src, _d in dk.mounts(n) if src.startswith("/"))))
    return out


def referenced(p, refs):
    keys = {p.rstrip("/")}
    try:
        keys.add(os.path.realpath(p))
    except OSError:
        pass
    who = []
    for name, txt in refs:
        if txt and any(re.search(re.escape(k) + r"(?=$|[/\s\"':;,)=])", txt) for k in keys):
            who.append(name)
    if not who:
        return "无引用"
    return "有引用（%s%s）" % ("、".join(who[:3]), " 等 %d 处" % len(who) if len(who) > 3 else "")


# ============================================================================ P, D and proxy machines

def ns_of(pid):
    try:
        return os.readlink("/proc/%d/ns/pid" % pid)
    except OSError:
        return None


def container_procs(pid0):
    ns = ns_of(pid0)
    return [(p, c) for p, c in proc_list() if ns and ns_of(p) == ns]


def proc_env(pid):
    try:
        with open("/proc/%d/environ" % pid, "rb") as f:
            return dict(x.split("=", 1) for x in f.read().decode("utf-8", "replace").split("\0") if "=" in x)
    except OSError:
        return None


def package_dirs(procs):
    """In-container paths: site of vllm, parent of vllm_ascend, libmooncake_store.so (from /proc maps, then globs)."""
    found = {}
    root = "/proc/%d/root" % procs[0][0]
    for pid, _c in procs:
        try:
            with open("/proc/%d/maps" % pid) as f:
                for line in f:
                    p = line.split(None, 5)[-1].strip() if len(line.split(None, 5)) == 6 else ""
                    for pkg in ("vllm", "vllm_ascend"):
                        k = "/%s/" % pkg
                        if pkg in found or k not in p:
                            continue
                        if pkg == "vllm_ascend" or "-packages/vllm/" in p:
                            found[pkg] = p[:p.index(k)]
                    if p.endswith("libmooncake_store.so") and "mooncake" not in found:
                        found["mooncake"] = p
        except OSError:
            continue
    pats = ["usr/local/lib/python3*/site-packages", "usr/local/python3*/lib/python3*/site-packages",
            "usr/lib/python3*/site-packages", "usr/lib/python3/dist-packages", "opt/*/lib/python3*/site-packages",
            "opt/*/*/lib/python3*/site-packages", "root/.local/lib/python3*/site-packages"]
    for pat in pats:
        for d in glob.glob(os.path.join(root, pat)):
            rel = "/" + os.path.relpath(d, root)
            if "vllm" not in found and os.path.isdir(os.path.join(d, "vllm")):
                found["vllm"] = rel
            if "vllm_ascend" not in found and os.path.isdir(os.path.join(d, "vllm_ascend")):
                found["vllm_ascend"] = rel
            if "mooncake" not in found and os.path.isfile(os.path.join(d, "mooncake", "libmooncake_store.so")):
                found["mooncake"] = rel + "/mooncake/libmooncake_store.so"
    return root, found


def version_of(root, site, pkg):
    if not site:
        raise Unreadable("找不到 %s 包" % pkg)
    for d in glob.glob(os.path.join(root + site, pkg.replace("_", "[-_]") + "-*.dist-info")):
        return os.path.basename(d)[len(pkg) + 1:-len(".dist-info")]
    for d in glob.glob(os.path.join(root + site, "__editable__*%s*" % pkg)) + glob.glob(
            os.path.join(root + site, pkg + ".egg-link")):
        return "可编辑安装（%s）" % os.path.basename(d)
    vf = os.path.join(root + site, pkg, "_version.py")
    if os.path.isfile(vf):
        m = re.search(r"""version\s*[:=][^'"]*['"]([^'"]+)""", open(vf, errors="replace").read())
        if m:
            return m.group(1)
    raise Unreadable("%s 的版本信息找不到" % pkg)


def vllm_containers(dk):
    """running containers with a process whose command has 'vllm serve' or 'api_server'"""
    res = []
    for n in sorted(dk.cs):
        if dk.state(n) != "运行中":
            continue
        try:
            procs = container_procs(dk.pid(n))
        except Unreadable:
            continue
        if any(re.search(r"vllm\s+serve|api_server", c) for _p, c in procs):
            res.append(n)
    return res


def proc_kind(c):
    w = c.split()
    if re.search(r"vllm\s+serve", c):
        return "vllm serve"
    if "api_server" in c:
        return "api_server"
    if w and w[0].startswith("VLLM::"):
        return w[0]
    return " ".join(x.rsplit("/", 1)[-1] for x in w[:3])[:50]


def check_node(T, role, cluster, container=None):
    add = T.add
    cfg = NODE[cluster]
    dk = Docker()
    if dk.err:
        T.note(dk.err)
    pd = "/data/pd-diag"
    G = "节点目录"
    if role in ("p", "d"):
        add("N-dir-conf", G, pd + "/conf", "存在", S02 % (96 if cluster == "test-glm53" else 75), lambda: exists_label(pd + "/conf"))
        if cluster == "glm53-1":
            add("N-dir-logs", G, pd + "/logs", "存在", S02 % 75, lambda: exists_label(pd + "/logs"))
        else:
            add("N-dir-kit", G, pd + "/t62-kit", "存在", S02 % 96, lambda: exists_label(pd + "/t62-kit"))
            add("N-dir-bconf", G, pd + "/t62-backup/conf", "存在", S02 % 96, lambda: exists_label(pd + "/t62-backup/conf"))
        add("N-dir-list", G, pd + " 下的条目", None, S02 % 96, lambda: "、".join(listdir(pd)))
        for n in ("t62-kit", "t62-backup", "fetcher"):
            add("N-size-" + n, G, "%s/%s 大小" % (pd, n), None, "用户 11-28 询问", lambda n=n: tree_facts(pd + "/" + n))
    else:
        if cluster == "test-glm53":
            add("N-px-sock", G, pd + "/proxy-sock", "存在，空目录", S02 % 98,
                lambda: ("存在，空目录" if not listdir(pd + "/proxy-sock") else "存在，%d 项" % len(listdir(pd + "/proxy-sock")))
                if os.path.isdir(pd + "/proxy-sock") else exists_label(pd + "/proxy-sock"))
            add("N-px-fetcher", G, pd + "/fetcher/pd_fetcher.py", "存在（文件）", "T62 包 fetcher/install.py",
                lambda: exists_label(pd + "/fetcher/pd_fetcher.py"))

            def lport():
                with open(pd + "/fetcher/fetcher.json") as f:
                    return str(json.load(f).get("listen_port"))
            add("N-px-port", G, "fetcher.json listen_port", "9300", S02 % 86,
                lambda: lport() if os.path.isfile(pd + "/fetcher/fetcher.json") else Unreadable("没有 fetcher.json"))
        else:
            add("N-px-logs", G, pd + "/logs/proxy", "存在", S02 % 79, lambda: exists_label(pd + "/logs/proxy"))
        add("N-dir-list", G, pd + " 下的条目", None, S02 % 98, lambda: "、".join(listdir(pd)))
        for n in ("t62-kit", "t62-backup", "fetcher"):
            add("N-size-" + n, G, "%s/%s 大小" % (pd, n), None, "用户 11-28 询问", lambda n=n: tree_facts(pd + "/" + n))
        G = "采集进程"
        fp = [(p, c) for p, c in proc_list() if "pd_fetcher.py" in c]
        if cluster == "test-glm53":
            add("N-px-run", G, "采集进程 pd_fetcher.py", "在运行", S02 % 86,
                lambda: "在运行（pid %s）" % ",".join(str(p) for p, _c in fp) if fp else "没有在运行",
                lambda e, a: a.startswith("在运行"))

            def listening():
                if not fp:
                    raise Unreadable("没有采集进程")
                for fn in ("tcp", "tcp6"):
                    try:
                        with open("/proc/%d/net/%s" % (fp[0][0], fn)) as f:
                            for l in f.readlines()[1:]:
                                x = l.split()
                                if x[3] == "0A" and int(x[1].rsplit(":", 1)[1], 16) == 9300:
                                    return "在监听"
                    except OSError:
                        pass
                return "没有监听"
            add("N-px-listen", G, "采集进程监听 9300", "在监听", S02 % 86, listening)
        add("N-px-containers", G, "本机运行中的容器", None, S02 % 98, lambda: "、".join(
            "%s(%s)" % (n, dk.image(n)) for n in sorted(dk.cs) if dk.state(n) == "运行中") or "无")
        return "proxy 机器（%s）" % cluster

    # ---------------- vLLM container
    G = "vLLM 容器"
    if container:
        name = container
        add("N-ct-name", G, "vLLM 容器 %s（--container 指定）" % name, "运行中", "命令行", lambda: dk.state(name))
    else:
        cands = [] if dk.err else vllm_containers(dk)
        name = cands[0] if cands else None
        add("N-ct-name", G, "运行中的 vLLM 容器（进程含 vllm serve 或 api_server）", "1 个", "自动查找",
            lambda: Unreadable(dk.err) if dk.err else ("%d 个：%s" % (len(cands), "、".join(cands)) if cands else "0 个"),
            lambda e, a: a.startswith("1 个"))
        if cfg.get("container"):
            add("N-ct-expected", G, "vLLM 容器名", cfg["container"], cfg["container_src"], lambda: name or "没有")
    try:
        if not name:
            raise Unreadable(dk.err or "没有运行中的 vLLM 容器")
        procs = container_procs(dk.pid(name))
        if not procs:
            raise Unreadable("读不了容器 %s 的进程" % name)
        root, dirs = package_dirs(procs)
        err = None
    except Unreadable as e:
        procs, root, dirs, err = [], "", {}, e
    add("N-ct-image", G, "vLLM 容器镜像", cfg.get("image"), cfg.get("image_src", S02 % 92),
        lambda: dk.image(name) if name else Unreadable("没有容器"))

    def level():
        if err:
            raise err
        vals = set()
        for p, c in procs:
            if "vllm" in c:
                e = proc_env(p)
                if e is not None:
                    vals.add(e.get("PD_DIAG_LEVEL", "未设置"))
        if not vals:
            raise Unreadable("读不了 vLLM 进程的环境变量")
        return "、".join(sorted(vals))
    add("N-level", G, "vLLM 进程的 PD_DIAG_LEVEL", cfg["level"], cfg["level_src"], level)
    if not err:
        groups = {}
        for p, c in procs:
            if "vllm" not in c.lower() and "VLLM" not in c:
                continue
            e = proc_env(p)
            key = (proc_kind(c), "读不了" if e is None else e.get("PD_DIAG_LEVEL", "未设置"))
            groups.setdefault(key, []).append(p)
        for (kind, val), pids in sorted(groups.items()):
            add("N-level-%s-%s" % (kind, val), "明细", "PD_DIAG_LEVEL｜%s" % kind, None, "直接打印",
                "%s｜%d 个进程（pid %s%s）" % (val, len(pids), ",".join(map(str, pids[:4])), "…" if len(pids) > 4 else ""))
    add("N-ver-vllm", G, "vLLM 版本", "0.30.0", S02 % 93, lambda: err or version_of(root, dirs.get("vllm"), "vllm"),
        lambda e, a: a.startswith(e))
    add("N-ver-ascend", G, "vllm-ascend 版本（含提交 d4ad497）", "d4ad497", S02 % 93,
        lambda: err or version_of(root, dirs.get("vllm_ascend"), "vllm_ascend"), contains)

    def mc():
        if err:
            raise err
        if "mooncake" not in dirs:
            raise Unreadable("找不到 libmooncake_store.so")
        return sha16(root + dirs["mooncake"])
    add("N-mooncake", G, "libmooncake_store.so sha256 前 16 位", cfg["mooncake"], cfg["mooncake_src"], mc)
    if cluster == "test-glm53":
        add("N-t53tar", G, "容器内 /tmp/t53/backup/vllm-ascend.tar", "存在（文件）", S02 % 97,
            lambda: err or exists_label(root + "/tmp/t53/backup/vllm-ascend.tar"))
        add("N-t53tar-size", G, "容器内 /tmp/t53/backup/vllm-ascend.tar 大小", cfg.get("t53tar"), SITE,
            lambda: err or tree_facts(root + "/tmp/t53/backup/vllm-ascend.tar"))

        def orig():
            hits = glob.glob(pd + "/t62-backup/*/conn_orig/**/mooncake_connector.py", recursive=True)
            if not hits:
                raise Unreadable("t62-backup/*/conn_orig 下没有 mooncake_connector.py")
            return "、".join(sorted({sha16(h) for h in hits}))
        add("N-conn-orig", G, "t62-backup 中现场原 mooncake_connector.py", SITE_ORIGINAL, S02 % 93, orig)
        add("N-bk-mc", G, pd + "/t62-backup/<容器名>/mooncake", "存在", S02 % 96,
            lambda: exists_label("%s/t62-backup/%s/mooncake" % (pd, name)))

    # ---------------- patch files
    G = "补丁文件"
    if cfg["state"] == "T62":
        want = dict(T62_FILES)
        want[CONNECTOR] = SITE_CONNECTOR
        srcs = {f: "980e5a2c SHA256SUMS.after" for f in want}
        srcs[CONNECTOR] = S02 % 94 + "（现场原文件重放）"
        absent = []
    else:
        want = dict(T73_FILES)
        want[CONNECTOR] = None
        srcs = {f: "T53 交付的 t73-glm53-patch-kit（980e5a2c states/SHA256SUMS.t73）" for f in want}
        srcs[CONNECTOR] = "T53 在现场原文件（%s）上重放，结果值未知" % cfg["connector"]
        absent = list(T62_NEW_FILES)

    def file_hash(rel):
        if err:
            raise err
        pkg = rel.split("/", 1)[0]
        base = dirs.get(pkg)
        if not base:
            raise Unreadable("找不到 %s 包" % pkg)
        p = root + base + "/" + rel
        return sha16(p) if os.path.isfile(p) else "不存在"
    for rel in sorted(want):
        add("P-" + rel, G, rel, want[rel], srcs[rel], lambda rel=rel: file_hash(rel))
    for rel in absent:
        add("P-new-" + rel, G, rel, "不存在", "980e5a2c states/NEW_FILES.txt", lambda rel=rel: file_hash(rel),
            lambda e, a: a == "不存在")
    T.note("容器 %s：进程 %d 个；包位置 %s" % (name, len(procs), json.dumps(dirs, ensure_ascii=False)))
    return "%s 机器（%s，容器 %s）" % (role.upper(), cluster, name or "未找到")


# ============================================================================ --all: the nodes over ssh

SSH_BASE = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "LogLevel=ERROR"]   # as the T62 / T53 tools (t62_site.py, uninstall_old.py)


def host_of(a):
    a = (a or "").strip()
    a = re.sub(r"^\w+://", "", a).split("/")[0]
    a = a.split("@")[-1]
    if a.startswith("["):
        return a[1:a.index("]")]
    return a.rsplit(":", 1)[0] if a.count(":") == 1 else a


def node_targets(home):
    """[(cluster, role, host)] and notes on what could not be read"""
    out, notes = [], []
    try:
        reg = json.load(open(os.path.join(home, ".vllm-pd-clusters.json"))).get("clusters") or {}
    except (OSError, ValueError):
        reg = {}
    for c in ("test-glm53", "glm53-1"):
        d = ((reg.get(c) or {}).get("panel2") or {}).get("dir") or panel2_dir(c)
        envf = d + ("/deploy.env" if c == "glm53-1" else "/deploy.env.before-t62")
        ev = env_values(envf) or {}
        mine = []
        invp = ev.get("INVENTORY") if ev.get("INVENTORY", "").startswith("/") else d + "/inventory.json"
        try:
            inv = json.load(open(invp))
        except (OSError, ValueError) as e:
            notes.append("%s：清单 %s 读不了（%s）" % (c, invp, getattr(e, "strerror", None) or e))
            inv = {}
        for side, role in (("prefill", "p"), ("decode", "d")):
            hs = []
            for a in (inv.get(side) or {}).get("api_servers") or []:
                h = host_of(a)
                if h and h not in hs:
                    hs.append(h)
            mine += [(role, h) for h in hs]
        px = None
        if c == "test-glm53":
            try:
                px = host_of(json.load(open(d + "/ingest.json")).get("fetcher_url"))
            except (OSError, ValueError):
                notes.append("%s：ingest.json 读不了，proxy 机器不明" % c)
        else:
            px = host_of(ev.get("PROXY_MACHINE")) if ev.get("PROXY_MACHINE") else None
            if not px:
                notes.append("%s：deploy.env 没有 PROXY_MACHINE，proxy 机器不明" % c)
        if px:
            mine.append(("proxy", px))
        opts = shlex.split(ev.get("SSH_OPTS") or "")
        out += [(c, r, h, "root", opts) for r, h in mine]
    return out, notes


def run_node(script, cluster, role, host, user, opts):
    cmd = SSH_BASE + opts + ["%s@%s" % (user, host),
                             "python3 -I -S -B - --node %s --cluster %s --json" % (role, cluster)]
    try:
        p = subprocess.run(cmd, input=script, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=600)
    except subprocess.TimeoutExpired:
        return None, "ssh 超过 600 秒没有返回"
    except OSError as e:
        return None, "ssh 启动不了：%s" % e
    for line in reversed(p.stdout.splitlines()):
        if line.startswith("{\"where\""):
            try:
                return json.loads(line), None
            except ValueError:
                break
    return None, re.sub(r"\b\d{1,3}(\.\d{1,3}){3}\b", "<IP>", "ssh 返回码 %d：%s" % (p.returncode, last(p.stderr, p.stdout)))


def check_all_nodes(home):
    import threading
    targets, notes = node_targets(home)
    me = os.path.abspath(globals().get("__file__") or "check_site_state.py")
    script = open(me).read()
    res = [None] * len(targets)
    sem = threading.Semaphore(8)

    def one(i, t):
        with sem:
            res[i] = run_node(script, t[0], t[1], t[2], t[3], t[4])
    th = [threading.Thread(target=one, args=(i, t)) for i, t in enumerate(targets)]
    for x in th:
        x.start()
    for x in th:
        x.join()
    segs, labels, n = [], [], {}
    for t, (j, why) in zip(targets, res):
        k = (t[0], t[1])
        n[k] = n.get(k, 0) + 1
        label = "%s %s%d" % (t[0], t[1].upper() if t[1] != "proxy" else "proxy", n[k])
        labels.append("%s = %s" % (label, t[2]))
        if j is None:
            segs.append({"label": label, "fail": "%s；在该机器上手工运行：python3 -I -S -B /data/pd-diag/check/check_site_state.py "
                                           "--node %s --cluster %s" % (why, t[1], t[0])})
        else:
            j["label"] = label
            segs.append(j)
    return segs, notes, labels


# ============================================================================ output
# ============================================================================ output

GROUP_ORDER = ["容器", "目录", "Grafana", "ClickHouse", "面板二配置", "入库进程", "注册表", "明细", "清理候选",
               "节点目录", "采集进程", "vLLM 容器", "补丁文件"]


def counts(items):
    c = {"一致": 0, "不一致": 0, "未知": 0, "无法检查": 0}
    for it in items:
        c[it[6]] += 1
    return c


def seg_lines(title, items, budget):
    c = counts(items)
    lines = ["== %s：一致 %d / 不一致 %d / 未知 %d / 无法检查 %d" % (title, c["一致"], c["不一致"], c["未知"], c["无法检查"])]
    shown = [it for it in items if it[6] != "一致"]
    groups = [g for g in GROUP_ORDER if any(it[1] == g for it in shown)] + sorted(
        {it[1] for it in shown} - set(GROUP_ORDER))
    per = {g: [it for it in shown if it[1] == g] for g in groups}
    room = budget - 1 - len(groups)
    cap = max((len(v) for v in per.values()), default=0)
    while cap > 1 and sum(min(len(v), cap) for v in per.values()) + sum(1 for v in per.values() if len(v) > cap) > room:
        cap -= 1
    for g in groups:
        lines.append("-- %s" % g)
        for it in per[g][:cap]:
            lines.append("%s｜%s｜%s｜%s：%s" % (it[0], it[2], it[3], it[6], it[5]))
        if len(per[g]) > cap:
            lines.append("    另有 %d 项见报告文件" % (len(per[g]) - cap))
    return lines


def result_key(items):
    return tuple(sorted((it[0], it[6], re.sub(r"pid [0-9,…]+", "pid", it[5])) for it in items if it[6] != "一致"))


def emit(segs, notes, labels, report_dir, budget):
    """segs: [{"label", "items", "info"} or {"label", "fail"}]. Nodes with the same result share one segment."""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    merged = []
    for sg in segs:
        if "fail" in sg:
            merged.append(dict(sg, labels=[sg["label"]]))
            continue
        k = (re.sub(r"，容器 [^）]*", "", sg["where"]), result_key(sg["items"]))
        for m in merged:
            if m.get("key") == k:
                m["labels"].append(sg["label"])
                break
        else:
            merged.append(dict(sg, key=k, labels=[sg["label"]]))
    fails = [m for m in merged if "fail" in m]
    oks = [m for m in merged if "fail" not in m]
    room = budget - 2 - len(fails) - len(notes)
    weights = [max(1, sum(1 for it in m["items"] if it[6] != "一致")) for m in oks]
    out = []
    for m, w in zip(oks, weights):
        b = max(6, int(room * w / max(1, sum(weights))))
        title = m["where"] if len(m["labels"]) == 1 else "%s 等 %d 台结果相同（%s）" % (
            m["where"], len(m["labels"]), "、".join(m["labels"]))
        if m["labels"][0] != "监控机":
            title = "%s｜%s" % (m["labels"][0], title) if len(m["labels"]) == 1 else title
        out += seg_lines(title, m["items"], b)
    for m in fails:
        out.append("== %s：无法检查：%s" % (m["label"], m["fail"]))
    for n in notes:
        out.append("说明：" + n)
    rpath = os.path.join(report_dir, "report-%s.txt" % stamp) if report_dir else None
    if rpath:
        try:
            with open(rpath, "w") as f:
                f.write("== 只读检测 %s\n" % stamp)
                for l in labels:
                    f.write("机器 %s\n" % l)
                for sg in segs:
                    if "fail" in sg:
                        f.write("== %s：无法检查：%s\n" % (sg["label"], sg["fail"]))
                        continue
                    c = counts(sg["items"])
                    f.write("== %s｜%s：一致 %d / 不一致 %d / 未知 %d / 无法检查 %d\n" % (
                        sg["label"], sg["where"], c["一致"], c["不一致"], c["未知"], c["无法检查"]))
                    f.write("编号｜分组｜对象｜预期｜出处｜实际｜结论\n")
                    for it in sg["items"]:
                        f.write("｜".join(str(x) for x in it) + "\n")
                    f.write("-- 明细\n")
                    for n in sg["info"]:
                        f.write(n + "\n")
                for n in notes:
                    f.write("说明：" + n + "\n")
            out.append("RESULT: 完成；全部检查项与明细见 %s" % rpath)
        except OSError as e:
            out.append("RESULT: 完成；报告文件写不了（%s：%s），只有上面的输出" % (rpath, e.strerror))
    else:
        out.append("RESULT: 完成（节点上不写报告文件）")
    if len(out) > budget:
        out = out[:budget - 2] + ["…另有 %d 行见报告文件" % (len(out) - budget + 1), out[-1]]
    for l in out:
        print(l)


def main(argv):
    ap = argparse.ArgumentParser(prog="check_site_state.py", description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--node", choices=("p", "d", "proxy"))
    ap.add_argument("--cluster", choices=sorted(NODE), default="test-glm53")
    ap.add_argument("--container", help="vLLM 容器名（默认自动找进程含 vllm serve 或 api_server 的运行中容器）")
    ap.add_argument("--all", action="store_true", help="监控机：再经 ssh 检查 test-glm53、glm53-1 的 P、D、proxy 机器")
    ap.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--report-dir")
    ap.add_argument("--personal-dir", help="监控机上放部署包和旧备份的个人目录，例如 /data/<个人目录>；不给时这些项报「不存在」")
    ap.add_argument("--assume", help="JSON 文件 {编号: 预期值}，替换上面的假设（实验室或假设更新时用）")
    ap.add_argument("--lines", type=int, default=150)
    a = ap.parse_args(argv)
    ov = {}
    if a.assume:
        try:
            with open(a.assume) as f:
                ov = json.load(f)
        except (OSError, ValueError) as e:
            print("FAIL --assume %s 读不了：%s" % (a.assume, e))
            return 1
    global OLD
    if a.personal_dir:
        OLD = a.personal_dir.rstrip("/")
    T = Table(ov)
    try:
        where = check_node(T, a.node, a.cluster, a.container) if a.node else check_monitor(T)
    except Exception as e:  # noqa: BLE001 - never a traceback
        where = "检测中断"
        T.note("检测中断：%s: %s" % (type(e).__name__, e))
        T.items.append(("中断", "检测中断", "检测脚本", "-", "-", "%s: %s" % (type(e).__name__, e), "无法检查"))
    if a.json:
        print(json.dumps({"where": where, "items": T.items, "info": T.info}, ensure_ascii=False))
        return 0
    segs = [{"label": "监控机" if not a.node else "本机", "where": where, "items": T.items, "info": T.info}]
    notes = [n for n in T.info if n.startswith(("docker 不可用", "查不了 Grafana", "检测中断"))]
    labels = []
    if a.all and not a.node:
        try:
            ns, nn, labels = check_all_nodes(os.environ.get("HOME") or "/root")
            segs += ns
            notes += nn
        except Exception as e:  # noqa: BLE001
            notes.append("--all 中断：%s: %s" % (type(e).__name__, e))
    rd = None
    if not a.node:
        rd = a.report_dir or os.path.dirname(os.path.abspath(globals().get("__file__") or "."))
    emit(segs, notes, labels, rd, a.lines)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
