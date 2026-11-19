#!/usr/bin/env python3
"""Index every version of the in-scope vllm-ascend files in the public repository's history.

For each blob seen at an in-scope path in commits since SINCE on the origin branches: sha256 (first 16 hex)
-> [path, first commit (9 hex), commit date, branches containing that commit (main / releases/*)].
In scope: vllm_ascend/patch/**/*.py, vllm_ascend/*.py, vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py.
Usage: history_index.py REPO SINCE OUT.json
"""
import hashlib, json, subprocess, sys

repo, since, out = sys.argv[1:4]
SCOPE = ["vllm_ascend/patch", "vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py"]


def git(*args, inp=None):
    return subprocess.run(["git", "-C", repo] + list(args), input=inp, capture_output=True, check=True).stdout


def in_scope(path):
    if not path.endswith(".py"):
        return False
    if path.startswith("vllm_ascend/patch/") or path == SCOPE[1]:
        return True
    return path.startswith("vllm_ascend/") and path.count("/") == 1


branches = [b.strip() for b in git("branch", "-r", "--format=%(refname:short)").decode().split() if b.startswith(("origin/main", "origin/releases/"))]
# commits oldest first, every origin branch
log = git("log", "--remotes=origin", "--since=" + since, "--reverse", "--format=C %H %cs", "--raw", "--no-abbrev", "--no-renames", "--",
          "vllm_ascend/patch", "vllm_ascend/*.py", SCOPE[1]).decode()
first = {}  # blob -> (path, commit, date)
commit = date = None
for line in log.splitlines():
    if line.startswith("C "):
        _, commit, date = line.split()
    elif line.startswith(":"):
        meta, path = line.split("\t", 1)
        blob = meta.split()[3]
        if blob != "0" * 40 and in_scope(path) and blob not in first:
            first[blob] = (path, commit, date)
# the starting state (oldest commit in range and d4ad49796) counts too
starts = [c for c in [git("rev-list", "--remotes=origin", "--since=" + since, "--reverse", "--max-count=100000").decode().split()[:1], ["d4ad49796"]] if c]
for c in [x[0] for x in starts]:
    for line in git("ls-tree", "-r", c, "--", "vllm_ascend").decode().splitlines():
        meta, path = line.split("\t", 1)
        blob = meta.split()[2]
        if in_scope(path) and blob not in first:
            d = git("log", "-1", "--format=%cs", c).decode().strip()
            first[blob] = (path, git("rev-parse", c).decode().strip(), d)
# branch membership of the introducing commits
member = {}
for b in branches:
    revs = set(git("rev-list", b, "--since=" + since).decode().split())
    revs.add(git("rev-parse", b).decode().strip())
    member[b] = revs
index = {}
batch = "".join(b + "\n" for b in first).encode()
raw = git("cat-file", "--batch", inp=batch)
pos = 0
for blob in first:
    hdr_end = raw.index(b"\n", pos)
    size = int(raw[pos:hdr_end].split()[2])
    data = raw[hdr_end + 1 : hdr_end + 1 + size]
    pos = hdr_end + 1 + size + 1
    path, c, d = first[blob]
    bs = sorted(b.replace("origin/", "").replace("releases/", "") for b, revs in member.items() if c in revs)
    index.setdefault(hashlib.sha256(data).hexdigest()[:16], [path, c[:9], d, bs[:3]])
json.dump(index, open(out, "w"), sort_keys=True)
print("blobs", len(first), "index", len(index), "branches", len(branches))
