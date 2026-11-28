#!/usr/bin/env python3
"""Back up and restore the files of an installed panel 1 (bin/panel1_backup.sh, bin/panel1_restore.sh).

    panel1_files.py backup  <panel-1 folder> [-o BACKUP_DIR] [--prometheus-container NAME]
    panel1_files.py restore <BACKUP_DIR> [--grafana-url URL] [--prometheus-url URL]

backup copies the files themselves (not the probe's export, whose copies have passwords replaced by <secret>):
    grafana/dashboards/*.json, grafana/provisioning/dashboards/*.y*ml, grafana/provisioning/datasources/*.y*ml,
    prometheus/prometheus.yml, prometheus/inventory.json, and, with --prometheus-container, every rule file that
    prometheus.yml lists (read from the container with docker cp, nothing is changed there).
It writes MANIFEST.json (where each file came from, size, sha256, mode) and SHA256SUMS. The folder is mode 700.
restore writes back every file whose content differs from the backup, in place (same inode: Grafana and Prometheus
have them bind-mounted), checks that every file is byte-identical to the backup, then makes the services read them:
dashboard or data source provisioning changed -> Grafana's provisioning reload API (Grafana server admin account:
GRAFANA_USER + GRAFANA_PASSWORD or GRAFANA_TOKEN); prometheus.yml or a rule file changed -> POST /-/reload when
Prometheus runs with --web.enable-lifecycle, otherwise it prints the command to run.
Files that appeared after the backup are listed, not deleted. Standard library only, Python >= 3.6.
"""
import argparse
import base64
import datetime
import glob
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request

GLOBS = ("grafana/dashboards/*.json", "grafana/provisioning/dashboards/*.y*ml", "grafana/provisioning/datasources/*.y*ml",
         "prometheus/prometheus.yml", "prometheus/inventory.json")


class Fail(Exception):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def rule_paths(yml_text):
    """The rule_files entries of prometheus.yml (container paths)."""
    out, inside = [], False
    for line in yml_text.splitlines():
        if re.match(r"^rule_files:\s*$", line):
            inside = True
            continue
        if inside:
            m = re.match(r"^\s+-\s+['\"]?([^'\"#]+?)['\"]?\s*$", line)
            if m:
                out.append(m.group(1))
            elif line.strip() and not line.lstrip().startswith("#"):
                inside = False
    return out


def docker_read(container, path):
    r = subprocess.run(["docker", "cp", "%s:%s" % (container, path), "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise Fail("docker cp %s:%s 失败：%s" % (container, path, r.stderr.decode(errors="replace").strip()))
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as t:
        for m in t.getmembers():
            if m.isfile():
                return t.extractfile(m).read(), m.mode
    raise Fail("docker cp %s:%s 没有拿到文件" % (container, path))


def docker_write(container, path, data, mode):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        info = tarfile.TarInfo(os.path.basename(path))
        info.size, info.mode = len(data), mode
        t.addfile(info, io.BytesIO(data))
    r = subprocess.run(["docker", "cp", "-", "%s:%s" % (container, os.path.dirname(path))], input=buf.getvalue(),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise Fail("docker cp 写回 %s:%s 失败：%s" % (container, path, r.stderr.decode(errors="replace").strip()))


def backup(args):
    src = os.path.abspath(args.dir)
    out = os.path.abspath(args.output or "panel1-backup-%s" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    if os.path.exists(out):
        raise Fail("%s 已存在，不覆盖" % out)
    files = sorted(set(p for g in GLOBS for p in glob.glob(os.path.join(src, g))))
    if not any(p.endswith(os.sep + "prometheus.yml") for p in files):
        raise Fail("%s 里没有 prometheus/prometheus.yml：不像面板一的安装目录" % src)
    os.makedirs(out, mode=0o700)
    os.chmod(out, 0o700)
    entries = []

    def keep(rel, data, source, mode):
        dst = os.path.join(out, "files", rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(data)
        os.chmod(dst, 0o600)
        entries.append({"file": "files/" + rel, "source": source, "size": len(data), "sha256": sha(data), "mode": mode & 0o7777})

    for p in files:
        with open(p, "rb") as f:
            data = f.read()
        keep(os.path.relpath(p, src), data, p, os.stat(p).st_mode)
    rules = rule_paths(open(os.path.join(src, "prometheus", "prometheus.yml"), encoding="utf-8").read())
    notes = []
    for r in rules:
        if args.prometheus_container:
            data, mode = docker_read(args.prometheus_container, r)
            keep("container" + r, data, "container:%s:%s" % (args.prometheus_container, r), mode)
        else:
            notes.append("prometheus.yml 引用了规则文件 %s（容器内路径），没有给 --prometheus-container，没有备份" % r)
    with open(os.path.join(out, "MANIFEST.json"), "w", encoding="utf-8") as f:
        json.dump({"format": "panel1-backup", "version": 1, "source_dir": src,
                   "created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "files": entries},
                  f, ensure_ascii=False, indent=2)
        f.write("\n")
    with open(os.path.join(out, "SHA256SUMS"), "w", encoding="utf-8") as f:
        f.write("".join("%s  %s\n" % (e["sha256"], e["file"]) for e in entries))
    for e in entries:
        print("备份 %-60s %8d 字节  %s" % (e["source"], e["size"], e["sha256"][:16]))
    for n in notes:
        print("注意：" + n)
    print("备份目录 %s：%d 个文件（只留在本机，含原始配置）" % (out, len(entries)))
    return 0


def current(entry):
    s = entry["source"]
    if s.startswith("container:"):
        _, name, path = s.split(":", 2)
        try:
            return docker_read(name, path)[0]
        except Fail:
            return None
    if not os.path.exists(s):
        return None
    with open(s, "rb") as f:
        return f.read()


def write_back(entry, data):
    s = entry["source"]
    if s.startswith("container:"):
        _, name, path = s.split(":", 2)
        docker_write(name, path, data, entry["mode"])
        return
    if os.path.exists(s):
        with open(s, "r+b") as f:  # in place: same inode
            f.write(data)
            f.truncate()
    else:
        os.makedirs(os.path.dirname(s), exist_ok=True)
        with open(s, "wb") as f:
            f.write(data)
        os.chmod(s, entry["mode"])


def post(url, auth=None):
    req = urllib.request.Request(url, method="POST", data=b"", headers={"Authorization": auth} if auth else {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:200]
    except urllib.error.URLError as e:
        return None, str(e.reason)


def grafana_auth():
    if os.environ.get("GRAFANA_TOKEN"):
        return "Bearer " + os.environ["GRAFANA_TOKEN"]
    if os.environ.get("GRAFANA_USER"):
        raw = "%s:%s" % (os.environ["GRAFANA_USER"], os.environ.get("GRAFANA_PASSWORD", ""))
        return "Basic " + base64.b64encode(raw.encode("utf-8")).decode("ascii")
    return None


def restore(args):
    bdir = os.path.abspath(args.backup)
    with open(os.path.join(bdir, "MANIFEST.json"), encoding="utf-8") as f:
        man = json.load(f)
    if man.get("format") != "panel1-backup":
        raise Fail("%s 不是 panel1_backup.sh 写的备份" % bdir)
    changed = []
    for e in man["files"]:
        with open(os.path.join(bdir, e["file"]), "rb") as f:
            data = f.read()
        if sha(data) != e["sha256"]:
            raise Fail("备份里的 %s 与 MANIFEST 记的 sha256 不同：备份本身被改过" % e["file"])
        if current(e) != data:
            write_back(e, data)
            changed.append(e["source"])
            print("恢复 %s" % e["source"])
    bad = [e["source"] for e in man["files"] if (lambda c: c is None or sha(c) != e["sha256"])(current(e))]
    if bad:
        raise Fail("恢复后仍与备份不同：%s" % "，".join(bad))
    src = man["source_dir"]
    listed = set(e["source"] for e in man["files"])
    extra = sorted(p for g in GLOBS for p in glob.glob(os.path.join(src, g)) if p not in listed)
    print("%d 个文件与备份逐字节相同（改回 %d 个）" % (len(man["files"]), len(changed)))
    for p in extra:
        print("注意：%s 是备份之后才有的，没有删" % p)
    rc = 0
    gch = [p for p in changed if "/grafana/provisioning/" in p or "/grafana/dashboards/" in p]
    for kind in ("dashboards", "datasources"):
        if not any(("/grafana/provisioning/%s/" % kind) in p or (kind == "dashboards" and "/grafana/dashboards/" in p) for p in gch):
            continue
        if not args.grafana_url:
            print("要让 Grafana 读入：curl -X POST -u <管理员> <Grafana 地址>/api/admin/provisioning/%s/reload" % kind)
            continue
        code, body = post(args.grafana_url.rstrip("/") + "/api/admin/provisioning/%s/reload" % kind, grafana_auth())
        print("Grafana 重新读取 %s 预置：HTTP %s %s" % (kind, code, body))
        rc = rc or (0 if code == 200 else 1)
    pch = [p for p in changed if p.endswith("/prometheus/prometheus.yml") or p.startswith("container:")]
    if pch:
        if args.prometheus_url:
            code, body = post(args.prometheus_url.rstrip("/") + "/-/reload")
            if code == 200:
                print("Prometheus 已热加载")
            else:
                print("Prometheus 没有开热加载（HTTP %s）：执行 docker kill -s HUP <Prometheus 容器名>，或重启它" % code)
        else:
            print("Prometheus 配置改回了：开了热加载时 curl -X POST <Prometheus 地址>/-/reload，否则 docker kill -s HUP <容器名>")
    return rc


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("backup")
    b.add_argument("dir")
    b.add_argument("-o", "--output")
    b.add_argument("--prometheus-container")
    r = sub.add_parser("restore")
    r.add_argument("backup")
    r.add_argument("--grafana-url", default=os.environ.get("GRAFANA_URL"))
    r.add_argument("--prometheus-url", default=os.environ.get("PROMETHEUS_URL"))
    args = ap.parse_args()
    try:
        if args.cmd == "backup":
            return backup(args)
        if args.cmd == "restore":
            return restore(args)
        ap.print_help()
        return 2
    except (Fail, OSError, ValueError) as e:
        print("错误：%s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
