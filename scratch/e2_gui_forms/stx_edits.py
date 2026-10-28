import pathlib
import sys

root = pathlib.Path("D:/gui_forms_w3/gui_forms")

edits = {
    "stx_png/src/decoder.cpp": [(
        """    bool accept(std::span<const std::byte> data, bool unicode) noexcept {
        for (std::byte item : data) {
            const unsigned value = byte(item);
            if (value == 0) return false;
            if (!unicode) continue;
            if (remaining != 0) {""",
        """    bool accept(std::span<const std::byte> data, bool unicode) noexcept {
        if (!unicode) {
            const bool has_nul = std::find(data.begin(), data.end(), std::byte{0}) != data.end();
            return !has_nul;
        }
        for (std::byte item : data) {
            const unsigned value = byte(item);
            if (value == 0) return false;
            if (remaining != 0) {""")],
    "stx_png/src/encoder.cpp": [(
        """    const std::byte* source = input.pixels.data() + static_cast<std::size_t>(input.row_bytes) * row;
    std::size_t at = 0;
    for (std::uint32_t column = 0; column < input.width; ++column) {
        const std::byte* pixel = source + static_cast<std::size_t>(column) * 4U;
        if (input.alpha == PngSourceAlpha::opaque) {
            line[at] = pixel[red];
            line[at + 1U] = pixel[1];
            line[at + 2U] = pixel[blue];
            at += 3U;
            continue;
        }
        const unsigned alpha = std::to_integer<unsigned>(pixel[3]);
        std::array<std::byte, 3> colour{pixel[red], pixel[1], pixel[blue]};
        if (input.alpha == PngSourceAlpha::premultiplied) {
            for (std::size_t channel = 0; channel < 3U; ++channel) {
                const unsigned value = std::to_integer<unsigned>(colour[channel]);
                unsigned straight = 0U;
                if (alpha != 0U) straight = std::min(255U, (value * 255U + alpha / 2U) / alpha);
                colour[channel] = static_cast<std::byte>(straight);
            }
        }
        line[at] = colour[0];
        line[at + 1U] = colour[1];
        line[at + 2U] = colour[2];
        line[at + 3U] = pixel[3];
        at += 4U;
    }
}""",
        """    const std::byte* source = input.pixels.data() + static_cast<std::size_t>(input.row_bytes) * row;
    std::size_t at = 0;
    // An opaque source drops its fourth byte.
    if (input.alpha == PngSourceAlpha::opaque) {
        for (std::uint32_t column = 0; column < input.width; ++column) {
            const std::byte* pixel = source + static_cast<std::size_t>(column) * 4U;
            line[at] = pixel[red];
            line[at + 1U] = pixel[1];
            line[at + 2U] = pixel[blue];
            at += 3U;
        }
        return;
    }
    // A premultiplied source divides each colour channel by alpha, rounding
    // to nearest; a transparent pixel's colour is zero.
    if (input.alpha == PngSourceAlpha::premultiplied) {
        for (std::uint32_t column = 0; column < input.width; ++column) {
            const std::byte* pixel = source + static_cast<std::size_t>(column) * 4U;
            const unsigned alpha = std::to_integer<unsigned>(pixel[3]);
            std::array<std::byte, 3> colour{};
            if (alpha != 0U) {
                const std::array<std::byte, 3> stored{pixel[red], pixel[1], pixel[blue]};
                for (std::size_t channel = 0; channel < 3U; ++channel) {
                    const unsigned value = std::to_integer<unsigned>(stored[channel]);
                    const unsigned straight = std::min(255U, (value * 255U + alpha / 2U) / alpha);
                    colour[channel] = static_cast<std::byte>(straight);
                }
            }
            line[at] = colour[0];
            line[at + 1U] = colour[1];
            line[at + 2U] = colour[2];
            line[at + 3U] = pixel[3];
            at += 4U;
        }
        return;
    }
    for (std::uint32_t column = 0; column < input.width; ++column) {
        const std::byte* pixel = source + static_cast<std::size_t>(column) * 4U;
        line[at] = pixel[red];
        line[at + 1U] = pixel[1];
        line[at + 2U] = pixel[blue];
        line[at + 3U] = pixel[3];
        at += 4U;
    }
}"""), (
        """// Writes filter type `filter` of line (previous row `above`, zero for the
// first) into output, whose first byte is the filter type.
void filter_row(unsigned filter, std::span<const std::byte> line, std::span<const std::byte> above,
                std::size_t stride, std::span<std::byte> output) noexcept {
    output[0] = static_cast<std::byte>(filter);
    for (std::size_t index = 0; index < line.size(); ++index) {
        const unsigned value = std::to_integer<unsigned>(line[index]);
        const unsigned up = std::to_integer<unsigned>(above[index]);
        const unsigned left = index < stride ? 0U : std::to_integer<unsigned>(line[index - stride]);
        const unsigned corner = index < stride ? 0U : std::to_integer<unsigned>(above[index - stride]);
        unsigned predicted = 0U;
        if (filter == 1U) predicted = left;
        if (filter == 2U) predicted = up;
        if (filter == 3U) predicted = (left + up) / 2U;
        if (filter == 4U) predicted = paeth_predictor(left, up, corner);
        output[1U + index] = static_cast<std::byte>((value - predicted) & 255U);
    }
}""",
        """// One filtered byte: the source byte less its prediction, modulo 256.
std::byte filtered_byte(std::byte value, unsigned predicted) noexcept {
    const std::byte result = static_cast<std::byte>((std::to_integer<unsigned>(value) - predicted) & 255U);
    return result;
}

// Writes filter type `filter` of line (previous row `above`, zero for the
// first) into output, whose first byte is the filter type. Each filter has
// its own loops; the first pixel's bytes, [0, edge), have no left
// neighbour, so their left and corner bytes are zero.
void filter_row(unsigned filter, std::span<const std::byte> line, std::span<const std::byte> above,
                std::size_t stride, std::span<std::byte> output) noexcept {
    output[0] = static_cast<std::byte>(filter);
    const std::span<std::byte> filtered = output.subspan(1U);
    const std::size_t edge = std::min(stride, line.size());
    if (filter == 1U) {
        std::copy(line.begin(), line.begin() + static_cast<std::ptrdiff_t>(edge), filtered.begin());
        for (std::size_t index = edge; index < line.size(); ++index) {
            filtered[index] = filtered_byte(line[index], std::to_integer<unsigned>(line[index - stride]));
        }
        return;
    }
    if (filter == 2U) {
        for (std::size_t index = 0; index < line.size(); ++index) {
            filtered[index] = filtered_byte(line[index], std::to_integer<unsigned>(above[index]));
        }
        return;
    }
    if (filter == 3U) {
        for (std::size_t index = 0; index < edge; ++index) {
            filtered[index] = filtered_byte(line[index], std::to_integer<unsigned>(above[index]) / 2U);
        }
        for (std::size_t index = edge; index < line.size(); ++index) {
            const unsigned left = std::to_integer<unsigned>(line[index - stride]);
            const unsigned up = std::to_integer<unsigned>(above[index]);
            filtered[index] = filtered_byte(line[index], (left + up) / 2U);
        }
        return;
    }
    if (filter == 4U) {
        for (std::size_t index = 0; index < edge; ++index) {
            const unsigned up = std::to_integer<unsigned>(above[index]);
            filtered[index] = filtered_byte(line[index], paeth_predictor(0U, up, 0U));
        }
        for (std::size_t index = edge; index < line.size(); ++index) {
            const unsigned left = std::to_integer<unsigned>(line[index - stride]);
            const unsigned up = std::to_integer<unsigned>(above[index]);
            const unsigned corner = std::to_integer<unsigned>(above[index - stride]);
            filtered[index] = filtered_byte(line[index], paeth_predictor(left, up, corner));
        }
        return;
    }
    // Filter 0 (none) copies the row.
    std::copy(line.begin(), line.end(), filtered.begin());
}""")],
    "stx_vorbis/src/packet.cpp": [(
        """    const unsigned int frames = workspace.previous_block == 0 ? 0 : (workspace.previous_block + block) / 4;
    for (unsigned int channel = 0; channel < channels; ++channel) {""",
        """    const unsigned int frames = workspace.previous_block == 0 ? 0 : (workspace.previous_block + block) / 4;
    // The packet's block size selects one of the setup's two transforms.
    const unsigned int transform = mode.large ? 1 : 0;
    for (unsigned int channel = 0; channel < channels; ++channel) {"""), (
        """            workspace.synthesize(workspace, setup, mode.large ? 1 : 0, spectrum, time);""",
        """            workspace.synthesize(workspace, setup, transform, spectrum, time);""")],
}

failed = 0
for name, pairs in edits.items():
    path = root / name
    data = path.read_bytes().decode("utf-8")
    crlf = "\r\n" in data
    text = data.replace("\r\n", "\n")
    for old, new in pairs:
        count = text.count(old)
        if count != 1:
            print("MISS", name, count, old[:70])
            failed += 1
            continue
        text = text.replace(old, new)
    if crlf:
        text = text.replace("\n", "\r\n")
    path.write_bytes(text.encode("utf-8"))
    print("ok", name)
sys.exit(1 if failed else 0)
