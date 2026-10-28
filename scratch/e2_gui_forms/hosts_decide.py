import json
q = json.load(open('hosts_now.json'))
reasons = {
    ('src/host/windows/application/windows_host.cpp', 'length_squared > 0.0'):
        ("keep", "guards a division per pixel; a hoisted reciprocal would change the rounding, and a split would duplicate the coverage body"),
    ('src/host/windows/paint_endpoint/windows_compatibility_paint_endpoint.cpp', 'sizeof(std::uintptr_t) >= sizeof(std::uint64_t)'):
        ("false_shape", "an if constexpr: the choice is made at compile time"),
    ('src/host/windows/paint_endpoint/windows_compatibility_paint_endpoint.cpp', 'publisher_stop'):
        ("false_shape", "another thread sets publisher_stop under the lock; the condition wait reloads it"),
    ('src/host/windows/win32_compat_shim.cpp', 'reverse'):
        ("keep", "the copy order for overlapping rows; one select per row, and the row copy is shared"),
    ('src/host/linux/settings/xsettings_parse.cpp', 'cursor.most_significant'):
        ("keep", "four bytes; the byte order picks each lane"),
    ('src/render/text/glyph_run/glyph_run_raster.cpp', 'placement.matrix.has_value()'):
        ("keep", "one FreeType transform per glyph, with or without the matrix; a split would duplicate the glyph body"),
    ('src/render/text/harfbuzz/harfbuzz_font_engine.cpp', 'Bounded'):
        ("false_shape", "an if constexpr on a template parameter: the choice is made at compile time"),
    ('src/audio/loop_transport/scheduler.cpp', 'phase == RequestPhase::rejected'):
        ("keep", "runs once, on the matching receipt, then returns"),
    ('demo/correspondence/correspondence_controls.cpp', '!search.empty()'):
        ("keep", "a few visible messages; a search adds its term to each item"),
    ('demo/responsive_tracks_lab.cpp', 'seam_at_right_'):
        ("keep", "a demonstration's few children; one placement select per child"),
}
out = 'D:/gui_forms_w3/gui_forms/tools/loop_branch_decisions.json'
document = json.load(open(out, encoding='utf-8'))
seen = {(d['path'], d['function'], d['condition']) for d in document['decisions']}
used = set()
for b in q:
    key = (b['path'], b['condition'])
    if b['cls'] != 'invariant' or key not in reasons:
        continue
    full = (b['path'], b['function'], b['condition'])
    used.add(key)
    if full in seen:
        continue
    seen.add(full)
    decision, reason = reasons[key]
    document['decisions'].append({'path': b['path'], 'function': b['function'], 'condition': b['condition'],
                                  'decision': decision, 'reason': reason})
print('unused', set(reasons) - used)
document['decisions'].sort(key=lambda d: (d['path'], d['function'], d['condition']))
with open(out, 'w', encoding='utf-8', newline='\n') as stream:
    json.dump(document, stream, indent=2, ensure_ascii=False)
    stream.write('\n')
print(len(document['decisions']))
