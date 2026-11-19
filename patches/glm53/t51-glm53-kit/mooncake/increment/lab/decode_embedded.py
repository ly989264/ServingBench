# Read-only: decode the base64 (gzip) string constants of the detector without running it, for the PII scan.
import ast, base64, gzip, json, sys
src, out = sys.argv[1], sys.argv[2]
tree = ast.parse(open(src, encoding="utf-8").read())
n = 0
def dump(name, raw):
    global n
    try:
        raw = gzip.decompress(raw)
    except Exception:
        pass
    n += 1
    open("%s/%02d_%s.txt" % (out, n, name), "wb").write(raw)
    try:
        obj = json.loads(raw)
    except Exception:
        return
    def walk(o, path):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, path + [str(k)])
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, path + [str(i)])
        elif isinstance(o, str) and len(o) > 200 and set(o) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n"):
            try:
                dump("_".join(path)[-60:].replace("/", "_"), base64.b64decode(o))
            except Exception:
                pass
    walk(obj, [name])
for node in ast.walk(tree):
    if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Constant, ast.JoinedStr)) and isinstance(getattr(node.value, "value", None), str):
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        v = node.value.value
        if names and len(v) > 1000:
            try:
                dump(names[0], base64.b64decode(v))
            except Exception:
                pass
print(n, "blobs")
