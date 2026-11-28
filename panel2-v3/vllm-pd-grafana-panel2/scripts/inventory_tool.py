#!/usr/bin/env python3
"""Reads inventory.json. Instance names come from list position: P0.., D0... Nothing is hard-coded."""
import argparse
import json
import sys


def load(path):
    with open(path) as f:
        inv = json.load(f)
    for side in ("prefill", "decode"):
        servers = inv.get(side, {}).get("api_servers")
        if not servers:
            raise SystemExit("inventory: %s.api_servers is empty" % side)
        for a in servers:
            if ":" not in a:
                raise SystemExit("inventory: %s is not IP:PORT" % a)
        if len(set(servers)) != len(servers):
            raise SystemExit("inventory: duplicate address in %s.api_servers" % side)
    # The proxy is optional: without the proxy patch it writes nothing this pack collects.
    return inv


def node_machines(inv):
    """Every P and D machine once, in the order P, D."""
    seen = []
    for host in [a.rsplit(":", 1)[0] for a in inv["prefill"]["api_servers"] + inv["decode"]["api_servers"]]:
        if host not in seen:
            seen.append(host)
    return seen


def machines(inv):
    """node_machines, plus the proxy when the inventory names one."""
    proxy = (inv.get("proxy") or {}).get("host")
    return ([proxy] if proxy else []) + [h for h in node_machines(inv) if h != proxy]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("inventory")
    ap.add_argument("command", choices=["csv", "machines", "summary", "names", "has"])
    ap.add_argument("host", nargs="?")
    args = ap.parse_args()
    inv = load(args.inventory)
    p, d = inv["prefill"]["api_servers"], inv["decode"]["api_servers"]
    if args.command == "csv":
        print("addr,name")
        for i, a in enumerate(p):
            print("%s,P%d" % (a, i))
        for i, a in enumerate(d):
            print("%s,D%d" % (a, i))
    elif args.command == "machines":
        print("\n".join(machines(inv)))
    elif args.command == "names":
        print(" ".join(["P%d" % i for i in range(len(p))] + ["D%d" % i for i in range(len(d))]))
    elif args.command == "summary":
        print("proxy: %s" % ((inv.get("proxy") or {}).get("host") or "（清单里没有，这一版不采 proxy）"))
        print("P: %d 个实例（P0–P%d），%d 台机器" % (len(p), len(p) - 1, len({a.rsplit(':', 1)[0] for a in p})))
        print("D: %d 个实例（D0–D%d），%d 台机器" % (len(d), len(d) - 1, len({a.rsplit(':', 1)[0] for a in d})))
        print("机器总数: %d" % len(machines(inv)))
    elif args.command == "has":
        if args.host not in machines(inv):
            print("清单里没有 %s。清单里的机器：%s" % (args.host, " ".join(machines(inv))), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
