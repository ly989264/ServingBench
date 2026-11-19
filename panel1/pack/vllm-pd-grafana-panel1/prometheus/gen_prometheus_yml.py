#!/usr/bin/env python3
"""Generate prometheus.yml for dashboards 面板一·P and 面板一·D (vLLM P/D service view) from an inventory JSON.

Usage:
    python3 gen_prometheus_yml.py inventory.json > prometheus.yml
    python3 gen_prometheus_yml.py inventory.json --list        # one line per target, for checking
    python3 gen_prometheus_yml.py inventory.json --instances   # instance name -> API server, DP index, nodes
    python3 gen_prometheus_yml.py inventory.json --api-servers # instance name and address of each API server with one instance

Standard library only (Python >= 3.8). The inventory format is described in README.md (appendix A).

The inventory gives, for each side, the list of API server addresses (the proxy's IP:PORT list) and the processes per
instance. Everything else is computed from that:
    instance name             P<n> / D<n>, n = position in the list = the instance's data_parallel_index
    machines                  the distinct hosts of the list, in order of first appearance
    global ranks              n * procs_per_instance ... n * procs_per_instance + procs_per_instance - 1
The name is the pd_instance label everywhere:
    vLLM metrics              a target label of the instance's API server (not taken from engine)
    Mooncake client and TE    a target label, from the instance of the process behind the port
a_order is the instance number, zero-padded, next to pd_instance. Prometheus sorts a result by its labels as strings and the
dashboards group by (a_order, pd_instance), so legends read D0, D1, ... D10 and not D0, D1, D10, ...
An API server with several instances is only possible with the api_servers form of README appendix B: there
metric_relabel_configs turn (api_server, engine) into pd_instance.

Jobs written (job names and labels are what the dashboards' PromQL expects):
    vllm-prefill / vllm-decode   one target per vLLM API server                       labels: role, api_server, node
    mooncake-master              Mooncake master admin port (/metrics, default 9003)  labels: node
    mooncake-client              one target per process with an embedded store client labels: role, pd_instance, node
    mooncake-te                  one target per Transfer Engine metrics port: the global TE of every process, plus the TE
                                 the store creates for itself where a node sets store_owns_te ("workers" or "all")
                                                                                      labels: role, pd_instance, node
    npu-exporter                 one target per node (MindCluster npu-exporter, 8082) labels: role, node
    node-exporter                one target per node (node_exporter, 9100)            labels: role, node
vllm_ascend_patched in the inventory says whether the vllm-ascend patch in ../patches is applied. It must be true or false.
With false, mooncake-client and mooncake-te are not written at all, so no target stays DOWN; those panels show No data.

Per-process ports follow the vllm-ascend patch in ../patches: port = base + slot, slot = global rank for workers,
scheduler_slot_offset + DP index for scheduler-side store clients. The DP of a worker is global rank // procs_per_dp
(vllm-ascend parallel_state.get_global_rank). The inventory's "retention" (default 15d) becomes
storage.tsdb.retention.time in prometheus.yml (the command-line flag is deprecated).
"""
import json
import sys

PREFIX = {"prefill": "P", "decode": "D"}
# per-side settings that apply to every machine of the side (README appendix A)
SIDE_NODE_FIELDS = ("store_owns_te", "mooncake_client_port_base", "transfer_engine_port_base", "store_transfer_engine_port_base")


def expand(spec):
    """'0-15' -> [0..15]; '0-3,8' -> [0,1,2,3,8]; [] or '' -> []."""
    if spec in (None, "", []):
        return []
    if isinstance(spec, list):
        return [int(x) for x in spec]
    out = []
    for part in str(spec).split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out


def required(obj, field, where):
    """A required inventory field, or one readable line that names it."""
    value = obj.get(field)
    if value in (None, "", []):
        raise SystemExit(f"{where}: required field {field!r} is missing (README appendix A)")
    return value


def store_owns_scope(nd):
    """store_owns_te of a node: None (the store shares the global TE), "workers" or "all" (README appendix A)."""
    value = nd.get("store_owns_te")
    if value is None or value is False:
        return None
    if value in ("workers", "all"):
        return value
    raise SystemExit(f"node {nd['node']}: store_owns_te must be \"workers\" or \"all\", got {value!r} (README appendix A)")


def q(s):
    return '"' + str(s).replace('\\', '\\\\').replace('"', '\\"') + '"'


def expand_side(role, side):
    """One side of the inventory (a list of IP:PORT and procs_per_instance) as API servers with one instance each."""
    addresses = required(side, "api_servers", role)
    if not isinstance(addresses, list) or not all(isinstance(a, str) for a in addresses):
        raise SystemExit(f"{role}: api_servers must be a list of \"IP:PORT\" strings, in the order of data_parallel_index (README appendix A)")
    procs = side.get("procs_per_instance")
    if not isinstance(procs, int) or isinstance(procs, bool) or procs < 1:
        raise SystemExit(f"{role}: procs_per_instance must be a positive integer (TP x PP x prefill-CP), got {procs!r} (README appendix A)")
    hosts = []
    servers = []
    for n, address in enumerate(addresses):
        host, _, port = address.rpartition(":")
        if not host or not port.isdigit():
            raise SystemExit(f"{role}: api_servers[{n}] must be \"IP:PORT\", got {address!r} (README appendix A)")
        if host not in hosts:
            hosts.append(host)
        node = {"node": f"{role[0]}-node-{hosts.index(host)}", "host": host, "ranks": f"{n * procs}-{(n + 1) * procs - 1}"}
        if side.get("scheduler_store_clients"):
            node["dp_indexes"] = str(n)
        node.update({k: side[k] for k in SIDE_NODE_FIELDS if k in side})
        servers.append({"name": f"{PREFIX[role]}-API-{n}", "role": role, "address": address, "procs_per_dp": procs, "nodes": [node]})
    return servers


def api_servers_of(inv):
    """The API servers of the inventory: from the prefill and decode lists, or the api_servers form of README appendix B."""
    sides = [role for role in PREFIX if role in inv]
    if "api_servers" in inv and sides:
        raise SystemExit(f"inventory: use either {' and '.join(sides)} or api_servers, not both (README appendix A)")
    if sides:
        return [srv for role in sides for srv in expand_side(role, inv[role])]
    if "api_servers" not in inv:
        raise SystemExit("inventory: required fields 'prefill' and 'decode' are missing (README appendix A)")
    return required(inv, "api_servers", "inventory")


def plan(inv):
    """Validate the API servers and give every DP its instance name, in inventory order per role."""
    sched_off = int(inv.get("scheduler_slot_offset", 1000))
    counters = {"prefill": 0, "decode": 0}
    names_seen = set()
    servers = []
    for i, srv in enumerate(api_servers_of(inv)):
        name = required(srv, "name", f"api_servers[{i}]")
        if name in names_seen:
            raise SystemExit(f"api_servers[{i}]: name {name!r} is used twice; every API server needs its own name (README appendix A)")
        names_seen.add(name)
        role = required(srv, "role", f"API server {name}")
        if role not in PREFIX:
            raise SystemExit(f"API server {name}: role must be prefill or decode, got {role!r}")
        address = required(srv, "address", f"API server {name}")
        nodes = required(srv, "nodes", f"API server {name}")
        for j, nd in enumerate(nodes):
            required(nd, "node", f"API server {name}, nodes[{j}]")
            required(nd, "host", f"API server {name}, nodes[{j}]")
        procs = srv.get("procs_per_dp")
        if not isinstance(procs, int) or isinstance(procs, bool) or procs < 1:
            raise SystemExit(f"API server {name}: procs_per_dp must be a positive integer (TP x PP x prefill-CP), got {procs!r} (README appendix A)")
        ranks = [r for nd in nodes for r in expand(nd.get("ranks"))]
        if not ranks:
            raise SystemExit(f"API server {name}: no node has ranks; every worker process needs its global rank (README appendix A)")
        if len(set(ranks)) != len(ranks):
            raise SystemExit(f"API server {name}: a global rank appears twice in ranks (README appendix A)")
        if max(ranks) >= sched_off:
            raise SystemExit(f"API server {name}: global rank {max(ranks)} is not below scheduler_slot_offset {sched_off}: worker and scheduler-side ports would collide and the DP would be wrong (README appendix D)")
        per_dp = {}
        for r in ranks:
            per_dp[r // procs] = per_dp.get(r // procs, 0) + 1
        for dp, n in sorted(per_dp.items()):
            if n != procs:
                raise SystemExit(f"API server {name}: DP {dp} has {n} ranks but procs_per_dp is {procs}; check ranks and procs_per_dp (README appendix A)")
        instances = {}
        for dp in sorted(per_dp):
            instances[dp] = f"{PREFIX[role]}{counters[role]}"
            counters[role] += 1
        servers.append({"name": name, "role": role, "address": address, "procs": procs, "nodes": nodes, "instances": instances})
    return servers, sched_off


def order_of(servers):
    """Instance name -> a_order: its number, zero-padded to the width of the largest number of its side."""
    width = {}
    for s in servers:
        for inst in s["instances"].values():
            width[s["role"]] = max(width.get(s["role"], 1), len(inst[len(PREFIX[s["role"]]):]))
    return {inst: inst[len(PREFIX[s["role"]]):].zfill(width[s["role"]]) for s in servers for inst in s["instances"].values()}


def instance_relabels(servers, role):
    """(api_server, engine) -> instance name for the API servers of one job that have several instances (README appendix B).
    An API server with one instance has pd_instance as a target label instead."""
    return [(f"{s['name']};{dp}", inst) for s in servers if s["role"] == role and len(s["instances"]) > 1 for dp, inst in s["instances"].items()]


def build(inv):
    bases = inv.get("metrics_port_bases", {})
    client_base = bases.get("mooncake_client")
    te_base = bases.get("transfer_engine")
    store_te_base = bases.get("store_transfer_engine")
    npu_port = int(inv.get("npu_exporter_port", 8082))
    node_port = int(inv.get("node_exporter_port", 9100))
    jobs = {k: [] for k in ["vllm-prefill", "vllm-decode", "mooncake-master", "mooncake-client", "mooncake-te", "npu-exporter", "node-exporter"]}
    seen_exporters = set()
    servers, sched_off = plan(inv)
    order = order_of(servers)
    patched = inv.get("vllm_ascend_patched")
    if not isinstance(patched, bool):
        raise SystemExit(f"inventory: vllm_ascend_patched must be true or false, got {patched!r} (README appendix A)")
    for srv in servers:
        role, name, procs, instances = srv["role"], srv["name"], srv["procs"], srv["instances"]
        vllm_labels = {"role": role, "api_server": name, "node": srv["nodes"][0]["node"]}
        if len(instances) == 1:
            vllm_labels["pd_instance"] = next(iter(instances.values()))
            vllm_labels["a_order"] = order[vllm_labels["pd_instance"]]
        jobs["vllm-" + role].append(([srv["address"]], vllm_labels))

        def by_instance(host, base, slots, labels):
            """Static-config groups for one port base: one group per instance, because pd_instance is a target label."""
            groups = {}
            for s in slots:
                dp = s - sched_off if s >= sched_off else s // procs
                if dp not in instances:
                    raise SystemExit(f"API server {name}: dp_indexes has DP {dp}, but no rank of this API server belongs to it (README appendix A)")
                groups.setdefault(dp, []).append(f"{host}:{int(base) + s}")
            return [(targets, dict(labels, pd_instance=instances[dp], a_order=order[instances[dp]])) for dp, targets in sorted(groups.items())]

        for nd in srv["nodes"]:
            host = nd["host"]
            labels = {"role": role, "node": nd["node"]}
            workers = expand(nd.get("ranks"))
            sched = [sched_off + d for d in expand(nd.get("dp_indexes"))]
            slots = workers + sched
            cb = nd.get("mooncake_client_port_base", client_base)
            tb = nd.get("transfer_engine_port_base", te_base)
            sb = nd.get("store_transfer_engine_port_base", store_te_base)
            if not patched:
                cb = tb = None
            scope = store_owns_scope(nd) if patched else None
            if scope and sb is None:
                raise SystemExit(f"node {nd['node']}: store_owns_te is {scope!r} but no store_transfer_engine port base is set (README appendix A)")
            if cb is not None and slots:
                jobs["mooncake-client"].extend(by_instance(host, cb, slots, labels))
            # "all": the scheduler-side store client creates its own TE too, so its process has no global TE.
            # "workers" (SSD prefetch only): it contributes no memory, stays on the keyword path, keeps the global TE.
            te_slots = workers if scope == "all" else slots
            store_slots = {"workers": workers, "all": slots}.get(scope, [])
            if tb is not None and te_slots:
                jobs["mooncake-te"].extend(by_instance(host, tb, te_slots, labels))
            if store_slots:
                jobs["mooncake-te"].extend(by_instance(host, sb, store_slots, labels))
            nl = {"role": role, "node": nd["node"]}
            for job, port in (("npu-exporter", npu_port), ("node-exporter", node_port)):
                # one machine, one exporter: several instances on it, or P and D sharing it, scrape it once
                if (job, host) not in seen_exporters:
                    seen_exporters.add((job, host))
                    jobs[job].append(([f"{host}:{port}"], nl))
    mm = inv.get("mooncake_master")
    if mm:
        jobs["mooncake-master"].append(([required(mm, "address", "mooncake_master")], {"node": mm.get("node", "mooncake-master")}))
    for job, groups in jobs.items():
        seen = set()
        for targets, _ in groups:
            for t in targets:
                if t in seen:
                    raise SystemExit(f"job {job}: target {t} appears twice: two processes would use the same port. Give the second API server on this machine its own port bases (README appendix D)")
                seen.add(t)
    return jobs


def render(inv, jobs):
    servers, _ = plan(inv)
    out = ["# Generated by gen_prometheus_yml.py from the inventory; edit the inventory, not this file.",
           "global:",
           f"  scrape_interval: {inv.get('scrape_interval', '15s')}",
           f"  scrape_timeout: {inv.get('scrape_timeout', '10s')}",
           f"  evaluation_interval: {inv.get('scrape_interval', '15s')}",
           "storage:",
           "  tsdb:",
           "    retention:",
           f"      time: {inv.get('retention', '15d')}",
           "scrape_configs:"]
    for job, groups in jobs.items():
        if not groups:
            continue
        out.append(f"  - job_name: {q(job)}")
        out.append("    metrics_path: /metrics")
        if job.startswith("mooncake-"):
            # Mooncake's HTTP servers are not prometheus_client; if a build sends no usable Content-Type,
            # Prometheus 3 would fail the scrape without this fallback.
            out.append("    fallback_scrape_protocol: PrometheusText0.0.4")
        relabels = instance_relabels(servers, job[len("vllm-"):]) if job.startswith("vllm-") else []
        if relabels:
            out.append("    metric_relabel_configs:")
            for regex, inst in relabels:
                out.append(f"      - {{source_labels: [api_server, engine], regex: {q(regex)}, target_label: pd_instance, replacement: {q(inst)}}}")
                out.append(f"      - {{source_labels: [api_server, engine], regex: {q(regex)}, target_label: a_order, replacement: {q(order_of(servers)[inst])}}}")
        out.append("    static_configs:")
        for targets, labels in groups:
            out.append("      - targets: [" + ", ".join(q(t) for t in targets) + "]")
            out.append("        labels: {" + ", ".join(f"{k}: {q(v)}" for k, v in labels.items()) + "}")
    return "\n".join(out) + "\n"


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    inv = json.load(open(sys.argv[1], encoding="utf-8"))
    jobs = build(inv)
    if "--instances" in sys.argv[2:]:
        servers, _ = plan(inv)
        for srv in servers:
            for dp, inst in srv["instances"].items():
                nodes = [nd["node"] for nd in srv["nodes"] if any(r // srv["procs"] == dp for r in expand(nd.get("ranks")))]
                print(f"{inst:12s} api_server={srv['name']} dp={dp} nodes={','.join(nodes)}")
        return
    if "--api-servers" in sys.argv[2:]:
        servers, _ = plan(inv)
        for srv in servers:
            if len(srv["instances"]) == 1:
                print(next(iter(srv["instances"].values())), srv["address"])
        return
    if "--list" in sys.argv[2:]:
        for job, groups in jobs.items():
            for targets, labels in groups:
                for t in targets:
                    print(f"{job:16s} {t:24s} " + " ".join(f"{k}={v}" for k, v in labels.items()))
        return
    sys.stdout.write(render(inv, jobs))


if __name__ == "__main__":
    main()
