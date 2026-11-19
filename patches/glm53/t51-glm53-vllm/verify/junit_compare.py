import sys, glob, os, xml.etree.ElementTree as ET, collections
def load(d):
    res = {}
    for f in glob.glob(os.path.join(d, '*.xml')):
        for tc in ET.parse(f).getroot().iter('testcase'):
            name = tc.get('classname','').replace('tests.','') + '::' + tc.get('name')
            st, msg = 'passed', ''
            for ch in tc:
                if ch.tag in ('failure','error'): st, msg = ch.tag, (ch.get('message') or '').split('\n')[0][:110]
                elif ch.tag == 'skipped': st, msg = 'skipped', (ch.get('message') or '')[:80]
            res[name] = (st, msg)
    return res
B = sys.argv[1]
V = {v: load(os.path.join(B, v)) for v in ('v030','v030port','v032','v032p')}
print("== patch changes a result vs its own clean base")
for a, b in (('v030','v030port'), ('v032','v032p')):
    for n in sorted(set(V[a]) & set(V[b])):
        if V[a][n][0] != V[b][n][0]:
            print(f"  {a}->{b}: {n}: {V[a][n][0]} -> {V[b][n][0]} | {V[b][n][1]}")
print("== tests that exist only with the patch (01-added): port vs 01")
added = (set(V['v030port']) - set(V['v030'])) | (set(V['v032p']) - set(V['v032']))
summary = collections.Counter()
for n in sorted(added):
    p, q = V['v030port'].get(n, ('absent',''))[0], V['v032p'].get(n, ('absent',''))[0]
    summary[(p, q)] += 1
    if p != 'passed' or q != 'passed':
        print(f"  port={p:8s} 01={q:8s} {n} | port: {V['v030port'].get(n,('',''))[1]} | 01: {V['v032p'].get(n,('',''))[1][:60]}")
print("  summary (port, 01):", dict(summary))
print("== upstream tests failing on clean v0.30.0 and on the port alike (environment)")
c = collections.Counter()
for n in V['v030']:
    if V['v030'][n][0] != 'passed' and V['v030port'].get(n, ('',''))[0] == V['v030'][n][0]:
        c[V['v030'][n][1][:70]] += 1
for k, v in c.most_common(): print(f"  {v:3d} x {k}")
