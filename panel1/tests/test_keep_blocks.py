#!/usr/bin/env python3
"""Offline test (test-only): gen_cluster.py keeps panel 2's marked blocks and an existing rule_files list when it
rewrites prometheus.yml. Usage: test_keep_blocks.py <panel-1 pack dir>"""
import json
import os
import re
import subprocess
import sys

PACK = os.path.abspath(sys.argv[1])
sys.path.insert(0, os.path.join(PACK, "scripts"))
sys.path.insert(0, os.path.join(PACK, "prometheus"))
import gen_cluster as gc  # noqa: E402
import gen_prometheus_yml as gen  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print("%s %s %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)


inv = json.load(open(os.path.join(PACK, "prometheus", "inventory.example.json"), encoding="utf-8"))
g1 = gen.render(inv, gen.build(inv))
inv2 = json.loads(json.dumps(inv))
inv2["decode"]["api_servers"] = inv2["decode"]["api_servers"][:-1]
g2 = gen.render(inv2, gen.build(inv2))


def panel2(text, mark, rules):
    """What panel 2's prometheus_merge.py add writes (same layout: rules before scrape_configs:, jobs right after)."""
    rb = "# BEGIN vllm-timing rules (%s)\nrule_files:\n  - /prometheus/%s\n# END vllm-timing rules (%s)\n" % (mark, rules, mark)
    jb = ('# BEGIN vllm-timing (%s)\n  - job_name: "vllm-timing-puller-x"\n    metrics_path: /metrics\n    static_configs:\n'
          '      - targets: ["p2puller:9598"]\n# END vllm-timing (%s)\n' % (mark, mark))
    text = re.sub(r"^scrape_configs:", lambda m: rb + "scrape_configs:", text, count=1, flags=re.M)
    return re.sub(r"^scrape_configs:\s*\n", lambda m: m.group(0) + jb, text, count=1, flags=re.M), rb, jb


for mark, rules in (("面板二", "vllm-timing-rules.yml"), ("面板二 c1", "vllm-timing-rules-c1.yml")):
    old, rb, jb = panel2(g1, mark, rules)
    out, n = gc.keep_existing(old, g1)
    check("markers (%s), same inventory: file unchanged byte for byte" % mark, out == old and n == 2)
    out, n = gc.keep_existing(old, g2)
    want, _, _ = panel2(g2, mark, rules)
    check("markers (%s), changed inventory: both blocks verbatim, in place; rest = new output" % mark,
          out == want and rb in out and jb in out and gc.PANEL2_BLOCK.sub("", out) == g2)
out, n = gc.keep_existing(g1, g2)
check("no blocks: plain generated output", out == g2 and n == 0)
hand = g1.replace("scrape_configs:", "rule_files:\n  - /prometheus/site-rules.yml\n\nscrape_configs:", 1)
out, n = gc.keep_existing(hand, g2)
check("rule_files written outside the markers: kept", "rule_files:\n  - /prometheus/site-rules.yml\n" in out
      and out.replace("rule_files:\n  - /prometheus/site-rules.yml\n\n", "") == g2)
both, rb, jb = panel2(g1, "面板二 c1", "vllm-timing-rules-c1.yml")
out, _ = gc.keep_existing(both, g2)
r = subprocess.run(["docker", "run", "--rm", "-i", "--entrypoint", "sh", "prom/prometheus:v3.11.2", "-c",
                    "mkdir -p /tmp/p && cat > /tmp/p/prometheus.yml && echo 'groups: []' > /prometheus/vllm-timing-rules-c1.yml"
                    " && promtool check config /tmp/p/prometheus.yml"], input=out.encode(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
check("result accepted by promtool (rule file present)", r.returncode == 0, r.stdout.decode()[-120:].strip())
n_fail = results.count(False)
print("RESULT: %d checks, %d failed" % (len(results), n_fail))
sys.exit(1 if n_fail else 0)
