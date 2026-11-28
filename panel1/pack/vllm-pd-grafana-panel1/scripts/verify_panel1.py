#!/usr/bin/env python3
"""Check that 面板一·P and 面板一·D have data: scrape targets, then every panel query of each dashboard.

Usage (standard library only, Python >= 3.8):
    python3 scripts/verify_panel1.py --prometheus http://<host>:9090
        [--inventory prometheus/inventory.json] [--grafana http://<host>:3000] [--minutes 30] [--json out.json]

    --inventory   also compare the live targets with the ones the inventory expects
    --grafana     also run every query through Grafana's /api/ds/query (the same path the dashboard uses);
                  credentials from the environment: GRAFANA_TOKEN, or GRAFANA_USER + GRAFANA_PASSWORD
                  (nothing needed if anonymous access is on)
    --cluster C   a cluster installed with bin/panel1_cluster.sh: everything above comes from this folder's
                  deploy.env (scripts/names.py): its Prometheus, its inventory, its two dashboards (cluster/*.json),
                  its data source uid, the shared Grafana and its account. Step 0 then checks in Grafana that the
                  data source is healthy and that both dashboards load and pick this cluster's data source.

Steps
  1. Targets: up / down per job, last scrape error of each down target, and (with --inventory) missing or extra targets.
  2. Variables and instances: $model_name = first value of label_values(vllm:num_requests_running, model_name);
     $pd_instance = .* (the dashboard default "All"). Lists the instances found for each role; with --inventory, also
     fails when they differ from the instance names the inventory gives (P0, ..., D0, ...), and when an API server's
     engine label is not its position in the inventory's list (the list is then not in data_parallel_index order).
  3. Panels: for each dashboard, numbered in dashboard order, rows not counted (the numbering of PANELS.md). Every Prometheus query is
     run over the last --minutes. A query "has data" if it returns a non-empty, non-NaN series; a panel is "data"
     when all its queries do, "partial" when only some do (the others usually point at a metric that is not exported).
     With --inventory and vllm_ascend_patched=false, a query on a Mooncake client or Transfer Engine metric (mooncake_*) may be
     empty: such a panel is marked "needs patch" and does not fail the check. The list is printed at the end.
Exit status: 0 if all targets are up and every query of every Prometheus panel has data (apart from "needs patch"), 1 otherwise
(the MTP panel needs MTP, the AscendStore and SSD latency panels need AscendStoreConnector and SSD, and the Client panels
of 面板一·D need a store client on D, so they can legitimately be empty; PANELS.md lists the condition of each panel).
"""
import argparse
import base64
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def http_json(url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def prom(base, path, params):
    body = urllib.parse.urlencode(params).encode()
    return http_json(base.rstrip("/") + path, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})


def dur_s(s):
    units = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}
    for u in ("ms", "s", "m", "h", "d"):
        if s.endswith(u) and s[: -len(u)].replace(".", "", 1).isdigit():
            return float(s[: -len(u)]) * units[u]
    return float(s)


def panels_in_order(dash):
    out, group = [], ""
    for p in dash["panels"]:
        if p["type"] == "row":
            group = p["title"]
            for q in p.get("panels", []):
                out.append((group, q))
        else:
            out.append((group, p))
    return out


def check_targets(args):
    t = http_json(args.prometheus.rstrip("/") + "/api/v1/targets?state=active")["data"]["activeTargets"]
    by_job, down, live = {}, [], set()
    for x in t:
        job = x["labels"].get("job", "?")
        st = by_job.setdefault(job, [0, 0])
        st[0 if x["health"] == "up" else 1] += 1
        if x["health"] != "up":
            down.append((job, x["scrapeUrl"], x.get("lastError", "")))
        live.add((job, x["labels"].get("instance", "")))
    res = {"jobs": by_job, "down": down}
    if args.inventory:
        sys.path.insert(0, os.path.join(HERE, "..", "prometheus"))
        import gen_prometheus_yml as gen  # noqa: E402

        inv = json.load(open(args.inventory, encoding="utf-8"))
        want = {(job, tgt) for job, groups in gen.build(inv).items() for tgts, _ in groups for tgt in tgts}
        res["missing"] = sorted(want - live)
        res["extra"] = sorted(live - want)
    return res


def variables(args):
    def values(label, match):
        r = http_json(args.prometheus.rstrip("/") + f"/api/v1/label/{label}/values?" + urllib.parse.urlencode({"match[]": match}))
        return sorted(r["data"])

    models = values("model_name", "vllm:num_requests_running")
    model = models[0] if models else ""
    p = values("pd_instance", f'vllm:num_requests_running{{role="prefill", model_name="{model}"}}')
    d = values("pd_instance", f'vllm:num_requests_running{{role="decode", model_name="{model}"}}')
    return {"model_name": model, "prefill": p, "decode": d, "models": models}


def engine_order_problems(args, servers):
    """An API server with one instance must report that instance's DP index as engine. If all of them report one value,
    the engine label says nothing about the order: a note, not a failure."""
    one = {s["address"]: s for s in servers if len(s["instances"]) == 1}
    rows = prom(args.prometheus, "/api/v1/query", {"query": "count by (instance, engine) (vllm:num_requests_running)"})["data"]["result"]
    seen = {}
    for r in rows:
        seen.setdefault(r["metric"].get("instance"), set()).add(r["metric"].get("engine"))
    if len(one) > 1 and len({frozenset(v) for a, v in seen.items() if a in one}) == 1:
        print("   NOTE: every API server reports the same engine value, so the order of the inventory's list cannot be checked here")
        return []
    out = []
    for address, srv in one.items():
        dp, name = next(iter(srv["instances"].items()))
        if address in seen and seen[address] != {str(dp)}:
            out.append(f"{name} ({address}) reports engine {','.join(sorted(seen[address]))}, expected {dp}: fix the order of the inventory's list")
    return out


def substitute(expr, vs):
    return expr.replace("$model_name", vs["model_name"]).replace("$pd_instance", ".*")


def builtins(expr, step, scrape, rng):
    rate_iv = max(step + scrape, 4 * scrape)
    return (expr.replace("$__rate_interval", f"{int(rate_iv)}s").replace("$__interval", f"{int(step)}s")
            .replace("$__range", f"{int(rng)}s"))


def has_values(result):
    for s in result:
        vals = s.get("values") or ([s["value"]] if "value" in s else [])
        if any(not math.isnan(float(v[1])) for v in vals):
            return True
    return False


def grafana_headers():
    h = {"Content-Type": "application/json"}
    if os.environ.get("GRAFANA_TOKEN"):
        h["Authorization"] = "Bearer " + os.environ["GRAFANA_TOKEN"]
    elif os.environ.get("GRAFANA_USER"):
        raw = f'{os.environ["GRAFANA_USER"]}:{os.environ.get("GRAFANA_PASSWORD", "")}'.encode()
        h["Authorization"] = "Basic " + base64.b64encode(raw).decode()
    return h


def grafana_query(args, ds_uid, expr, instant, frm, to, step):
    q = {"refId": "A", "datasource": {"type": "prometheus", "uid": ds_uid}, "expr": expr, "intervalMs": int(step * 1000),
         "maxDataPoints": 1000, "instant": bool(instant), "range": not instant}
    body = json.dumps({"queries": [q], "from": str(int(frm * 1000)), "to": str(int(to * 1000))}).encode()
    r = http_json(args.grafana.rstrip("/") + "/api/ds/query", data=body, headers=grafana_headers())
    res = r["results"]["A"]
    if res.get("error"):
        raise RuntimeError(res["error"])
    for fr in res.get("frames", []):
        for col in fr.get("data", {}).get("values", [])[1:]:
            if any(v is not None and not (isinstance(v, float) and math.isnan(v)) for v in col):
                return True
    return False


def js_regex(text):
    """A Grafana /pattern/flags variable regex as a Python pattern."""
    m = re.match(r"^/(.*)/([a-z]*)$", text or "")
    if not m:
        return re.compile(re.escape(text or ""))
    return re.compile(m.group(1), re.I if "i" in m.group(2) else 0)


def cluster_setup(args):
    """Fill the arguments from the cluster's deploy.env; returns the derived values."""
    sys.path.insert(0, HERE)
    import names as names_mod  # noqa: E402

    env_file = os.path.join(HERE, "..", "deploy.env")
    if not os.path.isfile(env_file):
        raise SystemExit(f"--cluster: no {os.path.normpath(env_file)} (run this script from the cluster's own folder)")
    env = names_mod.env_from_file(env_file)
    n = names_mod.Names(env).output("panel1")
    if n["CLUSTER"] != args.cluster:
        raise SystemExit(f"--cluster {args.cluster}: this folder is cluster {n['CLUSTER']}")
    base = os.path.normpath(os.path.join(HERE, ".."))
    args.prometheus = args.prometheus or n["PROMETHEUS_URL"]
    inv = os.path.join(base, "prometheus", "inventory.json")
    if not args.inventory and os.path.isfile(inv):
        args.inventory = inv
    if not args.dashboard:
        args.dashboard = [os.path.join(base, "cluster", f"{n[k]}.json") for k in ("PANEL1_P_UID", "PANEL1_D_UID")]
    args.grafana = args.grafana or os.environ.get("GRAFANA_URL") or env.get("GRAFANA_URL")
    args.grafana_datasource_uid = n["PROMETHEUS_DS_UID"]
    for k in ("GRAFANA_USER", "GRAFANA_PASSWORD"):
        if not os.environ.get(k) and env.get(k) and not os.environ.get("GRAFANA_TOKEN"):
            os.environ[k] = env[k]
    if args.inventory:
        si = json.load(open(args.inventory, encoding="utf-8")).get("scrape_interval")
        if si and args.scrape_interval == "15s":
            args.scrape_interval = si
    return n


def check_cluster_grafana(args, n):
    """Step 0: the data source exists and is healthy; each dashboard loads, sits in the folder MODEL_GROUP, and its
    datasource variable resolves to this cluster's data source only."""
    problems = []

    def get(path):
        return http_json(args.grafana.rstrip("/") + path, headers=grafana_headers())

    uid, name = n["PROMETHEUS_DS_UID"], n["PROMETHEUS_DS_NAME"]
    print(f"== 0. Grafana: data source {name} ({uid}), dashboards {n['PANEL1_P_UID']} {n['PANEL1_D_UID']}")
    try:
        ds = get(f"/api/datasources/uid/{uid}")
        if ds.get("name") != name:
            problems.append(f"data source {uid} is named {ds.get('name')!r}, expected {name!r}")
        h = get(f"/api/datasources/uid/{uid}/health")
        print(f"   data source health: {h.get('status')} {h.get('message', '')[:80]}")
        if h.get("status") != "OK":
            problems.append(f"data source {uid} health: {h.get('status')}")
    except Exception as ex:  # noqa: BLE001
        problems.append(f"data source {uid}: {str(ex)[:120]}")
    try:
        all_prom = [d for d in get("/api/datasources") if d.get("type") == "prometheus"]
    except Exception as ex:  # noqa: BLE001
        all_prom = []
        problems.append(f"data source list: {str(ex)[:120]}")
    for key in ("PANEL1_P_UID", "PANEL1_D_UID"):
        b = n[key]
        try:
            res = get(f"/api/dashboards/uid/{b}")
        except Exception as ex:  # noqa: BLE001
            problems.append(f"dashboard {b} does not load: {str(ex)[:120]}")
            continue
        d, meta = res["dashboard"], res.get("meta", {})
        var = [v for v in d.get("templating", {}).get("list", []) if v.get("type") == "datasource"]
        if len(var) != 1:
            problems.append(f"dashboard {b}: {len(var)} datasource variables")
            continue
        rx = js_regex(var[0].get("regex"))
        picked = [x["uid"] for x in all_prom if rx.search(x.get("name", ""))]
        cur = (var[0].get("current") or {}).get("value")
        print(f"   {b}: folder {meta.get('folderTitle')!r}, datasource variable regex {var[0].get('regex')!r} -> {picked}, current {cur}")
        if picked != [uid]:
            problems.append(f"dashboard {b}: the datasource variable matches {picked}, expected only {uid}")
        if cur != uid:
            problems.append(f"dashboard {b}: datasource variable current value {cur}, expected {uid}")
        if meta.get("folderTitle") != n["GRAFANA_FOLDER"]:
            problems.append(f"dashboard {b} is in folder {meta.get('folderTitle')!r}, expected {n['GRAFANA_FOLDER']!r}")
    for p in problems:
        print(f"   CLUSTER PROBLEM: {p}")
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prometheus")
    ap.add_argument("--cluster")
    ap.add_argument("--dashboard", action="append", help="dashboard JSON (repeatable); default: both dashboards of this pack")
    ap.add_argument("--inventory")
    ap.add_argument("--grafana")
    ap.add_argument("--grafana-datasource-uid", default="prometheus")
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--scrape-interval", default="15s")
    ap.add_argument("--json")
    args = ap.parse_args()
    cluster_problems = []
    if args.cluster:
        n = cluster_setup(args)
        cluster_problems = check_cluster_grafana(args, n) if args.grafana else ["--cluster needs a Grafana address (GRAFANA_URL)"]
    if not args.prometheus:
        ap.error("--prometheus is required (or --cluster)")

    args.unpatched = bool(args.inventory) and json.load(open(args.inventory, encoding="utf-8")).get("vllm_ascend_patched") is False
    report = {"targets": check_targets(args), "cluster_problems": cluster_problems}
    tg = report["targets"]
    print("== 1. scrape targets (up / down per job)")
    for job, (up, dn) in sorted(tg["jobs"].items()):
        print(f"   {job:18s} up {up:4d}   down {dn:4d}")
    for job, url, err in tg["down"]:
        print(f"   DOWN {job}: {url}  {err}")
    for k in ("missing", "extra"):
        for job, inst in tg.get(k, []):
            print(f"   {k.upper()} {job}: {inst}")

    vs = variables(args)
    report["variables"] = vs
    natural = lambda s: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", s)]  # noqa: E731
    print(f"\n== 2. model_name={vs['model_name']!r} (of {vs['models']})")
    print(f"   P instances: {' '.join(sorted(vs['prefill'], key=natural)) or '(none)'}")
    print(f"   D instances: {' '.join(sorted(vs['decode'], key=natural)) or '(none)'}")
    wrong_instances = []
    if args.inventory:
        sys.path.insert(0, os.path.join(HERE, "..", "prometheus"))
        import gen_prometheus_yml as gen  # noqa: E402

        servers, _ = gen.plan(json.load(open(args.inventory, encoding="utf-8")))
        for role in ("prefill", "decode"):
            want = {i for s in servers if s["role"] == role for i in s["instances"].values()}
            for name in sorted(want - set(vs[role]), key=natural):
                wrong_instances.append(f"{name} is in the inventory but has no vLLM metrics")
            for name in sorted(set(vs[role]) - want, key=natural):
                wrong_instances.append(f"{name} has vLLM metrics but is not in the inventory")
        wrong_instances += engine_order_problems(args, servers)
        for w in wrong_instances:
            print(f"   INSTANCE PROBLEM: {w}")
    report["instance_problems"] = wrong_instances

    dash_dir = os.path.join(HERE, "..", "grafana", "dashboards")
    paths = args.dashboard or [os.path.join(dash_dir, f) for f in ("vllm-pd-prefill.json", "vllm-pd-decode.json")]
    to = time.time()
    frm = to - args.minutes * 60
    scrape = dur_s(args.scrape_interval)
    step = max(scrape, math.ceil(args.minutes * 60 / 300))
    print(f"\n== 3. panels (last {args.minutes:g} min, step {step:g}s, $__rate_interval {max(step + scrape, 4 * scrape):g}s)")
    rows, groups = [], {}
    numbered = []
    for path in paths:
        dash = json.load(open(path, encoding="utf-8"))
        print(f"\n   -- {dash['title']} ({dash['uid']})")
        numbered = enumerate(panels_in_order(dash), 1)
        rows_for(args, vs, dash, numbered, frm, to, step, scrape, rows, groups)
    finish(report, tg, rows, groups, args)


def needs_patch(args, entry, targets):
    """True when the inventory says unpatched and every empty query of this panel reads a client or TE metric."""
    if not args.unpatched:
        return False
    empty = [t for t, q in zip(targets, entry["queries"]) if q["prometheus"] == "empty"]
    return bool(empty) and all(re.search(r"\bmooncake_", t["expr"]) for t in empty)


def rows_for(args, vs, dash, numbered, frm, to, step, scrape, rows, groups):
    for n, (group, p) in numbered:
        group = f"{dash['uid']} / {group}"
        ds = (p.get("datasource") or {}).get("type", "")
        entry = {"dashboard": dash["uid"], "n": n, "group": group, "title": p["title"], "type": p["type"], "queries": []}
        if ds == "prometheus":
            any_data, errors = False, []
            for t in p.get("targets", []):
                expr = substitute(t["expr"], vs)
                instant = bool(t.get("instant"))
                q = {"refId": t["refId"]}
                try:
                    e = builtins(expr, step, scrape, to - frm)
                    if instant:
                        r = prom(args.prometheus, "/api/v1/query", {"query": e, "time": to})
                    else:
                        r = prom(args.prometheus, "/api/v1/query_range", {"query": e, "start": frm, "end": to, "step": step})
                    q["prometheus"] = "data" if has_values(r["data"]["result"]) else "empty"
                except Exception as ex:  # noqa: BLE001
                    q["prometheus"] = "error: " + str(ex)[:200]
                    errors.append(t["refId"])
                if args.grafana:
                    try:
                        q["grafana"] = "data" if grafana_query(args, args.grafana_datasource_uid, expr, instant, frm, to, step) else "empty"
                    except Exception as ex:  # noqa: BLE001
                        q["grafana"] = "error: " + str(ex)[:200]
                        errors.append(t["refId"] + "(grafana)")
                any_data |= q["prometheus"] == "data"
                entry["queries"].append(q)
            empty = [q["refId"] for q in entry["queries"] if q["prometheus"] == "empty"]
            entry["status"] = ("error " + ",".join(errors) if errors else "NO DATA" if not any_data
                               else "partial, empty " + ",".join(empty) if empty else "data")
            if not errors and needs_patch(args, entry, p.get("targets", [])):
                entry["status"] = "needs patch"
                entry["no_data"] = not any_data
        if ds != "prometheus":
            entry["status"] = "no query"  # text panels: nothing to check
        else:
            g = groups.setdefault(group, {"panels": 0, "with_data": 0})
            g["panels"] += 1
            g["with_data"] += entry["status"] == "data"
        rows.append(entry)
        detail = " ".join(f"{q['refId']}:{q['prometheus'][:5]}" + (f"/{q['grafana'][:5]}" if "grafana" in q else "") for q in entry["queries"])
        print(f"   {n:3d} [{entry['status']:22s}] {p['title']}   {detail}")


def finish(report, tg, rows, groups, args):
    report["panels"], report["groups"] = rows, groups
    print("\n== 4. groups (panels with data / panels)")
    for gname, g in groups.items():
        print(f"   {g['with_data']:3d} / {g['panels']:<3d} {gname}")
    bad_panels = [r for r in rows if r["status"] not in ("data", "needs patch", "no query")]
    waiting = [r for r in rows if r["status"] == "needs patch"]
    if waiting:
        print("\n== 5. no data without the vllm-ascend patch (vllm_ascend_patched=false)")
        for uid in dict.fromkeys(r["dashboard"] for r in waiting):
            inside = [r for r in waiting if r["dashboard"] == uid]
            print(f"   {uid}: {len(inside)} panels")
            for r in inside:
                print(f"     {r['n']:3d} {r['title']}" + ("" if r["no_data"] else "  (some queries have data)"))
    ok = not tg["down"] and not tg.get("missing") and not bad_panels and not report["instance_problems"] and not report["cluster_problems"]
    cp = f", {len(report['cluster_problems'])} cluster problems" if report["cluster_problems"] else ""
    print("\nRESULT:", "OK" if ok else f"{len(tg['down'])} targets down, {len(tg.get('missing', []))} missing, {len(report['instance_problems'])} instance problems, {len(bad_panels)} panels without data or with errors{cp}")
    if args.json:
        json.dump(report, open(args.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
