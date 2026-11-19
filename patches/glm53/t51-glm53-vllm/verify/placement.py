"""Compare where added/removed lines land (enclosing def/class) between two diffs.
usage: placement.py REPO_A BASE_A HEAD_A REPO_B BASE_B HEAD_B FILE...
HEAD may be WORKTREE.
"""
import ast, subprocess, sys, collections, re
def show(repo, rev, path):
    if rev == 'WORKTREE':
        return open(f"{repo}/{path}").read()
    return subprocess.run(['git','-C',repo,'show',f'{rev}:{path}'],capture_output=True,text=True).stdout
def enclosing_map(src):
    tree = ast.parse(src)
    m = {}
    def walk(node, stack):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
                st = stack+[ch.name]
                for ln in range(ch.lineno, (ch.end_lineno or ch.lineno)+1):
                    m[ln] = '.'.join(st)
                walk(ch, st)
            else:
                walk(ch, stack)
    walk(tree, [])
    return m
def changes(repo, base, head, path):
    args = ['git','-C',repo,'diff','-U0',base] + ([] if head=='WORKTREE' else [head]) + ['--',path]
    d = subprocess.run(args,capture_output=True,text=True).stdout
    new_src = show(repo, head, path); old_src = show(repo, base, path)
    nm = enclosing_map(new_src) if new_src else {}; om = enclosing_map(old_src) if old_src else {}
    out = collections.Counter()
    oln = nln = 0
    for line in d.splitlines():
        h = re.match(r'@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
        if h: oln, nln = int(h.group(1)), int(h.group(2)); continue
        if line.startswith('+++') or line.startswith('---'): continue
        if line.startswith('+'):
            t = line[1:].strip()
            if t: out[('+', nm.get(nln,'<module>'), t)] += 1
            nln += 1
        elif line.startswith('-'):
            t = line[1:].strip()
            if t: out[('-', om.get(oln,'<module>'), t)] += 1
            oln += 1
    return out
ra, ba, ha, rb, bb, hb = sys.argv[1:7]
for f in sys.argv[7:]:
    a = changes(ra, ba, ha, f); b = changes(rb, bb, hb, f)
    only_a = a - b; only_b = b - a
    print(f"=== {f}: 01 changes {sum(a.values())}, port changes {sum(b.values())}, only-in-01 {sum(only_a.values())}, only-in-port {sum(only_b.values())}")
    for k,v in sorted(only_a.items()): print('  01  ', k[0], k[1], '|', k[2], f'x{v}' if v>1 else '')
    for k,v in sorted(only_b.items()): print('  PORT', k[0], k[1], '|', k[2], f'x{v}' if v>1 else '')
