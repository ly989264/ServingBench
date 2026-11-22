#!/usr/bin/env python3
"""T64 lab: reads the cluster's panel-two dashboard from Grafana (GET only) and prints, for the two distribution tables,
whether the 「有值 / 请求」 column is hidden and the widths of the shown columns. Runs in the cluster folder on the monitor.
Usage: python3 -I board_check.py <deploy.env>"""
import base64
import json
import sys
import urllib.request


def env(path):
    out = {}
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k] = v.strip().strip("'\"")
    return out


e = env(sys.argv[1])
uid = e.get("GRAFANA_BOARD_UID") or "vllm-pd-request-" + e.get("CLUSTER", "")
req = urllib.request.Request(e["GRAFANA_URL"].rstrip("/") + "/api/dashboards/uid/" + uid)
req.add_header("Authorization", "Basic " + base64.b64encode(("%s:%s" % (e["GRAFANA_USER"], e["GRAFANA_PASSWORD"])).encode()).decode())
d = json.load(urllib.request.urlopen(req, timeout=20))
print("看板 %s，文件夹 %s" % (uid, d["meta"].get("folderTitle")))
for p in d["dashboard"]["panels"]:
    if p.get("title") not in ("P 侧分段时延分布", "D 侧分段时延分布"):
        continue
    props = {}
    for o in p.get("fieldConfig", {}).get("overrides", []):
        m = o.get("matcher", {})
        if m.get("id") == "byName":
            props.setdefault(m.get("options"), {}).update({x["id"]: x["value"] for x in o.get("properties", [])})
    hv = props.get("有值 / 请求", {})
    cols = ["%s %s" % (c, props.get(c, {}).get("custom.width")) for c in ("阶段", "p50", "p90", "p99", "均值", "均值占比")]
    print("%s：「有值 / 请求」%s；宽度 %s" % (p["title"], "隐藏" if hv.get("custom.hidden") else "显示 %s" % hv.get("custom.width"), "、".join(cols)))
