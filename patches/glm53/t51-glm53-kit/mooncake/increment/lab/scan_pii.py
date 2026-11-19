# Read-only scan of the extracted patch kit for personal or site-identifying strings. Prints counts per category and every hit.
import os, re, sys
root = sys.argv[1]
cats = {
    "home_or_users_dir": re.compile(r"/(home|Users)/(?!<)[A-Za-z0-9_.-]+"),
    "data_person_dir": re.compile(r"/data/[a-z]\d{8}\b|/data/(?!<|grafana\b|pd-diag\b)[a-z][a-z0-9_]{2,}/"),
    "tilde_desktop": re.compile(r"~/Desktop|~/Downloads"),
    "mooncake_wheelbase": re.compile(r"Mooncake-wheelbase", re.I),
    "github_account": re.compile(r"github\.com/(?!vllm-project|kvcache-ai|ascend|huggingface|pytorch|python)[A-Za-z0-9_-]+", re.I),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "employee_id": re.compile(r"(?<![A-Za-z0-9])[a-zA-Z]\d{8}(?![0-9])"),
    "ipv4": re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"),
    "hostname": re.compile(r"\b[a-z0-9-]+\.(?:huawei|hisilicon|corp|intra|internal|lan)\.[a-z.]+\b|\b(?:lab|dev|node)-?\d+[a-z]*\.[a-z]+\b", re.I),
}
ok_ip = re.compile(r"^(0\.30\.0|127\.0\.0\.1|0\.0\.0\.0|192\.0\.2\.\d+|198\.51\.100\.\d+|203\.0\.113\.\d+)$")
hits = {k: [] for k in cats}
for dp, dn, fn in os.walk(root):
    for f in fn:
        p = os.path.join(dp, f)
        try:
            text = open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for k, rx in cats.items():
                for m in rx.finditer(line):
                    v = m.group(0)
                    if k == "ipv4":
                        parts = v.split(".")
                        if ok_ip.match(v) or any(int(x) > 255 for x in parts) or line[max(0, m.start() - 1):m.start()] in ("v", "=") and False:
                            continue
                    hits[k].append((os.path.relpath(p, root), n, v, line.strip()[:140]))
for k, h in hits.items():
    print("%s: %d" % (k, len(h)))
    for rel, n, v, line in h[:40]:
        print("   %s:%d %s | %s" % (rel, n, v, line))
