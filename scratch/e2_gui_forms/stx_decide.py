import json

q = json.load(open('stx_after.json'))
items = q if isinstance(q, list) else q.get('branches', q)
CONSTEXPR = ("false_shape", "an if constexpr on a template parameter: the choice is made at compile time")
reasons = {
    ('stx_conv/src/plan.cpp', 'source_length_ < 5'):
        ("keep", "a basin's few cells; the short-source rule changes only the interior weight, and a split would duplicate the edge clamping"),
    ('stx_conv/src/profile.cpp', 'Accumulate'): CONSTEXPR,
    ('stx_png/src/decoder.cpp', 'ValidateText'): CONSTEXPR,
    ('stx_png/src/decoder.cpp', 'Filter == 3'): CONSTEXPR,
    ('stx_png/src/decoder.cpp', 'Filter == 4'): CONSTEXPR,
    ('stx_png/src/decoder.cpp', 'Stride * 2 < 16'): ("false_shape", "an if constexpr on the template stride: the choice is made at compile time"),
    ('stx_png/src/decoder.cpp', 'Stride * 4 < 16'): ("false_shape", "an if constexpr on the template stride: the choice is made at compile time"),
    ('stx_png/src/decoder.cpp', 'Stride * 8 < 16'): ("false_shape", "an if constexpr on the template stride: the choice is made at compile time"),
    ('stx_png/src/decoder.cpp', 'Color == 3'): CONSTEXPR,
    ('stx_png/src/decoder.cpp', 'Color == 2 || Color == 6'): CONSTEXPR,
    ('stx_png/src/decoder.cpp', 'Color == 4 || Color == 6'): CONSTEXPR,
    ('stx_png/src/decoder.cpp', 'Color == 0 && Transparent'): CONSTEXPR,
    ('stx_png/src/decoder.cpp', 'Color == 2 && Transparent'): CONSTEXPR,
    ('stx_png/src/decoder.cpp', 'chunk.type == ztxt || chunk.type == itxt'):
        ("keep", "one test per inflate step of a metadata chunk, against the inflate itself; a split would duplicate the bounded inflate loop"),
    ('stx_png/src/encoder.cpp', 'input.compression_level == 0'):
        ("keep", "one test per row, against the row's filtering and deflate; a split would duplicate the gather, deflate and swap"),
    ('stx_png/src/pixels.cpp', 'Alpha == PixelAlpha::straight'): CONSTEXPR,
    ('stx_vorbis/src/c_adapter.cpp', 'interleaved != nullptr'):
        ("keep", "one layout select per decoded packet; the per-sample copies are already separate functions"),
    ('stx_vorbis/src/c_adapter.cpp', 'one_frame'):
        ("keep", "one-frame reads leave after the first packet; the test is per packet, not per sample"),
    ('stx_vorbis/src/ogg.cpp', 'page_eos && segment_count == 0'):
        ("false_shape", "begin_page() inside the loop reads the next page and sets page_eos and segment_count"),
    ('stx_vorbis/src/ogg.cpp', 'page_eos'):
        ("false_shape", "begin_page() inside the loop reads the next page and sets page_eos"),
    ('stx_vorbis/src/packet.cpp', 'pass == 0'):
        ("keep", "the bitstream interleaves the first pass's classification words with its partitions, so the pass cannot be split from the partition loop"),
    ('stx_vorbis/src/packet.cpp', 'configuration.type == 0'):
        ("keep", "one residue-type select per partition and channel, against Huffman decoding of the partition; a split would duplicate the classification walk"),
    ('stx_vorbis/src/packet.cpp', 'configuration.type == 1'):
        ("keep", "one residue-type select per codeword, against its Huffman decode; a split would duplicate the codeword loop"),
    ('stx_vorbis/src/setup.cpp', 'lookup == 1'):
        ("keep", "setup-time codebook expansion, once per stream; the lookup type picks each dimension's multiplicand"),
    ('stx_vorbis/src/setup.cpp', 'sequence'):
        ("keep", "setup-time codebook expansion, once per stream; a sequence book carries each value into the next"),
    ('stx_vorbis/src/setup.cpp', 'synthesis == Synthesis::automatic'):
        ("keep", "two iterations, one per block size"),
    ('stx_zlib/src/deflate.cpp', 'Checked'): ("false_shape", "an if constexpr on the template parameter: the choice is made at compile time"),
    ('stx_zlib/src/deflate.cpp', 'std::endian::native == std::endian::little'): ("false_shape", "an if constexpr on the target's byte order: the choice is made at compile time"),
    ('stx_zlib/src/deflate.cpp', 'std::endian::native == std::endian::big'): ("false_shape", "an if constexpr on the target's byte order: the choice is made at compile time"),
    ('stx_zlib/src/deflate.cpp', 'flush == DeflateFlush::none'):
        ("keep", "the loop's exit: it is taken once, when the input is consumed without a flush"),
    ('stx_zlib/src/deflate.cpp', 'options_.format == DeflateFormat::zlib'):
        ("keep", "one checksum select per input copy, not per byte; the Adler-32 update runs over the whole copy"),
    ('stx_zlib/src/inflate.cpp', '!distance_alphabet'):
        ("keep", "evaluated at compile time into two static constexpr trees; there is no runtime loop"),
}
out = 'D:/gui_forms_w3/gui_forms/tools/loop_branch_decisions.json'
document = json.load(open(out, encoding='utf-8'))
seen = {(d['path'], d['function'], d['condition']) for d in document['decisions']}
used = set()
missing = []
for b in items:
    if b['cls'] != 'invariant' or b.get('decision'):
        continue
    if any(part in ('tests', 'tools', 'fuzz', 'bench') for part in b['path'].split('/')):
        continue
    key = (b['path'], b['condition'])
    if key not in reasons:
        missing.append(key)
        continue
    used.add(key)
    full = (b['path'], b['function'], b['condition'])
    if full in seen:
        continue
    seen.add(full)
    decision, reason = reasons[key]
    document['decisions'].append({'path': b['path'], 'function': b['function'], 'condition': b['condition'],
                                  'decision': decision, 'reason': reason})
document['decisions'].sort(key=lambda d: (d['path'], d['function'], d['condition']))
with open(out, 'w', encoding='utf-8', newline='\n') as stream:
    json.dump(document, stream, indent=2, ensure_ascii=False)
    stream.write('\n')
print(len(document['decisions']), 'decisions; missing', missing, '; unused', set(reasons) - used)
