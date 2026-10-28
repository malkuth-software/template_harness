import json, sys
root = 'D:/gui_forms_w3/gui_forms/'
q = json.load(open('e2_queue.json'))
want = sys.argv[1]
def area(p):
    parts = p.split('/')
    if parts[0] in ('tests', 'tools', 'fuzz') or '/tests/' in p or '/fuzz/' in p or '/tools/' in p:
        return None
    if parts[0] == 'src':
        if parts[1] == 'host':
            return 'host-' + parts[2]
        return parts[1]
    return parts[0]
sites = sorted((b for b in q if b['cls'] == 'invariant' and area(b['path']) == want), key=lambda b: (b['path'], b['line']))
cache = {}
n = 0
for b in sites:
    n += 1
    lines = cache.setdefault(b['path'], open(root + b['path'], encoding='utf-8', errors='replace').read().split('\n'))
    lo = b['loop_line']; hi = b['line']
    print('#%d %s:%d loop@%d %s | %s' % (n, b['path'], hi, lo, b['function'].split('::')[-1], b['condition']))
    if hi - lo <= 12:
        rng = range(lo - 1, min(hi + 2, len(lines)))
    else:
        rng = list(range(lo - 1, lo + 1)) + [None] + list(range(hi - 4, hi + 2))
    for i in rng:
        if i is None:
            print('      ...'); continue
        print('%5d %s' % (i + 1, lines[i].rstrip()[:150]))
    print()
