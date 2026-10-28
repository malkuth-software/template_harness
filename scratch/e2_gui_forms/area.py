import json, sys, collections
q = json.load(open('e2_queue.json'))
def area(p):
    parts = p.split('/')
    if parts[0] in ('tests', 'tools', 'fuzz') or '/tests/' in p or '/fuzz/' in p or '/tools/' in p:
        return None
    if parts[0] == 'src':
        if parts[1] == 'host':
            return 'host-' + parts[2]
        return parts[1]
    return parts[0]
inv = [b for b in q if b['cls'] == 'invariant' and area(b['path'])]
c = collections.Counter(area(b['path']) for b in inv)
print(len(inv), c.most_common())
want = sys.argv[1] if len(sys.argv) > 1 else None
if want:
    for b in sorted((b for b in inv if area(b['path']) == want), key=lambda b: (b['path'], b['line'])):
        print('%s:%d [%s %s] %s | %s | %s' % (b['path'], b['line'], b['loop'], b['branch'], b['function'].split('::')[-1], b['condition'][:70], b['reason'][:60]))
