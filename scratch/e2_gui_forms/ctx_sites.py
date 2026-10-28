import json
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
def sites(want):
    return sorted((b for b in q if b['cls'] == 'invariant' and area(b['path']) == want), key=lambda b: (b['path'], b['line']))
