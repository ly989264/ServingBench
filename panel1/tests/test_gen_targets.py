#!/usr/bin/env python3
"""Check gen_prometheus_yml.py: names, labels, ranks, ports and targets are computed from the inventory (test-only).

Usage: python3 test_gen_targets.py <gen_prometheus_yml.py> <inventory.example.json>
"""
import json
import os
import subprocess
import sys
import tempfile

GEN, EXAMPLE = sys.argv[1:3]
fails = 0


def check(name, ok):
    global fails
    fails += not ok
    print(f"{'OK  ' if ok else 'FAIL'} {name}")


def run(inv, *flags):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(inv, f)
    try:
        return subprocess.run([sys.executable, "-I", GEN, f.name, *flags], capture_output=True, text=True)
    finally:
        os.unlink(f.name)


def listing(inv):
    r = run(inv, "--list")
    assert r.returncode == 0, r.stderr
    return [line.split() for line in r.stdout.splitlines()]


def label(row, key):
    return next((f[len(key) + 1:] for f in row if f.startswith(key + "=")), None)


def names(inv):
    r = run(inv, "--instances")
    assert r.returncode == 0, r.stderr
    return [line.split()[0] for line in r.stdout.splitlines()]


def refused(inv, text):
    r = run(inv, "--list")
    return r.returncode != 0 and text in r.stderr and "Traceback" not in r.stderr


def copy():
    return json.loads(json.dumps(EX))


def seq(tag, n):
    return [f"{tag}{i}" for i in range(n)]


def by_job(rows, job):
    return {r[1]: r for r in rows if r[0] == job}


EX = json.load(open(EXAMPLE, encoding="utf-8"))
ALL_NAMES = seq("P", 8) + seq("D", 64)

# the example as shipped: no patch, so no per-process target at all
check("example ships with vllm_ascend_patched false", EX["vllm_ascend_patched"] is False)
off = listing(EX)
check("no patch: 72 API servers + master + 12 + 12 = 97 targets", len(off) == 97)
check("no patch: no client or TE job", not [r for r in off if r[0] in ("mooncake-client", "mooncake-te")])
check("no patch: names are P0-7 and D0-63", names(EX) == ALL_NAMES)
check("no patch: store_owns_te is ignored", (lambda i: (i["prefill"].update(store_owns_te="all"), len(listing(i)))[1])(copy()) == 97)
for bad in (None, "false", 1):
    inv = copy()
    inv["vllm_ascend_patched"] = bad
    check(f"vllm_ascend_patched={bad!r} is refused", refused(inv, "vllm_ascend_patched must be true or false"))
inv = copy()
del inv["vllm_ascend_patched"]
check("vllm_ascend_patched missing is refused", refused(inv, "vllm_ascend_patched must be true or false"))

# every instance's name is a static label on its own API server, with no use of engine
vllm = by_job(off, "vllm-prefill") | by_job(off, "vllm-decode")
check("P3 is a target label of the 4th P address", label(vllm["10.0.1.4:9000"], "pd_instance") == "P3")
check("D63 is a target label of the last D address", label(vllm["10.0.2.4:9015"], "pd_instance") == "D63")
check("D17 is the 2nd address on the 2nd machine", label(vllm["10.0.2.2:9001"], "pd_instance") == "D17")
check("a_order pads to the width of the side: D3 is 03, D63 is 63, P3 is 3", label(vllm["10.0.2.1:9003"], "a_order") == "03" and label(vllm["10.0.2.4:9015"], "a_order") == "63" and label(vllm["10.0.1.4:9000"], "a_order") == "3")
check("no relabel rule when each API server has one instance", "metric_relabel_configs" not in run(EX).stdout)
check("--api-servers lists every name with its address", run(EX, "--api-servers").stdout.splitlines()[9] == "D1 10.0.2.1:9001")
check("machines come from the addresses: 8 + 4 node_exporters", len(by_job(off, "node-exporter")) == 12)
check("D machines are named in order of first appearance", label(by_job(off, "node-exporter")["10.0.2.3:9100"], "node") == "d-node-2")

EX["vllm_ascend_patched"] = True
rows = listing(EX)
te = by_job(rows, "mooncake-te")
client = by_job(rows, "mooncake-client")
check("patched: 97 + 200 TE + 200 client = 497 targets", len(rows) == 497 and len(te) == 200 and len(client) == 200)
check("rank 16k..16k+15 belong to Pk: 16 and 31 on the 2nd machine are P1", label(te["10.0.1.2:20016"], "pd_instance") == "P1" and label(te["10.0.1.2:20031"], "pd_instance") == "P1")
check("rank 127 on the 8th machine is P7", label(client["10.0.1.8:22127"], "pd_instance") == "P7")
check("scheduler-side slot 1000 + 7 is P7 on the 8th machine", label(te["10.0.1.8:21007"], "pd_instance") == "P7")
check("D has no scheduler-side client in the example", not [t for t in te if t.startswith("10.0.2.") and int(t.split(":")[1]) >= 21000])
check("D: rank 0 on the 1st machine is D0", label(te["10.0.2.1:20000"], "pd_instance") == "D0")
check("D: rank 16 is on the 2nd machine and is D16", label(te["10.0.2.2:20016"], "pd_instance") == "D16")
check("D: rank 63 on the 4th machine is D63", label(te["10.0.2.4:20063"], "pd_instance") == "D63")
check("Client and TE targets carry a_order too", label(te["10.0.2.1:20003"], "a_order") == "03" and label(client["10.0.1.8:22127"], "a_order") == "7")
check("no dp or engine label on the targets", " dp=" not in " ".join(" ".join(r) for r in rows))


# other combinations: nothing is tied to one topology
def side(addresses, procs, **extra):
    return dict({"procs_per_instance": procs, "api_servers": addresses}, **extra)


# DP=16 on 8 machines, 2 P on each
two = copy()
two["prefill"] = side([f"10.0.1.{m}:{9000 + j}" for m in range(1, 9) for j in range(2)], 8, scheduler_store_clients=True)
check("8 machines, 2 P on each: P0-15", names(two)[:16] == seq("P", 16) and names(two)[16:] == seq("D", 64))
rows = listing(two)
te = by_job(rows, "mooncake-te")
check("8 machines, 2 P on each: P3 is the 2nd address on the 2nd machine", label(by_job(rows, "vllm-prefill")["10.0.1.2:9001"], "pd_instance") == "P3")
check("8 machines, 2 P on each: its ranks are 24-31", label(te["10.0.1.2:20024"], "pd_instance") == "P3" and label(te["10.0.1.2:20031"], "pd_instance") == "P3")
check("8 machines, 2 P on each: its scheduler-side slot is 1003", label(te["10.0.1.2:21003"], "pd_instance") == "P3")
check("8 machines, 2 P on each: 8 node_exporters on P", len([t for t in by_job(rows, "node-exporter") if t.startswith("10.0.1.")]) == 8)

# D: other distributions
for machines, each, procs in ((2, 32, 1), (4, 4, 4), (1, 8, 2), (8, 1, 16)):
    inv = copy()
    inv["decode"] = side([f"10.0.2.{m}:{9000 + j}" for m in range(1, machines + 1) for j in range(each)], procs)
    total = machines * each
    rows = listing(inv)
    te = by_job(rows, "mooncake-te")
    last = f"10.0.2.{machines}:{20000 + total * procs - 1}"
    check(f"D {machines} machines x {each} x TP{procs}: D0-{total - 1}, {machines} machines, last rank on the last machine",
          names(inv)[8:] == seq("D", total)
          and len([t for t in by_job(rows, "node-exporter") if t.startswith("10.0.2.")]) == machines
          and label(te[last], "pd_instance") == f"D{total - 1}")

# a machine with a different number of instances, and the list's order decides the name
inv = copy()
inv["decode"] = side(["10.0.2.9:9000", "10.0.2.1:9000", "10.0.2.1:9001"], 2)
rows = listing(inv)
check("uneven machines: names follow the list", names(inv)[8:] == ["D0", "D1", "D2"])
check("the first address in the list is D0, whichever host it is", label(by_job(rows, "vllm-decode")["10.0.2.9:9000"], "pd_instance") == "D0")
check("uneven machines: ranks 2-5 on 10.0.2.1", {t for t in by_job(rows, "mooncake-te") if t.startswith("10.0.2.1:")} == {f"10.0.2.1:{20000 + r}" for r in (2, 3, 4, 5)})

# P and D sharing a machine: one exporter target
inv = copy()
inv["prefill"] = side(["10.0.9.1:9000"], 16, transfer_engine_port_base=30000, mooncake_client_port_base=32000)
inv["decode"] = side(["10.0.9.1:9001"], 1)
rows = listing(inv)
check("P and D on one machine: one npu-exporter target", len(by_job(rows, "npu-exporter")) == 1)
inv["prefill"] = side(["10.0.9.1:9000"], 16)
check("P and D on one machine with the same port bases are refused", refused(inv, "appears twice"))

# appendix B form: one API server with all instances, engine maps to the name
form = copy()
for k in ("prefill", "decode"):
    del form[k]
form["api_servers"] = [
    {"name": "P-API-0", "role": "prefill", "address": "10.0.1.1:9000", "procs_per_dp": 16, "nodes": [{"node": f"p-node-{i}", "host": f"10.0.1.{i + 1}", "ranks": f"{16 * i}-{16 * i + 15}", "dp_indexes": str(i)} for i in range(8)]},
    {"name": "D-API-0", "role": "decode", "address": "10.0.2.1:9000", "procs_per_dp": 1, "nodes": [{"node": f"d-node-{i}", "host": f"10.0.2.{i + 1}", "ranks": f"{16 * i}-{16 * i + 15}"} for i in range(4)]},
]
check("one API server with all instances: same names", names(form) == ALL_NAMES)
yml = run(form).stdout
check("one API server with all instances: a relabel rule per instance", yml.count("target_label: pd_instance") == 72 and yml.count("target_label: a_order") == 72)
check("one API server with all instances: D-API-0 engine 3 sorts as 03", 'regex: "D-API-0;3", target_label: a_order, replacement: "03"' in yml)
check("one API server with all instances: D-API-0 engine 63 becomes D63", 'regex: "D-API-0;63", target_label: pd_instance, replacement: "D63"' in yml)
check("one API server with all instances: same 497 - 70 targets", len(listing(form)) == 427)
mixed = copy()
del mixed["decode"]
mixed["api_servers"] = form["api_servers"]
check("both forms at once are refused", refused(mixed, "not both"))


# store_owns_te scopes, on the 2nd P (ranks 16-31, slot 1001)
def scoped(scope, **extra):
    inv = copy()
    inv["prefill"].update(store_owns_te=scope, **extra)
    return {r[1] for r in listing(inv) if r[0] == "mooncake-te" and r[1].startswith("10.0.1.2:")}


w = set(f"10.0.1.2:{p}" for p in range(20016, 20032))
s_ = set(f"10.0.1.2:{p}" for p in range(24016, 24032))
check('store_owns_te "all": no 21001, has 25001', scoped("all") == w | s_ | {"10.0.1.2:25001"})
check('store_owns_te "workers": has 21001, no 25001', scoped("workers") == w | s_ | {"10.0.1.2:21001"})
check('"all" with transfer_engine_port_base null: only the store TE', scoped("all", transfer_engine_port_base=None) == s_ | {"10.0.1.2:25001"})
inv = copy()
inv["decode"]["mooncake_client_port_base"] = None
check("D without a KV pool: no client target on D", not [r for r in listing(inv) if r[0] == "mooncake-client" and r[1].startswith("10.0.2.")])

# errors: one readable line, no traceback
for bad in (True, "scheduler"):
    inv = copy()
    inv["prefill"]["store_owns_te"] = bad
    check(f"store_owns_te={bad!r} is refused", refused(inv, "store_owns_te must be"))
inv = copy()
inv["metrics_port_bases"]["store_transfer_engine"] = None
inv["prefill"]["store_owns_te"] = "all"
check("store_owns_te without a store TE base is refused", refused(inv, "no store_transfer_engine port base"))
for bad in (None, 0, "16"):
    inv = copy()
    inv["prefill"]["procs_per_instance"] = bad
    check(f"procs_per_instance={bad!r} is refused", refused(inv, "procs_per_instance must be"))
for bad in ("10.0.1.1", ":9000", "10.0.1.1:http"):
    inv = copy()
    inv["prefill"]["api_servers"][0] = bad
    check(f"address {bad!r} is refused", refused(inv, 'must be "IP:PORT"'))
inv = copy()
inv["prefill"]["api_servers"] = "10.0.1.1:9000"
check("api_servers that is not a list is refused", refused(inv, "must be a list"))
inv = copy()
inv["decode"]["api_servers"][1] = inv["decode"]["api_servers"][0]
check("one address listed twice is refused", refused(inv, "appears twice"))
inv = copy()
inv["decode"]["procs_per_instance"] = 16
check("rank 1023 (not below scheduler_slot_offset 1000) is refused", refused(inv, "not below scheduler_slot_offset"))
inv = copy()
del inv["prefill"], inv["decode"]
check("no side at all: readable error", refused(inv, "'prefill' and 'decode'"))
for field in ("api_servers",):
    inv = copy()
    del inv["decode"][field]
    check(f"missing {field!r}: readable error", refused(inv, repr(field)))
inv = copy()
del inv["mooncake_master"]["address"]
check("missing 'address' of the master: readable error", refused(inv, "'address'"))

print(f"RESULT: {'OK' if not fails else str(fails) + ' failures'}")
sys.exit(1 if fails else 0)
