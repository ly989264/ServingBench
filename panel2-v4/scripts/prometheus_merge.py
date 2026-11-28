#!/usr/bin/env python3
"""Adds or removes the 面板二 block in the cluster's prometheus.yml (panel 1's Prometheus), and writes the alert rules.

Settings: the cluster folder's clickhouse.env (CLICKHOUSE_ENV, else the pack folder's): INGEST_JOB, INGEST_STATUS_PORT,
MONITOR_HOST, RULES_FILE, RULES_GROUP, PROMETHEUS_MARK, ALERT_STALE_SECONDS. Nothing is derived from a deployment name.

The block sits between BEGIN and END markers, so running it again replaces it and nothing else changes. The file is
rewritten in place (same inode) because it is bind-mounted into the Prometheus container; a backup is written next to it
the first time. `rules --out F` writes prometheus/pd-ingest-rules.yml with the cluster's group, job and threshold.

  prometheus_merge.py rules --out <file>
  prometheus_merge.py add|remove --file <prometheus.yml>
"""
import argparse
import os
import pathlib
import re
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from segdef import read_env  # noqa: E402

PACK = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = PACK / "prometheus" / "pd-ingest-rules.yml"
T_GROUP, T_JOB = "pd-ingest", "pd-ingest"
NEED = ("INGEST_JOB", "INGEST_STATUS_PORT", "MONITOR_HOST", "RULES_FILE", "RULES_GROUP", "PROMETHEUS_MARK", "ALERT_STALE_SECONDS")


def ch_env():
    path = os.environ.get("CLICKHOUSE_ENV") or str(PACK / "clickhouse.env")
    if not os.path.exists(path):
        raise SystemExit("没有 %s：先用 bin/panel2_cluster.sh add 写出这个集群的 clickhouse.env" % path)
    env = read_env(path)
    miss = [k for k in NEED if not env.get(k)]
    if miss:
        raise SystemExit("clickhouse.env 缺 %s" % "、".join(miss))
    if not re.fullmatch(r"\d+", env["ALERT_STALE_SECONDS"]):
        raise SystemExit("clickhouse.env 的 ALERT_STALE_SECONDS=%s 不是整数（秒）" % env["ALERT_STALE_SECONDS"])
    return env


def setup(action):
    """The markers and names. remove takes PROMETHEUS_MARK from the environment when it is set there (the uninstaller of
    earlier versions passes an old folder's settings that way), else from clickhouse.env."""
    global ENV, MARK, B, E, RB, RE, RULES_PATH, JOB_BLOCK, RULES_BLOCK
    if action == "remove" and os.environ.get("PROMETHEUS_MARK"):
        ENV = {"PROMETHEUS_MARK": os.environ["PROMETHEUS_MARK"], "RULES_FILE": os.environ.get("RULES_FILE") or "-"}
    else:
        ENV = ch_env()
    MARK = ENV["PROMETHEUS_MARK"]
    B, E = "# BEGIN vllm-timing (%s)" % MARK, "# END vllm-timing (%s)" % MARK
    RB, RE = "# BEGIN vllm-timing rules (%s)" % MARK, "# END vllm-timing rules (%s)" % MARK
    # RULES_FILE is the path inside the Prometheus container (/prometheus/<name>.yml).
    RULES_PATH = ENV["RULES_FILE"] if ENV["RULES_FILE"].startswith("/") else "/prometheus/" + ENV["RULES_FILE"]
    JOB_BLOCK = re.compile(r"^%s\n.*?^%s\n" % (re.escape(B), re.escape(E)), re.S | re.M)
    RULES_BLOCK = re.compile(r"^%s\n.*?^%s\n" % (re.escape(RB), re.escape(RE)), re.S | re.M)


def rules_text():
    """The rules template with the cluster's group, job and ALERT_STALE_SECONDS."""
    text = TEMPLATE.read_text(encoding="utf-8")
    out, n = re.subn(r"^(\s*- name: )%s\s*$" % re.escape(T_GROUP), lambda m: m.group(1) + ENV["RULES_GROUP"], text, count=1, flags=re.M)
    if n != 1:
        raise SystemExit("规则模板里没有组 %s" % T_GROUP)
    out = out.replace('job="%s"' % T_JOB, 'job="%s"' % ENV["INGEST_JOB"]).replace("{ALERT_STALE_SECONDS}", ENV["ALERT_STALE_SECONDS"])
    if out.count('job="%s"' % ENV["INGEST_JOB"]) != out.count("- alert:"):
        raise SystemExit("规则模板里每条规则都要按任务名筛选")
    return out


def strip(text):
    return RULES_BLOCK.sub("", JOB_BLOCK.sub("", text))


def blocks():
    """(rule_files block, scrape job block) as `add` puts them into prometheus.yml, each ending in a newline."""
    rules = "%s\nrule_files:\n  - %s\n%s\n" % (RB, RULES_PATH, RE)
    job = ('%s\n  - job_name: "%s"\n    metrics_path: /metrics\n    static_configs:\n      - targets: ["%s:%s"]\n%s\n'
           % (B, ENV["INGEST_JOB"], ENV["MONITOR_HOST"], ENV["INGEST_STATUS_PORT"], E))
    return rules, job


def marked(text):
    """(rule_files blocks, scrape job blocks) found in text; nothing outside the markers is looked at."""
    return RULES_BLOCK.findall(text), JOB_BLOCK.findall(text)


def main():
    ap = argparse.ArgumentParser(description="面板二在 prometheus.yml 中的抓取任务和告警规则")
    ap.add_argument("action", choices=["add", "remove", "rules"])
    ap.add_argument("--file")
    ap.add_argument("--out", help="rules：规则文件写到哪里")
    args = ap.parse_args()
    setup(args.action)
    if args.action == "rules":
        if not args.out:
            raise SystemExit("rules 要 --out")
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(rules_text())
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
        rules, job = blocks()
        text = re.sub(r"^scrape_configs:", lambda m: rules + "scrape_configs:", text, count=1, flags=re.M)
        text = re.sub(r"^scrape_configs:\s*\n", lambda m: m.group(0) + job, text, count=1, flags=re.M)
    if text == old:
        print("prometheus.yml 没有变化")
        return 0
    backup = args.file + ".before-panel2"
    if args.action == "add" and not os.path.exists(backup):
        shutil.copy2(args.file, backup)
        print("已备份到 %s" % backup)
    with open(args.file, "r+") as f:
        f.write(text)
        f.truncate()
    print("prometheus.yml 已%s" % ("加上入库进程的抓取任务和告警规则" if args.action == "add" else "去掉面板二的内容"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
