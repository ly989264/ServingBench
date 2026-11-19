#!/usr/bin/env python3
"""Adds or removes the 面板二 block in the cluster's prometheus.yml, and writes the cluster's alert rules.

The block sits between BEGIN and END markers, so running it again replaces it and nothing else changes.
The file is rewritten in place (same inode) because it is bind-mounted into the Prometheus container.
A backup is written next to it the first time.
Markers, job names, the rules file and its group are the cluster's (scripts/names.py, from the environment).
`rules --out F` writes prometheus/vllm-timing-rules.yml with the cluster's group and job names and a cluster label.
"""
import argparse
import os
import pathlib
import re
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from names import Names, NamesError  # noqa: E402

PACK = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = PACK / "prometheus" / "vllm-timing-rules.yml"
# The names the template is written with.
T_GROUP, T_JOBS = "vllm-timing", {"vllm-timing-puller": "PULLER_JOB", "vllm-timing-central": "CENTRAL_JOB"}
try:
    N = Names(os.environ)
    MARK = N.get("PROMETHEUS_MARK")
except NamesError as e:
    raise SystemExit("错误：%s" % e)
B, E = "# BEGIN vllm-timing (%s)" % MARK, "# END vllm-timing (%s)" % MARK
RB, RE = "# BEGIN vllm-timing rules (%s)" % MARK, "# END vllm-timing rules (%s)" % MARK
RULES_PATH = "/prometheus/" + N.get("RULES_FILE")


def render_rules(text, group, jobs, cluster):
    """The rules template with the cluster's group name and job names, and a cluster label on every rule."""
    out, n = re.subn(r"^(\s*- name: )%s\s*$" % re.escape(T_GROUP), lambda m: m.group(1) + group, text, count=1, flags=re.M)
    if n != 1:
        raise SystemExit("规则模板里没有组 %s" % T_GROUP)
    for old, new in jobs.items():
        out = out.replace('job="%s"' % old, 'job="%s"' % new)
    out, n = re.subn(r"^(\s*labels: \{)(.*)\}\s*$", lambda m: '%s%s, cluster: "%s"}' % (m.group(1), m.group(2), cluster), out, flags=re.M)
    if n != text.count("- alert:"):
        raise SystemExit("规则模板里有 %d 条规则，只给 %d 条加上了 cluster 标签" % (text.count("- alert:"), n))
    return out


# The two marked blocks as they sit in prometheus.yml: the scrape jobs, and the rule_files entry.
JOB_BLOCK = re.compile(r"^%s\n.*?^%s\n" % (re.escape(B), re.escape(E)), re.S | re.M)
RULES_BLOCK = re.compile(r"^%s\n.*?^%s\n" % (re.escape(RB), re.escape(RE)), re.S | re.M)


def strip(text):
    text = JOB_BLOCK.sub("", text)
    return RULES_BLOCK.sub("", text)


def rules_text():
    """The cluster's rules file, as `rules --out` writes it."""
    return render_rules(TEMPLATE.read_text(encoding="utf-8"), N.get("RULES_GROUP"),
                        {old: N.get(key) for old, key in T_JOBS.items()}, N.cluster)


def blocks(puller, central):
    """(rule_files block, scrape job block) as `add` puts them into prometheus.yml, each ending in a newline."""
    rules = "%s\nrule_files:\n  - %s\n%s\n" % (RB, RULES_PATH, RE)
    job = ('%s\n  - job_name: "%s"\n    metrics_path: /metrics\n    static_configs:\n      - targets: ["%s"]\n'
           '  - job_name: "%s"\n    metrics_path: /metrics\n    static_configs:\n      - targets: ["%s:9598"]\n%s\n'
           % (B, N.get("PULLER_JOB"), puller, N.get("CENTRAL_JOB"), central, E))
    return rules, job


def marked(text):
    """(rule_files blocks, scrape job blocks) found in text: lists of the marked blocks only. Nothing outside the markers
    is returned, so panel 1's part of the file is never looked at (scripts/compare.py)."""
    return RULES_BLOCK.findall(text), JOB_BLOCK.findall(text)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["add", "remove", "rules"])
    ap.add_argument("--file")
    ap.add_argument("--out", help="rules: where to write the cluster's rules file")
    ap.add_argument("--puller", help="address of the puller's status port, reachable from Prometheus")
    ap.add_argument("--central", help="the collector container, reachable from Prometheus on the docker network")
    args = ap.parse_args()
    if args.action == "rules":
        text = rules_text()
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        return 0
    if not args.file:
        raise SystemExit("add 和 remove 要 --file")
    with open(args.file) as f:
        old = f.read()
    text = strip(old)
    if args.action == "add":
        if not re.search(r"^scrape_configs:\s*$", text, re.M):
            raise SystemExit("%s 里没有 scrape_configs:" % args.file)
        if re.search(r"^rule_files:", text, re.M):
            raise SystemExit("%s 里已经有 rule_files:，请手工把 %s 加进去" % (args.file, RULES_PATH))
        rules, job = blocks(args.puller, args.central)
        text = re.sub(r"^scrape_configs:", lambda m: rules + "scrape_configs:", text, count=1, flags=re.M)
        # scrape_configs must stay the block this list belongs to, so insert right after its header.
        text = re.sub(r"^scrape_configs:\s*\n", lambda m: m.group(0) + job, text, count=1, flags=re.M)
    if text == old:
        print("prometheus.yml 没有变化")
        return 0
    backup = args.file + ".before-panel2"
    if args.action == "add" and not os.path.exists(backup):
        shutil.copy2(args.file, backup)
        print("已备份到 %s" % backup)
    # Write in place: replacing the file would leave the container reading the old inode.
    with open(args.file, "r+") as f:
        f.write(text)
        f.truncate()
    print("prometheus.yml 已%s" % ("加上面板二的抓取任务和规则" if args.action == "add" else "去掉面板二的内容"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
